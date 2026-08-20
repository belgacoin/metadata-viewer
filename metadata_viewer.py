#!/usr/bin/env python3
"""Simple graphical metadata viewer.

Reads a file's metadata with exiftool and, when the remove-ai-marks skill is
installed, adds its AI-provenance / invisible-Unicode analysis. Read-only:
nothing on disk is ever modified.
"""

from __future__ import annotations

import functools
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import deep_inspection

APP_TITLE = "Metadata Viewer"

SKILL_SCRIPTS = Path.home() / ".claude" / "skills" / "remove-ai-marks" / "scripts"


def resource_dir() -> Path:
    """Where bundled helpers live: inside the app when frozen, else beside us."""
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) if bundled else Path(__file__).resolve().parent


def install_hint(tool: str) -> str:
    """Platform-appropriate way to get a missing helper."""
    if sys.platform == "darwin":
        return f"brew install {tool}"
    if sys.platform.startswith("win"):
        return (f"download {tool} en zet het in de map 'tools' naast de app"
                if tool == "exiftool" else f"winget install {tool}")
    return f"apt install {tool}"


# Set on every subprocess we spawn. If we ever start up with it set, we were
# launched by ourselves — refuse to open a window rather than multiply.
CHILD_MARKER = "METADATA_VIEWER_CHILD"


@functools.lru_cache(maxsize=1)
def python_interpreter() -> str | None:
    """A real Python for running the skill's helper scripts.

    In a frozen app sys.executable is the app bundle itself, so using it would
    relaunch the GUI instead of running a script — once per opened file, which
    multiplies without bound.
    """
    if getattr(sys, "frozen", False):
        return shutil.which("python3") or shutil.which("python")
    return sys.executable


def child_env() -> dict[str, str]:
    """Environment for helper subprocesses, marked so they cannot recurse."""
    env = dict(os.environ)
    env[CHILD_MARKER] = "1"
    return env


@functools.lru_cache(maxsize=None)
def find_tool(name: str) -> str | None:
    """A helper binary shipped with the app, else whatever is on PATH."""
    for candidate in (resource_dir() / "tools" / name,
                      resource_dir() / "tools" / f"{name}.exe"):
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".heic"}

AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".aiff", ".aif", ".m4a", ".aac", ".ogg", ".opus", ".wma"}

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}


def media_kind(path: str) -> str:
    """Rough media class, used to pick the right caveats and analyses."""
    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    return "other"


# Warning shown per media class: what this tool structurally cannot see.
INVISIBLE_MARK_CAVEAT = {
    "image": "Let op: onzichtbare pixelwatermerken (zoals SynthID) zitten in de "
             "beeldpunten zelf. Die blijven na het wissen van metadata gewoon "
             "aanwezig en zijn hier niet zichtbaar.",
    "audio": "Let op: onhoorbare audiowatermerken zitten in het geluidssignaal zelf. "
             "Suno en vergelijkbare diensten passen die toe; ze blijven na het "
             "wissen van metadata aanwezig en zijn hier niet zichtbaar.",
    "video": "Let op: watermerken in de beeld- en geluidsdata zelf blijven na het "
             "wissen van metadata aanwezig en zijn hier niet zichtbaar.",
    "other": "Let op: deze viewer toont alleen metadata. Statistische watermerken "
             "in de inhoud zelf zijn hier niet zichtbaar.",
}

# Tags whose *name* alone signals content provenance / AI marking.
AI_KEY_PATTERNS = re.compile(
    r"c2pa|jumbf|contentcredential|provenance|digitalsourcetype|synthid",
    re.IGNORECASE,
)

# Values that give away a generator, regardless of which tag carries them.
AI_VALUE_PATTERNS = re.compile(
    r"\b("
    # text / image
    r"openai|chatgpt|dall[\s\-]?e|sora|gemini|imagen|synthid|midjourney|"
    r"stable[\s\-]?diffusion|sdxl|comfyui|automatic1111|invokeai|"
    r"firefly|anthropic|claude|grok|copilot|ideogram|recraft|nightcafe|"
    r"runwayml|runway ml|"
    # audio / music
    r"suno|udio|eleven\s?labs|musicgen|audiocraft|stable\s?audio|riffusion|"
    r"soundraw|mubert|lyria|beatoven|"
    # generic provenance wording
    r"trainedalgorithmicmedia|algorithmicmedia|ai[\s\-]generated|generative ai"
    r")\b",
    re.IGNORECASE,
)

PRIVACY_KEY_PATTERNS = re.compile(
    r"gps|geolocation|location|owner|artist|creator|author|serialnumber|"
    r"cameraserial|hostcomputer|username",
    re.IGNORECASE,
)

# Groups that only restate what the filesystem already knows.
BORING_GROUPS = {"ExifTool"}

# Groups whose tags are never metadata someone chose to embed.
DERIVED_GROUPS = {"File", "Composite", "ExifTool"}

# Format structure decoded from the image header, not embedded metadata.
STRUCTURAL_TAGS = {
    "ImageWidth", "ImageHeight", "BitDepth", "ColorType", "Compression",
    "Filter", "Interlace", "ColorComponents", "EncodingProcess",
    "YCbCrSubSampling", "ExifByteOrder", "JFIFVersion", "ResolutionUnit",
    "XResolution", "YResolution", "Orientation", "BitsPerSample",
    "PixelsPerUnitX", "PixelsPerUnitY", "PixelUnits", "SRGBRendering",
    "Gamma", "BackgroundColor", "Warning",
    # audio/video container structure
    "Encoding", "NumChannels", "SampleRate", "AvgBytesPerSec", "BitsPerSample",
    "BlockAlign", "AudioFormat", "SampleSize", "ChannelMode", "AudioBitrate",
    "AudioChannels", "AudioSampleRate", "Duration", "SampleCount", "VideoFrameRate",
    "MajorBrand", "MinorVersion", "CompatibleBrands", "MediaDataSize",
    "MediaDataOffset", "HandlerType", "MovieHeaderVersion",
    "MPEGAudioVersion", "AudioLayer", "MSStereo", "IntensityStereo",
    "CopyrightFlag", "OriginalMedia", "Emphasis", "VBRFrames", "VBRBytes",
    "VBRScale", "ID3Size",
}


def is_embedded(key: str) -> bool:
    """True when a tag is metadata someone embedded, not derived structure."""
    if key == "SourceFile":  # exiftool echoing the path back at us
        return False
    group, _, tag = key.partition(":")
    if not tag:
        group, tag = "", group
    return group not in DERIVED_GROUPS and tag not in STRUCTURAL_TAGS


# ---------------------------------------------------------------- data layer


def run_exiftool(path: str) -> tuple[dict[str, object], str | None]:
    """Return {"Group:Tag": value} plus an error string when it failed."""
    exe = find_tool("exiftool")
    if not exe:
        return {}, f"exiftool niet gevonden ({install_hint('exiftool')})"
    try:
        proc = subprocess.run(
            # -a keep duplicates, -u unknown tags, -ee dig into embedded data
            # (C2PA/JUMBF and XMP often hide there).
            [exe, "-j", "-G", "-a", "-u", "-ee", "-api", "largefilesupport=1", path],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {}, "exiftool time-out na 60s"
    if proc.returncode != 0 and not proc.stdout.strip():
        return {}, (proc.stderr.strip() or "exiftool gaf een fout").splitlines()[0]
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}, "exiftool gaf onleesbare JSON"
    return (payload[0] if payload else {}), None


