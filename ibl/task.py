"""IBL task using PsychoPy

All hardware contact goes through the Teensy / FakeTeensy interface in ibl.io;
"""

import csv
import datetime
import json
import random
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from ibl.config import (
    COUNTER_WINDOW_TRIALS, COUNTS_PER_MM, ERROR_TIMEOUT_S, EXPANSION_ACCURACY,
    EXPANSION_MIN_TRIALS, EXPANSION_TIERS, INITIAL_CONTRASTS,
    ITI_MAX_S, ITI_MEAN_S, ITI_MIN_S,
    OPEN_LOOP_HOLD_S, ORI_DEG, QUIESCENCE_MAX_S, QUIESCENCE_MEAN_S,
    QUIESCENCE_MIN_S, QUIESCENCE_STILL_BAND_DEG, RESPONSE_WINDOW_S,
    RIG_DISTANCE_CM, RIG_RESOLUTION,
    RIG_WIDTH_CM, SF_CPD, SIZE_DEG, STIM_START_OFFSET_DEG, SYNC_PIX,
    WHEEL_GAIN_DEG_PER_MM,
)


_CSV_HEADER = [
    "trial_index", "signed_contrast", "stim_side", "response", "correct",
    "response_time_s", "t_start", "t_cue", "t_response", "t_end",
    "iti_s", "reward_ul",
]


class TrialLogger:
    def __init__(self, session_dir: Path) -> None:
        session_dir.mkdir(parents=True, exist_ok=True)
        self.session_dir = session_dir
        self._trials_file = (session_dir / "trials.csv").open("w", newline="")
        self._csv = csv.writer(self._trials_file)
        self._csv.writerow(_CSV_HEADER)
        self._trials_file.flush()
        self._frames: list[tuple[float, float, float]] = []   # (t, gabor_x, wheel_pos)

    def log_trial(self, r):
        self._csv.writerow([
            r.trial_index, r.side * r.contrast, r.side, r.response,
            int(r.correct), r.response_time_s, r.t_start,
            r.t_cue, r.t_response, r.t_end, r.iti_s, r.reward_ul,
        ])
        self._trials_file.flush()

    def log_frame(self, t: float, gabor_x: float, wheel_pos: float) -> None:
        self._frames.append((t, gabor_x, wheel_pos))

    def close(self) -> None:
        self._trials_file.close()
        if not self._frames:
            return
        ts, gx, wp = zip(*self._frames)
        ts = np.array(ts, dtype=np.float64)
        gx = np.array(gx, dtype=np.float32)
        wp = np.array(wp, dtype=np.float32)
        speed = np.zeros_like(wp)
        if len(wp) > 1:
            dt = np.diff(ts)
            speed[1:] = np.diff(wp) / np.where(dt > 0, dt, 1.0)
        np.savez_compressed(
            self.session_dir / "stream.npz",
            t=ts, gabor_x_deg=gx, wheel_pos_deg=wp, wheel_speed_deg_s=speed,
        )


@dataclass
class TrialResult:
    trial_index: int
    side: int
    contrast: float
    response: int       # -1, +1, or 0 (no-go)
    correct: bool
    response_time_s: float
    t_start: float
    t_cue: float
    t_response: float
    t_end: float
    iti_s: float
    reward_ul: float    # experimenter-measured µL delivered (0 on incorrect)


def _p_type(t: TrialResult) -> int:
    """Bucket index from a trial: (cue_side L=0/R=1) * 2 + (correct 0/1)."""
    return (1 if t.side > 0 else 0) * 2 + (1 if t.correct else 0)


