# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol

from .constants import LIBRARY_TYPE, MANIFEST_SCHEMA_VERSION, MARKER_SCHEMA_VERSION, APP_VERSION
from .datetime_helpers import parse_utc_timestamp, stat_mtime_o, utc_now_o
from .file_io import read_json_file, write_json_file_atomically, write_text_file_atomically
from .interrupts import graceful_stop_requested
from .localization import display_mode_name, t, tp
from .logger import format_elapsed, write_sampler_log
from .paths import get_output_library_path, valid_library_marker_object
from .platform_api import (
    basename, dirname, is_path_ancestor, is_link_or_reparse_point, join_path, link_violation_message, no_link_ancestors,
    normalize_path, normcase, normpath, paths_equal, relative_is_ancestor, relative_key, relative_parts,
    storage_relative_path, valid_managed_relative_path,
)



class BuildRenderer(Protocol):
    def staged_recovery_failed(self) -> None: ...
    def staged_recovery_pending(self) -> None: ...
    def clear_progress(self) -> None: ...
    def copy_progress(self, mode: str, current: int, total: int, file_name: str) -> None: ...
    def validation_progress(self, mode: str, current: int, total: int) -> None: ...
    def copy_failed(self, mode: str, file_name: str) -> None: ...
    def timestamp_mismatch(self, mode: str, file_name: str) -> None: ...
    def manifest_update_failed(self, mode: str) -> None: ...
    def validation_failed(self, mode: str, file_name: str) -> None: ...
    def txt_write_failed(self, mode: str) -> None: ...
    def build_progress_started(self) -> None: ...
    def build_output_started(self, number: int, total: int, library_path: str, separate: bool) -> None: ...
    def unexpected_copy_failure(self, mode: str) -> None: ...
    def build_manifest_summary(self, summary: dict[str, Any]) -> None: ...


def get_library_marker_path(library_path:str)->str: return join_path(library_path,".mediadiversity-library.json")


def get_build_mode_folders(mode:str)->list[str]:
    return {"SDR":["SDR"],"HDR":["HDR"],"Uncertain":["SDR Uncertain","HDR Uncertain"]}[mode]


def get_manifest_file_name(mode:str)->str:
    return {"SDR":".mediadiversity-manifest-sdr.json","HDR":".mediadiversity-manifest-hdr.json","Uncertain":".mediadiversity-manifest-uncertain.json"}[mode]


def get_manifest_path(library_path:str, mode:str)->str: return join_path(library_path,get_manifest_file_name(mode))


def get_staged_manifest_path(library_path: str, mode: str) -> str:
    name = get_manifest_file_name(mode)
    return join_path(library_path, name[:-5] + ".stage.json")


def get_txt_file_name(mode: str) -> str:
    return {
        "SDR": "MediaDiversity_SDR.txt",
        "HDR": "MediaDiversity_HDR.txt",
        "Uncertain": "MediaDiversity_Uncertain.txt",
    }[mode]


def get_txt_path(library_path: str, mode: str) -> str:
    return join_path(library_path, get_txt_file_name(mode))


def new_library_marker(library_id:str)->dict[str,Any]:
    return {"ProgramVersion":APP_VERSION,"MarkerSchemaVersion":MARKER_SCHEMA_VERSION,"LibraryType":LIBRARY_TYPE,"LibraryId":library_id,"CreatedAtUtc":utc_now_o()}


def test_output_library_marker(library_path:str, expected_library_id:str)->dict[str,Any]:
    try:
        if not no_link_ancestors(library_path): raise ValueError(link_violation_message("output_library_traverses"))
    except Exception as exc: return {"Valid":False,"Reason":str(exc),"Marker":None}
    if os.path.isdir(library_path) and is_link_or_reparse_point(library_path): return {"Valid":False,"Reason":link_violation_message("output_library_root"),"Marker":None}
    marker_path=get_library_marker_path(library_path)
    if os.path.exists(marker_path) and is_link_or_reparse_point(marker_path): return {"Valid":False,"Reason":link_violation_message("marker"),"Marker":None}
    if not os.path.isfile(marker_path): return {"Valid":False,"Reason":"Library marker is missing.","Marker":None}
    try: marker=read_json_file(Path(marker_path))
    except Exception: return {"Valid":False,"Reason":"Library marker is unreadable.","Marker":None}
    if not valid_library_marker_object(marker): return {"Valid":False,"Reason":"Library marker schema or type is invalid.","Marker":marker}
    if str(marker["LibraryId"]).lower()!=expected_library_id.lower(): return {"Valid":False,"Reason":"LibraryId does not match the configured output.","Marker":marker}
    return {"Valid":True,"Reason":None,"Marker":marker}


