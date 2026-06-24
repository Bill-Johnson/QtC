# BUILD_WINDOWS.md — Building the QtC Windows exe
<!-- Copyright (C) 2025-2026 Bill Johnson, KC9MTP -->

Builds `QtC.exe` on Windows using PyInstaller.

**Throughout this doc, "build folder" means exactly `C:\build\QtC\`** — never
`C:\build\`. Every command below shows the cwd you must be in before running it.

---

## Prerequisites (one-time setup)

Open PowerShell and run:
```
pip install pyinstaller pillow
```
(PyQt6 and pyserial should already be installed.)

---

## Step 1 — Populate the build folder

**Be at:** `C:\` (or anywhere — this step creates the folder)

```
mkdir C:\build\QtC
cd C:\build\QtC
```

**Copy these files into `C:\build\QtC\`:**

| File | Source |
|---|---|
| `main_window.py`  | from repo |
| `bbs_session.py`  | from repo |
| `transport.py`    | from repo |
| `database.py`     | from repo |
| `ptt.py`          | from repo |
| `make_splash.py`  | from repo |
| `QtC.spec`        | from repo (windows branch) |
| `qtc_icon.svg`    | from repo |
| `qtc_icon.ico`    | from repo (or generate — see Appendix A) |

That's **6 .py files + spec + 2 icons = 9 files**. Do NOT skip `make_splash.py`.

---

## Step 2 — Generate the splash PNG

**Be at:** `C:\build\QtC\`

```
python make_splash.py
```

**Result:** `C:\build\QtC\qtc_splash.png` is created.
This pulls the current `APP_VERSION` out of `main_window.py`, so re-run it
every time you change versions.

After this step, your build folder contains **10 files**.

---

## Step 3 — Run PyInstaller

**Be at:** `C:\build\QtC\`

```
pyinstaller QtC.spec
```

**Result:** PyInstaller writes two new subfolders:
```
C:\build\QtC\build\          ← intermediate junk, ignore
C:\build\QtC\dist\QtC\       ← the distributable folder
C:\build\QtC\dist\QtC\QtC.exe
```

If PyInstaller errors with "Unable to find QtC.spec", you are NOT in
`C:\build\QtC\`. Run `cd C:\build\QtC` and retry.

---

## Step 4 — Test the exe

**Be at:** `C:\build\QtC\`

```
dist\QtC\QtC.exe
```

Verify:
- [ ] App launches (no console window)
- [ ] Icon appears in title bar and taskbar
- [ ] Splash shows briefly while loading
- [ ] Settings dialog opens — enter your callsign
- [ ] BBS list works — add/edit/remove an entry
- [ ] PTT tab shows COM ports
- [ ] Telnet connect works (if local node available)
- [ ] VARA connect works (if VARA HF running)
- [ ] Config persists at `%APPDATA%\qtc\config.json`
- [ ] SmartScreen popup — click "More info" → "Run anyway"

---

## Step 5 — Zip the distributable

**Be at:** `C:\build\QtC\dist\`

```
cd C:\build\QtC\dist
powershell Compress-Archive -Path QtC -DestinationPath QtC-0.11.0-beta-windows.zip
```

**Result:** `C:\build\QtC\dist\QtC-0.11.0-beta-windows.zip`

> The `-Path QtC` argument refers to the **folder** `C:\build\QtC\dist\QtC\`,
> not the exe. The zip will contain a top-level `QtC\` folder with the exe
> and all bundled files inside.

---

## Troubleshooting

**"Unable to find QtC.spec"**
You are not in `C:\build\QtC\`. Run `cd C:\build\QtC` and retry Step 3.

**"Cannot find qtc_splash.png" or "qtc_icon.svg"**
You skipped Step 2 or didn't copy the icon in Step 1. Confirm with
`dir C:\build\QtC\` — you should see all 10 files before running pyinstaller.

**App icon or splash missing at runtime (exe runs but plain window)**
Data files landed inside `dist\QtC\_internal\` instead of next to the exe.
This is a PyInstaller 6 layout issue — tell Claude and we'll patch the spec
or the runtime `sys._MEIPASS` lookup.

**App crashes immediately on launch**
Open a console and run the exe so you can see the traceback:
```
cd C:\build\QtC\dist\QtC
QtC.exe
```

**"Failed to execute script" error**
Missing hidden import. Add the module to `hiddenimports` in `QtC.spec`,
delete `C:\build\QtC\dist\` and `C:\build\QtC\build\`, rebuild.

**PyQt6 platform plugin error**
```
pip install pyinstaller --upgrade
```
Then rebuild.

**Antivirus flags the exe**
Expected for unsigned executables. Add a Windows Security exclusion or
submit to Microsoft at https://www.microsoft.com/en-us/wdsi/filesubmission

---

## Starting over with a clean build

If folders get out of whack, nuke and restart:

**Be at:** `C:\`
```
rmdir /s /q C:\build\QtC
mkdir C:\build\QtC
cd C:\build\QtC
```
Then re-copy the 9 source files from the USB stick / repo and go back to Step 2.

---

## Branch Notes

This doc lives on **both** `main` and `windows` so a fresh clone of `main`
can find it. `QtC.spec` lives on the `windows` branch only — that's where
PyInstaller actually runs.

To build a new release on the HP Win11 box, always:

```
git checkout windows
git pull origin windows
```

A fresh clone of the repo defaults to `main`, which doesn't contain
`QtC.spec`. Building from `main` will fail to find the spec, or worse —
if `QtC.spec` happens to be present from an earlier checkout, you may
end up building with the wrong (pre-0.13.2) spec that produced the
double-splash bug.

After every new `main` release, merge it into `windows` so the windows
branch carries the updated `.py` sources:

```
git checkout windows
git merge main
git push origin windows
```

Then rebuild from Step 2 (regenerate splash for the new version).

---

## Appendix A — Regenerating qtc_icon.ico (one-time)

The repo already contains `qtc_icon.ico`. Only do this if it's missing.

**Be at:** `C:\build\QtC\`

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
