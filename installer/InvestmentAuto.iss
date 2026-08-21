; Investment Auto 2.0 desktop installer (per-user, no admin, no PowerShell at runtime)
; Build: ISCC.exe installer\InvestmentAuto.iss

#define MyAppName "Investment Auto"
#define MyAppVersion "2.0.0"
#define MyAppExeName "InvestmentAuto.Desktop.exe"
#define MyAppPublisher "Investment Auto Contributors"

[Setup]
AppId={{B4E0A1F6-7C3E-4A8B-9D12-5F6E7A8B9C0D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\InvestmentAuto
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\release
OutputBaseFilename=InvestmentAuto-Setup-x64
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\windows\desktop\InvestmentAuto.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
; User data lives OUTSIDE {app}, so upgrades never touch it.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Files]
; desktop shell (dotnet publish output)
Source: "..\windows\desktop\bin\Release\net8.0-windows\win-x64\publish\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs
; bundled relocatable Python (with offline-installed dependencies)
Source: "..\build\runtime\python\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs
; portable Node.js 22 (flattened by bundle-runtime.ps1)
Source: "..\build\runtime\node\*"; DestDir: "{app}\node"; Flags: ignoreversion recursesubdirs
; investment engine (Python)
Source: "..\engine\*"; DestDir: "{app}\engine"; Flags: ignoreversion recursesubdirs
; DSH application shell (profiles, presets, skills, plugins, pinned node_modules)
Source: "..\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs
Source: "..\scripts\*"; DestDir: "{app}\scripts"; Flags: ignoreversion recursesubdirs
Source: "..\config\config.yaml"; DestDir: "{app}\config"; Flags: ignoreversion
Source: "..\config\market\*"; DestDir: "{app}\config\market"; Flags: ignoreversion recursesubdirs
Source: "..\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\requirements-lock.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
; WebView2 Evergreen bootstrapper (only installs when the runtime is missing)
Source: "..\build\runtime-downloads\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{app}\redist"; Flags: ignoreversion

[Dirs]
Name: "{localappdata}\InvestmentAuto"
Name: "{localappdata}\InvestmentAuto\runtime"
Name: "{localappdata}\InvestmentAuto\config"

[Icons]
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--app-root ""{app}"" --data-root ""{localappdata}\InvestmentAuto"""; IconFilename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--app-root ""{app}"" --data-root ""{localappdata}\InvestmentAuto"""; IconFilename: "{app}\{#MyAppExeName}"

[Run]
; WebView2 runtime (self-detecting bootstrapper; hidden)
Filename: "{app}\redist\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; Flags: runhidden skipifdoesntexist; StatusMsg: "确保 WebView2 运行时可用..."
; launch the app after install (checked by default)
Filename: "{app}\{#MyAppExeName}"; Parameters: "--app-root ""{app}"" --data-root ""{localappdata}\InvestmentAuto"""; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\node"

[Code]
// Ask whether to keep user data on uninstall (default: keep).
// When the user chooses NOT to keep it, actually delete the data directory
// after the uninstall finishes.
var
  RemoveUserData: Boolean;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    // Silent uninstall never shows a dialog and always keeps data.
    // Interactive: Yes = keep data (default); No = delete it.
    if UninstallSilent() then
      RemoveUserData := False
    else
      RemoveUserData := MsgBox('是否保留用户数据（模拟账户、报告、API 配置等）？' + #13#10 +
        '数据保存在 %LocalAppData%\InvestmentAuto。', mbConfirmation, MB_YESNO or MB_DEFBUTTON1) = IDNO;
  end;

  if CurUninstallStep = usPostUninstall then
  begin
    if RemoveUserData and DirExists(ExpandConstant('{localappdata}\InvestmentAuto')) then
      DelTree(ExpandConstant('{localappdata}\InvestmentAuto'), True, True, True);
  end;
end;