def initialize_new_output_library(config: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    base = str(output["BasePath"])
    library = get_output_library_path(config, output)
    try:
        if not no_link_ancestors(library):
            raise ValueError(link_violation_message("output_path_traverses"))
    except Exception as exc:
        return {"Success": False, "Reason": str(exc), "LibraryPath": library}

    if not os.path.isdir(base):
        return {"Success": False, "Reason": "Output base path is not available.", "LibraryPath": library}
    if os.path.isfile(library):
        return {"Success": False, "Reason": "Output library path exists as a file.", "LibraryPath": library}
    if os.path.isdir(library):
        marker = test_output_library_marker(library, str(output["LibraryId"]))
        return {"Success": bool(marker["Valid"]), "Reason": marker["Reason"], "LibraryPath": library}

    created = False
    try:
        os.makedirs(library, exist_ok=False)
        created = True
        marker_path = get_library_marker_path(library)
        write_json_file_atomically(Path(marker_path), new_library_marker(str(output["LibraryId"])))
        write_sampler_log(f"Created output library marker: {marker_path}", "OK")
        return {"Success": True, "Reason": None, "LibraryPath": library}
    except Exception as exc:
        if created:
            try:
                if os.path.isdir(library) and not os.listdir(library):
                    os.rmdir(library)
            except Exception:
                pass
        write_sampler_log(f"Could not create output library {library}: {exc}", "ERROR")
        return {"Success": False, "Reason": "Output library could not be created.", "LibraryPath": library}


def test_output_library_source_conflict(library_path:str, config:dict[str,Any])->dict[str,Any]:
    output=next((x for x in config["Outputs"] if paths_equal(get_output_library_path(config,x),library_path)),None)
    if output is not None:
        out_storage=str(output.get("StorageIdentityKey") or "").casefold(); base_rel=str(output.get("BaseRelativePath") or ""); library_rel=normpath(join_path(base_rel,str(config["LibraryName"]))) if base_rel else str(config["LibraryName"])
        for root in config["SourceRoots"]:
            if out_storage and out_storage==str(root.get("StorageIdentityKey") or "").casefold():
                source_rel=str(root.get("SourceRootRelativePath") or "")
                if relative_key(source_rel)==relative_key(library_rel) or relative_is_ancestor(source_rel,library_rel) or relative_is_ancestor(library_rel,source_rel):
                    return {"Conflict":True,"SourcePath":str(root["Path"]),"Reason":"Output library overlaps a media source on the same storage."}
    for root in config["SourceRoots"]:
        try:
            source=str(root["Path"])
            if is_path_ancestor(library_path,source,True): return {"Conflict":True,"SourcePath":source,"Reason":"Output library is inside a media source root."}
            if is_path_ancestor(source,library_path,True): return {"Conflict":True,"SourcePath":source,"Reason":"A media source root is inside the output library."}
        except Exception: return {"Conflict":True,"SourcePath":str(root["Path"]),"Reason":"Source/output relationship could not be validated safely."}
    return {"Conflict":False,"SourcePath":None,"Reason":None}


def test_mode_folder_safety(library_path:str, folder_name:str)->dict[str,Any]:
    target=join_path(library_path,folder_name)
    try:
        if not no_link_ancestors(target): raise ValueError(link_violation_message("mode_path_traverses"))
        if not is_path_ancestor(target,library_path): return {"Safe":False,"Path":target,"Reason":"Mode folder is not inside the verified library path."}
    except Exception as exc: return {"Safe":False,"Path":target,"Reason":str(exc)}
    if os.path.isfile(target): return {"Safe":False,"Path":target,"Reason":"Mode folder path exists as a file."}
    if os.path.isdir(target) and is_link_or_reparse_point(target): return {"Safe":False,"Path":target,"Reason":link_violation_message("mode_folder")}
    return {"Safe":True,"Path":target,"Reason":None}


def reliable_free_space_bytes(path:str)->Optional[int]:
    try: return int(shutil.disk_usage(normalize_path(path)).free)
    except Exception as exc:
        write_sampler_log(f"Free-space query failed path={path} error={exc}","WARNING","PREFLIGHT"); return None


def test_output_mode_write_access(library_path:str, mode_folders:list[str])->dict[str,Any]:
    for folder in mode_folders:
        safety=test_mode_folder_safety(library_path,folder)
        if not safety["Safe"]: return {"Success":False,"Reason":safety["Reason"]}
        path=safety["Path"]; created=False
        try:
            if not os.path.isdir(path): os.makedirs(path); created=True
            test_path=join_path(path,f".mediadiversity-write-test-{uuid.uuid4().hex}.tmp")
            Path(test_path).write_text("write-test",encoding="utf-8"); os.remove(test_path)
        except Exception: return {"Success":False,"Reason":f"Write access test failed for {folder}."}
        finally:
            if created:
                try:
                    if os.path.isdir(path) and not os.listdir(path): os.rmdir(path)
                except Exception: pass
    return {"Success":True,"Reason":None}


def safe_mode_tree_bytes(library_path:str, folders:list[str])->int:
    total=0
    for folder in folders:
        safety=test_mode_folder_safety(library_path,folder)
        if not safety["Safe"]: raise ValueError(safety["Reason"])
        if not os.path.isdir(safety["Path"]): continue
        stack=[safety["Path"]]
        while stack:
            d=stack.pop()
            with os.scandir(d) as it:
                for child in it:
                    if child.is_symlink() or is_link_or_reparse_point(child.path): raise ValueError(link_violation_message("mode_tree"))
                    if child.is_dir(follow_symlinks=False): stack.append(child.path)
                    else: total+=child.stat(follow_symlinks=False).st_size
    return total


def convert_media_for_manifest(record:dict[str,Any])->dict[str,Any]:
    m=record["Media"]; c=record["Classification"]
    keys=["Codec","Profile","Level","Width","Height","ResolutionClass","PixelFormat","Chroma","BitDepth","Fps","FpsClass","FrameRateMode","ScanType","DurationSeconds","DurationClass","BitRateBitsPerSecond","BitRateSource","BitRateClass","Container","AspectRatioClass","ColorRange","ColorSpace","ColorTransfer","ColorPrimaries"]
    result={k:m.get(k) for k in keys}; result.update({"HDRType":c["HDRType"],"HDRMetadataClass":c["HDRMetadataClass"],"DolbyVisionProfile":m["DolbyVisionProfile"],"Anomalies":list(c["Anomalies"]),"UnknownReason":c["UnknownReason"]}); return result


def destination_relative_path_for_record(record:dict[str,Any])->str:
    return join_path(str(record["Pool"]),str(record["Media"]["RootLabel"]),str(record["Media"]["RelativePath"]))


def selection_parameters_snapshot(mode:str)->dict[str,Any]:
    from .selection import (
        DOLBY_VISION_PROFILE_SUPPLEMENTAL_WEIGHT, HDR_ANOMALY_WEIGHT, HDR_PAIR_WEIGHTS,
        HDR_PRIMARY_WEIGHTS, HDR_SECONDARY_WEIGHTS, RARITY_MAXIMUM_FRACTION, SDR_PAIR_WEIGHTS,
        SDR_PRIMARY_WEIGHTS, SDR_SECONDARY_WEIGHTS,
    )
    def pair_list(pairs): return [{"Left":a,"Right":b,"Weight":w} for a,b,w in pairs]
    return {"BuildMode":mode,"FrameRateModeSelectionEnabled":False,"ProfileIdentityIncludesCodec":True,"FpsTolerance":0.1,"HdrBalancedFillIncludesCodecResolution":True,"RarityMaximumFraction":RARITY_MAXIMUM_FRACTION,"SdrPrimaryWeights":dict(SDR_PRIMARY_WEIGHTS),"SdrSecondaryWeights":dict(SDR_SECONDARY_WEIGHTS),"SdrPairWeights":pair_list(SDR_PAIR_WEIGHTS),"HDRPrimaryWeights":dict(HDR_PRIMARY_WEIGHTS),"HDRSecondaryWeights":dict(HDR_SECONDARY_WEIGHTS),"HDRPairWeights":pair_list(HDR_PAIR_WEIGHTS),"HDRAnomalyWeight":HDR_ANOMALY_WEIGHT,"DolbyVisionProfileSupplementalWeight":DOLBY_VISION_PROFILE_SUPPLEMENTAL_WEIGHT}


def new_build_snapshot(
    config: dict[str, Any],
    selection_mode: str,
    build_mode: str,
    pool_selections: list[dict[str, Any]],
    selection_id: Optional[str] = None,
    selection_created_at: Optional[str] = None,
) -> dict[str, Any]:
    # Despite the historical helper name, this object is a selection snapshot.
    # BuildId is intentionally created only after final build confirmation.
    selection_id = selection_id or str(uuid.uuid4())
    selection_created_at = selection_created_at or utc_now_o()
    selected_files: list[dict[str, Any]] = []
    requested: dict[str, int] = {}
    recommended: dict[str, int] = {}
    order = 0
    for pool_selection in pool_selections:
        pool = str(pool_selection["Pool"])
        selection = pool_selection["Selection"]
        requested[pool] = int(selection["RequestedCount"])
        recommended[pool] = int(selection["RecommendedMinimum"])
        for selected in selection["Selected"]:
            record = selected["Record"]
            order += 1
            selected_files.append({
                "SelectionOrder": order,
                "Pool": str(record["Pool"]),
                "SourcePath": str(record["Path"]),
                "SourceLength": int(record["Media"]["FileSizeBytes"]),
                "SourceLastWriteTimeUtc": str(record["Media"]["SourceLastWriteTimeUtc"]),
                "DestinationRelativePath": destination_relative_path_for_record(record),
                "Media": convert_media_for_manifest(record),
                "ReasonForSelection": selected["ReasonForSelection"],
            })
    return {
        "ProgramVersion": APP_VERSION,
        "ManifestSchemaVersion": MANIFEST_SCHEMA_VERSION,
        "SelectionId": selection_id,
        "SelectionMode": selection_mode,
        "BuildMode": build_mode,
        "SelectionCreatedAtUtc": selection_created_at,
        "RequestedCounts": requested,
        "RecommendedMinimums": recommended,
        "SourceRoots": [{"RootId": str(r["RootId"]), "Label": str(r["Label"]), "Path": str(r["Path"])} for r in config["SourceRoots"]],
        "SelectionParameters": selection_parameters_snapshot(build_mode),
        "SelectedFiles": selected_files,
    }


def new_output_manifest_from_snapshot(
    snapshot: dict[str, Any],
    output: dict[str, Any],
    library_path: str,
    build_id: str,
    build_created_at: str,
    build_modes: list[str],
    build_library_ids: list[str],
) -> dict[str, Any]:
    items = []
    for selected in snapshot["SelectedFiles"]:
        items.append({
            **selected,
            "CopyStatus": "Pending",
            "CopyAttempts": 0,
            "LastCopyExitCode": None,
            "LastCopyMessage": None,
            "CompletedAtUtc": None,
        })
    return {
        "ProgramVersion": APP_VERSION,
        "ManifestSchemaVersion": MANIFEST_SCHEMA_VERSION,
        "SelectionId": snapshot["SelectionId"],
        "BuildId": build_id,
        "SelectionMode": snapshot["SelectionMode"],
        "BuildMode": snapshot["BuildMode"],
        "SelectionCreatedAtUtc": snapshot["SelectionCreatedAtUtc"],
        "BuildCreatedAtUtc": build_created_at,
        "UpdatedAtUtc": utc_now_o(),
        "LibraryId": str(output["LibraryId"]),
        "OutputBasePath": str(output["BasePath"]),
        "LibraryPath": library_path,
        "BuildStatus": "Incomplete",
        "RequestedCounts": dict(snapshot["RequestedCounts"]),
        "RecommendedMinimums": dict(snapshot["RecommendedMinimums"]),
        "SourceRoots": list(snapshot["SourceRoots"]),
        "SelectionParameters": dict(snapshot["SelectionParameters"]),
        "BuildModes": list(build_modes),
        "BuildLibraryIds": list(build_library_ids),
        "SelectedFiles": items,
    }


def get_manifest_item_destination_path(manifest:dict[str,Any], item:dict[str,Any])->str:
    library=str(manifest["LibraryPath"]); destination=join_path(library,str(item["DestinationRelativePath"])); pool=join_path(library,str(item["Pool"]))
    if not is_path_ancestor(destination,library): raise ValueError("Destination path escapes the verified output library.")
    if not is_path_ancestor(destination,pool): raise ValueError("Destination path escapes its verified mode folder.")
    return destination


def test_destination_directory_chain_safe(library_path:str,pool:str,destination_directory:str)->bool:
    try:
        safety=test_mode_folder_safety(library_path,pool)
        if not safety["Safe"]: return False
        mode=safety["Path"]
        if not is_path_ancestor(destination_directory,mode,True): return False
        if os.path.isdir(mode) and is_link_or_reparse_point(mode): return False
        rel=storage_relative_path(destination_directory,mode); current=mode
        for segment in relative_parts(rel):
            if not segment: continue
            current=join_path(current,segment)
            if os.path.isdir(current) and is_link_or_reparse_point(current): return False
        return True
    except Exception: return False


def destination_file_valid(path:str,expected_length:int)->bool:
    try: return os.path.isfile(path) and not is_link_or_reparse_point(path) and os.stat(path,follow_symlinks=False).st_size==expected_length
    except Exception: return False


def test_build_manifest_shape(manifest: Any, expected_mode: str, expected_library_path: str) -> bool:
    try:
        if not isinstance(manifest, dict):
            return False
        if type(manifest.get("ManifestSchemaVersion")) is not int or manifest["ManifestSchemaVersion"] != MANIFEST_SCHEMA_VERSION:
            return False
        if not isinstance(manifest.get("SelectedFiles"), list) or not manifest["SelectedFiles"]:
            return False
        if not isinstance(manifest.get("SourceRoots"), list):
            return False
        if any(not isinstance(manifest.get(name), dict) for name in ("RequestedCounts", "RecommendedMinimums", "SelectionParameters")):
            return False
        required = (
            "ProgramVersion", "ManifestSchemaVersion", "SelectionId", "BuildId", "SelectionMode", "BuildMode",
            "SelectionCreatedAtUtc", "BuildCreatedAtUtc", "UpdatedAtUtc", "LibraryId", "OutputBasePath", "LibraryPath",
            "BuildStatus", "RequestedCounts", "RecommendedMinimums", "SourceRoots", "SelectionParameters",
            "BuildModes", "BuildLibraryIds", "SelectedFiles",
        )
        if any(key not in manifest for key in required):
            return False
        if str(manifest["BuildMode"]).casefold() != expected_mode.casefold() or not paths_equal(str(manifest["LibraryPath"]), expected_library_path):
            return False
        if str(manifest["SelectionMode"]) not in {"ALL", "SDR", "HDR", "Uncertain"}:
            return False
        uuid.UUID(str(manifest["SelectionId"])); uuid.UUID(str(manifest["BuildId"])); uuid.UUID(str(manifest["LibraryId"]))
        if manifest["BuildStatus"] not in {"Complete", "Incomplete"}:
            return False
        if not isinstance(manifest["BuildModes"], list) or not manifest["BuildModes"] or len(set(manifest["BuildModes"])) != len(manifest["BuildModes"]):
            return False
        if any(mode not in {"SDR", "HDR", "Uncertain"} for mode in manifest["BuildModes"]):
            return False
        if expected_mode not in manifest["BuildModes"]:
            return False
        if not isinstance(manifest["BuildLibraryIds"], list) or not manifest["BuildLibraryIds"] or len(set(str(x).casefold() for x in manifest["BuildLibraryIds"])) != len(manifest["BuildLibraryIds"]):
            return False
        for library_id in manifest["BuildLibraryIds"]:
            uuid.UUID(str(library_id))
        if str(manifest["LibraryId"]).casefold() not in {str(x).casefold() for x in manifest["BuildLibraryIds"]}:
            return False
        parse_utc_timestamp(manifest["SelectionCreatedAtUtc"])
        parse_utc_timestamp(manifest["BuildCreatedAtUtc"])
        parse_utc_timestamp(manifest["UpdatedAtUtc"])
        allowed = set(get_build_mode_folders(expected_mode))
        destinations: set[str] = set(); sources: set[str] = set(); orders: set[int] = set()
        for item in manifest["SelectedFiles"]:
            req = (
                "SelectionOrder", "Pool", "SourcePath", "SourceLength", "SourceLastWriteTimeUtc", "DestinationRelativePath",
                "CopyStatus", "CopyAttempts", "LastCopyExitCode", "LastCopyMessage", "CompletedAtUtc", "Media", "ReasonForSelection",
            )
            if not isinstance(item, dict) or any(key not in item for key in req):
                return False
            if item["Pool"] not in allowed or not str(item["SourcePath"]).strip() or not str(item["SourceLastWriteTimeUtc"]).strip() or not str(item["DestinationRelativePath"]).strip():
                return False
            if type(item["SelectionOrder"]) is not int or type(item["SourceLength"]) is not int or type(item["CopyAttempts"]) is not int:
                return False
            if item["SourceLength"] < 0 or item["SelectionOrder"] < 1 or item["CopyAttempts"] < 0:
                return False
            if item["LastCopyExitCode"] is not None and type(item["LastCopyExitCode"]) is not int:
                return False
            if item["CopyStatus"] not in {"Pending", "Copying", "Completed", "Failed"} or not isinstance(item["Media"], dict) or not isinstance(item["ReasonForSelection"], dict):
                return False
            parse_utc_timestamp(item["SourceLastWriteTimeUtc"])
            source = normcase(normalize_path(str(item["SourcePath"])))
            if source in sources or item["SelectionOrder"] in orders:
                return False
            sources.add(source); orders.add(item["SelectionOrder"])
            rel = str(item["DestinationRelativePath"])
            if not valid_managed_relative_path(rel):
                return False
            dest = normcase(normalize_path(get_manifest_item_destination_path(manifest, item)))
            if dest in destinations:
                return False
            destinations.add(dest)
        return True
    except Exception:
        return False


def write_build_manifest(manifest: dict[str, Any]) -> None:
    marker = test_output_library_marker(str(manifest["LibraryPath"]), str(manifest["LibraryId"]))
    if not marker["Valid"]:
        raise ValueError(f"Manifest checkpoint blocked: {marker['Reason']}")
    if not test_build_manifest_shape(manifest, str(manifest["BuildMode"]), str(manifest["LibraryPath"])):
        raise ValueError("Manifest checkpoint contract is invalid.")
    manifest["ProgramVersion"] = APP_VERSION
    manifest["UpdatedAtUtc"] = utc_now_o()
    write_json_file_atomically(Path(get_manifest_path(str(manifest["LibraryPath"]), str(manifest["BuildMode"]))), manifest)


def write_staged_manifest(manifest: dict[str, Any]) -> None:
    marker = test_output_library_marker(str(manifest["LibraryPath"]), str(manifest["LibraryId"]))
    if not marker["Valid"]:
        raise ValueError(f"Staged manifest blocked: {marker['Reason']}")
    if not test_build_manifest_shape(manifest, str(manifest["BuildMode"]), str(manifest["LibraryPath"])):
        raise ValueError("Staged manifest contract is invalid.")
    manifest["ProgramVersion"] = APP_VERSION
    manifest["UpdatedAtUtc"] = utc_now_o()
    write_json_file_atomically(Path(get_staged_manifest_path(str(manifest["LibraryPath"]), str(manifest["BuildMode"]))), manifest)


def read_manifest_file(path: str, expected_mode: str, expected_library_path: str) -> Optional[dict[str, Any]]:
    candidate = Path(path)
    if not candidate.is_file():
        return None
    try:
        manifest = read_json_file(candidate)
    except Exception:
        return None
    if not test_build_manifest_shape(manifest, expected_mode, expected_library_path):
        return None
    return manifest


def snapshot_total_bytes(snapshot:dict[str,Any])->int: return sum(int(i["SourceLength"]) for i in snapshot["SelectedFiles"])


def format_byte_size(bytes_:float)->str:
    if bytes_>=1024**4: return f"{bytes_/1024**4:,.2f} TB"
    if bytes_>=1024**3: return f"{bytes_/1024**3:,.2f} GB"
    if bytes_>=1024**2: return f"{bytes_/1024**2:,.2f} MB"
    return f"{bytes_:,.0f} bytes"


def _english_count(count: int, singular: str, plural: Optional[str] = None) -> str:
    key = {"file": "count.files", "location": "count.locations", "output": "count.outputs", "combo": "count.combos"}.get(singular)
    if key:
        return tp(key, count)
    word = singular if count == 1 else (plural if plural is not None else singular + "s")
    return f"{count} {word}"


_DISPLAY_FIELD_NAMES = {
    "ResolutionClass": "metadata.resolution", "BitDepth": "metadata.bit_depth", "FpsClass": "metadata.fps", "ScanType": "metadata.scan",
    "DurationClass": "metadata.duration", "BitRateClass": "metadata.bitrate", "AspectRatioClass": "metadata.aspect", "HDRType": "metadata.hdr_type",
    "ColorTransfer": "metadata.transfer", "ColorPrimaries": "metadata.primaries", "ColorSpace": "metadata.color_space", "HDRMetadataClass": "metadata.hdr_metadata",
    "DolbyVisionProfile": "metadata.dv_profile", "Profile": "metadata.profile", "Codec": "metadata.codec", "Chroma": "metadata.chroma", "Container": "metadata.container", "ColorRange": "metadata.color_range",
}


def _display_metadata_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    text = str(value).strip()
    return "" if text.casefold() in {"unknown", "n/a", "none", "null"} else text


def _format_metadata_row(fields: list[tuple[str, Any]]) -> str:
    cells = []
    for label, value in fields:
        cell = f"{label}: {_display_metadata_value(value)}" if label else ""
        cells.append(cell.ljust(25))
    return " | ".join(cells).rstrip()


def _localize_unknown_reason(reason: str) -> str:
    static = {
        "SDR transfer is missing or unspecified.": "unknown.sdr_transfer_missing",
        "DV profile, compatibility ID or RPU/BL/EL flag is missing.": "unknown.dv_fields_missing",
        "DV profile is outside the verified profile matrix.": "unknown.dv_profile",
        "DV codec is missing.": "unknown.dv_codec_missing",
        "DV RPU/BL/EL flags do not match the verified configuration.": "unknown.dv_flags",
        "DV is reported as interlaced.": "unknown.dv_interlaced",
        "Usable static HDR metadata is present, but no supported HDR type is confirmed.": "unknown.hdr_type",
        "Component bit depth cannot be determined reliably.": "unknown.bit_depth_missing",
        "Pixel format/chroma cannot be determined reliably.": "unknown.chroma_missing",
        "DV chroma is outside the verified 4:2:0 configuration.": "unknown.dv_chroma",
        "Unconfirmed proprietary DV transfer.": "unknown.dv_transfer",
        "Unconfirmed proprietary DV primaries.": "unknown.dv_primaries",
        "Unconfirmed proprietary DV matrix; expected IPT-PQ-C2 or unspecified.": "unknown.dv_matrix",
        "Additional HDR10+ signal without a confirmed PQ base context.": "unknown.hdr10plus_without_pq",
    }
    parts = [part.strip() for part in re.split(r"(?<=\.)\s+", str(reason).strip()) if part.strip()]
    localized: list[str] = []
    for part in parts:
        key = static.get(part)
        if key:
            localized.append(t(key))
            continue
        match = re.fullmatch(r"Transfer is outside the confirmed SDR mapping: (.+)\.", part)
        if match:
            localized.append(t("unknown.sdr_transfer_outside", transfer=match.group(1))); continue
        match = re.fullmatch(r"SDR transfer with an unconfirmed matrix combination: (.+)\.", part)
        if match:
            localized.append(t("unknown.sdr_matrix", matrix=match.group(1))); continue
        match = re.fullmatch(r"BT\.2020 transfer (.+) expects (\d+)-bit; reported (.+)\.", part)
        if match:
            localized.append(t("unknown.bt2020_depth", transfer=match.group(1), expected_depth=match.group(2), reported=match.group(3))); continue
        match = re.fullmatch(r"BT\.2020 transfer (.+) expects bt2020 primaries; reported (.+)\.", part)
        if match:
            localized.append(t("unknown.bt2020_primaries", transfer=match.group(1), reported=match.group(2))); continue
        match = re.fullmatch(r"BT\.2020 transfer (.+) expects bt2020nc or bt2020c matrix; reported (.+)\.", part)
        if match:
            localized.append(t("unknown.bt2020_matrix", transfer=match.group(1), reported=match.group(2))); continue
        match = re.fullmatch(r"Unconfirmed DV profile/compatibility combination: (.+)/(.+)\.", part)
        if match:
            localized.append(t("unknown.dv_profile_compat", profile=match.group(1), compat=match.group(2))); continue
        match = re.fullmatch(r"DV profile (.+) expects (.+); reported (.+)\.", part)
        if match:
            localized.append(t("unknown.dv_codec", profile=match.group(1), expected_codec=match.group(2), codec=match.group(3))); continue
        match = re.fullmatch(r"Bit depth (.+) is outside the verified configuration\.", part)
        if match:
            localized.append(t("unknown.bit_depth", depth=match.group(1))); continue
        match = re.fullmatch(r"HDR (.+) is missing or unspecified\.", part)
        if match:
            localized.append(t("unknown.hdr_missing", name=match.group(1))); continue
        match = re.fullmatch(r"Reported transfer (.+) conflicts with the declared HDR configuration \((.+)\)\.", part)
        if match:
            localized.append(t("unknown.hdr_transfer_conflict", value=match.group(1), expected=match.group(2))); continue
        match = re.fullmatch(r"Unconfirmed HDR (.+): (.+); expected (.+)\.", part)
        if match:
            localized.append(t("unknown.hdr_unconfirmed", name=match.group(1), value=match.group(2), expected=match.group(3))); continue
        generic = t("unknown.generic")
        if generic not in localized:
            localized.append(generic)
    return " ".join(localized)


def metadata_lines(media: dict[str, Any], pool: str) -> list[str]:
    label = _display_field_label
    common = [
        [(label("ResolutionClass"), media.get("ResolutionClass")), (label("Codec"), media.get("Codec")), (label("Profile"), media.get("Profile")), (label("BitDepth"), media.get("BitDepth"))],
        [(label("Chroma"), media.get("Chroma")), (label("FpsClass"), media.get("Fps")), (label("ScanType"), media.get("ScanType")), (label("Container"), media.get("Container"))],
        [(label("DurationClass"), media.get("DurationClass")), (label("BitRateClass"), media.get("BitRateClass")), (label("AspectRatioClass"), media.get("AspectRatioClass")), ("", None)],
    ]
    lines = [_format_metadata_row(row) for row in common]
    if "Uncertain" in str(pool):
        reason = str(media.get("UnknownReason") or "").strip()
        if reason:
            lines.append(t("selection.uncertain_reason", reason=_localize_unknown_reason(reason)))
    if str(pool).startswith("HDR"):
        lines.extend([
            _format_metadata_row([(label("HDRType"), media.get("HDRType")), (label("DolbyVisionProfile"), media.get("DolbyVisionProfile")), (label("ColorTransfer"), media.get("ColorTransfer")), (label("ColorPrimaries"), media.get("ColorPrimaries"))]),
            _format_metadata_row([(label("ColorSpace"), media.get("ColorSpace")), (label("HDRMetadataClass"), media.get("HDRMetadataClass")), ("", None), ("", None)]),
        ])
    return lines


def _display_field_label(name: str) -> str:
    key = _DISPLAY_FIELD_NAMES.get(name)
    return t(key) if key else re.sub(r"(?<!^)(?=[A-Z])", " ", name)


def selection_reason_text(reason: dict[str, Any]) -> str:
    labels: list[str] = []
    values = [*reason.get("NewPrimary", []), *reason.get("NewSecondary", []), *reason.get("NewSupplemental", [])]
    for raw in values:
        text = str(raw)
        label: Optional[str] = None
        if text.startswith("New combination: "):
            body = text[len("New combination: "):]
            names = [part.split("=", 1)[0].strip() for part in body.split(" x ") if "=" in part]
            if names:
                label = t("selection.reason_new_pair", labels="×".join(_display_field_label(name) for name in names))
        elif text.startswith("New ") and ":" in text:
            name = text[4:].split(":", 1)[0].strip()
            label = t("selection.reason_new", label=_display_field_label(name))
        elif text.startswith("Secondary diversity: "):
            body = text[len("Secondary diversity: "):]
            name = body.split("=", 1)[0].strip()
            label = t("selection.reason_new", label=_display_field_label(name))
        elif text.startswith("Additional Dolby Vision profile:"):
            label = t("selection.reason_new_dv_profile")
        elif text.startswith("HDR anomaly: "):
            continue
        if label is None:
            label = t("selection.reason_other")
        if label not in labels:
            labels.append(label)
    rarity_label = t("selection.reason_rarity")
    if float(reason.get("RarityBonus") or 0.0) > 0 and rarity_label not in labels:
        labels.append(rarity_label)
    balanced_label = t("selection.reason_balanced")
    if bool(reason.get("BalancedFill")) and balanced_label not in labels:
        labels.append(balanced_label)
    return " | ".join(labels)


def selected_item_lines(item: dict[str, Any], path: str) -> list[str]:
    lines = [path, "-" * 72]
    lines.extend(metadata_lines(item["Media"], str(item["Pool"])))
    lines.append(f"{t('selection.selected_label')} {selection_reason_text(item['ReasonForSelection'])}")
    return lines


def selected_item_ux_lines(item: dict[str, Any]) -> list[str]:
    media = item["Media"]
    label = _display_field_label
    fields = [
        [(label("ResolutionClass"), media.get("ResolutionClass")), (label("Codec"), media.get("Codec")), (label("Profile"), media.get("Profile")), (label("BitDepth"), media.get("BitDepth"))],
        [(label("Chroma"), media.get("Chroma")), (label("FpsClass"), media.get("Fps")), (label("ScanType"), media.get("ScanType")), (label("Container"), media.get("Container"))],
        [(label("DurationClass"), media.get("DurationClass")), (label("BitRateClass"), media.get("BitRateClass")), (label("AspectRatioClass"), media.get("AspectRatioClass"))],
    ]
    if str(item.get("Pool", "")).startswith("HDR"):
        fields.extend([
            [(label("HDRType"), media.get("HDRType")), (label("DolbyVisionProfile"), media.get("DolbyVisionProfile"))],
            [(label("ColorTransfer"), media.get("ColorTransfer")), (label("ColorPrimaries"), media.get("ColorPrimaries")), (label("ColorSpace"), media.get("ColorSpace"))],
            [(label("HDRMetadataClass"), media.get("HDRMetadataClass"))],
        ])
    lines = [basename(str(item["SourcePath"])), "-" * 72]
    for row in fields:
        cells = [f"{label}: {_display_metadata_value(value)}" for label, value in row if _display_metadata_value(value)]
        if cells:
            lines.append(" | ".join(cells))
    if "Uncertain" in str(item.get("Pool", "")):
        uncertain_reason = str(media.get("UnknownReason") or "").strip()
        if uncertain_reason:
            lines.append(t("selection.uncertain_reason", reason=_localize_unknown_reason(uncertain_reason)))
    lines.extend(["", t("selection.selected_label")])
    reason = selection_reason_text(item["ReasonForSelection"])
    current = ""
    for part in [x.strip() for x in reason.split("|")]:
        candidate = part if not current else current + " | " + part
        if len(candidate) > 100 and current:
            lines.append(current)
            current = part
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _compact_terminal_width() -> int:
    try:
        return max(48, min(72, shutil.get_terminal_size((120, 20)).columns - 1))
    except Exception:
        return 72


def _compact_filename(name: str, prefix_width: int) -> str:
    available = max(8, _compact_terminal_width() - prefix_width)
    if len(name) <= available:
        return name
    return name[:max(1, available - 1)] + "…"


def _compact_reason_parts(reason: dict[str, Any]) -> tuple[list[str], int]:
    fields: list[str] = []
    combos = 0
    values = [*reason.get("NewPrimary", []), *reason.get("NewSecondary", []), *reason.get("NewSupplemental", [])]
    for raw in values:
        value = str(raw)
        if value.startswith("New combination: "):
            combos += 1
            continue
        if value.startswith("HDR anomaly: "):
            continue
        label: Optional[str] = None
        if value.startswith("New ") and ":" in value:
            name = value[4:].split(":", 1)[0].strip()
            label = _display_field_label(name)
        elif value.startswith("Secondary diversity: "):
            body = value[len("Secondary diversity: "):]
            name = body.split("=", 1)[0].strip()
            label = _display_field_label(name)
        elif value.startswith("Additional Dolby Vision profile:"):
            label = _display_field_label("DolbyVisionProfile")
        if label and label not in fields:
            fields.append(label)
    return fields, combos


def _compact_new_lines(reason: dict[str, Any]) -> list[str]:
    fields, combos = _compact_reason_parts(reason)
    suffix = ""
    if combos:
        suffix = " + " + tp("count.combos", combos)
    tokens = fields if fields else [t("selection.new_none")]
    width = _compact_terminal_width()
    prefix = "    " + t("selection.new_prefix")
    continuation = " " * len(prefix)
    lines: list[str] = []
    current = prefix
    for index, token in enumerate(tokens):
        piece = token + ("," if index < len(tokens) - 1 else "")
        separator = "" if current == prefix else " "
        if len(current) + len(separator) + len(piece) > width and current != prefix:
            lines.append(current.rstrip())
            current = continuation + piece
        else:
            current += separator + piece
    if suffix:
        if len(current) + len(suffix) > width and current != prefix:
            lines.append(current.rstrip())
            current = continuation + suffix.strip()
        else:
            current += suffix
    lines.append(current.rstrip())
    return lines


def _compact_selected_item_lines(selected: dict[str, Any], number: int) -> list[str]:
    record = selected["Record"]
    media = convert_media_for_manifest(record)
    file_name = basename(str(record["Path"]))
    prefix = f"{number:>2}  "
    lines = [prefix + _compact_filename(file_name, len(prefix))]

    video = [
        _display_metadata_value(media.get("ResolutionClass")),
        _display_metadata_value(media.get("Codec")),
        _display_metadata_value(media.get("Profile")),
    ]
    bit_depth = _display_metadata_value(media.get("BitDepth"))
    if bit_depth:
        video.append(f"{bit_depth}-bit")
    video.append(_display_metadata_value(media.get("Chroma")))
    lines.append("    " + " · ".join(value for value in video if value))

    timing: list[str] = []
    fps = _display_metadata_value(media.get("Fps"))
    if fps:
        timing.append(f"{fps} fps")
    for key in ("ScanType", "DurationClass", "BitRateClass", "Container", "AspectRatioClass"):
        value = _display_metadata_value(media.get(key))
        if value:
            timing.append(value)
    if timing:
        lines.append("    " + " · ".join(timing))

    if str(record.get("Pool", "")).startswith("HDR"):
        hdr_type = _display_metadata_value(media.get("HDRType"))
        dv_profile = _display_metadata_value(media.get("DolbyVisionProfile"))
        if hdr_type.casefold() == "dolby vision" and dv_profile:
            hdr_type = f"{hdr_type} P{dv_profile}"
        hdr_values = [
            hdr_type,
            _display_metadata_value(media.get("ColorTransfer")),
            _display_metadata_value(media.get("ColorPrimaries")),
            _display_metadata_value(media.get("ColorSpace")),
        ]
        hdr_values = [value for value in hdr_values if value]
        if hdr_values:
            lines.append("    " + t("selection.hdr_line", values=" · ".join(hdr_values)))

    uncertain_reason = str(media.get("UnknownReason") or "").strip()
    if uncertain_reason:
        lines.append("    " + t("selection.uncertain_reason_compact", reason=_localize_unknown_reason(uncertain_reason)))

    lines.extend(_compact_new_lines(selected["ReasonForSelection"]))
    return lines


def write_manifest_txt(manifest: dict[str, Any]) -> None:
    if manifest.get("BuildStatus") != "Complete":
        return
    width = 72
    title = t("txt.selection_mode", mode=display_mode_name(str(manifest["BuildMode"])))
    lines = ["=" * width, title.center(width), "=" * width, ""]
    for item in manifest["SelectedFiles"]:
        destination = get_manifest_item_destination_path(manifest, item)
        lines.extend(selected_item_lines(item, destination))
        lines.append("=" * width)
    write_text_file_atomically(Path(get_txt_path(str(manifest["LibraryPath"]), str(manifest["BuildMode"]))), "\n".join(lines).rstrip() + "\n")


def invoke_output_preflight(config: dict[str, Any], mode: str, required_bytes: int) -> list[dict[str, Any]]:
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    folders = get_build_mode_folders(mode)
    for number, output in enumerate(config["Outputs"], 1):
        library = get_output_library_path(config, output)
        ready = True
        warnings: list[str] = []
        reason: Optional[str] = None
        free: Optional[int] = None
        reclaim = 0
        additional = required_bytes
        conflict = test_output_library_source_conflict(library, config)
        if conflict["Conflict"]:
            ready = False
            reason = conflict["Reason"]
        if ready:
            init = initialize_new_output_library(config, output)
            if not init["Success"]:
                ready = False
                reason = init["Reason"]
        if ready:
            marker = test_output_library_marker(library, str(output["LibraryId"]))
            if not marker["Valid"]:
                ready = False
                reason = marker["Reason"]
        if ready:
            for folder in folders:
                safety = test_mode_folder_safety(library, folder)
                if not safety["Safe"]:
                    ready = False
                    reason = safety["Reason"]
                    break
        if ready:
            check = test_output_mode_write_access(library, folders)
            if not check["Success"]:
                ready = False
                reason = check["Reason"]
        if ready:
            try:
                reclaim = safe_mode_tree_bytes(library, folders)
                additional = max(0, required_bytes - reclaim)
            except Exception as exc:
                ready = False
                reason = str(exc)
            free = reliable_free_space_bytes(str(output["BasePath"]))
            if free is None:
                warnings.append("Free space could not be determined reliably.")
        results.append({
            "OutputNumber": number,
            "Output": output,
            "LibraryPath": library,
            "Ready": ready,
            "Reason": reason,
            "Warnings": warnings,
            "FreeSpaceBytes": free,
            "RequiredBytes": required_bytes,
            "ReclaimBytes": reclaim,
            "AdditionalRequiredBytes": additional,
        })
    write_sampler_log(
        f"Preflight complete mode={mode} outputs={len(results)} "
        f"elapsed={format_elapsed(time.perf_counter()-started)}",
        "INFO", "PREFLIGHT"
    )
    return results


def mode_folder_has_content(library_path:str,folder:str)->bool:
    path=join_path(library_path,folder)
    if not os.path.isdir(path): return False
    try: return bool(os.listdir(path))
    except Exception: return True


def clear_verified_build_mode_folders(output: dict[str, Any], library_path: str, mode: str) -> None:
    marker = test_output_library_marker(library_path, str(output["LibraryId"]))
    if not marker["Valid"]:
        raise ValueError(f"Refusing destructive operation: {marker['Reason']}")
    for folder in get_build_mode_folders(mode):
        again = test_output_library_marker(library_path, str(output["LibraryId"]))
        if not again["Valid"]:
            raise ValueError(str(again["Reason"]))
        safe_mode_tree_bytes(library_path, [folder])
        safety = test_mode_folder_safety(library_path, folder)
        if not safety["Safe"]:
            raise ValueError(f"Refusing destructive operation: {safety['Reason']}")
        if os.path.isdir(safety["Path"]):
            write_sampler_log(f"Clearing verified mode folder: {safety['Path']}", "WARNING", "BUILD")
            shutil.rmtree(safety["Path"])
    for file_path in (get_manifest_path(library_path, mode), get_txt_path(library_path, mode)):
        if os.path.isfile(file_path):
            again = test_output_library_marker(library_path, str(output["LibraryId"]))
            if not again["Valid"]:
                raise ValueError(str(again["Reason"]))
            if is_link_or_reparse_point(file_path):
                raise ValueError(link_violation_message("managed_artifact"))
            os.remove(file_path)


def _validate_tree_without_links(root: str, preserve_names: set[str]) -> None:
    with os.scandir(root) as iterator:
        for child in iterator:
            if child.name in preserve_names:
                continue
            if child.is_symlink() or is_link_or_reparse_point(child.path):
                raise ValueError(link_violation_message("managed_library", child.path))
            if child.is_dir(follow_symlinks=False):
                _validate_tree_without_links(child.path, set())


def clear_verified_all_library_contents(output: dict[str, Any], library_path: str) -> None:
    marker_check = test_output_library_marker(library_path, str(output["LibraryId"]))
    if not marker_check["Valid"]:
        raise ValueError(f"Refusing destructive ALL replacement: {marker_check['Reason']}")
    marker_name = basename(get_library_marker_path(library_path))
    stage_names = {basename(get_staged_manifest_path(library_path, mode)) for mode in ("SDR", "HDR", "Uncertain")}
    preserve = {marker_name, *stage_names}
    _validate_tree_without_links(library_path, preserve)
    for child in list(os.scandir(library_path)):
        if child.name in preserve:
            continue
        if child.is_symlink() or is_link_or_reparse_point(child.path):
            raise ValueError(link_violation_message("managed_library", child.path))
        if child.is_dir(follow_symlinks=False):
            shutil.rmtree(child.path)
        else:
            os.remove(child.path)
    marker = marker_check["Marker"]
    marker["ProgramVersion"] = APP_VERSION
    write_json_file_atomically(Path(get_library_marker_path(library_path)), marker)


def validate_manifest_matrix(manifests: list[dict[str, Any]], staged: bool) -> bool:
    if not manifests:
        return False
    reference = manifests[0]
    build_id = str(reference["BuildId"])
    selection_id = str(reference["SelectionId"])
    expected_modes = list(reference["BuildModes"])
    expected_libraries = [str(x) for x in reference["BuildLibraryIds"]]
    expected = {(mode, library.casefold()) for mode in expected_modes for library in expected_libraries}
    found: set[tuple[str, str]] = set()
    for manifest in manifests:
        if str(manifest["BuildId"]) != build_id or str(manifest["SelectionId"]) != selection_id:
            return False
        if list(manifest["BuildModes"]) != expected_modes or [str(x) for x in manifest["BuildLibraryIds"]] != expected_libraries:
            return False
        mode = str(manifest["BuildMode"])
        library_id = str(manifest["LibraryId"])
        path = get_staged_manifest_path(str(manifest["LibraryPath"]), mode) if staged else get_manifest_path(str(manifest["LibraryPath"]), mode)
        loaded = read_manifest_file(path, mode, str(manifest["LibraryPath"]))
        if loaded is None or str(loaded.get("BuildId")) != build_id or str(loaded.get("SelectionId")) != selection_id:
            return False
        found.add((mode, library_id.casefold()))
    return found == expected


def remove_staged_manifests(manifests: Iterable[dict[str, Any]]) -> None:
    for manifest in manifests:
        try:
            path = Path(get_staged_manifest_path(str(manifest["LibraryPath"]), str(manifest["BuildMode"])))
            path.unlink(missing_ok=True)
        except Exception:
            pass


def create_build_manifests(
    selection_mode: str,
    snapshots_by_mode: dict[str, dict[str, Any]],
    ready_outputs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    build_modes = [mode for mode in ("SDR", "HDR", "Uncertain") if mode in snapshots_by_mode and snapshots_by_mode[mode].get("SelectedFiles")]
    if not build_modes or not ready_outputs:
        raise ValueError("Build plan is empty.")
    build_id = str(uuid.uuid4())
    build_created_at = utc_now_o()
    build_library_ids = [str(entry["Output"]["LibraryId"]) for entry in ready_outputs]
    manifests: list[dict[str, Any]] = []
    for entry in ready_outputs:
        output = entry["Output"]
        library_path = str(entry["LibraryPath"])
        for mode in build_modes:
            manifests.append(new_output_manifest_from_snapshot(
                snapshots_by_mode[mode], output, library_path, build_id, build_created_at, build_modes, build_library_ids
            ))
    return manifests


def stage_cleanup_commit_build_group(
    selection_mode: str,
    snapshots_by_mode: dict[str, dict[str, Any]],
    ready_outputs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    manifests = create_build_manifests(selection_mode, snapshots_by_mode, ready_outputs)
    written: list[dict[str, Any]] = []
    try:
        for manifest in manifests:
            write_staged_manifest(manifest)
            written.append(manifest)
        if not validate_manifest_matrix(manifests, staged=True):
            raise ValueError("The complete staged build plan could not be verified.")
    except Exception:
        remove_staged_manifests(written)
        raise

    # From this point the confirmed transaction is durable. Cleanup can be
    # repeated safely after a crash because all staged manifests are present.
    for entry in ready_outputs:
        output = entry["Output"]
        library_path = str(entry["LibraryPath"])
        if selection_mode == "ALL":
            clear_verified_all_library_contents(output, library_path)
        else:
            for mode in manifests[0]["BuildModes"]:
                clear_verified_build_mode_folders(output, library_path, mode)

    for manifest in manifests:
        stage_path = get_staged_manifest_path(str(manifest["LibraryPath"]), str(manifest["BuildMode"]))
        official_path = get_manifest_path(str(manifest["LibraryPath"]), str(manifest["BuildMode"]))
        os.replace(stage_path, official_path)
    if not validate_manifest_matrix(manifests, staged=False):
        raise ValueError("The committed build manifest matrix could not be verified.")
    return manifests


def _manifest_files_for_build(config: dict[str, Any], build_id: str, build_modes: list[str], library_ids: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    staged: list[dict[str, Any]] = []
    official: list[dict[str, Any]] = []
    outputs = {str(output["LibraryId"]).casefold(): output for output in config["Outputs"]}
    for library_id in library_ids:
        output = outputs.get(str(library_id).casefold())
        if output is None:
            continue
        library_path = get_output_library_path(config, output)
        if not os.path.isdir(library_path):
            continue
        for mode in build_modes:
            for is_stage, path in ((True, get_staged_manifest_path(library_path, mode)), (False, get_manifest_path(library_path, mode))):
                manifest = read_manifest_file(path, mode, library_path)
                if manifest is None or str(manifest.get("BuildId")) != build_id:
                    continue
                (staged if is_stage else official).append(manifest)
    return staged, official


def recover_staged_transactions(config: dict[str, Any], *, report_errors: bool = False, renderer: Optional[BuildRenderer] = None) -> bool:
    """Recover or safely discard staged transactions.

    Returns True only when it is safe for Build / Resume to continue. A pending
    or invalid staged transaction returns False so no new build can overwrite
    transaction state that has not been recovered.
    """
    seeds: dict[str, dict[str, Any]] = {}
    blocked = False

    for output in config.get("Outputs", []):
        library_path = get_output_library_path(config, output)
        if not os.path.isdir(library_path):
            continue
        for mode in ("SDR", "HDR", "Uncertain"):
            path = Path(get_staged_manifest_path(library_path, mode))
            if not path.is_file():
                continue
            try:
                manifest = read_json_file(path)
                if not isinstance(manifest, dict):
                    raise ValueError("Staged manifest is not a JSON object.")
                if manifest.get("ManifestSchemaVersion") != MANIFEST_SCHEMA_VERSION:
                    raise ValueError(f"Staged manifest schema is not compatible (expected {MANIFEST_SCHEMA_VERSION}).")
                if not test_build_manifest_shape(manifest, mode, library_path):
                    raise ValueError("Staged manifest contract is invalid.")
                build_id = str(manifest["BuildId"])
                seed = seeds.get(build_id)
                if seed is not None:
                    if (
                        str(seed["SelectionId"]) != str(manifest["SelectionId"])
                        or str(seed["SelectionMode"]) != str(manifest["SelectionMode"])
                        or list(seed["BuildModes"]) != list(manifest["BuildModes"])
                        or [str(x) for x in seed["BuildLibraryIds"]] != [str(x) for x in manifest["BuildLibraryIds"]]
                    ):
                        raise ValueError("Staged manifests with the same BuildId do not describe the same build transaction.")
                else:
                    seeds[build_id] = manifest
            except Exception as exc:
                blocked = True
                write_sampler_log(f"Invalid staged transaction file path={path}: {exc}", "ERROR", "RECOVERY", force=True)
                if report_errors and renderer is not None:
                    renderer.staged_recovery_failed()

    for build_id, seed in seeds.items():
        try:
            build_modes = [str(x) for x in seed["BuildModes"]]
            library_ids = [str(x) for x in seed["BuildLibraryIds"]]
            output_by_id = {str(x["LibraryId"]).casefold(): x for x in config["Outputs"]}

            unavailable_expected: list[str] = []
            for library_id in library_ids:
                output = output_by_id.get(library_id.casefold())
                if output is None:
                    unavailable_expected.append(library_id)
                    continue
                library_path = get_output_library_path(config, output)
                if not os.path.isdir(str(output["BasePath"])) or not os.path.isdir(library_path):
                    unavailable_expected.append(library_id)

            if unavailable_expected:
                blocked = True
                write_sampler_log(
                    f"Staged transaction recovery pending build={build_id}; unavailable LibraryIds={','.join(unavailable_expected)}",
                    "WARNING",
                    "RECOVERY",
                )
                if report_errors and renderer is not None:
                    renderer.staged_recovery_pending()
                continue

            staged, official = _manifest_files_for_build(config, build_id, build_modes, library_ids)
            expected_count = len(build_modes) * len(library_ids)
            combined = {(str(x["BuildMode"]), str(x["LibraryId"]).casefold()) for x in [*staged, *official]}

            if len(combined) != expected_count:
                if not official:
                    # All expected outputs are reachable and no official manifest
                    # exists, so cleanup cannot have started. This is an interrupted
                    # staging phase and the partial stage is safe to discard.
                    remove_staged_manifests(staged)
                    write_sampler_log(f"Discarded incomplete staged transaction build={build_id}", "WARNING", "RECOVERY")
                    continue
                raise ValueError("Partial committed transaction is missing required manifest members.")

            if not official:
                # A complete stage exists and cleanup may have completed only
                # partially. Repeating cleanup is intentionally idempotent.
                selection_mode = str(seed["SelectionMode"])
                for library_id in library_ids:
                    output = output_by_id.get(library_id.casefold())
                    if output is None:
                        raise ValueError(f"Configured output for LibraryId {library_id} is unavailable.")
                    library_path = get_output_library_path(config, output)
                    if selection_mode == "ALL":
                        clear_verified_all_library_contents(output, library_path)
                    else:
                        for mode in build_modes:
                            clear_verified_build_mode_folders(output, library_path, mode)

            for manifest in staged:
                stage_path = get_staged_manifest_path(str(manifest["LibraryPath"]), str(manifest["BuildMode"]))
                official_path = get_manifest_path(str(manifest["LibraryPath"]), str(manifest["BuildMode"]))
                os.replace(stage_path, official_path)

            _, official_after = _manifest_files_for_build(config, build_id, build_modes, library_ids)
            official_keys = {(str(x["BuildMode"]), str(x["LibraryId"]).casefold()) for x in official_after}
            expected_keys = {(mode, library_id.casefold()) for mode in build_modes for library_id in library_ids}
            if official_keys != expected_keys:
                raise ValueError("Recovered transaction could not verify the complete official manifest matrix.")

            write_sampler_log(f"Recovered staged build transaction build={build_id}", "INFO", "RECOVERY")
        except Exception as exc:
            blocked = True
            write_sampler_log(f"Staged transaction recovery failed build={build_id}: {exc}", "ERROR", "RECOVERY", force=True)
            if report_errors and renderer is not None:
                renderer.staged_recovery_failed()

    return not blocked



def _source_snapshot(path: str) -> Optional[dict[str, Any]]:
    from .scan import source_snapshot
    return source_snapshot(path)


def source_matches_manifest_item(item:dict[str,Any],snapshot:Optional[dict[str,Any]])->bool:
    return snapshot is not None and int(snapshot["Length"])==int(item["SourceLength"]) and str(snapshot["LastWriteTimeUtc"])==str(item["SourceLastWriteTimeUtc"])


def remove_partial_destination_file(manifest:dict[str,Any],item:dict[str,Any])->None:
    path=get_manifest_item_destination_path(manifest,item); marker=test_output_library_marker(str(manifest["LibraryPath"]),str(manifest["LibraryId"]))
    if not marker["Valid"]: raise ValueError(str(marker["Reason"]))
    if not test_destination_directory_chain_safe(str(manifest["LibraryPath"]),str(item["Pool"]),dirname(path)): raise ValueError("Unsafe partial destination directory chain.")
    if not os.path.exists(path): return
    if not os.path.isfile(path) or is_link_or_reparse_point(path): raise ValueError("Partial destination is not a safe regular file.")
    os.remove(path)


def set_manifest_item_failed(item:dict[str,Any],message:str,exit_code:Optional[int]=None)->None:
    item["CopyStatus"]="Failed"; item["LastCopyMessage"]=message; item["CompletedAtUtc"]=None
    if exit_code is not None: item["LastCopyExitCode"]=int(exit_code)


def copy_single_file(source:str,destination:str)->dict[str,Any]:
    try:
        shutil.copy2(source,destination); return {"ExitCode":0,"Output":"Python shutil.copy2 completed.","Started":True}
    except Exception as exc: return {"ExitCode":16,"Output":str(exc),"Started":True}


_FILE_NAME_LIMIT_BYTES = 255


def partial_copy_path(destination: str) -> str:
    """Temporary path used while copying `destination`.

    Normally `<destination>.part`. When appending the suffix would exceed the common 255-byte
    file-name limit (a valid 251..255 byte name would otherwise be uncopyable), a short
    deterministic name in the same directory is used instead, so stale files are still replaced.
    """
    name = basename(destination)
    if len(os.fsencode(name + ".part")) <= _FILE_NAME_LIMIT_BYTES:
        return destination + ".part"
    digest = hashlib.sha1(os.fsencode(name)).hexdigest()[:16]
    return join_path(dirname(destination), f".mds-{digest}.part")


_VERIFY_SAMPLE_BYTES = 1024 * 1024


def sampled_content_matches(source: str, copy: str, length: int) -> bool:
    """Compare the start, middle and end (1 MiB each) of the source and its copy.

    A full read-back would double the I/O of every Build. This cheap check still catches the failures a
    size comparison misses: truncated-then-padded, zero-filled or stale data at the ends and middle,
    and a copy of the wrong file. It cannot prove that every byte is identical.
    """
    if length <= 0:
        return True
    last = max(0, length - _VERIFY_SAMPLE_BYTES)
    offsets = sorted({0, last // 2, last})
    try:
        with open(source, "rb") as src, open(copy, "rb") as dst:
            for offset in offsets:
                size = min(_VERIFY_SAMPLE_BYTES, length - offset)
                src.seek(offset)
                dst.seek(offset)
                if src.read(size) != dst.read(size):
                    return False
        return True
    except OSError:
        return False


def invoke_manifest_item_copy(manifest: dict[str, Any], item: dict[str, Any], number: int, count: int, renderer: Optional[BuildRenderer] = None) -> bool:
    source = str(item["SourcePath"])
    file_name = basename(source)
    mode = str(manifest["BuildMode"])
    part_path = None

    def fail(message: str, log_message: str, exit_code: Optional[int] = None) -> bool:
        set_manifest_item_failed(item, message, exit_code)
        if renderer is not None:
            renderer.clear_progress()
            renderer.copy_failed(mode, file_name)
        write_sampler_log(log_message, "ERROR", "COPY")
        return False

    try:
        marker = test_output_library_marker(str(manifest["LibraryPath"]), str(manifest["LibraryId"]))
        if not marker["Valid"]:
            raise ValueError(str(marker["Reason"]))
        destination = get_manifest_item_destination_path(manifest, item)
        directory = dirname(destination)
        part_path = partial_copy_path(destination)
        if not test_destination_directory_chain_safe(str(manifest["LibraryPath"]), str(item["Pool"]), directory):
            raise ValueError("Destination directory chain is not safe.")
        os.makedirs(directory, exist_ok=True)
        if str(item["CopyStatus"]).lower() == "completed" and destination_file_valid(destination, int(item["SourceLength"])):
            write_sampler_log(f"Resume skip completed destination={destination}", "INFO", "COPY")
            return True
        before = _source_snapshot(source)
        if not source_matches_manifest_item(item, before):
            return fail("Source metadata no longer matches the build snapshot.", f"Copy failed path={source} reason=source changed or unavailable")
        if os.path.exists(destination):
            remove_partial_destination_file(manifest, item)
        if os.path.exists(part_path):
            if not os.path.isfile(part_path) or is_link_or_reparse_point(part_path):
                raise ValueError("Unsafe .part destination exists.")
            os.remove(part_path)
        item["CopyStatus"] = "Copying"
        item["CopyAttempts"] = int(item["CopyAttempts"]) + 1
        item["LastCopyMessage"] = None
        item["CompletedAtUtc"] = None
        write_build_manifest(manifest)
        started = time.perf_counter()
        result = copy_single_file(source, part_path)
        item["LastCopyExitCode"] = int(result["ExitCode"])
        write_sampler_log(f"copy path={source} destination={part_path} exit={result['ExitCode']} elapsed={format_elapsed(time.perf_counter()-started)} output={result['Output']}", "DEBUG", "COPY")
        after = _source_snapshot(source)
        stable = before is not None and after == before
        part_valid = destination_file_valid(part_path, int(item["SourceLength"]))
        if not stable:
            return fail("Source metadata changed during copy.", f"Copy failed path={source} reason=source changed during copy", int(result["ExitCode"]))
        if not part_valid:
            return fail("Temporary destination file is missing or has the wrong size after copy.", f"Copy failed path={source} reason=temporary destination size validation failed", int(result["ExitCode"]))
        if not sampled_content_matches(source, part_path, int(item["SourceLength"])):
            return fail("Copied file content differs from the source (sampled comparison).", f"Copy failed path={source} reason=sampled content comparison failed", int(result["ExitCode"]))
        os.replace(part_path, destination)
        if not destination_file_valid(destination, int(item["SourceLength"])):
            return fail("Final destination validation failed after atomic rename.", f"Copy failed path={source} reason=final destination validation failed", int(result["ExitCode"]))
        try:
            dst_stamp = stat_mtime_o(os.stat(destination, follow_symlinks=False))
            if dst_stamp != str(item["SourceLastWriteTimeUtc"]):
                if renderer is not None:
                    renderer.clear_progress()
                    renderer.timestamp_mismatch(mode, file_name)
                write_sampler_log(f"Timestamp mismatch destination={destination} expected={item['SourceLastWriteTimeUtc']} found={dst_stamp}", "WARNING", "COPY")
        except Exception as exc:
            write_sampler_log(f"Destination timestamp read failed destination={destination} error={exc}", "WARNING", "COPY")
        item["CopyStatus"] = "Completed"
        item["LastCopyMessage"] = "Copy and atomic validation completed."
        item["CompletedAtUtc"] = utc_now_o()
        return True
    except Exception as exc:
        result = fail(str(exc), f"Copy exception path={source}\n{traceback.format_exc()}")
        return result
    finally:
        if part_path and os.path.exists(part_path):
            try:
                if os.path.isfile(part_path) and not is_link_or_reparse_point(part_path):
                    os.remove(part_path)
            except Exception as exc:
                write_sampler_log(f"Could not remove partial copy file path={part_path} error={exc}", "WARNING", "COPY")
        try:
            write_build_manifest(manifest)
        except Exception:
            if renderer is not None:
                renderer.clear_progress()
                renderer.manifest_update_failed(mode)
            write_sampler_log(traceback.format_exc(), "ERROR", "MANIFEST")


def invoke_manifest_validation(manifest: dict[str, Any], renderer: Optional[BuildRenderer] = None) -> dict[str, Any]:
    started = time.perf_counter()
    mode = str(manifest["BuildMode"])
    items = manifest["SelectedFiles"]
    failed = 0
    write_sampler_log(f"Validation started mode={mode} library={manifest['LibraryPath']} total={len(items)}", "INFO", "VALIDATE")
    for index, item in enumerate(items, 1):
        if renderer is not None:
            renderer.validation_progress(mode, index, len(items))
        file_name = basename(str(item["SourcePath"]))
        valid = False
        reason = None
        copy_already_failed = str(item["CopyStatus"]).casefold() != "completed"
        try:
            marker = test_output_library_marker(str(manifest["LibraryPath"]), str(manifest["LibraryId"]))
            if not marker["Valid"]:
                raise ValueError(str(marker["Reason"]))
            destination = get_manifest_item_destination_path(manifest, item)
            if not test_destination_directory_chain_safe(str(manifest["LibraryPath"]), str(item["Pool"]), dirname(destination)):
                raise ValueError("Unsafe validation destination chain.")
            if copy_already_failed:
                reason = f"copy status is {item['CopyStatus']}"
            elif not os.path.isfile(destination):
                reason = "destination file is missing"
            elif is_link_or_reparse_point(destination):
                reason = link_violation_message("destination_file")
            elif os.stat(destination, follow_symlinks=False).st_size != int(item["SourceLength"]):
                reason = f"size mismatch; expected {item['SourceLength']} bytes, found {os.stat(destination, follow_symlinks=False).st_size} bytes"
            else:
                valid = True
        except Exception:
            reason = "destination could not be verified"
            write_sampler_log(f"Validation exception path={item['SourcePath']}\n{traceback.format_exc()}", "ERROR", "VALIDATE")
        if not valid:
            failed += 1
            reason = reason or "validation failed"
            if not copy_already_failed:
                if renderer is not None:
                    renderer.clear_progress()
                    renderer.validation_failed(mode, file_name)
            write_sampler_log(f"Validation failed mode={mode} path={item['SourcePath']} reason={reason}", "ERROR", "VALIDATE")
            if str(item["CopyStatus"]).casefold() == "completed":
                item["CopyStatus"] = "Failed"
                item["LastCopyMessage"] = f"Post-copy validation failed: {reason}."
                item["CompletedAtUtc"] = None
    if renderer is not None:
        renderer.clear_progress()
    manifest["BuildStatus"] = "Complete" if failed == 0 else "Incomplete"
    write_build_manifest(manifest)
    txt_written = False
    if failed == 0:
        try:
            write_manifest_txt(manifest)
            txt_written = True
        except Exception as exc:
            if renderer is not None:
                renderer.txt_write_failed(mode)
            write_sampler_log(f"Final TXT write failed library={manifest['LibraryPath']} mode={manifest['BuildMode']}: {exc}\n{traceback.format_exc()}", "ERROR", "OUTPUT", force=True)
    write_sampler_log(f"Validation complete mode={mode} total={len(items)} failed={failed} elapsed={format_elapsed(time.perf_counter()-started)}", "INFO", "VALIDATE")
    return {"Complete": failed == 0, "FailedCount": failed, "TotalCount": len(items), "TxtWritten": txt_written}


def _manifest_copy_summary(manifest: dict[str, Any], validation_complete: bool = False, stopped: bool = False) -> dict[str, Any]:
    statuses = [str(item.get("CopyStatus", "")).casefold() for item in manifest["SelectedFiles"]]
    completed = sum(1 for status in statuses if status == "completed")
    failed = sum(1 for status in statuses if status == "failed")
    total = len(statuses)
    pending = max(0, total - completed - failed)
    library_ids = [str(x) for x in manifest.get("BuildLibraryIds", [])]
    try:
        output_number = next(i for i, value in enumerate(library_ids, 1) if value.casefold() == str(manifest["LibraryId"]).casefold())
    except StopIteration:
        output_number = 1
    return {
        "LibraryPath": str(manifest["LibraryPath"]),
        "LibraryId": str(manifest["LibraryId"]),
        "OutputNumber": output_number,
        "OutputCount": max(1, len(library_ids)),
        "BuildMode": str(manifest["BuildMode"]),
        "BuildStatus": str(manifest["BuildStatus"]),
        "SelectedCount": total,
        "CompletedCount": completed,
        "FailedCount": failed,
        "PendingCount": pending,
        "ValidationComplete": validation_complete,
        "Stopped": stopped,
    }


def invoke_manifest_copy(manifest: dict[str, Any], output_number: int, output_count: int, renderer: Optional[BuildRenderer] = None) -> dict[str, Any]:
    started = time.perf_counter()
    mode = str(manifest["BuildMode"])
    items = manifest["SelectedFiles"]
    write_sampler_log(f"Copy started mode={mode} library={manifest['LibraryPath']} total={len(items)}", "INFO", "COPY")
    txt_path = Path(get_txt_path(str(manifest["LibraryPath"]), mode))
    if manifest.get("BuildStatus") != "Complete" and txt_path.is_file():
        try:
            txt_path.unlink()
        except Exception as exc:
            write_sampler_log(f"Could not remove stale TXT before resume path={txt_path}: {exc}", "WARNING", "OUTPUT")
    for index, item in enumerate(items, 1):
        if graceful_stop_requested():
            if renderer is not None:
                renderer.clear_progress()
            manifest["BuildStatus"] = "Incomplete"
            write_build_manifest(manifest)
            write_sampler_log("Copy stopped gracefully; build remains resumable.", "INFO", "COPY")
            return _manifest_copy_summary(manifest, stopped=True)
        file_name = basename(str(item["SourcePath"]))
        if renderer is not None:
            renderer.copy_progress(mode, index, len(items), file_name)
        invoke_manifest_item_copy(manifest, item, index, len(items), renderer)
        if graceful_stop_requested():
            if renderer is not None:
                renderer.clear_progress()
            manifest["BuildStatus"] = "Incomplete"
            write_build_manifest(manifest)
            write_sampler_log("Copy stopped gracefully; build remains resumable.", "INFO", "COPY")
            return _manifest_copy_summary(manifest, stopped=True)
    validation = invoke_manifest_validation(manifest, renderer)
    write_sampler_log(
        f"Copy workflow complete mode={mode} library={manifest['LibraryPath']} total={len(items)} completed={sum(1 for x in items if x['CopyStatus']=='Completed')} elapsed={format_elapsed(time.perf_counter()-started)}",
        "INFO", "COPY"
    )
    return _manifest_copy_summary(manifest, validation_complete=bool(validation["Complete"]))


# Higher-level build preparation/state helpers. User interaction remains in ui.py.
def create_selection_preparation(
    config: dict[str, Any],
    selection_mode: str,
    pool_selections: list[dict[str, Any]],
    scan_result: dict[str, Any],
) -> Optional[dict[str, Any]]:
    if sum(len(entry["Selection"]["Selected"]) for entry in pool_selections) == 0:
        return None
    selection_id = str(uuid.uuid4())
    selection_created_at = utc_now_o()
    by_pool = {str(entry["Pool"]): entry for entry in pool_selections}
    snapshots_by_mode: dict[str, dict[str, Any]] = {}
    if selection_mode in {"SDR", "HDR"}:
        entry = by_pool[selection_mode]
        if entry["Selection"]["Selected"]:
            snapshots_by_mode[selection_mode] = new_build_snapshot(
                config, selection_mode, selection_mode, [entry], selection_id, selection_created_at
            )
    elif selection_mode == "Uncertain":
        uncertain_entries = [by_pool[pool] for pool in ("SDR Uncertain", "HDR Uncertain")]
        if any(entry["Selection"]["Selected"] for entry in uncertain_entries):
            snapshots_by_mode["Uncertain"] = new_build_snapshot(
                config, selection_mode, "Uncertain", uncertain_entries, selection_id, selection_created_at
            )
    elif selection_mode == "ALL":
        if by_pool["SDR"]["Selection"]["Selected"]:
            snapshots_by_mode["SDR"] = new_build_snapshot(config, "ALL", "SDR", [by_pool["SDR"]], selection_id, selection_created_at)
        if by_pool["HDR"]["Selection"]["Selected"]:
            snapshots_by_mode["HDR"] = new_build_snapshot(config, "ALL", "HDR", [by_pool["HDR"]], selection_id, selection_created_at)
        uncertain_entries = [by_pool["SDR Uncertain"], by_pool["HDR Uncertain"]]
        if any(entry["Selection"]["Selected"] for entry in uncertain_entries):
            snapshots_by_mode["Uncertain"] = new_build_snapshot(config, "ALL", "Uncertain", uncertain_entries, selection_id, selection_created_at)
    if not snapshots_by_mode:
        return None
    return {
        "SelectionMode": selection_mode,
        "SelectionId": selection_id,
        "SelectionCreatedAtUtc": selection_created_at,
        "PoolSelections": pool_selections,
        "SnapshotsByMode": snapshots_by_mode,
        "ScanResult": scan_result,
    }


def preparation_total_bytes(preparation: dict[str, Any]) -> int:
    return sum(snapshot_total_bytes(snapshot) for snapshot in preparation["SnapshotsByMode"].values())


def validate_selection_snapshot_sources(preparation: dict[str, Any]) -> tuple[bool, Optional[str]]:
    for snapshot in preparation["SnapshotsByMode"].values():
        for item in snapshot["SelectedFiles"]:
            current = _source_snapshot(str(item["SourcePath"]))
            if not source_matches_manifest_item(item, current):
                write_sampler_log(f"Selection snapshot invalidated source={item['SourcePath']}", "WARNING", "SELECTION")
                return False, str(item["SourcePath"])
    return True, None


def raw_manifest_schema(path: str) -> Optional[int]:
    try:
        if not Path(path).is_file():
            return None
        data = read_json_file(Path(path))
        return data.get("ManifestSchemaVersion") if isinstance(data, dict) else None
    except Exception:
        return None


def build_mode_replace_required(library_path: str, mode: str) -> bool:
    if any(mode_folder_has_content(library_path, folder) for folder in get_build_mode_folders(mode)):
        return True
    return os.path.isfile(get_manifest_path(library_path, mode)) or os.path.isfile(get_txt_path(library_path, mode))


def all_library_replace_required(library_path: str) -> bool:
    if not os.path.isdir(library_path):
        return False
    marker_name = basename(get_library_marker_path(library_path)).casefold()
    stage_names = {get_manifest_file_name(mode).replace(".json", ".stage.json").casefold() for mode in ("SDR", "HDR", "Uncertain")}
    try:
        for name in os.listdir(library_path):
            folded = name.casefold()
            if folded == marker_name or folded in stage_names:
                continue
            return True
    except OSError:
        return True
    return False


def preparation_requires_replace(preparation: dict[str, Any], ready_outputs: list[dict[str, Any]]) -> bool:
    selection_mode = str(preparation["SelectionMode"])
    for entry in ready_outputs:
        library_path = str(entry["LibraryPath"])
        if selection_mode == "ALL":
            if all_library_replace_required(library_path):
                return True
        else:
            for mode in preparation["SnapshotsByMode"]:
                if build_mode_replace_required(library_path, mode):
                    return True
    return False


def combined_preflight(config: dict[str, Any], preparation: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    snapshots = preparation["SnapshotsByMode"]
    by_mode: dict[str, list[dict[str, Any]]] = {}
    for mode, snapshot in snapshots.items():
        by_mode[mode] = invoke_output_preflight(config, mode, snapshot_total_bytes(snapshot))

    selection_size = preparation_total_bytes(preparation)
    selected_files = sum(len(row["Selection"]["Selected"]) for row in preparation["PoolSelections"])
    by_library: dict[str, dict[str, Any]] = {}
    for output in config["Outputs"]:
        library_id = str(output["LibraryId"]).casefold()
        library_path = get_output_library_path(config, output)
        mode_entries: list[dict[str, Any]] = []
        reasons: list[str] = []
        for mode in snapshots:
            entry = next((x for x in by_mode[mode] if str(x["Output"]["LibraryId"]).casefold() == library_id), None)
            if entry is None:
                reasons.append(f"{mode}: preflight result is missing.")
            else:
                mode_entries.append(entry)
                if not entry["Ready"]:
                    reasons.append(f"{mode}: {entry['Reason'] or 'Not available'}")
        if not reasons and str(preparation["SelectionMode"]) == "ALL":
            marker = test_output_library_marker(library_path, str(output["LibraryId"]))
            if not marker["Valid"]:
                reasons.append(str(marker["Reason"]))
            else:
                try:
                    marker_name = basename(get_library_marker_path(library_path))
                    stage_names = {basename(get_staged_manifest_path(library_path, mode)) for mode in ("SDR", "HDR", "Uncertain")}
                    _validate_tree_without_links(library_path, {marker_name, *stage_names})
                except Exception as exc:
                    reasons.append(str(exc))
        free_values = [entry.get("FreeSpaceBytes") for entry in mode_entries if entry.get("FreeSpaceBytes") is not None]
        free = int(free_values[0]) if free_values else None
        reclaim = sum(int(entry.get("ReclaimBytes", 0)) for entry in mode_entries)
        additional = sum(int(entry.get("AdditionalRequiredBytes", 0)) for entry in mode_entries)
        by_library[library_id] = {
            "Output": output, "LibraryPath": library_path, "ModeEntries": mode_entries,
            "Ready": not reasons and len(mode_entries) == len(snapshots),
            "Reason": "; ".join(reasons) if reasons else None,
            "SelectionSizeBytes": selection_size, "SelectedFiles": selected_files,
            "FreeSpaceBytes": free, "ReclaimBytes": reclaim, "AdditionalRequiredBytes": additional,
            "ShortfallBytes": None, "StorageRequiredBytes": additional,
        }
    storage_groups: dict[str, dict[str, Any]] = {}
    for state in by_library.values():
        if not state["Ready"]:
            continue
        output = state["Output"]
        key = str(output.get("StorageIdentityKey") or normalize_path(str(output["BasePath"]))).casefold()
        group = storage_groups.setdefault(key, {"Path": str(output["BasePath"]), "Required": 0, "States": []})
        group["Required"] += int(state["AdditionalRequiredBytes"])
        group["States"].append(state)
    for group in storage_groups.values():
        free = reliable_free_space_bytes(group["Path"])
        for state in group["States"]:
            state["FreeSpaceBytes"] = free
            state["StorageRequiredBytes"] = int(group["Required"])
        if free is not None and free < int(group["Required"]):
            shortfall = int(group["Required"]) - free
            for state in group["States"]:
                state["Ready"] = False
                state["Reason"] = "Not enough free space for the complete build workload on this storage."
                state["ShortfallBytes"] = shortfall
    return [x for x in by_library.values() if x["Ready"]], [x for x in by_library.values() if not x["Ready"]]


def preflight_pool_rows(preparation: dict[str, Any]) -> list[tuple[str, int, int]]:
    rows: list[tuple[str, int, int]] = []
    for pool_selection in preparation["PoolSelections"]:
        pool = str(pool_selection["Pool"])
        selected = pool_selection["Selection"]["Selected"]
        size = sum(int(item["Record"]["Media"]["FileSizeBytes"]) for item in selected)
        rows.append((pool, len(selected), size))
    return rows


def copy_manifest_group(manifests: list[dict[str, Any]], renderer: Optional[BuildRenderer] = None) -> list[dict[str, Any]]:
    if not manifests:
        return []
    mode_order = {"SDR": 0, "HDR": 1, "Uncertain": 2}
    library_order = {library_id.casefold(): index + 1 for index, library_id in enumerate(manifests[0]["BuildLibraryIds"])}
    ordered = sorted(manifests, key=lambda m: (library_order.get(str(m["LibraryId"]).casefold(), 999), mode_order.get(str(m["BuildMode"]), 99)))
    summaries: list[dict[str, Any]] = []
    processed_manifest_ids: set[int] = set()
    if renderer is not None:
        renderer.build_progress_started()
    active_output: Optional[int] = None
    from .interrupts import scoped_graceful_stop
    with scoped_graceful_stop("COPY"):
        for manifest in ordered:
            if graceful_stop_requested():
                break
            output_number = library_order.get(str(manifest["LibraryId"]).casefold(), 1)
            if output_number != active_output:
                if renderer is not None:
                    renderer.build_output_started(output_number, len(library_order), str(manifest["LibraryPath"]), active_output is not None)
                active_output = output_number
            try:
                summary = invoke_manifest_copy(manifest, output_number, len(library_order), renderer)
            except Exception as exc:
                if renderer is not None:
                    renderer.clear_progress()
                    renderer.unexpected_copy_failure(str(manifest["BuildMode"]))
                write_sampler_log(f"Manifest copy exception library={manifest['LibraryPath']} mode={manifest['BuildMode']}: {exc}\n{traceback.format_exc()}", "ERROR", "COPY", force=True)
                manifest["BuildStatus"] = "Incomplete"
                try:
                    write_build_manifest(manifest)
                except Exception:
                    pass
                summary = _manifest_copy_summary(manifest)
            if renderer is not None:
                renderer.clear_progress()
            summaries.append(summary)
            processed_manifest_ids.add(id(manifest))
            if summary.get("Stopped"):
                break
            if renderer is not None:
                renderer.build_manifest_summary(summary)
    for manifest in ordered:
        if id(manifest) not in processed_manifest_ids:
            summaries.append(_manifest_copy_summary(manifest))
    return summaries


def stage_and_copy_new_build(
    preparation: dict[str, Any], ready: list[dict[str, Any]], renderer: Optional[BuildRenderer] = None
) -> dict[str, Any]:
    try:
        manifests = stage_cleanup_commit_build_group(str(preparation["SelectionMode"]), preparation["SnapshotsByMode"], ready)
    except Exception as exc:
        write_sampler_log(f"Build transaction failed before copy: {exc}\n{traceback.format_exc()}", "ERROR", "BUILD", force=True)
        return {"Success": False, "Error": str(exc), "Manifests": [], "Summaries": []}
    summaries = copy_manifest_group(manifests, renderer)
    return {"Success": True, "Error": None, "Manifests": manifests, "Summaries": summaries}


def configured_output_by_library_id(config: dict[str, Any], library_id: str) -> Optional[dict[str, Any]]:
    return next((output for output in config["Outputs"] if str(output["LibraryId"]).casefold() == str(library_id).casefold()), None)


def find_incomplete_build_groups(config: dict[str, Any], selection_mode: Optional[str] = None) -> tuple[list[dict[str, Any]], set[str]]:
    groups: dict[str, dict[str, Any]] = {}
    schema2_modes: set[str] = set()
    for output in config["Outputs"]:
        library = get_output_library_path(config, output)
        if not os.path.isdir(library):
            continue
        marker = test_output_library_marker(library, str(output["LibraryId"]))
        if not marker["Valid"]:
            continue
        for mode in ("SDR", "HDR", "Uncertain"):
            path = get_manifest_path(library, mode)
            if raw_manifest_schema(path) == 2:
                schema2_modes.add(mode)
                continue
            manifest = read_manifest_file(path, mode, library)
            if manifest is None:
                continue
            manifest_selection_mode = str(manifest.get("SelectionMode"))
            if selection_mode is not None and manifest_selection_mode != selection_mode:
                continue
            build_id = str(manifest["BuildId"])
            group = groups.setdefault(build_id, {
                "BuildId": build_id, "SelectionId": str(manifest["SelectionId"]),
                "SelectionMode": manifest_selection_mode, "BuildModes": list(manifest["BuildModes"]),
                "BuildLibraryIds": [str(x) for x in manifest["BuildLibraryIds"]],
                "BuildCreatedAtUtc": str(manifest["BuildCreatedAtUtc"]), "Manifests": [],
            })
            group["Manifests"].append(manifest)
    relevant_schema2 = schema2_modes if selection_mode in {None, "ALL"} else schema2_modes.intersection({selection_mode})
    incomplete: list[dict[str, Any]] = []
    for group in groups.values():
        expected = {(mode, library_id.casefold()) for mode in group["BuildModes"] for library_id in group["BuildLibraryIds"]}
        found = {(str(m["BuildMode"]), str(m["LibraryId"]).casefold()) for m in group["Manifests"]}
        any_incomplete = any(str(m.get("BuildStatus")) != "Complete" for m in group["Manifests"])
        if any_incomplete or found != expected:
            incomplete.append(group)
    return incomplete, relevant_schema2


def resume_group_states(config: dict[str, Any], group: dict[str, Any]) -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []
    for library_id in group["BuildLibraryIds"]:
        output = configured_output_by_library_id(config, library_id)
        if output is None:
            states.append({"LibraryId": library_id, "Output": None, "LibraryPath": "", "Status": "Unavailable", "Reason": "The output is no longer configured.", "Manifests": []})
            continue
        library = get_output_library_path(config, output)
        if not os.path.isdir(str(output["BasePath"])) or not os.path.isdir(library):
            states.append({"LibraryId": library_id, "Output": output, "LibraryPath": library, "Status": "Unavailable", "Reason": "Output or Library is currently not available.", "Manifests": []})
            continue
        marker = test_output_library_marker(library, library_id)
        if not marker["Valid"]:
            states.append({"LibraryId": library_id, "Output": output, "LibraryPath": library, "Status": "Unavailable", "Reason": str(marker["Reason"]), "Manifests": []})
            continue
        manifests: list[dict[str, Any]] = []
        missing = False
        for mode in group["BuildModes"]:
            manifest = read_manifest_file(get_manifest_path(library, mode), mode, library)
            if manifest is None or str(manifest.get("BuildId")) != str(group["BuildId"]):
                missing = True
                break
            manifests.append(manifest)
        if missing:
            states.append({"LibraryId": library_id, "Output": output, "LibraryPath": library, "Status": "Unavailable", "Reason": "The complete build manifest set is not available.", "Manifests": manifests})
            continue
        required = 0
        for manifest in manifests:
            for item in manifest["SelectedFiles"]:
                destination = get_manifest_item_destination_path(manifest, item)
                if not destination_file_valid(destination, int(item["SourceLength"])):
                    required += int(item["SourceLength"])
        free = reliable_free_space_bytes(str(output["BasePath"]))
        if free is not None and free < required:
            states.append({"LibraryId": library_id, "Output": output, "LibraryPath": library, "Status": "Unavailable", "Reason": "Not enough free space to resume the remaining files.", "Manifests": manifests, "RemainingBytes": required})
            continue
        states.append({"LibraryId": library_id, "Output": output, "LibraryPath": library, "Status": "Ready", "Reason": None, "Manifests": manifests, "RemainingBytes": required})
    return states
