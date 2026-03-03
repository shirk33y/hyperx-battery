PYTHON    := /usr/bin/python3
SCRIPT    := $(abspath src/hyperx.py)
AUTOSTART := $(HOME)/.config/autostart/hyperx-battery.desktop

# Use distrobox-host-exec when inside a distrobox container so the process
# runs in the host session (where the tray/display lives).
UID     := $(shell id -u)
XDG_RUN := /run/user/$(UID)
# Environment needed for GUI apps launched from inside distrobox
HOST_ENV := WAYLAND_DISPLAY=wayland-0 \
            DBUS_SESSION_BUS_ADDRESS=unix:path=$(XDG_RUN)/bus \
            XDG_RUNTIME_DIR=$(XDG_RUN)

.PHONY: start reload install

# Launch the app in the background (does not kill an existing instance)
start:
	env $(HOST_ENV) nohup $(PYTHON) $(SCRIPT) >/dev/null 2>&1 &
	@echo "Started."

# Kill any running instance and restart with the current code
reload:
	-pkill -f "hyperx.py"
	@sleep 0.5
	env $(HOST_ENV) nohup $(PYTHON) $(SCRIPT) >/dev/null 2>&1 &
	@echo "Reloaded."

# Write (or update) the GNOME/KDE autostart desktop entry
install:
	@mkdir -p $(dir $(AUTOSTART))
	@printf '[Desktop Entry]\nType=Application\nName=HyperX Battery\nExec=$(PYTHON) $(SCRIPT)\nX-GNOME-Autostart-enabled=true\n' > $(AUTOSTART)
	@echo "Autostart entry written: $(AUTOSTART)"
