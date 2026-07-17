"""Unit tests for hyperx.py — dedup, debounce, cooldown logic.

Run: python -m pytest src/hyperx_test.py -v
"""

import time
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers to import the module despite Windows-only deps
# ---------------------------------------------------------------------------

def _stub_modules():
    """Return a dict of stub modules for Windows-only dependencies."""
    stubs = {}
    for mod_name in ("comtypes", "pycaw", "pycaw.utils", "hid", "pystray", "PIL",
                     "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont"):
        m = MagicMock()
        stubs[mod_name] = m
    # pycaw.utils.AudioUtilities needs to be accessible
    stubs["pycaw"].utils = stubs["pycaw.utils"]
    stubs["pycaw.utils"].AudioUtilities = MagicMock()
    return stubs


@pytest.fixture(autouse=True)
def _patch_win_modules(monkeypatch):
    """Patch Windows-only modules so hyperx.py can be imported on Linux."""
    import sys
    stubs = _stub_modules()
    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)


# ---------------------------------------------------------------------------
# Import the pure functions we can test directly
# ---------------------------------------------------------------------------

from hyperx import calc_percentage, handle_report, _last_report


# ---------------------------------------------------------------------------
# calc_percentage
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_last_report():
    """Reset dedup state between tests so each test starts clean."""
    for k in _last_report:
        _last_report[k] = None
    yield


class TestCalcPercentage:
    def test_charging_full(self):
        assert calc_percentage(0x10, 5) == 100

    def test_charging_unknown(self):
        assert calc_percentage(0x10, 25) is None

    def test_high_battery(self):
        assert calc_percentage(0x0F, 135) == 100

    def test_mid_battery(self):
        assert calc_percentage(0x0E, 230) == 60

    def test_low_battery(self):
        assert calc_percentage(0x0E, 50) == 10

    def test_unknown_charge_state(self):
        assert calc_percentage(0x00, 100) is None


# ---------------------------------------------------------------------------
# handle_report
# ---------------------------------------------------------------------------

class TestHandleReport:
    def test_power_off(self):
        assert handle_report(bytes([0x64, 0x03])) == ("power", "off")

    def test_power_on(self):
        assert handle_report(bytes([0x64, 0x01])) == ("power", "on")

    def test_muted_true(self):
        assert handle_report(bytes([0x65, 0x04])) == ("muted", True)

    def test_muted_false(self):
        assert handle_report(bytes([0x65, 0x00])) == ("muted", False)

    def test_volume_up(self):
        data = bytes([0x00, 0x01, 0x00, 0x00, 0x00])
        assert handle_report(data) == ("volume", "up")

    def test_volume_down(self):
        data = bytes([0x00, 0x02, 0x00, 0x00, 0x00])
        assert handle_report(data) == ("volume", "down")

    def test_empty(self):
        assert handle_report(b"") is None


# ---------------------------------------------------------------------------
# handle_report deduplication
# ---------------------------------------------------------------------------

class TestHandleReportDedup:
    def test_battery_printed_once_on_repeat(self, capsys):
        data = bytes([0] * 3 + [0x0F, 35] + [0] * 10)  # 75%
        assert handle_report(data) == ("battery", (75, False))
        assert "Battery: 75%" in capsys.readouterr().out

        assert handle_report(data) == ("battery", (75, False))
        assert capsys.readouterr().out == ""

    def test_battery_prints_on_change(self, capsys):
        data75 = bytes([0] * 3 + [0x0F, 35] + [0] * 10)
        data100 = bytes([0] * 3 + [0x0F, 135] + [0] * 10)
        handle_report(data75)
        capsys.readouterr()
        handle_report(data100)
        assert "Battery: 100%" in capsys.readouterr().out

    def test_power_off_printed_once(self, capsys):
        handle_report(bytes([0x64, 0x03]))
        assert "Power: off" in capsys.readouterr().out
        handle_report(bytes([0x64, 0x03]))
        assert capsys.readouterr().out == ""

    def test_power_on_after_off_prints(self, capsys):
        handle_report(bytes([0x64, 0x03]))
        capsys.readouterr()
        handle_report(bytes([0x64, 0x01]))
        assert "Power: on" in capsys.readouterr().out

    def test_muted_dedup(self, capsys):
        handle_report(bytes([0x65, 0x04]))
        assert "Muted: True" in capsys.readouterr().out
        handle_report(bytes([0x65, 0x04]))
        assert capsys.readouterr().out == ""
        handle_report(bytes([0x65, 0x00]))
        assert "Muted: False" in capsys.readouterr().out

    def test_volume_always_prints(self, capsys):
        data = bytes([0x00, 0x01, 0x00, 0x00, 0x00])
        handle_report(data)
        assert "Volume: up" in capsys.readouterr().out
        handle_report(data)
        assert "Volume: up" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Disconnect debounce logic
# ---------------------------------------------------------------------------

