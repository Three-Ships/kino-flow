; Kino Flow 1.0 — Windows installer (Inno Setup)
; Build via build.ps1 (preps config + ffmpeg + icon, then invokes ISCC).
; Per-user install to LocalAppData so first-run venv creation needs no admin.

#define AppName "Kino Flow 1.0"
#define AppVer  "1.0.0"
#define AppPub  "Three Ships"

[Setup]
AppId={{7C4C7C2E-3E2A-4B9F-9E1D-KINOFLOW0001}
AppName={#AppName}
AppVersion={#AppVer}
AppPublisher={#AppPub}
DefaultDirName={localappdata}\Kino Flow
DefaultGroupName=Kino Flow
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=output
OutputBaseFilename=Kino-Flow-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=kino.ico

[Files]
; App payload — source trees minus envs/caches/vendored git.
Source: "..\..\studio\*";    DestDir: "{app}\studio";    Flags: recursesubdirs createallsubdirs; \
  Excludes: ".venv\*,__pycache__\*,*.pyc,*.egg-info\*"
Source: "..\..\video-use\*"; DestDir: "{app}\video-use"; Flags: recursesubdirs createallsubdirs; \
  Excludes: ".venv\*,.venv_disabled\*,__pycache__\*,*.pyc,.git\*,.git_disabled\*,node_modules\*"
; Launcher + config + entry script (config.json prepared by build.ps1).
Source: "..\launch.py";      DestDir: "{app}"
Source: "start-kino.cmd";    DestDir: "{app}"
Source: "kino.config.json";  DestDir: "{app}"
; Bundled ffmpeg (build.ps1 stages ffmpeg\bin\{ffmpeg,ffprobe}.exe here).
Source: "ffmpeg\bin\*";      DestDir: "{app}\ffmpeg\bin"; Flags: recursesubdirs
Source: "kino.ico";          DestDir: "{app}"

[Icons]
Name: "{group}\Kino Flow";           Filename: "{app}\start-kino.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\kino.ico"
Name: "{userdesktop}\Kino Flow";     Filename: "{app}\start-kino.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\kino.ico"; Tasks: desktopicon
Name: "{group}\Uninstall Kino Flow"; Filename: "{uninstallexe}"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\start-kino.cmd"; Description: "Launch Kino Flow now"; Flags: postinstall shellexec skipifsilent
