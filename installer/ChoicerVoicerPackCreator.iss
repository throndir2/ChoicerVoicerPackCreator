; Inno Setup script for the per-user Windows installer.
; Build it with Build-Installer.ps1, which passes AppVersion, FileVersion, SourceDir, and OutputDir.
;
; The installed editor updates itself by downloading a newer Setup EXE from GitHub and running:
;   Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART "/DIR=<app>" "/LOG=<file>"
;             /WAITPID=<editor pid> "/UPDATEDIR=<staging>" [/RELAUNCH=1 "/PROJECT=<path>"]

#ifndef AppVersion
  #error Define AppVersion, for example /DAppVersion=1.4.0
#endif
#ifndef FileVersion
  #error Define FileVersion, for example /DFileVersion=1.4.0.0
#endif
#ifndef SourceDir
  #error Define SourceDir as the validated portable application folder
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif

#define AppName "Choicer Voicer Pack Creator"
#define AppExe "Choicer Voicer Pack Creator.exe"
#define RunningMutex "ChoicerVoicerPackCreatorRunning"
#define RepoUrl "https://github.com/throndir2/ChoicerVoicerPackCreator"

[Setup]
AppId={{AC8C0BB3-2E02-4E7D-B31F-3B96039CF546}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Choicer Voicer Community
AppPublisherURL={#RepoUrl}
AppSupportURL={#RepoUrl}/issues
AppUpdatesURL={#RepoUrl}/releases
VersionInfoVersion={#FileVersion}
VersionInfoProductVersion={#FileVersion}
VersionInfoProductTextVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=Choicer-Voicer-Pack-Creator-{#AppVersion}-Setup
Compression=lzma2/fast
SolidCompression=yes
LZMANumBlockThreads=4
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "setup-install.json"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\_internal.previous"

[Code]
const
  SYNCHRONIZE = $00100000;
  WAIT_OBJECT_0 = 0;

function OpenProcess(DesiredAccess: Longword; InheritHandle: Longword; ProcessId: Longword): Longword;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(Handle: Longword; Milliseconds: Longword): Longword;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(Handle: Longword): Longword;
  external 'CloseHandle@kernel32.dll stdcall';

var
  UpdateMode: Boolean;
  EditorExited: Boolean;
  MovedInternal: Boolean;
  Installed: Boolean;

function WaitForEditor(): Boolean;
var
  ProcessId: Longword;
  Handle: Longword;
begin
  Result := True;
  ProcessId := StrToIntDef(ExpandConstant('{param:WAITPID|0}'), 0);
  if ProcessId = 0 then
    Exit;
  Handle := OpenProcess(SYNCHRONIZE, 0, ProcessId);
  if Handle <> 0 then
  begin
    Log('Waiting for the editor process to exit.');
    Result := WaitForSingleObject(Handle, 120000) = WAIT_OBJECT_0;
    CloseHandle(Handle);
  end;
  if Result then
    EditorExited := True
  else
    Log('The editor did not exit in time. The update was not installed.');
end;

function InitializeSetup(): Boolean;
var
  Waited: Integer;
begin
  UpdateMode := ExpandConstant('{param:UPDATEDIR}') <> '';
  Result := WaitForEditor();
  Waited := 0;
  while Result and CheckForMutexes('{#RunningMutex}') do
  begin
    if UpdateMode then
    begin
      if Waited >= 60 then
      begin
        Log('Another copy of the editor is still running. The update was not installed.');
        Result := False;
      end
      else
      begin
        Sleep(1000);
        Waited := Waited + 1;
      end;
    end
    else if SuppressibleMsgBox('Close {#AppName} before you continue, then click OK.',
      mbError, MB_OKCANCEL, IDCANCEL) <> IDOK then
      Result := False;
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  while Result and CheckForMutexes('{#RunningMutex}') do
    if SuppressibleMsgBox('Close {#AppName} before you continue, then click OK.',
      mbError, MB_OKCANCEL, IDCANCEL) <> IDOK then
      Result := False;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Internal, Previous: String;
begin
  Internal := ExpandConstant('{app}\_internal');
  Previous := ExpandConstant('{app}\_internal.previous');
  if CurStep = ssInstall then
  begin
    // Move the old runtime aside so obsolete files from older versions do not remain.
    if DirExists(Previous) then
      DelTree(Previous, True, True, True);
    if DirExists(Internal) then
    begin
      MovedInternal := RenameFile(Internal, Previous);
      if not MovedInternal then
        Log('Could not move the previous runtime folder; files are replaced in place.');
    end;
  end
  else if CurStep = ssPostInstall then
  begin
    Installed := True;
    if MovedInternal then
      DelTree(Previous, True, True, True);
  end;
end;

procedure DeinitializeSetup();
var
  Internal, Previous, Directory, Parameters, Project: String;
  ResultCode: Integer;
begin
  if MovedInternal and not Installed then
  begin
    Internal := ExpandConstant('{app}\_internal');
    Previous := ExpandConstant('{app}\_internal.previous');
    DelTree(Internal, True, True, True);
    if not RenameFile(Previous, Internal) then
      Log('Could not restore the previous runtime folder: ' + Previous);
  end;
  if UpdateMode and EditorExited and (ExpandConstant('{param:RELAUNCH|0}') = '1') then
  begin
    Directory := RemoveBackslashUnlessRoot(ExpandConstant('{param:DIR}'));
    Parameters := '"--update-result=' + ExpandConstant('{param:UPDATEDIR}') + '"';
    Project := ExpandConstant('{param:PROJECT}');
    if Project <> '' then
      Parameters := Parameters + ' "' + Project + '"';
    if not Exec(AddBackslash(Directory) + '{#AppExe}', Parameters, Directory,
      SW_SHOWNORMAL, ewNoWait, ResultCode) then
      Log('Could not restart the editor: ' + SysErrorMessage(ResultCode));
  end;
end;
