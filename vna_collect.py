"""Continuous S-parameter collection from Agilent E5071B ENA over LAN (VXI-11).

A setup dialog (checkboxes) lets you pick which S-parameters to collect
(S11/S21/S12/S22) and which quantities to record/plot (mag dB, phase,
real, imag). The sweep settings (number of points, IF bandwidth, start/
stop frequency) are read from the instrument and pre-filled so they match
the VNA's own setup by default; edit them to change the sweep. A sampling
period (seconds between sweeps) paces the time series.

Each sweep is read as complex (SDATA) and the chosen quantities are
derived from it, logged to one growing CSV, and shown on a live plot.
The instrument's sweep + trace setup is saved and restored on exit.
Press q to quit (Ctrl+C or closing the plot window also stop it safely).
"""

import csv
import time
from datetime import datetime

try:
    import msvcrt  # Windows only: read a q keypress from the terminal
except ImportError:
    msvcrt = None  # macOS/Linux: quit via the plot window (q) or Ctrl+C

import numpy as np
import matplotlib.pyplot as plt
import pyvisa
import tkinter as tk
from tkinter import messagebox

VNA_IP = "192.168.0.10"
RESOURCE = f"TCPIP0::{VNA_IP}::inst0::INSTR"
CHANNEL = 1
TIMEOUT_MS = 20000

SPARAMS = ["S11", "S21", "S12", "S22"]

# label -> (csv key, function(real, imag) -> values)
FORMATS = [
    ("Mag (dB)", "mag_db", lambda re, im: 20 * np.log10(np.maximum(np.hypot(re, im), 1e-15))),
    ("Phase (deg)", "phase_deg", lambda re, im: np.degrees(np.arctan2(im, re))),
    ("Real", "real", lambda re, im: re),
    ("Imag", "imag", lambda re, im: im),
]


def read_setup():
    """Read the VNA's current sweep settings to pre-fill the dialog."""
    rm = pyvisa.ResourceManager("@py")
    v = rm.open_resource(RESOURCE)
    v.timeout = TIMEOUT_MS
    d = dict(
        points=int(float(v.query(f":SENSe{CHANNEL}:SWEep:POINts?"))),
        ifbw=float(v.query(f":SENSe{CHANNEL}:BANDwidth?")),
        fstart=float(v.query(f":SENSe{CHANNEL}:FREQuency:STARt?")),
        fstop=float(v.query(f":SENSe{CHANNEL}:FREQuency:STOP?")),
    )
    v.close()
    return d


def setup_dialog(defaults):
    """Pop the checkbox dialog. Returns a config dict or None if cancelled."""
    root = tk.Tk()
    root.title("VNA Collection Setup")
    root.resizable(False, False)
    cfg = {}

    sp_vars = {p: tk.BooleanVar(value=(p == "S11")) for p in SPARAMS}
    fmt_vars = {lbl: tk.BooleanVar(value=(lbl == "Mag (dB)")) for lbl, _, _ in FORMATS}
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name_var = tk.StringVar(value=f"vna_{stamp}.csv")
    rate_var = tk.StringVar(value="0")
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
        except ValueError as e:
            messagebox.showwarning("VNA", str(e))
            return
        if fstop <= fstart:
            messagebox.showwarning("VNA", "Stop frequency must be greater than start.")
            return
        name = name_var.get().strip().strip('"') or f"vna_{stamp}.csv"
        if not name.lower().endswith(".csv"):
            name += ".csv"
        cfg.update(params=params, formats=fmts, interval=interval, csv_path=name,
                   points=points, ifbw=ifbw, fstart=fstart, fstop=fstop)
        root.destroy()

    tk.Button(root, text="Start", width=16, command=on_start).grid(row=5, column=0, pady=10)
    root.mainloop()
    return cfg or None


def quit_requested():
    if msvcrt is None:
        return False
    while msvcrt.kbhit():
        if msvcrt.getch() in (b"q", b"Q"):
            return True
    return False


