# HyperX Battery

System tray battery indicator for HyperX Cloud Flight wireless headset (Windows + Linux).

## Features

- **Battery monitoring** — reads battery level from the 2.4 GHz USB dongle via HID
- **Tray icon** — color-coded vertical bar (green/orange/red) with charging indicator
- **Low battery notifications** — Windows toast or Linux `notify-send` at 20% and 10%
- **Auto audio switching (Windows)** — switches default audio device to headset when connected, restores previous device (Focusrite) when disconnected
- **Mute indicator** — red border on tray icon when mic is muted
- **Fallback audio control** — uses [SoundVolumeCommandLine (svcl.exe)](https://www.nirsoft.net/utils/sound_volume_command_line.html) when COM-based switching fails

## Install

### Linux (source)

Prerequisites:
- Python 3.11+
- A desktop session with tray icon support
- `notify-send` available (package `libnotify-bin` on Debian/Ubuntu)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python src/hyperx.py
```

Notes (Linux):
- Audio auto-switching is currently Windows-only.
- Autostart toggle writes/removes: `~/.config/autostart/hyperx-battery.desktop`

### Windows (installer)

Download the latest `hyperx-battery-setup.exe` from [Releases](https://github.com/shirk33y/hyperx-battery/releases) and run it.

The installer:
- Installs to `%LOCALAPPDATA%\HyperX Battery`
- Bundles `svcl.exe` for audio device switching
- Creates a startup shortcut (auto-starts on login)
- Adds entry to Add/Remove Programs
- Supports silent install: `hyperx-battery-setup.exe /S`

## Build from source (Windows)

### Prerequisites

- Python 3.11+ (Windows, not WSL)
- [NSIS](https://nsis.sourceforge.io/) (for building the installer)

### Steps

```powershell
# Install dependencies
pip install -r requirements.txt

# Build the exe with PyInstaller
pyinstaller hyperx-battery.spec

# Build the installer (from repo root)
makensis installer\hyperx-battery.nsi
```

The installer will be at `installer\hyperx-battery-setup.exe`.

## Project structure

```
├── src/
│   ├── hyperx.py              # Main app (HID reader + tray icon + audio switcher)
│   └── hyperx_make_ico.py     # Icon generator (PNG → ICO)
├── assets/
│   ├── hyperx.ico             # App icon (multi-size)
│   └── hyperx.png             # Source icon image
├── installer/
│   ├── hyperx-battery.nsi     # NSIS installer script
│   └── hyperx_install.ps1     # Legacy PowerShell startup shortcut
├── tools/
│   └── svcl.exe               # NirSoft SoundVolumeCommandLine
├── hyperx-battery.spec        # PyInstaller spec
└── requirements.txt           # Python dependencies
```

## How it works

1. Connects to the HyperX Cloud Flight USB dongle via HID (vendor `0x0951`, product `0x16C4`)
2. Sends a bootstrap report to trigger battery status updates
3. Parses HID reports for battery level, power state, mute, and volume events
4. Renders a 16×16 tray icon with battery level bar
5. On headset connect: switches Windows default audio output to the headset
6. On headset disconnect/power-off: restores previous audio device

## Credits

- Battery HID protocol based on community reverse-engineering
- [svcl.exe](https://www.nirsoft.net/utils/sound_volume_command_line.html) by NirSoft (fallback audio switching)
