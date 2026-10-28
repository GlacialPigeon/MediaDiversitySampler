# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import concurrent.futures as cf
import os
import sqlite3
import time
from collections import OrderedDict
from typing import Any, Optional, Protocol

from .constants import APP_ROOT, PROBE_FAILED_LOG_PATH
from .datetime_helpers import stat_mtime_o, utc_now_o
from .ffprobe import expected_ffprobe_path, probe_media_json, test_ffprobe_health, test_sample_or_trailer_path
from .file_io import write_text_file_atomically
from .interrupts import graceful_stop_requested, scoped_graceful_stop
from .logger import format_elapsed, write_sampler_log
from .paths import configured_output_library_paths, recognized_sampler_library_directory
from .platform_api import (
    basename, canonical_existing_path, default_excluded_directories, dirname, ensure_trailing_separator,
    is_link_or_reparse_point, join_path, link_violation_message, no_link_ancestors, normpath, normalize_path,
    path_key, paths_equal, relative_key, splitext, storage_relative_path, is_path_ancestor,
)
from .probe_cache import (
    ProbeCacheDB, ProbeCacheRecoveryError, ProbeInfrastructureError,
    is_confirmed_sqlite_corruption, load_probe_cache,
)

VIDEO_EXTENSIONS = {
    ".3g2", ".3gp", ".asf", ".avi", ".divx", ".f4v", ".flv", ".m2t",
    ".m2ts", ".m2v", ".m4v", ".mkv", ".mov", ".mp4", ".mpe", ".mpeg",
    ".mpg", ".mpv", ".mts", ".mxf", ".ogm", ".ogv", ".rm", ".rmvb",
    ".ts", ".vob", ".vro", ".webm", ".wmv", ".wtv",
}

CURRENT_SCAN: Optional[dict[str, Any]] = None


class ScanRenderer(Protocol):
    def status(self, key: str, type_: str = "INFO", component: str = "APP", *, log_message: str, **kwargs: Any) -> None: ...
    def show_no_sources(self) -> None: ...
    def show_ffprobe_error(self, kind: str, expected_path: str) -> None: ...
    def scan_started(self) -> None: ...
    def show_inventory(self, stats: dict[str, int]) -> None: ...
    def probe_progress(self, completed: int, total: int, failures: int, elapsed: float) -> None: ...
    def clear_progress(self) -> None: ...
    def show_probe_summary(self, *, inventory_count: int, cache_hits: int, probed: int, failed: int, elapsed: float, progress_shown: bool) -> None: ...
    def show_cache_recovery(self, info: dict[str, Any]) -> None: ...
    def show_cache_error(self, key: str, exc: BaseException) -> None: ...
    def show_probe_start_failed(self, exc: BaseException) -> None: ...
    def show_scan_restart_inventory(self, stats: dict[str, int]) -> None: ...


def reset_scan_state() -> None:
    global CURRENT_SCAN
    CURRENT_SCAN = None


def get_current_scan() -> Optional[dict[str, Any]]:
    return CURRENT_SCAN


def path_is_utf8_encodable(path: str) -> bool:
    """False for paths holding undecodable bytes (lone surrogates from os.fsdecode)."""
    try:
        path.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def printable_path(path: str) -> str:
    """Escape undecodable bytes so the path can be logged and shown safely."""
    return path.encode("utf-8", "backslashreplace").decode("utf-8")


def enumeration_directory_excluded(directory: str, config: dict[str, Any]) -> bool:
    try:
        if is_path_ancestor(directory, str(APP_ROOT), True):
            return True
    except Exception:
        pass
    for library in configured_output_library_paths(config):
        try:
            if is_path_ancestor(directory, library, True):
                return True
        except Exception:
            pass
    return recognized_sampler_library_directory(directory)


