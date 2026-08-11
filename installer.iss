; Inno Setup script for EXR -> sRGB.
;
; Per-user by design. Everything this app touches is already per-user - the .exr
; association and the convert verbs live under HKCU\Software\Classes - so a
; machine-wide install would need elevation to place files and then still have
; to drop back to the signed-in user to register anything. Installing to
; %LOCALAPPDATA%\Programs means no UAC prompt at all, and the app still appears
; in Add/Remove Programs like any other.
;
; The installed exe deliberately has a stable name at a stable path. Windows
; records that path in the association, so a filename carrying the version
; breaks the double-click on every upgrade - which is exactly what happened in
; 3.0.4. The version lives in the installer's filename, the uninstall entry and
; the file properties instead.
;
; Build:  python build_installer.py
; That reads VERSION out of exr2srgb.py and passes it in as /DAppVersion, so the
; version lives in exactly one place. Building this file directly still works
; and falls back to 0.0.0.

#define AppName        "EXR to sRGB"
#define AppPublisher   "VISOR"
#define AppURL         "https://github.com/visorooo/EXRtoSRGB"
#define ExeName        "EXRtoSRGB.exe"

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8B5E1C24-7A93-4F1D-9C2E-2F5A6D3B4E71}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
VersionInfoVersion={#AppVersion}

; No admin, no UAC. See the header.
PrivilegesRequired=lowest
DefaultDirName={autopf}\EXRtoSRGB
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
AllowNoIcons=yes

OutputDir=.
OutputBaseFilename=EXRtoSRGB_Setup_v{#AppVersion}
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#ExeName}
UninstallDisplayName={#AppName}

Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The payload is a 37 MB one-file build; it is already compressed inside, so
; there is little left to win and a lot of time to lose recompressing hard.
LZMANumBlockThreads=4

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; \
    GroupDescription: "Shortcuts:"; Flags: unchecked

; Ticked by default: someone installing an EXR viewer almost certainly wants
; double-click to work, and it is one click to turn off here and another to
; change later under the cogwheel. It stays a choice rather than a silent
; takeover of a file type.
Name: "assoc"; Description: "Open .exr files with {#AppName}"; \
    GroupDescription: "File association:"
Name: "context"; Description: "Add ""Convert to sRGB"" to the right-click menu"; \
    GroupDescription: "File association:"

[Files]
Source: "EXRtoSRGB.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "docs\sample_render.exr"; DestDir: "{app}\sample"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#ExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon

[Run]
; The app owns the registration, so the installer calls it rather than writing
; a second copy of those registry keys in Pascal that then drifts from the
; Python one. Nothing here is elevated, so it lands in the right HKCU hive.
Filename: "{app}\{#ExeName}"; Parameters: "--register assoc"; \
    StatusMsg: "Registering .exr..."; Flags: runhidden waituntilterminated; \
    Tasks: assoc

; Its own entry, gated on its own task. Until 3.1 both registrations rode on
; the assoc checkbox, so ticking only the right-click menu did nothing at all
; and ticking only the association installed the menu as well.
Filename: "{app}\{#ExeName}"; Parameters: "--register context"; \
    StatusMsg: "Adding the right-click menu..."; \
    Flags: runhidden waituntilterminated; Tasks: context

; Registering is not the same as becoming the default, and on any machine with
; Photoshop, After Effects or another EXR viewer on it the registration above
; is outranked and completely inert. Since Windows 8 the default handler is
; `UserChoice`, whose hash is signed per user - no installer can write it, and
; a forged one makes Windows drop the association altogether. The only way is
; Windows' own "How do you want to open this?" dialog, so it is shown once,
; here, while the user is already thinking about it. Not runhidden: it IS a
; window. Skipped silently if .exr already resolves to us.
Filename: "{app}\{#ExeName}"; Parameters: "--choose-default"; \
    StatusMsg: "Setting EXR to sRGB as the default for .exr..."; \
    Flags: waituntilterminated; Tasks: assoc

Filename: "{app}\{#ExeName}"; Description: "Launch {#AppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; Undo the shell integration before the exe is deleted - afterwards there is
; nothing left to run, and the association would point at a file that is gone.
Filename: "{app}\{#ExeName}"; Parameters: "--unregister"; \
    Flags: runhidden waituntilterminated; RunOnceId: "UnregisterExr"

[UninstallDelete]
; The document icon is copied out of the exe to a stable location at register
; time, because a one-file build unpacks to a temp folder that is deleted on
; exit - an icon pointed there goes blank the moment the app closes.
Type: filesandordirs; Name: "{localappdata}\EXRtoSRGB"
