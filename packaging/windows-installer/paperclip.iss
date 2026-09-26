; Paperclip Windows Installer
;
; This is the user-facing distribution: one downloadable Setup.exe. The files
; installed by it contain the Paperclip application and a portable Node.js
; runtime, so the target machine does NOT need Node.js, npm, pnpm, Python, or
; any other developer tooling.
;
; Build the standalone payload first:
;   powershell -ExecutionPolicy Bypass -File packaging\windows-installer\build-installer.ps1
;
; The build script runs build_exe.py with --embed-node and verifies that the
; embedded runtime is present before invoking this file with Inno Setup 6.

#define MyAppName "Paperclip"
#define MyAppVersion "0.3.1"
#define MyAppPublisher "Paperclip"
#define MyAppURL "https://github.com/paperclipai/paperclip"
#define MyAppExeName "paperclip.exe"

[Setup]
AppId={{A57B7782-2A5F-4D38-9A57-7A1D9C4C4E4B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\{#MyAppName}
DisableDirPage=no
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
LicenseFile=..\..\LICENSE
OutputDir=dist
OutputBaseFilename=Paperclip-Setup-{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64 arm64
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription=Paperclip - orchestrate AI agent teams
VersionInfoCopyright=MIT License
; No administrator privileges are required: the app is installed per-user.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "addtopath"; Description: "Add Paperclip to your user PATH"; GroupDescription: "Other:"

[Files]
; The onedir bundle contains paperclip.exe and _internal/. The _internal tree
; contains payload/ and runtime/node/node.exe. Inno compresses all of it into
; this single Setup.exe; the installed app never downloads or looks for Node.
Source: "..\exe\dist\paperclip\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "doctor"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\Paperclip Onboard"; Filename: "{app}\{#MyAppExeName}"; Parameters: "onboard"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "doctor"; Tasks: desktopicon; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "doctor"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  Path: string;
begin
  if (CurStep = ssPostInstall) and IsTaskSelected('addtopath') then
  begin
    Path := '';
    if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Path) then
      Path := '';
    if Pos(LowerCase(ExpandConstant('{app}')), LowerCase(Path)) = 0 then
    begin
      if Path <> '' then
        Path := Path + ';' + ExpandConstant('{app}')
      else
        Path := ExpandConstant('{app}');
      RegWriteStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Path);
    end;
  end;
end;
