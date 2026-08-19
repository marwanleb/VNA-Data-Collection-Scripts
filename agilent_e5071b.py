"""Data collection from the Agilent E5071B ENA over LAN (VXI-11).

Three collection modes live in this one file (named after the instrument):

  collect   Continuous S-parameter logging (default). A setup dialog
            (checkboxes) lets you pick which S-parameters to collect
            (S11/S21/S12/S22) and which quantities to record/plot (mag dB,
            phase, real, imag). The sweep settings (number of points, IF
            bandwidth, start/stop frequency) are read from the instrument and
            pre-filled so they match the VNA's own setup by default; edit them
            to change the sweep. A sampling period (seconds between sweeps)
            paces the time series. Each sweep is read as complex (SDATA) and the
            chosen quantities are derived from it, logged to one growing CSV, and
            shown on a live plot. The instrument's sweep + trace setup is saved
            and restored on exit. Press q to quit (Ctrl+C or closing the plot
            window also stop it safely).

  drift     Drift modeling replication: the same logging as collect, but built
            to be left alone for hours. Runs for a fixed duration (3.5 h by
            default) and stops itself, logs monotonic elapsed time next to the
            wall clock, plots the tracked point against time, rides out LAN /
            instrument hiccups by retrying and reconnecting, keeps the machine
            awake, guards disk space, and writes a sidecar .log of the whole
            session.

  snapshot  Capture a few S11 sweeps headless and save a mag/phase plot PNG
            (no GUI).

Usage:
  python agilent_e5071b.py                    # same as: collect
  python agilent_e5071b.py collect
  python agilent_e5071b.py drift              # 3.5 h drift run
  python agilent_e5071b.py drift --hours 1 --no-plot
  python agilent_e5071b.py drift --no-dialog  # start with the VNA's own sweep
  python agilent_e5071b.py drift --simulate   # dry run, no instrument needed
  python agilent_e5071b.py snapshot

All collected data (CSV, PNG, drift .log) is written to the data/ folder next
to this script unless --outdir says otherwise.
"""

import argparse
import csv
import os
import shutil
import time
from datetime import datetime, timedelta

try:
    import msvcrt  # Windows only: read a q keypress from the terminal
except ImportError:
    msvcrt = None  # macOS/Linux: quit via the plot window (q) or Ctrl+C

import numpy as np
import matplotlib
import matplotlib.pyplot as plt
import pyvisa

VNA_IP = "192.168.0.10"
RESOURCE = f"TCPIP0::{VNA_IP}::inst0::INSTR"
CHANNEL = 1
TIMEOUT_MS = 20000

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

SPARAMS = ["S11", "S21", "S12", "S22"]

# label -> (csv key, function(real, imag) -> values)
FORMATS = [
    ("Mag (dB)", "mag_db", lambda re, im: 20 * np.log10(np.maximum(np.hypot(re, im), 1e-15))),
    ("Phase (deg)", "phase_deg", lambda re, im: np.degrees(np.arctan2(im, re))),
    ("Real", "real", lambda re, im: re),
    ("Imag", "imag", lambda re, im: im),
]

# --- drift run tuning -------------------------------------------------------
DRIFT_HOURS = 3.5            # default unattended run length
DRIFT_INTERVAL_S = 10.0      # default seconds between sweeps for a drift run
RETRY_DELAYS = (1, 2, 5, 10, 20, 30) + (60,) * 29  # ~30 min of reconnect backoff,
                                                     # long enough to ride out a grid outage
PLOT_EVERY_S = 2.0           # cap on live-plot redraws
PROGRESS_EVERY_S = 30.0      # cap on progress lines
FSYNC_EVERY = 20             # push the CSV to disk every N sweeps
DISK_CHECK_EVERY = 100       # re-check free space every N sweeps
MIN_FREE_BYTES = 256 * 1024 * 1024

SIMULATE = False  # --simulate: talk to vna_sim instead of the instrument


