# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import math
import os
import re
import time
from collections import Counter
from typing import Any, Iterable, Optional

from .logger import format_elapsed, write_sampler_log

PIXEL_FORMAT_INFO: dict[str, tuple[int, str]] = {
    'yuv420p': (8, '4:2:0'), 'yuyv422': (8, '4:2:2'), 'rgb24': (8, 'RGB'),
    'bgr24': (8, 'RGB'), 'yuv422p': (8, '4:2:2'), 'yuv444p': (8, '4:4:4'),
    'yuv410p': (8, '4:1:0'), 'yuv411p': (8, '4:1:1'), 'gray': (8, 'Gray'),
    'monow': (1, 'Gray'), 'monob': (1, 'Gray'), 'yuvj420p': (8, '4:2:0'),
    'yuvj422p': (8, '4:2:2'), 'yuvj444p': (8, '4:4:4'), 'uyvy422': (8, '4:2:2'),
    'uyyvyy411': (8, '4:1:1'), 'bgr8': (0, 'RGB'), 'bgr4': (0, 'RGB'),
    'bgr4_byte': (0, 'RGB'), 'rgb8': (0, 'RGB'), 'rgb4': (0, 'RGB'),
    'rgb4_byte': (0, 'RGB'), 'nv12': (8, '4:2:0'), 'nv21': (8, '4:2:0'),
    'argb': (8, 'RGB'), 'rgba': (8, 'RGB'), 'abgr': (8, 'RGB'), 'bgra': (8, 'RGB'),
    'gray16be': (16, 'Gray'), 'gray16le': (16, 'Gray'), 'yuv440p': (8, '4:4:0'),
    'yuvj440p': (8, '4:4:0'), 'yuva420p': (8, '4:2:0'), 'rgb48be': (16, 'RGB'),
    'rgb48le': (16, 'RGB'), 'rgb565be': (0, 'RGB'), 'rgb565le': (0, 'RGB'),
    'rgb555be': (5, 'RGB'), 'rgb555le': (5, 'RGB'), 'bgr565be': (0, 'RGB'),
    'bgr565le': (0, 'RGB'), 'bgr555be': (5, 'RGB'), 'bgr555le': (5, 'RGB'),
    'yuv420p16le': (16, '4:2:0'), 'yuv420p16be': (16, '4:2:0'),
    'yuv422p16le': (16, '4:2:2'), 'yuv422p16be': (16, '4:2:2'),
    'yuv444p16le': (16, '4:4:4'), 'yuv444p16be': (16, '4:4:4'),
    'rgb444le': (4, 'RGB'), 'rgb444be': (4, 'RGB'), 'bgr444le': (4, 'RGB'),
    'bgr444be': (4, 'RGB'), 'ya8': (8, 'Gray'), 'bgr48be': (16, 'RGB'),
    'bgr48le': (16, 'RGB'), 'yuv420p9be': (9, '4:2:0'), 'yuv420p9le': (9, '4:2:0'),
    'yuv420p10be': (10, '4:2:0'), 'yuv420p10le': (10, '4:2:0'),
    'yuv422p10be': (10, '4:2:2'), 'yuv422p10le': (10, '4:2:2'),
    'yuv444p9be': (9, '4:4:4'), 'yuv444p9le': (9, '4:4:4'),
    'yuv444p10be': (10, '4:4:4'), 'yuv444p10le': (10, '4:4:4'),
    'yuv422p9be': (9, '4:2:2'), 'yuv422p9le': (9, '4:2:2'), 'gbrp': (8, 'RGB'),
    'gbrp9be': (9, 'RGB'), 'gbrp9le': (9, 'RGB'), 'gbrp10be': (10, 'RGB'),
    'gbrp10le': (10, 'RGB'), 'gbrp16be': (16, 'RGB'), 'gbrp16le': (16, 'RGB'),
    'yuva422p': (8, '4:2:2'), 'yuva444p': (8, '4:4:4'),
    'yuva420p9be': (9, '4:2:0'), 'yuva420p9le': (9, '4:2:0'),
    'yuva422p9be': (9, '4:2:2'), 'yuva422p9le': (9, '4:2:2'),
    'yuva444p9be': (9, '4:4:4'), 'yuva444p9le': (9, '4:4:4'),
    'yuva420p10be': (10, '4:2:0'), 'yuva420p10le': (10, '4:2:0'),
    'yuva422p10be': (10, '4:2:2'), 'yuva422p10le': (10, '4:2:2'),
    'yuva444p10be': (10, '4:4:4'), 'yuva444p10le': (10, '4:4:4'),
    'yuva420p16be': (16, '4:2:0'), 'yuva420p16le': (16, '4:2:0'),
    'yuva422p16be': (16, '4:2:2'), 'yuva422p16le': (16, '4:2:2'),
    'yuva444p16be': (16, '4:4:4'), 'yuva444p16le': (16, '4:4:4'),
    'xyz12le': (12, 'Unknown'), 'xyz12be': (12, 'Unknown'), 'nv16': (8, '4:2:2'),
    'nv20le': (10, '4:2:2'), 'nv20be': (10, '4:2:2'), 'rgba64be': (16, 'RGB'),
    'rgba64le': (16, 'RGB'), 'bgra64be': (16, 'RGB'), 'bgra64le': (16, 'RGB'),
    'yvyu422': (8, '4:2:2'), 'ya16be': (16, 'Gray'), 'ya16le': (16, 'Gray'),
    'gbrap': (8, 'RGB'), 'gbrap16be': (16, 'RGB'), 'gbrap16le': (16, 'RGB'),
    '0rgb': (8, 'RGB'), 'rgb0': (8, 'RGB'), '0bgr': (8, 'RGB'), 'bgr0': (8, 'RGB'),
    'yuv420p12be': (12, '4:2:0'), 'yuv420p12le': (12, '4:2:0'),
    'yuv420p14be': (14, '4:2:0'), 'yuv420p14le': (14, '4:2:0'),
    'yuv422p12be': (12, '4:2:2'), 'yuv422p12le': (12, '4:2:2'),
    'yuv422p14be': (14, '4:2:2'), 'yuv422p14le': (14, '4:2:2'),
    'yuv444p12be': (12, '4:4:4'), 'yuv444p12le': (12, '4:4:4'),
    'yuv444p14be': (14, '4:4:4'), 'yuv444p14le': (14, '4:4:4'),
    'gbrp12be': (12, 'RGB'), 'gbrp12le': (12, 'RGB'), 'gbrp14be': (14, 'RGB'),
    'gbrp14le': (14, 'RGB'), 'yuvj411p': (8, '4:1:1'), 'yuv440p10le': (10, '4:4:0'),
    'yuv440p10be': (10, '4:4:0'), 'yuv440p12le': (12, '4:4:0'),
    'yuv440p12be': (12, '4:4:0'), 'ayuv64le': (16, '4:4:4'),
    'ayuv64be': (16, '4:4:4'), 'p010le': (10, '4:2:0'), 'p010be': (10, '4:2:0'),
    'gbrap12be': (12, 'RGB'), 'gbrap12le': (12, 'RGB'), 'gbrap10be': (10, 'RGB'),
    'gbrap10le': (10, 'RGB'), 'gray12be': (12, 'Gray'), 'gray12le': (12, 'Gray'),
    'gray10be': (10, 'Gray'), 'gray10le': (10, 'Gray'), 'p016le': (16, '4:2:0'),
    'p016be': (16, '4:2:0'), 'gray9be': (9, 'Gray'), 'gray9le': (9, 'Gray'),
    'gbrpf32be': (32, 'RGB'), 'gbrpf32le': (32, 'RGB'), 'gbrapf32be': (32, 'RGB'),
    'gbrapf32le': (32, 'RGB'), 'gray14be': (14, 'Gray'), 'gray14le': (14, 'Gray'),
    'grayf32be': (32, 'Gray'), 'grayf32le': (32, 'Gray'),
    'yuva422p12be': (12, '4:2:2'), 'yuva422p12le': (12, '4:2:2'),
    'yuva444p12be': (12, '4:4:4'), 'yuva444p12le': (12, '4:4:4'),
    'nv24': (8, '4:4:4'), 'nv42': (8, '4:4:4'), 'y210be': (10, '4:2:2'),
    'y210le': (10, '4:2:2'), 'x2rgb10le': (10, 'RGB'), 'x2rgb10be': (10, 'RGB'),
    'x2bgr10le': (10, 'RGB'), 'x2bgr10be': (10, 'RGB'), 'p210be': (10, '4:2:2'),
    'p210le': (10, '4:2:2'), 'p410be': (10, '4:4:4'), 'p410le': (10, '4:4:4'),
    'p216be': (16, '4:2:2'), 'p216le': (16, '4:2:2'), 'p416be': (16, '4:4:4'),
    'p416le': (16, '4:4:4'), 'vuya': (8, '4:4:4'), 'rgbaf16be': (16, 'RGB'),
    'rgbaf16le': (16, 'RGB'), 'vuyx': (8, '4:4:4'), 'p012le': (12, '4:2:0'),
    'p012be': (12, '4:2:0'), 'y212be': (12, '4:2:2'), 'y212le': (12, '4:2:2'),
    'xv30be': (10, '4:4:4'), 'xv30le': (10, '4:4:4'), 'xv36be': (12, '4:4:4'),
    'xv36le': (12, '4:4:4'), 'rgbf32be': (32, 'RGB'), 'rgbf32le': (32, 'RGB'),
    'rgbaf32be': (32, 'RGB'), 'rgbaf32le': (32, 'RGB'), 'p212be': (12, '4:2:2'),
    'p212le': (12, '4:2:2'), 'p412be': (12, '4:4:4'), 'p412le': (12, '4:4:4'),
    'gbrap14be': (14, 'RGB'), 'gbrap14le': (14, 'RGB'),
}