def run_skill_inspect(path: str) -> dict[str, object] | None:
    """Run the remove-ai-marks inspector when that skill is installed."""
    script = SKILL_SCRIPTS / "inspect_file.py"
    if not script.exists():
        return None
    if media_kind(path) in {"audio", "video"}:
        # The skill would read raw PCM as text and report thousands of bogus
        # "suspicious Unicode" hits. It has no audio/video analysis anyway.
        return None
    python = python_interpreter()
    if not python:
        return None
    try:
        proc = subprocess.run(
            [python, str(script), "--json", path],
            capture_output=True,
            text=True,
            timeout=120,
            env=child_env(),
        )
        return json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


# Raw byte signatures, as a backstop for anything exiftool does not surface.
# Every pattern must be long or distinctive enough not to appear by chance in
# megabytes of compressed audio or image data.
RAW_MARKERS: tuple[tuple[bytes, str], ...] = (
    (b"jumbc2pa", "JUMBF-container met C2PA"),
    (b"jumdc2pa", "JUMBF-beschrijving met C2PA"),
    (b"c2pa.assertions", "C2PA-assertions"),
    (b"c2pa.claim", "C2PA-claim"),
    (b"urn:c2pa", "C2PA-identificatie"),
    (b"contentcredentials", "Content Credentials"),
    (b"<x:xmpmeta", "XMP-blok"),
    (b"trainedAlgorithmicMedia", "C2PA: gemaakt door een AI-model"),
    (b"compositeWithTrainedAlgorithmicMedia", "C2PA: deels AI-gegenereerd"),
    (b"synthid", "SynthID-verwijzing"),
    (b"suno", "Suno-verwijzing"),
    (b"udio.com", "Udio-verwijzing"),
    (b"elevenlabs", "ElevenLabs-verwijzing"),
    (b"INFOICMT", "RIFF INFO-commentaar"),
)


def scan_raw_markers(path: str, window: int = 4 * 1024 * 1024) -> list[str]:
    """Look for provenance signatures in the raw bytes.

    Reads the head *and* the tail: WAV/AIFF writers routinely append their
    LIST INFO or ID3 chunk after the audio, far beyond any head-only window.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            blob = handle.read(window)
            if size > window * 2:
                handle.seek(-window, os.SEEK_END)
                blob += handle.read(window)
            elif size > window:
                blob += handle.read()
    except OSError:
        return []
    lowered = blob.lower()
    return [label for marker, label in RAW_MARKERS if marker.lower() in lowered]


def classify(key: str, value: object) -> str:
    """Return "ai", "privacy" or "" for a single metadata row.

    Only embedded tags are classified: the file path and container structure
    are not metadata, and a folder named "claude" or "suno" must not light up
    as provenance.
    """
    if not is_embedded(key):
        return ""
    text = f"{value}"
    if AI_KEY_PATTERNS.search(key) or AI_VALUE_PATTERNS.search(text):
        return "ai"
    if PRIVACY_KEY_PATTERNS.search(key):
        return "privacy"
    return ""


def summarize_skill(report: dict[str, object] | None) -> list[str]:
    """Turn the skill's JSON report into a few human-readable lines."""
    if not report:
        return []
    lines: list[str] = []
    kind = report.get("kind")
    if kind == "text":
        total = report.get("suspicious_total", 0)
        if total:
            lines.append(f"{total} verdachte Unicode-tekens:")
            for hit in report.get("hits", [])[:8]:
                lines.append(f"  • {hit.get('label')} ×{hit.get('count')}")
        else:
            lines.append("Geen verdachte Unicode-tekens.")
    else:
        if report.get("has_c2pa"):
            lines.append("C2PA / Content Credentials aanwezig.")
        if report.get("has_ai_metadata"):
            lines.append("AI-metadata gedetecteerd.")
        for finding in report.get("findings", [])[:8]:
            lines.append(f"  • {finding}")
        if not lines:
            lines.append("Skill vond geen C2PA/AI-markering.")
    return lines


def default_target(path: str) -> str:
    """Sibling of the source with a .cleaned marker before the suffix."""
    p = Path(path)
    return str(p.with_name(f"{p.stem}.cleaned{p.suffix}"))


def _copy_range(src, dst, start: int, end: int, block: int = 1 << 20) -> None:
    """Stream bytes [start, end) from one open file to another."""
    src.seek(start)
    remaining = end - start
    while remaining > 0:
        buf = src.read(min(block, remaining))
        if not buf:
            break
        dst.write(buf)
        remaining -= len(buf)


def strip_id3(source: str, target: str) -> list[str]:
    """Drop ID3v2 (head), ID3v1 and APEv2 (tail) without touching audio frames."""
    actions: list[str] = []
    size = os.path.getsize(source)
    start, end = 0, size

    with open(source, "rb") as src:
        head = src.read(10)
        if head[:3] == b"ID3" and len(head) == 10:
            # 28-bit synchsafe integer: 7 usable bits per byte.
            tag_size = 0
            for byte in head[6:10]:
                tag_size = (tag_size << 7) | (byte & 0x7F)
            start = 10 + tag_size + (10 if head[5] & 0x10 else 0)
            actions.append(f"ID3v2-blok verwijderd ({start} bytes)")

        if end - start > 128:
            src.seek(end - 128)
            if src.read(3) == b"TAG":
                end -= 128
                actions.append("ID3v1-blok verwijderd (128 bytes)")

        if end - start > 32:
            src.seek(end - 32)
            footer = src.read(32)
            if footer[:8] == b"APETAGEX":
                ape_size = int.from_bytes(footer[12:16], "little")
                end -= min(ape_size + 32, end - start)
                actions.append("APE-tag verwijderd")

        with open(target, "wb") as dst:
            _copy_range(src, dst, start, end)

    return actions or ["geen ID3-tags aangetroffen"]


# RIFF chunks that carry the actual audio; everything else is metadata.
RIFF_KEEP = {b"fmt ", b"data", b"fact"}


def strip_riff(source: str, target: str) -> list[str]:
    """Rewrite a WAV keeping only the audio chunks, dropping LIST/INFO, id3, bext."""
    actions: list[str] = []
    with open(source, "rb") as src:
        header = src.read(12)
        if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise RuntimeError("geen geldig RIFF/WAVE-bestand")
        size = os.path.getsize(source)
        keep: list[tuple[int, int]] = []

        while src.tell() + 8 <= size:
            offset = src.tell()
            head = src.read(8)
            if len(head) < 8:
                break
            cid = head[:4]
            length = int.from_bytes(head[4:8], "little")
            padded = length + (length % 2)
            if cid in RIFF_KEEP:
                keep.append((offset, 8 + padded))
            else:
                actions.append(f"chunk {cid.decode('latin-1').strip()} verwijderd")
            src.seek(padded, os.SEEK_CUR)

        body = sum(length for _, length in keep)
        with open(target, "wb") as dst:
            dst.write(b"RIFF" + (body + 4).to_bytes(4, "little") + b"WAVE")
            for offset, length in keep:
                _copy_range(src, dst, offset, offset + length)

    return actions or ["geen metadata-chunks aangetroffen"]


def strip_with_ffmpeg(source: str, target: str) -> list[str]:
    """Remux without re-encoding, dropping all metadata and attached pictures."""
    exe = find_tool("ffmpeg")
    if not exe:
        raise RuntimeError(
            f"Geen wismotor voor {Path(source).suffix or 'dit formaat'}. "
            f"Installeer ffmpeg ({install_hint('ffmpeg')})."
        )
    cmd = [exe, "-v", "error", "-y", "-i", source, "-map", "0",
           "-map", "-0:v", "-map_metadata", "-1", "-c", "copy", target]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip().splitlines()[-1] if proc.stderr else "ffmpeg mislukte")
    return ["ffmpeg remux zonder metadata (audio ongewijzigd)"]


