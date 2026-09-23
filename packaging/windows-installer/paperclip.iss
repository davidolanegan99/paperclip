; Paperclip Windows Installer - Inno Setup Script
; Builds a proper Windows installer that is NOT flagged by AV
; Unlike PyInstaller onefile which self-extracts to %TEMP%, this is a standard
; installer that puts files in Program Files and creates shortcuts.
; This installer is far less likely to be flagged than PyInstaller binaries.
;
; Requirements:
; - Inno Setup 6.x from https://jrsoftware.org/isinfo.php
; - Node.js 24.11+ installed on target machine (or bundled via --embed-node)
; - Payload staged via: python packaging/exe/build_exe.py --stage-payload --skip-app-build --allow-old-node --allow-version-mismatch --no-freeze
; - Zig launcher built via: packaging/windows-launcher/build.bat
;
; Build:
;   iscc packaging/windows-installer/paperclip.iss
;
; Output: packaging/windows-installer/dist/Paperclip-Setup.exe

#define MyAppName "Paperclip"
#define MyAppVersion "0.3.1"
#define MyAppPublisher "Paperclip"
#define MyAppURL "https://github.com/paperclipai/paperclip"
#define MyAppExeName "paperclip.exe"

[Setup]
AppId={{PAPERCLIP-12345678-1234-1234-1234-123456789012}
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
SetupIconFile=..\exe\assets\paperclip.ico
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
; No admin required - installs to %LOCALAPPDATA%
; This reduces SmartScreen warnings vs admin installers

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "addtopath"; Description: "Add Paperclip to PATH"; GroupDescription: "Other:"

[Files]
; Zig launcher - tiny native exe, NOT PyInstaller, far fewer AV false positives
Source: "..\exe\dist\paperclip.exe"; DestDir: "{app}"; Flags: ignoreversion
; Alternative Python launcher (onedir) - also low AV risk vs onefile
; Source: "..\exe\dist\paperclip\paperclip.exe"; DestDir: "{app}"; Flags: ignoreversion
; Source: "..\exe\dist\paperclip\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Payload - the real app
Source: "..\exe\build\payload\*"; DestDir: "{app}\payload"; Flags: ignoreversion recursesubdirs createallsubdirs
; Batch wrapper for double-click
Source: "paperclip.bat"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "doctor"; WorkingDir: "{app}"; IconFilename: "{app}\payload\assets\server\ui-dist\favicon.ico"; Flags: createonlyiffileexists
Name: "{group}\Paperclip Onboard"; Filename: "{app}\{#MyAppExeName}"; Parameters: "onboard"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "doctor"; Tasks: desktopicon; WorkingDir: "{app}"; IconFilename: "{app}\payload\assets\server\ui-dist\favicon.ico"; Flags: createonlyiffileexists

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "doctor"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#MyAppExeName}"; Parameters: "--launcher-doctor"; Description: "Run launcher diagnostics"; Flags: nowait postinstall skipifsilent unchecked

[UninstallDelete]
Type: filesandordirs; Name: "{app}\payload"

[Code]
function IsNodeInstalled(): Boolean;
var
  ResultCode: Integer;
begin
  // Check if node is available
  Result := Exec('node', '--version', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
  if not Result then
    Result := Exec('node.exe', '--version', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function InitializeSetup(): Boolean;
begin
  if not IsNodeInstalled then
  begin
    if MsgBox('Node.js >= 24.11.0 is required but not found in PATH.' + #13#10 + #13#10 +
              'Paperclip needs Node.js to run. Install it from https://nodejs.org/ and then run this installer again.' + #13#10 + #13#10 +
              'Do you want to open https://nodejs.org/ now?', mbConfirmation, MB_YESNO) = IDYES then
    begin
      ShellExec('open', 'https://nodejs.org/', '', '', SW_SHOWNORMAL, ewNoWait, ResultCode);
    end;
    Result := False;
    Exit;
  end;
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Path: string;
begin
  if (CurStep = ssPostInstall) and IsTaskSelected('addtopath') then
  begin
    Path := ExpandConstant('{app}');
    // Add to user PATH
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
