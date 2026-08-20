#!/usr/bin/env python3
"""Deep inspection helpers for the Metadata Viewer.

This module complements exiftool by:

* Reading C2PA / JUMBF / Content Credentials manifest data.
* Detecting simple steganography: trailing data, embedded files/signatures,
  LSB anomalies, whitespace channels, and encoded payloads.
* Inspecting text for invisible Unicode, BiDi controls and unusual whitespace.

Everything here is read-only. It deliberately uses only the Python stdlib
plus Pillow (which the viewer already uses for previews), so the module can be
imported from the main GUI without dragging in heavy ML packages.

Important scope statement: this is NOT a magic AI-watermark detector. The
statistical text watermarks used by Claude, OpenAI-style providers and
SynthID-Text require the provider's secret key and tokenizer to test. We do
report the presence of C2PA metadata and raw JUMBF structures, and flag
heuristic text artifacts, but we cannot confirm or refute a vendor-specific
statistical watermark without their keys.
"""

from __future__ import annotations

import binascii
import json
import math
import os
import re
import struct
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator


# --------------------------------------------------------------------- C2PA

C2PA_MANIFEST_BOX = b"c2ma"           # JUMBF superbox label for manifest store
C2PA_MANIFEST_BOX_ALT = b"c2pa"       # some early encoders use this spelling
C2PA_SIG_BOX = b"c2as"                # claim signature
C2PA_CLAIM_BOX = b"c2cl"              # V1 claim
C2PA_CLAIM_BOX_V2 = b"c2cs"           # V2 claim
C2PA_ASSERTION_BOX = b"c2a"           # assertion store prefix (c2a, c2a2, ...)

JUMBF_SIGNATURE = b"\x00\x00\x00\x0C\x6A\x75\x6D\x62\x20\x6A\x6D\x62\x32"
#              box length (12)  j  u  m  b     j  m  b  2


def _read_box(stream: BinaryIO, offset: int) -> tuple[bytes, int] | None:
    """Read one JUMBF / ISO box from *offset*. Returns (payload, next_offset)."""
    stream.seek(offset)
    head = stream.read(8)
    if len(head) < 8:
        return None
    length = struct.unpack(">I", head[:4])[0]
    box_type = head[4:8]
    if length == 0:
        length = os.fstat(stream.fileno()).st_size - offset
    elif length == 1:
        ext = stream.read(8)
        if len(ext) < 8:
            return None
        length = struct.unpack(">Q", ext)[0] - 16
        header_size = 16
    else:
        header_size = 8
        if length < header_size:
            return None
    payload_size = length - header_size
    if payload_size < 0:
        return None
    payload = stream.read(payload_size)
    if len(payload) < payload_size:
        return None
    return box_type + payload, offset + length


def _jumbf_box_labels(stream: BinaryIO, start: int, end: int) -> dict[bytes, int]:
    """Map known C2PA/JUMBF superbox labels to their absolute file offsets."""
    labels: dict[bytes, int] = {}
    offset = start
    while offset < end:
        stream.seek(offset)
        head = stream.read(8)
        if len(head) < 8:
            break
        length = struct.unpack(">I", head[:4])[0]
        if length < 8:
            break
        box_type = head[4:8]
        if box_type == b"jumb":
            # JUMBF superbox: skip 8-byte header, then description box.
            desc_head = stream.read(8)
            if len(desc_head) < 8:
                break
            desc_len = struct.unpack(">I", desc_head[:4])[0]
            desc_type = desc_head[4:8]
            if desc_type == b"jdcb":
                # Description box: toggle (1), label length, label, id, ...
                rest = stream.read(desc_len - 8)
                if len(rest) >= 1:
                    toggle = rest[0]
                    label_len = rest[1]
                    label = rest[2:2 + label_len]
                    if label and label in {C2PA_MANIFEST_BOX, C2PA_MANIFEST_BOX_ALT,
                                            C2PA_SIG_BOX, C2PA_CLAIM_BOX,
                                            C2PA_CLAIM_BOX_V2, b"c2pa.assertions"}:
                        labels[label] = offset
        offset += length
    return labels


