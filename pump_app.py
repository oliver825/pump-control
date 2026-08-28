"""
Pump Control - Python replacement for the LabVIEW app.

Run with:  python pump_app.py

The one architectural rule that fixes the freeze: the GUI thread never talks
to the serial port and never waits for anything. All pump traffic happens on
a background worker thread that takes instructions from a queue. The longest
the worker is ever busy is a single 1 second serial timeout, and even during
a 32 hour sequence it checks the queue ten times a second, so Stop is instant.
"""

from __future__ import annotations

import csv
import os
import queue
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime

# ---------------------------------------------------------------------------
# Preflight. A stack trace here means nothing to anyone, so check the three
# things that can be missing and say what to do about each.
# ---------------------------------------------------------------------------
try:
    import serial  # noqa: F401
except ImportError:
    sys.exit("\n  pyserial is not installed for this interpreter.\n\n"
             "  In PyCharm: View > Tool Windows > Terminal, then type:\n"
             "      pip install pyserial openpyxl\n\n"
             "  The package is 'pyserial', NOT 'serial'.\n")
try:
    import openpyxl  # noqa: F401
except ImportError:
    sys.exit("\n  openpyxl is not installed for this interpreter.\n\n"
             "  In PyCharm: View > Tool Windows > Terminal, then type:\n"
             "      pip install pyserial openpyxl\n")
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError:
    sys.exit("\n  This Python was built without Tkinter, so it cannot draw a\n"
             "  window. Install Python from python.org (the standard Windows\n"
             "  installer always includes Tkinter) and point PyCharm at it.\n")

import wm323
from wm323 import (WM323, MIN_RPM, MAX_RPM, PUMP_LABEL, PUMPHEADS,
                   DEFAULT_PUMPHEAD, PumpError)
import sequence as seqmod
from sequence import load_sequence, fmt_duration

POLL_INTERVAL = 2.0        # seconds between status reads from the pump
WATCHDOG_STRIKES = 3       # failed polls before we abort a running sequence
TICK = 0.1                 # worker loop period

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
BG      = "#161a1d"
PANEL   = "#1f2529"
PANEL2  = "#262d32"
FG      = "#e6edf3"
MUTED   = "#8b98a5"
ACCENT  = "#3fb950"
ACCENT2 = "#2f81f7"
WARN    = "#d29922"
DANGER  = "#da3633"
BORDER  = "#30363d"
MONO    = ("Consolas", 9)


def app_dir() -> str:
    """
    The folder the app lives in, used as the default home for report files.

    Run as a script this is the folder holding pump_app.py.

    Run as a PyInstaller build, __file__ points into a temporary extraction
    folder the OS deletes on exit, so reports written there would silently
    vanish. Use the executable's own location instead.

    On macOS the executable sits inside PumpControl.app/Contents/MacOS/.
    Never write into an .app bundle: it may be read only, and the contents are
    replaced wholesale when the app is updated. Step back out to the folder
    holding the bundle so Mac behaves like Windows.
    """
    if not getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(__file__))

    base = os.path.dirname(sys.executable)
    if base.endswith(os.path.join("Contents", "MacOS")):
        base = os.path.dirname(os.path.dirname(os.path.dirname(base)))
    return base


def reports_dir() -> str:
    """
    Default folder for reports: a "logs" folder beside the app.

    Falls back to the user's Documents folder when that is not writable, which
    happens if the app ends up in Program Files or /Applications. A report in
    an unexpected place beats a report that could not be written at all, and
    the activity log states the full path either way.
    """
    candidate = os.path.join(app_dir(), "logs")
    try:
        os.makedirs(candidate, exist_ok=True)
        probe = os.path.join(candidate, ".write_test")
        with open(probe, "w"):
            pass
        os.remove(probe)
        return candidate
    except OSError:
        fallback = os.path.join(os.path.expanduser("~"), "Documents",
                                "PumpControl logs")
        os.makedirs(fallback, exist_ok=True)
        return fallback


@dataclass
class AppState:
    connected: bool = False
    port: str = ""
    simulated: bool = False
    model: str = ""
    actual_rpm: int = 0
    actual_dir: str = "-"
    actual_running: bool = False
    comms_ok: bool = False
    last_reply_age: float = 999.0

    seq_loaded: bool = False
    seq_name: str = ""
    seq_total: float = 0.0
    seq_steps: int = 0
    seq_state: str = "idle"        # idle | running | paused | done
    seq_index: int = -1
    step_elapsed: float = 0.0
    step_total: float = 0.0
    run_elapsed: float = 0.0
    log_path: str = ""
    rpm_offset: int = 0
    pumphead: str = ""
    min_rpm: int = 3
    max_rpm: int = 400


