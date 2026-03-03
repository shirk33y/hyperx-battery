#!/usr/bin/env python3
r"""
HyperX Cloud Flight battery via 2.4 GHz dongle (HID).

Works on Windows and Linux (desktop session).

Run:
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
import warnings
from pathlib import Path
from typing import Optional, List, Dict

IS_WINDOWS = sys.platform.startswith("win")

# On Linux the pip-installed hidapi bundles libusb which can't open hidraw devices
# without usbfs access. Force the system hidraw backend via LD_PRELOAD.
if not IS_WINDOWS and "HYPERX_HIDRAW_PRELOADED" not in os.environ:
    _hidraw_lib = "/usr/lib64/libhidapi-hidraw.so.0"
    if not os.path.exists(_hidraw_lib):
        _hidraw_lib = "/usr/lib/libhidapi-hidraw.so.0"
    if os.path.exists(_hidraw_lib):
        _preload = os.environ.get("LD_PRELOAD", "")
        if _hidraw_lib not in _preload:
            env = os.environ.copy()
            env["LD_PRELOAD"] = (_hidraw_lib + ":" + _preload).strip(":")
            env["HYPERX_HIDRAW_PRELOADED"] = "1"
            os.execve(sys.executable, [sys.executable] + sys.argv, env)

if IS_WINDOWS:
    import comtypes
    from ctypes import HRESULT, c_int, c_wchar_p
    from comtypes import CLSCTX_ALL
    from comtypes import GUID
    from comtypes import COMMETHOD
    from pycaw.utils import AudioUtilities

# Silence noisy COMError warnings from pycaw device property reads
if IS_WINDOWS:
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


def handle_report(data: bytes):
    ln = len(data)
    if ln == 0:
        return

    if ln == 0x02:
        if data[0] == 0x64 and data[1] == 0x03:
            print("Power: off")
            return ("power", "off")
        if data[0] == 0x64 and data[1] == 0x01:
            print("Power: on")
            return ("power", "on")
        if data[0] == 0x65 and data[1] == 0x04:
            print("Muted: True")
            return ("muted", True)
        if data[0] == 0x65:
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
    log_path = Path(__file__).with_name("hyperx_audio_debug.log")

    # -------- Settings persistence --------
    if IS_WINDOWS:
        settings_path = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "HyperX Battery" / "settings.json"
    else:
        settings_path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "hyperx-battery" / "settings.json"
    STARTUP_LNK_NAME = "HyperX Battery.lnk"

    def _startup_folder() -> Path:
        if not IS_WINDOWS:
            return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart"
        startup = os.environ.get("APPDATA")
        if startup:
            return Path(startup) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        return Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"

    def _has_startup_shortcut() -> bool:
        if IS_WINDOWS:
            return (_startup_folder() / STARTUP_LNK_NAME).exists()
        return (_startup_folder() / "hyperx-battery.desktop").exists()

    def _set_startup(enabled: bool):
        if not IS_WINDOWS:
            desktop_file = _startup_folder() / "hyperx-battery.desktop"
            if enabled:
                try:
                    desktop_file.parent.mkdir(parents=True, exist_ok=True)
                    exec_target = f'"{sys.executable}" "{Path(__file__).resolve()}"'
                    desktop_file.write_text(
                        "\n".join([
                            "[Desktop Entry]",
                            "Type=Application",
                            "Name=HyperX Battery",
                            f"Exec={exec_target}",
                            "X-GNOME-Autostart-enabled=true",
                        ]) + "\n",
                        encoding="utf-8",
                    )
                except Exception as e:
                    print(f"[settings] Failed to create autostart entry: {e}", file=sys.stderr)
            else:
                try:
                    desktop_file.unlink(missing_ok=True)
                except Exception as e:
                    print(f"[settings] Failed to remove autostart entry: {e}", file=sys.stderr)
            return

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
                               capture_output=True, timeout=10)
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
        if not IS_WINDOWS:
            return
        try:
            comtypes.CoInitialize()
        except Exception:
            pass

    ensure_com()

    def log_audio(msg: str):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
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
    }
    hid_error_log_ts = {"t": 0.0}

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

    # -------- COM-based default audio switching (pycaw, Windows only) --------

    # IPolicyConfigVista (Vista+) minimal interface
    if IS_WINDOWS:
        class IPolicyConfig(comtypes.IUnknown):
            _iid_ = GUID("{568b9108-44bf-40b4-9006-86afe5b5a620}")
            _methods_ = [
                COMMETHOD([], HRESULT, "SetDefaultEndpoint", (['in'], c_wchar_p, 'deviceId'), (['in'], c_int, 'role')),
            ]

        POLICY_CONFIG_CLSID = GUID("{294935CE-F637-4E7C-A41B-AB255460B862}")
        ERoleConsole = 0
        ERoleMultimedia = 1
        ERoleCommunications = 2

    def _dev_id(dev) -> str:
        try:
            return dev.id  # pycaw device object usually exposes .id
        except Exception:
            try:
                return dev.GetId()
            except Exception:
                return ""

    def _find_pactl() -> Optional[str]:
        """Return path to pactl: local install, or host binary (toolbox/container)."""
        import shutil
        p = shutil.which("pactl")
        if p:
            return p
        host = "/run/host/usr/bin/pactl"
        if os.path.exists(host):
            return host
        return None

    def list_playback_devices() -> List[Dict[str, str]]:
        if not IS_WINDOWS:
            pactl = _find_pactl()
            if pactl:
                try:
                    res = subprocess.run([pactl, "list", "short", "sinks"], capture_output=True, text=True)
                    if res.returncode == 0:
                        items: List[Dict[str, str]] = []
                        for line in res.stdout.splitlines():
                            parts = line.split("\t")
                            if len(parts) < 2:
                                continue
                            sink_id = parts[0].strip()
                            sink_name = parts[1].strip()
                            if sink_name:
                                items.append({"id": sink_name, "name": sink_name, "index": sink_id})
                        if items:
                            return items
                except Exception:
                    pass
            # wpctl fallback (PipeWire native, available in toolbox)
            try:
                import shutil
                wpctl = shutil.which("wpctl")
                if wpctl:
                    res = subprocess.run([wpctl, "status"], capture_output=True, text=True)
                    if res.returncode == 0:
                        items = []
                        in_sinks = False
                        for line in res.stdout.splitlines():
                            if "Sinks:" in line:
                                in_sinks = True
                                continue
                            if in_sinks:
                                # stop at next section header
                                if line.strip() and not line.strip().startswith("│") and not line.strip().startswith("*"):
                                    break
                                # match lines like: │      46. Name [vol: ...]
                                #               or: │  *   64. Name [vol: ...]
                                stripped = line.replace("│", "").replace("*", "").strip()
                                import re as _re
                                m = _re.match(r'^(\d+)\.\s+(.+?)\s*(\[.*\])?$', stripped)
                                if m:
                                    wid = m.group(1).strip()
                                    name = m.group(2).strip()
                                    items.append({"id": wid, "name": name, "wpctl_id": wid})
                        if items:
                            return items
            except Exception:
                pass
            return []
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
        if not IS_WINDOWS:
            pactl = _find_pactl()
            if pactl:
                try:
                    res = subprocess.run([pactl, "get-default-sink"], capture_output=True, text=True)
                    if res.returncode == 0:
                        sink = res.stdout.strip()
                        if sink:
                            return {"id": sink, "name": sink}
                except Exception:
                    pass
            # wpctl fallback: find the sink marked with * in wpctl status
            try:
                import shutil, re as _re
                wpctl = shutil.which("wpctl")
                if wpctl:
                    res = subprocess.run([wpctl, "status"], capture_output=True, text=True)
                    if res.returncode == 0:
                        in_sinks = False
                        for line in res.stdout.splitlines():
                            if "Sinks:" in line:
                                in_sinks = True
                                continue
                            if in_sinks:
                                if line.strip() and not line.strip().startswith("│") and not line.strip().startswith("*"):
                                    break
                                if "*" in line:
                                    stripped = line.replace("│", "").replace("*", "").strip()
                                    m = _re.match(r'^(\d+)\.\s+(.+?)\s*(\[.*\])?$', stripped)
                                    if m:
                                        return {"id": m.group(1).strip(), "name": m.group(2).strip()}
            except Exception:
                pass
            return None
        ensure_com()
        try:
            dev = AudioUtilities.GetDefaultAudioEndpoint(0, ERoleConsole)
            if not dev:
                return None
            return {"id": dev.GetId(), "name": dev.FriendlyName}
        except Exception as e:
            log_audio(f"get_default_playback error: {e}")
            return None

    PREFERRED_SVCL_ID = "{0.0.0.00000000}.{3d3538fa-ebc7-4288-8a22-69971084d2d9}"
    PREFERRED_RESTORE_ID = "{0.0.0.00000000}.{96742d3a-654c-4a34-af9d-adea184110f7}"  # Focusrite Speakers

    def set_default_playback(device_id: str, device_name: Optional[str] = None, use_preferred: bool = True):
        if not IS_WINDOWS:
            if not device_id:
                return
            import shutil
            pactl = _find_pactl()
            wpctl = shutil.which("wpctl")
            # Try pactl first (PulseAudio/PipeWire), then wpctl for native PipeWire setups
            for cmd in (
                ([pactl, "set-default-sink", device_id] if pactl else None),
                ([wpctl, "set-default", device_id] if wpctl else None),
            ):
                if not cmd:
                    continue
                try:
                    res = subprocess.run(cmd, capture_output=True, text=True)
                    if res.returncode == 0:
                        return
                except Exception:
                    pass
            return
        ensure_com()
        if not device_id:
            log_audio("set_default_playback skipped: empty device_id")
            return
        try:
            pc = comtypes.CoCreateInstance(POLICY_CONFIG_CLSID, IPolicyConfig, clsctx=CLSCTX_ALL)
            ok = True
            for role in (ERoleConsole, ERoleMultimedia, ERoleCommunications):
                hr = pc.SetDefaultEndpoint(device_id, role)
                if hr != 0:
                    ok = False
                    log_audio(f"SetDefaultEndpoint hr={hr} role={role}")
                    print(f"[audio] SetDefaultEndpoint hr={hr} role={role}")
                else:
                    log_audio(f"SetDefaultEndpoint ok role={role} id={device_id}")
                    print(f"[audio] SetDefaultEndpoint ok role={role} id={device_id}")
            if ok:
                return
        except Exception as e:
            log_audio(f"set_default_playback error: {e}")
            print(f"[audio] set_default_playback error: {e}", file=sys.stderr)

        # Fallback: SoundVolumeView CLI (if available)
        svv_path = os.environ.get("SOUNDVOLUMEVIEW_EXE")
        if not svv_path:
            cand1 = Path(__file__).with_name("SoundVolumeView.exe")
            cand2 = Path(__file__).with_name("svcl.exe")
            if cand1.exists():
                svv_path = str(cand1)
            elif cand2.exists():
                svv_path = str(cand2)
        if not svv_path:
            log_audio("SoundVolumeView.exe not found; cannot fallback")
            print("[audio] svcl/SoundVolumeView not found; skipping fallback")
            return
        name = device_name or ""
        # If we have a device_id, svcl supports /SetDefault <id> all
        # try preferred id first (known working) only when requested
        candidate_ids = []
        if use_preferred and PREFERRED_SVCL_ID:
            candidate_ids.append(PREFERRED_SVCL_ID)
        if device_id and device_id not in candidate_ids:
            candidate_ids.append(device_id)
        tried = False
        for cid in candidate_ids:
            try:
                res = subprocess.run([svv_path, "/SetDefault", cid, "all"], capture_output=True, text=True)
                log_audio(f"svv id={cid} all rc={res.returncode} out={res.stdout.strip()} err={res.stderr.strip()}")
                print(f"[audio] svv id all rc={res.returncode} out={res.stdout.strip()} err={res.stderr.strip()}")
                tried = True
                if res.returncode == 0:
                    return
            except Exception as ee:
                log_audio(f"svv error id={cid}: {ee}")
                print(f"[audio] svv error id={cid}: {ee}")
        if tried:
            return
        for role in ("0", "1", "2"):
            try:
                res = subprocess.run([svv_path, "/SetDefault", name, role], capture_output=True, text=True)
                log_audio(f"svv role={role} rc={res.returncode} out={res.stdout.strip()} err={res.stderr.strip()}")
                print(f"[audio] svv role={role} rc={res.returncode} out={res.stdout.strip()} err={res.stderr.strip()}")
            except Exception as ee:
                log_audio(f"svv error role={role}: {ee}")
                print(f"[audio] svv error role={role}: {ee}")

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

    def find_non_headset() -> Optional[Dict[str, str]]:
        devices = list_playback_devices()
        # Prefer Focusrite
        for item in devices:
            name = (item.get("name") or "").lower()
            if "focusrite" in name:
                return item
        for item in devices:
            name = (item.get("name") or "").lower()
            if "hyperx" not in name and "cloud" not in name:
                return item
        return devices[0] if devices else None

    def audio_switch_to_headset():
        if not settings.get("auto_switch_device", True):
            return
        if state.get("auto_switched_to_headset"):
            return
        target = find_headset()
        if not target:
            log_audio("headset not found among playback devices")
            print("[audio] headset not found among playback devices")
            return
        devices_list = list_playback_devices()
        log_audio(f"devices: {devices_list}")
        log_audio(f"target: {target}")
        print(f"[audio] devices={devices_list}")
        print(f"[audio] target={target}")
        if IS_WINDOWS:
            # Keep existing Windows preference behavior
            update_state("previous_audio_device", {"id": PREFERRED_RESTORE_ID, "name": "Speakers (Focusrite USB Audio)"})
        else:
            current = get_default_playback()
            current_id = (current or {}).get("id", "")
            current_is_headset = any(
                k in current_id.lower() for k in ("hyperx", "cloud", "kingston")
            )
            if current and not current_is_headset:
                update_state("previous_audio_device", current)
            else:
                # Current default is already the headset (or unknown); find a non-headset fallback
                fallback = find_non_headset()
                if fallback and fallback.get("id") != target.get("id"):
                    update_state("previous_audio_device", fallback)
        # Try primary target; if svcl fallback fails, try other HyperX render ids
        set_default_playback(target["id"], target.get("name"), use_preferred=True)
        # fallback attempts for other HyperX render devices
        hyperx_ids = [d["id"] for d in devices_list if "hyperx" in (d["name"].lower())]
        for hid in hyperx_ids:
            if hid != target["id"]:
                set_default_playback(hid, target.get("name"))
        update_state("auto_switched_to_headset", True)

    def audio_restore_previous():
        if not settings.get("auto_switch_device", True):
            return
        if not state.get("auto_switched_to_headset"):
            return
        if IS_WINDOWS:
            prev = {"id": PREFERRED_RESTORE_ID, "name": "Speakers (Focusrite USB Audio)"}
        else:
            prev = state.get("previous_audio_device")
            if not prev or not prev.get("id"):
                update_state("auto_switched_to_headset", False)
                return
        log_audio(f"restore prev={prev}")
        print(f"[audio] restore to {prev}")
        # Allow force-restore even if user changed, since they asked automatic back
        set_default_playback(prev["id"], prev.get("name"), use_preferred=False)
        update_state("auto_switched_to_headset", False)

    stop_flag = {"stop": False}

    def log_hid_open_issue_once_per_interval(msg: str, interval_s: float = 5.0):
        now = time.time()
        if now - hid_error_log_ts["t"] >= interval_s:
            print(msg, file=sys.stderr)
            hid_error_log_ts["t"] = now

    def hid_loop():
        while not stop_flag["stop"]:
            devices = list_devices()
            if not devices:
                update_state("connected", False)
                update_state("device", "")
                update_state("battery", (None, None))
                update_state("last_seen", 0)
                audio_restore_previous()
                time.sleep(1)
                continue

            # Tentative device name; preserve connected state across re-scans
            # (do NOT force connected=False here — that causes spurious switch cycles)
            dev_name = devices[0].get("product_string") or "HyperX Cloud Flight"
            update_state("device", dev_name)

            bootstrap_info = pick_bootstrap_device(devices)
            if bootstrap_info:
                bdev = hid.device()
                try:
                    bdev.open_path(bootstrap_info["path"])
                    bootstrap(bdev)
                except Exception as e:
                    log_hid_open_issue_once_per_interval(f"Bootstrap failed: {e}")
                finally:
                    try:
                        bdev.close()
                    except Exception:
                        pass

            handles: List[hid.device] = []
            try:
                preferred_infos = [
                    info
                    for info in devices
                    if info.get("usage_page") == STATUS_USAGE_PAGE and info.get("usage") == STATUS_USAGE
                ]
                open_candidates = preferred_infos if preferred_infos else devices

                for info in open_candidates:
                    try:
                        dev = hid.device()
                        dev.open_path(info["path"])
                        dev.set_nonblocking(False)
                        handles.append(dev)
                    except Exception as e:
                        log_hid_open_issue_once_per_interval(
                            f"HID open failed for path={info.get('path')}: {e}"
                        )

                if not handles:
                    # Linux fallback: some hidapi builds cannot open enumerate() paths
                    # but can still open by VID/PID.
                    if sys.platform.startswith("linux"):
                        try:
                            fdev = hid.device()
                            fdev.open(VENDOR_ID, PRODUCT_ID)
                            fdev.set_nonblocking(False)
                            handles.append(fdev)
                        except Exception as e:
                            log_hid_open_issue_once_per_interval(
                                f"HID fallback open(VID,PID) failed: {e}"
                            )

                if not handles:
                    update_state("connected", False)
                    if sys.platform.startswith("linux"):
                        log_hid_open_issue_once_per_interval(
                            "No accessible HyperX HID interfaces. Check udev permissions for vendor=0951 product=16c4, "
                            "and if running inside toolbox/container/flatpak, ensure /dev/hidraw access is allowed."
                        )
                    time.sleep(1)
                    continue

                # Seed last_seen so the 10 s no-data timeout doesn't fire before
                # the first battery report arrives (headset reports every ~10 s).
                update_state("last_seen", time.time())

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
                                    update_state("connected", False)
                                    update_state("last_seen", 0)
                                    audio_restore_previous()
                                    any_data = True
                                    break
                                update_state("connected", True)
                                update_state("last_seen", time.time())
                                if not was_connected and state.get("connected"):
                                    audio_switch_to_headset()
                    if not any_data:
                        time.sleep(0.05)
                        last_seen = state.get("last_seen") or 0
                        if last_seen == 0 or time.time() - last_seen > 30:
                            # No data for a while — only treat as disconnect if dongle is gone
                            if not list_devices():
                                update_state("connected", False)
                                audio_restore_previous()
                                break
                            else:
                                # Dongle still present; re-bootstrap to request a fresh report
                                for dev in handles:
                                    try:
                                        dev.write(BOOTSTRAP_REPORT)
                                    except Exception:
                                        pass
                                update_state("last_seen", time.time())
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
                try:
                    if IS_WINDOWS:
                        from win10toast import ToastNotifier
                        ToastNotifier().show_toast(
                            "HyperX Battery",
                            f"Battery low: {level}%",
                            duration=5,
                            threaded=True,
                        )
                    else:
                        subprocess.Popen([
                            "notify-send",
                            "HyperX Battery",
                            f"Battery low: {level}%",
                        ])
                except Exception:
                    pass
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

    def on_toggle_auto_switch(icon, item):
        settings["auto_switch_device"] = not settings.get("auto_switch_device", True)
        save_settings(settings)

    def on_toggle_autostart(icon, item):
        new_val = not settings.get("autostart", False)
        settings["autostart"] = new_val
        _set_startup(new_val)
        save_settings(settings)

    def battery_label(item):
        b = state.get("battery")
        charging = state.get("charging", False)
        connected = state.get("connected", False)
        if charging and b is not None:
            return f"Charging: {b}%"
        elif b is not None:
            return f"Battery: {b}%"
        elif charging:
            return "Battery: Charging"
        elif not connected:
            return "Battery: Offline"
        return "Battery: Unknown"

    def tray_loop():
        menu = pystray.Menu(
            pystray.MenuItem("HyperX Battery Indicator", lambda: None, enabled=False),
            pystray.MenuItem(battery_label, lambda: None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Auto switch device",
                on_toggle_auto_switch,
                checked=lambda item: settings.get("auto_switch_device", True),
            ),
            pystray.MenuItem(
                "Autostart",
                on_toggle_autostart,
                checked=lambda item: settings.get("autostart", False),
            ),
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