def nullable_double(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "n/a":
        return None
    try:
        number = float(text)
        return number if math.isfinite(number) else None
    except ValueError:
        return None


def nullable_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        text = str(value).strip()
        return int(text) if re.fullmatch(r"[+-]?\d+", text) else None
    except Exception:
        return None


def rational_to_double(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"n/a", "0/0"}:
        return None
    if "/" in text:
        parts = text.split("/")
        if len(parts) == 2:
            n, d = nullable_double(parts[0]), nullable_double(parts[1])
            if n is not None and d is not None and abs(d) > 0.0000001:
                return n / d
        return None
    return nullable_double(text)


def normalized_codec(codec: Optional[str]) -> Optional[str]:
    if not codec or not str(codec).strip():
        return None
    raw = str(codec).strip()
    mapping = {
        "h264": "H264", "hevc": "HEVC", "h265": "HEVC", "av1": "AV1",
        "vp9": "VP9", "vp8": "VP8", "mpeg2video": "MPEG2", "mpeg4": "MPEG4",
        "vc1": "VC1", "wmv3": "WMV3", "theora": "Theora", "prores": "ProRes",
        "dnxhd": "DNxHD/DNxHR",
    }
    return mapping.get(raw.lower(), raw)


def resolution_class(width: Optional[int], height: Optional[int]) -> Optional[str]:
    if width is None or height is None or width <= 0 or height <= 0:
        return None
    long_side, short_side = max(width, height), min(width, height)
    if long_side > 8192 or short_side > 4320: return ">8K"
    if long_side >= 7000 or short_side >= 4000: return "8K"
    if long_side >= 3600 or short_side >= 2000: return "4K"
    if long_side >= 2500 or short_side >= 1300: return "1440p"
    if long_side >= 1800 or short_side >= 900: return "1080p"
    if long_side >= 1200 or short_side >= 650: return "720p"
    return "SD"


def bit_depth(bits_per_raw_sample: Any, pixel_format: Optional[str]) -> Optional[int]:
    explicit = nullable_int(bits_per_raw_sample)
    if explicit is not None and (explicit <= 0 or explicit > 64):
        explicit = None
    descriptor = None
    if pixel_format and pixel_format.lower() in PIXEL_FORMAT_INFO:
        value = int(PIXEL_FORMAT_INFO[pixel_format.lower()][0])
        descriptor = value if value > 0 else None
    if explicit is not None and descriptor is not None and explicit > descriptor:
        return None
    return explicit if explicit is not None else descriptor


def chroma_class(pixel_format: Optional[str]) -> Optional[str]:
    if pixel_format and pixel_format.lower() in PIXEL_FORMAT_INFO:
        value = PIXEL_FORMAT_INFO[pixel_format.lower()][1]
        return None if str(value).casefold() == "unknown" else value
    return None


_FPS_STANDARDS = [("23.976/24", (23.976, 24.0)), ("25", (25.0,)), ("29.97/30", (29.97, 30.0)), ("50", (50.0,)), ("59.94/60", (59.94, 60.0))]
_FPS_TOLERANCE = 0.10
_FPS_QUANTIZED_TOLERANCE = 0.05


def _standard_fps_class(fps: float) -> Optional[str]:
    for name, values in _FPS_STANDARDS:
        for target in values:
            if abs(fps - target) <= _FPS_TOLERANCE:
                return name
            # Containers with 1 ms time stamps (Matroska) store the frame duration rounded to whole
            # milliseconds, and FFprobe then reports that rounded rate: 42 ms -> 23.81 fps for 23.976/24,
            # 33 ms -> 30.30 for 29.97/30, 17 ms -> 58.82 for 59.94/60.
            if abs(fps - 1000.0 / round(1000.0 / target)) <= _FPS_QUANTIZED_TOLERANCE:
                return name
    return None


def fps_class(fps: Optional[float]) -> Optional[str]:
    if fps is None or fps <= 0:
        return None
    standard = _standard_fps_class(fps)
    if standard is not None:
        return standard
    if fps <= 15.0: return "<=15"
    if fps > 60.0: return ">60"
    return "Other"


def scan_type(field_order: Optional[str]) -> Optional[str]:
    if not field_order or not str(field_order).strip():
        return None
    value = str(field_order).strip().lower()
    if value == "progressive": return "Progressive"
    if value in {"tt", "bb", "tb", "bt"}: return "Interlaced"
    return None


def duration_class(seconds: Optional[float]) -> Optional[str]:
    if seconds is None or seconds <= 0:
        return None
    minutes = seconds / 60.0
    if minutes < 20: return "<20 min"
    if minutes < 60: return "20-<60 min"
    if minutes < 120: return "60-<120 min"
    return ">=120 min"


def aspect_ratio_class(width: Optional[int], height: Optional[int], display: Optional[str]) -> Optional[str]:
    ratio = rational_to_double(str(display or "").replace(":", "/"))
    if ratio is None or ratio <= 0:
        if width is None or height is None or width <= 0 or height <= 0:
            return None
        ratio = width / height
    if ratio < 1.0 / 1.05: return "Portrait"
    if abs(ratio - 4.0/3.0) <= 0.08: return "4:3"
    if abs(ratio - 16.0/9.0) <= 0.08: return "16:9"
    if ratio >= 1.95: return "Cinema/Wide"
    return "Other"


_MOV_FAMILY_EXTENSIONS = {".mov": "mov", ".m4v": "m4v", ".3gp": "3gp", ".3g2": "3g2", ".mp4": "mp4"}


_MP4_BRAND_PREFIXES = ("iso", "mp4", "mp7", "avc", "dash", "cmf", "msnv", "f4v", "mmp4", "xavc", "nd")


def _mov_family_class(major_brand: Any, extension: Optional[str]) -> Optional[str]:
    # FFprobe reports one format name ("mov,mp4,m4a,3gp,3g2,mj2") for the whole family, so the
    # major brand (or, without a usable brand, the file extension) tells MP4, QuickTime, M4V and 3GP apart.
    # When neither says which one it is, the class stays unknown instead of being guessed.
    brand = str(major_brand or "").strip().lower()
    if brand == "qt": return "mov"
    if brand == "m4v": return "m4v"
    if brand.startswith("3gp"): return "3gp"
    if brand.startswith("3g2"): return "3g2"
    if brand.startswith(_MP4_BRAND_PREFIXES): return "mp4"
    return _MOV_FAMILY_EXTENSIONS.get(str(extension or "").lower())


def container_class(format_name: Optional[str], major_brand: Any = None, extension: Optional[str] = None) -> Optional[str]:
    if not format_name or not str(format_name).strip():
        return None
    first = str(format_name).split(",")[0].strip().lower()
    if first == "mov":
        return _mov_family_class(major_brand, extension)
    return first or None


def _format_major_brand(tags: Any) -> Optional[str]:
    if not isinstance(tags, dict):
        return None
    for key, value in tags.items():
        if str(key).lower() == "major_brand":
            return None if value is None else str(value)
    return None


def bitrate_info(stream_rate: Any, format_rate: Any, file_size: int, duration: Optional[float]) -> dict[str, Any]:
    stream = nullable_double(stream_rate)
    if stream is not None and stream > 0: return {"BitsPerSecond": stream, "Source": "stream"}
    fmt = nullable_double(format_rate)
    if fmt is not None and fmt > 0: return {"BitsPerSecond": fmt, "Source": "container"}
    if duration is not None and duration > 0 and file_size > 0: return {"BitsPerSecond": file_size * 8.0 / duration, "Source": "estimated"}
    return {"BitsPerSecond": None, "Source": None}


def side_data_numbers(block: dict[str, Any], names: Iterable[str]) -> bool:
    usable = False
    for name in names:
        value = block.get(name)
        if value is None: continue
        number = rational_to_double(str(value))
        if number is not None and number >= 0: usable = True
    return usable


def hdr_signals(media: dict[str, Any]) -> dict[str, Any]:
    transfer = str(media.get("ColorTransfer", "")).lower()
    pq = transfer in {"smpte2084", "smpte2084 (pq)", "pq"}
    hlg = transfer in {"arib-std-b67", "hlg"}
    dv = None; hdr10plus = False; mastering = False; cll = False
    for block in media.get("SideDataList") or []:
        if not isinstance(block, dict): continue
        type_ = str(block.get("side_data_type", "")).lower()
        if type_ == "dovi configuration record":
            if side_data_numbers(block, ("dv_profile", "dv_version_major", "rpu_present_flag")): dv = block
        elif type_ == "hdr dynamic metadata smpte2094-40 (hdr10+)":
            if side_data_numbers(block, ("application_version", "num_windows", "targeted_system_display_maximum_luminance")): hdr10plus = True
        elif type_ == "mastering display metadata":
            mastering = side_data_numbers(block, ("red_x", "green_x", "blue_x", "white_point_x", "min_luminance", "max_luminance"))
        elif type_ == "content light level metadata":
            cll = side_data_numbers(block, ("max_content", "max_average"))
    return {"PQ": pq, "HLG": hlg, "DolbyVision": dv is not None, "DolbyVisionConfig": dv, "HDR10Plus": hdr10plus, "MasteringMetadata": mastering, "ContentLightMetadata": cll, "CredibleHDR": pq or hlg or dv is not None or hdr10plus or mastering or cll}


def hdr_metadata_class(signals: dict[str, Any]) -> Optional[str]:
    if signals["DolbyVision"]: return "DolbyVisionMetadata"
    if signals["HDR10Plus"]:
        return "Dynamic+Static" if signals["MasteringMetadata"] or signals["ContentLightMetadata"] else "Dynamic"
    if signals["MasteringMetadata"] and signals["ContentLightMetadata"]: return "Mastering+CLL"
    if signals["MasteringMetadata"]: return "Mastering"
    if signals["ContentLightMetadata"]: return "CLL"
    return None


def dolby_vision_profile(side_data: Iterable[Any]) -> Optional[str]:
    for block in side_data:
        if isinstance(block, dict) and str(block.get("side_data_type", "")).lower() == "dovi configuration record":
            value = nullable_int(block.get("dv_profile"))
            if value is not None and value > 0:
                return str(value)
    return None


def normalize_probe_media(scan_file: dict[str, Any]) -> Optional[dict[str, Any]]:
    entry = scan_file.get("ProbeEntry")
    if not entry or str(entry.get("ProbeStatus", "")).lower() != "success":
        return None
    probe = entry["ProbeData"]
    stream = probe["Stream"]
    fmt = probe["Format"]
    width, height = nullable_int(stream.get("Width")), nullable_int(stream.get("Height"))
    avg_fps = rational_to_double(stream.get("AverageFrameRate"))
    real_fps = rational_to_double(stream.get("RealFrameRate"))
    fps = avg_fps if avg_fps is not None and avg_fps > 0 else None
    stream_duration, format_duration = nullable_double(stream.get("Duration")), nullable_double(fmt.get("Duration"))
    duration = stream_duration if stream_duration is not None and stream_duration > 0 else (format_duration if format_duration is not None and format_duration > 0 else None)
    br = bitrate_info(stream.get("BitRate"), fmt.get("BitRate"), int(scan_file["Length"]), duration)
    pixel = str(stream.get("PixelFormat") or "").strip() or None
    depth = bit_depth(stream.get("BitsPerRawSample"), pixel)
    profile = str(stream.get("Profile") or "").strip() or None
    def display(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None
    side_data = list(stream.get("SideDataList") or [])
    return {
        "Path": str(scan_file["Path"]), "RootId": str(scan_file["RootId"]), "RootLabel": str(scan_file["RootLabel"]),
        "RootPath": str(scan_file["RootPath"]), "RelativePath": str(scan_file["RelativePath"]),
        "FileSizeBytes": int(scan_file["Length"]), "SourceLastWriteTimeUtc": str(scan_file["LastWriteTimeUtc"]),
        "Codec": normalized_codec(stream.get("CodecName")), "CodecRaw": display(stream.get("CodecName")), "Profile": profile, "Level": stream.get("Level"),
        "Width": width, "Height": height, "ResolutionClass": resolution_class(width, height), "PixelFormat": pixel, "Chroma": chroma_class(pixel),
        "BitDepth": str(depth) if depth is not None else None, "BitDepthNumeric": depth, "AverageFps": avg_fps, "RealFps": real_fps, "Fps": fps, "FpsClass": fps_class(fps),
        "FrameRateMode": None, "FieldOrder": display(stream.get("FieldOrder")), "ScanType": scan_type(stream.get("FieldOrder")),
        "DurationSeconds": duration, "DurationClass": duration_class(duration), "BitRateBitsPerSecond": br["BitsPerSecond"], "BitRateSource": br["Source"], "BitRateClass": None,
        "Container": container_class(fmt.get("FormatName"), _format_major_brand(fmt.get("Tags")), os.path.splitext(str(scan_file["Path"]))[1]), "ContainerRaw": display(fmt.get("FormatName")),
        "SampleAspectRatio": display(stream.get("SampleAspectRatio")), "DisplayAspectRatio": display(stream.get("DisplayAspectRatio")),
        "AspectRatioClass": aspect_ratio_class(width, height, stream.get("DisplayAspectRatio")), "ColorRange": display(stream.get("ColorRange")),
        "ColorSpace": display(stream.get("ColorSpace")), "ColorTransfer": display(stream.get("ColorTransfer")), "ColorPrimaries": display(stream.get("ColorPrimaries")),
        "SideDataList": side_data, "DolbyVisionProfile": dolby_vision_profile(side_data),
    }


def reliable_diversity_value(value: Any) -> bool:
    # PowerShell v3.4 uses -notin here, which is case-insensitive by default.
    # Preserve that behavior explicitly in Python.
    text = str(value if value is not None else "").strip()
    unreliable = {
        "unknown", "n/a", "unspecified", "reserved", "invalid",
        "not detected", "none", "unknown hdr", "sdr / unknown",
        "pq / unknown hdr", "-99",
    }
    return bool(text) and text.lower() not in unreliable


def _lower_text(value: Any) -> str:
    return "" if value is None else str(value).strip().lower()


def classify_normalized_media(media: dict[str, Any]) -> dict[str, Any]:
    signals = hdr_signals(media)
    flags: set[str] = set(); reasons: list[str] = []
    # A missing value stays empty ("unspecified" in the messages), never the text "none".
    transfer = _lower_text(media["ColorTransfer"]); primaries = _lower_text(media["ColorPrimaries"]); matrix = _lower_text(media["ColorSpace"])
    hdr_type: Optional[str] = None; pool = "SDR"; expected_transfer = None; proprietary = False; dv_sdr = False; expected_depth = None; is_dv = signals["DolbyVision"]
    if not signals["CredibleHDR"]:
        sdr = {"bt709","gamma22","gamma28","smpte170m","smpte240m","iec61966-2-1","iec61966-2-4","bt1361e","bt2020-10","bt2020-12"}
        hdr_type = "SDR"
        if transfer not in sdr:
            pool = "SDR Uncertain"
            if not reliable_diversity_value(transfer):
                flags.add("MissingMetadata"); reasons.append("SDR transfer is missing or unspecified.")
            else:
                flags.add("UnusualCombination"); reasons.append(f"Transfer is outside the confirmed SDR mapping: {transfer}.")
        elif matrix in {"ictcp", "ipt-c2"}:
            pool = "SDR Uncertain"; flags.add("UnusualCombination"); reasons.append(f"SDR transfer with an unconfirmed matrix combination: {matrix}.")
        if transfer in {"bt2020-10", "bt2020-12"}:
            expected_depth = 10 if transfer == "bt2020-10" else 12
            depth = media.get("BitDepthNumeric")
            if depth != expected_depth:
                pool = "SDR Uncertain"
                flags.add("UnusualCombination")
                reported = "unspecified" if depth is None else f"{depth}-bit"
                reasons.append(f"BT.2020 transfer {transfer} expects {expected_depth}-bit; reported {reported}.")
            if primaries != "bt2020":
                pool = "SDR Uncertain"
                flags.add("UnusualCombination")
                reasons.append(f"BT.2020 transfer {transfer} expects bt2020 primaries; reported {primaries or 'unspecified'}.")
            if matrix not in {"bt2020nc", "bt2020c"}:
                pool = "SDR Uncertain"
                flags.add("UnusualCombination")
                reasons.append(f"BT.2020 transfer {transfer} expects bt2020nc or bt2020c matrix; reported {matrix or 'unspecified'}.")
    else:
        pool = "HDR"
        if is_dv:
            hdr_type = "Dolby Vision"; dv = signals["DolbyVisionConfig"] or {}
            profile = nullable_int(dv.get("dv_profile")); compat = nullable_int(dv.get("dv_bl_signal_compatibility_id")); rpu = nullable_int(dv.get("rpu_present_flag")); bl = nullable_int(dv.get("bl_present_flag")); el = nullable_int(dv.get("el_present_flag"))
            if any(v is None for v in (profile, compat, rpu, bl, el)):
                flags.add("MissingMetadata"); reasons.append("DV profile, compatibility ID or RPU/BL/EL flag is missing.")
            allowed = {5:{0}, 7:{6}, 8:{1,4}, 9:{2}, 10:{0,1,4}, 20:{0,4}}
            if profile is None or profile not in allowed:
                flags.add("UnusualCombination"); reasons.append("DV profile is outside the verified profile matrix.")
            else:
                if compat is not None and compat not in allowed[profile]:
                    flags.add("UnusualCombination"); reasons.append(f"Unconfirmed DV profile/compatibility combination: {profile}/{compat}.")
                expected_codec = "HEVC"; expected_depth = 10
                if profile == 9: expected_codec = "H264"; expected_depth = 8
                if profile == 10: expected_codec = "AV1"
                if not reliable_diversity_value(media["Codec"]):
                    flags.add("MissingMetadata"); reasons.append("DV codec is missing.")
                elif media["Codec"] != expected_codec:
                    flags.add("UnusualCombination"); reasons.append(f"DV profile {profile} expects {expected_codec}; reported {media['Codec']}.")
                expected_el = 1 if profile == 7 else 0
                if (rpu is not None and rpu != 1) or (bl is not None and bl != 1) or (el is not None and el != expected_el):
                    flags.add("UnusualCombination"); reasons.append("DV RPU/BL/EL flags do not match the verified configuration.")
            proprietary = compat == 0; dv_sdr = profile == 9 and compat == 2
            if compat in {1,6}: expected_transfer = "smpte2084"
            if compat == 4: expected_transfer = "arib-std-b67"
            if dv_sdr: expected_transfer = "bt709"
            if media["ScanType"] == "Interlaced": flags.add("UnusualCombination"); reasons.append("DV is reported as interlaced.")
        elif signals["HDR10Plus"]: hdr_type = "HDR10+"; expected_transfer = "smpte2084"
        elif signals["PQ"]: hdr_type = "HDR10 / PQ"; expected_transfer = "smpte2084"
        elif signals["HLG"]: hdr_type = "HLG"; expected_transfer = "arib-std-b67"
        else: flags.add("UnknownHDRType"); reasons.append("Usable static HDR metadata is present, but no supported HDR type is confirmed.")
        if hdr_type is not None:
            depth = media["BitDepthNumeric"]
            if depth is None: flags.add("MissingMetadata"); reasons.append("Component bit depth cannot be determined reliably.")
            elif (expected_depth is not None and depth != expected_depth) or (expected_depth is None and depth not in {10,12}): flags.add("UnusualCombination"); reasons.append(f"Bit depth {depth} is outside the verified configuration.")
            if not reliable_diversity_value(media["Chroma"]): flags.add("MissingMetadata"); reasons.append("Pixel format/chroma cannot be determined reliably.")
            elif is_dv and media["Chroma"] != "4:2:0": flags.add("UnusualCombination"); reasons.append("DV chroma is outside the verified 4:2:0 configuration.")
            if proprietary:
                if reliable_diversity_value(transfer) and transfer != "smpte2084": flags.add("UnusualCombination"); reasons.append("Unconfirmed proprietary DV transfer.")
                if reliable_diversity_value(primaries) and primaries != "bt2020": flags.add("UnusualCombination"); reasons.append("Unconfirmed proprietary DV primaries.")
                if reliable_diversity_value(matrix) and matrix != "ipt-c2": flags.add("UnusualCombination"); reasons.append("Unconfirmed proprietary DV matrix; expected IPT-PQ-C2 or unspecified.")
            elif expected_transfer is not None:
                expected_primaries = "bt2020"; allowed_matrices = {"bt2020nc"}
                if dv_sdr: expected_primaries = "bt709"; allowed_matrices = {"bt709"}
                elif not is_dv and media["Chroma"] == "RGB": allowed_matrices = {"gbr", "rgb"}
                elif not is_dv and media["Chroma"] == "4:4:4": allowed_matrices = {"bt2020nc", "ictcp"}
                checks = [("transfer", transfer, {expected_transfer}), ("primaries", primaries, {expected_primaries}), ("matrix", matrix, allowed_matrices)]
                for name, value, expected in checks:
                    if not reliable_diversity_value(value): flags.add("MissingMetadata"); reasons.append(f"HDR {name} is missing or unspecified.")
                    elif value not in expected:
                        if name == "transfer" and (is_dv or signals["HDR10Plus"]) and value in {"smpte2084", "arib-std-b67", "bt709"}:
                            flags.add("InconsistentColorMetadata"); reasons.append(f"Reported transfer {value} conflicts with the declared HDR configuration ({'/'.join(sorted(expected))}).")
                        else:
                            flags.add("UnusualCombination"); reasons.append(f"Unconfirmed HDR {name}: {value}; expected {'/'.join(sorted(expected))}.")
            if signals["HDR10Plus"] and is_dv and not signals["PQ"] and not proprietary:
                flags.add("UnusualCombination"); reasons.append("Additional HDR10+ signal without a confirmed PQ base context.")
        if flags: pool = "HDR Uncertain"
    return {"Pool": pool, "HDRType": hdr_type, "HDRMetadataClass": hdr_metadata_class(signals), "HDRSignals": signals, "Anomalies": sorted(flags), "UnknownReason": " ".join(reasons)}



def convert_scan_to_classified_records(scan: dict[str, Any]) -> list[dict[str, Any]]:
    started = time.perf_counter()
    write_sampler_log("Phase: Classifying media...", "INFO", "APP")
    records = []
    for scan_file in scan["Files"]:
        entry = scan_file.get("ProbeEntry")
        if not entry:
            records.append({"Pool":"ProbeFailed","Path":str(scan_file["Path"]),"ProbeStatus":"NoProbeResult","ProbeErrorKind":"NoProbeResult","ProbeErrorMessage":"No probe result is available for this file.","Media":None,"Classification":None}); continue
        status = str(entry.get("ProbeStatus"))
        if status.lower() != "success":
            records.append({"Pool":"ProbeFailed","Path":str(scan_file["Path"]),"ProbeStatus":status,"ProbeErrorKind":entry.get("ProbeErrorKind"),"ProbeErrorMessage":entry.get("ProbeErrorMessage"),"Media":None,"Classification":None}); continue
        media = normalize_probe_media(scan_file)
        if media is None:
            records.append({"Pool":"ProbeFailed","Path":str(scan_file["Path"]),"ProbeStatus":"ProbeFailed","ProbeErrorKind":"NormalizationFailed","ProbeErrorMessage":"Probe data could not be normalized.","Media":None,"Classification":None}); continue
        classification = classify_normalized_media(media)
        records.append({"Pool":classification["Pool"],"Path":str(scan_file["Path"]),"ProbeStatus":"Success","ProbeErrorKind":None,"ProbeErrorMessage":None,"Media":media,"Classification":classification})
    counts = Counter(r["Pool"] for r in records)
    write_sampler_log(f"Classification: SDR {counts['SDR']} | HDR {counts['HDR']} | SDR Uncertain {counts['SDR Uncertain']} | HDR Uncertain {counts['HDR Uncertain']} | Probe failed {counts['ProbeFailed']}", "INFO", "CLASSIFY")
    write_sampler_log(f"Classification complete records={len(records)} elapsed={format_elapsed(time.perf_counter()-started)}", "INFO", "CLASSIFY")
    return records