def wipe_media(source: str, target: str) -> list[str]:
    """Strip metadata from audio/video, which exiftool cannot write."""
    suffix = Path(source).suffix.lower()
    if suffix == ".mp3":
        return strip_id3(source, target)
    if suffix == ".wav":
        return strip_riff(source, target)
    return strip_with_ffmpeg(source, target)


# ---------------------------------------------------------------- writing

# exiftool tag name -> ID3 frame, for formats mutagen handles.
ID3_FRAMES = {
    "Title": "TIT2", "Artist": "TPE1", "Album": "TALB", "AlbumArtist": "TPE2",
    "Composer": "TCOM", "Genre": "TCON", "Year": "TDRC", "Date": "TDRC",
    "Track": "TRCK", "Copyright": "TCOP", "Encoder": "TSSE", "Software": "TSSE",
    "Comment": "COMM", "Lyrics": "USLT", "SourceURL": "WOAS", "FileURL": "WOAF",
}

# exiftool tag name -> RIFF INFO four-character code.
RIFF_INFO_CODES = {
    "Title": b"INAM", "Artist": b"IART", "Comment": b"ICMT", "Software": b"ISFT",
    "Genre": b"IGNR", "Copyright": b"ICOP", "DateCreated": b"ICRD",
    "Product": b"IPRD", "Engineer": b"IENG", "Keywords": b"IKEY",
    "Subject": b"ISBJ", "Source": b"ISRC",
}

# Formats where mutagen owns the tags (exiftool cannot write these).
MP4_SUFFIXES = {".m4a", ".mp4", ".aac"}
VORBIS_SUFFIXES = {".flac", ".ogg", ".opus"}
MUTAGEN_SUFFIXES = {".mp3"} | MP4_SUFFIXES | VORBIS_SUFFIXES

# MP4 stores tags in atoms; mutagen's Easy layer maps friendly names onto them.
MP4_NAMES = [
    "album", "albumartist", "artist", "comment", "composersort", "copyright",
    "date", "description", "discnumber", "genre", "grouping", "title", "tracknumber",
]

# Vorbis comments accept free-form keys; these are the conventional ones.
VORBIS_NAMES = [
    "album", "albumartist", "artist", "comment", "composer", "copyright",
    "date", "description", "genre", "title", "tracknumber",
]


# Office Open XML (Word/Excel/PowerPoint): properties live in two XML parts
# inside the zip. exiftool reads these but cannot write them.
OOXML_SUFFIXES = {".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".pptm"}

# tag name -> (element, extra attributes needed when the element is created)
OOXML_CORE = {
    "Title": ("dc:title", ""),
    "Subject": ("dc:subject", ""),
    "Creator": ("dc:creator", ""),
    "Keywords": ("cp:keywords", ""),
    "Description": ("dc:description", ""),
    "LastModifiedBy": ("cp:lastModifiedBy", ""),
    "RevisionNumber": ("cp:revision", ""),
    "Category": ("cp:category", ""),
    "ContentStatus": ("cp:contentStatus", ""),
    "CreateDate": ("dcterms:created", ' xsi:type="dcterms:W3CDTF"'),
    "ModifyDate": ("dcterms:modified", ' xsi:type="dcterms:W3CDTF"'),
}
OOXML_APP = {"Application": "Application", "Company": "Company",
             "Manager": "Manager", "AppVersion": "AppVersion"}

# OpenDocument (LibreOffice): everything sits in meta.xml.
ODF_SUFFIXES = {".odt", ".ods", ".odp", ".odg"}
# Keys are exactly what exiftool reports, since the UI derives them from it.
ODF_META = {
    "Title": "dc:title",
    "Subject": "dc:subject",
    "Description": "dc:description",
    "Initial-creator": "meta:initial-creator",
    "Creator": "dc:creator",
    "Keyword": "meta:keyword",
    "Generator": "meta:generator",
    "Creation-date": "meta:creation-date",
    "Date": "dc:date",
}

DATE_TAGS = {"CreateDate", "ModifyDate", "Creation-date", "Date"}


def to_w3cdtf(value: str) -> str:
    """Accept what the viewer shows ("2026:01:15 09:00:00Z") and emit ISO."""
    text = value.strip()
    match = re.match(
        r"^(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})(.*)$", text
    )
    if not match:
        return text
    y, mo, d, h, mi, s, rest = match.groups()
    rest = rest.strip()
    if not rest or rest.upper() == "Z":
        rest = "Z"
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}{rest}"


