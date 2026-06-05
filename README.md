# VNA Data Collection

Continuous S-parameter logging from the **Agilent/Keysight E5071B ENA** vector
network analyzer over Ethernet (LAN / VXI-11). Run `vna_collect.py`, pick what
you want in a checkbox dialog, and it streams a live plot while logging every
sweep to a CSV.

> The VNA itself is already configured — **you do not need to touch the
> instrument.** You only need to get your computer onto the same network and
> install the Python dependencies. Both steps are below.

---

## What it does

- Pick any of **S11 / S21 / S12 / S22** and any of **Mag (dB) / Phase (deg) /
  Real / Imag** from a dialog.
- Sweep settings (**# points, IF bandwidth, start/stop frequency**) are
  pre-filled from the VNA's current setup; edit them if you want.
- Reads each sweep as complex data, derives your chosen quantities, shows a
  **live plot** (one subplot per quantity, one line per S-parameter), and logs
  everything to **one growing CSV**.
- Restores the instrument's sweep/trace setup when you quit.

---

## 1. Get on the VNA's network

The analyzer has a fixed address: **`192.168.0.10`**. Your computer just needs
to be on the same `192.168.0.x` subnet.

### Connect the cable
Plug an Ethernet cable from your computer to the VNA (or to the lab
switch/router the VNA is connected to). No Ethernet port on your laptop? Use a
USB-to-Ethernet adapter.

### Give your computer a matching static IP
Use any address from `192.168.0.2` to `192.168.0.254` **except `.10`** (that's
the VNA). `192.168.0.50` is a fine choice. Subnet mask `255.255.255.0`, no
gateway.

#### Windows
1. **Settings → Network & Internet → Ethernet** (click your Ethernet adapter)
2. **IP assignment → Edit → Manual**
3. Turn **IPv4 on** and enter:
   - IP address: `192.168.0.50`
   - Subnet mask: `255.255.255.0`
   - Gateway / DNS: leave blank
4. **Save**

#### macOS
1. **System Settings → Network** → select your **Ethernet** (or USB-Ethernet
   adapter)
2. Click **Details…** → **TCP/IP**
3. **Configure IPv4: Manually**
   - IP Address: `192.168.0.50`
   - Subnet Mask: `255.255.255.0`
   - Router: leave blank
4. **OK**

   *(Older macOS: System Preferences → Network → select Ethernet → Configure
   IPv4: Manually.)*

### Verify the connection
- **Windows:** `ping 192.168.0.10`
- **macOS:** `ping -c 4 192.168.0.10`

You should get replies. If it times out, re-check the cable, your static IP, and
that the mask is `255.255.255.0`.

---

## 2. Install Python and dependencies

Requires **Python 3.9+**.

### Windows
1. Install Python from <https://www.python.org/downloads/> — **tick "Add
   python.exe to PATH"** during setup.
2. Open **PowerShell** in this folder (the one containing `vna_collect.py`).
3. (Recommended) create and activate a virtual environment:
   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```
   If activation is blocked, run this once in the same window, then retry:
   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   ```
4. Install the dependencies:
   ```powershell
   pip install -r requirements.txt
   ```

### macOS
1. Install Python from <https://www.python.org/downloads/> (the python.org
   installer includes Tkinter, which the dialog needs).
2. Open **Terminal** in this folder.
3. (Recommended) create and activate a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
4. Install the dependencies:
   ```bash
   pip install -r requirements.txt
   ```

> **Tkinter note (macOS):** if you see `ModuleNotFoundError: No module named
> '_tkinter'`, you're on a Python without Tk. Easiest fix is the python.org
> installer above; with Homebrew Python run `brew install python-tk`.

No instrument driver / Keysight IO Libraries install is needed — the script uses
the pure-Python `pyvisa-py` backend.

---

## 3. Run it

- **Windows:** `python vna_collect.py`
- **macOS:** `python3 vna_collect.py`  (or just `python` inside an active venv)

A setup window appears:

| Section | What it does |
|---|---|
| **S-parameters** | Tick which of S11/S21/S12/S22 to collect |
| **Record / plot** | Tick the quantities to log & plot (Mag dB, Phase, Real, Imag) |
| **Sweep** | Points, IF bandwidth, start/stop freq — pre-filled from the VNA |
| **Sampling** | Seconds between sweeps (`0` = as fast as possible) |
| **Output file** | CSV file name |

Click **Start**. A live plot opens and data logging begins.

### Stopping
Stop any of these ways — all restore the instrument and flush the file safely:
- Press **q** with the **plot window** focused (works on Windows and macOS)
- Press **Ctrl+C** in the terminal
- Close the plot window

---

## Output CSV

One row per frequency point per sweep, in long format. Columns are
`timestamp, sweep, freq_Hz`, then one column per **parameter × quantity**. For
example, S11 + S21 with Mag + Phase gives:

```
timestamp, sweep, freq_Hz, S11_mag_db, S11_phase_deg, S21_mag_db, S21_phase_deg
```

Load in Python with:
```python
import pandas as pd
df = pd.read_csv("your_file.csv")
sweep0 = df[df.sweep == 0]        # one sweep
```

---

## Notes & troubleshooting

- **Wrong/changed VNA address?** Edit `VNA_IP` at the top of `vna_collect.py`.
- **Connection times out / "VI_ERROR":** confirm `ping 192.168.0.10` works, the
  VNA is powered on, and no other program currently holds the LAN connection
  (the instrument serves one client at a time).
- **`ModuleNotFoundError`:** dependencies aren't installed — run
  `pip install -r requirements.txt` (inside your venv if you made one).
- **Calibration:** S21/S12/S22 are only *calibrated* if a full 2-port cal is
  active on the VNA; with a 1-port S11 cal they read raw. Changing points or
  frequency moves off the cal grid (the ENA interpolates) — fine for monitoring,
  re-cal for best accuracy.
- Generated data (`*.csv`) and `__pycache__/` are git-ignored.
