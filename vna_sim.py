"""An in-process fake E5071B, so the collectors can be dry-run without hardware.

`python agilent_e5071b.py drift --simulate` routes through here instead of the
LAN. It answers the SCPI subset the collectors use, returns a synthetic S11
resonance whose centre frequency drifts with time (which is the whole point of
the drift run), and can inject the failures a real 3.5 h session hits: VISA
timeouts, a dropped link, a truncated data reply, a refused reconnect.

Tests drive it by editing CONFIG before opening, then reading STATE afterwards:

    import vna_sim
    vna_sim.reset(fail_at={5}, drop_at={9}, time_scale=600)
    ...
    vna_sim.STATE["writes"]   # every SCPI write, in order
"""

import re
import time

import numpy as np
import pyvisa
from pyvisa.constants import StatusCode

CONFIG = dict(
    points=201,
    ifbw=70000.0,
    fstart=300e3,
    fstop=8.5e9,
    sweep_time=0.0,     # real seconds each triggered sweep should take
    time_scale=1.0,     # 3600 makes one real second look like one drifted hour
    noise_db=0.02,
    fail_at=(),         # sweep indices that raise a VISA timeout
    drop_at=(),         # sweep indices where the link dies until reconnected
    short_at=(),        # sweep indices returning a truncated SDATa reply
    open_fail=0,        # refuse this many opens before letting one through
    open_fail_start=0,  # index of the first open to refuse (1 = let the run start)
)

STATE = dict(sweeps=0, opens=0, writes=[], t0=None, settings=None, open_fail_left=0)


def reset(**overrides):
    """Restore stock config/state, then apply overrides. Call before each test."""
    CONFIG.update(points=201, ifbw=70000.0, fstart=300e3, fstop=8.5e9,
                  sweep_time=0.0, time_scale=1.0, noise_db=0.02,
                  fail_at=(), drop_at=(), short_at=(), open_fail=0, open_fail_start=0)
    CONFIG.update(overrides)
    STATE.update(sweeps=0, opens=0, writes=[], t0=None,
                 open_fail_left=CONFIG["open_fail"],
                 settings=dict(points=CONFIG["points"], ifbw=CONFIG["ifbw"],
                               fstart=CONFIG["fstart"], fstop=CONFIG["fstop"],
                               trig="INT", defs=["S11"]))


def _timeout():
    return pyvisa.errors.VisaIOError(int(StatusCode.error_timeout))


def _lost():
    return pyvisa.errors.VisaIOError(int(StatusCode.error_connection_lost))


def _elapsed():
    """Virtual seconds since the first open, stretched by time_scale."""
    if STATE["t0"] is None:
        STATE["t0"] = time.monotonic()
    return (time.monotonic() - STATE["t0"]) * CONFIG["time_scale"]


