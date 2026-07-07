; Inno Setup script (optional installer)
#define AppName "DuplicateFinder"
#define AppVersion "1.0.0"
#define AppPublisher "YOUR_COMPANY_NAME"
#define AppExeName "DuplicateFinder.exe"

[Setup]
AppId={{D0C9A1A5-81B9-43C4-91D3-4A5E1A0D3E12}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..
OutputBaseFilename=DuplicateFinder-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64

[Files]
Source: "..\dist\DuplicateFinder\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
