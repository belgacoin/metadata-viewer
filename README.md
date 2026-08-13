# Metadata Viewer

Grafische viewer, wisser en editor voor bestandsmetadata. Toont EXIF, XMP, IPTC,
ID3, RIFF, C2PA en Office-eigenschappen, markeert AI-herkomst en privacygevoelige
velden, en kan die aanpassen of verwijderen.

## Draaien vanaf de broncode

```bash
python3 metadata_viewer.py [bestand]
```

Nodig: Python 3.10+ met tkinter. Optioneel maar aanbevolen: `exiftool` (lezen en
schrijven van beeldformaten), `ffmpeg` (audio en video anders dan MP3/WAV),
`mutagen` (audiotags), `pillow` (voorbeeldweergave).

## macOS bouwen

```bash
./build_macos.sh
```

Levert `dist/Metadata Viewer.app` en een `.dmg`. exiftool wordt meeverpakt — het
is een Perl-script en macOS levert zelf perl mee, dus de app werkt op een Mac
zonder Homebrew. ffmpeg zit er niet in (te groot, en de licentie hangt af van de
build); zonder ffmpeg werken MP3, WAV, afbeeldingen en documenten gewoon, maar
M4A, FLAC, OGG en video niet.

## Windows bouwen

Een `.exe` kan **niet** vanaf macOS of Linux gemaakt worden: PyInstaller
cross-compileert niet. Er zijn twee wegen.

**Op een Windows-machine:**

```powershell
.\build_windows.ps1
```

Haalt de actuele exiftool voor Windows op, verpakt die mee en levert
`dist\Metadata Viewer.exe` als één bestand. Met `-SkipExifTool` bouwt hij zonder,
en zoekt de app exiftool op het PATH.

**Zonder Windows-machine**, via GitHub Actions: push deze map naar een
repository en start de workflow `Build` handmatig (of push een `v*`-tag). De
`.exe` en de `.dmg` verschijnen als artefacten onder die run.

## Ondertekening

Beide builds zijn niet ondertekend. macOS toont bij de eerste start een
waarschuwing van Gatekeeper; openen via rechtsklik → Openen omzeilt die.
Windows SmartScreen doet iets vergelijkbaars. Een ontwikkelaarscertificaat lost
dat op maar is niet gratis.