def data_path(name):
    """Resolve an output file into the data/ folder, creating it if needed.

    A name that already carries a directory is left as-is so callers can still
    write somewhere specific; a bare filename lands in data/.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.dirname(name):
        return name
    return os.path.join(DATA_DIR, name)


def fmt_hms(seconds):
    seconds = int(max(seconds, 0))
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m{seconds % 60:02d}s"


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n:.0f} B"
        n /= 1024


def keep_awake(on):
    """Stop Windows sleeping mid-run; no-op on other platforms."""
    if os.name != "nt":
        return
    try:
        import ctypes
        es_continuous, es_system_required = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            es_continuous | (es_system_required if on else 0))
    except Exception:
        pass


# --- instrument -------------------------------------------------------------

def open_vna(timeout_ms=TIMEOUT_MS):
    """Open the analyzer and put it in ASCII transfer mode.

    Returns (resource_manager, instrument); hold on to both, dropping the
    manager can close the session underneath you.
    """
    if SIMULATE:
        import vna_sim
        rm, vna = vna_sim.open_sim()
    else:
        rm = pyvisa.ResourceManager("@py")
        vna = rm.open_resource(RESOURCE)
    vna.timeout = timeout_ms
    vna.write(":FORMat:DATA ASCii")
    return rm, vna


def read_sweep_setup(vna):
    return dict(
        points=int(float(vna.query(f":SENSe{CHANNEL}:SWEep:POINts?"))),
        ifbw=float(vna.query(f":SENSe{CHANNEL}:BANDwidth?")),
        fstart=float(vna.query(f":SENSe{CHANNEL}:FREQuency:STARt?")),
        fstop=float(vna.query(f":SENSe{CHANNEL}:FREQuency:STOP?")),
    )


def read_setup():
    """Read the VNA's current sweep settings to pre-fill the dialog."""
    rm, v = open_vna()
    try:
        return read_sweep_setup(v)
    finally:
        v.close()


def save_instrument_state(vna):
    """Snapshot the sweep, trace and trigger setup so we can put it all back."""
    orig = read_sweep_setup(vna)
    orig["trig"] = vna.query(":TRIGger:SEQuence:SOURce?").strip()
    orig["ntr"] = int(float(vna.query(f":CALCulate{CHANNEL}:PARameter:COUNt?")))
    orig["defs"] = [vna.query(f":CALCulate{CHANNEL}:PARameter{i}:DEFine?").strip()
                    for i in range(1, orig["ntr"] + 1)]
    return orig


def apply_config(vna, cfg, params):
    """Apply the requested sweep + traces and arm bus triggering."""
    vna.write(f":SENSe{CHANNEL}:FREQuency:STARt {cfg['fstart']}")
    vna.write(f":SENSe{CHANNEL}:FREQuency:STOP {cfg['fstop']}")
    vna.write(f":SENSe{CHANNEL}:SWEep:POINts {cfg['points']}")
    vna.write(f":SENSe{CHANNEL}:BANDwidth {cfg['ifbw']}")
    vna.write(f":CALCulate{CHANNEL}:PARameter:COUNt {len(params)}")
    for i, p in enumerate(params, start=1):
        vna.write(f":CALCulate{CHANNEL}:PARameter{i}:DEFine {p}")
    vna.write(":TRIGger:SEQuence:SOURce BUS")
    vna.write(f":INITiate{CHANNEL}:CONTinuous ON")


def restore_instrument_state(vna, orig):
    vna.write(f":SENSe{CHANNEL}:FREQuency:STARt {orig['fstart']}")
    vna.write(f":SENSe{CHANNEL}:FREQuency:STOP {orig['fstop']}")
    vna.write(f":SENSe{CHANNEL}:SWEep:POINts {orig['points']}")
    vna.write(f":SENSe{CHANNEL}:BANDwidth {orig['ifbw']}")
    vna.write(f":CALCulate{CHANNEL}:PARameter:COUNt {orig['ntr']}")
    for i, d in enumerate(orig["defs"], start=1):
        vna.write(f":CALCulate{CHANNEL}:PARameter{i}:DEFine {d}")
    vna.write(f":TRIGger:SEQuence:SOURce {orig['trig']}")
    vna.write(f":INITiate{CHANNEL}:CONTinuous ON")


def read_freq_axis(vna):
    return np.array(vna.query_ascii_values(f":SENSe{CHANNEL}:FREQuency:DATA?"))


def acquire_sweep(vna, params, npoints=None):
    """Trigger one sweep and read every selected parameter as complex data.

    npoints, when given, is enforced: a short read (a truncated VXI-11 reply)
    raises instead of quietly logging a ragged sweep.
    """
    vna.write(":TRIGger:SEQuence:SINGle")
    vna.query("*OPC?")
    out = {}
    for i, p in enumerate(params, start=1):
        vna.write(f":CALCulate{CHANNEL}:PARameter{i}:SELect")
        s = np.array(vna.query_ascii_values(f":CALCulate{CHANNEL}:SELected:DATA:SDATa?"))
        if npoints is not None and s.size != 2 * npoints:
            raise ValueError(f"{p}: expected {2 * npoints} values from SDATa?, got {s.size}")
        out[p] = (s[0::2], s[1::2])
    return out


def estimate_sweep_time(points, ifbw, params):
    """Rough seconds per acquisition, used to size timeouts and projections."""
    directions = len({p[2] for p in params}) or 1  # Sxy is driven from port y
    return directions * points * (1.0 / ifbw + 150e-6) + 0.06 * len(params) + 0.15


