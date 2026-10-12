; WinVanish Installer Script (Inno Setup)
; by Kotsch.Tech - https://kotsch.tech
;
; Upgrade-fest: dieser Installer kann ueber JEDEN Zustand installiert werden -
;   - aelteres WinVanish installiert (gleiche AppId -> Update an gleicher Stelle)
;   - aeltere Version laeuft gerade (wird vorher beendet, danach neu gestartet)
;   - Reste einer frueheren/halb deinstallierten Version ohne Uninstall-Eintrag
;   - Dateien aus aelteren Versionen (werden aufgeraeumt)
; Die persoenliche config.json (Taste, Ziele) und das Log bleiben dabei erhalten.

#define MyAppName "WinVanish"
#define MyAppVersion "1.3.0"
#define MyAppPublisher "Kotsch.Tech"
#define MyAppURL "https://kotsch.tech"
#define MyAppExeName "WinVanish.exe"

[Setup]
; Diese AppId NIEMALS aendern - sonst erkennt ein neuer Installer eine
; vorhandene Installation nicht mehr als dieselbe App und installiert daneben.
AppId={{6C6F0A3B-2E52-4B3F-9A61-3E9B7E9F5F2E}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Bei einem Update immer den bisherigen Installationsordner und die bisher
; gewaehlten Optionen (Desktop-Symbol, Autostart) wiederverwenden.
UsePreviousAppDir=yes
UsePreviousTasks=yes
LicenseFile=..\LICENSE
OutputDir=Output
OutputBaseFilename=WinVanish-Setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} by {#MyAppPublisher}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Setup
VersionInfoCopyright=(c) Kotsch.Tech - {#MyAppURL}
; Laufende WinVanish.exe wird vor dem Ueberschreiben beendet (zusaetzlich zur
; Absicherung in [Code]). Der Neustart danach erfolgt selbst (siehe [Run]),
; damit er auch bei /SILENT-Installationen und ohne Restart Manager klappt.
CloseApplications=force
RestartApplications=no

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "WinVanish automatisch mit Windows starten (empfohlen)"; GroupDescription: "Optionen:"

[InstallDelete]
; Dateien frueherer Versionen, die es heute nicht mehr gibt (Icons stecken seit
; 1.0.x in der exe, README liegt als .md vor).
Type: files; Name: "{app}\wv_hell.png"
Type: files; Name: "{app}\wv_dunkel.png"
Type: files; Name: "{app}\icon.png"
Type: files; Name: "{app}\README.txt"

[Files]
; WinVanish.exe traegt Icons (hell/dunkel) und Version-Info bereits eingebettet -
; im installierten Ordner landet also nur eine einzige exe plus Doku.
; ignoreversion: immer ueberschreiben, auch wenn die vorhandene Datei "neuer" aussieht.
; config.json und winvanish.log werden bewusst NICHT mitinstalliert (bleiben erhalten).
Source: "..\dist\WinVanish.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\SECURITY.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{group}\WinVanish auf kotsch.tech"; Filename: "{#MyAppURL}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "WinVanish"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
; Frische Installation (WinVanish lief vorher nicht), normaler Assistent:
; Haekchen "WinVanish starten".
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent; Check: not ShouldAutoLaunch
; Update einer laufenden Version oder stille Installation: automatisch neu starten.
Filename: "{app}\{#MyAppExeName}"; Flags: nowait; Check: ShouldAutoLaunch

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#MyAppExeName} /T"; Flags: runhidden skipifdoesntexist; RunOnceId: "KillWinVanish"

[UninstallDelete]
; Alles im Installationsordner entfernen (config.json, Log, Reste), damit nach
; einer Deinstallation nichts liegen bleibt, was eine spaetere Installation stoert.
Type: filesandordirs; Name: "{app}"

[Code]
var
  WasRunning: Boolean;

function IsWinVanishRunning: Boolean;
var
  ResultCode: Integer;
begin
  // find liefert 0, wenn die exe in der Prozessliste steht.
  Result := Exec(ExpandConstant('{cmd}'),
    '/C tasklist /FI "IMAGENAME eq {#MyAppExeName}" /NH | find /I "{#MyAppExeName}" >nul',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

procedure KillRunningWinVanish;
var
  ResultCode, I: Integer;
begin
  // Eine laufende WinVanish.exe (egal aus welcher Installation/welchem Ordner)
  // IMMER beenden, bevor Dateien kopiert werden - sonst kann eine alte Version
  // unbemerkt weiterlaufen und auf Tastendruecke reagieren, obwohl "aktualisiert"
  // wurde. Danach kurz warten, bis Windows die Datei wirklich freigegeben hat.
  Exec('taskkill.exe', '/F /IM {#MyAppExeName} /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  for I := 1 to 20 do
  begin
    if not IsWinVanishRunning then
      Break;
    Sleep(250);
  end;
end;

function ShouldAutoLaunch: Boolean;
begin
  // Update einer laufenden Version -> danach automatisch wieder starten.
  // Stille Installation (/SILENT, /VERYSILENT) -> ebenfalls starten.
  Result := WasRunning or WizardSilent;
end;

function InitializeSetup(): Boolean;
begin
  WasRunning := IsWinVanishRunning;
  KillRunningWinVanish;
  Result := True;
end;
