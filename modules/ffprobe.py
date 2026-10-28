# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import os
import re
import subprocess
import traceback
from pathlib import Path
from typing import Any, Optional

from .constants import APP_ROOT, FFMPEG_REGISTRY_PATH, APP_VERSION
from .datetime_helpers import utc_now_o
from .file_io import read_json_file, write_json_file_atomically
from .interrupts import register_active_process, unregister_active_process
from .logger import sha256_file, write_sampler_log
from .platform_api import basename, ffprobe_path, normcase, normalize_path, path_parts, splitext, subprocess_process_options

FFPROBE_VERSION: Optional[str] = None
LAST_HEALTH_ERROR: Optional[str] = None


def _load_ffmpeg_registry() -> dict[str, Any]:
    try:
        raw = read_json_file(FFMPEG_REGISTRY_PATH)
        if isinstance(raw, dict) and isinstance(raw.get("Entries"), list):
            return raw
    except Exception:
        pass
    return {"ProgramVersion": APP_VERSION, "RegistrySchemaVersion": 1, "Entries": []}


def _save_ffmpeg_registry(registry: dict[str, Any]) -> None:
    registry["ProgramVersion"] = APP_VERSION
    registry["RegistrySchemaVersion"] = 1
    write_json_file_atomically(FFMPEG_REGISTRY_PATH, registry)


def _ffprobe_registry_entry(path: str) -> tuple[dict[str, Any], dict[str, Any], bool]:
    registry = _load_ffmpeg_registry()
    st = os.stat(path)
    key = normcase(normalize_path(path))
    entries = registry["Entries"]
    entry = next((x for x in entries if normcase(str(x.get("Path", ""))) == key), None)
    same = bool(entry and int(entry.get("Size", -1)) == int(st.st_size) and int(entry.get("MTimeNs", -1)) == int(st.st_mtime_ns))
    if entry is None:
        entry = {"Path": path, "FirstSeenAtUtc": utc_now_o()}
        entries.append(entry)
    entry["Path"] = path
    entry["Size"] = int(st.st_size)
    entry["MTimeNs"] = int(st.st_mtime_ns)
    entry["LastSeenAtUtc"] = utc_now_o()
    return registry, entry, same


def _run_tracked_process(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **subprocess_process_options(),
    )
    register_active_process(process)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(args, int(process.returncode or 0), stdout, stderr)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        finally:
            stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr)
    finally:
        unregister_active_process(process)


def get_ffprobe_path() -> Optional[str]:
    path = ffprobe_path(APP_ROOT)
    return str(path) if path.is_file() else None


def expected_ffprobe_path() -> str:
    return str(ffprobe_path(APP_ROOT))


def test_ffprobe_health(config: Optional[dict[str, Any]] = None) -> Optional[str]:
    global FFPROBE_VERSION, LAST_HEALTH_ERROR
    FFPROBE_VERSION = None
    LAST_HEALTH_ERROR = None
    path = get_ffprobe_path()
    if not path:
        LAST_HEALTH_ERROR = "MISSING"
        write_sampler_log(f"FFprobe required for scanning but not found expected={expected_ffprobe_path()}", "ERROR", "FFPROBE")
        return None
    try:
        registry, entry, same = _ffprobe_registry_entry(path)
        if same and str(entry.get("FullVersion") or "").strip():
            FFPROBE_VERSION = str(entry["FullVersion"])
            first_line = FFPROBE_VERSION.splitlines()[0].strip()
            short_match = re.match(r"(?i)^ffprobe\s+version\s+([^\s]+)", first_line)
            changed = False
            if not str(entry.get("ShortVersion") or "").strip():
                entry["ShortVersion"] = short_match.group(1) if short_match else first_line
                changed = True
            if not str(entry.get("SHA256") or "").strip():
                entry["SHA256"] = sha256_file(Path(path))
                changed = True
            if not str(entry.get("ValidatedAtUtc") or "").strip():
                entry["ValidatedAtUtc"] = utc_now_o()
                changed = True
            _save_ffmpeg_registry(registry)
            write_sampler_log(
                f"Runtime identity unchanged; registry version reused path={path} version={FFPROBE_VERSION}"
                + (" metadata_completed=true" if changed else ""),
                "INFO", "FFPROBE",
            )
            return path

        timeout = int(config["FFprobeTimeoutSeconds"]) if config else 30
        cp = _run_tracked_process([path, "-version"], timeout)
        if cp.returncode != 0 or not cp.stdout.strip():
            raise RuntimeError(f"ffprobe -version failed: {cp.stderr.strip()}")

        first_line = cp.stdout.splitlines()[0].strip()
        short_match = re.match(r"(?i)^ffprobe\s+version\s+([^\s]+)", first_line)
        FFPROBE_VERSION = first_line
        entry["ShortVersion"] = short_match.group(1) if short_match else first_line
        entry["FullVersion"] = first_line
        entry["SHA256"] = sha256_file(Path(path))
        entry["ValidatedAtUtc"] = utc_now_o()
        _save_ffmpeg_registry(registry)
        write_sampler_log(f"Runtime validated path={path} version={FFPROBE_VERSION} sha256={entry['SHA256']}", "INFO", "FFPROBE")
        return path
    except Exception:
        LAST_HEALTH_ERROR = "FAILED"
        write_sampler_log(traceback.format_exc(), "ERROR", "FFPROBE")
        return None


