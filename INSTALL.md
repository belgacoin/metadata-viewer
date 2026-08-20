# Installatiehandleiding

Deze handleiding legt stap voor stap uit hoe je Metadata Viewer installeert en
gebruikt. Er zijn twee manieren:

- **Makkelijkste (aanbevolen):** download het kant-en-klare programma. Je hoeft
  geen code te zien of te installeren.
- **Voor wie wil kijken/leren:** download de broncode en draai die met Python.

Kies de manier die bij je past.

---

## Manier 1 — Het programma installeren (makkelijkst)

Je downloadt gewoon het kant-en-klare programma. Geen code, geen installatie.

### Windows

1. Open in je browser: **https://github.com/belgacoin/metadata-viewer/releases**
2. Je ziet de nieuwste versie met twee bestanden eronder:
   - `Metadata.Viewer.exe` ← **dit heb jij nodig** (Windows)
   - `Metadata.Viewer.1.0.dmg` ← dit is voor Mac, negeer je
3. Klik op `Metadata.Viewer.exe` om het te downloaden. Het staat dan in je map
   **Downloads**.
4. Dubbelklik op het bestand in Downloads. Het programma start.

**De waarschuwing die je misschien krijgt:** omdat het programma niet officieel
ondertekend is, toont Windows soms een blauw scherm "Windows heeft uw pc
beveiligd" (SmartScreen). Dat is normaal en veilig. Zo kom je erlangs:

1. Klik op **"Meer info"** (of "More info") onderaan dat scherm.
2. Er verschijnt dan een knop **"Toch uitvoeren"** (of "Run anyway").
3. Klik daarop → het programma start.

Dat is het. Het is een "portable" programma: je hoeft niets te installeren, je
dubbelklikt gewoon.

### macOS

1. Open in je browser: **https://github.com/belgacoin/metadata-viewer/releases**
2. Klik op `Metadata.Viewer.1.0.dmg` om het te downloaden.
3. Open het `.dmg`-bestand (dubbelklik).
4. Sleep `Metadata Viewer.app` naar de map **Programma's** (Applications).
5. Bij de eerste start: rechtsklik op de app → **Open** om de Gatekeeper-
   waarschuwing te omzeilen (de app is niet ondertekend).

---

## Manier 2 — De broncode downloaden en draaien (voor wie wil kijken/leren)

Dit is voor als je de code wilt zien of zelf wilt aanpassen.

### De code downloaden

1. Ga naar: **https://github.com/belgacoin/metadata-viewer**
2. Klik op de groene knop **"Code"** rechtsboven.
3. Klik op **"Download ZIP"**.
4. Pak het ZIP-bestand uit (rechtsklik → "Alles uitpakken"). Je krijgt een map
   `metadata-viewer-main`.

### Python installeren (eenmalig)

1. Ga naar **https://www.python.org/downloads/** en klik op de grote gele knop
   om de nieuwste versie te downloaden.
2. Open het gedownloade bestand en installeer Python.
   **Belangrijk:** vink tijdens de installatie het vakje
   **"Add Python to PATH"** aan.
3. Klik "Installeren" en wacht tot het klaar is.

### Het programma starten

1. Open **PowerShell** (klik op Start, typ "PowerShell", druk Enter).
2. Typ `cd ` (met een spatie aan het einde), sleep de map
   `metadata-viewer-main` in het PowerShell-venster, en druk Enter.
3. Typ `python metadata_viewer.py` en druk Enter.

Het programma start.

### Optioneel: meer bestandsformaten

Voor de meeste bestanden (afbeeldingen, MP3, WAV, Office-documenten) werkt het
programma meteen. Voor sommige audio- en videobestanden (M4A, FLAC, OGG, MOV,
MP4) kun je extra programma's installeren:

- **exiftool** — voor het lezen/schrijven van metadata in veel formaten
- **ffmpeg** — voor audio en video

Deze zijn optioneel. Zonder hen werkt het programma prima voor de meest
voorkomende bestanden.

---

## Problemen oplossen

| Probleem | Oplossing |
| --- | --- |
| Windows toont "Windows heeft uw pc beveiligd" | Klik op "Meer info" → "Toch uitvoeren" |
| macOS toont "kan niet worden geopend" | Rechtsklik op de app → "Open" |
| `python` wordt niet herkend in PowerShell | Python opnieuw installeren en "Add Python to PATH" aanvinken |
| Het programma start niet | Controleer dat je `metadata_viewer.py` en `deep_inspection.py` in dezelfde map hebt staan |
