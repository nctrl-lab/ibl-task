"""Teensy I/O. See teensy/teensy.ino for the wire format."""

import socket
import sys
import threading

import serial

from ibl.config import GAIN_DEG_PER_COUNT, MAX_VALVE_MS, MOCK_DEG_PER_KEY


class Teensy:
    """Serial driver. Daemon thread reads 3-byte packets at 1 kHz and updates
    the cumulative wheel position."""

    def __init__(self, port, gain_deg_per_count=GAIN_DEG_PER_COUNT):
        self._serial = serial.Serial(port=port, timeout=0.05)
        self._lock = threading.Lock()
        self._latest_pos_ticks = 0
        self._counter_prev = None
        self._dropped_packets = 0
        self.gain_deg_per_count = gain_deg_per_count
        self.reward_duration_ms = 0
        self.error = None
        self._stop = threading.Event()
        self._first_packet = threading.Event()
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)

        self._serial.write(b"s")
        self._reader.start()
        if not self._first_packet.wait(timeout=0.5):
            self.close()
            raise RuntimeError(f"Teensy did not send any packet within 0.5s on {port}")

    def trial_on(self):
        self._serial.write(b"t")

    def trial_off(self):
        self._serial.write(b"T")

    def cue_on(self):
        self._serial.write(b"c")

    def cue_off(self):
        self._serial.write(b"C")

    def noise(self):
        self._serial.write(b"n")

    def reward(self):
        self._serial.write(b"r")

    def set_reward_duration(self, ms):
        ms = max(0, min(int(ms), MAX_VALVE_MS))
        self._serial.write(b"w" + str(ms).encode("ascii") + b"\n")
        self.reward_duration_ms = ms

    def position_deg(self):
        with self._lock:
            return self._latest_pos_ticks * self.gain_deg_per_count

    def close(self):
        self._stop.set()
        try:
            self._serial.write(b"e")
        except Exception:
            pass
        if self._reader.is_alive():
            self._reader.join(timeout=1.0)
        try:
            self._serial.close()
        except Exception:
            pass

    def _reader_loop(self):
        try:
            last_logged = 0
            state = 0
            counter = 0
            while not self._stop.is_set():
                chunk = self._serial.read(self._serial.in_waiting or 1)
                if not chunk:
                    continue
                with self._lock:
                    for b in chunk:
                        if state == 0:
                            if b == 0:
                                state = 1
                        elif state == 1:
                            if b == 0:
                                continue  # stay in state 1; b was a fresh start marker
                            counter = b
                            state = 2
                        else:
                            if b == 0:
                                state = 1  # b is a fresh start marker; drop the partial frame
                                continue
                            self._latest_pos_ticks += b - 128
                            if self._counter_prev is not None:
                                expected = (
                                    1
                                    if self._counter_prev == 255
                                    else self._counter_prev + 1
                                )
                                if counter != expected:
                                    self._dropped_packets += (counter - expected) % 255
                            self._counter_prev = counter
                            self._first_packet.set()
                            state = 0
                if self._dropped_packets > last_logged:
                    last_logged = self._dropped_packets
                    print(
                        f"[Teensy] dropped packets so far: {last_logged}",
                        file=sys.stderr,
                    )
        except Exception as exc:
            self.error = exc


class FakeTeensy:
    """Same interface as Teensy; wheel driven by arrow keys (PsychoPy)."""

    def __init__(self, gain_deg_per_count=GAIN_DEG_PER_COUNT):
        self._pos_ticks = 0
        self.gain_deg_per_count = gain_deg_per_count
        self.reward_duration_ms = 0
        self.error = None

    def trial_on(self):
        print("[mock] trial on", file=sys.stderr)

    def trial_off(self):
        print("[mock] trial off", file=sys.stderr)

    def cue_on(self):
        print("[mock] cue on (tone)", file=sys.stderr)

    def cue_off(self):
        print("[mock] cue off", file=sys.stderr)

    def noise(self):
        print("[mock] noise", file=sys.stderr)

    def reward(self):
        print(f"[mock] reward {self.reward_duration_ms} ms", file=sys.stderr)

    def set_reward_duration(self, ms):
        self.reward_duration_ms = max(0, min(int(ms), MAX_VALVE_MS))

    def position_deg(self):
        # Lazy import so unit tests don't need PsychoPy.
        try:
            from psychopy import event

            keys = event.getKeys(["left", "right"])
            for k in keys:
                tick_step = max(1, int(round(MOCK_DEG_PER_KEY / self.gain_deg_per_count)))
                self._pos_ticks += -tick_step if k == "left" else tick_step
        except Exception:
            pass
        return self._pos_ticks * self.gain_deg_per_count

    def close(self):
        pass


class BMI(Teensy):
    """Teensy + spiketag BMI overlay.

    Inherits TTL output and encoder streaming from Teensy (the Teensy still
    runs during BMI sessions for trial/cue/reward signaling and optional
    wheel input) and adds a TCP client that receives decoded position from a
    spiketag BMI process. position_deg() returns a weighted sum:

        encoder_deg * encoder_gain  +  bmi_deg * bmi_gain

    Set encoder_gain=0 for pure-BMI sessions, bmi_gain=0 to fall back to
    plain wheel control, or mix both for assisted / hybrid blocks.
    """

    def __init__(
        self,
        port,
        bmi_host,
        bmi_port,
        gain_deg_per_count=GAIN_DEG_PER_COUNT,
        encoder_gain=1.0,
        bmi_gain=1.0,
    ):
        super().__init__(port, gain_deg_per_count=gain_deg_per_count)
        self.encoder_gain = encoder_gain
        self.bmi_gain = bmi_gain
        self._bmi_pos_deg = 0.0
        self._bmi_lock = threading.Lock()
        self._bmi_stop = threading.Event()
        try:
            self._bmi_sock = socket.create_connection((bmi_host, bmi_port), timeout=1.0)
            self._bmi_sock.settimeout(0.05)
        except Exception:
            super().close()
            raise
        self._bmi_thread = threading.Thread(target=self._bmi_loop, daemon=True)
        self._bmi_thread.start()

    def position_deg(self):
        encoder_deg = super().position_deg()
        with self._bmi_lock:
            bmi_deg = self._bmi_pos_deg
        return encoder_deg * self.encoder_gain + bmi_deg * self.bmi_gain

    def _bmi_loop(self):
        # TODO: replace with the actual spiketag message format. Placeholder
        # accepts newline-delimited ASCII floats representing decoded position
        # in degrees.
        buf = b""
        try:
            while not self._bmi_stop.is_set():
                try:
                    chunk = self._bmi_sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        pos = float(line)
                    except ValueError:
                        continue
                    with self._bmi_lock:
                        self._bmi_pos_deg = pos
        except Exception as exc:
            self.error = exc

    def close(self):
        self._bmi_stop.set()
        try:
            self._bmi_sock.close()
        except Exception:
            pass
        if self._bmi_thread.is_alive():
            self._bmi_thread.join(timeout=1.0)
        super().close()
