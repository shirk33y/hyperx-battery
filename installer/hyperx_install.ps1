# Creates a Startup shortcut for HyperX battery tray app
# Usage (PowerShell):
#   powershell -ExecutionPolicy Bypass -File C:\Users\shirk3y\hyperx_install.ps1

$ErrorActionPreference = 'Stop'

# Paths
$UserProfile = [Environment]::GetFolderPath('UserProfile')
$Startup = Join-Path ([Environment]::GetFolderPath('Startup')) ''
$ScriptPath = Join-Path $UserProfile 'hyperx.py'
$ShortcutPath = Join-Path $Startup 'HyperX Battery.lnk'

# Prefer pythonw.exe to avoid console window; fallback to python.exe
$pythonwCandidates = @(
    "$UserProfile\scoop\apps\python311\current\pythonw.exe",
    "$UserProfile\AppData\Local\Programs\Python\Python311\pythonw.exe",
    "$UserProfile\AppData\Local\Microsoft\WindowsApps\pythonw.exe"
)
$pythonCandidates = @(
    "$UserProfile\scoop\apps\python311\current\python.exe",
    "$UserProfile\AppData\Local\Programs\Python\Python311\python.exe",
    "$UserProfile\AppData\Local\Microsoft\WindowsApps\python.exe"
)

$pythonExe = $pythonwCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $pythonExe) {
    $pythonExe = $pythonCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $pythonExe) {
    throw "Could not find pythonw/python. Please update hyperx_install.ps1 with your Python path."
}

if (-not (Test-Path $ScriptPath)) {
    throw "hyperx.py not found at $ScriptPath"
}

# Create shortcut
$WScriptShell = New-Object -ComObject WScript.Shell
$Shortcut = $WScriptShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $pythonExe
$Shortcut.Arguments = "`"$ScriptPath`""
$Shortcut.WorkingDirectory = $UserProfile
$Shortcut.WindowStyle = 7 # Minimized
$Shortcut.Save()

Write-Host "Created startup shortcut: $ShortcutPath"
Write-Host "Target: $pythonExe $ScriptPath"
