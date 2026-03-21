# QtC — BBS Client for Amateur Radio  
**v0.9.0-beta** · Linux · Windows 11 · Raspberry Pi

> *QTC — Q-code for "I have messages for you."*

QtC is a modern desktop BBS client for amateur radio operators.  
It connects to LinBPQ / BPQ32 nodes via **VARA HF** and **Telnet**,  
and handles mail download, compose, send, address book, and a clean three-pane GUI.

Developed by **Bill Johnson KC9MTP** — Valparaiso, Indiana.

---

## Features

- **VARA HF** — RF connect with busy-channel detection and PTT control
- **Telnet** — for local testing and LAN-connected nodes
- **Mail check** — `LM` with new-only (PN) or full (PN+PY) options
- **Auto-download** — new personal mail downloads automatically on connect
- **Compose & reply** — personal (P) and bulletin (B) message types
- **Outbox queue** — stage messages, send in one batch when connected
- **Address book** — auto-fill in compose, use-count ranked dropdown
- **Progress tracking** — download and send progress in toolbar and status bar
- **VARA link stats** — live bitrate, SN, and bandwidth in status bar
- **Terminal view** — clean dumb terminal with quick-command buttons
- **Debug view** — verbose session log for troubleshooting
- **Folder badges** — Inbox (N new), Outbox (N), Sent (N)
- **Mark all read** — one-click bulk read in inbox
- **PTT control** — RTS or DTR via serial port
- **Cross-platform** — Linux, Windows 11, Raspberry Pi OS

---

## Requirements

- Python 3.10 or newer (3.12 recommended)
- PyQt6
- pyserial (for PTT)
- VARA HF modem (registered or trial) — *run natively on Windows; run under Wine or Crossover on Linux / Pi*
- A VOX or serial PTT interface
- USB Soundcard — Signalink, Rigblaster, Digirig
- A LinBPQ / BPQ32 node to connect to

---

## Installation

### Raspberry Pi 4 / 5 (Raspberry Pi OS Bookworm)

```bash
sudo apt install python3-pyqt6 python3-pyserial
tar -xzf QtC-0.9.0-beta.tar.gz
cd QtC-0.9.0-beta
./install.sh
```

Once installed, you can launch QtC two ways:
- **Desktop icon** — look for the QtC icon in your applications menu or desktop
- **Terminal** — type `qtc` from anywhere and press Enter

**Pi notes:**
- VARA HF does not run natively on Pi — most Pi users have the best luck with
  Pi-Apps and Winetricks. Search online for install and troubleshooting instructions
- For a pure Telnet setup (LAN node), no VARA or PTT needed
- PTT serial ports: `/dev/ttyUSB0`, `/dev/ttyACM0`, etc.
- Serial port permission error? Run: `sudo usermod -aG dialout $USER` then log out and back in

---

### Linux (Fedora / Ubuntu / Debian)

```bash
tar -xzf QtC-0.9.0-beta.tar.gz
cd QtC-0.9.0-beta
./install.sh
```

The installer checks all five source files, installs dependencies, and places  
a `qtc` launcher in `~/.local/bin/`. Config and messages are preserved on reinstall.

Once installed, you can launch QtC two ways:
- **Desktop icon** — look for the QtC icon in your applications menu or desktop
- **Terminal** — type `qtc` from anywhere and press Enter

To run manually without installing:
```bash
pip install -r requirements.txt --break-system-packages
python3 main_window.py
```

Package manager alternatives:
```bash
# Fedora
sudo dnf install python3-pyqt6 python3-pyserial

# Ubuntu / Debian
sudo apt install python3-pyqt6 python3-pyserial
```

---

### Windows 11

QtC installs itself — you just need to get Python on your computer first,
then run the installer script once. After that, a **QtC shortcut appears on
your Desktop** and you never need to touch the command line again.

---

#### Step 1 — Install Python (one time only)

Python is the programming language QtC is written in. You only do this once.

1. Open your web browser and go to: **https://www.python.org/downloads/**
2. Click the big yellow **"Download Python 3.x.x"** button
3. Run the downloaded installer
4. **Important:** On the very first screen, check the box that says
   **"Add Python to PATH"** before clicking Install Now

   > If you miss this checkbox, the installer won't be able to find Python.
   > If that happens, uninstall Python and run the installer again with the
   > box checked.

5. Click **Install Now** and let it finish

To verify Python installed correctly:
- Press **Windows key + R**, type `cmd`, press Enter
- In the black window, type `python --version` and press Enter
- You should see something like `Python 3.12.x`
- Close the window

---

#### Step 2 — Extract the QtC files

QtC comes in a `.tar.gz` archive file (similar to a `.zip`).

**Windows 11 can open it directly:**
1. Right-click `QtC-0.9.0-beta.tar.gz`
2. Select **Extract All...**
3. Click **Extract**
4. You may need to extract twice — once for the `.tar.gz` → `.tar`,
   then again for the `.tar` → folder. Keep extracting until you see
   a folder called `QtC-0.9.0-beta` containing `.py` files.

**Or use 7-Zip** (free, recommended): https://www.7-zip.org/
Right-click the file → **7-Zip → Extract Here**

When done, you should have a folder called `QtC-0.9.0-beta` with files
like `main_window.py`, `install.ps1`, etc. inside it.

---

#### Step 3 — Run the installer

Windows has a security feature that blocks scripts downloaded from the
internet. You need to do one extra step to allow the QtC installer to run.

1. Open the `QtC-0.9.0-beta` folder
2. Hold **Shift** and right-click on **`install.ps1`**
3. Select **"Run with PowerShell"**

   > A blue PowerShell window will appear. This is normal — it's the
   > installer running.

