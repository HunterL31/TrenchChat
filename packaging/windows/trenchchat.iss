; Inno Setup script for TrenchChat
;
; Produces a Windows installer (.exe) that:
;   - Installs to C:\Program Files\TrenchChat\ (or user-chosen dir)
;   - Upgrades in-place when the same AppId is detected
;   - Does NOT touch %USERPROFILE%\.trenchchat\ (user identity, DB, config)
;
; Build from CI:
;   iscc /DAppVersion=%APP_VERSION% packaging\windows\trenchchat.iss

#define AppName "TrenchChat"
#define AppPublisher "TrenchChat"
#define AppURL "https://github.com/HunterL31/TrenchChat"
#define AppExeName "TrenchChat.exe"
; AppVersion is injected by CI via /DAppVersion=... on the iscc command line.
; Fallback for local builds:
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
; Fixed GUID — Inno Setup uses this to detect an existing installation and
; perform an in-place upgrade rather than a side-by-side install.
; NEVER change this GUID once the first installer has been shipped.
AppId={{F3A2B1C0-9D4E-4F5A-8B6C-7E2D1A0F3B9C}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
; Allow per-user installs without admin rights
PrivilegesRequiredOverridesAllowed=dialog
; Output
OutputDir=..\..\dist\installer
OutputBaseFilename=TrenchChat-Setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
; Appearance
WizardStyle=modern
; Minimum Windows version: Windows 10
MinVersion=10.0
; Architecture
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The entire PyInstaller onedir output — all binaries, Qt libs, Python runtime
Source: "..\..\dist\TrenchChat\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove the installation directory on uninstall (app files only).
; The user data directory %USERPROFILE%\.trenchchat\ is intentionally
; NOT listed here — it is preserved across uninstalls and upgrades.
Type: filesandordirs; Name: "{app}"
