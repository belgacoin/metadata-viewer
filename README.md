# Metadata Viewer

A graphical viewer, wiper and editor for file metadata. Shows EXIF, XMP, IPTC,
ID3, RIFF, C2PA and Office document properties, flags AI provenance traces and
privacy-sensitive fields, and lets you change or remove them.

## Features

- **Read** anything exiftool understands, grouped and searchable, plus a raw
  byte scan that catches C2PA/JUMBF containers exiftool only reports as unknown
  chunks.
- **Flag** AI provenance (Suno, Udio, ElevenLabs, Gemini, OpenAI, Midjourney,
  Stable Diffusion and others) in red, and privacy fields (GPS, artist, owner,
  camera serial) in orange.
- **Wipe** all metadata, or only the AI and C2PA traces while keeping the rest.
- **Edit** individual tags, add new ones, delete others. Nothing touches disk
  until you save, and saving writes to a copy by default.
- **Deep inspection** detects hidden structure and steganographic traces:
  C2PA / Content Credentials manifests, embedded file signatures, trailing data,
  LSB anomalies in lossless images, invisible Unicode characters, whitespace
  channels and encoded payloads.

Audio streams stay bit-identical through both wiping and editing: nothing is
re-encoded. The app asks exiftool which formats it can actually write, so
editing is never offered where it would fail at save time.

## Format support

| Format | Read | Wipe | Edit |
| --- | --- | --- | --- |
| PNG, JPEG, HEIC, TIFF, WebP, GIF, BMP | yes | yes | any tag |
| PDF | yes | yes | any tag |
| MP3 | yes | built-in ID3 code | 16 tags |
| WAV | yes | built-in RIFF code | 12 INFO tags |
| M4A, MP4, AAC | yes | ffmpeg | 13 tags |
| FLAC, OGG, Opus | yes | ffmpeg | 11 tags |
| MOV | yes | ffmpeg | any tag |
| DOCX, XLSX, PPTX | yes | yes | 15 properties |
| ODT, ODS, ODP, ODG | yes | yes | 9 properties |
| SVG, HTML, Markdown, text | yes | yes | no |
| AIFF, WMA, MKV, WebM, AVI | yes | ffmpeg | no |

Reading works more broadly than this table suggests — exiftool recognises
hundreds of formats, and anything it can read shows up in the tree. The table
is about writing.

exiftool cannot write MP3, WAV, DOCX or ODT, so those have their own writers:
ID3v2/v1/APE parsing for MP3, RIFF chunk rewriting for WAV, and XML patching
inside the zip package for Office and OpenDocument files.

## Running from source

```bash
python3 metadata_viewer.py [file]
```

The helper file `deep_inspection.py` must live next to `metadata_viewer.py`;
it has no extra dependencies.

Requires Python 3.10+ with tkinter. Optional but recommended: `exiftool`
(reading and writing image formats), `ffmpeg` (audio and video other than
MP3/WAV), `mutagen` (audio tags), `pillow` (image preview).

## Installing the ready-made app

### macOS

1. Download the latest `Metadata Viewer X.Y.dmg` from the GitHub Actions
   artifacts (or from a release, if published).
2. Open the `.dmg` and drag `Metadata Viewer.app` to **Applications**.
3. On first launch, right-click the app → **Open** to bypass Gatekeeper,
   because the build is not signed.

### Windows

1. Download the latest `Metadata Viewer.exe` from the GitHub Actions artifacts.
2. Run it directly; a single-file executable is produced.
3. Windows SmartScreen may warn because the build is not signed.

## Building for macOS

```bash
./build_macos.sh
```

Produces `dist/Metadata Viewer.app` and a `.dmg`. exiftool is bundled — it is a
Perl script and macOS ships its own perl, so the app runs on a Mac without
Homebrew. ffmpeg is not bundled (too large, and its licence depends on the
build); without it MP3, WAV, images and documents work fine, but M4A, FLAC, OGG
and video do not.

## Building for Windows

A `.exe` **cannot** be built from macOS or Linux: PyInstaller does not
cross-compile. Two options.

**On a Windows machine:**

```powershell
.\build_windows.ps1
```

Fetches the current Windows build of exiftool, bundles it, and produces a
single-file `dist\Metadata Viewer.exe`. Pass `-SkipExifTool` to build without
it, in which case the app looks for exiftool on PATH at runtime.

**Without a Windows machine**, via GitHub Actions: run the `Build` workflow
manually (or push a `v*` tag). The `.exe` and the `.dmg` appear as artifacts
under that run.

## What the deep inspection panel looks for

The deep inspection module searches the file bytes for traces that normal
metadata tools do not show:

- **C2PA / Content Credentials**: JUMBF superboxes and CBOR assertion stores,
  including source type (`trainedAlgorithmicMedia`, `compositeWith...`),
  software agents and AI-model statements.
- **Embedded file signatures**: known file magic numbers (ZIP, RAR, PDF, ELF, EXE,
  PNG, JPEG, MP4, WAV and more) hidden inside other files. Internal ZIP entries
  in Office/ODF documents are treated as structure, not as hidden files.
- **Trailing data**: bytes appended after the natural end of PNG, JPEG, GIF,
  PDF and WAV containers, with a text preview and entropy estimate.
- **LSB analysis**: per-colour-channel least-significant-bit inspection of
  lossless images (PNG, BMP, TIFF, WebP, GIF), looking for readable text runs
  and chi-square balance anomalies.
- **Invisible Unicode**: zero-width spaces/joiners, BiDi controls, variation
  selectors and language tag characters.
- **Whitespace channels**: trailing spaces or tabs at line endings, mixed
  indentation and unusual line endings that can carry steganographic payloads.
- **Encoded payloads**: blocks that look like Base64, hex or percent-encoded
  strings.

## Watermarks and detection limits

The app detects **metadata and structural traces** of AI-generated content:
C2PA manifests, tool claims in XMP, and known generator signatures.

It does **not** detect provider-specific **statistical** watermarks embedded in
the content itself — Claude layer-2 / Anthropic watermarks, SynthID-Text,
OpenAI text watermarks, SynthID image/audio watermarks, Suno's audio watermark,
etc. Those require the provider's secret detection key and/or model access and
survive any metadata strip. This tool reports what it can see in the file rather
than claiming a file is "clean".

## Signing

Neither build is signed. macOS shows a Gatekeeper warning on first launch;
right-click → Open gets past it. Windows SmartScreen does something similar. A
developer certificate solves this but is not free.

## Notes

The app is read-only until you explicitly wipe or save. Wiping and saving write
to a `.cleaned` copy by default; overwriting the original is behind a separate
confirmation and always leaves a `.bak` alongside.

Metadata is only what sits *in* the file. Invisible watermarks embedded in the
pixels or the audio signal itself — SynthID, Suno's audio watermark and similar
— survive any metadata strip and are not visible to this or any other metadata
tool.

The interface is in Dutch.
