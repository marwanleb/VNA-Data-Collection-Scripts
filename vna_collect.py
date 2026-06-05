"""Continuous S11 collection from Agilent E5071B ENA over LAN (VXI-11).
Reads formatted (dB) + complex (real/imag) each sweep, logs to one growing
CSV, and shows a live magnitude/phase plot. Uses the instrument's existing
setup as-is. Stop with Ctrl+C or by closing the plot window.
"""

import csv
import time
from datetime import datetime

import numpy as np
import matplotlib.pyplot as plt
import pyvisa

VNA_IP = "192.168.0.10"
RESOURCE = f"TCPIP0::{VNA_IP}::inst0::INSTR"
CHANNEL = 1
TRACE = 1
TIMEOUT_MS = 20000

run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
csv_path = f"vna_s11_{run_stamp}.csv"

rm = pyvisa.ResourceManager("@py")
vna = rm.open_resource(RESOURCE)
vna.timeout = TIMEOUT_MS
print("Connected:", vna.query("*IDN?").strip())

vna.write(":FORMat:DATA ASCii")
vna.write(f":CALCulate{CHANNEL}:PARameter{TRACE}:SELect")

orig_trig = vna.query(":TRIGger:SEQuence:SOURce?").strip()
vna.write(":TRIGger:SEQuence:SOURce BUS")
vna.write(f":INITiate{CHANNEL}:CONTinuous ON")

freq = np.array(vna.query_ascii_values(f":SENSe{CHANNEL}:FREQuency:DATA?"))
fghz = freq / 1e9
print(f"Points: {freq.size} | {freq[0]/1e6:.3f} MHz to {freq[-1]/1e9:.3f} GHz")
print("Logging to:", csv_path)

stop = {"flag": False}

def on_close(_evt):
    stop["flag"] = True

plt.ion()
fig, (ax_mag, ax_ph) = plt.subplots(2, 1, figsize=(9, 7), sharex=True)
fig.canvas.mpl_connect("close_event", on_close)
(line_mag,) = ax_mag.plot(fghz, np.zeros_like(fghz))
(line_ph,) = ax_ph.plot(fghz, np.zeros_like(fghz))
ax_mag.set_ylabel("S11 (dB)")
ax_mag.grid(True)
ax_ph.set_ylabel("Phase (deg)")
ax_ph.set_xlabel("Frequency (GHz)")
ax_ph.set_ylim(-180, 180)
ax_ph.grid(True)


def acquire():
    vna.write(":TRIGger:SEQuence:SINGle")
    vna.query("*OPC?")
    fdat = np.array(vna.query_ascii_values(":CALCulate{}:SELected:DATA:FDATa?".format(CHANNEL)))
    sdat = np.array(vna.query_ascii_values(":CALCulate{}:SELected:DATA:SDATa?".format(CHANNEL)))
    db = fdat[0::2]
    re, im = sdat[0::2], sdat[1::2]
    return db, re, im


sweep = 0
try:
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "sweep", "freq_Hz", "s11_db", "real", "imag"])
        while not stop["flag"]:
            db, re, im = acquire()
            ts = datetime.now().isoformat(timespec="milliseconds")
            for i in range(freq.size):
                w.writerow([ts, sweep, f"{freq[i]:.1f}", f"{db[i]:.6f}",
                            f"{re[i]:.8f}", f"{im[i]:.8f}"])
            f.flush()

            phase = np.degrees(np.arctan2(im, re))
            line_mag.set_ydata(db)
            line_ph.set_ydata(phase)
            ax_mag.relim()
            ax_mag.autoscale_view()
            fig.suptitle(f"Sweep {sweep}  |  {ts}")
            plt.pause(0.01)

            sweep += 1
            print(f"Sweep {sweep}: min {db.min():.2f} dB at "
                  f"{fghz[db.argmin()]:.4f} GHz")
except KeyboardInterrupt:
    print("\nStopped by user.")
finally:
    vna.write(f":TRIGger:SEQuence:SOURce {orig_trig}")
    vna.write(f":INITiate{CHANNEL}:CONTinuous ON")
    vna.close()
    print(f"Done. {sweep} sweeps saved to {csv_path}")
    plt.ioff()
    plt.show()
