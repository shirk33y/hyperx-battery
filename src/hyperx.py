#!/usr/bin/env python3
r"""
HyperX Cloud Flight battery via 2.4 GHz dongle (HID), Windows.

Requires (Windows Python, not WSL):
    pip install hidapi pystray pillow

Run from PowerShell/cmd:
    python hyperx.py

Tray icon shows battery %, tooltips show status; right-click Quit exits.
Based on community reverse-engineering (dongle HID reports). Battery
mapping follows the JS example you shared.
"""

import sys
import time
import threading
import signal
import subprocess
import os
import json
import csv
import io
import logging
import logging.handlers
import warnings
from pathlib import Path
from typing import Optional, List, Dict
import comtypes
from pycaw.utils import AudioUtilities

# Silence noisy COMError warnings from pycaw device property reads
warnings.filterwarnings("ignore", message="COMError attempting to get property", category=UserWarning)

import hid
import pystray
from PIL import Image, ImageDraw, ImageFont

VENDOR_ID = 0x0951  # 2385
PRODUCT_ID = 0x16C4  # 5828 (Cloud Flight dongle)

# Usage page for "status" reports (battery etc.)
STATUS_USAGE_PAGE = 65363
STATUS_USAGE = 771

# Bootstrap report (same as JS):
BOOTSTRAP_REPORT = [
    0x21,
    0xFF,
    0x05,
    0x00,
    0x00,
    0x00,
    0x00,
    0x00,
    0x00,
    0x00,
    0x00,
    0x00,
    0x00,
    0x00,
    0x00,
    0x00,
    0x00,
    0x00,
    0x00,
    0x00,
    0x00,
]


def list_devices():
    return [d for d in hid.enumerate() if d["vendor_id"] == VENDOR_ID and d["product_id"] == PRODUCT_ID]


def pick_bootstrap_device(devices):
    for d in devices:
        if d.get("usage_page") == STATUS_USAGE_PAGE and d.get("usage") == STATUS_USAGE:
            return d
    return devices[0] if devices else None


def calc_percentage(charge_state: int, magic: int) -> Optional[int]:
    # Ported from the provided JS mapping
    if charge_state == 0x10:
        # charging; magic >= 20 => charging indicator
        if magic <= 11:
            return 100
        return None

    if charge_state == 0x0F:
        if magic >= 130:
            return 100
        if 120 <= magic < 130:
            return 95
        if 100 <= magic < 120:
            return 90
        if 70 <= magic < 100:
            return 85
        if 50 <= magic < 70:
            return 80
        if 20 <= magic < 50:
            return 75
        if 0 < magic < 20:
            return 70
        return None

    if charge_state == 0x0E:
        if 240 < magic < 250:
            return 65
        if 220 <= magic <= 240:
            return 60
        if 208 <= magic < 220:
            return 55
        if 200 <= magic < 208:
            return 50
        if 190 <= magic < 200:
            return 45
        if 180 <= magic < 190:
            return 40
        if 169 <= magic < 179:
            return 35
        if 159 <= magic < 169:
            return 30
        if 148 <= magic < 159:
            return 25
        if 119 <= magic < 148:
            return 20
        if 90 <= magic < 119:
            return 15
        if magic < 90:
            return 10
        return None

    return None


# Track last reported state to suppress duplicate log lines
_last_report = {"battery": None, "charging": None, "power": None, "muted": None}


