# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from .constants import APP_ROOT, CONFIG_PATH, CONFIG_SCHEMA_VERSION, DATA_DIR, DEFAULT_LIBRARY_NAME, RECOVERY_DIR
from .file_io import write_json_file_atomically
from .localization import CANONICAL_LOCALE, t
from .logger import LOGGING_LEVELS, set_logging_level, write_sampler_log
from .platform_api import (
    adopt_storage_identity, descriptor_persistence_fields, empty_descriptor_persistence_fields,
    leaf_label, normalize_path, path_key, relative_is_ancestor, relative_key,
    resolve_storage, valid_filename_component,
)

CURRENT_CONFIG: Optional[dict[str, Any]] = None
IS_INITIAL_RUN = False
STARTUP_NOTICES: list[tuple[str, dict[str, Any]]] = []

def new_default_config() -> dict[str, Any]:
    return {
        "ConfigSchemaVersion": CONFIG_SCHEMA_VERSION,
        "SourceRoots": [],
        "Outputs": [],
        "LibraryName": DEFAULT_LIBRARY_NAME,
        "Locale": CANONICAL_LOCALE,
        "ProbeWorkers": 8,
        "FFprobeTimeoutSeconds": 30,
        "ProbeRetries": 1,
        "LoggingLevel": "normal",
    }


def save_config(config: dict[str, Any]) -> None:
    global CURRENT_CONFIG
    config.pop("ProgramVersion", None)
    config["ConfigSchemaVersion"] = CONFIG_SCHEMA_VERSION
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_json_file_atomically(CONFIG_PATH, config)
    CURRENT_CONFIG = config
    set_logging_level(str(config.get("LoggingLevel", "normal")))
    write_sampler_log("Configuration saved.", "INFO", "CONFIG")


def save_runtime_config(candidate: dict[str, Any]) -> bool:
    try:
        save_config(candidate)
        return True
    except Exception as exc:
        write_sampler_log(
            f"Runtime configuration save failed error={exc}",
            "ERROR", "CONFIG", force=True,
        )
        return False




def _valid_source_roots_block(value: Any) -> bool:
    try:
        if not isinstance(value, list):
            return False
        ids: set[str] = set()
        labels: set[str] = set()
        for root in value:
            if not isinstance(root, dict) or any(k not in root for k in ("RootId", "Label", "Path")):
                return False
            root_id = str(root["RootId"])
            label = str(root["Label"])
            path = str(root["Path"])
            uuid.UUID(root_id)
            if not valid_filename_component(label) or not path.strip():
                return False
            normalize_path(path)
            if root_id.casefold() in ids or label.casefold() in labels:
                return False
            ids.add(root_id.casefold())
            labels.add(label.casefold())
        return True
    except Exception:
        return False


def _valid_outputs_block(value: Any) -> bool:
    try:
        if not isinstance(value, list):
            return False
        ids: set[str] = set()
        for output in value:
            if not isinstance(output, dict) or any(k not in output for k in ("BasePath", "LibraryId")):
                return False
            base = str(output["BasePath"])
            library_id = str(output["LibraryId"])
            uuid.UUID(library_id)
            if not base.strip():
                return False
            normalize_path(base)
            if library_id.casefold() in ids:
                return False
            ids.add(library_id.casefold())
        return True
    except Exception:
        return False


def _extract_complete_json_block(text: str, key: str) -> Any:
    # Recovery is deliberately conservative: only a complete JSON value for the
    # requested top-level key is accepted. Individual entries are never salvaged.
    match = re.search(rf'"{re.escape(key)}"\s*:\s*', text)
    if not match:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text, match.end())
        return value
    except Exception:
        return None


def _salvage_config_path_blocks_from_text(text: str) -> tuple[Optional[list[dict[str, Any]]], Optional[list[dict[str, Any]]]]:
    roots = _extract_complete_json_block(text, "SourceRoots")
    outputs = _extract_complete_json_block(text, "Outputs")
    valid_roots = roots if _valid_source_roots_block(roots) else None
    valid_outputs = outputs if _valid_outputs_block(outputs) else None
    return valid_roots, valid_outputs


def _apply_salvaged_path_blocks(config: dict[str, Any], roots: Any, outputs: Any) -> None:
    if _valid_source_roots_block(roots):
        config["SourceRoots"] = [dict(item) for item in roots]
    if _valid_outputs_block(outputs):
        config["Outputs"] = [dict(item) for item in outputs]