def get_media_files_for_root(root: dict[str, Any], config: dict[str, Any], stats: dict[str, int], renderer: ScanRenderer) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    root_path = str(root["Path"])
    if graceful_stop_requested():
        return result
    if not no_link_ancestors(root_path):
        renderer.status("source.path_link", "WARNING", component="SCAN", log_message=link_violation_message("source_traverses", root_path), path=root_path)
        return result
    if enumeration_directory_excluded(root_path, config):
        return result
    if not os.path.isdir(root_path):
        renderer.status("source.unavailable", "WARNING", component="SOURCE", log_message=f"Media source is unavailable: {root_path}", path=root_path)
        write_sampler_log(f"Source root unavailable path={root_path}", "WARNING", "SOURCE")
        return result
    try:
        canonical_root = canonical_existing_path(root_path)
    except Exception as exc:
        renderer.status("source.open_failed", "WARNING", component="SOURCE", log_message=f"Media source could not be opened: {root_path}", path=root_path)
        write_sampler_log(f"Source root open failed path={root_path} error={exc}", "WARNING", "SOURCE")
        return result
    if enumeration_directory_excluded(canonical_root, config):
        return result
    storage_key = str(root.get("StorageIdentityKey") or ("PATH:" + path_key(canonical_root)))
    source_rel = str(root.get("SourceRootRelativePath") or "")
    stack = [canonical_root]
    excluded_names = default_excluded_directories()
    while stack and not graceful_stop_requested():
        directory = stack.pop()
        if not paths_equal(directory, canonical_root) and enumeration_directory_excluded(directory, config):
            write_sampler_log(f"Enumeration skipped internal/output directory: {directory}", "DEBUG", "SCAN")
            continue
        try:
            iterator = os.scandir(directory)
        except OSError as exc:
            renderer.status("source.folder_read_failed", "WARNING", component="SCAN", log_message=f"Cannot read folder; continuing: {directory}", path=directory)
            write_sampler_log(f"Enumeration failed directory={directory} error={exc}", "WARNING", "SCAN")
            continue
        with iterator:
            for child in iterator:
                if graceful_stop_requested():
                    break
                try:
                    if child.is_symlink() or is_link_or_reparse_point(child.path):
                        write_sampler_log(link_violation_message("skipped_link", child.path), "DEBUG", "SCAN")
                        continue
                    if child.is_dir(follow_symlinks=False):
                        if child.name.casefold() in excluded_names:
                            write_sampler_log(f"Enumeration skipped Windows system directory: {child.path}", "DEBUG", "SCAN")
                            continue
                        stack.append(child.path)
                        continue
                    stats["FilesFound"] += 1
                    if splitext(child.name)[1].lower() not in VIDEO_EXTENSIONS:
                        stats["Excluded"] += 1
                        continue
                    if not path_is_utf8_encodable(child.path):
                        # Undecodable filename bytes (surrogateescape) cannot be stored in the
                        # SQLite probe cache or JSON manifests; skip the file instead of aborting the scan.
                        stats["Excluded"] += 1
                        safe_path = printable_path(child.path)
                        renderer.status("source.file_inspect_failed", "WARNING", component="SCAN", log_message=f"Skipped file with a name that is not valid UTF-8: {safe_path}", path=safe_path)
                        write_sampler_log(f"Skipped file with non-UTF-8 name path={safe_path}", "WARNING", "SCAN")
                        continue
                    if test_sample_or_trailer_path(os.path.relpath(child.path, canonical_root)):
                        stats["Excluded"] += 1
                        write_sampler_log(f"Excluded sample/trailer candidate: {child.path}", "DEBUG", "SCAN")
                        continue
                    st = child.stat(follow_symlinks=False)
                    canonical = normalize_path(child.path)
                    rel = storage_relative_path(canonical, canonical_root)
                    storage_rel = normpath(join_path(source_rel, rel)) if source_rel and rel else (normpath(source_rel or rel) if (source_rel or rel) else "")
                    result.append({
                        "Path": canonical, "RootId": str(root["RootId"]), "RootLabel": str(root["Label"]),
                        "RootPath": canonical_root, "RelativePath": rel, "StorageIdentityKey": storage_key,
                        "StorageRelativePath": storage_rel, "Length": int(st.st_size), "LastWriteTimeUtc": stat_mtime_o(st),
                    })
                except Exception as exc:
                    stats["Excluded"] += 1
                    renderer.status("source.file_inspect_failed", "WARNING", component="SCAN", log_message=f"Cannot inspect file; continuing: {child.path}", path=child.path)
                    write_sampler_log(f"File metadata failed path={child.path} error={exc}", "WARNING", "SCAN")
    return result