def patch_xml_element(xml: str, element: str, attrs: str, value: str | None) -> str:
    """Set, replace or drop a simple <ns:name>text</ns:name> element."""
    pattern = re.compile(
        rf"<{re.escape(element)}(\s[^>]*)?>.*?</{re.escape(element)}>|"
        rf"<{re.escape(element)}(\s[^>]*)?/>",
        re.DOTALL,
    )
    if value is None:
        return pattern.sub("", xml)

    escaped = (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    replacement = f"<{element}{attrs}>{escaped}</{element}>"
    if pattern.search(xml):
        return pattern.sub(lambda _: replacement, xml, count=1)
    # Not present yet: insert just before the closing root tag.
    closing = re.search(r"</[A-Za-z_][\w.:-]*>\s*$", xml.rstrip())
    if not closing:
        raise RuntimeError(f"kan {element} niet invoegen: onverwachte XML")
    return xml[:closing.start()] + replacement + xml[closing.start():]


def rewrite_zip_parts(path: str, parts: dict[str, str]) -> None:
    """Replace named entries in a zip, preserving every other entry as-is."""
    import zipfile

    tmp = f"{path}.tmp-zip"
    with zipfile.ZipFile(path) as src:
        infos = src.infolist()
        payloads = {info.filename: src.read(info.filename) for info in infos}
    for name, text in parts.items():
        payloads[name] = text.encode("utf-8")

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in infos:
            # mimetype must stay first and uncompressed for ODF readers.
            method = zipfile.ZIP_STORED if info.filename == "mimetype" else zipfile.ZIP_DEFLATED
            new = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            new.compress_type = method
            new.external_attr = info.external_attr
            dst.writestr(new, payloads[info.filename])
    os.replace(tmp, path)


def write_tags_ooxml(path: str, changes: dict[str, str | None]) -> list[str]:
    """Patch docProps/core.xml and app.xml inside an Office document."""
    import zipfile

    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        core = z.read("docProps/core.xml").decode("utf-8") if "docProps/core.xml" in names else ""
        app = z.read("docProps/app.xml").decode("utf-8") if "docProps/app.xml" in names else ""

    actions: list[str] = []
    for key, value in changes.items():
        name = tag_name(key)
        text = value if value is None else (
            to_w3cdtf(value) if name in DATE_TAGS else value
        )
        if name in OOXML_CORE:
            if not core:
                raise RuntimeError("dit document heeft geen docProps/core.xml")
            element, attrs = OOXML_CORE[name]
            core = patch_xml_element(core, element, attrs, text)
        elif name in OOXML_APP:
            if not app:
                raise RuntimeError("dit document heeft geen docProps/app.xml")
            app = patch_xml_element(app, OOXML_APP[name], "", text)
        else:
            raise RuntimeError(
                f"'{name}' hoort niet bij de bewerkbare documenteigenschappen"
            )
        actions.append(f"{name} {'verwijderd' if value is None else 'gewijzigd'}")

    parts = {}
    if core:
        parts["docProps/core.xml"] = core
    if app:
        parts["docProps/app.xml"] = app
    rewrite_zip_parts(path, parts)
    return actions


def write_tags_odf(path: str, changes: dict[str, str | None]) -> list[str]:
    """Patch meta.xml inside an OpenDocument file."""
    import zipfile

    with zipfile.ZipFile(path) as z:
        if "meta.xml" not in z.namelist():
            raise RuntimeError("dit document heeft geen meta.xml")
        meta = z.read("meta.xml").decode("utf-8")

    actions: list[str] = []
    for key, value in changes.items():
        name = tag_name(key)
        if name not in ODF_META:
            raise RuntimeError(f"'{name}' hoort niet bij de bewerkbare eigenschappen")
        text = value if value is None else (
            to_w3cdtf(value).rstrip("Z") if name in DATE_TAGS else value
        )
        meta = patch_xml_element(meta, ODF_META[name], "", text)
        actions.append(f"{name} {'verwijderd' if value is None else 'gewijzigd'}")

    rewrite_zip_parts(path, {"meta.xml": meta})
    return actions


def tag_name(key: str) -> str:
    """"RIFF:Comment" -> "Comment"."""
    return key.partition(":")[2] or key


def editable_names(path: str) -> list[str] | None:
    """Tag names this file's writer supports, or None when anything goes."""
    suffix = Path(path).suffix.lower()
    if suffix == ".wav":
        return sorted(RIFF_INFO_CODES)
    if suffix == ".mp3":
        return sorted(ID3_FRAMES)
    if suffix in MP4_SUFFIXES:
        return MP4_NAMES
    if suffix in VORBIS_SUFFIXES:
        return VORBIS_NAMES
    if suffix in OOXML_SUFFIXES:
        return sorted(set(OOXML_CORE) | set(OOXML_APP))
    if suffix in ODF_SUFFIXES:
        return sorted(ODF_META)
    return None  # exiftool: any tag it knows


# Filesystem timestamps exiftool can set on any file. These are not document
# properties: changing them does not touch what Word shows under "Properties".
FILESYSTEM_TAGS = {"FileModifyDate", "FileCreateDate"}

# Groups that describe container plumbing rather than editable metadata.
READ_ONLY_GROUPS = {"ZIP", "ExifTool", "Composite", "MPEG", "RIFF-structure"}


@functools.lru_cache(maxsize=1)
def exiftool_writable() -> frozenset[str]:
    """Extensions exiftool can write, asked of exiftool itself."""
    exe = find_tool("exiftool")
    if not exe:
        return frozenset()
    try:
        proc = subprocess.run([exe, "-listwf"], capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return frozenset()
    words = proc.stdout.replace("Writable file extensions:", "").split()
    return frozenset(word.lower() for word in words)


def why_format_not_writable(path: str) -> str | None:
    """Explain why no metadata can be written to this format at all."""
    if editable_names(path) is not None:
        return None  # a dedicated writer handles it
    ext = Path(path).suffix.lower().lstrip(".")
    if ext in exiftool_writable():
        return None
    return (f"Metadata in .{ext} kan niet geschreven worden.\n\n"
            "exiftool ondersteunt dit formaat alleen om te lezen, en er is geen "
            "eigen schrijver voor. Lezen en wissen werken wel.")


def why_not_editable(path: str, key: str) -> str | None:
    """Explain why a row cannot be edited, or None when it can."""
    group = key.partition(":")[0]
    name = tag_name(key)

    if group == "ZIP":
        return ("ZIP-velden beschrijven hoe het bestand is ingepakt, niet de "
                "documenteigenschappen. Voor een Word-document wil je de "
                "XML-groep: CreateDate en ModifyDate.")
    if group in READ_ONLY_GROUPS:
        return f"De groep {group} bevat structuur uit het bestand zelf en is niet bewerkbaar."
    if group == "File":
        if name in FILESYSTEM_TAGS:
            return None
        return (f"'{name}' wordt afgeleid van het bestand op schijf en is niet "
                "bewerkbaar.")

    blocked = why_format_not_writable(path)
    if blocked:
        return blocked

    allowed = editable_names(path)
    if allowed is not None and name not in allowed:
        return (f"'{name}' is niet bewerkbaar in dit formaat.\n\n"
                f"Wel bewerkbaar: {', '.join(allowed)}")
    return None


def write_tags_mutagen(path: str, changes: dict[str, str | None]) -> list[str]:
    """Write ID3/Vorbis/MP4 tags via mutagen."""
    try:
        import mutagen
        from mutagen.id3 import ID3, ID3NoHeaderError
    except ImportError:
        raise RuntimeError("mutagen ontbreekt: pip3 install mutagen")

    actions: list[str] = []
    suffix = Path(path).suffix.lower()

    if suffix == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        import mutagen.id3 as id3

        for key, value in changes.items():
            name = tag_name(key)
            frame_id = ID3_FRAMES.get(name)
            if not frame_id:
                raise RuntimeError(f"'{name}' kan niet naar ID3 geschreven worden")
            if value is None:
                tags.delall(frame_id)
                actions.append(f"{name} verwijderd")
                continue
            cls = getattr(id3, frame_id)
            if frame_id in {"COMM", "USLT"}:
                tags.delall(frame_id)
                tags.add(cls(encoding=3, lang="eng", desc="", text=value))
            elif frame_id.startswith("W"):
                tags.delall(frame_id)
                tags.add(cls(url=value))
            else:
                tags.setall(frame_id, [cls(encoding=3, text=value)])
            actions.append(f"{name} gewijzigd")
        tags.save(path)
        return actions

    if suffix in MP4_SUFFIXES:
        from mutagen.easymp4 import EasyMP4

        audio = EasyMP4(path)
        allowed = set(EasyMP4.Get)
    else:
        audio = mutagen.File(path)
        if audio is None:
            raise RuntimeError(f"mutagen herkent {suffix} niet")
        if audio.tags is None:
            audio.add_tags()
        allowed = None  # Vorbis comments take free-form keys

    for key, value in changes.items():
        name = tag_name(key).lower()
        if allowed is not None and name not in allowed:
            raise RuntimeError(f"'{tag_name(key)}' kan niet naar {suffix} geschreven worden")
        if value is None:
            audio.pop(name, None)
            actions.append(f"{tag_name(key)} verwijderd")
        else:
            audio[name] = value
            actions.append(f"{tag_name(key)} gewijzigd")
    audio.save()
    return actions


def read_riff_info(path: str) -> dict[bytes, str]:
    """Existing LIST/INFO values, so an edit does not drop the others."""
    values: dict[bytes, str] = {}
    with open(path, "rb") as src:
        size = os.path.getsize(path)
        src.seek(12)
        while src.tell() + 8 <= size:
            head = src.read(8)
            if len(head) < 8:
                break
            cid = head[:4]
            length = int.from_bytes(head[4:8], "little")
            if cid == b"LIST":
                payload = src.read(length + length % 2)
                if payload[:4] == b"INFO":
                    pos = 4
                    while pos + 8 <= len(payload):
                        code = payload[pos:pos + 4]
                        vlen = int.from_bytes(payload[pos + 4:pos + 8], "little")
                        raw = payload[pos + 8:pos + 8 + vlen]
                        values[code] = raw.split(b"\x00")[0].decode("utf-8", "replace")
                        pos += 8 + vlen + (vlen % 2)
            else:
                src.seek(length + length % 2, os.SEEK_CUR)
    return values


def write_tags_riff(path: str, changes: dict[str, str | None]) -> list[str]:
    """Rewrite a WAV with an updated LIST/INFO chunk, audio untouched."""
    info = read_riff_info(path)
    actions: list[str] = []
    for key, value in changes.items():
        name = tag_name(key)
        code = RIFF_INFO_CODES.get(name)
        if not code:
            raise RuntimeError(f"'{name}' kan niet naar een WAV geschreven worden")
        if value is None:
            info.pop(code, None)
            actions.append(f"{name} verwijderd")
        else:
            info[code] = value
            actions.append(f"{name} gewijzigd")

    payload = b"INFO"
    for code, value in info.items():
        raw = value.encode("utf-8") + b"\x00"
        if len(raw) % 2:
            raw += b"\x00"
        payload += code + len(raw).to_bytes(4, "little") + raw

    tmp = f"{path}.tmp-tags.wav"
    with open(path, "rb") as src:
        size = os.path.getsize(path)
        keep: list[tuple[int, int]] = []
        src.seek(12)
        while src.tell() + 8 <= size:
            offset = src.tell()
            head = src.read(8)
            if len(head) < 8:
                break
            cid = head[:4]
            length = int.from_bytes(head[4:8], "little")
            padded = length + (length % 2)
            if cid != b"LIST":
                keep.append((offset, 8 + padded))
            src.seek(padded, os.SEEK_CUR)

        body = sum(length for _, length in keep) + (8 + len(payload) if len(info) else 0)
        with open(tmp, "wb") as dst:
            dst.write(b"RIFF" + (body + 4).to_bytes(4, "little") + b"WAVE")
            written_info = False
            for offset, length in keep:
                # INFO goes before the audio data, where WAV readers expect it.
                if not written_info and info:
                    src.seek(offset)
                    if src.read(4) == b"data":
                        dst.write(b"LIST" + len(payload).to_bytes(4, "little") + payload)
                        written_info = True
                _copy_range(src, dst, offset, offset + length)
            if not written_info and info:
                dst.write(b"LIST" + len(payload).to_bytes(4, "little") + payload)
    os.replace(tmp, path)
    return actions


def write_tags_exiftool(path: str, changes: dict[str, str | None]) -> list[str]:
    exe = find_tool("exiftool")
    if not exe:
        raise RuntimeError(f"exiftool ontbreekt ({install_hint('exiftool')})")
    args = [f"-{key}=" if value is None else f"-{key}={value}"
            for key, value in changes.items()]
    proc = subprocess.run([exe] + args + ["-overwrite_original", path],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip().splitlines()[-1] if proc.stderr
                           else "exiftool kon niet schrijven")
    return [f"{tag_name(k)} {'verwijderd' if v is None else 'gewijzigd'}"
            for k, v in changes.items()]


def apply_changes(
    source: str, output: str | None, changes: dict[str, str | None]
) -> dict[str, object]:
    """Write pending edits, to a copy by default (output=None edits in place)."""
    if not changes:
        return {"output": source, "actions": []}

    if output is None:
        shutil.copy2(source, f"{source}.bak")
        target = source
    else:
        shutil.copy2(source, output)
        target = output

    # Filesystem timestamps go through exiftool whatever the format is, and
    # must be applied last: rewriting the file bumps the modify time again.
    fs_changes = {k: v for k, v in changes.items() if k.startswith("File:")}
    doc_changes = {k: v for k, v in changes.items() if not k.startswith("File:")}

    suffix = Path(target).suffix.lower()
    try:
        actions = []
        if doc_changes:
            if suffix == ".wav":
                actions = write_tags_riff(target, doc_changes)
            elif suffix in MUTAGEN_SUFFIXES:
                actions = write_tags_mutagen(target, doc_changes)
            elif suffix in OOXML_SUFFIXES:
                actions = write_tags_ooxml(target, doc_changes)
            elif suffix in ODF_SUFFIXES:
                actions = write_tags_odf(target, doc_changes)
            else:
                actions = write_tags_exiftool(target, doc_changes)
        if fs_changes:
            actions += write_tags_exiftool(target, fs_changes)
    except Exception:
        if output is not None and os.path.exists(output):
            os.unlink(output)  # do not leave a half-written copy behind
        raise
    return {"output": target, "actions": actions}


def drop_ai_tags(path: str) -> list[str]:
    """Delete individual tags that look AI-related, leaving the rest intact.

    The skill's selective mode only knows C2PA-shaped markers, so it keeps
    things like Software="Made with Google Gemini". This closes that gap.
    """
    exe = find_tool("exiftool")
    if not exe:
        return []
    exif, _ = run_exiftool(path)
    targets = [
        k for k, v in exif.items()
        if k != "SourceFile" and is_embedded(k) and classify(k, v) == "ai"
    ]
    if not targets:
        return []
    cmd = [exe] + [f"-{k}=" for k in targets] + ["-overwrite_original", path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return [f"kon AI-tags niet wissen: {proc.stderr.strip().splitlines()[0]}"]
    return [f"drop tag {k}" for k in targets]


def wipe_metadata(path: str, output: str | None, keep_non_ai: bool) -> dict[str, object]:
    """Strip metadata. output=None means in place (a .bak is kept).

    Prefers the remove-ai-marks cleaner (atomic writes, refuses symlinked
    destinations, handles text and containers as well as images) and falls
    back to plain exiftool.
    """
    if media_kind(path) in {"audio", "video"}:
        # Neither the skill nor exiftool can write these containers.
        final = output or path
        # Keep the suffix: ffmpeg picks its output format from the extension.
        tmp = f"{final}.tmp-wipe{Path(final).suffix}"
        try:
            actions = wipe_media(path, tmp)
            if output is None:
                os.replace(path, f"{path}.bak")
            os.replace(tmp, final)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return {"engine": "ingebouwd", "input": path, "output": final, "actions": actions}

    script = SKILL_SCRIPTS / "clean_file.py"
    python = python_interpreter()
    if script.exists() and python:
        cmd = [python, str(script), path, "--json"]
        cmd += ["--in-place"] if output is None else ["-o", output]
        if keep_non_ai:
            cmd.append("--keep-non-ai-metadata")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                              env=child_env())
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "clean_file.py mislukte")
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise RuntimeError("clean_file.py gaf onleesbare JSON")
        result["engine"] = "remove-ai-marks"
        if keep_non_ai:
            written = str(result.get("output") or path)
            extra = drop_ai_tags(written)
            result["actions"] = list(result.get("actions") or []) + extra
        return result

    exe = find_tool("exiftool")
    if not exe:
        raise RuntimeError("Geen wismotor: installeer exiftool of de remove-ai-marks skill.")
    tail = ["-overwrite_original"] if output is None else ["-o", output]
    cmd = [exe, "-all="] + tail + [path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "exiftool mislukte")
    return {
        "engine": "exiftool",
        "input": path,
        "output": output or path,
        "actions": ["exiftool -all="],
    }


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


# ------------------------------------------------------------------ UI layer


class MetadataViewer(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=8)
        self.master = master
        self.path: str | None = None
        self.exif: dict[str, object] = {}
        self.skill_report: dict[str, object] | None = None
        self.raw_markers: list[str] = []
        self.deep_report: deep_inspection.DeepInspectionResult | None = None
        # "Group:Tag" -> new value, or None to delete. Nothing touches disk
        # until the user saves.
        self.pending: dict[str, str | None] = {}
        self._preview_image = None  # keep a reference alive
        # Workers hand results back here; only the main thread touches Tk.
        self._results: queue.Queue[tuple] = queue.Queue()

        self.filter_var = tk.StringVar()
        self.flagged_only = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Open een bestand om te beginnen.")

        self.pack(fill="both", expand=True)
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

        self.filter_var.trace_add("write", lambda *_: self._populate())
        self._poll_results()

    # -- construction

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))

        ttk.Button(bar, text="Open bestand…", command=self.open_dialog).pack(side="left")
        ttk.Button(bar, text="Kopieer JSON", command=self.copy_json).pack(side="left", padx=(6, 0))
        self.wipe_button = ttk.Button(
            bar, text="Metadata wissen…", command=self.open_wipe_dialog, state="disabled"
        )
        self.wipe_button.pack(side="left", padx=(6, 0))

        self.add_button = ttk.Button(
            bar, text="Tag toevoegen…", command=self.open_add_dialog, state="disabled"
        )
        self.add_button.pack(side="left", padx=(6, 0))
        self.save_button = ttk.Button(
            bar, text="Opslaan…", command=self.open_save_dialog, state="disabled"
        )
        self.save_button.pack(side="left", padx=(6, 0))
        self.revert_button = ttk.Button(
            bar, text="Herstel", command=self.revert_changes, state="disabled"
        )
        self.revert_button.pack(side="left", padx=(6, 0))
        ttk.Checkbutton(
            bar, text="Alleen opvallend", variable=self.flagged_only, command=self._populate
        ).pack(side="left", padx=(12, 0))

        ttk.Entry(bar, textvariable=self.filter_var, width=24).pack(side="right")
        ttk.Label(bar, text="Filter:").pack(side="right", padx=(0, 4))

    def _build_body(self) -> None:
        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)

        left = ttk.Frame(panes, padding=(0, 0, 8, 0))
        panes.add(left, weight=0)

        self.preview_label = ttk.Label(left, anchor="center")
        self.preview_label.pack(fill="x")

        ttk.Label(left, text="AI / herkomst", font=("", 12, "bold")).pack(
            anchor="w", pady=(10, 2)
        )
        self.notes = tk.Text(left, width=34, height=18, wrap="word", relief="flat",
                             highlightthickness=1, padx=6, pady=6)
        self.notes.pack(fill="both", expand=True)
        self.notes.configure(state="disabled")

        right = ttk.Frame(panes)
        panes.add(right, weight=1)

        self.tree = ttk.Treeview(right, columns=("value",), show="tree headings")
        self.tree.heading("#0", text="Tag")
        self.tree.heading("value", text="Waarde")
        self.tree.column("#0", width=260, stretch=False)
        self.tree.column("value", width=460)
        self.tree.tag_configure("ai", foreground="#c0392b")
        self.tree.tag_configure("privacy", foreground="#b9770e")
        self.tree.tag_configure("group", font=("", 12, "bold"))
        self.tree.tag_configure("edited", foreground="#1f6f43")
        self.tree.tag_configure("deleted", foreground="#7f8c8d")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", self._edit_row)

        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Bewerken…", command=lambda: self._edit_row(None))
        menu.add_command(label="Tag verwijderen", command=self._delete_row)
        menu.add_separator()
        menu.add_command(label="Kopieer regel", command=self._copy_row)
        self.row_menu = menu
        self.tree.bind("<Button-2>", self._show_row_menu)
        self.tree.bind("<Control-Button-1>", self._show_row_menu)
        self.tree.bind("<Button-3>", self._show_row_menu)

        scroll = ttk.Scrollbar(right, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Label(bar, textvariable=self.status_var).pack(side="left")
        ttk.Label(
            bar,
            text="⚠︎ rood = AI/herkomst   ⚠︎ oranje = privacy   ✎ groen = niet opgeslagen",
            foreground="#7f8c8d",
        ).pack(side="right")

    # -- actions

    def open_dialog(self) -> None:
        path = filedialog.askopenfilename(title="Kies een bestand")
        if path:
            self.load(path)

    def load(self, path: str) -> None:
        if not os.path.isfile(path):
            messagebox.showerror(APP_TITLE, f"Geen bestand: {path}")
            return
        if self.pending and not messagebox.askyesno(
            APP_TITLE,
            f"Er zijn {len(self.pending)} niet-opgeslagen wijziging(en).\n\nVerwerpen?",
        ):
            return
        self.path = path
        self.status_var.set(f"Bezig met lezen van {os.path.basename(path)}…")
        self.master.title(f"{APP_TITLE} — {os.path.basename(path)}")
        threading.Thread(target=self._load_worker, args=(path,), daemon=True).start()

    def _load_worker(self, path: str) -> None:
        exif, error = run_exiftool(path)
        report = run_skill_inspect(path)
        raw = scan_raw_markers(path)
        deep = deep_inspection.inspect(path)
        self._results.put((path, exif, error, report, raw, deep))

    def _poll_results(self) -> None:
        """Drain finished reads on the main thread; Tk is not thread-safe."""
        try:
            while True:
                self._load_done(*self._results.get_nowait())
        except queue.Empty:
            pass
        self.after(80, self._poll_results)

    def _load_done(
        self,
        path: str,
        exif: dict[str, object],
        error: str | None,
        report: dict[str, object] | None,
        raw_markers: list[str],
        deep_report: deep_inspection.DeepInspectionResult,
    ) -> None:
        if path != self.path:  # a newer file was opened while this one loaded
            return
        self.exif = exif
        self.skill_report = report
        self.raw_markers = raw_markers
        self.deep_report = deep_report
        self.pending.clear()
        self._refresh_edit_state()
        self._show_preview(path)
        self._show_notes(error)
        self._populate()
        self.wipe_button.configure(state="normal")
        self.add_button.configure(
            state="disabled" if why_format_not_writable(path) else "normal"
        )

        size = human_size(os.path.getsize(path))
        ftype = exif.get("File:FileType", Path(path).suffix.lstrip(".").upper() or "?")
        self.status_var.set(f"{os.path.basename(path)} — {ftype}, {size} — {len(exif)} velden")

    def _show_preview(self, path: str) -> None:
        self._preview_image = None
        self.preview_label.configure(image="", text="")
        if Path(path).suffix.lower() not in IMAGE_SUFFIXES:
            self.preview_label.configure(text="(geen voorbeeld)", foreground="#7f8c8d")
            return
        try:
            from PIL import Image, ImageTk

            with Image.open(path) as img:
                img.thumbnail((260, 220))
                self._preview_image = ImageTk.PhotoImage(img.convert("RGBA"))
            self.preview_label.configure(image=self._preview_image)
        except Exception:
            self.preview_label.configure(text="(voorbeeld mislukt)", foreground="#7f8c8d")

    def _show_notes(self, error: str | None) -> None:
        lines: list[str] = []
        if error:
            lines += [f"⚠︎ {error}", ""]

        # Everything outside these groups is metadata someone actually embedded;
        # File/Composite are derived from the bytes and always present.
        embedded = [k for k in self.exif if k != "SourceFile" and is_embedded(k)]

        flagged_ai = [k for k, v in self.exif.items() if classify(k, v) == "ai"]
        if flagged_ai:
            lines.append("Tags die op AI/herkomst wijzen:")
            lines += [f"  • {k.split(':', 1)[-1]}" for k in flagged_ai[:10]]
        elif not embedded:
            kind = media_kind(self.path or "")
            reason = (
                "Meestal betekent dit dat de metadata gestript is bij het "
                "downloaden of delen"
            )
            reason += ", of dat het een screenshot is." if kind == "image" else (
                ", of dat het bestand opnieuw is geëxporteerd."
                if kind in {"audio", "video"} else "."
            )
            lines.append("Dit bestand bevat GEEN ingebedde metadata.")
            lines.append("")
            lines.append(
                "Alles wat je rechts ziet is uit de bytes afgeleid "
                f"(formaat, bestandsdatum). {reason}"
            )
        else:
            lines.append(
                f"{len(embedded)} ingebedde velden, maar geen AI-verwijzing erin."
            )

        if self.raw_markers:
            lines.append("")
            lines.append("Sporen in de ruwe bytes:")
            lines += [f"  • {marker}" for marker in self.raw_markers]
        lines.append("")

        lines.append(INVISIBLE_MARK_CAVEAT[media_kind(self.path or "")])
        lines.append("")

        skill_lines = summarize_skill(self.skill_report)
        if skill_lines:
            lines.append("")
            lines.append("remove-ai-marks:")
            lines += skill_lines
        elif media_kind(self.path or "") in {"audio", "video"}:
            lines.append("")
            lines.append("(diepte-analyse niet van toepassing op audio/video)")
        elif not (SKILL_SCRIPTS / "inspect_file.py").exists():
            lines.append("")
            lines.append("(skill remove-ai-marks niet geïnstalleerd)")

        if self.deep_report:
            lines.append("")
            lines.append("Diepe inspectie:")
            lines += deep_inspection.summary(self.deep_report)
            lines.append("")
            lines.append(
                "Let op: deze diepte-inspectie toont alleen sporen die in het "
                "bestand zichtbaar zijn. Statistische watermerken van Claude, "
                "SynthID-Text of andere providers zijn zonder hun geheime sleutel "
                "niet detecteerbaar."
            )

        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", "\n".join(lines))
        self.notes.configure(state="disabled")

    def _populate(self) -> None:
        self.tree.delete(*self.tree.get_children())
        needle = self.filter_var.get().strip().lower()
        only_flagged = self.flagged_only.get()

        # Pending edits win over what is on disk; new tags are appended.
        merged: dict[str, object] = dict(self.exif)
        for key, value in self.pending.items():
            merged[key] = "" if value is None else value

        groups: dict[str, list[tuple[str, object, str]]] = {}
        for key, value in merged.items():
            if key == "SourceFile":
                continue
            if key in self.pending and self.pending[key] is None and key not in self.exif:
                continue
            group, _, tag = key.partition(":")
            if not tag:
                group, tag = "Overig", group
            if group in BORING_GROUPS:
                continue
            flag = classify(key, value)
            if only_flagged and not flag:
                continue
            if needle and needle not in tag.lower() and needle not in f"{value}".lower():
                continue
            groups.setdefault(group, []).append((tag, value, flag))

        for group in sorted(groups):
            parent = self.tree.insert("", "end", text=group, open=True, tags=("group",))
            for tag, value, flag in groups[group]:
                key = f"{group}:{tag}"
                staged = key in self.pending
                label = f"✎ {tag}" if staged else (f"⚠︎ {tag}" if flag else tag)
                if staged and self.pending[key] is None:
                    text, row_tag = "(wordt verwijderd)", "deleted"
                else:
                    text = " ".join(f"{value}".split())
                    if len(text) > 300:
                        text = text[:300] + "…"
                    row_tag = "edited" if staged else flag
                self.tree.insert(parent, "end", text=label, values=(text,),
                                 tags=(row_tag,) if row_tag else ())

        if not groups:
            self.tree.insert("", "end", text="(niets gevonden)", values=("",))

    # -- wiping

    def open_wipe_dialog(self) -> None:
        if not self.path:
            return
        source = self.path

        win = tk.Toplevel(self)
        win.title("Metadata wissen")
        win.transient(self.master)
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text=os.path.basename(source), font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="Verwijdert EXIF, XMP, IPTC, C2PA en AI-tags uit een kopie.",
            foreground="#7f8c8d",
        ).pack(anchor="w", pady=(2, 10))

        mode = tk.StringVar(value="copy")
        keep_non_ai = tk.BooleanVar(value=False)
        target = tk.StringVar(value=default_target(source))

        ttk.Radiobutton(
            frame, text="Naar een nieuwe kopie (aanbevolen)", variable=mode, value="copy"
        ).pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(fill="x", padx=(20, 0), pady=(2, 8))
        entry = ttk.Entry(row, textvariable=target, width=44)
        entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="…", width=3,
                   command=lambda: self._pick_target(target)).pack(side="left", padx=(4, 0))

        ttk.Radiobutton(
            frame, text="Origineel overschrijven (bewaart een .bak)",
            variable=mode, value="inplace",
        ).pack(anchor="w")

        ttk.Separator(frame).pack(fill="x", pady=10)
        ttk.Checkbutton(
            frame, text="Alleen AI/C2PA-sporen wissen, overige metadata behouden",
            variable=keep_non_ai,
        ).pack(anchor="w")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Annuleren", command=win.destroy).pack(side="right")
        ttk.Button(
            buttons, text="Wissen",
            command=lambda: self._run_wipe(win, source, mode.get(), target.get(),
                                           keep_non_ai.get()),
        ).pack(side="right", padx=(0, 6))

        win.grab_set()
        entry.focus_set()

    def _pick_target(self, target: tk.StringVar) -> None:
        chosen = filedialog.asksaveasfilename(
            title="Opslaan als", initialfile=os.path.basename(target.get())
        )
        if chosen:
            target.set(chosen)

    def _run_wipe(
        self, win: tk.Toplevel, source: str, mode: str, target: str, keep_non_ai: bool
    ) -> None:
        output: str | None
        if mode == "inplace":
            if not messagebox.askyesno(
                APP_TITLE,
                f"{os.path.basename(source)} wordt overschreven.\n\n"
                "Er blijft een back-up achter als .bak. Doorgaan?",
                parent=win,
            ):
                return
            output = None
        else:
            target = target.strip()
            if not target:
                messagebox.showerror(APP_TITLE, "Geef een doelbestand op.", parent=win)
                return
            if os.path.abspath(target) == os.path.abspath(source):
                messagebox.showerror(
                    APP_TITLE, "Doel is het origineel. Kies 'overschrijven' als je dat wilt.",
                    parent=win,
                )
                return
            if os.path.exists(target) and not messagebox.askyesno(
                APP_TITLE, f"{os.path.basename(target)} bestaat al. Overschrijven?", parent=win
            ):
                return
            output = target

        win.destroy()
        self.status_var.set("Bezig met wissen…")
        self.update_idletasks()
        try:
            result = wipe_metadata(source, output, keep_non_ai)
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            self.status_var.set("Wissen mislukt.")
            messagebox.showerror(APP_TITLE, f"Wissen mislukt:\n\n{exc}")
            return

        written = str(result.get("output") or source)
        actions = result.get("actions") or []
        summary = "\n".join(f"  • {a}" for a in actions) or "  • (geen wijzigingen nodig)"
        residual = result.get("still_has_c2pa") or result.get("still_has_ai_metadata")

        message = f"Geschreven naar:\n{written}\n\nUitgevoerd:\n{summary}"
        if residual:
            message += "\n\n⚠︎ Er zijn nog restsporen van C2PA/AI-metadata gevonden."
        message += "\n\n" + INVISIBLE_MARK_CAVEAT[media_kind(written)]
        messagebox.showinfo(APP_TITLE, message)
        self.load(written)  # reload so the panel shows the result

    # -- editing

    def _selected_key(self) -> str | None:
        """The "Group:Tag" behind the focused row, or None for a group header."""
        item = self.tree.focus()
        if not item or not self.tree.parent(item):
            return None
        group = self.tree.item(self.tree.parent(item), "text")
        tag = self.tree.item(item, "text").removeprefix("⚠︎ ").removeprefix("✎ ")
        return f"{group}:{tag}"

    def _show_row_menu(self, event: tk.Event) -> None:
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.focus(item)
            self.tree.selection_set(item)
            self.row_menu.tk_popup(event.x_root, event.y_root)

    def _edit_row(self, _event: object = None) -> None:
        key = self._selected_key()
        if not key or not self.path:
            return
        problem = why_not_editable(self.path, key)
        if problem:
            messagebox.showinfo(APP_TITLE, problem)
            return

        current = self.pending.get(key, self.exif.get(key, ""))
        self._value_dialog(
            title=f"Bewerken — {tag_name(key)}",
            initial="" if current is None else str(current),
            on_ok=lambda value: self._stage(key, value),
        )

    def _value_dialog(self, title: str, initial: str, on_ok) -> None:
        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self.master)
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=title, font=("", 13, "bold")).pack(anchor="w", pady=(0, 8))

        text = tk.Text(frame, width=64, height=10, wrap="word", padx=6, pady=6)
        text.insert("1.0", initial)
        text.pack(fill="both", expand=True)

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(12, 0))

        def confirm() -> None:
            value = text.get("1.0", "end").rstrip("\n")
            win.destroy()
            on_ok(value)

        ttk.Button(buttons, text="Annuleren", command=win.destroy).pack(side="right")
        ttk.Button(buttons, text="OK", command=confirm).pack(side="right", padx=(0, 6))
        win.grab_set()
        text.focus_set()

    def _delete_row(self) -> None:
        key = self._selected_key()
        if key:
            self._stage(key, None)

    def _stage(self, key: str, value: str | None) -> None:
        """Record a pending change without touching the file."""
        if value is not None and str(self.exif.get(key, "")) == value:
            self.pending.pop(key, None)
        else:
            self.pending[key] = value
        self._refresh_edit_state()
        self._populate()

    def revert_changes(self) -> None:
        self.pending.clear()
        self._refresh_edit_state()
        self._populate()
        self.status_var.set("Wijzigingen ongedaan gemaakt.")

    def _refresh_edit_state(self) -> None:
        state = "normal" if self.pending else "disabled"
        self.save_button.configure(state=state)
        self.revert_button.configure(state=state)
        if self.pending:
            self.status_var.set(f"{len(self.pending)} niet-opgeslagen wijziging(en).")

    def open_add_dialog(self) -> None:
        if not self.path:
            return
        allowed = editable_names(self.path)

        win = tk.Toplevel(self)
        win.title("Tag toevoegen")
        win.transient(self.master)
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Tagnaam").pack(anchor="w")
        name_var = tk.StringVar()
        if allowed:
            name_widget = ttk.Combobox(frame, textvariable=name_var, values=allowed, width=34)
        else:
            name_widget = ttk.Entry(frame, textvariable=name_var, width=36)
        name_widget.pack(anchor="w", pady=(2, 10))

        ttk.Label(frame, text="Waarde").pack(anchor="w")
        value_text = tk.Text(frame, width=52, height=6, wrap="word", padx=6, pady=6)
        value_text.pack(fill="both", expand=True, pady=(2, 0))

        def confirm() -> None:
            name = name_var.get().strip()
            if not name:
                messagebox.showerror(APP_TITLE, "Geef een tagnaam op.", parent=win)
                return
            if allowed is not None and name not in allowed:
                messagebox.showerror(
                    APP_TITLE, f"'{name}' wordt niet ondersteund in dit formaat.", parent=win
                )
                return
            group = self._group_for(name)
            value = value_text.get("1.0", "end").rstrip("\n")
            win.destroy()
            self._stage(f"{group}:{name}", value)

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Annuleren", command=win.destroy).pack(side="right")
        ttk.Button(buttons, text="Toevoegen", command=confirm).pack(side="right", padx=(0, 6))
        win.grab_set()
        name_widget.focus_set()

    def _group_for(self, name: str) -> str:
        """Pick the group a newly added tag should live under."""
        for key in self.exif:
            if key.partition(":")[2] == name:
                return key.partition(":")[0]
        suffix = Path(self.path or "").suffix.lower()
        if suffix == ".wav":
            return "RIFF"
        if suffix == ".mp3":
            return "ID3"
        if suffix in MP4_SUFFIXES:
            return "QuickTime"
        if suffix in VORBIS_SUFFIXES:
            return "Vorbis"
        if suffix in OOXML_SUFFIXES:
            return "XML"
        return "XMP"

    def open_save_dialog(self) -> None:
        if not self.path or not self.pending:
            return
        source = self.path
        target = default_target(source)

        if not messagebox.askyesno(
            APP_TITLE,
            f"{len(self.pending)} wijziging(en) opslaan?\n\n"
            f"Naar kopie:\n{os.path.basename(target)}\n\n"
            "Kies 'Nee' om het origineel te overschrijven (met .bak).",
        ):
            if not messagebox.askyesno(
                APP_TITLE,
                f"{os.path.basename(source)} overschrijven?\n\nEr blijft een .bak achter.",
            ):
                return
            target = None

        if target and os.path.exists(target) and not messagebox.askyesno(
            APP_TITLE, f"{os.path.basename(target)} bestaat al. Overschrijven?"
        ):
            return

        self.status_var.set("Bezig met opslaan…")
        self.update_idletasks()
        try:
            result = apply_changes(source, target, dict(self.pending))
        except Exception as exc:
            self.status_var.set("Opslaan mislukt.")
            messagebox.showerror(APP_TITLE, f"Opslaan mislukt:\n\n{exc}")
            return

        self.pending.clear()
        self._refresh_edit_state()
        written = str(result["output"])
        actions = "\n".join(f"  • {a}" for a in result["actions"])
        messagebox.showinfo(APP_TITLE, f"Opgeslagen in:\n{written}\n\n{actions}")
        self.load(written)

    def _copy_row(self, _event: object = None) -> None:
        item = self.tree.focus()
        if not item:
            return
        tag = self.tree.item(item, "text").removeprefix("⚠︎ ")
        values = self.tree.item(item, "values")
        line = f"{tag}: {values[0]}" if values and values[0] else tag
        self.clipboard_clear()
        self.clipboard_append(line)
        self.status_var.set(f"Gekopieerd: {line[:70]}")

    def copy_json(self) -> None:
        if not self.path:
            return
        payload = {
            "file": self.path,
            "exiftool": self.exif,
            "remove_ai_marks": self.skill_report,
            "raw_markers": self.raw_markers,
            "deep_inspection": self.deep_report,
        }
        self.clipboard_clear()
        self.clipboard_append(json.dumps(payload, indent=2, ensure_ascii=False))
        self.status_var.set("Volledige metadata als JSON naar klembord gekopieerd.")


def main() -> None:
    # Circuit breaker: a helper subprocess must never turn into a second GUI.
    # Without this, one bad interpreter path multiplies windows without bound.
    if os.environ.get(CHILD_MARKER):
        print("Metadata Viewer: gestart als hulpproces, geen venster geopend.",
              file=sys.stderr)
        return

    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("1000x640")
    app = MetadataViewer(root)

    root.bind("<Command-o>", lambda _e: app.open_dialog())
    root.bind("<Control-o>", lambda _e: app.open_dialog())

    # Files dropped on the Dock icon / opened via Finder on macOS.
    try:
        root.createcommand("::tk::mac::OpenDocument", lambda *paths: app.load(paths[0]))
    except tk.TclError:
        pass

    if len(sys.argv) > 1:
        app.load(sys.argv[1])

    root.mainloop()


if __name__ == "__main__":
    main()
