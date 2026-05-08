QtC — BBS Client for Amateur Radio
Beta · Linux · Windows 11 · Raspberry Pi

  NOTE: QTC — Q-code for "I have messages for you."

QtC is a modern desktop BBS client for amateur radio operators.
It connects to LinBPQ / BPQ32 nodes via VARA HF, VARA FM, and Telnet,
and handles mail download, bulletin subscriptions, compose, send, address book,
and a clean three-pane GUI.

Developed by Bill Johnson KC9MTP — Valparaiso, Indiana.

------------------------------------------------------------------------

Features

- VARA HF / VARA FM — RF connect with busy-channel detection and PTT control
- Telnet — for local testing and LAN-connected nodes; auto-disconnects after mail check
- Mail check — LM with new-only (PN) or full (PN+PY) options
- Auto-download — new personal mail downloads automatically on connect
- Bulletins — subscribe to categories (SITREP, EWN, WX, etc.); browse in folder panel
- Compose & reply — personal (P) and bulletin (B) message types
- Outbox queue — stage messages, send in one batch when connected
- Address book — auto-fill in compose, use-count ranked dropdown
- Multi-select delete — Ctrl+click or Shift+click to select and delete multiple messages
- Message search — real-time filter with scope dropdown and amber highlight in preview
- Progress tracking — download and send progress in toolbar and status bar
- VARA link stats — live bitrate, SN, and bandwidth in status bar
- Terminal view — clean dumb terminal for manual BBS commands
- Debug view — verbose session log for troubleshooting
- Folder badges — Inbox (N new), Outbox (N), Bulletins (N new)
- Mark all read — one-click bulk read in inbox
- Dark mode — full Fusion dark palette, toggled in Settings -> App
- Font size — adjustable message font with live preview
- PTT control — RTS or DTR via serial port
- Cross-platform — Linux, Windows 11, Raspberry Pi OS

------------------------------------------------------------------------

Requirements

- Python 3.10 or newer (3.12 recommended)
- PyQt6
- pyserial (for PTT)
- VARA HF modem (registered or trial) — run natively on Windows;
  run under Wine or Crossover on Linux / Pi
- A VOX or serial PTT interface
- USB Soundcard — Signalink, Rigblaster, Digirig
- A LinBPQ / BPQ32 node to connect to

------------------------------------------------------------------------

Installation

Raspberry Pi 4 / 5 (Raspberry Pi OS Bookworm)

  sudo apt install python3-pyqt6 python3-pyserial
  tar -xzf QtC-<version>-beta.tar.gz
  cd QtC-<version>-beta
  ./install.sh

Once installed, launch QtC from your applications menu or type qtc in a terminal.

Pi notes:
- VARA HF does not run natively on Pi — most Pi users have the best luck with
  Pi-Apps and Winetricks
- For a pure Telnet setup (LAN node), no VARA or PTT needed
- PTT serial ports: /dev/ttyUSB0, /dev/ttyACM0, etc.
- Serial port permission error? Run: sudo usermod -aG dialout $USER
  then log out and back in

------------------------------------------------------------------------

Linux (Fedora / Ubuntu / Debian)

  tar -xzf QtC-<version>-beta.tar.gz
  cd QtC-<version>-beta
  ./install.sh

The installer checks all five source files, installs dependencies, and places
a qtc launcher in /usr/local/bin/. Config and messages are preserved on reinstall.

To run manually without installing:
  pip install -r requirements.txt --break-system-packages
  python3 main_window.py

Package manager alternatives:
  Fedora:         sudo dnf install python3-pyqt6 python3-pyserial
  Ubuntu/Debian:  sudo apt install python3-pyqt6 python3-pyserial

------------------------------------------------------------------------

Windows 11

QtC ships as a standalone exe — no Python required.

Step 1 — Download

  From the Releases page, download QtC-<version>-beta-windows.zip:
  https://github.com/Bill-Johnson/QtC/releases

Step 2 — Extract

  Right-click QtC-<version>-beta-windows.zip -> Extract All
  Result: a QtC\ folder containing QtC.exe and supporting files.