def get_current_media_inventory(config: dict[str, Any], renderer: ScanRenderer) -> tuple[list[dict[str, Any]], dict[str, int]]:
    started = time.perf_counter()
    write_sampler_log(f"Inventory started sources={len(config['SourceRoots'])}", "INFO", "SCAN")
    write_sampler_log("Phase: Scanning files...", "INFO", "APP")
    renderer.scan_started()
    all_files: list[dict[str, Any]] = []
    stats = {"FilesFound": 0, "Excluded": 0, "VideoCandidates": 0}
    seen: set[tuple[str, str]] = set()
    for root in config["SourceRoots"]:
        if graceful_stop_requested():
            break
        for file in get_media_files_for_root(root, config, stats, renderer):
            if graceful_stop_requested():
                break
            key = (str(file["StorageIdentityKey"]).casefold(), relative_key(str(file["StorageRelativePath"])))
            if key not in seen:
                seen.add(key)
                all_files.append(file)
            else:
                stats["Excluded"] += 1
                write_sampler_log(f"Storage duplicate skipped path={file['Path']}", "DEBUG", "SCAN")
    stats["VideoCandidates"] = len(all_files)
    stats["Excluded"] = max(0, stats["FilesFound"] - stats["VideoCandidates"])
    elapsed = time.perf_counter() - started
    if graceful_stop_requested():
        write_sampler_log(f"Inventory stopped files={len(all_files)} elapsed={format_elapsed(elapsed)}", "WARNING", "SCAN")
    else:
        renderer.show_inventory(stats)
        write_sampler_log(
            f"Inventory complete files_found={stats['FilesFound']} excluded={stats['Excluded']} video_candidates={stats['VideoCandidates']} elapsed={format_elapsed(elapsed)}",
            "INFO", "SCAN",
        )
    return all_files, stats


def source_snapshot(path: str) -> Optional[dict[str, Any]]:
    try:
        if not no_link_ancestors(path) or not os.path.isfile(path) or is_link_or_reparse_point(path):
            return None
        st = os.stat(path, follow_symlinks=False)
        return {"Length": int(st.st_size), "LastWriteTimeUtc": stat_mtime_o(st)}
    except OSError:
        return None


def probe_worker(ffprobe_path: str, file: dict[str, Any], timeout_seconds: int, retries: int = 1) -> dict[str, Any]:
    def run_once(candidate: dict[str, Any]) -> dict[str, Any]:
        path = str(candidate["Path"])
        before = source_snapshot(path)
        if before is None:
            return {"Path": path, "ProbeStatus": "SourceChanged", "ProbeErrorKind": "SourceUnavailableBeforeProbe", "ProbeErrorMessage": "Source is unavailable before probe.", "Length": int(candidate["Length"]), "LastWriteTimeUtc": str(candidate["LastWriteTimeUtc"]), "ProbeData": None}
        if before["Length"] != candidate["Length"] or before["LastWriteTimeUtc"] != candidate["LastWriteTimeUtc"]:
            return {"Path": path, "ProbeStatus": "SourceChanged", "ProbeErrorKind": "ChangedBeforeProbe", "ProbeErrorMessage": "Source metadata changed after enumeration.", "Length": before["Length"], "LastWriteTimeUtc": before["LastWriteTimeUtc"], "ProbeData": None}

        def complete(status: str, kind: Optional[str], message: Optional[str], data: Any) -> dict[str, Any]:
            after = source_snapshot(path)
            if after is None:
                return {"Path": path, "ProbeStatus": "SourceChanged", "ProbeErrorKind": "UnavailableAfterProbe", "ProbeErrorMessage": "Source became unavailable during probe.", "Length": before["Length"], "LastWriteTimeUtc": before["LastWriteTimeUtc"], "ProbeData": None}
            if after != before:
                return {"Path": path, "ProbeStatus": "SourceChanged", "ProbeErrorKind": "ChangedDuringProbe", "ProbeErrorMessage": "Source metadata changed during probe.", "Length": after["Length"], "LastWriteTimeUtc": after["LastWriteTimeUtc"], "ProbeData": None}
            return {"Path": path, "ProbeStatus": status, "ProbeErrorKind": kind, "ProbeErrorMessage": message, "Length": after["Length"], "LastWriteTimeUtc": after["LastWriteTimeUtc"], "ProbeData": data}

        result = probe_media_json(ffprobe_path, path, timeout_seconds)
        return complete(str(result["ProbeStatus"]), result.get("ProbeErrorKind"), result.get("ProbeErrorMessage"), result.get("ProbeData"))

    attempts = max(1, int(retries) + 1)
    last: Optional[dict[str, Any]] = None
    for attempt in range(1, attempts + 1):
        last = run_once(file)
        if last["ProbeStatus"] == "Success":
            last["Attempts"] = attempt
            return last
        if last["ProbeStatus"] == "SourceChanged":
            break
        if attempt < attempts:
            write_sampler_log(f"Probe retry path={file['Path']} attempt={attempt+1}/{attempts} previous_status={last['ProbeStatus']}", "DEBUG", "PROBE")
    if last is not None and last["ProbeStatus"] == "SourceChanged":
        current = source_snapshot(str(file["Path"]))
        if current is not None:
            stabilized = {**file, "Length": int(current["Length"]), "LastWriteTimeUtc": str(current["LastWriteTimeUtc"])}
            retry = run_once(stabilized)
            retry["StabilizationRetry"] = True
            if retry["ProbeStatus"] == "Success":
                retry["Attempts"] = (last.get("Attempts", 0) or 0) + 1
                return retry
            last = retry
    assert last is not None
    last["Attempts"] = attempts
    return last


