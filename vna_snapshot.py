"""Capture a few S11 sweeps and save a mag/phase plot PNG (no GUI)."""

import sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pyvisa

VNA_IP = "192.168.0.10"
RESOURCE = f"TCPIP0::{VNA_IP}::inst0::INSTR"
CH = 1
N = 8
OUT = "vna_snapshot.png"

rm = pyvisa.ResourceManager("@py")
vna = rm.open_resource(RESOURCE)
vna.timeout = 20000
print("Connected:", vna.query("*IDN?").strip())

vna.write(":FORMat:DATA ASCii")
vna.write(f":CALCulate{CH}:PARameter1:SELect")
orig = vna.query(":TRIGger:SEQuence:SOURce?").strip()
vna.write(":TRIGger:SEQuence:SOURce BUS")
vna.write(f":INITiate{CH}:CONTinuous ON")

freq = np.array(vna.query_ascii_values(f":SENSe{CH}:FREQuency:DATA?"))
fghz = freq / 1e9

mags, phases = [], []
for k in range(N):
    vna.write(":TRIGger:SEQuence:SINGle")
    vna.query("*OPC?")
    fdat = np.array(vna.query_ascii_values(f":CALCulate{CH}:SELected:DATA:FDATa?"))
    sdat = np.array(vna.query_ascii_values(f":CALCulate{CH}:SELected:DATA:SDATa?"))
    db = fdat[0::2]
    re, im = sdat[0::2], sdat[1::2]
    mags.append(db)
    phases.append(np.degrees(np.arctan2(im, re)))
    print(f"Sweep {k}: min {db.min():.2f} dB at {fghz[db.argmin()]:.4f} GHz")

vna.write(f":TRIGger:SEQuence:SOURce {orig}")
vna.write(f":INITiate{CH}:CONTinuous ON")
vna.close()

fig, (ax_mag, ax_ph) = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
for i, m in enumerate(mags):
    ax_mag.plot(fghz, m, color="C0", alpha=0.15 + 0.6 * i / max(N - 1, 1))
ax_mag.plot(fghz, mags[-1], color="C3", lw=1.5, label="latest")
ax_mag.set_ylabel("S11 (dB)")
ax_mag.set_title(f"E5071B S11  |  {N} sweeps overlaid  |  {freq[0]/1e6:.3f} MHz to {freq[-1]/1e9:.3f} GHz")
ax_mag.grid(True)
ax_mag.legend(loc="lower right")
ax_ph.plot(fghz, phases[-1], color="C2")
ax_ph.set_ylabel("Phase (deg)")
ax_ph.set_xlabel("Frequency (GHz)")
ax_ph.set_ylim(-180, 180)
ax_ph.grid(True)
fig.tight_layout()
fig.savefig(OUT, dpi=110)
print("Saved:", OUT)
