# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import copy
import os
import uuid
from pathlib import Path
from typing import Any, Iterable

from . import config as config_module
from .logger import write_sampler_log
from .constants import LIBRARY_TYPE, MARKER_SCHEMA_VERSION
from .file_io import read_json_file
from .platform_api import (
    adopt_storage_identity, join_path, path_key, relative_is_ancestor, remove_outer_quotes, resolve_storage, source_alias_group_key, source_review_alias as platform_source_review_alias,
)


def _unique_source_label(path: str, used: set[str]) -> str:
    from .platform_api import leaf_label
    base = leaf_label(path)
    label = base
    suffix = 2
    while label.casefold() in used:
        label = f"{base}_{suffix}"
        suffix += 1
    used.add(label.casefold())
    return label


def plan_source_batch(config: dict[str, Any], paths: Iterable[str]) -> dict[str, Any]:
    existing = [dict(x) for x in config.get("SourceRoots", [])]
    requested = [remove_outer_quotes(str(x)) for x in paths if remove_outer_quotes(str(x))]
    resolved: list[dict[str, Any]] = []
    resolved_inputs: list[dict[str, str]] = []
    resolutions: list[tuple[str, str]] = []
    unavailable: list[tuple[str, str]] = []
    invalid: list[tuple[str, str]] = []
    duplicate_inputs: list[tuple[str, str]] = []
    selected_covered: list[tuple[str, str]] = []
    already_configured: list[str] = []
    already_covered: list[tuple[str, str]] = []
    replacements: list[tuple[str, list[str]]] = []

    known_for_identity: list[dict[str, Any]] = [*existing, *config.get("Outputs", [])]
    for raw in requested:
        try:
            desc = resolve_storage(raw, True)
            desc = adopt_storage_identity(desc, [*known_for_identity, *resolved])
            candidate = config_module._descriptor_to_root(desc)
        except FileNotFoundError as exc:
            unavailable.append((raw, str(exc) or "Source is unavailable."))
            continue
        except Exception as exc:
            if not os.path.exists(remove_outer_quotes(raw)):
                unavailable.append((raw, str(exc) or "Source is unavailable."))
            else:
                invalid.append((raw, str(exc)))
            continue

        resolved_inputs.append({
            "Input": raw,
            "Resolved": str(candidate["Path"]),
            "StorageIdentityType": str(candidate.get("StorageIdentityType", "")),
            "StorageIdentityKey": str(candidate.get("StorageIdentityKey", "")),
            "SourceRootRelativePath": str(candidate.get("SourceRootRelativePath") or ""),
        })
        if path_key(raw) != path_key(str(candidate["Path"])):
            resolutions.append((raw, str(candidate["Path"])))
        ident = config_module._identity_tuple(candidate, "SourceRootRelativePath")
        same = next((x for x in resolved if config_module._identity_tuple(x, "SourceRootRelativePath") == ident), None)
        if same is not None:
            duplicate_inputs.append((raw, str(same["Path"])))
            continue

        parent = next((
            x for x in resolved
            if str(x.get("StorageIdentityKey", "")).casefold() == str(candidate.get("StorageIdentityKey", "")).casefold()
            and relative_is_ancestor(str(x.get("SourceRootRelativePath") or ""), str(candidate.get("SourceRootRelativePath") or ""), True)
        ), None)
        if parent is not None:
            selected_covered.append((str(candidate["Path"]), str(parent["Path"])))
            continue
        covered_selected = [
            x for x in resolved
            if str(x.get("StorageIdentityKey", "")).casefold() == str(candidate.get("StorageIdentityKey", "")).casefold()
            and relative_is_ancestor(str(candidate.get("SourceRootRelativePath") or ""), str(x.get("SourceRootRelativePath") or ""), True)
        ]
        for child in covered_selected:
            selected_covered.append((str(child["Path"]), str(candidate["Path"])))
        resolved = [x for x in resolved if x not in covered_selected]
        resolved.append(candidate)

    additions: list[dict[str, Any]] = []
    replaced_ids: set[str] = set()
    for candidate in resolved:
        ident = config_module._identity_tuple(candidate, "SourceRootRelativePath")
        exact = next((x for x in existing if config_module._identity_tuple(x, "SourceRootRelativePath") == ident), None)
        if exact is not None:
            already_configured.append(str(candidate["Path"]))
            continue
        parent = next((
            x for x in existing
            if str(x.get("StorageIdentityKey", "")).casefold() == str(candidate.get("StorageIdentityKey", "")).casefold()
            and relative_is_ancestor(str(x.get("SourceRootRelativePath") or ""), str(candidate.get("SourceRootRelativePath") or ""), True)
        ), None)
        if parent is not None:
            already_covered.append((str(candidate["Path"]), str(parent["Path"])))
            continue
        children = [
            x for x in existing
            if str(x.get("StorageIdentityKey", "")).casefold() == str(candidate.get("StorageIdentityKey", "")).casefold()
            and relative_is_ancestor(str(candidate.get("SourceRootRelativePath") or ""), str(x.get("SourceRootRelativePath") or ""), True)
        ]
        if children:
            replaced_ids.update(str(x["RootId"]).casefold() for x in children)
            replacements.append((str(candidate["Path"]), [str(x["Path"]) for x in children]))
        additions.append(candidate)

    final = [x for x in existing if str(x["RootId"]).casefold() not in replaced_ids]
    used_labels = {str(x.get("Label", "")).casefold() for x in final if str(x.get("Label", "")).strip()}
    for candidate in additions:
        candidate["Label"] = _unique_source_label(str(candidate["Path"]), used_labels)
        final.append(candidate)

    same_storage_groups: list[list[str]] = []
    grouped: dict[str, list[str]] = {}
    for candidate in resolved:
        group_key = source_alias_group_key(candidate)
        if group_key:
            grouped.setdefault(group_key, []).append(str(candidate["Path"]))
    for values in grouped.values():
        unique = list(dict.fromkeys(values))
        if len(unique) > 1:
            same_storage_groups.append(unique)

    before = [(str(x.get("RootId")), path_key(str(x.get("Path", "")))) for x in existing]
    after = [(str(x.get("RootId")), path_key(str(x.get("Path", "")))) for x in final]
    return {
        "Requested": requested, "ResolvedInputs": resolved_inputs, "Resolutions": resolutions, "SameStorageGroups": same_storage_groups,
        "Unavailable": unavailable, "Invalid": invalid, "DuplicateInputs": duplicate_inputs,
        "SelectedCovered": selected_covered, "AlreadyConfigured": already_configured,
        "AlreadyCovered": already_covered, "Replacements": replacements,
        "FinalSources": final, "Changed": before != after,
    }


