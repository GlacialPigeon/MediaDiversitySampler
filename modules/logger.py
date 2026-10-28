# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import datetime as dt
import hashlib
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .constants import APP_ROOT, CONFIG_PATH, DATA_DIR, LOCALES_DIR, LOGS_DIR, APP_VERSION
from .file_io import read_json_file

RUN_ID = str(uuid.uuid4())
LOG_PATH: Optional[Path] = None
ACTIVE_LOG_LEVEL = "normal"
LOGGING_LEVELS = {"error_only", "normal", "full"}
_LOG_LOCK = threading.Lock()
SESSION_STARTED_PERF = time.perf_counter()


def format_elapsed(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000.0)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def build_fingerprint() -> str:
    """Deterministic fingerprint of modular source plus bundled locale files."""
    digest = hashlib.sha256()
    paths = sorted(APP_ROOT.glob("*.py"), key=lambda item: item.relative_to(APP_ROOT).as_posix().casefold())
    modules_dir = APP_ROOT / "modules"
    if modules_dir.is_dir():
        paths.extend(sorted(modules_dir.rglob("*.py"), key=lambda item: item.relative_to(APP_ROOT).as_posix().casefold()))
    if LOCALES_DIR.is_dir():
        paths.extend(sorted(LOCALES_DIR.glob("*.json"), key=lambda item: item.relative_to(APP_ROOT).as_posix().casefold()))
    for path in paths:
        relative = path.relative_to(APP_ROOT).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def peek_logging_level() -> str:
    try:
        raw = read_json_file(CONFIG_PATH)
        value = str(raw.get("LoggingLevel", "normal")).strip().casefold()
        return value if value in LOGGING_LEVELS else "normal"
    except Exception:
        return "normal"


def set_logging_level(value: str) -> None:
    global ACTIVE_LOG_LEVEL
    normalized = str(value or "normal").strip().casefold()
    ACTIVE_LOG_LEVEL = normalized if normalized in LOGGING_LEVELS else "normal"


def initialize_logger() -> None:
    global LOG_PATH, ACTIVE_LOG_LEVEL
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")[:-3]
    candidate = LOGS_DIR / f"MediaDiversitySampler_{stamp}.log"
    index = 2
    while candidate.exists():
        candidate = LOGS_DIR / f"MediaDiversitySampler_{stamp}_{index}.log"
        index += 1
    LOG_PATH = candidate
    LOG_PATH.write_text("", encoding="utf-8")
    ACTIVE_LOG_LEVEL = peek_logging_level()


def write_sampler_log(message: str, level: str = "INFO", component: str = "APP", force: bool = False) -> bool:
    if LOG_PATH is None:
        return False
    normalized = str(level).upper()
    if normalized == "OK":
        normalized = "INFO"
    allowed = {"WARNING", "ERROR", "CRITICAL"} if ACTIVE_LOG_LEVEL == "error_only" else ({"INFO", "WARNING", "ERROR", "CRITICAL"} if ACTIVE_LOG_LEVEL == "normal" else {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
    if not force and normalized not in allowed:
        return True
    stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    prefix = f"{stamp} [{normalized}] [{str(component).upper()}] "
    try:
        with _LOG_LOCK:
            with LOG_PATH.open("a", encoding="utf-8", newline="") as fh:
                for line in str(message).splitlines() or [""]:
                    fh.write(prefix + line + "\n")
        return True
    except Exception:
        return False


def write_run_environment_header(
    config: Optional[dict[str, Any]],
    *,
    platform_name: str,
    config_schema_version: int,
    cache_schema_version: int,
    probe_schema_version: int,
    manifest_schema_version: int,
    marker_schema_version: int,
    canonical_locale: str,
) -> None:
    write_sampler_log(f"ProgramVersion={APP_VERSION}", "INFO", "SESSION", force=True)
    write_sampler_log(f"RunId={RUN_ID}", "INFO", "SESSION", force=True)
    write_sampler_log(f"Build fingerprint=SHA256:{build_fingerprint()}", "INFO", "SESSION", force=True)
    write_sampler_log(f"PythonVersion={sys.version.split()[0]}", "INFO", "SESSION", force=True)
    write_sampler_log(f"Platform={platform_name} os.name={os.name}", "INFO", "SESSION", force=True)
    write_sampler_log(
        f"ConfigSchemaVersion={config_schema_version} CacheSchemaVersion={cache_schema_version} "
        f"ProbeSchemaVersion={probe_schema_version} ManifestSchemaVersion={manifest_schema_version} MarkerSchemaVersion={marker_schema_version}",
        "INFO", "SESSION", force=True,
    )
    if config:
        write_sampler_log(
            f"Probe workers={config['ProbeWorkers']} timeout={config['FFprobeTimeoutSeconds']}s retries={config['ProbeRetries']} "
            f"logging={config['LoggingLevel']} locale={config.get('Locale', canonical_locale)}",
            "INFO", "CONFIG", force=True,
        )