class SimInstrument:
    """Enough of a pyvisa resource for the collectors to run against."""

    def __init__(self):
        self.timeout = 20000
        self.dead = False
        self.closed = False
        self.selected = 1

    # --- plumbing ---------------------------------------------------------
    def _check(self):
        if self.closed:
            raise _lost()
        if self.dead:
            raise _lost()

    def _settings(self):
        return STATE["settings"]

    def close(self):
        self.closed = True

    # --- synthetic measurement -------------------------------------------
    def _freq(self):
        s = self._settings()
        return np.linspace(s["fstart"], s["fstop"], s["points"])

    def _sparam(self, name):
        """A notch that walks down in frequency as the run warms up."""
        f = self._freq()
        t_hours = _elapsed() / 3600.0
        f0 = 2.45e9 - 3.0e6 * t_hours          # centre drifts ~3 MHz/hour
        q = 40.0 + 2.0 * t_hours
        x = f / f0 - f0 / f
        gamma = (1j * q * x) / (1 + 1j * q * x)
        if name != "S11":
            gamma = 0.05 * (1 - gamma)          # something bounded for the others
        rng = np.random.default_rng(abs(hash((name, STATE["sweeps"]))) % (2 ** 32))
        noise = CONFIG["noise_db"] * (rng.standard_normal(f.size)
                                      + 1j * rng.standard_normal(f.size)) / 20
        return gamma + noise

    # --- SCPI -------------------------------------------------------------
    def write(self, cmd):
        self._check()
        STATE["writes"].append(cmd)
        s = self._settings()
        up = cmd.upper()

        if ":TRIG" in up and "SING" in up:
            n = STATE["sweeps"]
            STATE["sweeps"] += 1
            if n in CONFIG["drop_at"]:
                self.dead = True
                raise _lost()
            if n in CONFIG["fail_at"]:
                raise _timeout()
            if CONFIG["sweep_time"]:
                time.sleep(CONFIG["sweep_time"])
            return
        m = re.match(r":CALC\w*\d*:PAR\w*?(\d+):SEL", up)
        if m:
            self.selected = int(m.group(1))
            return
        m = re.match(r":CALC\w*\d*:PAR\w*?(\d+):DEF\w*\s+(\S+)", up)
        if m:
            i, name = int(m.group(1)), m.group(2)
            while len(s["defs"]) < i:
                s["defs"].append("S11")
            s["defs"][i - 1] = name
            return
        m = re.match(r":CALC\w*\d*:PAR\w*:COUN\w*\s+(\d+)", up)
        if m:
            n = int(m.group(1))
            s["defs"] = (s["defs"] + ["S11"] * n)[:n]
            return
        for key, pat in (("points", r":SENS\w*\d*:SWE\w*:POIN\w*\s+(\S+)"),
                         ("ifbw", r":SENS\w*\d*:BAND\w*\s+(\S+)"),
                         ("fstart", r":SENS\w*\d*:FREQ\w*:STAR\w*\s+(\S+)"),
                         ("fstop", r":SENS\w*\d*:FREQ\w*:STOP\s+(\S+)")):
            m = re.match(pat, up)
            if m:
                s[key] = int(float(m.group(1))) if key == "points" else float(m.group(1))
                return
        m = re.match(r":TRIG\w*:SEQ\w*:SOUR\w*\s+(\S+)", up)
        if m:
            s["trig"] = m.group(1)
            return
        # :FORMat:DATA, :INITiate:CONTinuous and friends need no state

    def query(self, cmd):
        self._check()
        s = self._settings()
        up = cmd.upper()
        if up.startswith("*IDN"):
            return "Agilent Technologies,E5071B,MY00000000,A.09.10 [SIMULATED]\n"
        if up.startswith("*OPC"):
            return "1\n"
        if "SWE" in up and "POIN" in up:
            return f"{s['points']}\n"
        if "BAND" in up:
            return f"{s['ifbw']:.6f}\n"
        if "FREQ" in up and "STAR" in up:
            return f"{s['fstart']:.6f}\n"
        if "FREQ" in up and "STOP" in up:
            return f"{s['fstop']:.6f}\n"
        if "TRIG" in up and "SOUR" in up:
            return s["trig"] + "\n"
        if "PAR" in up and "COUN" in up:
            return f"{len(s['defs'])}\n"
        m = re.match(r":CALC\w*\d*:PAR\w*?(\d+):DEF\w*\?", up)
        if m:
            i = int(m.group(1))
            return (s["defs"][i - 1] if i <= len(s["defs"]) else "S11") + "\n"
        raise ValueError(f"vna_sim: unhandled query {cmd!r}")

    def query_ascii_values(self, cmd):
        self._check()
        up = cmd.upper()
        if "FREQ" in up and "DATA" in up:
            return list(self._freq())
        if "SDAT" in up or "FDAT" in up:
            name = self._settings()["defs"][self.selected - 1]
            g = self._sparam(name)
            if "SDAT" in up:
                out = np.empty(g.size * 2)
                out[0::2], out[1::2] = g.real, g.imag
            else:
                out = np.zeros(g.size * 2)
                out[0::2] = 20 * np.log10(np.maximum(np.abs(g), 1e-15))
            if (STATE["sweeps"] - 1) in CONFIG["short_at"]:
                return list(out[:-4])           # truncated reply
            return list(out)
        raise ValueError(f"vna_sim: unhandled query {cmd!r}")


def open_sim():
    """Stand in for ResourceManager.open_resource; returns (manager, instrument)."""
    if STATE["settings"] is None:
        reset()
    STATE["opens"] += 1
    if STATE["open_fail_left"] > 0 and STATE["opens"] > CONFIG["open_fail_start"]:
        STATE["open_fail_left"] -= 1
        raise _lost()
    return None, SimInstrument()
