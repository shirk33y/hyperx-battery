# HyperX Battery — build Windows installer from WSL2
#
# Usage:
#   make            # build exe + installer
#   make exe        # build PyInstaller exe only
#   make installer  # build NSIS installer only (requires exe)
#   make deps       # install Python dependencies
#   make clean      # remove build artifacts
#
# Prerequisites (Windows side):
#   - Python 3.11+ with pyinstaller on PATH
#   - NSIS (makensis) on PATH
#   pip install -r requirements.txt
#
# Works from both WSL and Windows filesystems. When the repo lives on
# the WSL filesystem (\\wsl.localhost\...), sources are rsynced to a
# temp dir under %TEMP% for the build, then artifacts are copied back.

PYINST   := $(shell cmd.exe /c "where pyinstaller" 2>/dev/null | head -1 | tr -d '\r')
MAKENSIS := $(shell cmd.exe /c "where makensis" 2>/dev/null | head -1 | tr -d '\r')
PIP      := $(shell cmd.exe /c "where python" 2>/dev/null | head -1 | tr -d '\r')

# Detect if we're on the WSL filesystem (UNC path) vs Windows (/mnt/c/...)
WIN_PATH := $(shell wslpath -w $(CURDIR) 2>/dev/null)
IS_UNC   := $(if $(filter \\\\%,$(WIN_PATH)),1,)

# When on WSL fs, stage to a Windows temp dir; otherwise build in-place
ifdef IS_UNC
  WIN_TEMP   := $(shell cmd.exe /c "echo %TEMP%" 2>/dev/null | tr -d '\r')
  BUILD_DIR  := $(WIN_TEMP)\hyperx-battery-build
  BUILD_WSL  := $(shell wslpath '$(WIN_TEMP)/hyperx-battery-build' 2>/dev/null)
else
  BUILD_DIR  := $(WIN_PATH)
  BUILD_WSL  := $(CURDIR)
endif

DIST_EXE := dist/hyperx-battery.exe
SETUP    := installer/hyperx-battery-setup.exe

DOCKER     := podman
DOCKER_IMG := hyperx-battery-builder

.PHONY: all exe installer deps clean check-tools stage test docker-image docker-build

all: installer

check-tools:
ifndef PYINST
	$(error pyinstaller not found on Windows PATH. Install: pip install pyinstaller)
endif
ifndef MAKENSIS
	$(error makensis not found on Windows PATH. Install: scoop install nsis)
endif

stage:
ifdef IS_UNC
	@echo "==> Staging sources to Windows filesystem..."
	@mkdir -p "$(BUILD_WSL)"
	@rsync -a --delete --exclude=dist/ --exclude=build/ \
		$(CURDIR)/ "$(BUILD_WSL)/"
endif

deps: check-tools
	@echo "==> Installing Python dependencies..."
	@cmd.exe /c "cd /d $(BUILD_DIR) && $(PIP) -m pip install -r requirements.txt --quiet"

exe: check-tools stage $(DIST_EXE)

$(DIST_EXE): src/hyperx.py hyperx-battery.spec assets/hyperx.ico tools/svcl.exe
	@echo "==> Building exe with PyInstaller..."
	@cmd.exe /c "cd /d $(BUILD_DIR) && $(PYINST) hyperx-battery.spec --noconfirm"
ifdef IS_UNC
	@mkdir -p dist
	@cp "$(BUILD_WSL)/dist/hyperx-battery.exe" dist/
endif
	@echo "==> $(CURDIR)/$(DIST_EXE) ($(shell du -h $(DIST_EXE) 2>/dev/null | cut -f1 || echo '?'))"

installer: check-tools stage $(SETUP)

$(SETUP): $(DIST_EXE) installer/hyperx-battery.nsi assets/hyperx.ico tools/svcl.exe
ifdef IS_UNC
	@mkdir -p "$(BUILD_WSL)/dist"
	@cp dist/hyperx-battery.exe "$(BUILD_WSL)/dist/"
endif
	@echo "==> Building NSIS installer..."
	@cmd.exe /c "cd /d $(BUILD_DIR) && $(MAKENSIS) installer\hyperx-battery.nsi"
ifdef IS_UNC
	@cp "$(BUILD_WSL)/installer/hyperx-battery-setup.exe" installer/
endif
	@echo "==> $(CURDIR)/$(SETUP) ($(shell du -h $(SETUP) 2>/dev/null | cut -f1 || echo '?'))"

test:
	@python -m pytest src/hyperx_test.py -v

clean:
	@echo "==> Cleaning build artifacts..."
	rm -rf dist/ build/ installer/hyperx-battery-setup.exe
ifdef IS_UNC
	rm -rf "$(BUILD_WSL)"
endif
	@echo "==> Clean."

# ---- Docker-based Windows cross-build (no Windows Python/NSIS needed) ----

docker-image: Dockerfile
	@echo "==> Building Docker image (this may take a while the first time)..."
	@$(DOCKER) build -q -t $(DOCKER_IMG) .

docker-build: docker-image
	@echo "==> Building exe + installer in Docker..."
	@$(DOCKER) run --rm -v "$(CURDIR):/src" $(DOCKER_IMG) \
		sh -c '\
			xvfb-run sh -c "wine pyinstaller hyperx-battery.spec --noconfirm && wineserver -w" && \
			makensis -V2 installer/hyperx-battery.nsi \
		'
	@echo "==> $(CURDIR)/$(DIST_EXE) ($(shell du -h $(DIST_EXE) 2>/dev/null | cut -f1 || echo '?'))"
	@echo "==> $(CURDIR)/$(SETUP) ($(shell du -h $(SETUP) 2>/dev/null | cut -f1 || echo '?'))"