Step 3 — Run

  Double-click QtC.exe inside the extracted folder.

  If Windows shows "Windows protected your PC" — click More info -> Run anyway.
  This is expected for unsigned executables and only appears once.

Windows notes:
- VARA HF must be running before you click Connect in QtC
- Windows Firewall may ask to allow QtC on ports 8300/8301 — click Allow access
- PTT serial ports show as COM3, COM4, etc. — select yours in Settings -> PTT
- Config and messages are stored in %APPDATA%\qtc\ and preserved across updates

Running from source (advanced):
  If you prefer to run from Python directly, install.ps1 is still included in
  the Linux/Mac .tar.gz release. Requires Python 3.10+, PyQt6, and pyserial.

------------------------------------------------------------------------

First-Time Setup

1. Open File -> Settings -> My Station
   Enter callsign, name, QTH, and Home BBS
2. Go to the BBS List tab — add your BBS with transport (VARA HF or Telnet)
3. Go to the PTT tab — select serial port and signal (RTS recommended for Digirig)
4. Go to the Bulletins tab — enter category subscriptions (e.g. SITREP, EWN, WX)
5. Close Settings, select your BBS from the dropdown, and click Connect

On your first connection QtC will ask whether to download all personal messages
or new only. After that, only new messages (PN) are fetched automatically —
keeping sessions short and efficient over slow RF links.

------------------------------------------------------------------------

VARA Setup

- VARA HF must be running on the same machine as QtC
- VARA command port: 8300 (default)
- VARA data port: 8301 (default)
- Set your callsign in VARA to match the callsign in QtC Settings
- Set VARA's PTT setting to None — QtC keys the radio via RTS/DTR directly

------------------------------------------------------------------------

Data Storage

  Platform             Location
  Linux / Pi           ~/.local/share/qtc/
  Windows              %APPDATA%\qtc\

  messages.db — SQLite database (inbox, outbox, sent, bulletins, contacts)
  config.json — all settings

Both files are preserved when you reinstall or upgrade.

To force a full bulletin re-download:
  sqlite3 ~/.local/share/qtc/data/messages.db \
    "DELETE FROM bulletin_tombstones; DELETE FROM bulletins;"

------------------------------------------------------------------------

Source Files

  main_window.py  — GUI: PyQt6 main window, toolbar, mail view, terminal, dialogs
  bbs_session.py  — BBS login, mail check, message download and send
  transport.py    — VARA HF, VARA FM, and Telnet transports
  ptt.py          — PTT control via serial RTS/DTR
  database.py     — SQLite inbox/outbox/sent/bulletins/contacts

------------------------------------------------------------------------

Known Limitations (Beta)

- No rig control yet — set frequency manually on your radio
- VARA FM support in code but not yet field tested
- Direwolf and Soundmodem transports planned for a future release
- Windows exe available as a separate release asset — no Python required (see releases page)
- install.ps1 remains available for users who prefer running from source

------------------------------------------------------------------------

Changelog

0.12.0-beta (2026-05-07)
- Added: Splash screen — shown both by the PyInstaller bootloader
  (visible immediately on Windows .exe launch, before Python loads)
  and by an in-Python QSplashScreen while MainWindow constructs.
  Together these cover the slow first-run launch on Windows. Splash
  image (qtc_splash.png) is generated from scratch by a new
  make_splash.py Pillow script that reads APP_VERSION so the version
  line always matches the installed release.
- Added: "Save Log..." button in the Debug view — writes the verbose
  session monitor buffer to a plain-text .log file via a save dialog.
  Useful for capturing RF transfer traces.
- Changed: terminal "Get File" button relabeled to "File Download -
  YAPP" and widened to make the function obvious to both old-school
  and new amateur radio operators (YAPP has been the BBS file-transfer
  standard since Jeff Jacobsen WA7MBL published the RFC in 1986).
- Fixed: YAPP file download left LinBPQ stuck in transfer mode — file
  saved correctly but the next user command was rejected with
  "Unexpected message during YAPP Transfer. Transfer cancelled". Root
  cause: LinBPQ ends a YAPP session by sending a second SOH header
  with the same filename and size=0 as a YAPP-C batch end-of-session
  sentinel, not [EOT] as the prior implementation expected.
  YappReceiver now parses that sentinel and replies with NAK (0x15),
  releasing the BBS cleanly so subsequent commands work.