def _find_jumbf_boxes(stream: BinaryIO, total: int) -> dict[bytes, int]:
    """Search the whole file for JUMBF superboxes (used for PNG, TIFF, etc.)."""
    labels: dict[bytes, int] = {}
    # Heuristic: find every 'jumb' signature.
    data = stream.read()
    for m in re.finditer(b"jumb", data):
        # We need a valid superbox just before. Read from m.start()-4 backwards
        # until we see a plausible length. Limit scan.
        off = max(0, m.start() - 64)
        while off < m.start() + 4:
            length_bytes = data[off:off + 4]
            if len(length_bytes) < 4:
                break
            length = struct.unpack(">I", length_bytes)[0]
            if length >= 8 and off + length <= total and data[off + 4:off + 8] == b"jumb":
                desc_offset = off + 8
                desc_head = data[desc_offset:desc_offset + 8]
                if len(desc_head) == 8 and desc_head[4:8] == b"jdcb":
                    desc_len = struct.unpack(">I", desc_head[:4])[0]
                    rest = data[desc_offset + 8:desc_offset + desc_len]
                    if len(rest) >= 2:
                        label_len = rest[1]
                        label = rest[2:2 + label_len]
                        if label:
                            labels.setdefault(label, off)
            off += 1
    return labels


def _extract_cbor_strings(blob: bytes) -> Iterator[str]:
    """Cheaply pull UTF-8 strings out of a CBOR blob without a CBOR library."""
    # Look for definite-length text strings (0x60-0x77 short, 0x78 length-prefixed,
    # 0x79 2-byte length). This misses indefinite-length strings, but those are
    # rare in C2PA deterministic encoding.
    i = 0
    while i < len(blob):
        tag = blob[i]
        if 0x60 <= tag <= 0x77:
            length = tag - 0x60
            i += 1
            yield blob[i:i + length].decode("utf-8", "replace")
            i += length
        elif tag == 0x78:
            if i + 1 >= len(blob):
                break
            length = blob[i + 1]
            i += 2
            yield blob[i:i + length].decode("utf-8", "replace")
            i += length
        elif tag == 0x79:
            if i + 2 >= len(blob):
                break
            length = struct.unpack(">H", blob[i + 1:i + 3])[0]
            i += 3
            yield blob[i:i + length].decode("utf-8", "replace")
            i += length
        elif tag == 0x7F:  # indefinite text: scan for break 0xFF
            i += 1
            parts: list[bytes] = []
            while i < len(blob) and blob[i] != 0xFF:
                if 0x60 <= blob[i] <= 0x77:
                    length = blob[i] - 0x60
                    i += 1
                    parts.append(blob[i:i + length])
                    i += length
                else:
                    break
            yield b"".join(parts).decode("utf-8", "replace")
            i += 1
        else:
            i += 1


def _read_jumbf_payload(stream: BinaryIO, offset: int) -> bytes | None:
    """Read the content of a JUMBF superbox, skipping the description box."""
    stream.seek(offset)
    head = stream.read(8)
    if len(head) < 8 or head[4:8] != b"jumb":
        return None
    length = struct.unpack(">I", head[:4])[0]
    if length < 8:
        return None
    payload = stream.read(length - 8)
    # First child is the description box; skip it.
    if len(payload) < 8:
        return None
    desc_len = struct.unpack(">I", payload[:4])[0]
    if desc_len < 8 or desc_len > len(payload):
        return None
    return payload[desc_len:]


def inspect_c2pa(path: str) -> dict[str, object]:
    """Return a plain dict with C2PA manifest facts, or empty when none found."""
    result: dict[str, object] = {"present": False, "manifests": [], "errors": []}
    try:
        total = os.path.getsize(path)
        with open(path, "rb") as stream:
            data = stream.read(min(total, 4 * 1024 * 1024))
            has_jumbf = JUMBF_SIGNATURE in data or b"jumb" in data
            has_c2pa_text = any(
                m in data.lower()
                for m in (b"c2pa.assertions", b"c2pa.claim", b"urn:c2pa", b"contentcredentials")
            )
            if not has_jumbf and not has_c2pa_text:
                return result

            # Try the official SDK first if available (it validates + parses).
            try:
                import c2pa  # type: ignore
                with open(path, "rb") as f:
                    reader = c2pa.Reader.try_create(path)
                    if reader:
                        store = json.loads(reader.json())
                        result["sdk"] = "c2pa-python"
                        result["present"] = True
                        result["store"] = store
                        active = store.get("active_manifest")
                        for label, manifest in store.get("manifests", {}).items():
                            summary = _summarise_manifest(manifest, is_active=(label == active))
                            result["manifests"].append(summary)
                        return result
            except Exception as exc:
                result["errors"].append(f"c2pa-python kon niet lezen: {exc}")

            # Fallback: raw JUMBF/CBOR harvest.
            labels = _find_jumbf_boxes(stream, total)
            if not labels:
                # Last attempt: scan raw markers only.
                result["present"] = has_c2pa_text
                result["raw_only"] = True
                result["findings"] = list(
                    set(_raw_c2pa_markers(stream, total))
                )
                return result

            result["present"] = True
            result["raw_only"] = True
            result["labels"] = {k.decode("latin-1", "replace"): v for k, v in labels.items()}
            for label, offset in labels.items():
                payload = _read_jumbf_payload(stream, offset) or b""
                strings = list(_extract_cbor_strings(payload))[:80]
                result["manifests"].append({
                    "label": label.decode("latin-1", "replace"),
                    "offset": offset,
                    "strings": strings,
                    "digital_source_types": _extract_digital_source_types(strings),
                    "software_agents": _extract_software_agents(strings),
                })
                result.setdefault("findings", []).extend(
                    f"label '{label.decode('latin-1', 'replace')}': {s[:80]}"
                    for s in strings[:5]
                )
    except Exception as exc:
        result["errors"].append(str(exc))
    return result