4. If Windows shows a warning like *"Windows protected your PC"* or
   *"Do you want to allow this app?"* — click **"Run anyway"** or **"Yes"**

5. If you see a red error like *"cannot be loaded because running scripts
   is disabled"*, do this:
   - Press **Windows key**, search for **PowerShell**
   - Right-click PowerShell → **Run as administrator**
   - Type this command and press Enter:
     ```
     Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
     ```
   - Type `Y` and press Enter
   - Close PowerShell, then go back to Step 3 and try again

6. The installer will check for Python, install the required libraries
   (PyQt6 and pyserial), and create a **QtC shortcut on your Desktop**

7. When it says **"QtC installed successfully!"** press Enter to close

---

#### Step 4 — Run QtC

Double-click the **QtC** shortcut on your Desktop. That's it.

The first time it opens, a setup window will appear automatically —
enter your callsign and BBS details there.

---

**Windows notes:**
- VARA HF must be running before you click Connect in QtC
- Windows Firewall may ask to allow QtC when it first connects to VARA
  (ports 8300/8301) — click **Allow access**
- PTT serial ports show as `COM3`, `COM4`, etc. — select yours in
  **Settings → PTT** and click **Test PTT** to verify
- If QtC won't start after install, open a Command Prompt and run:
  `python %APPDATA%\qtc\main_window.py` — any error will show there

---

## First-Time Setup

1. Open **File → Settings → My Station**
   - Enter your callsign, name, QTH, and Home BBS (Home BBS is optional)
   - *New to BBS? Just fill in your Name and Callsign — that is all you need
     to get connected. Add QTH, Home BBS, and other details later as you
     learn your way around.*
2. Go to the **BBS List** tab
   - Edit the example entry or add your own
   - Set transport (VARA HF recommended for HF operations)
3. Go to the **PTT** tab
   - Select your COM / serial port and signal (RTS recommended for Digirig)
   - Click **Test PTT** to verify keying
4. Close Settings and select your BBS from the dropdown
5. Click **⚡ Connect**

On your first connection, QtC will ask whether to download all personal messages  
or new ones only. After that, only new messages (PN) are fetched automatically —  
keeping sessions short and efficient over slow RF links.

---

## VARA Setup — Beta

- VARA HF must be running on the **same machine** as QtC
- VARA command port: **8300** (default)
- VARA data port: **8301** (default)
- Set your callsign in VARA to match the callsign in QtC Settings
- Set VARA's own PTT setting to **None** — QtC keys the radio via RTS/DTR directly
- *Access to VARA on a remote machine via IP address — coming soon*

---

## Data Storage

| Platform | Location |
|---|---|
| Linux / Pi | `~/.local/share/qtc/` |
| Windows | `%APPDATA%\qtc\` |

- `messages.db` — SQLite database (inbox, outbox, sent, contacts)
- `config.json` — all settings

Both files are preserved when you reinstall or upgrade.

---

## Source Files

| File | Purpose |
|---|---|
| `main_window.py` | GUI — PyQt6 main window, toolbar, mail view, terminal, dialogs |
| `bbs_session.py` | BBS login, mail check, message download and send |
| `transport.py` | VARA HF and Telnet transports, VaraControl |
| `ptt.py` | PTT control via serial RTS/DTR |
| `database.py` | SQLite inbox/outbox/sent/contacts |

---

## Known Limitations (Beta)

- No rig control yet — set frequency manually on your radio
- Bulletin sending via compose works — bulletin *receiving* requires subscriptions in Settings → Bulletins
- VARA FM support in code but not yet field tested
- Direwolf and Soundmodem transports are planned for a future release
- Windows 11 install script not yet tested on real hardware (0.9.0)

---

## Changelog

### 0.9.5-beta (2026-03-18)
- Fixed: Bulletin body blank in preview pane after download
- Fixed: Second+ bulletin subscriptions returning empty — flush and settle between L> commands
- Fixed: Terminal mode not disabled during bulletin download

### 0.9.4-beta (2026-03-17)
- Bulletin support — Settings → Bulletins tab with subscription list
- Bulletin folder panel — category subfolders with unread counts
- Bulletin selection dialog — checkbox list with size and time estimates
- Search now includes Bulletins scope
- Dark mode note text readable in both modes
- Search highlight — amber highlight on matched terms in message body

### 0.9.3-beta (2026-03-17)
- Fixed: VARA reconnect after BBS idle timeout — cmd monitor thread and PTT port now released on remote disconnect
- Fixed: Dynamic VARA reset delay after disconnect
- Message search — 🔍 button, scope dropdown, real-time filtering
- Fixed: VARA Error 111 on reconnect after failed RF connect (station not responding)
- Manual refresh — mail check logic implemented, button hidden pending field testing
- Mark All Read — bulk mark inbox as read in one click
- Inbox column widths — Date and Size fixed, Subject stretches to fill available space
- Date display normalized — Sent folder now shows `16-Mar` style (was `2026-03-16`)
- VARA link stats in status bar — live bitrate, SN, and bandwidth during connection
- App name corrected to **QtC** throughout
- README rewritten with QtC branding and complete install instructions

### 0.8.2-beta (2026-03-15)
- Windows 11 support — `install.ps1`, cross-platform data paths

### 0.8.1-beta
- Linux release — battle tested, demo-ready
- VARA HF RF connect/disconnect with busy channel detection
- Mail check, download with progress tracker
- Compose, reply, outbox queue, sequential send
- Address book with auto-fill and use-count ranking
- Terminal view, Debug view, folder badges

---

*73 de KC9MTP*