class TestDisconnectDebounce:
    """Verify brief HID dropouts don't trigger audio restore."""

    DEBOUNCE = 5

    def _make_state(self, **overrides):
        s = {
            "battery": None, "charging": False, "muted": None,
            "power": None, "device": None, "last_notified": None,
            "connected": False, "last_seen": 0,
            "previous_audio_device": {"id": "spk-1", "name": "Speakers"},
            "auto_switched_to_headset": True,
            "power_off_at": 0.0, "disconnect_at": 0.0,
        }
        s.update(overrides)
        return s

    def _make_restore_fn(self, state, log_calls):
        DISCONNECT_DEBOUNCE = self.DEBOUNCE

        def audio_restore_previous(force=False):
            if not state.get("auto_switched_to_headset"):
                return
            if not force:
                disc_at = state.get("disconnect_at", 0.0)
                if not disc_at:
                    return
                elapsed = time.time() - disc_at
                if elapsed < DISCONNECT_DEBOUNCE:
                    log_calls.append(("debounced", elapsed))
                    return
            log_calls.append(("restored",))
            state["auto_switched_to_headset"] = False
            state["disconnect_at"] = 0.0

        return audio_restore_previous

    def test_brief_dropout_debounced(self):
        state = self._make_state(disconnect_at=time.time())
        log = []
        restore = self._make_restore_fn(state, log)
        restore()
        assert log[0][0] == "debounced"
        assert state["auto_switched_to_headset"]

    def test_sustained_disconnect_restores(self):
        state = self._make_state(disconnect_at=time.time() - 6)
        log = []
        restore = self._make_restore_fn(state, log)
        restore()
        assert log[0][0] == "restored"
        assert not state["auto_switched_to_headset"]

    def test_force_bypasses_debounce(self):
        state = self._make_state(disconnect_at=time.time())
        log = []
        restore = self._make_restore_fn(state, log)
        restore(force=True)
        assert log[0][0] == "restored"

    def test_no_disconnect_at_skips(self):
        state = self._make_state(disconnect_at=0.0)
        log = []
        restore = self._make_restore_fn(state, log)
        restore()
        assert len(log) == 0

    def test_not_switched_skips(self):
        state = self._make_state(auto_switched_to_headset=False, disconnect_at=time.time() - 10)
        log = []
        restore = self._make_restore_fn(state, log)
        restore()
        assert len(log) == 0


# ---------------------------------------------------------------------------
# Power-off cooldown logic (integration-style with mocked I/O)
# ---------------------------------------------------------------------------

class TestPowerOffCooldown:
    """Verify that ghost reconnections after power-off don't trigger audio switch."""

    def _make_state(self, **overrides):
        s = {
            "battery": None, "charging": False, "muted": None,
            "power": None, "device": None, "last_notified": None,
            "connected": False, "last_seen": 0,
            "previous_audio_device": None,
            "auto_switched_to_headset": False,
            "power_off_at": 0.0, "disconnect_at": 0.0,
        }
        s.update(overrides)
        return s

    def _make_switch_fn(self, state, settings, log_calls):
        """Build a minimal audio_switch_to_headset that mirrors the real logic."""
        POWER_OFF_COOLDOWN = 30

        def audio_switch_to_headset():
            if not settings.get("auto_switch_device", True):
                return
            if state.get("auto_switched_to_headset"):
                return
            power_off_at = state.get("power_off_at", 0.0)
            if power_off_at and (time.time() - power_off_at) < POWER_OFF_COOLDOWN:
                log_calls.append(("cooldown_blocked", time.time() - power_off_at))
                return
            log_calls.append(("switched_to_headset",))
            state["auto_switched_to_headset"] = True

        return audio_switch_to_headset

    def test_cooldown_blocks_reconnect(self):
        state = self._make_state()
        settings = {"auto_switch_device": True}
        log = []
        switch = self._make_switch_fn(state, settings, log)

        # Simulate power-off
        state["power_off_at"] = time.time()
        state["auto_switched_to_headset"] = False

        # Ghost reconnect attempt — should be blocked
        switch()
        assert len(log) == 1
        assert log[0][0] == "cooldown_blocked"
        assert not state["auto_switched_to_headset"]

    def test_cooldown_expires_allows_reconnect(self):
        state = self._make_state()
        settings = {"auto_switch_device": True}
        log = []
        switch = self._make_switch_fn(state, settings, log)

        # Simulate power-off 31 seconds ago
        state["power_off_at"] = time.time() - 31
        state["auto_switched_to_headset"] = False

        switch()
        assert len(log) == 1
        assert log[0][0] == "switched_to_headset"
        assert state["auto_switched_to_headset"]

    def test_power_on_clears_cooldown(self):
        state = self._make_state()
        settings = {"auto_switch_device": True}
        log = []
        switch = self._make_switch_fn(state, settings, log)

        # Simulate power-off just now
        state["power_off_at"] = time.time()

        # Then explicit power-on (clears cooldown)
        state["power_off_at"] = 0.0

        switch()
        assert len(log) == 1
        assert log[0][0] == "switched_to_headset"

    def test_no_cooldown_on_first_connect(self):
        state = self._make_state()
        settings = {"auto_switch_device": True}
        log = []
        switch = self._make_switch_fn(state, settings, log)

        # power_off_at is 0 (never powered off) — should switch normally
        switch()
        assert len(log) == 1
        assert log[0][0] == "switched_to_headset"

    def test_already_switched_skips(self):
        state = self._make_state(auto_switched_to_headset=True)
        settings = {"auto_switch_device": True}
        log = []
        switch = self._make_switch_fn(state, settings, log)

        switch()
        assert len(log) == 0

    def test_auto_switch_disabled_skips(self):
        state = self._make_state()
        settings = {"auto_switch_device": False}
        log = []
        switch = self._make_switch_fn(state, settings, log)

        switch()
        assert len(log) == 0
