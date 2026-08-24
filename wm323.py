"""
Watson-Marlow 323Du serial driver.

Protocol (from the 323E/S/U/Du manual, section 17.2):
    9600 baud, 8 data bits, 2 stop bits, no parity, no flow control, ECHO ON
    Every command is prefixed with the pump ID (always "1" on a 323Du)
    Every command is terminated with CR (ASCII 13)
    Minimum 10 ms between commands

    1SPxxx  set speed to xxx rpm      1RC   reverse direction
    1SI     speed +1 rpm              1RR   set clockwise
    1SD     speed -1 rpm              1RL   set counter-clockwise
    1GO     start                     1RS   report everything
    1ST     stop                      1ZY   report running state (0/1)

    1RS returns e.g.:  323Du 110 CW 1 !
                       [type] [speed] [direction] [running] [terminator]

DESIGN RULE: no read in this file can ever block forever. Every read has a
hard deadline. This is the single thing the LabVIEW version got wrong.
"""

from __future__ import annotations

import re
import time
import threading
from dataclasses import dataclass

import serial
from serial.tools import list_ports

CR = b"\r"

# ---------------------------------------------------------------------------
# Pump configuration. Confirmed hardware: Watson-Marlow 323Du, 3-400 rpm.
# If the drive is ever swapped for a different gearbox or pumphead, this is
# the only place you need to change the limits.
# ---------------------------------------------------------------------------
MIN_RPM = 3
MAX_RPM = 400
PUMP_LABEL = "Watson-Marlow 323Du  \u00b7  9600 8-N-2"

# The old LabVIEW software had a "Select Pumphead" dropdown. Same idea here.
# Name -> (min rpm, max rpm). If a head has a different speed range, put the
# real numbers in here and the app will clamp to them and reject spreadsheet
# steps that ask for more.
PUMPHEADS = {
    "Low flow rate pump head": (3, 400),
    "High flow rate pump head": (3, 400),
}
DEFAULT_PUMPHEAD = "Low flow rate pump head"


class PumpError(RuntimeError):
    pass


@dataclass
class PumpStatus:
    """Whatever the pump last told us about itself."""
    model: str = ""
    rpm: int = 0
    direction: str = ""       # "CW" or "CCW"
    running: bool = False
    raw: str = ""
    stale: bool = True        # True if the last poll failed
    parsed: bool = False      # True if we understood the reply, not just got one


# The manual documents the 1RS reply as:
#     [type] [speed] [direction] [running 0/1] [!]     e.g. "323Du 110 CW 1 !"
# Real firmware varies: decimals ("30.0"), zero padding ("030"), model names
# with spaces, extra prefixes. So rather than counting from the left, find the
# direction word and work outwards from it. That anchor is unambiguous.
_DIRECTIONS = {
    "CW": "CW", "CLOCKWISE": "CW", "FWD": "CW", "FORWARD": "CW",
    "CCW": "CCW", "ACW": "CCW", "ANTICLOCKWISE": "CCW",
    "COUNTERCLOCKWISE": "CCW", "REV": "CCW", "REVERSE": "CCW",
}
_NUMBER = re.compile(r"[-+]?\d*\.?\d+")


def _as_number(token: str) -> float | None:
    m = _NUMBER.search(token)
    return float(m.group()) if m else None


def parse_status(reply: str) -> PumpStatus:
    st = PumpStatus(raw=reply)
    if not reply.strip():
        return st

    tokens = reply.replace("!", " ").replace(",", " ").split()
    st.stale = False          # the pump said *something*, so comms are alive

    # Anchor on the direction word.
    d_at = next((i for i, t in enumerate(tokens)
                 if t.upper().strip(".") in _DIRECTIONS), None)

    if d_at is not None:
        st.direction = _DIRECTIONS[tokens[d_at].upper().strip(".")]
        # Speed is the nearest number to the left of the direction.
        for t in reversed(tokens[:d_at]):
            v = _as_number(t)
            if v is not None:
                st.rpm = int(round(v))
                st.model = " ".join(tokens[:d_at]).replace(t, "", 1).strip()
                st.parsed = True
                break
        # Running flag is the first 0 or 1 to the right of the direction.
        for t in tokens[d_at + 1:]:
            if t in ("0", "1"):
                st.running = t == "1"
                break
        else:
            st.running = st.rpm > 0
        return st

    # No direction word at all. Fall back to the documented positions.
    if len(tokens) >= 4:
        st.model = tokens[0]
        v = _as_number(tokens[1])
        if v is not None:
            st.rpm = int(round(v))
            st.parsed = True
        st.direction = tokens[2].upper()
        st.running = tokens[3] == "1"
    return st