def _raw_c2pa_markers(stream: BinaryIO, total: int) -> list[str]:
    """Return human labels for raw C2PA/JUMBF strings found in the bytes."""
    markers: list[str] = []
    stream.seek(0)
    blob = stream.read(min(total, 8 * 1024 * 1024)).lower()
    if b"jumb" in blob:
        markers.append("JUMBF-superbox gevonden")
    if b"c2pa.assertions" in blob:
        markers.append("C2PA-asserties gevonden")
    if b"c2pa.claim" in blob:
        markers.append("C2PA-claim gevonden")
    if b"urn:c2pa" in blob:
        markers.append("C2PA-identificatie (URN) gevonden")
    if b"contentcredentials" in blob:
        markers.append("Content Credentials-string gevonden")
    if b"trainedalgorithmicmedia" in blob:
        markers.append("C2PA: 'gemaakt door AI-model'")
    if b"compositewithtrainedalgorithmicmedia" in blob:
        markers.append("C2PA: 'deels AI-gegenereerd'")
    return markers


def _summarise_manifest(manifest: dict, is_active: bool) -> dict[str, object]:
    """Flatten the SDK manifest JSON into something displayable."""
    claim = manifest.get("claim") or manifest.get("claim_v2") or {}
    actions = []
    for assertion in manifest.get("assertions", []):
        data = assertion.get("data", {})
        if "actions" in data:
            actions.extend(data["actions"])
    return {
        "active": is_active,
        "label": manifest.get("label", ""),
        "claim_generator": claim.get("claim_generator", ""),
        "claim_generator_info": claim.get("claim_generator_info", []),
        "date": claim.get("date", ""),
        "digital_source_type": _extract_digital_source_types_from_manifest(manifest),
        "actions": actions[:20],
        "ingredients": [
            {
                "title": ing.get("title", ""),
                "format": ing.get("format", ""),
                "relationship": ing.get("relationship", ""),
            }
            for ing in manifest.get("ingredients", [])[:10]
        ],
    }


def _extract_digital_source_types(strings: list[str]) -> list[str]:
    """Find source-type URIs in a list of strings."""
    out: list[str] = []
    for s in strings:
        if "trainedAlgorithmicMedia" in s or "compositeWithTrainedAlgorithmicMedia" in s:
            out.append(s)
    return out


def _extract_digital_source_types_from_manifest(manifest: dict) -> list[str]:
    """Harvest source-type URIs from a parsed SDK manifest."""
    out: list[str] = []
    for assertion in manifest.get("assertions", []):
        data = assertion.get("data", {})
        for action in data.get("actions", []):
            dst = action.get("digitalSourceType")
            if dst:
                out.append(dst)
    return out


def _extract_software_agents(strings: list[str]) -> list[str]:
    """Pull likely software-agent names out of raw strings."""
    agents: list[str] = []
    for s in strings:
        if any(v in s.lower() for v in ("adobe", "photoshop", "lightroom",
                                         "generative", "firefly", "openai",
                                         "dall-e", "midjourney", "stable diffusion")):
            agents.append(s)
    return agents[:10]


# ------------------------------------------------------------------ stego