def test_config_shape(config: Any) -> bool:
    try:
        required = (
            "ConfigSchemaVersion", "SourceRoots", "Outputs", "LibraryName", "Locale",
            "ProbeWorkers", "FFprobeTimeoutSeconds", "ProbeRetries", "LoggingLevel",
        )
        if not isinstance(config, dict) or any(k not in config for k in required):
            return False
        if "ProgramVersion" in config:
            return False
        if config.get("ConfigSchemaVersion") != CONFIG_SCHEMA_VERSION:
            return False
        if not _valid_source_roots_block(config.get("SourceRoots")) or not _valid_outputs_block(config.get("Outputs")):
            return False
        if type(config["ProbeWorkers"]) is not int or not 1 <= config["ProbeWorkers"] <= 32:
            return False
        if type(config["FFprobeTimeoutSeconds"]) is not int or config["FFprobeTimeoutSeconds"] < 1:
            return False
        if type(config["ProbeRetries"]) is not int or config["ProbeRetries"] < 0:
            return False
        if str(config["LoggingLevel"]) not in LOGGING_LEVELS:
            return False
        if not isinstance(config["Locale"], str) or not str(config["Locale"]).strip():
            return False
        if not valid_filename_component(str(config["LibraryName"])):
            return False
        return True
    except Exception:
        return False


def _identity_tuple(item: dict[str, Any], relative_field: str) -> tuple[str, str]:
    return (str(item.get("StorageIdentityKey") or "").casefold(), relative_key(str(item.get(relative_field) or "")))


def _descriptor_to_root(desc: dict[str, Any], root_id: Optional[str] = None, label: Optional[str] = None) -> dict[str, Any]:
    base = label or leaf_label(str(desc["Path"]))
    result = {
        "RootId": root_id or str(uuid.uuid4()), "Label": base, "Path": str(desc["Path"]),
        "StorageIdentityType": str(desc["StorageIdentityType"]), "StorageIdentityKey": str(desc["StorageIdentityKey"]),
        "StorageRoot": str(desc["StorageRoot"]), "SourceRootRelativePath": str(desc["StorageRelativePath"]),
    }
    result.update(descriptor_persistence_fields(desc))
    return result


def _descriptor_to_output(desc: dict[str, Any], library_id: Optional[str] = None) -> dict[str, Any]:
    result = {
        "BasePath": str(desc["Path"]), "LibraryId": library_id or str(uuid.uuid4()),
        "StorageIdentityType": str(desc["StorageIdentityType"]), "StorageIdentityKey": str(desc["StorageIdentityKey"]),
        "StorageRoot": str(desc["StorageRoot"]), "BaseRelativePath": str(desc["StorageRelativePath"]),
    }
    result.update(descriptor_persistence_fields(desc))
    return result


def _objects_overlap(a: dict[str, Any], b: dict[str, Any], field: str) -> bool:
    if str(a.get("StorageIdentityKey","")).casefold()!=str(b.get("StorageIdentityKey","")).casefold(): return False
    ar=str(a.get(field) or ""); br=str(b.get(field) or "")
    return relative_key(ar)==relative_key(br) or relative_is_ancestor(ar,br) or relative_is_ancestor(br,ar)


def _preserve_recovery_file(path: Path, label: str) -> Optional[Path]:
    if not path.exists():
        return None
    RECOVERY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    target = RECOVERY_DIR / f"{label}_{stamp}{path.suffix}"
    shutil.copy2(path, target)
    return target








