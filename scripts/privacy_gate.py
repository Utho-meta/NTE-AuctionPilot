#!/usr/bin/env python3
"""Inspect public payloads, including nested ZIP/PyInstaller archives and metadata.

No archive member or Python bytecode is executed. Human content review is required
and bound to the exact recursive payload inventory; regex checks alone are not a
claim that arbitrary personal information can be recognized automatically.
"""
import argparse
import base64
from collections import Counter
import hashlib
import io
import json
import marshal
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import tempfile
import types
import zipfile
import zlib


class PrivacyError(RuntimeError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


PATTERNS = {
    "private-local-path": re.compile(rb"(?i)(?:[a-z]:[\\/]+(?:Users|Documents and Settings|CodexProjects|CodexData|AuctionPilot)[\\/]|/(?:Users|home)/[a-z0-9_.-]+/|\\\\[^\\\s]+\\(?:Users|home)\\)"),
    "private-page-link": re.compile(rb"(?i)https?://(?:chatgpt\.com/(?:space/|c/)|[^/\s]+\.sharepoint\.com/personal/|pan\.baidu\.com/s/)"),
    "credential": re.compile(rb"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|sk-proj-[A-Za-z0-9_-]{40,}|sk-[A-Za-z0-9]{48}(?![A-Za-z0-9])|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|AKIA[0-9A-Z]{16})"),
    "private-account-field": re.compile(rb'(?i)["\'](?:player_uid|game_uid|account_id|account_name|user_nickname|author_email|access_token|refresh_token|api_key|password)["\']\s*[:=]\s*["\'][^"\'\s]{3,}'),
}
PRIVATE_NAMES = re.compile(r"(?i)(?:^|/)(?:复核记录|内部记录|交接记录|\.git|\.env|events\.jsonl|auction-pilot\.json|settings\.json|user-settings\.json|inventory\.json|.*\.snk|.*\.pdb|.*\.log|.*\.bak)(?:/|$)")
ALLOWED = {".dll", ".exe", ".pyd", ".lib", ".onnx", ".png", ".ico", ".jpg", ".jpeg", ".webp", ".gif", ".json", ".txt", ".md", ".html", ".htm", ".csv", ".srt", ".zip", ".mp4", ".pyc", ".pyz"}
TEXT = {".json", ".txt", ".md", ".html", ".htm", ".csv", ".srt"}
PNG_META = {b"tEXt", b"iTXt", b"zTXt", b"eXIf"}
MEI = b"MEI\x0c\x0b\x0a\x0b\x0e"
# Reproducible/public vendor CI paths are not personal workstation locations.
# The independent private-local-path rule still applies inside these prefixes.
PUBLIC_BUILD_PATH = re.compile(r"^(?:/_/|[CD]:/(?:a/_work/\d+/|__w/\d+/)|N:/_work/\d+/|D:/a/opencvsharp/opencvsharp/|C:/b/abs_[a-z0-9]+/croot/python-split_\d+/work/|D:/bld/openssl_split_\d+/work/)")


def pe_debug(data):
    """Return CodeView paths and byte ranges, without loading a Windows binary."""
    if data[:2] != b"MZ":
        return []
    try:
        pe = struct.unpack_from("<I", data, 60)[0]
        if data[pe:pe+4] != b"PE\0\0":
            raise ValueError("invalid PE header")
        sections, opt_size = struct.unpack_from("<H12xH", data, pe + 6)
        opt = pe + 24
        magic = struct.unpack_from("<H", data, opt)[0]
        directories = opt + (112 if magic == 0x20b else 96)
        debug_rva, debug_size = struct.unpack_from("<II", data, directories + 6 * 8)
        headers = opt + opt_size
        def offset(rva):
            for i in range(sections):
                vsize, va, rawsize, raw = struct.unpack_from("<IIII", data, headers + 40*i + 8)
                if va <= rva < va + max(vsize, rawsize):
                    return raw + rva - va
            raise ValueError("unmapped debug RVA")
        result = []
        if debug_rva:
            start = offset(debug_rva)
            for pos in range(start, start + debug_size, 28):
                kind, size, rva, raw = struct.unpack_from("<IIII", data, pos + 12)
                if kind == 2:
                    record = data[raw:raw+size]
                    skip = 24 if record[:4] == b"RSDS" else 16 if record[:4] == b"NB10" else None
                    if skip is None:
                        raise ValueError("unrecognized CodeView format")
                    path = record[skip:].split(b"\0", 1)[0].decode("utf-8", "strict")
                    result.append({"path": path, "offset": raw+skip, "capacity": size-skip})
        return result
    except (ValueError, struct.error, UnicodeError) as e:
        raise PrivacyError("Unparseable PE debug directory: " + str(e)) from e


class Scanner:
    def __init__(self):
        self.inventory = []
        self.findings = []
        self.counts = Counter()
        self.total = 0
        self.string_cache = {}

    def fail(self, name, rule):
        # Findings identify files/rules, never echo the private matched value.
        item = {"file": name, "rule": rule}
        if item not in self.findings:
            self.findings.append(item)

    def strings(self, name, data):
        key = sha(data)
        if key in self.string_cache:
            for rule in self.string_cache[key]:
                self.fail(name, rule)
            return
        # Decode UTF-16 without concatenating independent NUL-terminated strings.
        # Concatenation makes locale names such as sk-SK look like credentials.
        variants = [data]
        for pattern in (rb"(?:[\x20-\x7e]\x00){4,}", rb"(?:\x00[\x20-\x7e]){4,}"):
            variants.append(b"\n".join(m[0].replace(b"\0", b"") for m in re.finditer(pattern, data)))
        if b"\\" in data:
            variants.append(data.replace(b"\\\\", b"\\"))
        matched = []
        for rule, pattern in PATTERNS.items():
            if any(pattern.search(v) for v in variants):
                self.fail(name, rule)
                matched.append(rule)
        self.string_cache[key] = matched

    def zip(self, name, data, depth):
        self.counts["zip_archives"] += 1
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
                if len(names) != len(set(names)):
                    self.fail(name, "duplicate-archive-member")
                if archive.comment:
                    self.fail(name, "archive-comment")
                for info in archive.infolist():
                    n = info.filename.replace("\\", "/")
                    if n.startswith("/") or ".." in PurePosixPath(n).parts or re.match(r"^[A-Za-z]:", n):
                        self.fail(name, "unsafe-archive-path")
                    if info.comment or info.extra:
                        self.fail(name + "!" + n, "archive-member-metadata")
                    if info.flag_bits & 1:
                        raise PrivacyError("Encrypted archive cannot be inspected")
                    if info.is_dir():
                        continue
                    if info.file_size > 1_000_000_000:
                        raise PrivacyError("Archive member exceeds inspection limit")
                    self.visit(name + "!" + n, archive.read(info), depth + 1)
        except (zipfile.BadZipFile, RuntimeError) as e:
            raise PrivacyError("ZIP inspection failed: " + name) from e

    def carchive(self, name, data, depth):
        pos = data.rfind(MEI)
        if pos < 0:
            return
        # A real cookie must extend to EOF (or precede a PE Authenticode trailer).
        if pos + 88 > len(data):
            return
        self.counts["pyinstaller_archives"] += 1
        _, length, toc_offset, toc_size, pyver, pylib = struct.unpack_from("!8sIIII64s", data, pos)
        start = pos + 88 - length
        if start < 0 or start+toc_offset+toc_size > pos:
            raise PrivacyError("Invalid PyInstaller archive bounds")
        cursor, end = start+toc_offset, start+toc_offset+toc_size
        while cursor < end:
            entry_len, off, size, usize, compressed, kind = struct.unpack_from("!IIIIBc", data, cursor)
            if entry_len < 18 or cursor+entry_len > end or start+off+size > cursor:
                raise PrivacyError("Invalid PyInstaller TOC entry")
            label = data[cursor+18:cursor+entry_len].split(b"\0", 1)[0].decode("utf-8")
            blob = data[start+off:start+off+size]
            blob = zlib.decompress(blob) if compressed else blob
            if len(blob) != usize:
                raise PrivacyError("PyInstaller member size mismatch")
            self.visit(name + "!" + label, blob, depth+1, embedded=True)
            cursor += entry_len

    def pyz(self, name, data, depth):
        self.counts["python_module_archives"] += 1
        toc_offset = struct.unpack_from("!I", data, 8)[0]
        toc = marshal.loads(data[toc_offset:])
        for label, (kind, off, size) in (toc.items() if isinstance(toc, dict) else toc):
            if size:
                blob = zlib.decompress(data[off:off+size])
                self.visit(name+"!"+label, blob, depth+1, embedded=True)

    def png(self, name, data):
        pos = 8
        while pos < len(data):
            length = struct.unpack_from("!I", data, pos)[0]
            kind = data[pos+4:pos+8]
            if pos+12+length > len(data):
                raise PrivacyError("Malformed PNG")
            if kind in PNG_META:
                self.fail(name, "image-metadata-"+kind.decode())
            pos += 12+length
            if kind == b"IEND":
                if pos != len(data):
                    self.fail(name, "image-trailing-payload")
                return
        raise PrivacyError("PNG missing IEND")

    def video(self, name, data):
        ffprobe = os.environ.get("FFPROBE") or shutil.which("ffprobe")
        if not ffprobe:
            raise PrivacyError("ffprobe is required to inspect all video metadata")
        with tempfile.TemporaryDirectory(prefix="privacy-media-") as directory:
            path = Path(directory)/"media.mp4"
            path.write_bytes(data)
            result = subprocess.run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-show_chapters", "-of", "json", str(path)], capture_output=True, check=True)
            metadata = json.loads(result.stdout)
        allowed = {"major_brand", "minor_version", "compatible_brands", "encoder", "language", "handler_name", "vendor_id"}
        for part in [metadata.get("format", {})] + metadata.get("streams", []) + metadata.get("chapters", []):
            tags = part.get("tags", {})
            if set(tags) - allowed:
                self.fail(name, "unapproved-media-metadata")
            self.strings(name, json.dumps(tags, ensure_ascii=False).encode())
            if part.get("codec_type") not in (None, "audio", "video"):
                self.fail(name, "unreviewed-media-data-stream")
        if metadata.get("chapters"):
            self.fail(name, "media-chapter-metadata")
        self.counts["video_metadata"] += 1

    def visit(self, name, data, depth=0, embedded=False):
        if depth > 12:
            raise PrivacyError("Maximum nested archive depth exceeded")
        self.total += len(data)
        if self.total > 6_000_000_000:
            raise PrivacyError("Maximum recursive inspection size exceeded")
        self.inventory.append([name, len(data), sha(data)])
        self.counts["files"] += 1
        self.strings(name, name.encode())
        self.strings(name, data)
        simple = name.rsplit("!", 1)[-1]
        ext = PurePosixPath(simple).suffix.lower()
        # Arena's shipped model defaults are not a user's settings file. Their
        # exact contents remain subject to string scanning and hash-bound review.
        model_defaults = simple.endswith("02-Arena模拟器/Data/Environment/settings.json")
        if PRIVATE_NAMES.search(simple) and not model_defaults:
            self.fail(name, "private-record-or-debug-file")
        if not embedded and ext not in ALLOWED:
            self.fail(name, "unsupported-payload-type")
        if data.startswith(b"PK\x03\x04") or ext == ".zip":
            self.zip(name, data, depth)
        elif data.startswith(b"PYZ\0"):
            self.pyz(name, data, depth)
        elif data.startswith(b"MZ"):
            self.counts["pe_files"] += 1
            for debug in pe_debug(data):
                self.counts["debug_records"] += 1
                path = debug["path"].replace("\\", "/")
                if (re.match(r"^[a-zA-Z]:/", path) or path.startswith("/")) and not PUBLIC_BUILD_PATH.match(path):
                    self.fail(name, "absolute-debug-symbol-path")
            self.carchive(name, data, depth)
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            self.counts["png_images"] += 1
            self.png(name, data)
        elif ext == ".mp4":
            self.video(name, data)
        elif ext in {".jpg", ".jpeg", ".webp"}:
            if any(m in data for m in (b"Exif\0", b"http://ns.adobe.com/xap/", b"EXIF", b"XMP ")):
                self.fail(name, "image-metadata")
        if ext in TEXT:
            self.counts["text_files"] += 1
            text = data.decode("utf-8-sig", "strict")
            if ext == ".json":
                parsed = json.loads(text)
                self.strings(name, json.dumps(parsed, ensure_ascii=False).encode())
            for i, match in enumerate(re.finditer(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)", text)):
                self.visit(name+f"!inline-{i}."+match[1], base64.b64decode(match[2], validate=True), depth+1)

    def result(self):
        rows = sorted(self.inventory)
        fingerprint = sha(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode())
        return {"schema": 1, "passed": not self.findings, "payload_inventory_sha256": fingerprint,
                "counts": dict(self.counts), "inspected_bytes": self.total, "findings": self.findings}


def scan_archive(path):
    scanner = Scanner()
    # Outer filename is deliberately excluded; content/member names are bound.
    scanner.zip("payload", Path(path).read_bytes(), 0)
    return scanner.result()


def require_review(result, review):
    if not result["passed"]:
        raise PrivacyError("Privacy scan failed: " + json.dumps(result["findings"], ensure_ascii=False))
    if review.get("payload_inventory_sha256") != result["payload_inventory_sha256"]:
        raise PrivacyError("Content changed since human privacy review; review the current payload")
    for topic in ("public_text_and_data", "images_and_video_frames", "spoken_audio_and_subtitles", "binary_and_archive_metadata", "licenses_preserved"):
        if review.get("reviewed", {}).get(topic) is not True:
            raise PrivacyError("Missing human privacy review: " + topic)
    return dict(result, human_review_matches=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("archive")
    p.add_argument("--report", required=True)
    args = p.parse_args()
    try:
        result = scan_archive(args.archive)
        Path(args.report).write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["passed"] else 1)
    except Exception as error:
        Path(args.report).write_text(json.dumps({"passed": False, "error": str(error)}, ensure_ascii=False), encoding="utf-8")
        raise
