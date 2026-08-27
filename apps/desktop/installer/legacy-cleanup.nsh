; Safe pre-install cleanup for legacy Ansatz installs.
;
; This include is intentionally conservative. It only moves a directory when
; the directory name is exactly the configured application filename and the
; directory contains the files that identify an Ansatz Electron installation.
; User data and the per-user Hermes runtime live outside the application
; directory and are never touched here.

; The custom include is prepended before electron-builder's installer.nsi, so
; import the standard helper macros that our functions use explicitly.
!include "LogicLib.nsh"
!include "FileFunc.nsh"

; These definitions are normally introduced by common.nsh/multiUser.nsh, but
; custom includes are parsed before those files. Define the stable values here
; so the include is self-contained and does not depend on include order.
!define ANSATZ_APP_EXECUTABLE_FILENAME "${PRODUCT_FILENAME}.exe"
!define ANSATZ_INSTALL_REGISTRY_KEY "Software\${APP_GUID}"

Var /GLOBAL AnsatzLegacyInstallDir
Var /GLOBAL AnsatzLegacyBackupDir
Var /GLOBAL AnsatzLegacyCleanupPending

!macro AnsatzLegacyLog MESSAGE
  DetailPrint "Ansatz installer: ${MESSAGE}"
  !ifmacrodef LogText
    !insertmacro LogText "Ansatz installer: ${MESSAGE}"
  !endif
!macroend

Function AnsatzResolveLegacyInstallDir
  StrCpy $AnsatzLegacyInstallDir "$INSTDIR"

  ; The directory page lets users choose the parent directory. The standard
  ; electron-builder page appends ${APP_FILENAME} immediately before install;
  ; mirror that logic here so validation and cleanup use the same path.
  ${GetFileName} "$AnsatzLegacyInstallDir" $0
  StrCmp "$0" "${APP_FILENAME}" resolved
  StrCpy $AnsatzLegacyInstallDir "$AnsatzLegacyInstallDir\${APP_FILENAME}"

resolved:
  ; The directory page supplies a normalized absolute path. The signature
  ; check below also rejects drive roots and paths without a parent.
FunctionEnd

Function AnsatzLegacySignatureIsValid
  ; Never operate on a root or on a directory with a different leaf name.
  ${GetFileName} "$AnsatzLegacyInstallDir" $0
  StrCmp "$0" "${APP_FILENAME}" 0 invalid
  ${GetParent} "$AnsatzLegacyInstallDir" $1
  StrCmp "$1" "" invalid
  StrCmp "$1" "$AnsatzLegacyInstallDir" invalid

  ; New installers carry a unique marker. Keep a legacy signature for
  ; versions produced before the marker was introduced so existing users can
  ; upgrade once without manual deletion.
  ${If} ${FileExists} "$AnsatzLegacyInstallDir\resources\ansatz-install-marker.json"
    ${IfNot} ${FileExists} "$AnsatzLegacyInstallDir\${ANSATZ_APP_EXECUTABLE_FILENAME}"
      Goto invalid
    ${EndIf}
    ${IfNot} ${FileExists} "$AnsatzLegacyInstallDir\resources\app.asar"
      Goto invalid
    ${EndIf}
    Goto valid
  ${EndIf}

  ; Legacy packages must have all of these independent signatures. This is
  ; deliberately stricter than checking only Ansatz.exe, preventing a folder
  ; belonging to another Electron application from being removed.
  ${IfNot} ${FileExists} "$AnsatzLegacyInstallDir\${ANSATZ_APP_EXECUTABLE_FILENAME}"
    Goto invalid
  ${EndIf}
  ${IfNot} ${FileExists} "$AnsatzLegacyInstallDir\Uninstall ${PRODUCT_FILENAME}.exe"
    Goto invalid
  ${EndIf}
  ${IfNot} ${FileExists} "$AnsatzLegacyInstallDir\resources\app.asar"
    Goto invalid
  ${EndIf}
  ${IfNot} ${FileExists} "$AnsatzLegacyInstallDir\resources\install-stamp.json"
    Goto invalid
  ${EndIf}
  ${IfNot} ${FileExists} "$AnsatzLegacyInstallDir\resources\bootstrap\install.ps1"
    Goto invalid
  ${EndIf}
  ${IfNot} ${FileExists} "$AnsatzLegacyInstallDir\resources\bootstrap\payload-manifest.json"
    Goto invalid
  ${EndIf}

valid:
  StrCpy $0 1
  Return

invalid:
  StrCpy $0 0
FunctionEnd

