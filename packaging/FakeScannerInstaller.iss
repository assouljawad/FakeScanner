; Inno Setup installer script for FakeScanner.
; Build after PyInstaller has created dist\FakeScanner\*.

#define AppName "FakeScanner"
#define AppVersion "1.0.0"
#define AppPublisher "OpenAI Demo Tools"
#define AppExeName "FakeScanner.exe"

[Setup]
AppId={{A6AECC42-8B54-4AC6-91C2-64A70597E518}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=dist\installer
OutputBaseFilename=FakeScannerSetup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
Source: "dist\FakeScanner\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch FakeScanner"; Flags: nowait postinstall skipifsilent