# Magic bytes for files commonly appended inside or to other files.
FILE_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"PK\x03\x04", "zip", "ZIP / Office / OpenDocument"),
    (b"PK\x05\x06", "zip_empty", "lege ZIP"),
    (b"Rar!", "rar", "RAR"),
    (b"7z\xBC\xAF\x27\x1C", "7z", "7-Zip"),
    (b"\x89PNG\r\n\x1a\n", "png", "PNG"),
    (b"\xFF\xD8\xFF", "jpeg", "JPEG"),
    (b"GIF87a", "gif", "GIF"),
    (b"GIF89a", "gif", "GIF"),
    (b"RIFF", "riff", "RIFF (WAV/AVI/WebP)"),
    (b"%PDF-", "pdf", "PDF"),
    (b"\x1A\x45\xDF\xA3", "mkv", "Matroska/WebM"),
    (b"ftyp", "mp4", "ISO-base media / MP4"),
    (b"\x00\x00\x00 ftyp", "mp4", "ISO-base media / MP4"),
    (b"\x00\x00\x00\x18ftyp", "mp4", "ISO-base media / MP4"),
    (b"ID3", "mp3", "MP3 ID3"),
    (b"OggS", "ogg", "OGG"),
    (b"fLaC", "flac", "FLAC"),
    (b"MZ", "exe", "Windows executable"),
    (b"\x7fELF", "elf", "ELF executable"),
    (b"\xCF\xFA\xED\xFE", "macho", "Mach-O"),
    (b"\xFE\xED\xFA\xCF", "macho", "Mach-O"),
)

# End-of-file markers for the formats we know; data after these is suspicious.
EOF_MARKERS: dict[str, bytes] = {
    "png": b"\x49\x45\x4E\x44\xAE\x42\x60\x82",
    "jpeg": b"\xFF\xD9",
    "gif": b"\x00\x3B",
    "riff": b"\x00\x00\x00\x00",  # placeholder; handled specially
    "pdf": b"%%EOF",
    "mp3": b"",  # handled by ID3 / audio frame scan
}


def _file_type_from_suffix(path: str) -> str:
    return Path(path).suffix.lower().lstrip(".")


def find_embedded_files(path: str, limit: int = 50) -> list[dict[str, object]]:
    """Scan for file signatures embedded anywhere in the file.

    Treats the file's own natural header as structure, not as an embedded
    foreign object. For ZIP-based documents (Office, ODF, EPUB) the internal
    ZIP entries are also structure, so only non-ZIP signatures are reported.
    """
    hits: list[dict[str, object]] = []
    try:
        with open(path, "rb") as stream:
            data = stream.read()
    except OSError:
        return hits
    own_kind = _file_type_from_suffix(path)

    # Files whose whole format *is* a ZIP archive: their internal entries are
    # not steganographic embeddings.
    zip_based = own_kind in {"docx", "xlsx", "pptx", "odt", "ods", "odp", "odg", "epub", "zip"}

    first_offset = None
    if own_kind in {"png", "jpeg", "jpg", "gif", "bmp", "webp", "tiff", "tif",
                    "wav", "mp3", "flac", "ogg", "opus", "m4a", "mp4", "mov", "pdf"}:
        for sig, kind, _desc in FILE_SIGNATURES:
            if kind == own_kind:
                idx = data.find(sig)
                if idx != -1:
                    first_offset = idx
                    break
        if first_offset is None and own_kind in {"jpeg", "jpg"}:
            first_offset = data.find(b"\xFF\xD8\xFF")
        if first_offset is None and own_kind == "png":
            first_offset = data.find(b"\x89PNG\r\n\x1a\n")

    for sig, kind, desc in FILE_SIGNATURES:
        if kind == own_kind:
            start = data.find(sig)
            if first_offset is not None and start == first_offset:
                offset = data.find(sig, start + len(sig))
            else:
                offset = start
        else:
            offset = data.find(sig)
        seen = 0
        while offset != -1 and seen < limit:
            if kind in {"mp4"} and offset >= 4:
                length = struct.unpack(">I", data[offset - 4:offset])[0]
                if length < 8 or length > len(data) - offset + 4:
                    offset = data.find(sig, offset + 1)
                    continue
            hits.append({"offset": offset, "kind": kind, "description": desc})
            seen += 1
            offset = data.find(sig, offset + 1)

    hits.sort(key=lambda h: h["offset"])
    deduped: list[dict[str, object]] = []
    for h in hits:
        if deduped and h["offset"] <= deduped[-1]["offset"]:
            continue
        if deduped and h["offset"] < deduped[-1]["offset"] + 22:
            if h["kind"] == deduped[-1]["kind"]:
                continue
            if h["kind"] == "zip_empty":
                continue
            if len(h["description"]) <= len(deduped[-1]["description"]):
                continue
        if h["kind"] == "zip_empty":
            nearest_zip = next(
                (d for d in deduped if d["kind"] == "zip" and h["offset"] - d["offset"] < 512),
                None,
            )
            if nearest_zip:
                continue
        # For ZIP-based documents, ZIP signatures are internal structure.
        if zip_based and h["kind"] in {"zip", "zip_empty"}:
            continue
        deduped.append(h)
    return deduped