def handle_report(data: bytes):
    ln = len(data)
    if ln == 0:
        return

    if ln == 0x02:
        if data[0] == 0x64 and data[1] == 0x03:
            if _last_report["power"] != "off":
                _last_report["power"] = "off"
                print("Power: off")
            return ("power", "off")
        if data[0] == 0x64 and data[1] == 0x01:
            if _last_report["power"] != "on":
                _last_report["power"] = "on"
                print("Power: on")
            return ("power", "on")
        if data[0] == 0x65 and data[1] == 0x04:
            if _last_report["muted"] is not True:
                _last_report["muted"] = True
                print("Muted: True")
            return ("muted", True)
        if data[0] == 0x65:
            if _last_report["muted"] is not False:
                _last_report["muted"] = False
                print("Muted: False")
            return ("muted", False)

    elif ln == 0x05:
        direction = "up" if data[1] == 0x01 else "down" if data[1] == 0x02 else None
        if direction:
            print(f"Volume: {direction}")
            return ("volume", direction)
        return None

    elif ln in (0x0F, 0x14):
        charge_state = data[3]
        magic_value = data[4] if ln > 4 else charge_state
        pct = calc_percentage(charge_state, magic_value)
        charging_flag = charge_state == 0x10
        if pct is not None:
            if _last_report["battery"] != pct or _last_report["charging"] != charging_flag:
                _last_report["battery"] = pct
                _last_report["charging"] = charging_flag
                print(f"Battery: {pct}% (charge_state=0x{charge_state:02x}, magic={magic_value}, charging={charging_flag})")
            return ("battery", (pct, charging_flag))
        return None

    else:
        print(f"Unknown report len={ln}: {list(data)}")
    return None


def bootstrap(dev):
    try:
        dev.write(BOOTSTRAP_REPORT)
    except Exception as e:
        print(f"Bootstrap write failed: {e}", file=sys.stderr)