- Fixed: BBS prompt "de KC9MTP>" not shown in the terminal view after
  a successful YAPP download — read_until(">") in download_file()'s
  finally block consumed the prompt for protocol cleanup without
  re-emitting it; the prompt is now logged as [RX] so it appears in
  the terminal view the same way as after any other command.

0.10.10-beta (2026-04-14)
- Fixed: messages lost between sessions when running as exe — data_dir
  relative path resolved against working directory instead of
  %APPDATA%\qtc; now always anchored to _APP_DIR

0.10.9-beta (2026-04-14)
- Windows exe release — PyInstaller one-folder build; no Python required on
  target machine
- Fixed: app icon not found when running as frozen exe — sys.frozen guard
  replaces __file__-based icon path lookup

0.10.8-beta (2026-03-28)
- Fixed: outbox send cancelled immediately — LinBPQ splits "Enter Title
  (only):" across two TCP packets; previous fix matched on "nter " which
  fired on the first packet before "Title" arrived, hit the failure
  branch, and sent a bare Enter cancelling the message; fixed by waiting
  for "itle" which only matches once the full title prompt has arrived;
  simplified send_message to always expect title-then-body (LinBPQ
  always follows this sequence for SP CALL)

0.10.7-beta (2026-03-28)
- Fixed: outbox send failed silently — terminal monitor thread was
  consuming BBS prompts before send_message could read them; fixed by
  pausing the monitor in _run_send before calling send_message
- Fixed: second queued message sent into wrong BBS state when first
  send failed mid-compose — send_message now sends a bare Enter to
  cancel at the title prompt and restore the BBS > prompt
- Fixed: send_message now handles both LinBPQ prompt styles:
  "Enter Title (only):" and "Enter Message Text..." by waiting for
  "nter " and branching on content

0.10.6-beta (2026-03-28)
- Fixed: outbox send hung at "Enter Title (only):" — send_message was
  waiting for bare ":" which matched too early on the "Address @HOMEBBS
  added from HomeBBS" line LinBPQ emits before the title prompt; now
  waits for "itle" to ensure the correct prompt is consumed first
- Fixed: _expect("Enter Message") was case-sensitive; LinBPQ sends
  lowercase "Enter message"; changed to match "nter message"

0.10.5-beta (2026-03-28)
- Fixed: bulletin dialog never appeared on first connect —
  _process_ll_bulletins called self.sig_log.emit() but sig_log lives
  on the worker, not the main window; the silent AttributeError aborted
  the function before the dialog could show; fixed by using
  self.worker.sig_log.emit()
- Fixed: spurious [BULL] No new bulletins. on returning connects when
  no mail was found — bulletin check was firing regardless of whether
  check_on_connect was enabled or subscriptions were configured
- First connect via Telnet now uses LL 50 (was LL 20); VARA remains LL 20

0.10.4-beta (2026-03-28)
- Bulletin check on connect no longer sends L> CATEGORY for each
  subscription — bulletins are now extracted directly from the LL N /
  L watermark- scan already performed on connect; sig_ll_ready now
  carries the filtered bulletin list alongside the personal mail lists;
  _process_ll_bulletins() applies tombstone/exists filtering and feeds
  the existing selection dialog; L> is still used for the manual
  mid-session Refresh path where the scan data is stale

0.10.3-beta (2026-03-28)
- Fixed: message download hang/scramble when downloading via the new
  LL/L watermark path — _run_download was not pausing the terminal
  monitor thread before calling download_message, causing a race
  condition where both threads read from the same socket simultaneously;
  fixed by adding set_terminal_mode(False) / flush_input() guard around
  the download loop in _run_download, matching the pattern used by
  _run_check_bulletins and _run_mail_check
- Reverted: spurious post-read flush added in 0.10.2

0.10.2-beta (2026-03-28)
- Fixed: message download hang on messages with quoted/forwarded content
  — LinBPQ delivers trailing buffered data after [End of Message] and the
  BBS prompt in the same TCP burst; read_until returned on the prompt
  match but left that data in the buffer, poisoning the next _expect;
  fixed by flushing the buffer after each message download