# --------------------------------------------------------------------------
# Simulator - lets you build and test the whole app with no pump plugged in
# --------------------------------------------------------------------------
class SimulatedPumpPort:
    """Pretends to be a serial.Serial talking to a 323Du, echo included."""

    def __init__(self, model="323Du", latency=0.02):
        self._buf = bytearray()
        self._rpm = 0
        self._dir = "CW"
        self._run = False
        self._model = model
        self._latency = latency
        self.is_open = True

    # -- serial.Serial API surface we actually use -------------------------
    @property
    def in_waiting(self):
        return len(self._buf)

    def write(self, data: bytes):
        time.sleep(self._latency)
        self._buf.extend(data)          # echo on, exactly like the real pump
        self._handle(data.decode("ascii", "replace").strip())
        return len(data)

    def read(self, n=1):
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def reset_input_buffer(self):
        self._buf.clear()

    def reset_output_buffer(self):
        pass

    def close(self):
        self.is_open = False

    # -- fake firmware -----------------------------------------------------
    def _handle(self, cmd: str):
        c = cmd.upper()
        if c.startswith("1SP"):
            try:
                self._rpm = int(c[3:])
            except ValueError:
                self._reply("?")
                return
        elif c == "1GO":
            self._run = True
        elif c == "1ST":
            self._run = False
        elif c == "1RR":
            self._dir = "CW"
        elif c == "1RL":
            self._dir = "CCW"
        elif c == "1RC":
            self._dir = "CCW" if self._dir == "CW" else "CW"
        elif c == "1SI":
            self._rpm += 1
        elif c == "1SD":
            self._rpm -= 1
        elif c == "1RS":
            self._reply(f"{self._model} {self._rpm} {self._dir} "
                        f"{1 if self._run else 0} !")
            return
        elif c == "1ZY":
            self._reply(f"{1 if self._run else 0} !")
            return
        else:
            self._reply("?")
            return
        self._reply("!")

    def _reply(self, text: str):
        self._buf.extend((text + "\r\n").encode("ascii"))


# --------------------------------------------------------------------------
# Real driver
# --------------------------------------------------------------------------
class WM323:
    def __init__(self, port: str | None, pump_id: int = 1,
                 min_rpm: int = MIN_RPM, max_rpm: int = MAX_RPM,
                 speed_format: str = "{:03d}", simulate: bool = False,
                 timeout: float = 1.0):
        """
        speed_format: the manual writes the command as "1SPxxx", which implies
        three digits. If your pump ignores speed commands, try "{:d}" instead.
        """
        self.pump_id = pump_id
        self.min_rpm = min_rpm
        self.max_rpm = max_rpm
        self.speed_format = speed_format
        self.timeout = timeout
        self._lock = threading.Lock()
        self._last_tx = 0.0

        if simulate:
            self.ser = SimulatedPumpPort()
            self.port_name = "SIMULATOR"
        else:
            self.ser = serial.Serial(
                port=port,
                baudrate=9600,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_TWO,
                timeout=0,            # non-blocking; we do our own deadlines
                write_timeout=2.0,
                rtscts=False, dsrdtr=False, xonxoff=False,
            )
            self.port_name = port
            time.sleep(0.2)
            self.ser.reset_input_buffer()

    # -- low level ---------------------------------------------------------
    def _drain(self, deadline: float, echo_len: int = 0,
               idle_gap: float = 0.12) -> str:
        """
        Read until the pump sends its '!' terminator, or goes quiet for
        idle_gap seconds, or hits the deadline. Whichever comes first.
        Never blocks past deadline - that is the whole point of this method.
        """
        chunks = bytearray()
        last_byte_at = None
        while time.monotonic() < deadline:
            n = self.ser.in_waiting
            if n:
                chunks.extend(self.ser.read(n))
                last_byte_at = time.monotonic()
                # Past the echo and a terminator has arrived: we're done.
                if len(chunks) > echo_len:
                    tail = chunks[echo_len:]
                    if b"!" in tail or b"?" in tail:
                        break
            elif last_byte_at and (time.monotonic() - last_byte_at) > idle_gap:
                break
            else:
                time.sleep(0.005)
        return chunks.decode("ascii", "replace")

    def command(self, body: str, wait: float | None = None) -> str:
        """
        Send one command, strip the echo, return the pump's reply.
        Thread-safe. Enforces the 10 ms inter-command gap from the manual.
        """
        wait = self.timeout if wait is None else wait
        line = f"{self.pump_id}{body}"
        with self._lock:
            gap = time.monotonic() - self._last_tx
            if gap < 0.05:
                time.sleep(0.05 - gap)

            self.ser.reset_input_buffer()
            self.ser.write(line.encode("ascii") + CR)
            self._last_tx = time.monotonic()

            raw = self._drain(time.monotonic() + wait, echo_len=len(line) + 1)

        # Echo is on, so the first thing back is our own command. Remove it.
        cleaned = raw.replace(line, "", 1)
        return cleaned.replace("\r", "\n").strip()

    # -- commands ----------------------------------------------------------
    def start(self):
        return self.command("GO")

    def stop(self):
        return self.command("ST")

    def set_rpm(self, rpm: int):
        rpm = int(round(rpm))
        if not (self.min_rpm <= rpm <= self.max_rpm):
            raise PumpError(
                f"{rpm} rpm is outside this pump's range "
                f"({self.min_rpm}-{self.max_rpm} rpm)")
        return self.command("SP" + self.speed_format.format(rpm))

    def set_direction(self, direction: str):
        d = direction.strip().upper()
        if d in ("CW", "CLOCKWISE"):
            return self.command("RR")
        if d in ("CCW", "ACW", "COUNTER-CLOCKWISE", "ANTICLOCKWISE"):
            return self.command("RL")
        raise PumpError(f"Unknown direction {direction!r}")

    def read_status(self) -> PumpStatus:
        """1RS -> something like '323Du 110 CW 1 !'"""
        return parse_status(self.command("RS"))

    def is_running(self) -> bool | None:
        reply = self.command("ZY")
        for tok in reply.replace("!", " ").split():
            if tok in ("0", "1"):
                return tok == "1"
        return None

    def close(self):
        """Always try to stop the pump before letting go of the port."""
        try:
            self.stop()
        except Exception:
            pass
        try:
            self.ser.close()
        except Exception:
            pass


def available_ports() -> list[tuple[str, str]]:
    """[(device, human readable description), ...]"""
    out = []
    for p in list_ports.comports():
        out.append((p.device, f"{p.device} - {p.description}"))
    return out