class TrainingSchedule:
    """Countermeasure cue scheduler. For each of 4 buckets indexed by the
    previous trial's (cue_side, outcome), keep the last WINDOW choices and
    present Right with probability 1 − sumRight[bucket] / WINDOW — i.e. counter
    to the mouse's recent bias after that situation. Contrast is uniform over
    active_contrasts."""

    WINDOW = COUNTER_WINDOW_TRIALS

    def __init__(self, active_contrasts: list[float] | None = None,
                 expansion_tiers: tuple[float, ...] | None = None,
                 history: list[TrialResult] | None = None) -> None:
        self.active_contrasts = list(active_contrasts) if active_contrasts is not None else list(INITIAL_CONTRASTS)
        self.expansion_tiers = tuple(expansion_tiers) if expansion_tiers is not None else EXPANSION_TIERS
        # Balanced prior: each bucket starts with WINDOW/2 R + WINDOW/2 L so
        # p(R)≈0.5 until real data evicts the synthetic entries.
        prior = [i % 2 for i in range(self.WINDOW)]
        self._sum_right = [sum(prior)] * 4
        self._window: list[deque[int]] = [deque(prior, maxlen=self.WINDOW) for _ in range(4)]
        self._p_type_prev: int | None = None
        self._last: TrialResult | None = None
        self._recent: deque[tuple[float, bool]] = deque(maxlen=EXPANSION_MIN_TRIALS)
        if history:
            for i in range(2, len(history)):
                self._attribute(history[i].response, _p_type(history[i - 2]))
            if len(history) >= 2:
                self._p_type_prev = _p_type(history[-2])
            self._last = history[-1]
            for h in history:
                self._recent.append((h.contrast, h.correct))

    def _attribute(self, response: int, p: int) -> None:
        if response == 0:
            return
        bit = 1 if response > 0 else 0
        bucket = self._window[p]
        if len(bucket) == self.WINDOW:
            self._sum_right[p] += bit - bucket[0]
        else:
            self._sum_right[p] += bit
        bucket.append(bit)

    def next_trial(self):
        """Returns (side, contrast). Contrast is drawn uniformly over signed
        trial types as in the IBL protocol: each non-zero magnitude weights 2
        (it has ±c forms) and 0% weights 1, matching the paper's 2/11 vs 1/11
        distribution on the full set."""
        weights = [1.0 if c == 0 else 2.0 for c in self.active_contrasts]
        contrast = random.choices(self.active_contrasts, weights=weights, k=1)[0]
        if self._last is not None and self._p_type_prev is not None:
            self._attribute(self._last.response, self._p_type_prev)

        if self._last is None:
            return random.choice([-1, 1]), contrast

        self._p_type_prev = _p_type(self._last)
        side = 1 if random.randrange(self.WINDOW) >= self._sum_right[self._p_type_prev] else -1
        return side, contrast

    def record(self, result: TrialResult) -> None:
        self._last = result
        self._recent.append((result.contrast, result.correct))

    def maybe_expand(self) -> bool:
        if len(self._recent) < EXPANSION_MIN_TRIALS:
            return False
        relevant = [ok for c, ok in self._recent if c == max(self.active_contrasts)]
        if not relevant or sum(relevant) / len(relevant) < EXPANSION_ACCURACY:
            return False
        new = next((t for t in self.expansion_tiers if t not in self.active_contrasts), None)
        if new is None:
            return False
        self.active_contrasts.append(new)
        return True


class EscapeRequested(Exception):
    pass


