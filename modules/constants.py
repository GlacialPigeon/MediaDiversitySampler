# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

from pathlib import Path

APP_NAME = "Media Diversity Sampler"
APP_VERSION = "1.0.0"

CONFIG_SCHEMA_VERSION = 4
CACHE_SCHEMA_VERSION = 1
PROBE_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 3
MARKER_SCHEMA_VERSION = 1

APP_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = APP_ROOT / "data"
RECOVERY_DIR = DATA_DIR / "recovery"
CONFIG_PATH = DATA_DIR / "config.json"
PROBE_CACHE_DB_PATH = DATA_DIR / "probe_cache.db"
PROBE_CACHE_EXPORT_PATH = DATA_DIR / "probe_cache.json"
FFMPEG_REGISTRY_PATH = DATA_DIR / "ffmpeg_registry.json"
PATHLISTS_DIR = APP_ROOT / "pathlists"
MEDIA_SOURCES_PATH = PATHLISTS_DIR / "media_sources.txt"
LOGS_DIR = APP_ROOT / "logs"
LOCALES_DIR = APP_ROOT / "locales"
PROBE_FAILED_LOG_PATH = LOGS_DIR / "probe_failed.log"
FFMPEG_DIR = APP_ROOT / "ffmpeg"
FFMPEG_BIN_DIR = FFMPEG_DIR / "bin"

LIBRARY_TYPE = "MediaDiversitySamplerTestLibrary"
DEFAULT_LIBRARY_NAME = "MediaDiversity_TestLibrary"