def probe_failure_groups(scan: dict[str, Any]) -> list[dict[str, Any]]:
    groups: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for item in scan.get("Files", []):
        entry = item.get("ProbeEntry") or {}
        if str(entry.get("ProbeStatus")) not in {"ProbeFailed", "ProbeTimeout"}:
            continue
        path_text = str(item.get("Path", ""))
        relative = str(item.get("RelativePath") or "")
        key = relative_key(relative) if relative else path_key(path_text)
        group = groups.setdefault(key, {"Name": basename(path_text), "Items": [], "Locations": []})
        parent = ensure_trailing_separator(dirname(path_text))
        if parent and all(path_key(parent) != path_key(existing) for existing in group["Locations"]):
            group["Locations"].append(parent)
        group["Items"].append({
            "Path": path_text,
            "Status": str(entry.get("ProbeStatus") or "ProbeFailed"),
            "Kind": str(entry.get("ProbeErrorKind") or ""),
            "Message": str(entry.get("ProbeErrorMessage") or ""),
        })
    return list(groups.values())


def update_probe_failed_log(scan: dict[str, Any]) -> None:
    groups = probe_failure_groups(scan)
    if not groups:
        try:
            PROBE_FAILED_LOG_PATH.unlink()
        except FileNotFoundError:
            pass
        except Exception as exc:
            write_sampler_log(f"Could not remove stale probe failure log path={PROBE_FAILED_LOG_PATH} error={exc}", "WARNING", "PROBE")
        return
    location_count = sum(len(group["Locations"]) for group in groups)
    lines = [
        "# Media Diversity Sampler probe failures",
        f"# CompletedAtUtc: {scan.get('CompletedAtUtc') or utc_now_o()}",
        f"# Files: {len(groups)}",
        f"# Locations: {location_count}",
        "",
    ]
    for group in groups:
        lines.append(str(group["Name"]))
        for item in group["Items"]:
            lines.append(f"  Path: {item['Path']}")
            lines.append(f"  Status: {item['Status']}")
            if item["Kind"]:
                lines.append(f"  Kind: {item['Kind']}")
            if item["Message"]:
                lines.append(f"  Error: {item['Message']}")
            lines.append("")
    write_text_file_atomically(PROBE_FAILED_LOG_PATH, "\n".join(lines).rstrip() + "\n")