def main():
    # -------- File logging (rotating, capped at 2 MB) --------
    log_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "HyperX Battery"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "hyperx.log"
    file_logger = logging.getLogger("hyperx")
    file_logger.setLevel(logging.DEBUG)
    _handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8",
    )
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    file_logger.addHandler(_handler)
    file_logger.info("=== hyperx-battery started ===")

    # -------- Settings persistence --------
    settings_path = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "HyperX Battery" / "settings.json"
    STARTUP_LNK_NAME = "HyperX Battery.lnk"

    def _startup_folder() -> Path:
        startup = os.environ.get("APPDATA")
        if startup:
            return Path(startup) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        return Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"

    def _has_startup_shortcut() -> bool:
        return (_startup_folder() / STARTUP_LNK_NAME).exists()

    def _set_startup(enabled: bool):
        lnk = _startup_folder() / STARTUP_LNK_NAME
        if enabled:
            try:
                # Find our exe: if frozen (PyInstaller), use sys.executable; else python + script
                if getattr(sys, 'frozen', False):
                    target = sys.executable
                    args = ""
                else:
                    target = sys.executable
                    args = f'"{Path(__file__).resolve()}"'
                # Create .lnk via PowerShell (avoids COM dependency on WScript.Shell)
                ps_cmd = (
                    f'$ws = New-Object -ComObject WScript.Shell; '
                    f'$s = $ws.CreateShortcut("{lnk}"); '
                    f'$s.TargetPath = "{target}"; '
                    f'$s.Arguments = \'{args}\'; '
                    f'$s.WorkingDirectory = "{Path(target).parent}"; '
                    f'$s.WindowStyle = 7; '
                    f'$s.Save()'
                )
                subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd],
                               capture_output=True, timeout=10,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            except Exception as e:
                print(f"[settings] Failed to create startup shortcut: {e}", file=sys.stderr)
        else:
            try:
                lnk.unlink(missing_ok=True)
            except Exception as e:
                print(f"[settings] Failed to remove startup shortcut: {e}", file=sys.stderr)

    def load_settings() -> dict:
        defaults = {"auto_switch_device": True, "autostart": _has_startup_shortcut()}
        try:
            if settings_path.exists():
                with settings_path.open("r", encoding="utf-8") as f:
                    saved = json.load(f)
                defaults.update(saved)
        except Exception:
            pass
        return defaults

    def save_settings(settings: dict):
        try:
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            with settings_path.open("w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)
        except Exception as e:
            print(f"[settings] Failed to save: {e}", file=sys.stderr)

    settings = load_settings()

    def ensure_com():
        try:
            comtypes.CoInitialize()
        except Exception:
            pass

    ensure_com()

    def log_audio(msg: str):
        try:
            file_logger.info(msg)
        except Exception:
            pass

    state = {
        "battery": None,
        "charging": False,
        "muted": None,
        "power": None,
        "device": None,
        "last_notified": None,
        "connected": False,
        "last_seen": 0,
        "previous_audio_device": None,
        "auto_switched_to_headset": False,
        "power_off_at": 0.0,
        "disconnect_at": 0.0,
    }

    # Restore persisted previous_audio_device into state (skip if it's the headset itself)
    _prev = settings.get("previous_audio_device")
    if _prev and _prev.get("id"):
        _prev_low = (_prev.get("name", "") + " " + _prev.get("id", "")).lower()
        if "hyperx" not in _prev_low and "cloud" not in _prev_low:
            state["previous_audio_device"] = _prev

    last_power_ts = {"t": 0.0, "v": None}

    def update_state(evt: str, value):
        if evt == "battery":
            if isinstance(value, tuple):
                pct, chg = value
            else:
                pct, chg = value, None
            state["battery"] = pct
            if chg is not None:
                state["charging"] = bool(chg)
            # low-battery notifications
            if pct is not None:
                maybe_notify(pct)
        elif evt == "charging":
            state["charging"] = bool(value)
        elif evt == "muted":
            state["muted"] = bool(value)
        elif evt == "power":
            now = time.time()
            if state.get("power") != value and (now - last_power_ts["t"] > 1.0 or last_power_ts["v"] != value):
                print(f"Power: {value}")
                last_power_ts["t"] = now
                last_power_ts["v"] = value
            state["power"] = value
        elif evt == "device":
            state["device"] = value
        elif evt == "connected":
            state["connected"] = bool(value)
        elif evt == "last_seen":
            state["last_seen"] = float(value)
        elif evt == "previous_audio_device":
            state["previous_audio_device"] = value
        elif evt == "auto_switched_to_headset":
            state["auto_switched_to_headset"] = bool(value)

    # -------- Audio device helpers (pycaw for enumeration, svcl.exe for switching) --------

    ERoleConsole = 0

    def _dev_id(dev) -> str:
        try:
            return dev.id  # pycaw device object usually exposes .id
        except Exception:
            try:
                return dev.GetId()
            except Exception:
                return ""

    def list_playback_devices() -> List[Dict[str, str]]:
        ensure_com()
        try:
            devices = AudioUtilities.GetAllDevices()
            items = []
            for d in devices:
                try:
                    name = getattr(d, "FriendlyName", None)
                    if not name:
                        continue
                    items.append({"id": _dev_id(d), "name": name})
                except Exception:
                    continue
            log_audio(f"list_playback_devices count={len(items)} names={[i['name'] for i in items]}")
            return items
        except Exception as e:
            log_audio(f"list_playback_devices error: {e}")
            print(f"[audio] list_playback_devices error: {e}", file=sys.stderr)
            return []

    def get_default_playback() -> Optional[Dict[str, str]]:
        ensure_com()
        try:
            dev = AudioUtilities.GetDefaultAudioEndpoint(0, ERoleConsole)
            if not dev:
                return None
            return {"id": dev.GetId(), "name": dev.FriendlyName}
        except Exception as e:
            log_audio(f"get_default_playback error: {e}")
            return None

    def _find_svcl() -> Optional[str]:
        svv_path = os.environ.get("SOUNDVOLUMEVIEW_EXE")
        if svv_path:
            return svv_path
        # When frozen (PyInstaller onefile), svcl.exe is extracted to _MEIPASS
        if getattr(sys, 'frozen', False):
            meipass = getattr(sys, '_MEIPASS', None)
            if meipass:
                cand = Path(meipass) / "svcl.exe"
                if cand.exists():
                    return str(cand)
            # Also check next to the exe itself
            cand = Path(sys.executable).with_name("svcl.exe")
            if cand.exists():
                return str(cand)
        # Dev mode: check next to the script
        cand = Path(__file__).with_name("svcl.exe")
        if cand.exists():
            return str(cand)
        # Check in tools/ relative to repo root
        cand = Path(__file__).resolve().parent.parent / "tools" / "svcl.exe"
        if cand.exists():
            return str(cand)
        return None

    def set_default_playback(device_id: str, device_name: Optional[str] = None):
        if not device_id:
            log_audio("set_default_playback skipped: empty device_id")
            return
        svv_path = _find_svcl()
        if not svv_path:
            log_audio("svcl.exe not found; cannot switch audio device")
            print("[audio] svcl.exe not found; cannot switch audio device")
            return
        try:
            res = subprocess.run([svv_path, "/SetDefault", device_id, "all"], capture_output=True, text=True,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            log_audio(f"svcl id={device_id} all rc={res.returncode} out={res.stdout.strip()} err={res.stderr.strip()}")
            print(f"[audio] svcl id={device_id} all rc={res.returncode} out={res.stdout.strip()} err={res.stderr.strip()}")
        except Exception as e:
            log_audio(f"svcl error: {e}")
            print(f"[audio] svcl error: {e}", file=sys.stderr)

    def svcl_get_default_render() -> Optional[Dict[str, str]]:
        """Use svcl.exe /scomma with /Columns to find the current default render device."""
        svcl_path = _find_svcl()
        if not svcl_path:
            log_audio("svcl_get_default_render: svcl.exe not found")
            return None
        try:
            # Request exactly the columns we need in a known order:
            # 0=Name, 1=Type, 2=Direction, 3=Default, 4=Item ID, 5=Command-Line Friendly ID
            res = subprocess.run(
                [svcl_path, "/scomma", "",
                 "/Columns", "Name,Type,Direction,Default,Item ID,Command-Line Friendly ID"],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if res.returncode != 0:
                log_audio(f"svcl /scomma failed rc={res.returncode} err={res.stderr.strip()}")
                return None
            raw = res.stdout.strip()
            if not raw:
                log_audio("svcl /scomma returned empty output")
                return None
            reader = csv.reader(io.StringIO(raw))
            for row in reader:
                if len(row) < 4:
                    continue
                name = row[0].strip()
                item_type = row[1].strip() if len(row) > 1 else ""
                direction = row[2].strip() if len(row) > 2 else ""
                default_field = row[3].strip() if len(row) > 3 else ""
                item_id = row[4].strip() if len(row) > 4 else ""
                cli_id = row[5].strip() if len(row) > 5 else ""
                # Default field contains "Render" when it's the default render device
                if item_type == "Device" and direction == "Render" and "Render" in default_field:
                    device_id = cli_id or item_id
                    log_audio(f"svcl default render: name={name} id={device_id}")
                    return {"id": device_id, "name": name}
            log_audio("svcl: no default render device found in output")
        except Exception as e:
            log_audio(f"svcl_get_default_render error: {e}")
        return None

    def find_headset() -> Optional[Dict[str, str]]:
        # Prefer exact known name, then substring, else first render device
        target_name = "speakers (hyperx cloud flight wireless headset)"
        devices = list_playback_devices()
        if not devices:
            print("[audio] no playback devices enumerated")
            log_audio("no playback devices enumerated")
            return None
        # collect all hyperx render devices
        hyperx_candidates: List[Dict[str, str]] = []
        for item in devices:
            name = (item.get("name") or "").lower()
            if name == target_name:
                return item
        for item in devices:
            name = (item.get("name") or "").lower()
            if "hyperx" in name or "cloud" in name:
                hyperx_candidates.append(item)
                return item
        if hyperx_candidates:
            return hyperx_candidates[0]
        return devices[0] if devices else None

    POWER_OFF_COOLDOWN = 30  # seconds to ignore reconnect after power-off
    DISCONNECT_DEBOUNCE = 5   # seconds: brief HID dropouts don't trigger audio restore

    def audio_switch_to_headset():
        if not settings.get("auto_switch_device", True):
            return
        if state.get("auto_switched_to_headset"):
            return
        # Suppress ghost reconnections shortly after power-off
        power_off_at = state.get("power_off_at", 0.0)
        if power_off_at and (time.time() - power_off_at) < POWER_OFF_COOLDOWN:
            log_audio(f"ignoring reconnect {time.time() - power_off_at:.1f}s after power-off (cooldown {POWER_OFF_COOLDOWN}s)")
            return
        target = find_headset()
        if not target:
            log_audio("headset not found among playback devices")
            print("[audio] headset not found among playback devices")
            return
        # Store current default device before switching (use svcl for consistent ID format)
        current = svcl_get_default_render()
        if not current:
            current = get_default_playback()
        if current and current.get("id") != target.get("id") and not _is_hyperx(current.get("name", ""), current.get("id", "")):
            update_state("previous_audio_device", current)
            settings["previous_audio_device"] = current
            save_settings(settings)
            log_audio(f"stored previous device: {current}")
            print(f"[audio] stored previous device: {current}")
        log_audio(f"switching to headset: {target}")
        print(f"[audio] switching to headset: {target}")
        set_default_playback(target["id"], target.get("name"))
        update_state("auto_switched_to_headset", True)
        prev_name = (current.get("name") if current else None) or "unknown"
        _notify("HyperX Battery", f"Switched audio to {target.get('name', 'headset')}\nPrevious: {prev_name}")

    def audio_restore_previous(force: bool = False):
        if not settings.get("auto_switch_device", True):
            return
        if not state.get("auto_switched_to_headset"):
            return
        # Debounce: skip unless forced (explicit power-off) or sustained disconnect
        if not force:
            disc_at = state.get("disconnect_at", 0.0)
            if not disc_at:
                return
            elapsed = time.time() - disc_at
            if elapsed < DISCONNECT_DEBOUNCE:
                log_audio(f"restore debounced: only {elapsed:.1f}s since disconnect (need {DISCONNECT_DEBOUNCE}s)")
                return
        prev = state.get("previous_audio_device")
        if not prev or not prev.get("id"):
            log_audio("no previous device stored; skipping restore")
            print("[audio] no previous device stored; skipping restore")
            update_state("auto_switched_to_headset", False)
            return
        log_audio(f"restoring to: {prev}")
        print(f"[audio] restoring to: {prev}")
        set_default_playback(prev["id"], prev.get("name"))
        update_state("auto_switched_to_headset", False)
        state["disconnect_at"] = 0.0
        _notify("HyperX Battery", f"Restored audio to {prev.get('name', 'previous device')}")

    def _notify(title: str, msg: str, duration: int = 3):
        try:
            icon = icon_ref.get("icon")
            if icon:
                icon.notify(msg, title)
        except Exception as e:
            log_audio(f"notify exception: {e}")

    stop_flag = {"stop": False}

    def _is_hyperx(name: str, device_id: str = "") -> bool:
        low = (name or "").lower()
        low_id = (device_id or "").lower()
        return "hyperx" in low or "cloud" in low or "hyperx" in low_id or "cloud" in low_id

    def poll_default_device():
        while not stop_flag["stop"]:
            try:
                current = svcl_get_default_render()
                if current and current.get("id") and not _is_hyperx(current.get("name", ""), current.get("id", "")):
                    prev = state.get("previous_audio_device")
                    if not prev or prev.get("id") != current.get("id"):
                        update_state("previous_audio_device", current)
                        # Persist to settings so it survives relaunch
                        settings["previous_audio_device"] = current
                        save_settings(settings)
                        log_audio(f"poll: stored previous device: {current}")
            except Exception as e:
                log_audio(f"poll_default_device error: {e}")
            time.sleep(10)

    def hid_loop():
        while not stop_flag["stop"]:
            devices = list_devices()
            if not devices:
                if state.get("connected"):
                    state["disconnect_at"] = state.get("disconnect_at") or time.time()
                    log_audio("HID device gone — dongle disconnected")
                update_state("connected", False)
                update_state("device", "")
                update_state("battery", (None, None))
                update_state("last_seen", 0)
                audio_restore_previous()
                time.sleep(1)
                continue

            # Tentative device name, but keep connected False until data arrives
            dev_name = devices[0].get("product_string") or "HyperX Cloud Flight"
            update_state("device", dev_name)
            update_state("connected", False)

            bootstrap_info = pick_bootstrap_device(devices)
            if bootstrap_info:
                bdev = hid.device()
                try:
                    bdev.open_path(bootstrap_info["path"])
                    bootstrap(bdev)
                except Exception as e:
                    print(f"Bootstrap failed: {e}", file=sys.stderr)
                finally:
                    try:
                        bdev.close()
                    except Exception:
                        pass

            handles: List[hid.device] = []
            try:
                for info in devices:
                    dev = hid.device()
                    dev.open_path(info["path"])
                    dev.set_nonblocking(False)
                    handles.append(dev)

                # do not set last_seen until data arrives

                while not stop_flag["stop"]:
                    any_data = False
                    for dev in handles:
                        data = dev.read(64, 200)
                        if data:
                            any_data = True
                            was_connected = state.get("connected", False)
                            evt = handle_report(bytes(data))
                            if isinstance(evt, tuple):
                                update_state(evt[0], evt[1])
                                if evt[0] == "power" and evt[1] == "off":
                                    state["power_off_at"] = time.time()
                                    state["disconnect_at"] = time.time()
                                    log_audio("explicit power-off received")
                                    update_state("connected", False)
                                    update_state("last_seen", 0)
                                    audio_restore_previous(force=True)
                                    any_data = True
                                    break
                                if evt[0] == "power" and evt[1] == "on":
                                    state["power_off_at"] = 0.0
                                    state["disconnect_at"] = 0.0
                                    log_audio("explicit power-on received")
                                update_state("connected", True)
                                update_state("last_seen", time.time())
                                if not was_connected and state.get("connected"):
                                    state["disconnect_at"] = 0.0
                                    log_audio("headset reconnected")
                                    audio_switch_to_headset()
                    if not any_data:
                        time.sleep(0.05)
                        # Periodically re-check if HID device is still present
                        # (don't disconnect just because no data — headset only reports on state changes)
                        if not list_devices():
                            if state.get("connected"):
                                state["disconnect_at"] = state.get("disconnect_at") or time.time()
                                log_audio("HID device gone during read loop")
                            update_state("connected", False)
                            audio_restore_previous()
                            break
                    else:
                        time.sleep(0.01)
            finally:
                for dev in handles:
                    try:
                        dev.close()
                    except Exception:
                        pass

    def make_icon(battery: Optional[int], charging: bool, muted: bool, connected: bool) -> Image.Image:
        """Render a 16x16 vertical bar icon with transparent background and mute border."""
        icon_size = (16, 16)
        img = Image.new("RGBA", icon_size, (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        # Determine fill color by ranges; when battery unknown, fill mid-gray full width with rounded edges
        pad = 1
        bar_left, bar_right = 1, 14  # almost full width
        bar_top, bar_bottom = 1, 14  # almost full height
        usable_height = bar_bottom - bar_top

        if battery is not None:
            if battery <= 33:
                fill_color = (220, 53, 69)      # red
            elif battery <= 66:
                fill_color = (255, 159, 64)     # orange
            else:
                fill_color = (76, 175, 80)      # green

            level_px = int((battery / 100.0) * usable_height)
            level_px = max(0, min(usable_height, level_px))
            y_top = bar_bottom - level_px
            d.rounded_rectangle((bar_left, y_top, bar_right, bar_bottom), radius=2, fill=fill_color)
        else:
            # Unknown battery: fill with neutral gray to full bar height
            fill_color = (160, 160, 160)
            d.rounded_rectangle((bar_left, bar_top, bar_right, bar_bottom), radius=2, fill=fill_color)

        # Charging bolt
        if charging:
            d.text((1, 1), "⚡", fill=(0, 0, 0), font=ImageFont.load_default())

        line_color = (255, 255, 255)
        connected_border = (190, 190, 190)
        # Mute border takes precedence; else disconnected (white); else connected (light gray)
        if muted:
            d.rounded_rectangle((0, 0, 15, 15), radius=3, outline=(220, 53, 69), width=2)
        elif not connected:
            d.rounded_rectangle((0, 0, 15, 15), radius=3, outline=line_color, width=1)
        else:
            d.rounded_rectangle((0, 0, 15, 15), radius=3, outline=connected_border, width=1)

        # Disconnected overlay: white diagonal lines (1px, inset for rounded corners)
        if not connected:
            d.line((2, 13, 13, 2), fill=line_color, width=1)
            d.line((2, 2, 13, 13), fill=line_color, width=1)

        return img

    def tooltip_text():
        parts = []
        b = state.get("battery")
        charging = state.get("charging")
        connected = state.get("connected")
        if charging and b is not None:
            parts.append(f"Charging: {b}%")
        elif b is not None:
            parts.append(f"Battery: {b}%")
        elif charging:
            parts.append("Charging")
        if state.get("muted") is True:
            parts.append("Muted")
        if state.get("power"):
            parts.append(f"Power: {state['power']}")
        if state.get("device"):
            parts.append(f"Device: {state['device']}")
        if connected is False:
            parts.append("Disconnected")
        return " | ".join(parts) or "HyperX Cloud Flight"

    def maybe_notify(level: int):
        # Notify at 20% and 10% on drop, once per threshold
        last = state.get("last_notified")
        thresholds = [20, 10]
        for th in thresholds:
            if level <= th and (last is None or level <= th < last):
                _notify("HyperX Battery", f"Battery low: {level}%", duration=5)
                state["last_notified"] = level
                break

    icon_ref = {"icon": None}

    def refresh_icon(icon):
        icon.icon = make_icon(
            state.get("battery"),
            state.get("charging", False),
            state.get("muted", False),
            state.get("connected", False),
        )
        icon.title = tooltip_text()

    def on_quit(icon, _item):
        stop_flag["stop"] = True
        icon.stop()
        return 0

    def on_toggle_auto_switch(icon, item):
        settings["auto_switch_device"] = not settings.get("auto_switch_device", True)
        save_settings(settings)
        return 0

    def on_toggle_autostart(icon, item):
        new_val = not settings.get("autostart", False)
        settings["autostart"] = new_val
        _set_startup(new_val)
        save_settings(settings)
        return 0

    def on_open_logs(icon, _item):
        os.startfile(str(log_path))
        return 0

    def on_open_settings(icon, _item):
        os.startfile(str(settings_path))
        return 0

    def _prev_device_label(_item=None) -> str:
        prev = state.get("previous_audio_device")
        if prev and prev.get("name"):
            return f"  Prev: {prev['name']}"
        return "  Prev: (none)"

    def tray_loop():
        # Use _noop that returns 0 to avoid WNDPROC/LRESULT and WPARAM warnings
        def _noop(*_args, **_kwargs):
            return 0

        menu = pystray.Menu(
            pystray.MenuItem("HyperX Battery Indicator", _noop, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Auto switch device",
                on_toggle_auto_switch,
                checked=lambda item: settings.get("auto_switch_device", True),
            ),
            pystray.MenuItem(
                _prev_device_label,
                _noop,
                enabled=False,
            ),
            pystray.MenuItem(
                "Autostart",
                on_toggle_autostart,
                checked=lambda item: settings.get("autostart", False),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open logs", on_open_logs),
            pystray.MenuItem("Open settings", on_open_settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", on_quit),
        )
        image = make_icon(None, False, False, False)
        icon = pystray.Icon("hyperx_battery", image, "HyperX Battery", menu)
        icon_ref["icon"] = icon
        # periodic refresh based on state
        def updater():
            while not stop_flag["stop"]:
                refresh_icon(icon)
                time.sleep(1)
        threading.Thread(target=updater, daemon=True).start()
        tray_thread = threading.Thread(target=icon.run, daemon=True)
        tray_thread.start()
        # Block here to allow KeyboardInterrupt to be caught in main
        try:
            while not stop_flag["stop"]:
                time.sleep(0.2)
        finally:
            if icon_ref.get("icon"):
                try:
                    icon_ref["icon"].stop()
                except Exception:
                    pass

    # start HID reader thread
    t = threading.Thread(target=hid_loop, daemon=True)
    t.start()

    # start default device poller thread
    threading.Thread(target=poll_default_device, daemon=True).start()

    def handle_signal(_sig, _frame):
        stop_flag["stop"] = True
        if icon_ref.get("icon"):
            try:
                icon_ref["icon"].stop()
            except Exception:
                pass

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except Exception:
            pass

    try:
        tray_loop()
    except KeyboardInterrupt:
        stop_flag["stop"] = True
        if icon_ref.get("icon"):
            icon_ref["icon"].stop()
    finally:
        stop_flag["stop"] = True
        t.join(timeout=1)


if __name__ == "__main__":
    main()