0.10.1-beta (2026-03-28)
- Fixed: "No new personal mail" shown incorrectly when choosing PN+PY
  on first connect — personal mail lists were set on the worker thread
  object but read from the main window object; replaced shared attributes
  with sig_ll_ready signal for safe cross-thread delivery
- Fixed: Telnet auto-disconnect not firing after mail check — "no new
  mail" paths in _on_first_visit were not calling _prompt_outbox()
- Fixed: Bulletins not checked on returning connect with no new personal
  mail — returning-visit "no mail" branch now triggers bulletin check
  before _prompt_outbox(), mirroring _on_mail_summary logic

0.10.0-beta (2026-03-28)
- Watermark-based mail check — replaces LM with LL N on first connect
  and L watermark- on subsequent connects; only personal mail addressed
  to mycall (type P, status N) is auto-downloaded; skips PF, PY,
  TO=SYSOP, FROM=SYSTEM
- bbs_watermarks table added to database; tracks highest seen message
  number per callsign/BBS pair; migrates automatically on first run
- Bulletin filter updated to accept BN (status N) and B$ (forwarded,
  status $) — was previously limited to status N only

0.9.11-beta (2026-03-25)
- Fixed: Telnet login on non-standard LinBPQ nodes — QtC now drains all
  trailing node status lines after sending the bbs command before declaring
  login complete, and properly detects both "de N0CALL>" and plain ">"
  BBS prompt styles; resolves mail retrieval failure on multi-hop nodes
  where circuit status lines arrive after the initial prompt

0.9.10-beta (2026-03-24)
- Fixed: Windows install.ps1 version header check failing on machines where
  PowerShell reads UTF-8 files with em-dash characters as mojibake — header
  check now uses Python to read the first line, bypassing PowerShell encoding

0.9.9-beta (2026-03-24)
- Bulletin first-connect backlog management — on first visit to a BBS, all
  but the 2 newest bulletins per category are auto-tombstoned; no more giant
  download backlog on a new install
- Bulletins skipped in the selection dialog are tombstoned immediately and
  will not reappear on future connects
- My Station -> Home BBS hint updated to hierarchical address format
  (e.g. K5DAT.#NEWI.WI.USA.NOAM)

0.9.8-beta (2026-03-22)
- Fixed: Windows installer fails to detect Python when installed via the
  Python Launcher (py.exe) — install.ps1 now tries py first, then falls
  back to python; fixes Python 3.11+ installs that do not add python to PATH

0.9.7-beta (2026-03-22)
- Multi-select delete — Ctrl+click or Shift+click to select multiple messages
  or bulletins; Delete button shows count; confirm dialog names quantity and type

0.9.6-beta (2026-03-21)
- Fixed: Telnet Terminal View frozen after login
- Fixed: Telnet Mail View not downloading messages
- Fixed: Toolbar status text hard-clipped — now elides with ...
- Telnet auto-disconnect after mail check and outbox send

0.9.5-beta (2026-03-18)
- Fixed: Message download body bleeding — two-stage read waits for
  [End of Message] before BBS prompt
- Fixed: BBS List edit crash
- Fixed: Telnet flush_input crash
- Bulletin tombstone 120-day cleanup on every launch

0.9.4-beta (2026-03-17)
- Bulletin support — subscribe to categories, browse in folder panel,
  selection dialog with size estimates
- Search highlight — amber highlight on matched terms in message preview

0.9.3-beta (2026-03-17)
- Message search — real-time filter with scope dropdown
- Dark mode and font size in Settings -> App
- Fixed: VARA reconnect after BBS idle timeout

0.9.1-beta
- Fixed: Windows crash on missing or corrupt config.json
- GPL-3 headers, qtc_icon.ico for Windows

0.9.0-beta (2026-03-16)
- Fixed: VARA Error 111 on reconnect
- Mark All Read, VARA link stats, inbox column widths

------------------------------------------------------------------------

73 de KC9MTP — Bill Johnson — Valparaiso, IN
GPL-3 — https://github.com/Bill-Johnson/QtC
