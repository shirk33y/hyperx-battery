; HyperX Battery Indicator - NSIS Installer
; Minimal UI: progress bar + status text only
; Silent install: hyperx-battery-setup.exe /S

!include "MUI2.nsh"
!include "FileFunc.nsh"

; ---- App metadata ----
!define APP_NAME "HyperX Battery"
!define APP_EXE "hyperx-battery.exe"
!define APP_VERSION "1.0.0"
!define APP_PUBLISHER "shirk33y"
!define APP_URL "https://github.com/shirk33y/hyperx-battery"
!define INSTALL_DIR "$LOCALAPPDATA\${APP_NAME}"
!define UNINSTALL_REG "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"

; ---- Installer settings ----
Name "${APP_NAME}"
OutFile "hyperx-battery-setup.exe"
InstallDir "${INSTALL_DIR}"
RequestExecutionLevel user
SetCompress off
ShowInstDetails nevershow
ShowUninstDetails nevershow
AutoCloseWindow true
BrandingText "${APP_NAME} v${APP_VERSION}"

; ---- MUI2 settings (minimal: progress bar + status) ----
!define MUI_ICON "..\assets\hyperx.ico"
!define MUI_UNICON "..\assets\hyperx.ico"

; Skip all pages except install progress — auto-closes when done
!insertmacro MUI_PAGE_INSTFILES

; Uninstaller pages
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; ---- Install section ----
Section "Install"
    ; Kill running instance if any
    DetailPrint "Stopping running instances..."
    nsExec::ExecToLog 'taskkill /F /IM "${APP_EXE}" /T'
    
    ; Wait for process to fully terminate (retry up to 10 times, 500ms each)
    StrCpy $0 0
    retry_kill:
        IntOp $0 $0 + 1
        IntCmp $0 10 done_kill done_kill
        Sleep 500
        ClearErrors
        FileOpen $1 "$INSTDIR\${APP_EXE}" w
        IfErrors retry_kill
        FileClose $1
    done_kill:

    ; Install files
    SetOutPath "$INSTDIR"
    DetailPrint "Installing ${APP_NAME}..."
    File "..\dist\${APP_EXE}"

    DetailPrint "Installing audio switcher (svcl)..."
    File "..\tools\svcl.exe"

    DetailPrint "Installing icon..."
    File "..\assets\hyperx.ico"

    ; Create startup shortcut (auto-start on login)
    DetailPrint "Creating startup shortcut..."
    CreateShortCut "$SMSTARTUP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\hyperx.ico" 0

    ; Create Start Menu shortcut
    CreateDirectory "$SMPROGRAMS\${APP_NAME}"
    CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\hyperx.ico" 0
    CreateShortCut "$SMPROGRAMS\${APP_NAME}\Uninstall.lnk" "$INSTDIR\uninstall.exe"

    ; Write uninstaller
    DetailPrint "Creating uninstaller..."
    WriteUninstaller "$INSTDIR\uninstall.exe"

    ; Add/Remove Programs registry
    WriteRegStr HKCU "${UNINSTALL_REG}" "DisplayName" "${APP_NAME}"
    WriteRegStr HKCU "${UNINSTALL_REG}" "DisplayVersion" "${APP_VERSION}"
    WriteRegStr HKCU "${UNINSTALL_REG}" "Publisher" "${APP_PUBLISHER}"
    WriteRegStr HKCU "${UNINSTALL_REG}" "URLInfoAbout" "${APP_URL}"
    WriteRegStr HKCU "${UNINSTALL_REG}" "UninstallString" '"$INSTDIR\uninstall.exe"'
    WriteRegStr HKCU "${UNINSTALL_REG}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
    WriteRegStr HKCU "${UNINSTALL_REG}" "InstallLocation" "$INSTDIR"
    WriteRegStr HKCU "${UNINSTALL_REG}" "DisplayIcon" "$INSTDIR\hyperx.ico"
    WriteRegDWORD HKCU "${UNINSTALL_REG}" "NoModify" 1
    WriteRegDWORD HKCU "${UNINSTALL_REG}" "NoRepair" 1

    ; Estimate size for Add/Remove Programs
    ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
    IntFmt $0 "0x%08X" $0
    WriteRegDWORD HKCU "${UNINSTALL_REG}" "EstimatedSize" $0

    ; Launch the app after install
    Exec '"$INSTDIR\${APP_EXE}"'
SectionEnd

; ---- Uninstall section ----
Section "Uninstall"
    ; Kill running instance
    nsExec::ExecToLog 'taskkill /F /IM "${APP_EXE}" /T'

    ; Remove files
    Delete "$INSTDIR\${APP_EXE}"
    Delete "$INSTDIR\svcl.exe"
    Delete "$INSTDIR\hyperx.ico"
    Delete "$INSTDIR\uninstall.exe"
    RMDir "$INSTDIR"

    ; Remove shortcuts
    Delete "$SMSTARTUP\${APP_NAME}.lnk"
    Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
    Delete "$SMPROGRAMS\${APP_NAME}\Uninstall.lnk"
    RMDir "$SMPROGRAMS\${APP_NAME}"

    ; Remove registry
    DeleteRegKey HKCU "${UNINSTALL_REG}"
SectionEnd