def collect(cfg):
    params, fmts = cfg["params"], cfg["formats"]
    interval, csv_path = cfg["interval"], cfg["csv_path"]

    rm = pyvisa.ResourceManager("@py")
    vna = rm.open_resource(RESOURCE)
    vna.timeout = TIMEOUT_MS
    print("Connected:", vna.query("*IDN?").strip())
    vna.write(":FORMat:DATA ASCii")

    # save current sweep + trace + trigger setup for restore
    orig = dict(
        points=int(float(vna.query(f":SENSe{CHANNEL}:SWEep:POINts?"))),
        ifbw=float(vna.query(f":SENSe{CHANNEL}:BANDwidth?")),
        fstart=float(vna.query(f":SENSe{CHANNEL}:FREQuency:STARt?")),
        fstop=float(vna.query(f":SENSe{CHANNEL}:FREQuency:STOP?")),
        trig=vna.query(":TRIGger:SEQuence:SOURce?").strip(),
        ntr=int(float(vna.query(f":CALCulate{CHANNEL}:PARameter:COUNt?"))),
    )
    orig["defs"] = [vna.query(f":CALCulate{CHANNEL}:PARameter{i}:DEFine?").strip()
                    for i in range(1, orig["ntr"] + 1)]

    # apply requested sweep + traces
    vna.write(f":SENSe{CHANNEL}:FREQuency:STARt {cfg['fstart']}")
    vna.write(f":SENSe{CHANNEL}:FREQuency:STOP {cfg['fstop']}")
    vna.write(f":SENSe{CHANNEL}:SWEep:POINts {cfg['points']}")
    vna.write(f":SENSe{CHANNEL}:BANDwidth {cfg['ifbw']}")
    vna.write(f":CALCulate{CHANNEL}:PARameter:COUNt {len(params)}")
    for i, p in enumerate(params, start=1):
        vna.write(f":CALCulate{CHANNEL}:PARameter{i}:DEFine {p}")
    vna.write(":TRIGger:SEQuence:SOURce BUS")
    vna.write(f":INITiate{CHANNEL}:CONTinuous ON")

    freq = np.array(vna.query_ascii_values(f":SENSe{CHANNEL}:FREQuency:DATA?"))
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

    header = ["timestamp", "sweep", "freq_Hz"]
    for p in params:
        for _, key, _ in fmts:
            header.append(f"{p}_{key}")

    def acquire():
        vna.write(":TRIGger:SEQuence:SINGle")
        vna.query("*OPC?")
        out = {}
        for i, p in enumerate(params, start=1):
            vna.write(f":CALCulate{CHANNEL}:PARameter{i}:SELect")
            s = np.array(vna.query_ascii_values(f":CALCulate{CHANNEL}:SELected:DATA:SDATa?"))
            out[p] = (s[0::2], s[1::2])
        return out

    sweep = 0
    try:
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            while not stop["flag"] and not quit_requested():
                t0 = time.monotonic()
                raw = acquire()
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
                        lines[(p, lbl)].set_ydata(fn(*raw[p]))
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
        vna.write(f":SENSe{CHANNEL}:FREQuency:STARt {orig['fstart']}")
        vna.write(f":SENSe{CHANNEL}:FREQuency:STOP {orig['fstop']}")
        vna.write(f":SENSe{CHANNEL}:SWEep:POINts {orig['points']}")
        vna.write(f":SENSe{CHANNEL}:BANDwidth {orig['ifbw']}")
        vna.write(f":CALCulate{CHANNEL}:PARameter:COUNt {orig['ntr']}")
        for i, d in enumerate(orig["defs"], start=1):
            vna.write(f":CALCulate{CHANNEL}:PARameter{i}:DEFine {d}")
        vna.write(f":TRIGger:SEQuence:SOURce {orig['trig']}")
        vna.write(f":INITiate{CHANNEL}:CONTinuous ON")
        vna.close()
        print(f"Done. {sweep} sweeps saved to {csv_path}")
        plt.ioff()
        plt.show()


if __name__ == "__main__":
    try:
        defaults = read_setup()
    except Exception as e:
        print("Could not read VNA setup, using fallback defaults:", e)
        defaults = dict(points=201, ifbw=70000.0, fstart=300e3, fstop=8.5e9)
    config = setup_dialog(defaults)
    if config:
        collect(config)
    else:
        print("Cancelled.")