def build_window(screen=0):
    """Open the PsychoPy Window, GratingStim, and photodiode-sync square."""
    from psychopy import monitors, visual

    mon = monitors.Monitor("ibl_rig", width=RIG_WIDTH_CM, distance=RIG_DISTANCE_CM)
    mon.setSizePix(RIG_RESOLUTION)  # placeholder; PsychoPy splash needs sizePix pre-Window
    win = visual.Window(monitor=mon, units="deg", color=(0, 0, 0),
                        fullscr=True, screen=screen, checkTiming=False)
    mon.setSizePix(tuple(win.size))
    gabor = visual.GratingStim(
        win=win, tex="sin", mask="gauss", units="deg",
        size=SIZE_DEG, sf=SF_CPD, ori=ORI_DEG, contrast=0.0,
        phase=0.0, pos=(0.0, 0.0), autoLog=False, autoDraw=True,
    )
    w, h = win.size
    sync = visual.Rect(
        win, width=SYNC_PIX, height=SYNC_PIX, units="pix",
        pos=(w // 2 - SYNC_PIX // 2, -h // 2 + SYNC_PIX // 2),
        fillColor="black", lineColor=None, autoLog=False, autoDraw=True,
    )
    return win, gabor, sync


def _check_escape() -> None:
    from psychopy import event
    if "escape" in event.getKeys(["escape"]):
        raise EscapeRequested()


def _wait(duration: float, flip: Callable[[], None]) -> None:
    end = time.monotonic() + duration
    while time.monotonic() < end:
        _check_escape()
        flip()


def run_trial(win, gabor, sync_sq, hw, side, contrast, trial_index,
              reward_ul, error_timeout_s,
              iti_min_s, iti_mean_s, iti_max_s, log_frame=None):
    def flip():
        win.flip()
        if log_frame is not None:
            log_frame(time.monotonic(), float(gabor.pos[0]), hw.position_deg())

    hw.trial_on()
    gabor.contrast = 0.0
    gabor.pos = (side * STIM_START_OFFSET_DEG, 0.0)
    gabor.phase = random.random()         # IBL spec: randomize phase per trial
    sync_sq.fillColor = "black"
    flip()

    # Quiescence: t = QUIESCENCE_MIN_S + x, x ~ Exp(QUIESCENCE_MEAN_S),
    # truncated to x ∈ [0, QUIESCENCE_MAX_S - QUIESCENCE_MIN_S].
    t_start = time.monotonic()
    while True:
        x = random.expovariate(1.0 / QUIESCENCE_MEAN_S)
        if x <= QUIESCENCE_MAX_S - QUIESCENCE_MIN_S:
            break
    quiescence_dur = QUIESCENCE_MIN_S + x
    band_anchor = hw.position_deg()
    band_start = time.monotonic()
    while time.monotonic() - band_start < quiescence_dur:
        _check_escape()
        pos = hw.position_deg()
        if abs(pos - band_anchor) > QUIESCENCE_STILL_BAND_DEG:
            band_anchor = pos
            band_start = time.monotonic()
        flip()

    pos_at_stim_on = hw.position_deg()
    gabor.contrast = contrast
    sync_sq.fillColor = "white"
    hw.cue_on()                           # raises CUE TTL + fires 100 ms tone
    flip()
    t_cue = time.monotonic()

    response = 0
    while time.monotonic() - t_cue < RESPONSE_WINDOW_S:
        _check_escape()
        dx = hw.position_deg() - pos_at_stim_on
        gabor.pos = (side * STIM_START_OFFSET_DEG + dx, 0.0)
        if abs(dx) >= STIM_START_OFFSET_DEG:
            # IBL convention: response = side reported by the mouse.
            # Wheel moves gabor toward 0 (sign(dx) opposite to side) ⇒ correct.
            response = side if (dx < 0) == (side > 0) else -side
            break
        flip()
    t_response = time.monotonic()
    response_time_s = t_response - t_cue
    correct = response != 0 and response == side

    if correct:
        hw.reward()
        gabor.pos = (0.0, 0.0)
        _wait(OPEN_LOOP_HOLD_S, flip)
        dispensed_ul = reward_ul
    else:
        hw.noise()
        _wait(error_timeout_s, flip)
        dispensed_ul = 0.0
    gabor.contrast = 0.0
    sync_sq.fillColor = "black"
    hw.cue_off()
    hw.trial_off()
    t_end = time.monotonic()

    iti_dur = iti_min_s
    if iti_max_s > iti_min_s and iti_mean_s > 0:
        while True:
            x = random.expovariate(1.0 / iti_mean_s)
            if x <= iti_max_s - iti_min_s:
                iti_dur = iti_min_s + x
                break
    _wait(iti_dur, flip)

    return TrialResult(
        trial_index=trial_index, side=side, contrast=contrast, response=response,
        correct=correct, response_time_s=response_time_s,
        t_start=t_start, t_cue=t_cue, t_response=t_response, t_end=t_end,
        iti_s=iti_dur, reward_ul=dispensed_ul,
    )


def run_session(hw, win, gabor, sync, *, session_dir, n_trials,
                reward_ms, reward_ul, error_timeout_s,
                iti_min_s, iti_mean_s, iti_max_s,
                auto_reward=False, calibration=None,
                active_contrasts=None, expansion_tiers=None,
                on_trial_complete=None, on_reward_set=None,
                should_stop=None):
    """Loop trials, log results. Returns 0 / 2 (hw error).

    With auto_reward, advance one tier in `calibration` per correct trial
    (capped) and reset to the smallest on wrong / no-go.
    """
    schedule = TrainingSchedule(active_contrasts=active_contrasts,
                                expansion_tiers=expansion_tiers)
    logger = TrialLogger(session_dir)
    tier = 0
    try:
        for trial_index in range(n_trials):
            if hw.error is not None:
                return 2
            if should_stop and should_stop():
                break
            if auto_reward and calibration:
                cal = calibration[tier]
                ms_now = int(cal["ms"])
                ul_now = float(cal["target_ul"])
            else:
                ms_now = reward_ms
                ul_now = reward_ul
            hw.set_reward_duration(ms_now)
            if on_reward_set:
                on_reward_set(ms_now, ul_now)
            side, contrast = schedule.next_trial()
            try:
                result = run_trial(win, gabor, sync, hw, side, contrast, trial_index,
                                   reward_ul=ul_now, error_timeout_s=error_timeout_s,
                                   iti_min_s=iti_min_s, iti_mean_s=iti_mean_s,
                                   iti_max_s=iti_max_s,
                                   log_frame=logger.log_frame)
            except EscapeRequested:
                break
            schedule.record(result)
            logger.log_trial(result)
            if on_trial_complete:
                on_trial_complete(result)
            if auto_reward and calibration:
                if result.correct:
                    tier = min(tier + 1, len(calibration) - 1)
                else:
                    tier = 0
        schedule.maybe_expand()
        return 0
    finally:
        logger.close()


def _write_summary(sd, start, end, args, results, calibration=None):
    n = len(results)
    n_correct = sum(1 for r in results if r.correct)
    n_nogo = sum(1 for r in results if r.response == 0)
    n_right = sum(1 for r in results if r.response > 0)
    n_left = sum(1 for r in results if r.response < 0)
    responded = n_right + n_left
    rts = [r.response_time_s for r in results if r.correct]

    s = {
        "subject": args.subject,
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "duration_s": (end - start).total_seconds(),
        "n_trials_target": args.n_trials,
        "auto_reward": bool(args.auto_reward),
        "reward_ms": None if args.auto_reward else args.reward_ms,
        "reward_ul": None if args.auto_reward else args.reward_ul,
        "calibration": calibration if args.auto_reward else None,
        "error_timeout_s": args.error_timeout,
        "iti_min_s": args.iti_min,
        "iti_mean_s": args.iti_mean,
        "iti_max_s": args.iti_max,
        "water_limit_ul": args.water_limit,
        "gain_deg_per_mm": args.gain,
        "contrasts": args.contrasts,
        "mock": args.mock,
        "port": None if args.mock else args.port,
        "screen": args.screen,
        "n_completed": n,
        "n_correct": n_correct,
        "n_nogo": n_nogo,
        "correct_rate": (n_correct / n) if n else None,
        "mean_rt_correct_s": (sum(rts) / len(rts)) if rts else None,
        "response_bias": ((n_right - n_left) / responded) if responded else None,
        "water_dispensed_ul": sum(r.reward_ul for r in results),
    }
    (sd / "summary.json").write_text(json.dumps(s, indent=2) + "\n")

    if s["auto_reward"]:
        tiers_ul = [c['target_ul'] for c in calibration]
        tiers_ms = [c['ms'] for c in calibration]
        reward_line = f"- Reward: auto-adjust, tiers = {tiers_ul} µL (ms = {tiers_ms})"
    else:
        reward_line = f"- Reward: {s['reward_ms']} ms / {s['reward_ul']} µL"
    pct = f"{100 * s['correct_rate']:.1f}%" if s["correct_rate"] is not None else "—"
    rt_s = f"{s['mean_rt_correct_s']:.3f} s" if s["mean_rt_correct_s"] is not None else "—"
    bias_s = f"{s['response_bias']:+.3f}" if s["response_bias"] is not None else "—"
    lines = [
        f"# {s['subject']} — {start:%Y-%m-%d %H:%M:%S}",
        "",
        f"Duration: {str(end - start).split('.')[0]}",
        "",
        "## Setup",
        "",
        f"- N trials (target): {s['n_trials_target']}",
        reward_line,
        f"- Water limit: {s['water_limit_ul'] or '—'} µL",
        f"- Error timeout: {s['error_timeout_s']} s",
        f"- ITI: min {s['iti_min_s']} / mean {s['iti_mean_s']} / max {s['iti_max_s']} s",
        f"- Wheel gain: {s['gain_deg_per_mm']} deg/mm",
        f"- Contrasts: {s['contrasts'] or 'default'}",
        f"- Hardware: {'mock' if s['mock'] else s['port']}, screen {s['screen']}",
        "",
        "## Outcome",
        "",
        f"- Trials completed: {s['n_completed']}",
        f"- Correct: {s['n_correct']} ({pct})",
        f"- No-go: {s['n_nogo']}",
        f"- Mean RT (correct): {rt_s}",
        f"- Response bias (R−L)/(R+L): {bias_s}",
        f"- Water dispensed: {s['water_dispensed_ul']:.1f} µL  ({s['n_correct']} rewards)",
    ]
    (sd / "SUMMARY.md").write_text("\n".join(lines) + "\n")


# CLI runner: `python -m ibl.task ...`. Emits one JSON line per trial on stdout.
def _runner_main():
    import argparse, dataclasses, json, queue, signal, sys, threading, traceback
    from pathlib import Path as _P
    _log = _P.home() / "ibl_runner.log"
    def _crumb(msg):
        line = f"[runner] {msg}"
        print(line, file=sys.stderr, flush=True)
        try:
            with open(_log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    _crumb("entered _runner_main")

    def emit(payload, _out=sys.stdout):
        _out.write(json.dumps(payload) + "\n"); _out.flush()

    ap = argparse.ArgumentParser(prog="ibl.task")
    ap.add_argument("--subject", required=True)
    ap.add_argument("--n-trials", type=int, required=True)
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--reward-ms", type=int, default=50,
                    help="valve open time per correct trial (Teensy control)")
    ap.add_argument("--reward-ul", type=float, default=3.0,
                    help="water delivered per correct trial in µL (for logging)")
    ap.add_argument("--gain", type=float, default=WHEEL_GAIN_DEG_PER_MM,
                    help="wheel gain in deg/mm (visual deg per mm of wheel surface)")
    ap.add_argument("--contrasts", type=str, default=None,
                    help="comma-separated active contrasts, e.g. 1.0,0.5,0.25")
    ap.add_argument("--contrast-tiers", type=str, default=None,
                    help="comma-separated expansion tiers, e.g. 0.25,0.125,0.0625")
    ap.add_argument("--screen", type=int, default=0,
                    help="display index for the PsychoPy fullscreen window")
    ap.add_argument("--error-timeout", type=float, default=ERROR_TIMEOUT_S,
                    help="error feedback duration in seconds (gabor + white noise)")
    ap.add_argument("--iti-min", type=float, default=ITI_MIN_S)
    ap.add_argument("--iti-mean", type=float, default=ITI_MEAN_S)
    ap.add_argument("--iti-max", type=float, default=ITI_MAX_S)
    ap.add_argument("--water-limit", type=int, default=None,
                    help="logged in SUMMARY.md; enforcement lives in the GUI")
    ap.add_argument("--auto-reward", action="store_true",
                    help="advance reward one tier per correct trial; reset on wrong/no-go")
    ap.add_argument("--calibration", type=str, default=None,
                    help="JSON list [{target_ul, ms}, ...]; required with --auto-reward")
    ap.add_argument("--ready", action="store_true",
                    help="open window in standby; wait for a 'start' line on stdin before trials")
    args = ap.parse_args()
    _crumb("argparse ok")

    calibration = None
    if args.calibration:
        calibration = sorted(json.loads(args.calibration),
                             key=lambda c: c["target_ul"])
    if args.auto_reward and not calibration:
        raise SystemExit("--auto-reward requires --calibration")
    initial_ms = int(calibration[0]["ms"]) if args.auto_reward else args.reward_ms

    gain_deg_per_count = args.gain / COUNTS_PER_MM
    active = (tuple(float(x) for x in args.contrasts.split(","))
              if args.contrasts else None)
    tiers = (tuple(float(x) for x in args.contrast_tiers.split(","))
             if args.contrast_tiers else None)
    stop = {"flag": False}
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.update(flag=True))

    cmd_q = queue.Queue()
    def _stdin_reader():
        try:
            for line in iter(sys.stdin.readline, ''):
                line = line.strip()
                if line:
                    cmd_q.put(line)
        except Exception:
            pass
    threading.Thread(target=_stdin_reader, daemon=True).start()

    _crumb(f"argv: {sys.argv[1:]}")
    win = hw = None
    try:
        from ibl.io import FakeTeensy, Teensy
        _crumb("connecting hw...")
        hw = (FakeTeensy(gain_deg_per_count=gain_deg_per_count) if args.mock
              else Teensy(args.port, gain_deg_per_count=gain_deg_per_count))
        _crumb(f"hw ok: {type(hw).__name__}")
        hw.set_reward_duration(initial_ms)
        _crumb(f"opening window screen={args.screen}...")
        win, gabor, sync = build_window(screen=args.screen)
        _crumb(f"window ok: size={tuple(win.size)}")
        emit({"type": "ready"})
        _crumb("emitted ready")

        if args.ready:
            # Hold the window open until the host writes "start" on stdin.
            # While in standby the host may also request manual valve actions
            # ("reward" = single pulse at the configured duration; "flush" =
            # one pulse at 200 ms then restore the configured duration).
            from psychopy import event
            started = False
            FLUSH_MS = 200
            while not started:
                if stop["flag"]:
                    return 0
                if "escape" in event.getKeys(["escape"]):
                    return 0
                try:
                    cmd = cmd_q.get_nowait()
                    if cmd == "start":
                        started = True
                    elif cmd == "reward":
                        hw.reward()
                    elif cmd == "flush":
                        hw.set_reward_duration(FLUSH_MS)
                        hw.reward()
                        time.sleep((FLUSH_MS + 20) / 1000.0)
                        hw.set_reward_duration(initial_ms)
                except queue.Empty:
                    pass
                win.flip()
            emit({"type": "running"})

        start = datetime.datetime.now()
        sd = Path(f"{args.subject}_{start.strftime('%Y-%m-%d_%H-%M-%S')}")
        sd.mkdir(parents=True, exist_ok=True)
        results = []
        def on_trial(r):
            results.append(r)
            emit({"type": "trial", **dataclasses.asdict(r)})
        def on_reward(ms, ul):
            emit({"type": "reward", "ms": ms, "ul": ul})
        try:
            return run_session(
                hw, win, gabor, sync, session_dir=sd,
                n_trials=args.n_trials,
                reward_ms=args.reward_ms, reward_ul=args.reward_ul,
                error_timeout_s=args.error_timeout,
                iti_min_s=args.iti_min, iti_mean_s=args.iti_mean,
                iti_max_s=args.iti_max,
                auto_reward=args.auto_reward, calibration=calibration,
                active_contrasts=active, expansion_tiers=tiers,
                on_trial_complete=on_trial,
                on_reward_set=on_reward,
                should_stop=lambda: stop["flag"],
            )
        finally:
            _write_summary(sd, start, datetime.datetime.now(), args, results, calibration)
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        emit({"type": "error", "msg": f"{type(exc).__name__}: {exc}"})
        return 1 if win is None else 2
    finally:
        for obj in (hw, win):
            if obj is not None:
                try: obj.close()
                except Exception: pass


if __name__ == "__main__":
    raise SystemExit(_runner_main())