Function AnsatzStopLegacyProcesses
  ; Stop only processes whose executable path is exactly the validated old
  ; application executable. We do not use taskkill /IM, which could kill an
  ; unrelated application with the same filename.
  StrCpy $1 "$AnsatzLegacyInstallDir\${ANSATZ_APP_EXECUTABLE_FILENAME}"
  StrCpy $2 "$SYSDIR\WindowsPowerShell\v1.0\powershell.exe"
  ${If} ${FileExists} "$2"
    nsExec::ExecToStack `"$2" -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$$target = [IO.Path]::GetFullPath('$1'); Get-Process -Name '${PRODUCT_FILENAME}' -ErrorAction SilentlyContinue | Where-Object { $$_.Path -and [String]::Equals([IO.Path]::GetFullPath($$_.Path), $$target, [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { Stop-Process -Id $$_.Id -Force -ErrorAction SilentlyContinue }"`
    Pop $3
    Pop $4
  ${EndIf}
  Sleep 500
FunctionEnd

Function AnsatzPrepareLegacyCleanup
  StrCpy $AnsatzLegacyCleanupPending 0
  Call AnsatzResolveLegacyInstallDir
  Call AnsatzLegacySignatureIsValid
  StrCmp $0 1 0 done

  ; If this path is registered, electron-builder's own uninstallOldVersion
  ; flow remains authoritative. The custom path is for unregistered legacy
  ; copies such as the historical D:\an\Ansatz installation.
  ReadRegStr $1 SHELL_CONTEXT "${ANSATZ_INSTALL_REGISTRY_KEY}" InstallLocation
  StrCmp "$1" "$AnsatzLegacyInstallDir" done

  ; Keep a recoverable sibling backup until the new files are installed. Do
  ; not overwrite an existing backup: that would destroy recoverability.
  StrCpy $AnsatzLegacyBackupDir "$AnsatzLegacyInstallDir.__ansatz-old"
  ${If} ${FileExists} "$AnsatzLegacyBackupDir"
    !insertmacro AnsatzLegacyLog "legacy backup exists; cleanup skipped: $AnsatzLegacyBackupDir"
    Goto done
  ${EndIf}

  !insertmacro AnsatzLegacyLog "verified legacy directory; stopping old processes: $AnsatzLegacyInstallDir"
  Call AnsatzStopLegacyProcesses
  Rename "$AnsatzLegacyInstallDir" "$AnsatzLegacyBackupDir"
  IfErrors rename_failed rename_ok

rename_ok:
  StrCpy $AnsatzLegacyCleanupPending 1
  !insertmacro AnsatzLegacyLog "legacy directory moved to backup: $AnsatzLegacyBackupDir"
  Goto done

rename_failed:
  !insertmacro AnsatzLegacyLog "legacy directory is locked or inaccessible; cleanup skipped"
  MessageBox MB_OK|MB_ICONEXCLAMATION "旧版 Ansatz 目录无法安全移除。请先退出 Ansatz 后重试；为避免误删，本次安装不会继续。"
  Abort

done:
FunctionEnd

Function AnsatzFinalizeLegacyCleanup
  StrCmp $AnsatzLegacyCleanupPending 1 0 done
  ${If} ${FileExists} "$AnsatzLegacyBackupDir"
    RMDir /r "$AnsatzLegacyBackupDir"
    IfErrors backup_remove_failed backup_remove_ok
backup_remove_failed:
      !insertmacro AnsatzLegacyLog "unable to remove legacy backup; retained: $AnsatzLegacyBackupDir"
      Goto done
backup_remove_ok:
      !insertmacro AnsatzLegacyLog "legacy backup removed: $AnsatzLegacyBackupDir"
  ${EndIf}
  StrCpy $AnsatzLegacyCleanupPending 0
done:
FunctionEnd

; Called after the user has selected the destination and before the install
; section extracts any new files.
!macro customPageAfterChangeDir
  ; The directory page leaves its own custom-pre define in scope in some
  ; electron-builder releases. Clear it before attaching our instfiles hook.
  !ifdef MUI_PAGE_CUSTOMFUNCTION_PRE
    !undef MUI_PAGE_CUSTOMFUNCTION_PRE
  !endif
  !define MUI_PAGE_CUSTOMFUNCTION_PRE AnsatzLegacyPreparePage
  Function AnsatzLegacyPreparePage
    ; Preserve electron-builder's destination normalization callback. Without
    ; this, choosing D:\an would make the installer extract into D:\an rather
    ; than the intended D:\an\Ansatz directory.
    ${StrContains} $0 "${APP_FILENAME}" $INSTDIR
    ${If} $0 == ""
      StrCpy $INSTDIR "$INSTDIR\${APP_FILENAME}"
    ${EndIf}
    Call AnsatzPrepareLegacyCleanup
  FunctionEnd
!macroend

; The new application files are now in place. Remove only the verified,
; recoverable backup created above.
!macro customInstall
  Call AnsatzFinalizeLegacyCleanup
!macroend
