# Cross-compile hyperx-battery for Windows using Wine + PyInstaller + NSIS
FROM docker.io/tobix/pywine:3.12

# Install NSIS (Linux-native, cross-compiles Windows installers)
RUN apt-get update -qq && apt-get install -y -qq nsis > /dev/null 2>&1 && rm -rf /var/lib/apt/lists/*

# Install Python dependencies inside Wine
RUN xvfb-run sh -c "\
    wine pip install --no-warn-script-location hidapi pystray pillow pycaw comtypes pyinstaller; \
    wineserver -w"

WORKDIR /src