def invoke_probe_coordinator(inventory: list[dict[str, Any]], config: dict[str, Any], ffprobe_executable: str, cache: ProbeCacheDB, renderer: ScanRenderer) -> dict[str, Any]:
    started = time.perf_counter()
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    hit_by_key: dict[tuple[str, str], bool] = {}
    to_probe: list[dict[str, Any]] = []
    cache_hits = 0
    for file in inventory:
        key = (str(file["StorageIdentityKey"]).casefold(), relative_key(str(file["StorageRelativePath"])))
        entry = cache.get_current_success(file)
        if entry is not None:
            by_key[key] = entry
            hit_by_key[key] = True
            cache_hits += 1
        else:
            to_probe.append(file)
            hit_by_key[key] = False

    write_sampler_log("Phase: Probing media...", "INFO", "APP")
    write_sampler_log(f"Candidates: {len(inventory)} | Cache hits: {cache_hits} | Need probe: {len(to_probe)}", "INFO", "PROBE")
    workers = max(1, min(32, int(config["ProbeWorkers"])))
    timeout = max(1, int(config["FFprobeTimeoutSeconds"]))
    retries = max(0, int(config["ProbeRetries"]))
    completed = failures = timeouts = changed = 0
    dirty = 0
    last_commit = time.monotonic()
    last_progress = time.monotonic()
    progress_interval = 0.25
    source_outages: set[str] = set()
    futures: dict[cf.Future[Any], dict[str, Any]] = {}
    stopped = False
    live_progress_visible = False
    try:
        executor = cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ffprobe")
    except Exception as exc:
        raise ProbeInfrastructureError(f"Could not create the probe worker pool: {exc}") from exc
    try:
        for file in to_probe:
            if graceful_stop_requested():
                stopped = True
                break
            try:
                future = executor.submit(probe_worker, ffprobe_executable, file, timeout, retries)
            except Exception as exc:
                raise ProbeInfrastructureError(f"Could not submit probe work: {exc}") from exc
            futures[future] = file
        pending = set(futures)
        while pending:
            if graceful_stop_requested():
                stopped = True
                for future in pending:
                    future.cancel()
            done, pending = cf.wait(pending, timeout=0.1, return_when=cf.FIRST_COMPLETED)
            if not done:
                if dirty and time.monotonic() - last_commit >= 5.0:
                    cache.commit()
                    dirty = 0
                    last_commit = time.monotonic()
                if stopped and not any(not f.done() and not f.cancelled() for f in pending):
                    break
            else:
                for future in done:
                    file = futures[future]
                    if future.cancelled():
                        continue
                    try:
                        result = future.result()
                    except Exception as exc:
                        current = source_snapshot(str(file["Path"]))
                        result = {
                            "Path": file["Path"], "ProbeStatus": "SourceChanged" if current is None else "ProbeFailed",
                            "ProbeErrorKind": "SourceUnavailable" if current is None else "CoordinatorException",
                            "ProbeErrorMessage": str(exc), "Length": int(current["Length"]) if current else int(file["Length"]),
                            "LastWriteTimeUtc": str(current["LastWriteTimeUtc"]) if current else str(file["LastWriteTimeUtc"]), "ProbeData": None,
                        }
                    completed += 1
                    status = str(result["ProbeStatus"])
                    key = (str(file["StorageIdentityKey"]).casefold(), relative_key(str(file["StorageRelativePath"])))
                    entry = {
                        "Path": str(file["Path"]), "Length": int(result["Length"]), "LastWriteTimeUtc": str(result["LastWriteTimeUtc"]),
                        "ProbeStatus": status, "ProbeErrorKind": result.get("ProbeErrorKind"), "ProbeErrorMessage": result.get("ProbeErrorMessage"),
                        "ProbedAtUtc": utc_now_o(), "ProbeData": result.get("ProbeData"),
                    }
                    by_key[key] = entry
                    cache.upsert(file, entry)
                    dirty += 1
                    if status != "Success":
                        if status == "SourceChanged":
                            changed += 1
                            rid = str(file.get("RootId"))
                            if rid not in source_outages:
                                source_outages.add(rid)
                                write_sampler_log(f"Source became unavailable or changed during probe root={file.get('RootPath')} first_path={file['Path']}", "WARNING", "SOURCE")
                        else:
                            failures += 1
                            timeouts += 1 if status == "ProbeTimeout" else 0
                            write_sampler_log(f"Probe failed path={file['Path']} status={status} kind={entry.get('ProbeErrorKind')} message={entry.get('ProbeErrorMessage')}", "WARNING", "PROBE")
                    if dirty >= 100 or time.monotonic() - last_commit >= 5.0:
                        cache.commit()
                        dirty = 0
                        last_commit = time.monotonic()
            now = time.monotonic()
            elapsed_now = time.perf_counter() - started
            if elapsed_now >= 1.0 and now - last_progress >= progress_interval:
                renderer.probe_progress(completed, len(to_probe), failures, elapsed_now)
                live_progress_visible = True
                last_progress = now
            if stopped and all(f.done() or f.cancelled() for f in pending):
                break
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        cache.commit()

    elapsed = time.perf_counter() - started
    if live_progress_visible:
        renderer.clear_progress()

    if stopped or graceful_stop_requested():
        write_sampler_log(f"Probe coordinator stopped completed={completed} cache_hits={cache_hits} elapsed={format_elapsed(elapsed)}", "WARNING", "PROBE")
        return {
            "Stopped": True, "Files": [], "InventoryCount": len(inventory), "CacheHitCount": cache_hits,
            "ProbeCount": completed, "ProbeFailureCount": failures, "SourceChangedCount": changed,
            "ProbeElapsedSeconds": elapsed, "ProbeProgressShown": live_progress_visible,
        }

    scan_files = []
    for file in inventory:
        key = (str(file["StorageIdentityKey"]).casefold(), relative_key(str(file["StorageRelativePath"])))
        entry = by_key.get(key)
        current = dict(file)
        if entry and entry.get("ProbeStatus") == "Success":
            current["Length"] = int(entry["Length"])
            current["LastWriteTimeUtc"] = str(entry["LastWriteTimeUtc"])
        current["CacheHit"] = bool(hit_by_key.get(key, False))
        current["ProbeEntry"] = entry
        scan_files.append(current)
    failed_paths = [str(x["Path"]) for x in scan_files if x.get("ProbeEntry") and x["ProbeEntry"].get("ProbeStatus") in {"ProbeFailed", "ProbeTimeout"}]
    source_problem_roots = sorted({str(x.get("RootPath")) for x in scan_files if x.get("ProbeEntry") and x["ProbeEntry"].get("ProbeStatus") == "SourceChanged"})
    write_sampler_log(f"Probe coordinator complete candidates={len(inventory)} cache_hits={cache_hits} probed={completed} failures={failures} source_changed={changed} elapsed={format_elapsed(elapsed)}", "INFO", "PROBE")
    scan = {
        "Stopped": False, "Files": scan_files, "InventoryCount": len(inventory), "CacheHitCount": cache_hits,
        "ProbeCount": completed, "ProbeFailureCount": len(failed_paths), "ProbeTimeoutCount": timeouts,
        "NewProbeFailureCount": failures, "NewProbeTimeoutCount": timeouts, "SourceChangedCount": changed,
        "ProbeFailurePaths": failed_paths, "SourceProblemRoots": source_problem_roots, "CompletedAtUtc": utc_now_o(),
        "ProbeElapsedSeconds": elapsed, "ProbeProgressShown": live_progress_visible,
    }
    renderer.show_probe_summary(
        inventory_count=len(inventory), cache_hits=cache_hits, probed=completed,
        failed=len(failed_paths), elapsed=elapsed, progress_shown=live_progress_visible,
    )
    return scan


