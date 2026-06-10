"""Experiment-mode S-parameter collector for Agilent E5071B ENA over LAN.

All experiment parameters (S-params, quantities, sweep settings, output file,
number of samples, and sweeps per sample) are entered once in a setup dialog.
For each sample you are prompted in the terminal for only the sample name;
all sweeps for that sample then collect automatically. Every sweep is labelled
with its sample name and appended to a single CSV. The live plot overlays all
samples so you can compare them as you go. Press Ctrl+C to stop early; the
VNA is restored on exit.
"""

import csv
import re
from datetime import datetime
from pathlib import Path

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

FORMATS = [
    ("Mag (dB)",   "mag_db",    lambda re, im: 20 * np.log10(np.maximum(np.hypot(re, im), 1e-15))),
    ("Phase (deg)","phase_deg", lambda re, im: np.degrees(np.arctan2(im, re))),
    ("Real",       "real",      lambda re, im: re),
    ("Imag",       "imag",      lambda re, im: im),
]


def read_setup():
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
    root = tk.Tk()
    root.title("VNA Experiment Setup")
    root.resizable(False, False)
    cfg = {}

    sp_vars  = {p: tk.BooleanVar(value=(p == "S11")) for p in SPARAMS}
    fmt_vars = {lbl: tk.BooleanVar(value=(lbl == "Mag (dB)")) for lbl, _, _ in FORMATS}
    stamp    = datetime.now().strftime("%Y%m%d_%H%M%S")
    name_var    = tk.StringVar(value=f"experiment_{stamp}.csv")
    pts_var     = tk.StringVar(value=str(defaults["points"]))
    ifbw_var    = tk.StringVar(value=f"{defaults['ifbw']:.0f}")
    fstart_var  = tk.StringVar(value=f"{defaults['fstart'] / 1e9:.6g}")
    fstop_var   = tk.StringVar(value=f"{defaults['fstop'] / 1e9:.6g}")
    nsamples_var = tk.StringVar(value="20")
    nsweeps_var  = tk.StringVar(value="1")

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
    sweep_fields = [("Points:", pts_var), ("IF bandwidth (Hz):", ifbw_var),
                    ("Start (GHz):", fstart_var), ("Stop (GHz):", fstop_var)]
    for r, (lbl, var) in enumerate(sweep_fields):
        tk.Label(f3, text=lbl).grid(row=r, column=0, sticky="w", pady=1)
        tk.Entry(f3, textvariable=var, width=14).grid(row=r, column=1, padx=6, pady=1)

    f4 = tk.LabelFrame(root, text="Experiment", padx=10, pady=6)
    f4.grid(row=3, column=0, padx=10, pady=8, sticky="ew")
    tk.Label(f4, text="Number of samples:").grid(row=0, column=0, sticky="w", pady=1)
    tk.Entry(f4, textvariable=nsamples_var, width=8).grid(row=0, column=1, padx=6, pady=1)
    tk.Label(f4, text="Sweeps per sample:").grid(row=1, column=0, sticky="w", pady=1)
    tk.Entry(f4, textvariable=nsweeps_var, width=8).grid(row=1, column=1, padx=6, pady=1)

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
        fmts   = [(lbl, key, fn) for lbl, key, fn in FORMATS if fmt_vars[lbl].get()]
        if not params:
            messagebox.showwarning("VNA", "Select at least one S-parameter.")
            return
        if not fmts:
            messagebox.showwarning("VNA", "Select at least one quantity to record.")
            return
        try:
            points   = num(pts_var,      "Points",             int,   lo=2)
            ifbw     = num(ifbw_var,     "IF bandwidth",       float, lo=1)
            fstart   = num(fstart_var,   "Start",              float, lo=0) * 1e9
            fstop    = num(fstop_var,    "Stop",               float, lo=0) * 1e9
            nsamples = num(nsamples_var, "Number of samples",  int,   lo=1)
            nsweeps  = num(nsweeps_var,  "Sweeps per sample",  int,   lo=1)
        except ValueError as e:
            messagebox.showwarning("VNA", str(e))
            return
        if fstop <= fstart:
            messagebox.showwarning("VNA", "Stop frequency must be greater than start.")
            return
        name = name_var.get().strip().strip('"') or f"experiment_{stamp}.csv"
        if not name.lower().endswith(".csv"):
            name += ".csv"
        cfg.update(params=params, formats=fmts, csv_path=name,
                   points=points, ifbw=ifbw, fstart=fstart, fstop=fstop,
                   nsamples=nsamples, nsweeps=nsweeps)
        root.destroy()

    tk.Button(root, text="Start Experiment", width=20, command=on_start).grid(row=5, column=0, pady=10)
    root.mainloop()
    return cfg or None