def build_header(params, fmts, elapsed=False):
    header = ["timestamp"] + (["elapsed_s"] if elapsed else []) + ["sweep", "freq_Hz"]
    for p in params:
        for _, key, _ in fmts:
            header.append(f"{p}_{key}")
    return header


# --- setup dialog -----------------------------------------------------------

def setup_dialog(defaults, drift_hours=None):
    """Pop the checkbox dialog. Returns a config dict or None if cancelled.

    Passing drift_hours switches it to drift-run mode: a run-duration field, a
    paced default sampling period, and a warning if the settings would fill the
    disk.
    """
    import tkinter as tk
    from tkinter import messagebox

    is_drift = drift_hours is not None
    root = tk.Tk()
    root.title("VNA Drift Run Setup" if is_drift else "VNA Collection Setup")
    root.resizable(False, False)
    cfg = {}

    sp_vars = {p: tk.BooleanVar(value=(p == "S11")) for p in SPARAMS}
    fmt_vars = {lbl: tk.BooleanVar(value=(lbl == "Mag (dB)" or (is_drift and lbl == "Phase (deg)")))
                for lbl, _, _ in FORMATS}
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name_var = tk.StringVar(value=f"{'drift' if is_drift else 'vna'}_{stamp}.csv")
    rate_var = tk.StringVar(value=f"{DRIFT_INTERVAL_S:g}" if is_drift else "0")
    hours_var = tk.StringVar(value=f"{drift_hours:g}") if is_drift else None
    pts_var = tk.StringVar(value=str(defaults["points"]))
    ifbw_var = tk.StringVar(value=f"{defaults['ifbw']:.0f}")
    fstart_var = tk.StringVar(value=f"{defaults['fstart'] / 1e9:.6g}")
    fstop_var = tk.StringVar(value=f"{defaults['fstop'] / 1e9:.6g}")

    f1 = tk.LabelFrame(root, text="S-parameters", padx=10, pady=6)
    f1.grid(row=0, column=0, padx=10, pady=8, sticky="ew")
    for i, p in enumerate(SPARAMS):
        tk.Checkbutton(f1, text=p, variable=sp_vars[p]).grid(row=0, column=i, sticky="w", padx=6)

    f2 = tk.LabelFrame(root, text="Record / plot", padx=10, pady=6)
    f2.grid(row=1, column=0, padx=10, pady=8, sticky="ew")
    for i, (lbl, _, _) in enumerate(FORMATS):
        tk.Checkbutton(f2, text=lbl, variable=fmt_vars[lbl]).grid(row=0, column=i, sticky="w", padx=6)

    f3 = tk.LabelFrame(root, text="Sweep (pre-filled from VNA)", padx=10, pady=6)
    f3.grid(row=2, column=0, padx=10, pady=8, sticky="ew")
    grid = [("Points:", pts_var), ("IF bandwidth (Hz):", ifbw_var),
            ("Start (GHz):", fstart_var), ("Stop (GHz):", fstop_var)]
    for r, (lbl, var) in enumerate(grid):
        tk.Label(f3, text=lbl).grid(row=r, column=0, sticky="w", pady=1)
        tk.Entry(f3, textvariable=var, width=14).grid(row=r, column=1, padx=6, pady=1)

    f4 = tk.LabelFrame(root, text="Sampling", padx=10, pady=6)
    f4.grid(row=3, column=0, padx=10, pady=8, sticky="ew")
    tk.Label(f4, text="Seconds between sweeps (0 = fastest):").grid(row=0, column=0, sticky="w")
    tk.Entry(f4, textvariable=rate_var, width=8).grid(row=0, column=1, padx=6)
    if is_drift:
        tk.Label(f4, text="Run duration (hours):").grid(row=1, column=0, sticky="w")
        tk.Entry(f4, textvariable=hours_var, width=8).grid(row=1, column=1, padx=6)

    f5 = tk.LabelFrame(root, text="Output file", padx=10, pady=6)
    f5.grid(row=4, column=0, padx=10, pady=8, sticky="ew")
    tk.Entry(f5, textvariable=name_var, width=40).grid(row=0, column=0, padx=2)

    def num(var, name, cast=float, lo=None):
        try:
            val = cast(var.get())
        except ValueError:
            raise ValueError(f"{name} must be a number.")
        if lo is not None and val < lo:
            raise ValueError(f"{name} must be >= {lo}.")
        return val

    def on_start():
        params = [p for p in SPARAMS if sp_vars[p].get()]
        fmts = [(lbl, key, fn) for lbl, key, fn in FORMATS if fmt_vars[lbl].get()]
        if not params:
            messagebox.showwarning("VNA", "Select at least one S-parameter.")
            return
        if not fmts:
            messagebox.showwarning("VNA", "Select at least one quantity to record.")
            return
        try:
            points = num(pts_var, "Points", int, lo=2)
            ifbw = num(ifbw_var, "IF bandwidth", float, lo=1)
            fstart = num(fstart_var, "Start", float, lo=0) * 1e9
            fstop = num(fstop_var, "Stop", float, lo=0) * 1e9
            interval = num(rate_var, "Sampling period", float, lo=0)
            hours = num(hours_var, "Run duration", float, lo=0.001) if is_drift else None
        except ValueError as e:
            messagebox.showwarning("VNA", str(e))
            return
        if fstop <= fstart:
            messagebox.showwarning("VNA", "Stop frequency must be greater than start.")
            return
        name = name_var.get().strip().strip('"') or f"vna_{stamp}.csv"
        if not name.lower().endswith(".csv"):
            name += ".csv"
        if is_drift:
            est = estimate_sweep_time(points, ifbw, params)
            sweeps = max(1, int(hours * 3600 / max(interval, est)))
            size = points * sweeps * (58 + 13 * len(params) * len(fmts))
            if interval and interval < est:
                messagebox.showwarning(
                    "VNA", f"A sweep takes about {est:.1f}s with these settings, longer than "
                           f"the {interval:g}s sampling period. The run will free-run at "
                           f"~{est:.1f}s per sweep instead.")
            if size > 1 << 30 and not messagebox.askokcancel(
                    "VNA", f"This will write roughly {fmt_bytes(size)} "
                           f"({sweeps} sweeps x {points} points).\n\n"
                           f"Raise the sampling period to shrink it. Continue anyway?"):
                return
        cfg.update(params=params, formats=fmts, interval=interval, csv_path=name,
                   points=points, ifbw=ifbw, fstart=fstart, fstop=fstop)
        if is_drift:
            cfg["hours"] = hours
        root.destroy()

    tk.Button(root, text="Start", width=16, command=on_start).grid(row=5, column=0, pady=10)
    root.mainloop()
    return cfg or None