def normalize_loaded_config_paths(config: dict[str, Any]) -> dict[str, Any]:
    changed = False
    resolved_roots: list[dict[str, Any]] = []

    # Resolve/canonicalize each source independently. Unavailable sources are
    # retained with a textual fallback identity and are never guessed away.
    for root in config["SourceRoots"]:
        try:
            desc = resolve_storage(str(root["Path"]), False)
            desc = adopt_storage_identity(desc, [*config["SourceRoots"], *resolved_roots, *config.get("Outputs", [])])
            enriched = _descriptor_to_root(desc, str(root["RootId"]), str(root["Label"]))
        except Exception:
            enriched = dict(root)
            enriched["Path"] = normalize_path(str(root["Path"]))
            enriched["StorageIdentityType"] = "PATH_FALLBACK"
            enriched["StorageIdentityKey"] = "PATH:" + path_key(enriched["Path"])
            enriched["StorageRoot"] = enriched["Path"]
            enriched["SourceRootRelativePath"] = ""
            enriched.update(empty_descriptor_persistence_fields())
        if enriched != root:
            changed = True

        ident = _identity_tuple(enriched, "SourceRootRelativePath")
        duplicate = next((x for x in resolved_roots if _identity_tuple(x, "SourceRootRelativePath") == ident), None)
        if duplicate is not None:
            changed = True
            write_sampler_log(f"Removed duplicate configured source after startup normalization path={enriched['Path']}", "WARNING", "PATH")
            continue

        # Collapse only verified same-storage coverage. PATH_FALLBACK entries are
        # deliberately retained because equivalence/coverage is not verified.
        if str(enriched.get("StorageIdentityType")) != "PATH_FALLBACK":
            parent = next((
                x for x in resolved_roots
                if str(x.get("StorageIdentityType")) != "PATH_FALLBACK"
                and str(x.get("StorageIdentityKey", "")).casefold() == str(enriched.get("StorageIdentityKey", "")).casefold()
                and relative_is_ancestor(str(x.get("SourceRootRelativePath") or ""), str(enriched.get("SourceRootRelativePath") or ""), True)
            ), None)
            if parent is not None:
                changed = True
                write_sampler_log(f"Removed covered configured source path={enriched['Path']} parent={parent['Path']}", "WARNING", "PATH")
                continue
            before = len(resolved_roots)
            resolved_roots = [
                x for x in resolved_roots
                if not (
                    str(x.get("StorageIdentityType")) != "PATH_FALLBACK"
                    and str(x.get("StorageIdentityKey", "")).casefold() == str(enriched.get("StorageIdentityKey", "")).casefold()
                    and relative_is_ancestor(str(enriched.get("SourceRootRelativePath") or ""), str(x.get("SourceRootRelativePath") or ""), True)
                )
            ]
            if len(resolved_roots) != before:
                changed = True
        resolved_roots.append(enriched)

    outputs: list[dict[str, Any]] = []
    for output in config["Outputs"]:
        try:
            desc = resolve_storage(str(output["BasePath"]), False)
            desc = adopt_storage_identity(desc, [output, *resolved_roots, *outputs])
            enriched = _descriptor_to_output(desc, str(output["LibraryId"]))
        except Exception:
            enriched = dict(output)
            enriched["BasePath"] = normalize_path(str(output["BasePath"]))
            enriched["StorageIdentityType"] = "PATH_FALLBACK"
            enriched["StorageIdentityKey"] = "PATH:" + path_key(enriched["BasePath"])
            enriched["StorageRoot"] = enriched["BasePath"]
            enriched["BaseRelativePath"] = ""
            enriched.update(empty_descriptor_persistence_fields())
        if enriched != output:
            changed = True
        ident = _identity_tuple(enriched, "BaseRelativePath")
        if any(_identity_tuple(x, "BaseRelativePath") == ident for x in outputs):
            changed = True
            write_sampler_log(f"Removed duplicate configured output after startup normalization path={enriched['BasePath']}", "WARNING", "PATH")
            continue
        outputs.append(enriched)

    config["SourceRoots"] = resolved_roots
    config["Outputs"] = outputs
    if changed:
        save_config(config)
    return config


def add_startup_notice(key: str, **kwargs: Any) -> None:
    STARTUP_NOTICES.append((key, dict(kwargs)))


def consume_startup_notices() -> list[tuple[str, dict[str, Any]]]:
    notices = list(STARTUP_NOTICES)
    STARTUP_NOTICES.clear()
    return notices


def get_current_config() -> Optional[dict[str, Any]]:
    return CURRENT_CONFIG