def test_sample_or_trailer_path(path: str) -> bool:
    segments = path_parts(path)
    if any(re.fullmatch(r"(?i:sample|samples|trailer|trailers)", s) for s in segments):
        return True
    name = splitext(basename(path))[0]
    return bool(re.search(r"(?i)(?:^|[\s._\-\[\(])(?:sample|samples|trailer|trailers)[\]\)]?$", name))


def probe_media_json(ffprobe_executable: str, path: str, timeout_seconds: int) -> dict[str, Any]:
    """Run one FFprobe attempt and return parsed primary-video metadata or a stable failure."""
    try:
        cp = _run_tracked_process([
            ffprobe_executable, "-v", "error", "-print_format", "json",
            "-show_streams", "-show_format", "-select_streams", "v", path,
        ], timeout_seconds)
    except subprocess.TimeoutExpired:
        return {"ProbeStatus": "ProbeTimeout", "ProbeErrorKind": "Timeout", "ProbeErrorMessage": f"ffprobe exceeded {timeout_seconds} seconds.", "ProbeData": None}
    except Exception as exc:
        return {"ProbeStatus": "ProbeFailed", "ProbeErrorKind": "WorkerException", "ProbeErrorMessage": str(exc), "ProbeData": None}
    if cp.returncode != 0:
        return {"ProbeStatus": "ProbeFailed", "ProbeErrorKind": "FFprobeError", "ProbeErrorMessage": f"Exit code {cp.returncode}: {cp.stderr.strip() or 'ffprobe returned a non-zero exit code.'}", "ProbeData": None}
    if not cp.stdout.strip():
        return {"ProbeStatus": "ProbeFailed", "ProbeErrorKind": "EmptyOutput", "ProbeErrorMessage": "ffprobe returned no JSON output.", "ProbeData": None}
    try:
        parsed = __import__("json").loads(cp.stdout)
    except Exception as exc:
        return {"ProbeStatus": "ProbeFailed", "ProbeErrorKind": "InvalidJson", "ProbeErrorMessage": str(exc), "ProbeData": None}
    usable = []
    for stream in parsed.get("streams") or []:
        if str(stream.get("codec_type", "")).lower() != "video":
            continue
        if int((stream.get("disposition") or {}).get("attached_pic", 0) or 0) != 0:
            continue
        usable.append(stream)
    if not usable:
        return {"ProbeStatus": "ProbeFailed", "ProbeErrorKind": "NoUsableVideo", "ProbeErrorMessage": "No non-attached video stream was found.", "ProbeData": None}
    main = next((x for x in usable if int((x.get("disposition") or {}).get("default", 0) or 0) != 0), usable[0])
    disp = main.get("disposition") or {}
    fmt = parsed.get("format") or {}
    data = {
        "Stream": {
            "Index": main.get("index"), "CodecName": main.get("codec_name"), "CodecLongName": main.get("codec_long_name"),
            "CodecTag": main.get("codec_tag"), "CodecTagString": main.get("codec_tag_string"), "Profile": main.get("profile"),
            "Level": main.get("level"), "Width": main.get("width"), "Height": main.get("height"), "CodedWidth": main.get("coded_width"),
            "CodedHeight": main.get("coded_height"), "PixelFormat": main.get("pix_fmt"), "BitsPerRawSample": main.get("bits_per_raw_sample"),
            "AverageFrameRate": main.get("avg_frame_rate"), "RealFrameRate": main.get("r_frame_rate"), "SampleAspectRatio": main.get("sample_aspect_ratio"),
            "DisplayAspectRatio": main.get("display_aspect_ratio"), "FieldOrder": main.get("field_order"), "Duration": main.get("duration"),
            "BitRate": main.get("bit_rate"), "ColorRange": main.get("color_range"), "ColorSpace": main.get("color_space"),
            "ColorTransfer": main.get("color_transfer"), "ColorPrimaries": main.get("color_primaries"), "DispositionDefault": disp.get("default"),
            "DispositionAttachedPicture": disp.get("attached_pic"), "Tags": main.get("tags"), "SideDataList": list(main.get("side_data_list") or []),
        },
        "Format": {
            "FormatName": fmt.get("format_name"), "FormatLongName": fmt.get("format_long_name"), "Duration": fmt.get("duration"),
            "Size": fmt.get("size"), "BitRate": fmt.get("bit_rate"), "Tags": fmt.get("tags"),
        },
    }
    return {"ProbeStatus": "Success", "ProbeErrorKind": None, "ProbeErrorMessage": None, "ProbeData": data}