def default_config(defaults, params=("S11",), labels=("Mag (dB)", "Phase (deg)"),
                   interval=0.0, hours=None, name=None):
    """Build a config without the dialog, for --no-dialog / scripted starts."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cfg = dict(defaults)
    cfg.update(
        params=list(params),
        formats=[(lbl, key, fn) for lbl, key, fn in FORMATS if lbl in labels],
        interval=interval,
        csv_path=name or f"{'drift' if hours else 'vna'}_{stamp}.csv",
    )
    if hours:
        cfg["hours"] = hours
    return cfg


def quit_requested():
    if msvcrt is None:
        return False
    while msvcrt.kbhit():
        if msvcrt.getch() in (b"q", b"Q"):
            return True
    return False


# --- collect ----------------------------------------------------------------

def collect(cfg):
    params, fmts = cfg["params"], cfg["formats"]
    interval = cfg["interval"]
    csv_path = data_path(cfg["csv_path"])

    rm, vna = open_vna()
    print("Connected:", vna.query("*IDN?").strip())

    orig = save_instrument_state(vna)
    apply_config(vna, cfg, params)

    freq = read_freq_axis(vna)
    fghz = freq / 1e9
    print(f"Params: {','.join(params)} | Quantities: {','.join(l for l,_,_ in fmts)}")
    print(f"Points: {freq.size} | {freq[0]/1e6:.3f} MHz to {freq[-1]/1e9:.3f} GHz | "
          f"IFBW {cfg['ifbw']:.0f} Hz | interval {interval}s")
    print("Logging to:", csv_path)
    print("To stop: press q in the plot window, or Ctrl+C here.")

    # live plot: one subplot per quantity, one line per S-parameter
    stop = {"flag": False}
    plt.ion()
    fig, axes = plt.subplots(len(fmts), 1, figsize=(9, 2.6 * len(fmts) + 1), sharex=True)
    if len(fmts) == 1:
        axes = [axes]
    fig.canvas.mpl_connect("close_event", lambda _e: stop.update(flag=True))
    lines = {}
    for ax, (lbl, _, _) in zip(axes, fmts):
        for p in params:
            (lines[(p, lbl)],) = ax.plot(fghz, np.zeros_like(fghz), label=p)
        ax.set_ylabel(lbl)
        ax.grid(True)
        ax.legend(loc="upper right", fontsize=8)
        if lbl == "Phase (deg)":
            ax.set_ylim(-180, 180)
    axes[-1].set_xlabel("Frequency (GHz)")

    header = build_header(params, fmts)
    sweep = 0
    try:
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            while not stop["flag"] and not quit_requested():
                t0 = time.monotonic()
                raw = acquire_sweep(vna, params, freq.size)
                vals = {(p, key): fn(*raw[p]) for p in params for _, key, fn in fmts}
                ts = datetime.now().isoformat(timespec="milliseconds")

                for i in range(freq.size):
                    row = [ts, sweep, f"{freq[i]:.1f}"]
                    for p in params:
                        for _, key, _ in fmts:
                            row.append(f"{vals[(p, key)][i]:.8g}")
                    w.writerow(row)
                f.flush()

                for (lbl, key, fn) in fmts:
                    for p in params:
                        lines[(p, lbl)].set_ydata(vals[(p, key)])
                for ax, (lbl, _, _) in zip(axes, fmts):
                    if lbl != "Phase (deg)":
                        ax.relim()
                        ax.autoscale_view()
                fig.suptitle(f"Sweep {sweep}  |  {ts}")
                plt.pause(0.01)

                sweep += 1
                print(f"Sweep {sweep} logged at {ts}")

                while interval > 0 and time.monotonic() - t0 < interval:
                    if stop["flag"] or quit_requested():
                        break
                    plt.pause(0.05)
    except KeyboardInterrupt:
        print("\nStopped (Ctrl+C).")
    finally:
        try:
            restore_instrument_state(vna, orig)
        except Exception as e:
            print("Warning: could not restore instrument setup:", e)
        try:
            vna.close()
        except Exception:
            pass
        print(f"Done. {sweep} sweeps saved to {csv_path}")
        plt.ioff()
        plt.show()


# --- drift ------------------------------------------------------------------

def drift(cfg, plot=True):
    """Drift modeling replication: log on a fixed schedule for cfg['hours'].

    Built for an unattended run, so every failure mode that would otherwise
    throw away hours of measurement is handled: transient VISA errors retry and
    reconnect, short reads are rejected before they reach the CSV, the disk is
    checked before and during the run, and the instrument is restored on every
    exit path. Stops itself when the duration is up.
    """
    params, fmts = cfg["params"], cfg["formats"]
    interval, hours = cfg["interval"], cfg["hours"]
    duration = hours * 3600.0
    csv_path = data_path(cfg["csv_path"])
    log_path = os.path.splitext(csv_path)[0] + ".log"
    logf = open(log_path, "a", buffering=1)

    def log(msg):
        logf.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")
        print(msg, flush=True)

    def wait(dt):
        """Sleep dt seconds while keeping the plot window alive."""
        end = time.monotonic() + dt
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            if plot:
                plt.pause(min(0.2, left))
            else:
                time.sleep(min(0.2, left))

    est = estimate_sweep_time(cfg["points"], cfg["ifbw"], params)
    timeout_ms = max(TIMEOUT_MS, int(est * 4000) + 5000)

    state = {}

    def connect():
        state["rm"], state["vna"] = open_vna(timeout_ms)
        return state["vna"]

    stop = {"flag": False, "why": "duration reached"}
    counters = {"errors": 0, "reconnects": 0, "slow": 0}
    sweep = 0
    t_start = time.monotonic()
    deadline = t_start + duration

    def connect_retry():
        """Open the link, riding out a VNA that is still booting or busy."""
        err = None
        for attempt, delay in enumerate((0.0,) + RETRY_DELAYS):
            try:
                if delay:
                    wait(delay)
                    log(f"  connect retry {attempt}/{len(RETRY_DELAYS)}...")
                return connect()
            except Exception as e:
                err = e
                log(f"  connect failed: {type(e).__name__}: {e}")
            if time.monotonic() >= deadline:
                break
        raise err

    try:
        vna = connect_retry()
        log(f"Connected: {vna.query('*IDN?').strip()}")
        orig = save_instrument_state(vna)
        apply_config(vna, cfg, params)
        freq = read_freq_axis(vna)
    except Exception as e:
        log(f"ABORT: could not set up the instrument at {RESOURCE}: {type(e).__name__}: {e}")
        log("Check that the VNA is on, that `ping 192.168.0.10` replies, and that no "
            "other program holds the LAN session (the ENA serves one client at a time).")
        try:
            state["vna"].close()
        except Exception:
            pass
        logf.close()
        return

    npoints = freq.size
    fghz = freq / 1e9
    freq_str = [f"{x:.1f}" for x in freq]

    def reconnect():
        """Rebuild the link from scratch and re-apply our sweep."""
        try:
            state["vna"].close()
        except Exception:
            pass
        v = connect()
        v.query("*IDN?")
        apply_config(v, cfg, params)
        if read_freq_axis(v).size != npoints:
            raise RuntimeError(f"frequency grid changed on reconnect "
                               f"(expected {npoints} points)")
        counters["reconnects"] += 1
        return v

    def acquire_retry():
        """One sweep, riding out transient faults. Returns None if it gave up."""
        for attempt, delay in enumerate((0.0,) + RETRY_DELAYS):
            try:
                if attempt:
                    wait(delay)
                    log(f"  retry {attempt}/{len(RETRY_DELAYS)}: reconnecting...")
                    reconnect()
                return acquire_sweep(state["vna"], params, npoints)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                counters["errors"] += 1
                log(f"  sweep {sweep} failed: {type(e).__name__}: {e}")
            if stop["flag"] or time.monotonic() >= deadline:
                break  # no point retrying into the end of the run
        return None

    try:
        # --- preflight: catch anything that would kill the run at hour 3 ----
        ncols = len(params) * len(fmts)
        period = max(interval, est)
        expect_sweeps = max(1, int(duration / period))
        projected = npoints * expect_sweeps * (58 + 13 * ncols)
        free = shutil.disk_usage(os.path.dirname(csv_path)).free
        ends = datetime.now() + timedelta(seconds=duration)

        log("=" * 72)
        log(f"Drift modeling replication  |  {hours:g} h  |  started {datetime.now():%Y-%m-%d %H:%M:%S}")
        log(f"Params: {','.join(params)} | Quantities: {','.join(l for l,_,_ in fmts)}")
        log(f"Points: {npoints} | {freq[0]/1e6:.3f} MHz to {freq[-1]/1e9:.3f} GHz | "
            f"IFBW {cfg['ifbw']:.0f} Hz")
        log(f"Sampling period: {interval:g}s | estimated sweep time: {est:.2f}s | "
            f"VISA timeout: {timeout_ms/1000:.0f}s")
        log(f"Expected: ~{expect_sweeps} sweeps, ~{fmt_bytes(projected)} of CSV "
            f"({fmt_bytes(free)} free)")
        log(f"Ends at ~{ends:%Y-%m-%d %H:%M:%S} (or press q / Ctrl+C / close the plot)")
        log(f"CSV: {csv_path}")
        log(f"Log: {log_path}")

        if interval and interval < est:
            log(f"NOTE: a sweep takes ~{est:.2f}s, longer than the {interval:g}s period; "
                f"sweeps will free-run instead of keeping to the schedule.")
        if projected + MIN_FREE_BYTES > free:
            stop["why"] = "not enough disk space"
            log(f"ABORT: {fmt_bytes(projected)} projected vs {fmt_bytes(free)} free. "
                f"Raise the sampling period, cut the points, or use --outdir.")
            return
        if "onedrive" in os.path.abspath(csv_path).lower():
            log("NOTE: the output folder is inside OneDrive, which will try to sync the "
                "CSV while it grows. Consider --outdir on a local disk for long runs.")
        log("=" * 72)

        keep_awake(True)

        # --- live plot: sweeps on top, the tracked point vs time underneath --
        axes, lines, ax_t, tlines = [], {}, None, {}
        if plot:
            plt.ion()
            fig = plt.figure(figsize=(10, 2.4 * len(fmts) + 3.4))
            gs = fig.add_gridspec(len(fmts) + 1, 1, hspace=0.45)
            for i, (lbl, _, _) in enumerate(fmts):
                ax = fig.add_subplot(gs[i], sharex=axes[0] if axes else None)
                for p in params:
                    (lines[(p, lbl)],) = ax.plot(fghz, np.zeros_like(fghz), label=p)
                ax.set_ylabel(lbl)
                ax.grid(True)
                ax.legend(loc="upper right", fontsize=8)
                if lbl == "Phase (deg)":
                    ax.set_ylim(-180, 180)
                axes.append(ax)
            axes[-1].set_xlabel("Frequency (GHz)")
            ax_t = fig.add_subplot(gs[len(fmts)])
            for p in params:
                (tlines[p],) = ax_t.plot([], [], label=p)
            ax_t.set_xlabel("Elapsed (min)")
            ax_t.set_ylabel(fmts[0][0])
            ax_t.grid(True)
            ax_t.legend(loc="upper right", fontsize=8)
            fig.canvas.mpl_connect(
                "close_event", lambda _e: stop.update(flag=True, why="plot window closed"))

        header = build_header(params, fmts, elapsed=True)
        track = {"idx": None}
        tmin, series = [], {p: [] for p in params}
        last_plot = last_progress = -1e9

        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            t_start = time.monotonic()
            deadline = t_start + duration

            while True:
                t0 = time.monotonic()
                if t0 >= deadline:
                    break
                if stop["flag"] or quit_requested():
                    stop.update(flag=True, why=stop["why"] if stop["flag"] else "q pressed")
                    break

                raw = acquire_retry()
                t_acq = time.monotonic()
                if raw is None:
                    stop.update(flag=True, why="instrument unreachable, gave up retrying")
                    log(f"STOPPING: {stop['why']}")
                    break

                elapsed = t_acq - t_start
                vals = {(p, key): fn(*raw[p]) for p in params for _, key, fn in fmts}
                ts = datetime.now().isoformat(timespec="milliseconds")
                el = f"{elapsed:.3f}"

                for i in range(npoints):
                    row = [ts, el, sweep, freq_str[i]]
                    for p in params:
                        for _, key, _ in fmts:
                            row.append(f"{vals[(p, key)][i]:.8g}")
                    w.writerow(row)
                f.flush()
                if sweep % FSYNC_EVERY == 0:
                    os.fsync(f.fileno())

                # the tracked point: the first sweep's resonance, else mid-band
                if track["idx"] is None:
                    mag = vals.get((params[0], "mag_db"))
                    track["idx"] = int(np.argmin(mag)) if mag is not None else npoints // 2
                    log(f"Tracking {fghz[track['idx']]:.4f} GHz for the drift trace.")
                idx = track["idx"]
                tmin.append(elapsed / 60.0)
                for p in params:
                    series[p].append(float(vals[(p, fmts[0][1])][idx]))

                sweep += 1
                if t_acq - t0 > max(interval, est) * 1.5:
                    counters["slow"] += 1

                if plot and t_acq - last_plot >= PLOT_EVERY_S:
                    for (lbl, key, _) in fmts:
                        for p in params:
                            lines[(p, lbl)].set_ydata(vals[(p, key)])
                    for ax, (lbl, _, _) in zip(axes, fmts):
                        if lbl != "Phase (deg)":
                            ax.relim()
                            ax.autoscale_view()
                    for p in params:
                        tlines[p].set_data(tmin, series[p])
                    ax_t.relim()
                    ax_t.autoscale_view()
                    ax_t.set_title(f"Drift at {fghz[idx]:.4f} GHz", fontsize=9)
                    fig.suptitle(f"Sweep {sweep}  |  {fmt_hms(elapsed)} of {fmt_hms(duration)}"
                                 f"  |  {ts}")
                    plt.pause(0.001)
                    last_plot = t_acq

                if t_acq - last_progress >= PROGRESS_EVERY_S:
                    log(f"[{100 * elapsed / duration:5.1f}%] sweep {sweep} | "
                        f"{fmt_hms(elapsed)} elapsed, {fmt_hms(duration - elapsed)} left | "
                        f"errors {counters['errors']} reconnects {counters['reconnects']} | "
                        f"{fmt_bytes(os.path.getsize(csv_path))}")
                    last_progress = t_acq

                if sweep % DISK_CHECK_EVERY == 0:
                    if shutil.disk_usage(os.path.dirname(csv_path)).free < MIN_FREE_BYTES:
                        stop.update(flag=True, why="disk nearly full")
                        log(f"STOPPING: {stop['why']} — keeping the {sweep} sweeps logged so far.")
                        break

                while interval > 0 and time.monotonic() - t0 < interval:
                    if stop["flag"] or quit_requested():
                        stop.update(flag=True,
                                    why=stop["why"] if stop["flag"] else "q pressed")
                        break
                    wait(min(0.2, interval))
                if stop["flag"]:
                    break
    except KeyboardInterrupt:
        stop.update(flag=True, why="Ctrl+C")
        print()
    finally:
        keep_awake(False)
        try:
            restore_instrument_state(state.get("vna", vna), orig)
        except Exception as e:
            log(f"Warning: could not restore instrument setup: {e}")
        try:
            state.get("vna", vna).close()
        except Exception:
            pass
        total = time.monotonic() - t_start
        log(f"Done ({stop['why']}). {sweep} sweeps over {fmt_hms(total)} "
            f"| errors {counters['errors']} | reconnects {counters['reconnects']} "
            f"| slow sweeps {counters['slow']}")
        if os.path.exists(csv_path):
            log(f"Saved {fmt_bytes(os.path.getsize(csv_path))} to {csv_path}")
        logf.close()
        if plot:
            plt.ioff()
            plt.show()


# --- entry points -----------------------------------------------------------

def get_defaults():
    try:
        return read_setup()
    except Exception as e:
        print("Could not read VNA setup, using fallback defaults:", e)
        return dict(points=201, ifbw=70000.0, fstart=300e3, fstop=8.5e9)


def run_collect(dialog=True):
    """Continuous S-parameter logging with the setup dialog and live plot."""
    defaults = get_defaults()
    config = setup_dialog(defaults) if dialog else default_config(defaults)
    if config:
        collect(config)
    else:
        print("Cancelled.")


def run_drift(hours=DRIFT_HOURS, plot=True, dialog=True):
    """Drift modeling replication run (3.5 h by default)."""
    defaults = get_defaults()
    config = (setup_dialog(defaults, drift_hours=hours) if dialog
              else default_config(defaults, interval=DRIFT_INTERVAL_S, hours=hours))
    if config:
        drift(config, plot=plot)
    else:
        print("Cancelled.")


def run_snapshot(n=8, out="vna_snapshot.png"):
    """Capture a few S11 sweeps and save a mag/phase plot PNG (no GUI)."""
    plt.switch_backend("Agg")
    out_path = data_path(out)

    rm, vna = open_vna()
    print("Connected:", vna.query("*IDN?").strip())

    vna.write(f":CALCulate{CHANNEL}:PARameter1:SELect")
    orig = vna.query(":TRIGger:SEQuence:SOURce?").strip()
    vna.write(":TRIGger:SEQuence:SOURce BUS")
    vna.write(f":INITiate{CHANNEL}:CONTinuous ON")

    freq = read_freq_axis(vna)
    fghz = freq / 1e9

    mags, phases = [], []
    for k in range(n):
        vna.write(":TRIGger:SEQuence:SINGle")
        vna.query("*OPC?")
        fdat = np.array(vna.query_ascii_values(f":CALCulate{CHANNEL}:SELected:DATA:FDATa?"))
        sdat = np.array(vna.query_ascii_values(f":CALCulate{CHANNEL}:SELected:DATA:SDATa?"))
        db = fdat[0::2]
        re, im = sdat[0::2], sdat[1::2]
        mags.append(db)
        phases.append(np.degrees(np.arctan2(im, re)))
        print(f"Sweep {k}: min {db.min():.2f} dB at {fghz[db.argmin()]:.4f} GHz")

    vna.write(f":TRIGger:SEQuence:SOURce {orig}")
    vna.write(f":INITiate{CHANNEL}:CONTinuous ON")
    vna.close()

    fig, (ax_mag, ax_ph) = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
    for i, m in enumerate(mags):
        ax_mag.plot(fghz, m, color="C0", alpha=0.15 + 0.6 * i / max(n - 1, 1))
    ax_mag.plot(fghz, mags[-1], color="C3", lw=1.5, label="latest")
    ax_mag.set_ylabel("S11 (dB)")
    ax_mag.set_title(f"E5071B S11  |  {n} sweeps overlaid  |  {freq[0]/1e6:.3f} MHz to {freq[-1]/1e9:.3f} GHz")
    ax_mag.grid(True)
    ax_mag.legend(loc="lower right")
    ax_ph.plot(fghz, phases[-1], color="C2")
    ax_ph.set_ylabel("Phase (deg)")
    ax_ph.set_xlabel("Frequency (GHz)")
    ax_ph.set_ylim(-180, 180)
    ax_ph.grid(True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    print("Saved:", out_path)


def main():
    global DATA_DIR, SIMULATE
    parser = argparse.ArgumentParser(description="Agilent E5071B ENA data collection.")
    parser.add_argument("mode", nargs="?", default="collect",
                        choices=["collect", "drift", "snapshot"],
                        help="collect: continuous logging with live plot (default). "
                             "drift: fixed-duration drift modeling replication run. "
                             "snapshot: a few S11 sweeps saved to a PNG (no GUI).")
    parser.add_argument("--hours", type=float, default=DRIFT_HOURS,
                        help=f"drift run length in hours (default {DRIFT_HOURS}).")
    parser.add_argument("--no-plot", action="store_true",
                        help="drift: log without the live plot window.")
    parser.add_argument("--no-dialog", action="store_true",
                        help="skip the setup dialog and use the VNA's current sweep.")
    parser.add_argument("--outdir", help="write output here instead of data/.")
    parser.add_argument("--simulate", action="store_true",
                        help="dry run against a fake instrument (vna_sim), no hardware.")
    args = parser.parse_args()

    if args.outdir:
        DATA_DIR = os.path.abspath(args.outdir)
    if args.simulate:
        SIMULATE = True
        print("SIMULATE: talking to the fake instrument in vna_sim.py, not the VNA.")
    if args.no_plot:
        plt.switch_backend("Agg")

    if args.mode == "snapshot":
        run_snapshot()
    elif args.mode == "drift":
        run_drift(hours=args.hours, plot=not args.no_plot, dialog=not args.no_dialog)
    else:
        run_collect(dialog=not args.no_dialog)


if __name__ == "__main__":
    main()