def find_trailing_data(path: str) -> dict[str, object] | None:
    """Detect data appended after the natural end of a known container."""
    try:
        total = os.path.getsize(path)
        with open(path, "rb") as stream:
            data = stream.read(min(total, 4 * 1024 * 1024))
    except OSError:
        return None
    suffix = _file_type_from_suffix(path)
    trailer_start: int | None = None
    if suffix == "png":
        idx = data.rfind(EOF_MARKERS["png"])
        if idx != -1:
            trailer_start = idx + len(EOF_MARKERS["png"])
    elif suffix in {"jpg", "jpeg"}:
        # JPEG can legitimately have trailing data, but report it.
        idx = data.rfind(EOF_MARKERS["jpeg"])
        if idx != -1:
            trailer_start = idx + 2
    elif suffix == "gif":
        idx = data.rfind(EOF_MARKERS["gif"])
        if idx != -1:
            trailer_start = idx + 1
    elif suffix == "pdf":
        idx = data.rfind(EOF_MARKERS["pdf"])
        if idx != -1:
            trailer_start = idx + len(EOF_MARKERS["pdf"])
    elif suffix == "wav":
        # RIFF size field at offset 4 should match actual file.
        if len(data) >= 12 and data[:4] == b"RIFF":
            declared = struct.unpack("<I", data[4:8])[0] + 8
            if declared < total:
                trailer_start = declared

    if trailer_start is not None and trailer_start < total:
        # Read trailer in full.
        with open(path, "rb") as stream:
            stream.seek(trailer_start)
            trailer = stream.read(min(total - trailer_start, 1 * 1024 * 1024))
        preview = trailer[:200]
        return {
            "offset": trailer_start,
            "size": total - trailer_start,
            "text_preview": _bytes_preview(preview),
            "has_text": _contains_printable_text(trailer),
            "entropy": _shannon_entropy(trailer),
        }
    return None