def source_review_alias(raw: str) -> tuple[str, str]:
    return platform_source_review_alias(raw)


def set_configured_source_roots(config: dict[str, Any], paths: Iterable[str]) -> tuple[bool, dict[str, Any], bool]:
    plan = plan_source_batch({**config, "SourceRoots": []}, paths)
    final = plan["FinalSources"]
    if final == config.get("SourceRoots", []):
        return False, plan, False
    candidate = copy.deepcopy(config)
    candidate["SourceRoots"] = final
    saved = config_module.save_runtime_config(candidate)
    return saved, plan, not saved


def add_configured_source_roots(config: dict[str, Any], paths: Iterable[str]) -> tuple[bool, dict[str, Any], bool]:
    plan = plan_source_batch(config, paths)
    if not plan["Changed"]:
        return False, plan, False
    candidate = copy.deepcopy(config)
    candidate["SourceRoots"] = plan["FinalSources"]
    saved = config_module.save_runtime_config(candidate)
    return saved, plan, not saved


def remove_configured_source_root(config: dict[str, Any], root_id: str) -> tuple[bool, bool]:
    candidate = copy.deepcopy(config)
    candidate["SourceRoots"] = [r for r in candidate["SourceRoots"] if str(r["RootId"]).casefold() != root_id.casefold()]
    saved = config_module.save_runtime_config(candidate)
    return saved, not saved


def add_configured_outputs(config: dict[str, Any], base_paths: Iterable[str]) -> dict[str, Any]:
    outputs = list(config["Outputs"])
    added = 0
    issues: list[dict[str, Any]] = []
    for raw_value in base_paths:
        raw = remove_outer_quotes(str(raw_value))
        if not raw.strip():
            continue
        try:
            desc = resolve_storage(raw, True)
            desc = adopt_storage_identity(desc, [*outputs, *config.get("SourceRoots", [])])
            candidate = config_module._descriptor_to_output(desc)
        except Exception as exc:
            issues.append({"Type": "INVALID", "Path": raw, "Error": str(exc)})
            write_sampler_log(f"Output path rejected path={raw!r} error={exc}", "WARNING", "PATH")
            continue
        ident = config_module._identity_tuple(candidate, "BaseRelativePath")
        if any(config_module._identity_tuple(x, "BaseRelativePath") == ident for x in outputs):
            issues.append({"Type": "ALREADY_CONFIGURED", "Path": str(candidate["BasePath"])})
            continue
        overlap = next((x for x in outputs if config_module._objects_overlap(x, candidate, "BaseRelativePath")), None)
        if overlap:
            issues.append({"Type": "OVERLAP", "Path": str(candidate["BasePath"])})
            continue
        outputs.append(candidate)
        added += 1
    save_failed = False
    if added:
        candidate_config = copy.deepcopy(config)
        candidate_config["Outputs"] = outputs
        if not config_module.save_runtime_config(candidate_config):
            added = 0
            save_failed = True
    return {"Added": added, "Issues": issues, "SaveFailed": save_failed, "Changed": bool(added)}


def remove_configured_output(config: dict[str, Any], library_id: str) -> tuple[bool, bool]:
    candidate = copy.deepcopy(config)
    candidate["Outputs"] = [o for o in candidate["Outputs"] if str(o["LibraryId"]).lower() != library_id.lower()]
    saved = config_module.save_runtime_config(candidate)
    return saved, not saved


def get_output_library_path(config: dict[str, Any], output: dict[str, Any]) -> str:
    return join_path(str(output["BasePath"]), str(config["LibraryName"]))


def import_paths_file(path: Path) -> list[str]:
    result = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            result.append(remove_outer_quotes(line))
    return result



def valid_library_marker_object(marker: Any) -> bool:
    try:
        return (
            isinstance(marker, dict)
            and bool(marker.get("ProgramVersion"))
            and marker.get("LibraryType") == LIBRARY_TYPE
            and int(marker.get("MarkerSchemaVersion")) == MARKER_SCHEMA_VERSION
            and uuid.UUID(str(marker.get("LibraryId"))).int != 0
        )
    except Exception:
        return False


def recognized_sampler_library_directory(directory: str) -> bool:
    marker_path = Path(directory) / ".mediadiversity-library.json"
    if not marker_path.is_file():
        return False
    try:
        return valid_library_marker_object(read_json_file(marker_path))
    except Exception as exc:
        write_sampler_log(f"Could not validate potential library marker at {marker_path}: {exc}", "WARNING", "PATH")
        return False


def configured_output_library_paths(config: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for output in config["Outputs"]:
        try:
            result.append(get_output_library_path(config, output))
        except Exception:
            pass
    return result