class Worker(threading.Thread):
    """Owns the serial port. Never let the GUI touch it directly."""

    def __init__(self):
        super().__init__(daemon=True)
        self.cmds: queue.Queue = queue.Queue()
        self.events: queue.Queue = queue.Queue()      # log lines for the GUI
        self.state = AppState()
        self._lock = threading.Lock()
        self._stop_flag = threading.Event()

        self.pump: WM323 | None = None
        self.seq: seqmod.Sequence | None = None
        self._step_deadline = 0.0
        self._step_started = 0.0
        self._run_started = 0.0
        self._next_poll = 0.0
        self._strikes = 0
        self._last_problem = None
        self.rpm_offset = 0                 # live adjustment, old app had this
        self.max_strikes = WATCHDOG_STRIKES
        self.report_dir = "logs"
        self.report_name = ""
        self.report_enabled = True
        self.pumphead = DEFAULT_PUMPHEAD
        self.min_rpm, self.max_rpm = PUMPHEADS[DEFAULT_PUMPHEAD]
        self._csv = None
        self._csv_file = None

    # -- API used by the GUI ------------------------------------------------
    def send(self, action: str, **kw):
        self.cmds.put((action, kw))

    def snapshot(self) -> AppState:
        with self._lock:
            return AppState(**asdict(self.state))

    def log(self, msg: str, level: str = "info"):
        self.events.put((datetime.now().strftime("%H:%M:%S"), level, msg))

    # -- main loop ----------------------------------------------------------
    def run(self):
        while not self._stop_flag.is_set():
            try:
                self._tick()
            except Exception:
                self.log("Worker error: " + traceback.format_exc(limit=2), "error")
                self._abort_sequence("internal error")
            time.sleep(TICK)
        self._safe_shutdown()

    def _tick(self):
        # 1. Drain every queued command first - this is what keeps Stop instant
        while True:
            try:
                action, kw = self.cmds.get_nowait()
            except queue.Empty:
                break
            self._handle(action, kw)
            if action in ("stop", "manual_start", "manual_stop", "set_rpm",
                          "set_dir", "pause_sequence", "resume_sequence"):
                self._next_poll = 0.0        # re-read the pump on this tick

        if not self.pump:
            return

        # 2. Advance the sequence
        now = time.monotonic()
        with self._lock:
            running = self.state.seq_state == "running"
        if running and now >= self._step_deadline:
            self._advance_step()

        # 3. Poll the pump. Normally every POLL_INTERVAL seconds, but any
        #    command we just sent forces an immediate re-read so the readout
        #    never lags behind what the user just did (this matters most for
        #    STOP: nobody should press it and still see RUNNING).
        if now >= self._next_poll:
            # Advance on a fixed grid so rows land on 2.0 s boundaries instead
            # of creeping to 2.1, 2.2... Catch up if we ever fall behind.
            self._next_poll += POLL_INTERVAL
            if self._next_poll <= now:
                self._next_poll = now + POLL_INTERVAL
            self._poll()

        # 4. Update the clocks the GUI reads
        with self._lock:
            if self.state.seq_state == "running":
                self.state.step_elapsed = now - self._step_started
                self.state.run_elapsed = now - self._run_started

    # -- command handling ---------------------------------------------------
    def _handle(self, action, kw):
        if action == "connect":
            self._connect(**kw)
        elif action == "disconnect":
            self._disconnect()
        elif action == "stop":                  # the big red button
            self._abort_sequence("stopped by user")
            self._pump_stop()
        elif action == "manual_start":
            self._manual_start(**kw)
        elif action == "manual_stop":
            self._pump_stop()
        elif action == "set_rpm":
            self._set_rpm(kw["rpm"], from_user=True)
        elif action == "set_dir":
            self._set_dir(kw["direction"])
        elif action == "load_sequence":
            self._load_sequence(**kw)
        elif action == "run_sequence":
            self._start_sequence()
        elif action == "pause_sequence":
            self._pause_sequence()
        elif action == "resume_sequence":
            self._resume_sequence()
        elif action == "set_offset":
            self._set_offset(kw["rpm"])
        elif action == "set_report":
            self.report_dir = kw.get("folder") or self.report_dir
            self.report_name = kw.get("name", self.report_name)
            if "enabled" in kw:
                self.report_enabled = bool(kw["enabled"])
        elif action == "set_retries":
            self.max_strikes = max(1, int(kw["n"]))
        elif action == "skip_step":
            self._advance_step(skipped=True)
        elif action == "shutdown":
            self._stop_flag.set()

    # -- connection ---------------------------------------------------------
    def _connect(self, port, simulate, pumphead=DEFAULT_PUMPHEAD):
        self._disconnect()
        self.pumphead = pumphead
        self.min_rpm, self.max_rpm = PUMPHEADS.get(pumphead, (MIN_RPM, MAX_RPM))
        try:
            self.pump = WM323(port=port, min_rpm=self.min_rpm,
                              max_rpm=self.max_rpm, simulate=simulate)
        except Exception as e:
            self.log(f"Could not open {port}: {e}", "error")
            return
        with self._lock:
            self.state.connected = True
            self.state.port = self.pump.port_name
            self.state.simulated = simulate
            self.state.model = PUMP_LABEL
            self.state.pumphead = pumphead
            self.state.min_rpm = self.min_rpm
            self.state.max_rpm = self.max_rpm
        self.log(f"Opened {self.pump.port_name} at 9600 8-N-2, "
                 f"{pumphead.lower()} ({self.min_rpm}-{self.max_rpm} rpm)")
        st = self.pump.read_status()
        if st.stale:
            self.log("Port opened but the pump did not answer. Check the pump "
                     "is in 'dig' mode (press MODE until the display shows dig) "
                     "and that you have the right COM port.", "warn")
        elif not st.parsed:
            self.log(f"Pump replied but I could not read a speed out of it. "
                     f"Raw reply: {st.raw!r}. Send that line to whoever wrote "
                     f"this and it can be fixed properly.", "warn")
        else:
            self.log(f"Pump replied: {st.raw}  ->  {st.rpm} rpm {st.direction}, "
                     f"{'running' if st.running else 'stopped'}")
        self._poll()

    def _disconnect(self):
        if self.pump:
            self._abort_sequence("disconnected")
            self.pump.close()
            self.log("Port closed, pump stopped")
        self.pump = None
        with self._lock:
            self.state.connected = False
            self.state.comms_ok = False

    # -- pump actions -------------------------------------------------------
    def _pump_stop(self):
        if not self.pump:
            return
        try:
            self.pump.stop()
            self.log("STOP sent")
        except Exception as e:
            self.log(f"STOP failed: {e}", "error")

    def _set_rpm(self, rpm, from_user=False):
        if not self.pump:
            return
        if from_user:
            with self._lock:
                if self.state.seq_state in ("running", "paused"):
                    self.log("A sequence is running. Use 'Live RPM adjust' by "
                             "the sequence controls instead, so the change "
                             "survives into the next step.", "warn")
                    return
        try:
            self.pump.set_rpm(rpm)
            self.log(f"Speed set to {rpm} rpm")
        except PumpError as e:
            self.log(str(e), "warn")
        except Exception as e:
            self.log(f"Speed command failed: {e}", "error")

    def _set_dir(self, direction):
        if not self.pump:
            return
        try:
            self.pump.set_direction(direction)
            self.log(f"Direction set to {direction}")
        except Exception as e:
            self.log(f"Direction command failed: {e}", "error")

    def _manual_start(self, rpm, direction):
        if not self.pump:
            return
        with self._lock:
            if self.state.seq_state == "running":
                self.log("Sequence is running - stop it before driving manually", "warn")
                return
        self._set_dir(direction)
        self._set_rpm(rpm)
        try:
            self.pump.start()
            self.log(f"Started at {rpm} rpm {direction}")
        except Exception as e:
            self.log(f"Start failed: {e}", "error")

    def _effective_rpm(self, step) -> int:
        """Step speed plus the live adjustment, clamped to the pumphead range."""
        return max(self.min_rpm, min(self.max_rpm, step.rpm + self.rpm_offset))

    def _set_offset(self, rpm):
        self.rpm_offset = int(rpm)
        with self._lock:
            running = self.state.seq_state == "running"
            idx = self.state.seq_index
            self.state.rpm_offset = self.rpm_offset
        if self.rpm_offset:
            self.log(f"Live adjustment set to {self.rpm_offset:+d} rpm, applied "
                     f"to every step")
        else:
            self.log("Live adjustment cleared, steps run at their sheet value")
        # Apply straight away rather than waiting for the next step boundary.
        if running and self.seq and 0 <= idx < len(self.seq.steps):
            step = self.seq.steps[idx]
            if step.motor_on:
                self._set_rpm(self._effective_rpm(step))

    # -- sequence -----------------------------------------------------------
    def _load_sequence(self, path, unit):
        try:
            seq = load_sequence(path, unit=unit,
                                min_rpm=self.min_rpm, max_rpm=self.max_rpm)
        except Exception as e:
            self.log(f"Could not load sequence: {e}", "error")
            with self._lock:
                self.state.seq_loaded = False
            return
        self.seq = seq
        with self._lock:
            self.state.seq_loaded = True
            self.state.seq_name = os.path.basename(path)
            self.state.seq_total = seq.total_seconds
            self.state.seq_steps = len(seq.steps)
            self.state.seq_state = "idle"
            self.state.seq_index = -1
        self.log(f"Loaded {len(seq.steps)} steps, total run {fmt_duration(seq.total_seconds)}")
        for w in seq.warnings:
            self.log(w, "warn")

    def _start_sequence(self):
        if not (self.pump and self.seq):
            self.log("Connect to the pump and load a sequence first", "warn")
            return
        self._open_log()
        self._run_started = time.monotonic()
        with self._lock:
            self.state.seq_state = "running"
            self.state.seq_index = -1
            self.state.run_elapsed = 0.0
            self.state.step_elapsed = 0.0
        self.log(f"Sequence started - {len(self.seq.steps)} steps, "
                 f"{fmt_duration(self.seq.total_seconds)} total")
        self._advance_step()

    def _advance_step(self, skipped=False):
        if not self.seq:
            return
        with self._lock:
            idx = self.state.seq_index + 1
        if skipped:
            self.log(f"Step {self.state.seq_index + 1} skipped", "warn")

        if idx >= len(self.seq.steps):
            self._pump_stop()
            with self._lock:
                self.state.seq_state = "done"
                self.state.seq_index = len(self.seq.steps) - 1
            self.log("Sequence complete, pump stopped", "ok")
            self._close_log()
            return

        step = self.seq.steps[idx]
        now = time.monotonic()
        self._last_problem = None       # re-arm the readback warning
        self._step_started = now
        self._step_deadline = now + step.duration_s
        with self._lock:
            self.state.seq_index = idx
            self.state.step_total = step.duration_s
            self.state.step_elapsed = 0.0

        try:
            if step.motor_on:
                self.pump.set_direction(step.direction)
                self.pump.set_rpm(self._effective_rpm(step))
                self.pump.start()
            else:
                self.pump.stop()
        except Exception as e:
            self.log(f"Step {idx + 1} failed to apply: {e}", "error")
            self._abort_sequence("command failure")
            return
        self.log(f"Step {idx + 1}/{len(self.seq.steps)} (row {step.row}): {step.label}")
        # Read the pump back BEFORE writing the marker row, otherwise the row
        # records the pump's previous state and looks like a fault.
        self._next_poll = time.monotonic() + POLL_INTERVAL
        self._poll(note=f"step {idx + 1} start")

    def _pause_sequence(self):
        with self._lock:
            if self.state.seq_state != "running":
                return
            self.state.seq_state = "paused"
            self._paused_remaining = self._step_deadline - time.monotonic()
        self._pump_stop()
        self.log("Sequence paused, pump stopped. Remaining on this step: "
                 f"{fmt_duration(max(0, self._paused_remaining))}", "warn")

    def _resume_sequence(self):
        with self._lock:
            if self.state.seq_state != "paused":
                return
            self.state.seq_state = "running"
            idx = self.state.seq_index
        remaining = getattr(self, "_paused_remaining", 0)
        self._step_deadline = time.monotonic() + max(0, remaining)
        self._step_started = time.monotonic() - (self.seq.steps[idx].duration_s - remaining)
        step = self.seq.steps[idx]
        try:
            if step.motor_on:
                self.pump.set_direction(step.direction)
                self.pump.set_rpm(self._effective_rpm(step))
                self.pump.start()
        except Exception as e:
            self.log(f"Resume failed: {e}", "error")
            self._abort_sequence("resume failure")
            return
        self.log(f"Resumed step {idx + 1}, {fmt_duration(remaining)} remaining")

    def _abort_sequence(self, reason):
        with self._lock:
            was = self.state.seq_state
            self.state.seq_state = "idle"
        if was in ("running", "paused"):
            self.log(f"Sequence aborted: {reason}", "warn")
            self._write_log(note=f"aborted: {reason}")
            self._close_log()

    # -- polling and watchdog ----------------------------------------------
    def _poll(self, note=""):
        if not self.pump:
            return
        try:
            st = self.pump.read_status()
        except Exception as e:
            st = wm323.PumpStatus()
            self.log(f"Poll error: {e}", "error")
        with self._lock:
            self.state.comms_ok = not st.stale
            if not st.stale:
                self.state.actual_rpm = st.rpm
                self.state.actual_dir = st.direction
                self.state.actual_running = st.running
                self.state.last_reply_age = 0.0
            running_seq = self.state.seq_state == "running"
            idx = self.state.seq_index

        if st.stale:
            self._strikes += 1
            if self._strikes >= self.max_strikes and running_seq:
                self.log(f"No reply from the pump after {self._strikes} tries. "
                         f"Aborting the sequence and sending STOP.", "error")
                self._abort_sequence("pump stopped responding")
                self._pump_stop()
        else:
            self._strikes = 0
            self._write_log(note)
            # Cross-check: is the pump actually doing what we told it to?
            problem = None
            if running_seq and self.seq and 0 <= idx < len(self.seq.steps):
                step = self.seq.steps[idx]
                if step.motor_on and not st.running:
                    problem = ("Pump reports STOPPED but the step expects it to "
                               "be running.")
                elif step.motor_on and abs(st.rpm - self._effective_rpm(step)) > 1:
                    problem = (f"Pump reports {st.rpm} rpm, step asks for "
                               f"{self._effective_rpm(step)} rpm.")
                if problem and not st.parsed:
                    problem += (" I could not make sense of the pump's reply, so "
                                "this is probably a display problem rather than a "
                                f"pump problem. Raw reply: {st.raw!r}")
                elif problem:
                    problem += (" Check nobody pressed the pump's front panel and "
                                "that it is still in 'dig' mode.")

            # Only log when the situation changes. Repeating this every 2
            # seconds for 32 hours buries everything useful.
            if problem != self._last_problem:
                if problem:
                    self.log(problem, "warn")
                elif self._last_problem and running_seq:
                    self.log("Pump readback matches the step again", "ok")
                self._last_problem = problem

    # -- run log ------------------------------------------------------------
    def _open_log(self):
        if not self.report_enabled:
            # Say so out loud. Silently not recording a 32 hour feed is a
            # much worse failure than an extra line in the log.
            self.log("Report saving is OFF for this run. Nothing will be "
                     "written to disk.", "warn")
            return
        folder = self.report_dir or reports_dir()
        os.makedirs(folder, exist_ok=True)
        stem = "".join(c for c in (self.report_name or "Pump run")
                       if c not in '\\/:*?"<>|').strip() or "Pump run"
        path = os.path.join(folder, f"{stem} {datetime.now():%Y-%m-%d %H%M}.csv")
        self._csv_file = open(path, "w", newline="", encoding="utf-8")
        self._csv = csv.writer(self._csv_file)

        # Metadata rows. Excel shows them as ordinary text rows, pandas skips
        # them with comment="#". Six months from now somebody will want to know
        # which pumphead and which spreadsheet produced this run.
        for k, v in [
                ("report", stem),
                ("started", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                ("pump", PUMP_LABEL),
                ("pumphead", f"{self.pumphead} "
                             f"({self.min_rpm}-{self.max_rpm} rpm)"),
                ("port", self.state.port),
                ("sequence file", self.state.seq_name),
                ("time unit", self.seq.unit if self.seq else ""),
                ("steps", str(self.state.seq_steps)),
                ("total run", fmt_duration(self.state.seq_total)),
                ("live rpm adjustment", f"{self.rpm_offset:+d}")]:
            self._csv.writerow([f"# {k}", v])
        self._csv.writerow([])
        self._csv.writerow(["timestamp", "elapsed_s", "step", "target_rpm",
                            "actual_rpm", "direction", "running", "note"])
        with self._lock:
            self.state.log_path = path
        self.log(f"Report file: {path}")

    def _write_log(self, note=""):
        if not self._csv:
            return
        # Measure elapsed time here and now rather than reading a field the
        # tick loop refreshes later. Anything cached is a tick stale at best
        # and left over from the previous run at worst.
        elapsed = max(0.0, time.monotonic() - self._run_started)
        with self._lock:
            s = self.state
            step = s.seq_index + 1
            target = (self._effective_rpm(self.seq.steps[s.seq_index])
                      if self.seq and 0 <= s.seq_index < len(self.seq.steps) else "")
            row = [datetime.now().isoformat(timespec="seconds"),
                   round(elapsed, 1), step, target,
                   s.actual_rpm, s.actual_dir, int(s.actual_running), note]
        self._csv.writerow(row)
        self._csv_file.flush()

    def _close_log(self):
        if self._csv_file:
            self._csv_file.close()
        self._csv = self._csv_file = None

    def _safe_shutdown(self):
        try:
            if self.pump:
                self.pump.close()
        finally:
            self._close_log()


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Pump Control")
        self.geometry("1080x720")
        self.minsize(940, 640)
        self.configure(bg=BG)

        self.worker = Worker()
        self.worker.start()

        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._refresh)
        self.bind("<Escape>", lambda e: self._emergency_stop())

    # -- theme --------------------------------------------------------------
    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=PANEL, foreground=FG,
                    fieldbackground=PANEL2, bordercolor=BORDER,
                    font=("Segoe UI", 10))
        s.configure("TFrame", background=PANEL)
        s.configure("Bg.TFrame", background=BG)
        s.configure("TLabel", background=PANEL, foreground=FG)
        s.configure("Muted.TLabel", background=PANEL, foreground=MUTED,
                    font=("Segoe UI", 9))
        s.configure("Head.TLabel", background=PANEL, foreground=FG,
                    font=("Segoe UI Semibold", 11))
        s.configure("Big.TLabel", background=PANEL, foreground=FG,
                    font=("Segoe UI", 26, "bold"))
        s.configure("Unit.TLabel", background=PANEL, foreground=MUTED,
                    font=("Segoe UI", 10))
        s.configure("TLabelframe", background=PANEL, bordercolor=BORDER,
                    relief="solid", borderwidth=1)
        s.configure("TLabelframe.Label", background=PANEL, foreground=MUTED,
                    font=("Segoe UI Semibold", 9))
        s.configure("TButton", background=PANEL2, foreground=FG,
                    borderwidth=1, focusthickness=0, padding=(10, 6))
        s.map("TButton",
              background=[("active", "#323b42"), ("disabled", "#1c2226")],
              foreground=[("disabled", "#55606a")])
        s.configure("Accent.TButton", background=ACCENT2, foreground="#ffffff")
        s.map("Accent.TButton", background=[("active", "#4d94ff"),
                                            ("disabled", "#1c2226")])
        s.configure("Go.TButton", background=ACCENT, foreground="#06210d")
        s.map("Go.TButton", background=[("active", "#4fd063"),
                                        ("disabled", "#1c2226")])
        s.configure("TEntry", fieldbackground=PANEL2, foreground=FG,
                    insertcolor=FG, bordercolor=BORDER)
        s.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2,
                    foreground=FG, arrowcolor=FG)
        s.configure("TRadiobutton", background=PANEL, foreground=FG)
        s.map("TRadiobutton", background=[("active", PANEL)])
        s.configure("TCheckbutton", background=PANEL, foreground=FG)
        s.map("TCheckbutton", background=[("active", PANEL)])
        s.configure("TScale", background=PANEL, troughcolor=PANEL2)
        s.configure("Horizontal.TProgressbar", background=ACCENT2,
                    troughcolor=PANEL2, bordercolor=BORDER, thickness=10)
        s.configure("Step.Horizontal.TProgressbar", background=ACCENT)
        s.configure("Treeview", background=PANEL2, fieldbackground=PANEL2,
                    foreground=FG, borderwidth=0, rowheight=22)
        s.configure("Treeview.Heading", background=PANEL, foreground=MUTED,
                    borderwidth=0, font=("Segoe UI Semibold", 9))
        s.map("Treeview", background=[("selected", "#2d4f7c")])

    # -- layout -------------------------------------------------------------
    def _build(self):
        root = ttk.Frame(self, style="Bg.TFrame", padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=0, minsize=330)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(1, weight=1)

        # header ------------------------------------------------------------
        head = ttk.Frame(root, style="Bg.TFrame")
        head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        tk.Label(head, text="PUMP CONTROL", bg=BG, fg=FG,
                 font=("Segoe UI Semibold", 15)).pack(side="left")
        self.dot = tk.Canvas(head, width=10, height=10, bg=BG, highlightthickness=0)
        self.dot.pack(side="right", padx=(8, 0), pady=6)
        self._dot_id = self.dot.create_oval(1, 1, 9, 9, fill=DANGER, outline="")
        self.conn_lbl = tk.Label(head, text="disconnected", bg=BG, fg=MUTED,
                                 font=("Segoe UI", 9))
        self.conn_lbl.pack(side="right")

        left = ttk.Frame(root, style="Bg.TFrame")
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        right = ttk.Frame(root, style="Bg.TFrame")
        right.grid(row=1, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        self._build_connection(left)
        self._build_readout(left)
        self._build_manual(left)
        self._build_stop(left)
        self._build_sequence(right)
        self._build_log(right)

    def _card(self, parent, title):
        f = ttk.Labelframe(parent, text="  " + title.upper() + "  ", padding=12)
        f.pack(fill="x", pady=(0, 10))
        return f

    def _build_connection(self, parent):
        c = self._card(parent, "connection")
        c.columnconfigure(1, weight=1)

        ttk.Label(c, text="Port", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.port_cb = ttk.Combobox(c, state="readonly", width=22)
        self.port_cb.grid(row=0, column=1, sticky="ew", padx=(8, 4), pady=3)
        ttk.Button(c, text="\u21bb", width=3, command=self._refresh_ports
                   ).grid(row=0, column=2)

        ttk.Label(c, text="Pumphead", style="Muted.TLabel").grid(row=1, column=0, sticky="w")
        self.head_cb = ttk.Combobox(c, state="readonly", values=list(PUMPHEADS),
                                    width=22)
        self.head_cb.set(DEFAULT_PUMPHEAD)
        self.head_cb.grid(row=1, column=1, columnspan=2, sticky="ew",
                          padx=(8, 0), pady=3)

        ttk.Label(c, text="Retries", style="Muted.TLabel").grid(row=2, column=0, sticky="w")
        self.retry_var = tk.IntVar(value=WATCHDOG_STRIKES)
        ttk.Spinbox(c, from_=1, to=50, width=5, textvariable=self.retry_var,
                    command=self._apply_retries).grid(row=2, column=1, sticky="w", padx=(8, 0))
        ttk.Label(c, text="failed polls before abort", style="Muted.TLabel"
                  ).grid(row=3, column=0, columnspan=3, sticky="w")

        ttk.Label(c, text=PUMP_LABEL, style="Muted.TLabel"
                  ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.sim_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(c, text="Simulator (no hardware)", variable=self.sim_var
                        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 4))

        self.connect_btn = ttk.Button(c, text="Connect", style="Accent.TButton",
                                      command=self._toggle_connect)
        self.connect_btn.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self._refresh_ports()

    def _build_readout(self, parent):
        c = self._card(parent, "pump reports")
        c.columnconfigure(0, weight=1)
        row = ttk.Frame(c)
        row.pack(fill="x")
        self.rpm_lbl = ttk.Label(row, text="--", style="Big.TLabel")
        self.rpm_lbl.pack(side="left")
        ttk.Label(row, text=" rpm", style="Unit.TLabel").pack(side="left", pady=(14, 0))
        self.state_lbl = ttk.Label(row, text="", style="Muted.TLabel")
        self.state_lbl.pack(side="right", pady=(16, 0))
        self.comms_lbl = ttk.Label(c, text="no data", style="Muted.TLabel")
        self.comms_lbl.pack(anchor="w", pady=(2, 0))

    def _build_manual(self, parent):
        c = self._card(parent, "manual control")
        c.columnconfigure(1, weight=1)

        ttk.Label(c, text="Speed", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.rpm_var = tk.IntVar(value=30)
        self.rpm_spin = ttk.Spinbox(c, from_=MIN_RPM, to=MAX_RPM, width=6,
                                    textvariable=self.rpm_var)
        self.rpm_spin.grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(c, text=f"rpm  ({MIN_RPM}-{MAX_RPM})", style="Muted.TLabel"
                  ).grid(row=0, column=2, sticky="w")

        self.rpm_scale = ttk.Scale(c, from_=MIN_RPM, to=MAX_RPM, orient="horizontal",
                                   command=lambda v: self.rpm_var.set(int(float(v))))
        self.rpm_scale.set(30)
        self.rpm_scale.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 8))

        self.dir_var = tk.StringVar(value="CW")
        drow = ttk.Frame(c)
        drow.grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(drow, text="Clockwise", value="CW",
                        variable=self.dir_var).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(drow, text="Counter-clockwise", value="CCW",
                        variable=self.dir_var).pack(side="left")

        brow = ttk.Frame(c)
        brow.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        brow.columnconfigure((0, 1), weight=1)
        ttk.Button(brow, text="Start", style="Go.TButton",
                   command=self._manual_start).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(brow, text="Stop",
                   command=lambda: self.worker.send("manual_stop")
                   ).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        ttk.Button(c, text="Apply speed while running",
                   command=lambda: self.worker.send("set_rpm", rpm=self.rpm_var.get())
                   ).grid(row=4, column=0, columnspan=3, sticky="ew", pady=(6, 0))

    def _build_stop(self, parent):
        self.stop_btn = tk.Button(
            parent, text="STOP  (Esc)", bg=DANGER, fg="white",
            activebackground="#f0483f", activeforeground="white",
            font=("Segoe UI Semibold", 14), relief="flat", bd=0,
            height=2, cursor="hand2", command=self._emergency_stop)
        self.stop_btn.pack(fill="x", pady=(4, 0))

    def _build_sequence(self, parent):
        c = ttk.Labelframe(parent, text="  TIMED SEQUENCE  ", padding=12)
        c.grid(row=0, column=0, sticky="ew")
        c.columnconfigure(0, weight=1)

        top = ttk.Frame(c)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        ttk.Button(top, text="Open .xlsx", command=self._browse).grid(row=0, column=0)
        self.file_lbl = ttk.Label(top, text="no file loaded", style="Muted.TLabel")
        self.file_lbl.grid(row=0, column=1, sticky="w", padx=10)
        ttk.Label(top, text="Each duration is in", style="Muted.TLabel"
                  ).grid(row=0, column=2, sticky="e")
        self.unit_cb = ttk.Combobox(top, state="readonly", width=9,
                                    values=["seconds", "minutes", "hours"])
        self.unit_cb.current(1)
        self.unit_cb.grid(row=0, column=3, padx=(6, 0))
        self.unit_cb.bind("<<ComboboxSelected>>",
                          lambda e: self._reload(from_dropdown=True))

        rep = ttk.Frame(c)
        rep.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        rep.columnconfigure(1, weight=1)
        self.report_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(rep, text="Save report file", variable=self.report_on,
                        command=self._toggle_report).grid(
            row=0, column=0, columnspan=3, sticky="w")

        self.report_entry = ttk.Entry(rep, textvariable=tk.StringVar())
        self.report_var = tk.StringVar(
            value=f"Fermentation Feed {datetime.now():%d-%m-%y}")
        self.report_entry.configure(textvariable=self.report_var)
        self.report_entry.grid(row=1, column=1, sticky="ew", padx=(8, 6), pady=(4, 0))
        ttk.Label(rep, text="Name", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(4, 0))
        self.folder_btn = ttk.Button(rep, text="Save to...",
                                     command=self._choose_folder)
        self.folder_btn.grid(row=1, column=2, pady=(4, 0))
        self.folder_lbl = ttk.Label(rep, text="logs folder", style="Muted.TLabel")
        self.folder_lbl.grid(row=2, column=1, columnspan=2, sticky="w", padx=(8, 0))

        cols = ("#", "row", "duration", "rpm", "dir", "motor")
        self.tree = ttk.Treeview(c, columns=cols, show="headings", height=9)
        for name, w, anchor in (("#", 40, "e"), ("row", 45, "e"),
                                ("duration", 110, "w"), ("rpm", 60, "e"),
                                ("dir", 60, "w"), ("motor", 60, "w")):
            self.tree.heading(name, text=name)
            self.tree.column(name, width=w, anchor=anchor, stretch=(name == "duration"))
        self.tree.grid(row=2, column=0, sticky="ew", pady=(10, 8))
        self.tree.tag_configure("active", background="#1d3a24", foreground=ACCENT)
        self.tree.tag_configure("done", foreground=MUTED)

        prog = ttk.Frame(c)
        prog.grid(row=3, column=0, sticky="ew")
        prog.columnconfigure(0, weight=1)
        self.step_lbl = ttk.Label(prog, text="idle", style="Muted.TLabel")
        self.step_lbl.grid(row=0, column=0, sticky="w")
        self.step_bar = ttk.Progressbar(prog, style="Step.Horizontal.TProgressbar",
                                        maximum=1000)
        self.step_bar.grid(row=1, column=0, sticky="ew", pady=(2, 6))
        self.total_lbl = ttk.Label(prog, text="", style="Muted.TLabel")
        self.total_lbl.grid(row=2, column=0, sticky="w")
        self.total_bar = ttk.Progressbar(prog, maximum=1000)
        self.total_bar.grid(row=3, column=0, sticky="ew", pady=(2, 0))

        btns = ttk.Frame(c)
        btns.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        btns.columnconfigure((0, 1, 2, 3), weight=1)
        self.run_btn = ttk.Button(btns, text="Run sequence", style="Go.TButton",
                                  command=self._run_sequence)
        self.run_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.pause_btn = ttk.Button(btns, text="Pause", command=self._pause)
        self.pause_btn.grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(btns, text="Skip step",
                   command=lambda: self.worker.send("skip_step")
                   ).grid(row=0, column=2, sticky="ew", padx=4)
        ttk.Button(btns, text="Abort",
                   command=self._emergency_stop).grid(row=0, column=3, sticky="ew", padx=(4, 0))

        adj = ttk.Frame(c)
        adj.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(adj, text="Live RPM adjust", style="Muted.TLabel").pack(side="left")
        self.offset_var = tk.IntVar(value=0)
        ttk.Spinbox(adj, from_=-100, to=100, width=5, textvariable=self.offset_var,
                    command=self._apply_offset).pack(side="left", padx=(8, 6))
        ttk.Button(adj, text="Apply", command=self._apply_offset).pack(side="left")
        self.offset_lbl = ttk.Label(adj, text="every step runs at its sheet value",
                                    style="Muted.TLabel")
        self.offset_lbl.pack(side="left", padx=12)

    def _build_log(self, parent):
        c = ttk.Labelframe(parent, text="  ACTIVITY  ", padding=8)
        c.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        c.rowconfigure(0, weight=1)
        c.columnconfigure(0, weight=1)
        self.log_txt = tk.Text(c, bg=PANEL2, fg=FG, font=MONO, relief="flat",
                               wrap="word", height=8, padx=8, pady=6,
                               insertbackground=FG)
        self.log_txt.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(c, orient="vertical", command=self.log_txt.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log_txt.configure(yscrollcommand=sb.set, state="disabled")
        for tag, colour in (("info", FG), ("warn", WARN),
                            ("error", DANGER), ("ok", ACCENT), ("time", MUTED)):
            self.log_txt.tag_configure(tag, foreground=colour)

    # -- actions ------------------------------------------------------------
    def _refresh_ports(self):
        ports = wm323.available_ports()
        self.port_cb["values"] = [d for _, d in ports] or ["(no COM ports found)"]
        self._port_map = {d: dev for dev, d in ports}
        if ports:
            self.port_cb.current(0)

    def _toggle_connect(self):
        if self.worker.snapshot().connected:
            self.worker.send("disconnect")
            return
        sim = self.sim_var.get()
        port = self._port_map.get(self.port_cb.get())
        if not sim and not port:
            messagebox.showwarning(
                "No port", "No COM port selected.\n\nPlug the pump in, press the "
                "refresh button, or tick Simulator to try the app without hardware.")
            return
        self.worker.send("set_retries", n=self.retry_var.get())
        self.worker.send("connect", port=port, simulate=sim,
                         pumphead=self.head_cb.get())

    def _manual_start(self):
        self.worker.send("manual_start", rpm=self.rpm_var.get(),
                         direction=self.dir_var.get())

    def _apply_retries(self):
        self.worker.send("set_retries", n=self.retry_var.get())

    def _apply_offset(self):
        try:
            v = int(self.offset_var.get())
        except (tk.TclError, ValueError):
            return
        self.worker.send("set_offset", rpm=v)

    def _toggle_report(self):
        on = self.report_on.get()
        state = "normal" if on else "disabled"
        self.report_entry.configure(state=state)
        self.folder_btn.configure(state=state)
        self.folder_lbl.configure(
            text=self.folder_lbl.cget("text") if on else "not saving to disk")
        self.worker.send("set_report", enabled=on, name=self.report_var.get())

    def _choose_folder(self):
        folder = filedialog.askdirectory(title="Where should report files go?")
        if folder:
            self.folder_lbl.configure(text=folder)
            self.worker.send("set_report", folder=folder,
                             name=self.report_var.get(),
                             enabled=self.report_on.get())

    def _emergency_stop(self):
        self.worker.send("stop")

    def _browse(self):
        path = filedialog.askopenfilename(
            title="Select sequence spreadsheet",
            filetypes=[("Excel files", "*.xlsx *.xlsm"), ("All files", "*.*")])
        if path:
            self._seq_path = path
            self._reload()

    def _reload(self, from_dropdown=False):
        path = getattr(self, "_seq_path", None)
        if not path:
            return
        s = self.worker.snapshot()
        if s.seq_state in ("running", "paused"):
            # Never re-time a sequence that is already driving the pump.
            messagebox.showwarning(
                "Sequence in progress",
                "Stop the sequence before changing the time unit.")
            self.unit_cb.set(getattr(self, "_unit", "minutes"))
            return
        self._unit = self.unit_cb.get()
        self.worker.send("load_sequence", path=path, unit=self._unit)

    def _run_sequence(self):
        s = self.worker.snapshot()
        if s.seq_state == "paused":
            self.worker.send("resume_sequence")
            return
        if not s.seq_loaded:
            messagebox.showwarning("No sequence", "Load a spreadsheet first.")
            return
        report = (f"Report: {self.report_var.get()}" if self.report_on.get()
                  else "NO REPORT FILE will be saved for this run.")
        if not messagebox.askyesno(
                "Start sequence",
                f"{s.seq_steps} steps, total run time {fmt_duration(s.seq_total)}.\n"
                f"{report}\n\n"
                f"The pump will start immediately. Continue?"):
            return
        self.worker.send("set_report", name=self.report_var.get(),
                         enabled=self.report_on.get())
        self.worker.send("run_sequence")

    def _pause(self):
        s = self.worker.snapshot()
        self.worker.send("resume_sequence" if s.seq_state == "paused"
                         else "pause_sequence")

    # -- periodic GUI refresh ----------------------------------------------
    def _refresh(self):
        s = self.worker.snapshot()

        # log lines
        while True:
            try:
                ts, level, msg = self.worker.events.get_nowait()
            except queue.Empty:
                break
            self.log_txt.configure(state="normal")
            self.log_txt.insert("end", ts + "  ", "time")
            self.log_txt.insert("end", msg + "\n", level)
            self.log_txt.see("end")
            self.log_txt.configure(state="disabled")

        # connection
        self.connect_btn.configure(text="Disconnect" if s.connected else "Connect")
        if not s.connected:
            colour, text = DANGER, "disconnected"
        elif s.comms_ok:
            colour, text = ACCENT, f"{s.port}{'  (simulated)' if s.simulated else ''}"
        else:
            colour, text = WARN, f"{s.port}  no reply"
        self.dot.itemconfigure(self._dot_id, fill=colour)
        self.conn_lbl.configure(text=text, fg=colour)

        # readout
        self.rpm_lbl.configure(text=str(s.actual_rpm) if s.comms_ok else "--")
        self.state_lbl.configure(
            text=(f"{'RUNNING' if s.actual_running else 'STOPPED'}  {s.actual_dir}"
                  if s.comms_ok else ""))
        self.comms_lbl.configure(
            text="live" if s.comms_ok else ("waiting for reply" if s.connected else "no data"))

        # sequence table + progress
        if s.seq_loaded and getattr(self, "_shown_seq", None) != (s.seq_name, s.seq_steps, s.seq_total):
            self._shown_seq = (s.seq_name, s.seq_steps, s.seq_total)
            self._fill_tree()
            self.file_lbl.configure(
                text=f"{s.seq_name}  -  {s.seq_steps} steps, {fmt_duration(s.seq_total)}")

        if s.seq_state in ("running", "paused"):
            frac = s.step_elapsed / s.step_total if s.step_total else 0
            self.step_bar["value"] = min(1000, frac * 1000)
            remain = max(0, s.step_total - s.step_elapsed)
            verb = "PAUSED at step" if s.seq_state == "paused" else "Step"
            self.step_lbl.configure(
                text=f"{verb} {s.seq_index + 1} of {s.seq_steps}  -  "
                     f"{fmt_duration(remain)} left on this step")
            done = (self.worker.seq.elapsed_at_start_of(s.seq_index)
                    if self.worker.seq else 0) + s.step_elapsed
            self.total_bar["value"] = min(1000, done / s.seq_total * 1000) if s.seq_total else 0
            self.total_lbl.configure(
                text=f"Total {fmt_duration(done)} of {fmt_duration(s.seq_total)}  -  "
                     f"finishes about {self._eta(s.seq_total - done)}")
            self._highlight(s.seq_index)
        elif s.seq_state == "done":
            self.step_bar["value"] = self.total_bar["value"] = 1000
            self.step_lbl.configure(text="Sequence complete")
        self.offset_lbl.configure(
            text=("every step runs at its sheet value" if not s.rpm_offset
                  else f"every step shifted by {s.rpm_offset:+d} rpm"))
        self.run_btn.configure(
            text="Resume" if s.seq_state == "paused" else "Run sequence")
        self.pause_btn.configure(
            text="Resume" if s.seq_state == "paused" else "Pause")

        self.after(200, self._refresh)

    def _eta(self, seconds_left):
        return datetime.fromtimestamp(time.time() + seconds_left).strftime("%a %H:%M")

    def _fill_tree(self):
        self.tree.delete(*self.tree.get_children())
        seq = self.worker.seq
        if not seq:
            return
        for st in seq.steps:
            self.tree.insert("", "end", iid=str(st.index), values=(
                st.index + 1, st.row, fmt_duration(st.duration_s),
                st.rpm if st.motor_on else "-", st.direction,
                "ON" if st.motor_on else "OFF"))

    def _highlight(self, idx):
        if getattr(self, "_hl", None) == idx:
            return
        self._hl = idx
        for child in self.tree.get_children():
            i = int(child)
            self.tree.item(child, tags=("active",) if i == idx
                           else ("done",) if i < idx else ())
        if 0 <= idx < len(self.tree.get_children()):
            self.tree.see(str(idx))

    def _on_close(self):
        s = self.worker.snapshot()
        if s.seq_state == "running" and not messagebox.askyesno(
                "Sequence running",
                "A sequence is still running.\n\nClosing will STOP the pump. Continue?"):
            return
        self.worker.send("stop")
        self.worker.send("shutdown")
        self.after(400, self.destroy)


if __name__ == "__main__":
    App().mainloop()