def _bytes_preview(blob: bytes, max_len: int = 80) -> str:
    if all(32 <= b < 127 or b in (9, 10, 13) for b in blob):
        return blob.decode("ascii", "replace")[:max_len]
    hexed = binascii.hexlify(blob[:max_len // 2]).decode("ascii")
    return hexed[:max_len]


def _contains_printable_text(blob: bytes, threshold: float = 0.8) -> bool:
    if not blob:
        return False
    text_count = sum(1 for b in blob if 32 <= b < 127 or b in (9, 10, 13))
    return text_count / len(blob) >= threshold


def _longest_printable_run(blob: bytes) -> str:
    """Return the longest ASCII printable run inside a byte blob."""
    best = ""
    current = ""
    for b in blob:
        if 32 <= b < 127:
            current += chr(b)
        else:
            if len(current) > len(best):
                best = current
            current = ""
    if len(current) > len(best):
        best = current
    return best


def _shannon_entropy(blob: bytes) -> float:
    if not blob:
        return 0.0
    counts = [0] * 256
    for b in blob:
        counts[b] += 1
    total = len(blob)
    entropy = 0.0
    for c in counts:
        if c:
            p = c / total
            entropy -= p * math.log2(p)
    return entropy


def inspect_lsb(path: str) -> dict[str, object]:
    """Very basic LSB steganography heuristics for lossless images."""
    result: dict[str, object] = {"analysed": False, "note": ""}
    suffix = Path(path).suffix.lower()
    if suffix not in {".png", ".bmp", ".tiff", ".tif", ".gif", ".webp"}:
        result["note"] = "LSB-analyse is alleen zinvol voor lossless beeldformaten."
        return result
    try:
        from PIL import Image
        with Image.open(path) as img:
            if img.mode not in {"RGB", "RGBA", "L", "P"}:
                result["note"] = f"Kleurenmodus {img.mode} wordt niet geanalyseerd."
                return result
            # Convert to RGB to simplify.
            rgb = img.convert("RGB")
            pixels = list(rgb.getdata())
            # Extract LSB per channel; report each channel separately.
            channels = {
                "R": [r & 1 for r, _g, _b in pixels],
                "G": [g & 1 for _r, g, _b in pixels],
                "B": [b & 1 for _r, _g, b in pixels],
            }
            # For overall statistics use all channels flattened.
            flat_lsbs = [bit for ch_bits in channels.values() for bit in ch_bits]
            ones = sum(flat_lsbs)
            zeros = len(flat_lsbs) - ones
            expected = len(flat_lsbs) / 2
            if expected == 0:
                return result
            chi = ((ones - expected) ** 2 / expected) + ((zeros - expected) ** 2 / expected)

            # Try to find a printable text preview in any channel.
            best_preview = ""
            text_like = False
            for ch_name, bits in channels.items():
                raw_bits = bits[:256 * 8]
                preview = bytes(
                    sum(bit << (7 - j) for j, bit in enumerate(raw_bits[i:i + 8]))
                    for i in range(0, len(raw_bits), 8)
                )
                run = _longest_printable_run(preview)
                # Require a longer run to reduce noise on random LSBs.
                if len(run) >= 8:
                    best_preview = run
                    text_like = True
                    break
            if not text_like:
                # fallback: first 64 bytes from all channels flattened
                raw_bits = flat_lsbs[:64 * 8]
                bytes_preview = bytes(
                    sum(bit << (7 - j) for j, bit in enumerate(raw_bits[i:i + 8]))
                    for i in range(0, len(raw_bits), 8)
                )
                best_preview = _bytes_preview(bytes_preview)

            balance = abs(ones - zeros) / len(flat_lsbs) if flat_lsbs else 0.0
            if balance > 0.25:
                note = (
                    "De LSB-verdeling is sterk scheef, waarschijnlijk door grote "
                    "uniforme vlakken. Deze test is dan geen betrouwbaar steganogram."
                )
            elif balance < 0.05:
                note = (
                    "LSB-bits zijn vrijwel gelijk verdeeld; dat kan op naïeve "
                    "steganografie duiden, maar is ook normaal voor ruis."
                )
            else:
                note = "LSB-bits zijn lichtjes scheef; waarschijnlijk natuurlijke ruis."

            result.update({
                "analysed": True,
                "pixels": len(pixels),
                "lsb_ones": ones,
                "lsb_zeros": zeros,
                "chi_square": round(chi, 2),
                "text_preview": best_preview,
                "text_like": text_like,
                "note": note,
            })
    except Exception as exc:
        result["note"] = f"LSB-analyse mislukt: {exc}"
    return result


def inspect_text_file(path: str) -> dict[str, object]:
    """Look for encoded payloads, invisible characters and whitespace channels."""
    result: dict[str, object] = {
        "encoding_payloads": [],
        "invisible_unicode": [],
        "whitespace_channel": None,
        "mixed_indentation": False,
    }
    try:
        with open(path, "rb") as stream:
            raw = stream.read(min(os.path.getsize(path), 2 * 1024 * 1024))
    except OSError:
        return result

    text = raw.decode("utf-8", "replace")
    lines = text.splitlines()

    # Invisible Unicode.
    invisible = _INVISIBLE_CPS
    for i, line in enumerate(lines, start=1):
        for cp in invisible:
            if chr(cp) in line:
                result["invisible_unicode"].append({"line": i, "cp": cp, "name": _cp_name(cp)})
    # Cap to keep the UI sane.
    result["invisible_unicode"] = result["invisible_unicode"][:50]

    # Whitespace at line endings (stegsnow-style).
    endings = [line[len(line.rstrip()):] for line in lines if line.rstrip() != line]
    if endings:
        spaces = sum(e.count(" ") for e in endings)
        tabs = sum(e.count("\t") for e in endings)
        result["whitespace_channel"] = {
            "lines_with_trailing": len(endings),
            "trailing_spaces": spaces,
            "trailing_tabs": tabs,
            "hint": "trailing whitespace kan een steganografisch kanaal zijn (stegsnow).",
        }

    # Mixed indentation in code-like files.
    indent_lines = [line for line in lines if line.startswith((" ", "\t"))]
    has_spaces = any(l.startswith(" ") for l in indent_lines)
    has_tabs = any(l.startswith("\t") for l in indent_lines)
    result["mixed_indentation"] = has_spaces and has_tabs and len(indent_lines) > 5

    # Encoded payloads in the whole text.
    result["encoding_payloads"] = _find_encoded_payloads(text)

    return result


# Unicode categories we consider invisible or control-like.
_INVISIBLE_CPS: tuple[int, ...] = (
    0x00AD,  # soft hyphen
    0x034F,  # combining grapheme joiner
    0x061C,  # Arabic letter mark
    0x115F, 0x1160,  # Hangul choseong filler / jungseong filler
    0x17B4, 0x17B5,  # Khmer vowel inherent AQ / AA
    0x180B, 0x180C, 0x180D, 0x180E,  # Mongolian free variation selectors / vowel separator
    0x200B, 0x200C, 0x200D, 0x200E, 0x200F,  # ZWSP, ZWNJ, ZWJ, LRM, RLM
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,  # BiDi embeddings / overrides / PDF
    0x2060, 0x2061, 0x2062, 0x2063, 0x2064,  # word joiner, invisible ops
    0x2066, 0x2067, 0x2068, 0x2069,  # isolate BiDi controls
    0x3164,  # Hangul filler
    0xFE00, 0xFE01, 0xFE02, 0xFE03, 0xFE04, 0xFE05, 0xFE06, 0xFE07,
    0xFE08, 0xFE09, 0xFE0A, 0xFE0B, 0xFE0C, 0xFE0D, 0xFE0E, 0xFE0F,  # variation selectors
    0xFEFF,  # BOM / ZWNBSP
    0xFFA0,  # halfwidth Hangul filler
    0xFFF0, 0xFFF1, 0xFFF2, 0xFFF3, 0xFFF4, 0xFFF5, 0xFFF6, 0xFFF7,
    0xFFF8,  # interlinear annotation / object replacement chars
    0x1D159, 0x1D15A, 0x1D15B, 0x1D15C, 0x1D15D, 0x1D15E, 0x1D15F,
    0x1D160, 0x1D161, 0x1D162, 0x1D163, 0x1D164, 0x1D165, 0x1D166,
    0x1D167, 0x1D168, 0x1D169, 0x1D16A, 0x1D16B, 0x1D16C, 0x1D16D,
    0x1D16E, 0x1D16F, 0x1D170, 0x1D171, 0x1D172, 0x1D173, 0x1D174,
    0x1D175, 0x1D176, 0x1D177, 0x1D178, 0x1D179, 0x1D17A,
    0xE0000, 0xE0001,  # tags block
    0xE0020, 0xE007F,  # tag space .. cancel tag (range endpoints)
    0xE0100, 0xE01EF,  # variation selectors supplement
)


def _cp_name(cp: int) -> str:
    try:
        return f"U+{cp:04X} {unicodedata.name(chr(cp))}"
    except Exception:
        return f"U+{cp:04X}"


def _find_encoded_payloads(text: str) -> list[dict[str, object]]:
    """Spot plausible Base64/hex/percent-encoded blobs and zero-width payloads."""
    hits: list[dict[str, object]] = []
    # Base64 blocks (URL-safe or normal).
    for pat in (
        r"[A-Za-z0-9+/]{40,}={0,2}",
        r"[A-Za-z0-9_-]{40,}",
    ):
        for m in re.finditer(pat, text):
            snippet = m.group(0)
            if len(snippet) % 4 == 0:
                try:
                    decoded = base64.b64decode(snippet, validate=True)
                    if _contains_printable_text(decoded, threshold=0.7):
                        hits.append({
                            "kind": "base64_text",
                            "position": m.start(),
                            "length": len(snippet),
                            "preview": _bytes_preview(decoded, 60),
                        })
                        if len(hits) >= 10:
                            return hits
                except Exception:
                    pass
    # Hex blobs: 0xAA... or long hex runs.
    for m in re.finditer(r"(?:0x|\b)([0-9a-fA-F]{32,})(?:\b|\Z)", text):
        blob = bytes.fromhex(m.group(1))
        if _contains_printable_text(blob, threshold=0.7):
            hits.append({
                "kind": "hex_text",
                "position": m.start(),
                "length": len(m.group(1)),
                "preview": _bytes_preview(blob, 60),
            })
            if len(hits) >= 10:
                return hits
    # URL percent-encoding runs.
    for m in re.finditer(r"(?:%[0-9a-fA-F]{2}){12,}", text):
        raw = urllib.parse.unquote(m.group(0))
        if _contains_printable_text(raw.encode("utf-8", "replace"), threshold=0.7):
            hits.append({
                "kind": "percent_encoded",
                "position": m.start(),
                "length": len(m.group(0)),
                "preview": raw[:60],
            })
            if len(hits) >= 10:
                return hits
    return hits


# ------------------------------------------------------------------ wrapper

@dataclass
class DeepInspectionResult:
    c2pa: dict[str, object] = field(default_factory=dict)
    stego: dict[str, object] = field(default_factory=dict)
    text: dict[str, object] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def inspect(path: str) -> DeepInspectionResult:
    """Run every read-only deep inspection available for *path*."""
    result = DeepInspectionResult()
    try:
        result.c2pa = inspect_c2pa(path)
        result.stego = {
            "embedded_files": find_embedded_files(path),
            "trailing_data": find_trailing_data(path),
            "lsb": inspect_lsb(path),
        }
        kind = _file_type_from_suffix(path)
        if kind in {"txt", "md", "py", "js", "json", "xml", "html", "css", "csv", "log"}:
            result.text = inspect_text_file(path)
    except Exception as exc:
        result.errors.append(str(exc))
    return result


def summary(result: DeepInspectionResult) -> list[str]:
    """Human-readable Dutch summary lines for the left-hand notes panel."""
    lines: list[str] = []
    c2pa = result.c2pa
    if c2pa.get("present"):
        if c2pa.get("sdk"):
            lines.append("Content Credentials (C2PA) gedetecteerd en geparsed.")
        else:
            lines.append("C2PA / JUMBF-sporen gevonden (raw scan).")
        for m in c2pa.get("manifests", []):
            label = m.get("label", "")
            active = " (actief)" if m.get("active") else ""
            lines.append(f"  • manifest {label}{active}")
            dst = m.get("digital_source_type") or m.get("digital_source_types")
            if dst:
                if isinstance(dst, list):
                    for d in dst[:2]:
                        lines.append(f"    bron: {d}")
                else:
                    lines.append(f"    bron: {dst}")
        for finding in c2pa.get("findings", [])[:6]:
            lines.append(f"  • {finding}")
    else:
        lines.append("Geen C2PA / Content Credentials gevonden.")

    stego = result.stego
    embedded = stego.get("embedded_files", [])
    if embedded:
        lines.append("")
        lines.append("Ingebedde bestanden/signatures:")
        for h in embedded[:8]:
            lines.append(f"  • {h['description']} @ 0x{h['offset']:X}")
    trailer = stego.get("trailing_data")
    if trailer:
        lines.append("")
        lines.append(
            f"Data na het formateinde: {trailer['size']} bytes vanaf 0x{trailer['offset']:X}"
        )
        if trailer.get("has_text"):
            lines.append(f"  tekst-voorbeeld: {trailer.get('text_preview', '')[:80]}")
        lines.append(f"  entropie: {trailer.get('entropy', 0):.2f} bits/byte")

    lsb = stego.get("lsb", {})
    if lsb.get("analysed"):
        lines.append("")
        lines.append("LSB-analyse (lossless afbeelding):")
        lines.append(f"  pixels: {lsb['pixels']} — LSB 1/0: {lsb['lsb_ones']}/{lsb['lsb_zeros']}")
        lines.append(f"  chi²: {lsb['chi_square']}; {lsb['note']}")
        if lsb.get("text_like"):
            lines.append(f"  tekst in LSB: {lsb['text_preview'][:60]}")

    text = result.text
    if text:
        inv = text.get("invisible_unicode", [])
        if inv:
            lines.append("")
            lines.append(f"Onzichtbare Unicode-tekens: {len(inv)} keer")
            by_name: dict[str, int] = {}
            for hit in inv:
                by_name[hit["name"]] = by_name.get(hit["name"], 0) + 1
            for name, count in sorted(by_name.items(), key=lambda kv: -kv[1])[:6]:
                lines.append(f"  • {name} ×{count}")
        ws = text.get("whitespace_channel")
        if ws and ws.get("lines_with_trailing"):
            lines.append("")
            lines.append(
                f"Trailing whitespace op {ws['lines_with_trailing']} regels "
                f"({ws['trailing_spaces']} spaties, {ws['trailing_tabs']} tabs)"
            )
            lines.append(f"  ({ws['hint']})")
        payloads = text.get("encoding_payloads", [])
        if payloads:
            lines.append("")
            lines.append("Gecodeerde payload-kandidaten:")
            for p in payloads[:6]:
                lines.append(f"  • {p['kind']} @ {p['position']}: {p['preview'][:50]}")
        if text.get("mixed_indentation"):
            lines.append("")
            lines.append("Gemengde tabs/spaties indentatie (kan steganografie zijn).")

    if result.errors:
        lines.append("")
        lines.append("Fouten tijdens diepe inspectie:")
        for err in result.errors[:5]:
            lines.append(f"  • {err}")

    return lines


# Late imports (so module loads even if these are unavailable).
try:
    import base64
    import unicodedata
    import urllib.parse
except Exception:
    base64 = None  # type: ignore
    unicodedata = None  # type: ignore
    urllib = None  # type: ignore


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        res = inspect(arg)
        print(f"=== {arg} ===")
        for line in summary(res):
            print(line)
