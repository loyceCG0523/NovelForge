#ifndef MyAppVersion
  #define MyAppVersion "0.2.6"
#endif
#ifndef MyAppId
  #define MyAppId "{{0E2E915A-9CF2-49B2-88A9-95D9B9349127}"
#endif
#ifndef MyOutputBaseFilename
  #define MyOutputBaseFilename "NovelForge-Windows-x64-Setup"
#endif
#ifndef MyOutputDir
  #define MyOutputDir "..\release"
#endif
#ifndef MyCompression
  #define MyCompression "lzma2/ultra64"
#endif
#ifndef MySolidCompression
  #define MySolidCompression "yes"
#endif

#define MyAppName "NovelForge"
#define MyAppPublisher "NovelForge"
#define MyAppExeName "NovelForge.exe"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#MyOutputDir}
OutputBaseFilename={#MyOutputBaseFilename}
SetupIconFile=..\assets\novelforge.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression={#MyCompression}
SolidCompression={#MySolidCompression}
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
AppMutex=NovelForge.Windows.App
VersionInfoVersion={#MyAppVersion}.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription=NovelForge Windows x64 Installer
VersionInfoProductName=NovelForge Windows
VersionInfoProductVersion={#MyAppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\NovelForge\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: files; Name: "{app}\_internal\icuuc.dll"
Type: files; Name: "{app}\_internal\icudt58.dll"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\{#MyAppName}（兼容渲染）"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--safe-rendering"; WorkingDir: "{app}"; Comment: "显卡驱动或远程桌面显示异常时使用软件渲染"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
