# BUILD_WINDOWS.md — Building the QtC Windows exe
<!-- Copyright (C) 2025-2026 Bill Johnson, KC9MTP -->

Builds `QtC.exe` on Windows with PyInstaller, straight from a git clone of
the repo, and zips it for the GitHub Release.

**Throughout this doc, the build folder is the repo itself: `C:\Ham\QtC\`.**
Nothing is copied by hand. Every command below shows the folder you must be
in before running it. Commands are for **PowerShell**.

---

## Prerequisites (one-time setup)

Python 3.10 or newer and Git for Windows, then in PowerShell:
```
pip install pyinstaller pillow pyqt6 pyserial
```

If `C:\Ham\QtC\` does not exist yet, clone it and switch to the `windows`
branch — a fresh clone starts on `main`, which has the wrong `QtC.spec`:

**Be at:** `C:\Ham\`
```
git clone https://github.com/Bill-Johnson/QtC.git
cd C:\Ham\QtC
git checkout windows
```

---

## Step 1 — Get the latest source

**Be at:** `C:\Ham\QtC\`
```
git checkout windows
git pull origin windows
git branch --show-current
git status --short
```

`git branch --show-current` must print **windows**. `git status --short`
should print nothing — if it lists changed files, ask before going on.

Check the spec is the windows one:
```
Select-String -Path QtC.spec -SimpleMatch "Bootloader splash intentionally NOT used"
```
If that prints nothing, you are building from the wrong spec — stop and fix
the branch first. The `main` branch's spec produces a stuck double splash.

(Work not yet pushed to GitHub can be brought over as a git bundle instead
of `git pull` — Claude does that over SSH.)

---

## Step 2 — Close QtC

If QtC is running on this machine, close it. PyInstaller cannot replace
`dist\QtC\` while the exe in it is open.

---

## Step 3 — Generate the splash PNG

**Be at:** `C:\Ham\QtC\`
```
python make_splash.py
```

Writes `qtc_splash.png` with the version from `main_window.py`, so it always
matches the build.

---

## Step 4 — Run PyInstaller

**Be at:** `C:\Ham\QtC\`
```
python -m PyInstaller --clean --noconfirm QtC.spec
```

`--clean` throws away the previous build's leftovers; `--noconfirm` replaces
the old `dist\QtC\` without asking. Result:
```
C:\Ham\QtC\build\           ← intermediate files, ignore
C:\Ham\QtC\dist\QtC\        ← the folder that gets zipped
C:\Ham\QtC\dist\QtC\QtC.exe
```
Both folders are ignored by git.

---

## Step 5 — Zip it

**Be at:** `C:\Ham\QtC\`
```
Compress-Archive -Path dist\QtC -DestinationPath dist\QtC-X.Y.Z-beta-windows.zip -Force
```

Use the version being built in place of `X.Y.Z`. The zip lands next to the
`QtC` folder, in `C:\Ham\QtC\dist\`, and contains one top-level `QtC\`
folder with the exe and everything it needs. Make a zip on every build, so
the latest exe is always ready to hand over. Only the release build's zip
goes up to GitHub.

---

## Step 6 — Put the splash back

**Be at:** `C:\Ham\QtC\`
```
git checkout -- qtc_splash.png
git status --short
```

Step 3 rewrote a tracked file. This puts it back so the repo stays clean;
the exe already has its own copy. `git status --short` should print nothing.

---

## Step 7 — Test the exe

Double-click `C:\Ham\QtC\dist\QtC\QtC.exe`.

Verify:
- [ ] Splash shows once, icon in title bar and taskbar
- [ ] Settings opens (and the radio does not key); PTT tab lists the COM ports
- [ ] Telnet connect works (if a local node is available)
- [ ] VARA connect keys the radio (if VARA is running)
- [ ] Direwolf / SoundModem connect works (if a modem is running)
- [ ] Config persists in `%APPDATA%\qtc\config.json` after close and reopen
- [ ] SmartScreen popup — click "More info" → "Run anyway"

Upload `C:\Ham\QtC\dist\QtC-X.Y.Z-beta-windows.zip` to the existing GitHub
Release as its second asset. Do not create a separate release.

---

## Troubleshooting

**"Unable to find QtC.spec"**
You are not in `C:\Ham\QtC\`, or not on the `windows` branch. Redo Step 1.

**"PermissionError" / "Access is denied" removing dist\QtC**
QtC is still running. Close it (Step 2) and rerun Step 4.

**App icon or splash missing at runtime (exe runs but plain window)**
Data files landed inside `dist\QtC\_internal\` instead of next to the exe.
This is a PyInstaller 6 layout issue — tell Claude and we'll patch the spec
or the runtime `sys._MEIPASS` lookup.

**App crashes immediately on launch**
Run the exe from a console so you can see the traceback:

**Be at:** `C:\Ham\QtC\dist\QtC\`
```
.\QtC.exe
```

**"Failed to execute script" error**
Missing hidden import. Add the module to `hiddenimports` in `QtC.spec` and
rerun Step 4 (`--clean` already clears the old build).

**PyQt6 platform plugin error**
```
pip install pyinstaller --upgrade
```
Then rerun Step 4.

**Antivirus flags the exe**
Expected for unsigned executables. Add a Windows Security exclusion or
submit to Microsoft at https://www.microsoft.com/en-us/wdsi/filesubmission

---

## Branch Notes

This doc lives on **both** `main` and `windows` so a fresh clone of `main`
can find it. `QtC.spec` differs between the two branches; the `windows`
one is the only one to build with.

After every new `main` release, merge it into `windows` so the windows
branch carries the updated `.py` sources:

**Be at:** the repo on the Linux machine (`~/vara_bbs_client/QtC`)
```
git checkout windows
git merge main
git push origin windows
```

Then build from Step 1.

---

## Appendix A — Regenerating qtc_icon.ico (one-time)

The repo already contains `qtc_icon.ico`. Only do this if it's missing.

**Be at:** `C:\Ham\QtC\`

```
python -c "
from PIL import Image, ImageDraw, ImageFont
import io
def make(size):
    img = Image.new('RGBA',(size,size),(0,0,0,0))
    d = ImageDraw.Draw(img)
    m = max(1,int(size*0.03))
    d.ellipse([m,m,size-m-1,size-m-1],fill=(26,42,26,255),outline=(58,90,58,255),width=max(1,size//64))
    cx,cy=int(size*0.67),int(size*0.50)
    g,gm,gd=(0,255,136,255),(0,255,136,165),(0,255,136,89)
    for r,col,w in [(int(size*0.12),g,max(2,size//20)),(int(size*0.20),gm,max(1,size//28)),(int(size*0.29),gd,max(1,size//40))]:
        d.arc([cx-r,cy-r,cx+r,cy+r],start=-60,end=60,fill=col,width=w)
    fs=max(6,int(size*0.28))
    try: font=ImageFont.truetype('arialbd.ttf',fs)
    except:
        try: font=ImageFont.truetype('arial.ttf',fs)
        except: font=ImageFont.load_default()
    bb=d.textbbox((0,0),'QtC',font=font)
    d.text((int(size*0.10),(size-(bb[3]-bb[1]))//2-bb[1]),'QtC',fill=g,font=font)
    return img
imgs=[make(s) for s in [256,128,64,48,32,16]]
buf=io.BytesIO()
imgs[0].save(buf,format='ICO',append_images=imgs[1:])
open('qtc_icon.ico','wb').write(buf.getvalue())
print('qtc_icon.ico written OK')
"
```

---

*73 de KC9MTP — Bill Johnson — Valparaiso, IN*
