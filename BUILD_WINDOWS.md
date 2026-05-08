# BUILD_WINDOWS.md — Building the QtC Windows exe
<!-- Copyright (C) 2025-2026 Bill Johnson, KC9MTP -->

This document covers building `QtC.exe` on your Windows machine using PyInstaller.
Run these steps whenever you cut a new Windows release.

---

## Prerequisites (one-time setup)

1. **Python 3.10+** with PyQt6 and pyserial already installed (you have this).

2. **Install PyInstaller:**
   ```
   pip install pyinstaller
   ```

3. **Pillow** (for icon generation — already used by install.ps1):
   ```
   pip install pillow
   ```

---

## Build Steps

### 1. Assemble your build folder

Create a clean working folder, e.g. `C:\build\QtC\`, and copy in:

```
main_window.py
bbs_session.py
transport.py
database.py
ptt.py
make_splash.py
QtC.spec
qtc_icon.svg
qtc_icon.ico        ← generate this first (see below)
qtc_splash.png      ← generate this second (see below)
```

> **Generating qtc_icon.ico** — run this once from your build folder:
> ```
> python -c "
> from PIL import Image, ImageDraw, ImageFont
> import io
> def make(size):
>     img = Image.new('RGBA',(size,size),(0,0,0,0))
>     d = ImageDraw.Draw(img)
>     m = max(1,int(size*0.03))
>     d.ellipse([m,m,size-m-1,size-m-1],fill=(26,42,26,255),outline=(58,90,58,255),width=max(1,size//64))
>     cx,cy=int(size*0.67),int(size*0.50)
>     g,gm,gd=(0,255,136,255),(0,255,136,165),(0,255,136,89)
>     for r,col,w in [(int(size*0.12),g,max(2,size//20)),(int(size*0.20),gm,max(1,size//28)),(int(size*0.29),gd,max(1,size//40))]:
>         d.arc([cx-r,cy-r,cx+r,cy+r],start=-60,end=60,fill=col,width=w)
>     fs=max(6,int(size*0.28))
>     try: font=ImageFont.truetype('arialbd.ttf',fs)
>     except:
>         try: font=ImageFont.truetype('arial.ttf',fs)
>         except: font=ImageFont.load_default()
>     bb=d.textbbox((0,0),'QtC',font=font)
>     d.text((int(size*0.10),(size-(bb[3]-bb[1]))//2-bb[1]),'QtC',fill=g,font=font)
>     return img
> imgs=[make(s) for s in [256,128,64,48,32,16]]
> import io; buf=io.BytesIO()
> imgs[0].save(buf,format='ICO',append_images=imgs[1:])
> open('qtc_icon.ico','wb').write(buf.getvalue())
> print('qtc_icon.ico written OK')
> "
> ```

> **Generating qtc_splash.png** — run from your build folder so the splash
> picks up the current `APP_VERSION` from `main_window.py`:
> ```
> python make_splash.py
> ```
> Output: `qtc_splash.png` (600×400). Used by both PyInstaller's bootloader
> splash (configured in `QtC.spec`) and the in-Python `QSplashScreen` shown
> while `MainWindow` constructs.

### 2. Run PyInstaller

From your build folder:

```
pyinstaller QtC.spec
```

PyInstaller creates:
```
build\     ← intermediate files, ignore
dist\
  QtC\     ← this is your distributable folder
    QtC.exe
    ... (supporting DLLs and data)
```

### 3. Test the exe

**Before zipping**, run `dist\QtC\QtC.exe` directly and verify:

- [ ] App launches (no console window)
- [ ] Icon appears in title bar and taskbar
- [ ] Settings dialog opens — enter your callsign
- [ ] BBS list works — add/edit/remove an entry
- [ ] PTT tab shows serial ports (COM ports listed)
- [ ] Telnet connect works (if you have a local node)
- [ ] VARA connect works (if VARA HF is running)
- [ ] Config persists after closing and reopening (`%APPDATA%\qtc\config.json`)
- [ ] SmartScreen popup appears — click "More info" → "Run anyway" to confirm behavior

### 4. Package the release

```
cd dist
powershell Compress-Archive -Path QtC -DestinationPath QtC-0.10.9-beta-windows.zip
```

This produces `dist\QtC-0.10.9-beta-windows.zip`.

### 5. Upload to GitHub Releases

Upload `QtC-0.10.9-beta-windows.zip` to the same GitHub Release as the Linux tarball at:
`https://github.com/Bill-Johnson/QtC/releases`

---

## Troubleshooting

**App crashes immediately on launch**
Run from a command prompt to see the traceback:
```
dist\QtC\QtC.exe
```
(without `--noconsole` it will print to the terminal even though it's a windowed app)

**"Failed to execute script" error**
Usually a missing hidden import. Check the traceback, add the module to
`hiddenimports` in `QtC.spec`, and rebuild.

**PyQt6 platform plugin error: "Could not find or load the Qt platform plugin windows"**
This is rare with PyInstaller 6+. If it happens:
```
pip install pyinstaller --upgrade
```
Then rebuild.

**Serial ports not listed in PTT settings**
Add to `hiddenimports` in QtC.spec if missing:
```
'serial.tools.list_ports_windows',
```
Already included by default.

**Antivirus flags the exe**
Expected for unsigned executables. Add an exclusion in Windows Security or
submit the file for analysis to Microsoft at:
https://www.microsoft.com/en-us/wdsi/filesubmission

---

## Branch Notes

This file lives on the `windows` branch only.
`QtC.spec` lives on the `windows` branch only.
All `.py` source files are identical to `main`.
To update after a new `main` release:
```
git checkout windows
git merge main
```
Then rebuild.

---

*73 de KC9MTP — Bill Johnson — Valparaiso, IN*