def _safe_name(s):
    """Strip characters unsafe for filenames, cap length."""
    return re.sub(r'[^\w\-]', '_', s)[:40]


def collect(cfg):
    params, fmts = cfg["params"], cfg["formats"]
    csv_path = Path(cfg["csv_path"])
    nsamples, nsweeps = cfg["nsamples"], cfg["nsweeps"]

    rm = pyvisa.ResourceManager("@py")
    vna = rm.open_resource(RESOURCE)
    vna.timeout = TIMEOUT_MS
    print("Connected:", vna.query("*IDN?").strip())
    vna.write(":FORMat:DATA ASCii")

    # save current VNA state for restore on exit
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

    def _apply_setup(v):
        v.write(f":SENSe{CHANNEL}:FREQuency:STARt {cfg['fstart']}")
        v.write(f":SENSe{CHANNEL}:FREQuency:STOP {cfg['fstop']}")
        v.write(f":SENSe{CHANNEL}:SWEep:POINts {cfg['points']}")
        v.write(f":SENSe{CHANNEL}:BANDwidth {cfg['ifbw']}")
        v.write(f":CALCulate{CHANNEL}:PARameter:COUNt {len(params)}")
        for i, p in enumerate(params, start=1):
            v.write(f":CALCulate{CHANNEL}:PARameter{i}:DEFine {p}")
        v.write(":TRIGger:SEQuence:SOURce BUS")
        v.write(f":INITiate{CHANNEL}:CONTinuous ON")

    _apply_setup(vna)

    freq = np.array(vna.query_ascii_values(f":SENSe{CHANNEL}:FREQuency:DATA?"))
    fghz = freq / 1e9

    print(f"\nExperiment: {nsamples} samples × {nsweeps} sweep(s) each | "
          f"Params: {','.join(params)} | Quantities: {','.join(l for l,_,_ in fmts)}")
    print(f"Points: {freq.size} | {freq[0]/1e6:.3f} MHz – {freq[-1]/1e9:.3f} GHz | "
          f"IFBW {cfg['ifbw']:.0f} Hz")
    print(f"Saving to: {csv_path}  (+ one file per sample)")
    print("─" * 60)
    print("For each sample: type the name and press Enter — all sweeps collect automatically.")
    print("Press Ctrl+C at any time to stop early.")
    print("─" * 60)

    # live plot — new line added per sweep, all overlaid
    plt.ion()
    fig, axes = plt.subplots(len(fmts), 1, figsize=(9, 2.6 * len(fmts) + 1), sharex=True)
    if len(fmts) == 1:
        axes = [axes]
    for ax, (lbl, _, _) in zip(axes, fmts):
        ax.set_ylabel(lbl)
        ax.grid(True)
        if lbl == "Phase (deg)":
            ax.set_ylim(-180, 180)
    axes[-1].set_xlabel("Frequency (GHz)")
    fig.suptitle("Waiting for first sweep…")
    plt.tight_layout()
    plt.pause(0.01)

    prop_cycle = plt.rcParams["axes.prop_cycle"].by_key()["color"]

    header = ["sample", "timestamp", "sample_idx", "sweep", "freq_Hz"]
    for p in params:
        for _, key, _ in fmts:
            header.append(f"{p}_{key}")

    def acquire():
        nonlocal vna
        for attempt in range(2):
            try:
                vna.write(":TRIGger:SEQuence:SINGle")
                vna.query("*OPC?")
                out = {}
                for i, p in enumerate(params, start=1):
                    vna.write(f":CALCulate{CHANNEL}:PARameter{i}:SELect")
                    s = np.array(vna.query_ascii_values(
                        f":CALCulate{CHANNEL}:SELected:DATA:SDATa?"))
                    out[p] = (s[0::2], s[1::2])
                return out
            except Exception:
                if attempt == 0:
                    print("connection lost, reconnecting…", end=" ", flush=True)
                    try:
                        vna.close()
                    except Exception:
                        pass
                    vna = rm.open_resource(RESOURCE)
                    vna.timeout = TIMEOUT_MS
                    vna.write(":FORMat:DATA ASCii")
                    _apply_setup(vna)
                else:
                    raise

    completed = 0
    samples_done = 0
    try:
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)

            for sample_idx in range(nsamples):
                plt.pause(0.01)

                prompt = f"\n[Sample {sample_idx + 1}/{nsamples}] Name (blank to skip, Ctrl+C to stop): "
                try:
                    sample_name = input(prompt).strip()
                except EOFError:
                    print("\nInput closed — stopping.")
                    break

                if sample_name == "":
                    print(f"  Skipped sample {sample_idx + 1}.")
                    continue

                color = prop_cycle[sample_idx % len(prop_cycle)]
                samples_done += 1
                sample_rows = []

                for sweep_num in range(1, nsweeps + 1):
                    if nsweeps > 1:
                        print(f"  Sweep {sweep_num}/{nsweeps} '{sample_name}'…", end=" ", flush=True)
                    else:
                        print(f"  Collecting '{sample_name}'…", end=" ", flush=True)

                    raw = acquire()
                    ts = datetime.now().isoformat(timespec="milliseconds")
                    vals = {(p, key): fn(*raw[p]) for p in params for _, key, fn in fmts}

                    for i in range(freq.size):
                        row = [sample_name, ts, sample_idx + 1, sweep_num, f"{freq[i]:.1f}"]
                        for p in params:
                            for _, key, _ in fmts:
                                row.append(f"{vals[(p, key)][i]:.8g}")
                        sample_rows.append(row)
                        w.writerow(row)
                    f.flush()
                    completed += 1
                    print(f"done  ({ts})")

                    for ax, (lbl, key, fn) in zip(axes, fmts):
                        for p in params:
                            label = (f"{sample_name} {p}" if len(params) > 1 else sample_name) \
                                    if sweep_num == 1 else "_nolegend_"
                            ax.plot(fghz, fn(*raw[p]), color=color, alpha=0.75,
                                    linewidth=1.2, label=label)
                        if lbl != "Phase (deg)":
                            ax.relim()
                            ax.autoscale_view()
                        ax.legend(loc="upper right", fontsize=7,
                                  ncol=max(1, (sample_idx + 1) // 8))
                    fig.suptitle(f"Sample {sample_idx + 1}/{nsamples}  |  {sample_name}"
                                 + (f"  sweep {sweep_num}/{nsweeps}" if nsweeps > 1 else "")
                                 + f"  |  {ts}")
                    plt.pause(0.01)

                # write per-sample CSV immediately after all its sweeps finish
                sample_file = csv_path.parent / (
                    f"{csv_path.stem}_{sample_idx + 1:02d}_{_safe_name(sample_name)}.csv")
                with open(sample_file, "w", newline="") as sf:
                    sw = csv.writer(sf)
                    sw.writerow(header)
                    sw.writerows(sample_rows)
                print(f"  → {sample_file.name}")

    except KeyboardInterrupt:
        print("\nStopped early (Ctrl+C).")
    finally:
        try:
            vna.write(f":SENSe{CHANNEL}:FREQuency:STARt {orig['fstart']}")
            vna.write(f":SENSe{CHANNEL}:FREQuency:STOP {orig['fstop']}")
            vna.write(f":SENSe{CHANNEL}:SWEep:POINts {orig['points']}")
            vna.write(f":SENSe{CHANNEL}:BANDwidth {orig['ifbw']}")
            vna.write(f":CALCulate{CHANNEL}:PARameter:COUNt {orig['ntr']}")
            for i, d in enumerate(orig["defs"], start=1):
                vna.write(f":CALCulate{CHANNEL}:PARameter{i}:DEFine {d}")
            vna.write(f":TRIGger:SEQuence:SOURce {orig['trig']}")
            vna.write(f":INITiate{CHANNEL}:CONTinuous ON")
        except Exception as e:
            print(f"Warning: could not restore VNA settings ({e})")
        try:
            vna.close()
        except Exception:
            pass
        print(f"\nDone. {completed} sweep(s) across {samples_done} sample(s) saved to {csv_path}")
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
