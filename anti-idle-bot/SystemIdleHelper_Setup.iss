; Unified Inno Setup Script for System Idle Helper
#define MyProgName "System Idle Helper"
#define MyProgVersion "1.6"
#define MyProgPublisher "System Management"
#define MyProgExeName "SystemIdleHelper.exe"

[Setup]
AppId={{C1F2B3A4-D5E6-4F70-B8C9-D0E1F2A3B4C5}
AppName={#MyProgName}
AppVersion={#MyProgVersion}
AppPublisher={#MyProgPublisher}
; Install to Program Files by default
DefaultDirName={autopf}\{#MyProgName}
DefaultGroupName={#MyProgName}
DisableProgramGroupPage=yes
; Ask for admin rights only if needed for {autopf}
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
OutputBaseFilename=SystemIdleHelper_Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
OutputDir=Output

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"

[Files]
Source: "dist\{#MyProgExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyProgName}"; Filename: "{app}\{#MyProgExeName}"
Name: "{userstartup}\{#MyProgName}"; Filename: "{app}\{#MyProgExeName}"

[Registry]
; Standard Auto-start for the current user (Backup to startup folder)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "SystemIdleHelper"; ValueData: """{app}\{#MyProgExeName}"""; Flags: uninsdeletevalue

[Run]
; Launch the program automatically after installation (with checkbox)
Filename: "{app}\{#MyProgExeName}"; Description: "{cm:LaunchProgram,System Idle Helper}"; Flags: nowait postinstall skipifsilent
