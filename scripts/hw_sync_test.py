"""Manual hardware sync test: flips the screen black/white once per second and
fires the reward valve on each flip, so a photodiode + valve trace can be used
to measure end-to-end timing. Not a pytest test — run directly:

    python scripts/hw_sync_test.py
"""

import os
import serial
from psychopy import monitors, visual, core, event

RIG_WIDTH_CM = 19.7
RIG_DISTANCE_CM = 8.0
RIG_RESOLUTION = (2048, 1536)
WIN_SIZE = (2048, 1536)

FLIP_PERIOD = 1.0


def make_monitor() -> monitors.Monitor:
    mon = monitors.Monitor("ibl_rig", width=RIG_WIDTH_CM, distance=RIG_DISTANCE_CM)
    mon.setSizePix(RIG_RESOLUTION)
    return mon


def main() -> None:
    ser = serial.Serial("/dev/ttyACM0", 2000000, timeout=0.005)
    try:
        ser.set_low_latency_mode(True)
    except (AttributeError, OSError):
        pass
    ser_fd = ser.fileno()
    core.wait(2.0)
    ser.reset_input_buffer()

    mon = make_monitor()
    win = visual.Window(
        size=WIN_SIZE,
        monitor=mon,
        units="norm",
        color=(-1, -1, -1),
        fullscr=True,
    )

    bg = visual.Rect(
        win,
        width=2,
        height=2,
        units="norm",
        fillColor=(-1, -1, -1),
        lineColor=None,
        autoLog=False,
    )
    bg.autoDraw = True

    clock = core.Clock()
    next_flip_t = FLIP_PERIOD
    is_white = False
    ser.write(b"s")  # ensure valve closed on exit

    try:
        while True:
            if "escape" in event.getKeys(keyList=["escape"]):
                break
            if clock.getTime() >= next_flip_t:
                is_white = not is_white
                bg.fillColor = (1, 1, 1) if is_white else (-1, -1, -1)
                win.callOnFlip(os.write, ser_fd, b"r")
                next_flip_t += FLIP_PERIOD
            win.flip()
    finally:
        ser.write(b"e")  # ensure valve closed on exit
        ser.close()
        win.close()
        core.quit()


if __name__ == "__main__":
    main()