def invoke_media_scan(config: dict[str, Any], renderer: ScanRenderer) -> Optional[dict[str, Any]]:
    global CURRENT_SCAN
    configured_sources = list(config.get("SourceRoots", []))
    unavailable_sources = [str(root.get("Path", "")) for root in configured_sources if not os.path.isdir(str(root.get("Path", "")))]
    available_sources = [root for root in configured_sources if os.path.isdir(str(root.get("Path", "")))]
    ffprobe_executable = test_ffprobe_health(config)

    if not configured_sources:
        renderer.show_no_sources()
        write_sampler_log("No media sources are configured.", "WARNING", "SOURCE")
    elif unavailable_sources:
        for source in unavailable_sources:
            renderer.status("source.unavailable", "WARNING", component="SOURCE", log_message=f"Media source is unavailable: {source}", path=source)

    if ffprobe_executable is None:
        from .ffprobe import LAST_HEALTH_ERROR
        renderer.show_ffprobe_error(str(LAST_HEALTH_ERROR or "FAILED"), expected_ffprobe_path())
        return None
    if not available_sources:
        return None

    started = time.perf_counter()
    with scoped_graceful_stop("SCAN"):
        try:
            cache = load_probe_cache()
        except ProbeCacheRecoveryError as exc:
            renderer.show_cache_error("cache.recovery_failed", exc)
            return None
        except sqlite3.DatabaseError as exc:
            renderer.show_cache_error("cache.access_failed", exc)
            return None
        except (OSError, PermissionError) as exc:
            renderer.show_cache_error("cache.access_failed", exc)
            return None

        try:
            if cache.last_recovery is not None:
                renderer.show_cache_recovery(cache.last_recovery)

            inventory_config = dict(config)
            inventory_config["SourceRoots"] = available_sources
            inventory, inventory_stats = get_current_media_inventory(inventory_config, renderer)
            if graceful_stop_requested():
                write_sampler_log("Scan stopped gracefully. No current scan was created.", "INFO", "SCAN")
                return None

            corruption_recovery_used = cache.last_recovery is not None
            while True:
                try:
                    scan = invoke_probe_coordinator(inventory, config, ffprobe_executable, cache, renderer)
                    if scan.get("Stopped"):
                        write_sampler_log("Scan stopped gracefully. Completed probe results were cached; no current scan was created.", "INFO", "SCAN")
                        return None
                    try:
                        cache.export_json()
                    except sqlite3.DatabaseError:
                        raise
                    except Exception as exc:
                        renderer.status("cache.export_failed", "WARNING", component="CACHE", log_message="Probe cache JSON export failed; the SQLite cache remains valid.")
                        write_sampler_log(f"Probe cache JSON export failed error={exc}", "WARNING", "CACHE")
                    break
                except ProbeInfrastructureError as exc:
                    renderer.clear_progress()
                    renderer.show_probe_start_failed(exc)
                    return None
                except sqlite3.DatabaseError as exc:
                    renderer.clear_progress()
                    if is_confirmed_sqlite_corruption(exc) and not corruption_recovery_used:
                        try:
                            info = cache.recover_confirmed_corruption(exc)
                        except ProbeCacheRecoveryError as recovery_exc:
                            renderer.show_cache_error("cache.recovery_failed", recovery_exc)
                            return None
                        corruption_recovery_used = True
                        renderer.show_cache_recovery(info)
                        renderer.show_scan_restart_inventory(inventory_stats)
                        write_sampler_log("Probe phase restarting from the beginning after cache corruption recovery.", "WARNING", "CACHE", force=True)
                        continue
                    if is_confirmed_sqlite_corruption(exc):
                        renderer.show_cache_error("cache.still_corrupt", exc)
                    else:
                        renderer.show_cache_error("cache.access_failed", exc)
                    return None
        finally:
            try:
                cache.close(commit=False)
            except Exception as exc:
                write_sampler_log(f"Probe cache close failed error={exc}", "WARNING", "CACHE", force=True)

    scan["FilesFoundCount"] = int(inventory_stats["FilesFound"])
    scan["ExcludedCount"] = int(inventory_stats["Excluded"])
    scan["VideoCandidateCount"] = int(inventory_stats["VideoCandidates"])
    CURRENT_SCAN = scan
    try:
        update_probe_failed_log(scan)
    except Exception as exc:
        write_sampler_log(f"Probe failure log update failed path={PROBE_FAILED_LOG_PATH} error={exc}", "WARNING", "PROBE")
    write_sampler_log(
        f"Scan complete files_found={scan['FilesFoundCount']} excluded={scan['ExcludedCount']} video_candidates={scan['VideoCandidateCount']} cache_hits={scan['CacheHitCount']} probed={scan['ProbeCount']} probe_failures={scan['ProbeFailureCount']}",
        "INFO", "SCAN",
    )
    write_sampler_log(f"Full scan/probe completed elapsed={format_elapsed(time.perf_counter()-started)}", "INFO", "SCAN")
    return scan