def load_config(status_callback: Optional[Callable[..., None]] = None) -> dict[str, Any]:
    global IS_INITIAL_RUN, CURRENT_CONFIG
    legacy_config_path = APP_ROOT / "config.json"
    source_config_path = CONFIG_PATH if CONFIG_PATH.is_file() else (legacy_config_path if legacy_config_path.is_file() else CONFIG_PATH)
    IS_INITIAL_RUN = not source_config_path.is_file()
    if IS_INITIAL_RUN:
        config = new_default_config()
        save_config(config)
        return config

    legacy_location = source_config_path != CONFIG_PATH
    raw_text = ""
    try:
        raw_text = source_config_path.read_text(encoding="utf-8-sig")
        raw = json.loads(raw_text)
        if not isinstance(raw, dict):
            raise ValueError("Configuration root must be an object.")
    except Exception as exc:
        backup = None
        try:
            backup = _preserve_recovery_file(source_config_path, "config_invalid")
        except Exception as backup_exc:
            write_sampler_log(f"Could not preserve invalid config: {backup_exc}", "ERROR", "CONFIG")
        config = new_default_config()
        roots, outputs = _salvage_config_path_blocks_from_text(raw_text)
        _apply_salvaged_path_blocks(config, roots, outputs)
        if status_callback is not None:
            status_callback(t("config.invalid_defaults"), "WARNING", component="CONFIG", log_message="Configuration file is invalid. A new configuration will be created.")
        if roots is not None or outputs is not None:
            add_startup_notice("config.recovered_paths")
        add_startup_notice("config.invalid_preserved", path=backup) if backup else add_startup_notice("config.invalid_replaced")
        write_sampler_log(f"Invalid config error={exc} backup={backup}", "ERROR", "CONFIG")
        config = normalize_loaded_config_paths(config)
        save_config(config)
        return config

    schema = raw.get("ConfigSchemaVersion")
    if schema in {2, 3}:
        backup = None
        try:
            backup = _preserve_recovery_file(source_config_path, f"config_schema{schema}")
        except Exception as exc:
            write_sampler_log(f"Could not preserve schema-{schema} config: {exc}", "ERROR", "CONFIG")
        config = new_default_config()
        if _valid_source_roots_block(raw.get("SourceRoots")):
            config["SourceRoots"] = [dict(x) for x in raw["SourceRoots"]]
        if _valid_outputs_block(raw.get("Outputs")):
            config["Outputs"] = [dict(x) for x in raw["Outputs"]]
        if isinstance(raw.get("LibraryName"), str) and valid_filename_component(raw["LibraryName"]):
            config["LibraryName"] = raw["LibraryName"]
        if type(raw.get("ProbeWorkers")) is int and 1 <= raw["ProbeWorkers"] <= 32:
            config["ProbeWorkers"] = raw["ProbeWorkers"]
        if type(raw.get("FFprobeTimeoutSeconds")) is int and raw["FFprobeTimeoutSeconds"] >= 1:
            config["FFprobeTimeoutSeconds"] = raw["FFprobeTimeoutSeconds"]
        if type(raw.get("ProbeRetries")) is int and raw["ProbeRetries"] >= 0:
            config["ProbeRetries"] = raw["ProbeRetries"]
        if str(raw.get("LoggingLevel", "")) in LOGGING_LEVELS:
            config["LoggingLevel"] = str(raw["LoggingLevel"])
        if isinstance(raw.get("Locale"), str) and str(raw.get("Locale")).strip():
            config["Locale"] = str(raw["Locale"])
        config = normalize_loaded_config_paths(config)
        save_config(config)
        if backup:
            add_startup_notice("config.schema_upgraded_backup", old=schema, new=CONFIG_SCHEMA_VERSION, path=backup)
        else:
            add_startup_notice("config.schema_upgraded", old=schema, new=CONFIG_SCHEMA_VERSION)
        if legacy_location:
            add_startup_notice("config.legacy_imported", path=CONFIG_PATH)
        return config

    if schema != CONFIG_SCHEMA_VERSION:
        backup = None
        try:
            backup = _preserve_recovery_file(source_config_path, "config_unsupported_schema")
        except Exception:
            pass
        if status_callback is not None:
            status_callback(t('config.unsupported_schema', schema=schema), 'WARNING', component='CONFIG', log_message=f'Configuration schema {schema} is not supported. A default configuration will be created.')
        config = new_default_config()
        save_config(config)
        add_startup_notice("config.unsupported_preserved", path=backup) if backup else add_startup_notice("config.unsupported_replaced")
        return config

    if not test_config_shape(raw):
        backup = None
        try:
            backup = _preserve_recovery_file(source_config_path, "config_invalid")
        except Exception:
            pass
        config = new_default_config()
        _apply_salvaged_path_blocks(config, raw.get("SourceRoots"), raw.get("Outputs"))
        if status_callback is not None:
            status_callback(t("config.invalid_defaults"), "WARNING", component="CONFIG", log_message="Configuration is incomplete or invalid. Defaults will be created.")
        add_startup_notice("config.invalid_preserved", path=backup) if backup else add_startup_notice("config.invalid_replaced")
        if config["SourceRoots"] or config["Outputs"]:
            add_startup_notice("config.recovered_paths")
        config = normalize_loaded_config_paths(config)
        save_config(config)
        return config

    config = normalize_loaded_config_paths(raw)
    CURRENT_CONFIG = config
    set_logging_level(str(config.get("LoggingLevel", "normal")))
    if legacy_location:
        save_config(config)
        add_startup_notice("config.legacy_imported", path=CONFIG_PATH)
    return config
