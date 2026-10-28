# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import copy
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from . import config as config_module
from . import localization as localization_module
from . import logger as logger_module
from . import paths as paths_module
from .constants import APP_NAME, APP_ROOT, MEDIA_SOURCES_PATH, APP_VERSION
from .file_io import write_text_file_atomically
from .localization import display_mode_name, display_pool_name, t, tp
from .logger import format_elapsed, write_sampler_log
from .platform_api import (
    clear_screen as platform_clear_screen, path_key, relative_key, remove_outer_quotes,
    platform_reason_locale_key, source_alias_group_key, source_alias_render_spec,
)

ANSI = {
    "reset": "\x1b[0m", "white": "\x1b[97m", "cyan": "\x1b[96m",
    "green": "\x1b[92m", "yellow": "\x1b[93m", "red": "\x1b[91m",
}
USE_COLOR = sys.stdout.isatty()
_UI_WAIT_LOCK = threading.Lock()
UI_WAIT_SECONDS = 0.0
_IN_PLACE_PROGRESS_WIDTH = 0
CURRENT_CONFIG: Optional[dict[str, Any]] = None
CURRENT_SCAN: Optional[dict[str, Any]] = None
CURRENT_CLASSIFIED_RECORDS: list[dict[str, Any]] = []
HAS_CURRENT_SCAN = False
CURRENT_SELECTION_SNAPSHOTS: dict[str, dict[str, Any]] = {}

_SCREEN_TITLE_KEYS = {
    "MAIN MENU": "screen.main_menu", "SCAN MEDIA": "screen.scan_media",
    "CONFIGURATION": "screen.configuration", "MEDIA SOURCES": "screen.media_sources",
    "ADD SOURCE": "screen.add_source", "REMOVE SOURCE": "screen.remove_source",
    "IMPORT SOURCES": "screen.import_sources", "EXPORT SOURCES": "screen.export_sources",
    "OUTPUTS": "screen.outputs", "ADD OUTPUT": "screen.add_output", "REMOVE OUTPUT": "screen.remove_output",
    "FFPROBE SETTINGS": "screen.ffprobe_settings", "LANGUAGE": "screen.language",
    "PREVIEW": "screen.preview", "BUILD": "screen.build", "RESUME COPY": "screen.resume_copy",
    "STARTUP RECOVERY": "screen.startup_recovery", "BUILD CONFIRMATION": "screen.build_confirmation",
    "BUILD REPLACEMENT CONFIRMATION": "screen.build_replacement_confirmation",
}


def set_current_config(config: dict[str, Any]) -> None:
    global CURRENT_CONFIG
    CURRENT_CONFIG = config


def refresh_current_config() -> dict[str, Any]:
    global CURRENT_CONFIG
    current = config_module.get_current_config()
    if current is not None:
        CURRENT_CONFIG = current
    assert CURRENT_CONFIG is not None
    return CURRENT_CONFIG


def color(text: str, name: str) -> str:
    return f"{ANSI.get(name, '')}{text}{ANSI['reset']}" if USE_COLOR else text


def localized_screen_title(menu_name: str) -> str:
    value = str(menu_name)
    if value.casefold().startswith("selection - "):
        mode = value.split("-", 1)[1].strip()
        return t("screen.selection", mode=display_mode_name(mode))
    key = _SCREEN_TITLE_KEYS.get(value.upper())
    return t(key) if key else value


def ui_input(prompt: str = "", action: str = "USER_INPUT") -> str:
    global UI_WAIT_SECONDS
    write_sampler_log(f"Waiting action={action}", "INFO", "UI")
    started = time.perf_counter()
    try:
        return input(prompt)
    finally:
        elapsed = max(0.0, time.perf_counter() - started)
        with _UI_WAIT_LOCK:
            UI_WAIT_SECONDS += elapsed
        write_sampler_log(f"Wait finished action={action} wait={format_elapsed(elapsed)}", "INFO", "UI")


def session_timing() -> tuple[float, float, float]:
    wall = max(0.0, time.perf_counter() - logger_module.SESSION_STARTED_PERF)
    with _UI_WAIT_LOCK:
        ui_wait = max(0.0, UI_WAIT_SECONDS)
    return max(0.0, wall - ui_wait), ui_wait, wall


def write_status(message: str, type_: str = "INFO", component: str = "APP", *, log_message: Optional[str]) -> None:
    c = {"OK": "green", "WARNING": "yellow", "ERROR": "red"}.get(type_, "cyan")
    print(color(str(message), c))
    write_sampler_log(str(log_message), type_, component)
    if type_ in {"WARNING", "ERROR"}:
        print()


def write_subheader(text: str) -> None:
    print(f"--- {str(text).upper()} ---")
    print()


def show_header(menu_name: str) -> None:
    width = 72
    title = f"{APP_NAME.upper()} {APP_VERSION}     |     {localized_screen_title(menu_name).upper()}"
    print(color("=" * width, "white"))
    print(color(title.center(width), "white"))
    print(color("=" * width, "white"))
    print()


def clear_screen() -> None:
    platform_clear_screen()


def show_menu_screen(menu_name: str) -> None:
    clear_screen()
    show_header(menu_name)


def show_graceful_interrupt_notice(component: str) -> None:
    print()
    key = "interrupt.graceful_copy" if str(component).upper() == "COPY" else "interrupt.graceful_task"
    print(t(key))


def wait_for_continue() -> None:
    print()
    ui_input(t("common.press_enter_continue"), "CONTINUE_ACK")


def read_submenu_choice(allowed: Iterable[int], *, screen: str = "SUBMENU") -> int:
    allowed_set = set(allowed)
    screen_id = screen.upper().replace(" ", "_").replace("-", "_")
    action = f"{screen_id}_MENU"
    while True:
        value = ui_input(t("common.choice_back"), action)
        if not value.strip():
            write_sampler_log(f"Selection screen={screen_id} action=BACK", "INFO", "UI")
            return 0
        try:
            number = int(value.strip())
            if number in allowed_set:
                if number == 0:
                    write_sampler_log(f"Selection screen={screen_id} action=BACK", "INFO", "UI")
                return number
        except ValueError:
            pass
        write_sampler_log(f"Invalid input screen={screen_id} value={value!r}", "DEBUG", "UI")
        write_status(t("common.invalid_selection"), "WARNING", component="UI", log_message="Invalid selection")


def read_main_menu_choice(allowed: Iterable[int]) -> int:
    allowed_set = set(allowed)
    while True:
        value = ui_input(t("common.choice"), "MAIN_MENU")
        if not value.strip():
            return -1
        try:
            number = int(value.strip())
            if number in allowed_set:
                return number
        except ValueError:
            pass
        write_sampler_log(f"Invalid input screen=MAIN_MENU value={value!r}", "DEBUG", "UI")
        write_status(t("common.invalid_selection"), "WARNING", component="UI", log_message="Invalid selection")


def read_positive_integer(prompt: str, default: Optional[int] = None, minimum: int = 1, maximum: int = 2_147_483_647) -> int:
    while True:
        suffix = f" [{default}]" if default is not None else ""
        value = ui_input(f"{prompt}{suffix}: ", "INTEGER_INPUT").strip()
        if not value and default is not None:
            return default
        try:
            number = int(value)
            if minimum <= number <= maximum:
                return number
        except ValueError:
            pass
        write_status(t("common.enter_number_range", minimum=minimum, maximum=maximum), "WARNING", component="UI", log_message=f"Enter a number between {minimum} and {maximum}")


def write_in_place_progress(text: str) -> None:
    global _IN_PLACE_PROGRESS_WIDTH
    value = str(text)
    try:
        width = max(20, shutil.get_terminal_size((120, 20)).columns - 1)
    except Exception:
        width = 119
    if len(value) > width:
        value = value[:max(1, width - 1)] + "…"
    padded_width = max(_IN_PLACE_PROGRESS_WIDTH, len(value))
    print(f"\r{value.ljust(padded_width)}", end="", flush=True)
    _IN_PLACE_PROGRESS_WIDTH = len(value)


def clear_in_place_progress() -> None:
    global _IN_PLACE_PROGRESS_WIDTH
    if _IN_PLACE_PROGRESS_WIDTH:
        print("\r" + (" " * _IN_PLACE_PROGRESS_WIDTH) + "\r", end="", flush=True)
        _IN_PLACE_PROGRESS_WIDTH = 0


def reset_current_scan_state() -> None:
    global CURRENT_SCAN, CURRENT_CLASSIFIED_RECORDS, HAS_CURRENT_SCAN
    CURRENT_SCAN = None
    CURRENT_CLASSIFIED_RECORDS = []
    HAS_CURRENT_SCAN = False
    CURRENT_SELECTION_SNAPSHOTS.clear()
    try:
        from . import scan
        scan.reset_scan_state()
    except Exception:
        pass


def _print_scan_count(label: str, value: int, suffix: str = "") -> None:
    key = {
        "Files found": "scan.files_found", "Excluded": "scan.excluded", "Video candidates": "scan.video_candidates",
        "Cache hits": "scan.cache_hits", "Probed": "scan.probed", "Failed": "scan.failed",
        "SDR": "classification.sdr", "HDR": "classification.hdr",
        "SDR Uncertain": "classification.sdr_uncertain", "HDR Uncertain": "classification.hdr_uncertain",
    }.get(label)
    display = t(key) if key else str(label)
    print(f"  {display:<18}{int(value):>6}{suffix}")


def _format_short_duration(seconds: float) -> str:
    total = max(0, int(round(float(seconds))))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s" if secs else f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    return f"{secs}s"


def _build_progress_bar(current: int, total: int, width: int = 14) -> str:
    import math
    filled = width if total <= 0 else max(0, min(width, math.floor((max(0, current) / total) * width)))
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


def _probe_progress_line(completed: int, total: int, failures: int, elapsed: float) -> str:
    bar = _build_progress_bar(completed, total)
    count_width = max(1, len(str(max(0, total))))
    completed_text = f"{completed:>{count_width}}"
    if elapsed >= 3.0 and completed > 0 and total > completed:
        rate = completed / elapsed
        if rate > 0:
            remaining = (total - completed) / rate
            return t("scan.probing_progress_eta", bar=bar, completed=completed_text, total=total, failed=failures, eta=_format_short_duration(remaining))
    return t("scan.probing_progress", bar=bar, completed=completed_text, total=total, failed=failures)


def _english_count(count: int, singular: str, plural: Optional[str] = None) -> str:
    key = {"file": "count.files", "location": "count.locations", "output": "count.outputs", "combo": "count.combos"}.get(singular)
    if key:
        return tp(key, count)
    word = singular if count == 1 else (plural if plural is not None else singular + "s")
    return f"{count} {word}"


class ConsoleRenderer:
    def status(self, key: str, type_: str, component: str = "APP", log_message: Optional[str] = None, **kwargs: Any) -> None:
        write_status(t(key, **kwargs), type_, component=component, log_message=log_message or key)

    def scan_started(self) -> None:
        print(t("scan.section")); print()

    def show_inventory(self, stats: dict[str, int]) -> None:
        _print_scan_count("Files found", stats["FilesFound"])
        _print_scan_count("Excluded", stats["Excluded"])
        _print_scan_count("Video candidates", stats["VideoCandidates"])
        print()

    def probe_progress(self, completed: int, total: int, failures: int, elapsed: float) -> None:
        write_in_place_progress(_probe_progress_line(completed, total, failures, elapsed))

    def clear_progress(self) -> None:
        clear_in_place_progress()

    def show_probe_summary(self, *, inventory_count: int, cache_hits: int, probed: int, failed: int, elapsed: float, progress_shown: bool) -> None:
        duration_text = _format_short_duration(elapsed) if progress_shown else None
        print(t("scan.probe_section") if duration_text is None else f"{t('scan.probe_section')}{'':15}{duration_text}")
        print()
        cache_suffix = t("scan.cache_all_suffix") if cache_hits == inventory_count and probed == 0 else ""
        _print_scan_count("Cache hits", cache_hits, cache_suffix)
        if probed > 0:
            _print_scan_count("Probed", probed)
            _print_scan_count("Failed", failed)
        print()

    def show_no_sources(self) -> None:
        print(t("source.none_configured")); print(t("source.configure_hint"))

    def show_ffprobe_error(self, kind: str, expected_path: str) -> None:
        if kind == "MISSING":
            print(t("ffprobe.required"))
            print(t("ffprobe.expected"))
            print(expected_path)
            print()
            print(t("ffprobe.readme"))
            return
        write_status(t("ffprobe.health_failed"), "ERROR", component="FFPROBE", log_message="FFprobe health check failed.")
        if logger_module.LOG_PATH is not None:
            print(color(t("common.log_path", path=logger_module.LOG_PATH), "cyan"))
            print()

    def show_cache_error(self, key: str, exc: BaseException) -> None:
        write_status(t(key), "ERROR", component="CACHE", log_message=t(key))
        write_sampler_log(f"Probe cache operation failed error={exc}", "ERROR", "CACHE", force=True)
        print(t("common.see_log")); wait_for_continue()

    def show_cache_recovery(self, info: dict[str, Any]) -> None:
        print(); print(t("cache.corruption_detected")); print()
        preserved = list(info.get("Preserved") or [])
        if preserved:
            print(t("cache.damaged_preserved"))
            for item in preserved:
                try: display = str(Path(item).relative_to(APP_ROOT))
                except Exception: display = str(item)
                print(f"  {display}")
            print()
        print(t("cache.new_created")); print(t("cache.reprobe")); wait_for_continue(); show_menu_screen("SCAN MEDIA")

    def show_scan_restart_inventory(self, stats: dict[str, int]) -> None:
        print(t("scan.section")); print()
        _print_scan_count("Files found", stats["FilesFound"]); _print_scan_count("Excluded", stats["Excluded"]); _print_scan_count("Video candidates", stats["VideoCandidates"]); print()

    def show_probe_start_failed(self, exc: BaseException) -> None:
        write_status(t("cache.probe_start_failed"), "ERROR", component="PROBE", log_message="Probe worker infrastructure failed.")
        write_sampler_log(f"Probe worker infrastructure failed error={exc}", "ERROR", "PROBE", force=True)
        wait_for_continue()

    def show_classification(self, counts: Any) -> None:
        print(t("scan.classification")); print()
        _print_scan_count("SDR", counts["SDR"]); _print_scan_count("HDR", counts["HDR"])
        _print_scan_count("SDR Uncertain", counts["SDR Uncertain"]); _print_scan_count("HDR Uncertain", counts["HDR Uncertain"])

    def show_probe_failures(self, scan_result: dict[str, Any]) -> None:
        from . import scan
        groups = scan.probe_failure_groups(scan_result)
        if not groups: return
        locations = sum(len(group["Locations"]) for group in groups)
        print(); print(t("scan.probe_failed", files=_english_count(len(groups), "file"), locations=_english_count(locations, "location"))); print()
        visible = groups[:10]
        for group in visible:
            print(f"  {group['Name']}")
            for location in group["Locations"]: print(f"    {location}")
            print()
        remaining = len(groups) - len(visible)
        if remaining > 0: print(tp("scan.probe_more", remaining))

    def selection_analysis_started(self, pool: str, live_preview: bool) -> None:
        if live_preview: write_in_place_progress(t("selection.analyzing_pool", pool=display_pool_name(pool)))
        else:
            print(); write_subheader(display_pool_name(pool)); print(t("selection.analyzing"))

    def selection_analysis_prepared(self, pool: str, live_preview: bool) -> None:
        if not live_preview: print(t("selection.analysis_prepared"))

    def selection_started(self, pool: str) -> None:
        print(); print(t("selection.selecting", pool=display_pool_name(pool)))

    def selection_finished(self, pool: str, count: int) -> None:
        write_status(t("selection.selected_sentence", files=_english_count(count, "file")), "OK", component="SELECTION", log_message=f"Selected {count} files.")

    def staged_recovery_failed(self) -> None:
        write_status(t("build.staged_recovery_failed"), "ERROR", component="RECOVERY", log_message="A staged build transaction could not be recovered safely. See the log for details.")

    def staged_recovery_pending(self) -> None:
        write_status(t("build.staged_pending"), "WARNING", component="RECOVERY", log_message="Build transaction recovery is pending because a participating Output is unavailable.")

    def copy_progress(self, mode: str, current: int, total: int, file_name: str) -> None:
        write_in_place_progress(_build_progress_line(mode, current, total, "copying", file_name=file_name))

    def validation_progress(self, mode: str, current: int, total: int) -> None:
        write_in_place_progress(_build_progress_line(mode, current, total, "validating"))

    def copy_failed(self, mode: str, file_name: str) -> None: print(t("build.copy_failed", mode=display_mode_name(mode), file=file_name))
    def timestamp_mismatch(self, mode: str, file_name: str) -> None: print(t("build.timestamp_mismatch", mode=display_mode_name(mode), file=file_name))
    def manifest_update_failed(self, mode: str) -> None: print(t("build.manifest_update_failed", mode=display_mode_name(mode)))
    def validation_failed(self, mode: str, file_name: str) -> None: print(t("build.validation_failed", mode=display_mode_name(mode), file=file_name))
    def txt_write_failed(self, mode: str) -> None: print(t("build.txt_failed", mode=display_mode_name(mode)))

    def build_progress_started(self) -> None:
        print(); write_subheader(t("build.progress"))

    def build_output_started(self, number: int, total: int, library_path: str, separate: bool) -> None:
        if separate: print()
        print(t("build.output_number", number=number, total=total)); print(t("build.library", path=library_path)); print(); print(t("build.ctrl_c")); print()

    def unexpected_copy_failure(self, mode: str) -> None:
        print(t("build.unexpected_copy_failure", mode=display_mode_name(mode)))

    def build_manifest_summary(self, summary: dict[str, Any]) -> None:
        print(_build_summary_line(summary))


RENDERER = ConsoleRenderer()


def _build_progress_line(mode: str, current: int, total: int, state: str, *, file_name: Optional[str] = None, errors: int = 0) -> str:
    if state == "copying": detail = t("status.copying") + (f" · {file_name}" if file_name else "")
    elif state == "validating": detail = t("status.validating")
    elif errors: detail = tp("status.copied_errors", errors)
    elif state == "incomplete": detail = t("status.copied_incomplete")
    elif state == "not_started": detail = t("status.not_started")
    else: detail = t("status.copied_validated")
    return f"{display_mode_name(mode):<10} {_build_progress_bar(current, total)} {current:>{max(1, len(str(total)))}}/{total}  {detail}"


def _build_summary_line(summary: dict[str, Any]) -> str:
    if int(summary.get("FailedCount", 0)) > 0: state = "complete"
    elif str(summary.get("BuildStatus")) == "Complete" and bool(summary.get("ValidationComplete")): state = "complete"
    elif int(summary.get("CompletedCount", 0)) == 0 and int(summary.get("PendingCount", 0)) == int(summary.get("SelectedCount", 0)): state = "not_started"
    else: state = "incomplete"
    return _build_progress_line(str(summary["BuildMode"]), int(summary["CompletedCount"]), int(summary["SelectedCount"]), state, errors=int(summary.get("FailedCount", 0)))


def invoke_scan_and_classification(config: dict[str, Any]) -> Optional[dict[str, Any]]:
    global CURRENT_SCAN, CURRENT_CLASSIFIED_RECORDS, HAS_CURRENT_SCAN
    from . import selection
    result = selection.invoke_scan_and_classification(config, RENDERER)
    if result is None: return None
    CURRENT_SCAN = result["Scan"]; CURRENT_CLASSIFIED_RECORDS = result["ClassifiedRecords"]; HAS_CURRENT_SCAN = True
    return result


def _current_or_new_scan_result() -> Optional[dict[str, Any]]:
    if HAS_CURRENT_SCAN and CURRENT_SCAN is not None and CURRENT_CLASSIFIED_RECORDS:
        return {"Scan": CURRENT_SCAN, "ClassifiedRecords": CURRENT_CLASSIFIED_RECORDS}
    if CURRENT_CONFIG is None: return None
    return invoke_scan_and_classification(CURRENT_CONFIG)



def _show_config_save_failed() -> None:
    write_status(t("config.save_failed"), "ERROR", component="CONFIG", log_message="Configuration could not be saved. No changes were applied.")


def save_runtime_config(candidate: dict[str, Any]) -> bool:
    if not config_module.save_runtime_config(candidate):
        _show_config_save_failed()
        return False
    refresh_current_config()
    return True


def show_source_batch_review(plan: dict[str, Any], *, compact_network: bool = False) -> bool:
    network_groups: dict[tuple[str, str], dict[str, Any]] = {}
    network_input_keys: set[str] = set()
    for item in plan.get("ResolvedInputs", []):
        alias_group = source_alias_group_key(item)
        if not alias_group:
            continue
        key = (alias_group, relative_key(str(item.get("SourceRootRelativePath", ""))))
        group = network_groups.setdefault(key, {"Resolved": str(item.get("Resolved", "")), "From": []})
        raw = str(item.get("Input", "")); network_input_keys.add(path_key(raw))
        if raw not in group["From"]: group["From"].append(raw)
    visible_network_groups = [g for g in network_groups.values() if len(g["From"]) > 1 or any(path_key(raw) != path_key(str(g["Resolved"])) for raw in g["From"])]
    visible_duplicates = [(source, resolved) for source, resolved in plan.get("DuplicateInputs", []) if path_key(source) not in network_input_keys]
    has_review = bool(visible_network_groups or plan.get("SameStorageGroups") or plan.get("AlreadyConfigured") or plan.get("AlreadyCovered") or visible_duplicates or plan.get("SelectedCovered") or plan.get("Unavailable") or plan.get("Invalid") or plan.get("Replacements"))
    if not has_review: return False
    print()
    if compact_network:
        if visible_network_groups:
            print(t("source.review.resolved_network_paths", count=len(visible_network_groups))); print()
            for group in visible_network_groups:
                resolved = str(group["Resolved"]); print(f"  {resolved}")
                aliases: list[tuple[int, int, str, str]] = []
                for index, raw in enumerate(group["From"]):
                    if path_key(raw) == path_key(resolved):
                        continue
                    kind, value = paths_module.source_review_alias(raw)
                    order, label_key = source_alias_render_spec(kind)
                    aliases.append((order, index, label_key, value))
                for _order, _index, label_key, value in sorted(aliases, key=lambda item: (item[0], item[1])):
                    print(f"      {t(label_key):<10}{value}")
                print()
    else:
        write_subheader(t("source.review.source_resolution"))
        if visible_network_groups:
            write_subheader(t("source.review.resolved_network"))
            for group in visible_network_groups:
                print(t("source.review.resolved_to")); print(group["Resolved"]); print(); print(t("source.review.from"))
                for raw in group["From"]: print(raw)
                print()
    if plan.get("SameStorageGroups"):
        write_subheader(t("source.review.same_network_storage"))
        for group in plan["SameStorageGroups"]:
            print(t("source.review.equivalent_paths"))
            for item in group: print(item)
            print()
    if plan.get("AlreadyConfigured"):
        write_subheader(t("source.review.already_configured"))
        for item in plan["AlreadyConfigured"]: print(item)
        print()
    if plan.get("AlreadyCovered"):
        write_subheader(t("source.review.already_covered"))
        for source, covering in plan["AlreadyCovered"]:
            print(t("source.review.input")); print(source); print(t("source.review.covered_by")); print(covering); print()
    if visible_duplicates:
        write_subheader(t("source.review.duplicate_inputs")); print(t("source.review.ignored"))
        for source, _ in visible_duplicates: print(source)
        print()
    if plan.get("SelectedCovered"):
        write_subheader(t("source.review.selected_covered"))
        for source, covering in plan["SelectedCovered"]:
            print(t("source.review.input")); print(source); print(t("source.review.covered_by")); print(covering); print()
    if plan.get("Unavailable"):
        write_subheader(t("source.review.unavailable"))
        for source, _ in plan["Unavailable"]: print(source)
        print()
    if plan.get("Invalid"):
        write_subheader(t("source.review.invalid"))
        for source, reason in plan["Invalid"]:
            print(t("source.review.input")); print(source)
            if reason:
                print(t("source.review.reason")); print(t("source.review.invalid_reason")); write_sampler_log(f"Invalid source path={source!r} reason={reason}", "WARNING", "PATH")
            print()
    if plan.get("Replacements"):
        write_subheader(t("source.review.replacements"))
        for parent, children in plan["Replacements"]:
            print(t("source.review.selected_source")); print(parent); print(t("source.review.replaces"))
            for child in children: print(child)
            print()
    return True


def set_configured_source_roots(config: dict[str, Any], values: Iterable[str], *, compact_network: bool = False) -> tuple[bool, bool]:
    changed, plan, save_failed = paths_module.set_configured_source_roots(config, values)
    review = show_source_batch_review(plan, compact_network=compact_network)
    if save_failed:
        _show_config_save_failed(); return False, review
    if not changed:
        write_status(t("config.no_changes"), "INFO", component="PATH", log_message="No configuration changes were required.")
        return False, review
    refresh_current_config(); reset_current_scan_state(); return True, review


def add_configured_source_roots(config: dict[str, Any], values: Iterable[str]) -> tuple[bool, bool]:
    changed, plan, save_failed = paths_module.add_configured_source_roots(config, values)
    review = show_source_batch_review(plan)
    if save_failed:
        _show_config_save_failed(); return False, review
    if not changed:
        write_status(t("config.no_changes"), "INFO", component="PATH", log_message="No configuration changes were required.")
        return False, review
    refresh_current_config(); reset_current_scan_state(); return True, review


def remove_configured_source_root(config: dict[str, Any], root_id: str) -> bool:
    changed, save_failed = paths_module.remove_configured_source_root(config, root_id)
    if save_failed: _show_config_save_failed(); return False
    if changed: refresh_current_config(); reset_current_scan_state()
    return changed


def add_configured_outputs(config: dict[str, Any], base_paths: Iterable[str]) -> int:
    result = paths_module.add_configured_outputs(config, base_paths)
    for issue in result["Issues"]:
        kind, path = str(issue["Type"]), str(issue["Path"])
        if kind == "INVALID":
            write_status(t("output.invalid", path=path), "WARNING", component="PATH", log_message=f"Output is unavailable or invalid: {path}")
        elif kind == "ALREADY_CONFIGURED":
            write_status(t("output.already_configured", path=path), "WARNING", component="PATH", log_message=f"Output is already configured: {path}")
        elif kind == "OVERLAP":
            write_status(t("output.overlap_skipped", path=path), "WARNING", component="PATH", log_message=f"Output overlaps a configured output and was skipped: {path}")
    if result["SaveFailed"]:
        _show_config_save_failed(); return 0
    if result["Added"]:
        refresh_current_config()
    else:
        write_status(t("config.no_changes"), "INFO", component="PATH", log_message="No configuration changes were required.")
    return int(result["Added"])


def remove_configured_output(config: dict[str, Any], library_id: str) -> bool:
    changed, save_failed = paths_module.remove_configured_output(config, library_id)
    if save_failed: _show_config_save_failed(); return False
    if changed: refresh_current_config()
    return changed


def import_source_roots_from_text_file(config: dict[str, Any]) -> bool:
    show_menu_screen("IMPORT SOURCES")
    raw = remove_outer_quotes(ui_input(t("source.import_file_prompt"), "SOURCE_IMPORT_FILE"))
    if not raw.strip(): return False
    path = Path(os.path.expanduser(raw))
    if not path.is_file():
        write_status(t("source.import_not_found"), "WARNING", component="PATH", log_message="Import file was not found."); wait_for_continue(); return False
    try: values = paths_module.import_paths_file(path)
    except Exception as exc:
        write_status(t("source.import_read_failed"), "ERROR", component="PATH", log_message="The import file could not be read.")
        write_sampler_log(f"Source import read failed path={path} error={exc}", "ERROR", "PATH", force=True); wait_for_continue(); return False
    if not values:
        write_status(t("source.import_empty"), "WARNING", component="PATH", log_message="No media sources were found in the selected file."); wait_for_continue(); return False
    changed, review_shown = set_configured_source_roots(config, values, compact_network=True)
    if review_shown: wait_for_continue()
    return changed


def export_source_roots_to_text_file(config: dict[str, Any]) -> bool:
    show_menu_screen("EXPORT SOURCES")
    lines = ["# " + t("source.export_header", app_name=APP_NAME, version=APP_VERSION), "# " + t("source.export_hint")] + [str(r["Path"]) for r in config["SourceRoots"]]
    try: write_text_file_atomically(MEDIA_SOURCES_PATH, "\n".join(lines) + "\n")
    except Exception as exc:
        write_status(t("source.export_failed"), "ERROR", component="PATH", log_message="Media Sources could not be exported.")
        write_sampler_log(f"Source export write failed path={MEDIA_SOURCES_PATH} error={exc}", "ERROR", "PATH", force=True); wait_for_continue(); return False
    write_sampler_log(f"Media Sources exported to {MEDIA_SOURCES_PATH}", "INFO", "PATH")
    print(t("source.exported_to")); print(MEDIA_SOURCES_PATH); wait_for_continue(); return True


# Migrated v1.2.0 console workflows/menus.
def show_compact_selected_files(pool_selections: list[dict[str, Any]]) -> None:
    print()
    for pool_selection in pool_selections:
        pool = str(pool_selection["Pool"])
        selected = list(pool_selection["Selection"]["Selected"])
        if not selected:
            print(t("selection.pool_empty", pool=display_pool_name(pool), none=t("common.none")))
            print()
            continue
        print(t("selection.pool_files", pool=display_pool_name(pool), files=_english_count(len(selected), "file")))
        print("-" * 72)
        for number, item in enumerate(selected, 1):
            for line in _compact_selected_item_lines(item, number):
                print(line)
            print()


def show_selected_snapshot_files(snapshot: dict[str, Any]) -> None:
    for item in snapshot.get("SelectedFiles", []):
        print()
        for line in selected_item_ux_lines(item):
            print(line)
        print("=" * 72)


def invoke_general_scan_workflow() -> bool:
    if CURRENT_CONFIG is None:
        return False
    return invoke_scan_and_classification(CURRENT_CONFIG) is not None


def show_selection_quantities(contexts: list[dict[str, Any]], selected: dict[str, int]) -> None:
    print()
    print(t("selection.quantities"))
    print()
    print(f"{'':<18}{t('selection.table_candidates'):>10}   {t('selection.table_recommended'):>11}   {t('selection.table_selected'):>8}")
    for context in contexts:
        pool = str(context["Pool"])
        candidates = len(context["Candidates"])
        recommended = int(context["Plan"]["RecommendedMinimum"])
        chosen = int(selected.get(pool, 0))
        print(f"{display_pool_name(pool):<18}{candidates:>10}   {recommended:>11}   {chosen:>8}")
    print()
    total_selected = sum(int(selected.get(str(context["Pool"]), 0)) for context in contexts)
    print(t("selection.total", files=_english_count(total_selected, "file")))


def read_requested_counts(contexts: list[dict[str, Any]], *, compact_preview: bool = False) -> dict[str, int]:
    requested: dict[str, int] = {}
    if compact_preview:
        defaults = {str(context["Pool"]): selection_default_count(context) for context in contexts}
        show_selection_quantities(contexts, defaults)
        for context in contexts:
            pool = str(context["Pool"])
            candidates = len(context["Candidates"])
            recommended = int(context["Plan"]["RecommendedMinimum"])
            if candidates == 0:
                requested[pool] = 0
                continue
            default = defaults[pool]
            print()
            print(display_pool_name(pool))
            count = read_positive_integer(t("selection.number_of_files"), default, 1, candidates)
            requested[pool] = count
            if count < recommended:
                write_status(t('selection.requested_below'), 'WARNING', component='SELECTION', log_message='Requested count is below the recommended minimum. Full detected diversity will not be covered.')
        return requested

    print()
    write_subheader(t("selection.quantities"))
    for context in contexts:
        pool = str(context["Pool"])
        candidates = len(context["Candidates"])
        recommended = int(context["Plan"]["RecommendedMinimum"])
        print()
        write_subheader(display_pool_name(pool))
        print(t("selection.candidates", count=candidates))
        print(t("selection.recommended_min", count=recommended))
        if candidates == 0:
            requested[pool] = 0
            print(t("selection.number_zero"))
            continue
        default = min(candidates, max(1, recommended))
        count = read_positive_integer(t("selection.number_of_files"), default, 1, candidates)
        requested[pool] = count
        if count < recommended:
            write_status(t('selection.requested_below'), 'WARNING', component='SELECTION', log_message='Requested count is below the recommended minimum. Full detected diversity will not be covered.')
    return requested


def invoke_selection_preparation(
    selection_mode: str,
    *,
    force_new_selection: bool = True,
    preview_ui: bool = False,
) -> Optional[dict[str, Any]]:
    if CURRENT_CONFIG is None:
        return None
    if force_new_selection:
        CURRENT_SELECTION_SNAPSHOTS.pop(selection_mode, None)
    scan_result = _current_or_new_scan_result()
    if scan_result is None:
        return None
    contexts = analyze_selection_diversity(
        scan_result["ClassifiedRecords"], selection_mode, live_preview=preview_ui
    )
    if not any(context["Candidates"] for context in contexts):
        write_status(t('selection.no_candidates', mode=display_mode_name(selection_mode)), 'WARNING', component='SELECTION', log_message=f'No candidates were found for Selection - {selection_mode}.')
        return None
    requested = read_requested_counts(contexts, compact_preview=preview_ui)
    pool_selections = finalize_pool_selections(contexts, requested, quiet=preview_ui)
    preparation = create_selection_preparation(selection_mode, pool_selections, scan_result)
    if preparation is not None:
        if preview_ui:
            show_compact_selected_files(pool_selections)
        else:
            for mode in ("SDR", "HDR", "Uncertain"):
                snapshot = preparation["SnapshotsByMode"].get(mode)
                if snapshot is None:
                    continue
                print()
                write_subheader(t("selection.selected_files", mode=display_mode_name(mode)))
                show_selected_snapshot_files(snapshot)
    return preparation


def show_preview_summary(preparation: dict[str, Any]) -> None:
    rows = preparation["PoolSelections"]
    print("-" * 72)
    print(t("preview.complete"))
    print()
    print(f"{'':<18}{t('preview.table_selected'):>8}   {t('preview.table_requested'):>9}   {t('preview.table_recommended'):>11}")
    for row in rows:
        selection = row["Selection"]
        print(
            f"{display_pool_name(str(row['Pool'])):<18}{len(selection['Selected']):>8}   "
            f"{int(selection['RequestedCount']):>9}   {int(selection['RecommendedMinimum']):>11}"
        )
    print()
    selected_count = sum(len(row["Selection"]["Selected"]) for row in rows)
    print(t("preview.total", files=_english_count(selected_count, "file"), size=format_byte_size(preparation_total_bytes(preparation))))


def invoke_preview_workflow(selection_mode: str) -> bool:
    show_menu_screen("PREVIEW")
    write_sampler_log(f"Preview started mode={selection_mode}", "INFO", "SELECTION")
    preparation = invoke_selection_preparation(selection_mode, force_new_selection=True, preview_ui=True)
    if preparation is None:
        write_sampler_log(f"Preview completed mode={selection_mode} status=NO_SELECTION", "INFO", "SELECTION")
        wait_for_continue()
        return False
    show_preview_summary(preparation)
    selected_count = sum(len(x["Selection"]["Selected"]) for x in preparation["PoolSelections"])
    write_sampler_log(f"Preview completed mode={selection_mode} selected={selected_count} size_bytes={preparation_total_bytes(preparation)}", "INFO", "SELECTION")
    print()
    print(f"[1] {t('menu.start_copy')}")
    print(f"[0] {t('common.back')}")
    print()
    choice = read_submenu_choice((0, 1), screen="PREVIEW_COMPLETE")
    if choice == 1:
        write_sampler_log("Selection screen=PREVIEW_COMPLETE action=START_COPY", "INFO", "UI")
        show_menu_screen("BUILD")
        build_result = invoke_new_build_from_preparation(preparation)
        if build_result is not None:
            wait_for_continue()
    return True


def _localize_build_reason(reason: str) -> str:
    text = str(reason or "").strip()
    platform_key = platform_reason_locale_key(text)
    if platform_key:
        return t(platform_key)
    exact = {
        "Library marker is missing.": "build.reason.marker_missing",
        "Library marker is unreadable.": "build.reason.marker_unreadable",
        "Library marker schema or type is invalid.": "build.reason.marker_invalid",
        "LibraryId does not match the configured output.": "build.reason.library_id_mismatch",
        "Output base path is not available.": "build.reason.output_base_unavailable",
        "Output library path exists as a file.": "build.reason.library_path_file",
        "Output library could not be created.": "build.reason.library_create_failed",
        "Output library overlaps a media source on the same storage.": "build.reason.overlap_source",
        "Output library is inside a media source root.": "build.reason.inside_source",
        "A media source root is inside the output library.": "build.reason.source_inside_library",
        "Source/output relationship could not be validated safely.": "build.reason.relationship_unverified",
        "Mode folder is not inside the verified library path.": "build.reason.mode_outside",
        "Mode folder path exists as a file.": "build.reason.mode_path_file",
        "Not enough free space for the complete build workload on this storage.": "build.reason.space_complete",
        "The output is no longer configured.": "build.reason.output_removed",
        "Output or Library is currently not available.": "build.reason.output_library_unavailable",
        "The complete build manifest set is not available.": "build.reason.manifest_set_missing",
        "Not enough free space to resume the remaining files.": "build.reason.space_resume",
        "Not available": "common.not_available",
    }
    key = exact.get(text)
    if key:
        return t(key)
    match = re.fullmatch(r"Write access test failed for (.+)\.", text)
    if match:
        return t("build.reason.write_access", path=match.group(1))
    return t("build.reason.generic")


def _preflight_reason_text(state: dict[str, Any]) -> str:
    reasons: list[str] = []
    for raw in str(state.get("Reason") or "").split(";"):
        reason = raw.strip()
        if ": " in reason and reason.split(": ", 1)[0] in {"SDR", "HDR", "Uncertain"}:
            reason = reason.split(": ", 1)[1]
        localized = _localize_build_reason(reason) if reason else ""
        if localized and localized not in reasons:
            reasons.append(localized)
    return "; ".join(reasons)


def show_build_plan(preparation: dict[str, Any], ready: list[dict[str, Any]], unavailable: list[dict[str, Any]]) -> None:
    assert CURRENT_CONFIG is not None
    states = {str(x["Output"]["LibraryId"]).casefold(): x for x in [*ready, *unavailable]}
    rows = preflight_pool_rows(preparation)
    total_files = sum(count for _, count, _ in rows)
    total_size = sum(size for _, _, size in rows)

    write_subheader(t("build.preflight"))
    print(f"{'':<18}{t('build.table_files'):>7}   {t('build.table_size'):>10}")
    for pool, count, size in rows:
        print(f"{display_pool_name(pool):<18}{count:>7}   {format_byte_size(size):>10}")
    print("-" * 35)
    print(f"{t('build.table_total'):<18}{total_files:>7}   {format_byte_size(total_size):>10}")
    print()
    print(t("build.output"))

    outputs = list(CURRENT_CONFIG["Outputs"])
    for number, output in enumerate(outputs, 1):
        state = states.get(str(output["LibraryId"]).casefold())
        library = get_output_library_path(CURRENT_CONFIG, output)
        is_ready = bool(state and state["Ready"])
        status = t("common.ready") if is_ready else t("common.not_available")
        prefix = "  " if len(outputs) == 1 else f"  {number}  "
        print(f"{prefix}{library}   {status}")

        reason = _preflight_reason_text(state or {})
        if reason:
            print(("     " if len(outputs) > 1 else "  ") + t("build.reason_label", reason=reason))

        free = state.get("FreeSpaceBytes") if state else None
        reclaim = int(state.get("ReclaimBytes", 0)) if state else 0
        required = int(state.get("AdditionalRequiredBytes", total_size)) if state else total_size
        metrics_indent = "     " if len(outputs) > 1 else ""
        print(metrics_indent + t("build.available", value=t("common.unknown") if free is None else format_byte_size(int(free))))
        if reclaim > 0:
            print(metrics_indent + t("build.reclaim", value=format_byte_size(reclaim)))
        print(metrics_indent + t("build.required", value=format_byte_size(required)))
        if state and state.get("ShortfallBytes") is not None:
            print(metrics_indent + t("build.not_enough_space", value=format_byte_size(int(state["ShortfallBytes"]))))
        if len(outputs) > 1 and number != len(outputs):
            print()

    if len(outputs) > 1 and unavailable:
        print()
        print(t("build.ready_summary", ready=len(ready), unavailable=len(unavailable)))

    print()
    print(t("build.mode", mode=display_mode_name(str(preparation["SelectionMode"]))))


def confirm_continue_build() -> bool:
    print()
    print(f"[1] {t('common.continue')}")
    print(f"[0] {t('common.back')}")
    print()
    choice = read_submenu_choice((0, 1), screen="BUILD_CONFIRMATION")
    if choice == 1:
        write_sampler_log("Selection screen=BUILD_CONFIRMATION action=CONTINUE", "INFO", "UI")
        return True
    return False


def confirm_destructive_replace(selection_mode: str) -> bool:
    print()
    warning_key = {
        "SDR": "build.replace_warning_sdr", "HDR": "build.replace_warning_hdr",
        "Uncertain": "build.replace_warning_uncertain", "ALL": "build.replace_warning_all",
    }.get(selection_mode, "build.replace_warning_default")
    warning = t(warning_key)
    print(warning)
    print()
    print(f"[1] {t('common.continue')}")
    print(f"[0] {t('common.back')}")
    print()
    choice = read_submenu_choice((0, 1), screen="BUILD_REPLACEMENT_CONFIRMATION")
    if choice == 1:
        write_sampler_log("Selection screen=BUILD_REPLACEMENT_CONFIRMATION action=CONTINUE", "INFO", "UI")
        return True
    return False


def show_build_final_summary(summaries: list[dict[str, Any]], expected_manifest_count: Optional[int] = None) -> None:
    expected = expected_manifest_count if expected_manifest_count is not None else len(summaries)
    complete = len(summaries) == expected and all(str(summary["BuildStatus"]) == "Complete" for summary in summaries)
    total = sum(int(summary["SelectedCount"]) for summary in summaries)
    completed = sum(int(summary["CompletedCount"]) for summary in summaries)
    errors = sum(int(summary["FailedCount"]) for summary in summaries)
    if complete:
        write_sampler_log(f"Build completed status=COMPLETE manifests={len(summaries)}/{expected}", "INFO", "BUILD")
        return
    print()
    write_subheader(t("build.incomplete"))
    grouped: dict[int, list[dict[str, Any]]] = {}
    for summary in summaries:
        grouped.setdefault(int(summary.get("OutputNumber", 1)), []).append(summary)
    mode_order = {"SDR": 0, "HDR": 1, "Uncertain": 2}
    for output_number in sorted(grouped):
        group = sorted(grouped[output_number], key=lambda x: mode_order.get(str(x.get("BuildMode")), 99))
        print(t("build.output_number", number=output_number, total=group[0].get("OutputCount", 1)))
        print(t("build.library", path=group[0]["LibraryPath"]))
        print()
        for summary in group:
            print(_build_summary_line(summary))
        print()
    print(t("build.files_errors", completed=completed, total=total, errors=errors))
    print()
    print(t("build.resume_hint"))
    write_sampler_log(f"Build completed status=INCOMPLETE manifests={len(summaries)}/{expected}", "INFO", "BUILD")


def invoke_new_build_from_preparation(
    preparation: dict[str, Any],
    *,
    required_ready_library_ids: Optional[Iterable[str]] = None,
    force_destructive_replace: bool = False,
) -> Optional[bool]:
    assert CURRENT_CONFIG is not None
    write_sampler_log(f"Build started mode={preparation['SelectionMode']} resume=False", "INFO", "BUILD")
    if not CURRENT_CONFIG["Outputs"]:
        write_status(t('output.none_configured'), 'WARNING', component='BUILD', log_message='No outputs are configured.')
        return False
    if not validate_selection_snapshot_sources(preparation):
        return False

    ready, unavailable = _combined_preflight(preparation)
    show_build_plan(preparation, ready, unavailable)

    if not ready:
        for state in unavailable:
            write_sampler_log(f"Preflight unavailable library={state.get('LibraryPath')} reason={state.get('Reason')}", "ERROR", "PREFLIGHT")
        write_status(t('build.no_output_preflight'), 'ERROR', component='BUILD', log_message='No configured output passed preflight.')
        return False

    required_ids = {str(x).casefold() for x in (required_ready_library_ids or [])}
    if required_ids:
        ready_ids = {str(entry["Output"]["LibraryId"]).casefold() for entry in ready}
        missing = required_ids - ready_ids
        if missing:
            write_status(t('build.replace_blocked'), 'ERROR', component='BUILD', log_message='The incomplete build cannot be safely replaced until every participating Output is available and passes preflight.')
            return False

    replace_required = force_destructive_replace or preparation_requires_replace(preparation, ready)
    if replace_required:
        confirmed = confirm_destructive_replace(str(preparation["SelectionMode"]))
    elif unavailable:
        print()
        print(f"[1] {t('build.continue_ready')}")
        print(f"[0] {t('common.back')}")
        print()
        build_choice = read_submenu_choice((0, 1), screen="BUILD_CONFIRMATION")
        confirmed = build_choice == 1
        if confirmed:
            write_sampler_log("Selection screen=BUILD_CONFIRMATION action=CONTINUE", "INFO", "UI")
    else:
        confirmed = confirm_continue_build()

    if not confirmed:
        write_sampler_log("Build cancelled.", "INFO", "BUILD")
        return None

    _stage_and_copy_new_build(preparation, ready)
    return True


def show_resume_summary(group: dict[str, Any], states: list[dict[str, Any]]) -> None:
    print()
    print(t("resume.outputs", count=len(states)))
    print()
    print(t("build.mode", mode=display_mode_name(str(group["SelectionMode"]))))
    print()
    for number, state in enumerate(states, 1):
        status = t("common.ready") if state["Status"] == "Ready" else t("common.not_available")
        print(t("resume.output", number=number))
        print("  " + t("resume.library_line", path=state["LibraryPath"]))
        print("  " + t("resume.status_line", status=status))
        if state.get("Reason"):
            print("  " + t("resume.reason", reason=_localize_build_reason(str(state["Reason"]))))
        for manifest in state.get("Manifests", []):
            selected = len(manifest["SelectedFiles"])
            completed = sum(1 for item in manifest["SelectedFiles"] if item["CopyStatus"] == "Completed" and destination_file_valid(get_manifest_item_destination_path(manifest, item), int(item["SourceLength"])))
            remaining = selected - completed
            print("  " + t("resume.counts", mode=display_mode_name(str(manifest["BuildMode"])), selected=selected, completed=completed, remaining=remaining))
        if state.get("RemainingBytes") is not None:
            print("  " + t("resume.remaining_size", size=format_byte_size(int(state["RemainingBytes"]))))
        print()


def invoke_resume_group(group: dict[str, Any], states: list[dict[str, Any]]) -> None:
    ready_states = [state for state in states if state["Status"] == "Ready"]
    if not ready_states:
        write_status(t('resume.none_available'), 'WARNING', component='BUILD', log_message='No participating output is currently available to resume.')
        return
    manifests: list[dict[str, Any]] = []
    for state in ready_states:
        manifests.extend(state["Manifests"])
    show_menu_screen("RESUME COPY")
    write_sampler_log(f"Resume started mode={group['SelectionMode']} manifests={len(manifests)}", "INFO", "BUILD")
    summaries = _copy_manifest_group(manifests)
    expected_total = len(group["BuildModes"]) * len(group["BuildLibraryIds"])
    show_build_final_summary(summaries, expected_total)


def invoke_build_resume_workflow(selection_mode: str) -> bool:
    if CURRENT_CONFIG is None:
        return True

    # A staged transaction is higher priority than any normal build/resume
    # decision. If it cannot be recovered safely, do not allow a new build to
    # overwrite or obscure that state.
    if not recover_staged_transactions(CURRENT_CONFIG, report_errors=True):
        return True

    all_incomplete = find_incomplete_build_groups(None)
    requested_modes = {"SDR", "HDR", "Uncertain"} if selection_mode == "ALL" else {selection_mode}
    conflicting = [
        group for group in all_incomplete
        if str(group["SelectionMode"]) != selection_mode
        and requested_modes.intersection(set(group["BuildModes"]))
    ]
    if conflicting:
        modes = ", ".join(sorted({str(group["SelectionMode"]) for group in conflicting}))
        write_status(t('resume.overlap_other', modes=modes), 'ERROR', component='BUILD', log_message=f'An incomplete build from another selection workflow overlaps this managed scope ({modes}). Resolve it through its Build / Resume menu before replacing this scope.')
        return True

    groups = [group for group in all_incomplete if str(group["SelectionMode"]) == selection_mode]
    if len(groups) > 1:
        write_status(t('resume.multiple_groups'), 'ERROR', component='BUILD', log_message='Multiple incomplete build groups were found for this selection mode. Automatic resume is blocked to avoid choosing the wrong build.')
        return True

    if groups:
        group = groups[0]
        states = _resume_group_states(group)
        show_resume_summary(group, states)
        print(f"[1] {t('menu.resume_copy')}")
        print(f"[2] {t('menu.start_new_selection')}")
        print(f"[0] {t('common.back')}")
        print()
        choice = read_submenu_choice((0, 1, 2), screen="RESUME_COPY")
        if choice == 0:
            return False
        if choice == 1:
            write_sampler_log("Selection screen=RESUME_COPY action=RESUME", "INFO", "UI")
            invoke_resume_group(group, states)
            return True
        write_sampler_log("Selection screen=RESUME_COPY action=START_NEW_SELECTION", "INFO", "UI")

        show_menu_screen("BUILD")
        preparation = invoke_selection_preparation(selection_mode, force_new_selection=True)
        if preparation is None:
            return True

        build_result = invoke_new_build_from_preparation(
            preparation,
            required_ready_library_ids=group["BuildLibraryIds"],
            force_destructive_replace=True,
        )
        return build_result is not None

    preparation = CURRENT_SELECTION_SNAPSHOTS.get(selection_mode)
    if preparation is not None:
        if not validate_selection_snapshot_sources(preparation):
            return True
    else:
        preparation = invoke_selection_preparation(selection_mode, force_new_selection=True)
        if preparation is None:
            return True

    build_result = invoke_new_build_from_preparation(preparation)
    return build_result is not None


def show_configured_source_roots() -> None:
    assert CURRENT_CONFIG is not None
    roots = CURRENT_CONFIG["SourceRoots"]
    print(t("source.list_heading", count=len(roots)))
    print()
    for root in roots:
        print(f"  {root['Path']}")
    print()


def invoke_manual_source_root_input() -> bool:
    assert CURRENT_CONFIG is not None
    show_menu_screen("ADD SOURCE")
    print(t("source.add.instructions"))
    print()
    paths: list[str] = []
    while True:
        value = ui_input(t("source.path_prompt"), "SOURCE_PATH")
        if not value.strip():
            break
        paths.append(remove_outer_quotes(value))
    if not paths:
        return False
    changed, review_shown = add_configured_source_roots(CURRENT_CONFIG, paths)
    if review_shown:
        wait_for_continue()
    return changed


def invoke_remove_source_root() -> bool:
    assert CURRENT_CONFIG is not None
    while True:
        show_menu_screen("REMOVE SOURCE")
        roots = list(CURRENT_CONFIG["SourceRoots"])
        if not roots:
            return False
        for index, root in enumerate(roots, 1):
            print(f"[{index}] {root['Path']}")
        print(f"[A] {t('menu.remove_all')}")
        print(f"[0] {t('common.back')}")
        print()
        value = ui_input(t("common.choice_back"), "REMOVE_SOURCE_MENU").strip()
        if not value or value == "0":
            write_sampler_log("Selection screen=REMOVE_SOURCE action=BACK", "INFO", "UI")
            return False
        if value.casefold() == "a":
            write_sampler_log("Selection screen=REMOVE_SOURCE action=REMOVE_ALL", "INFO", "UI")
            candidate = copy.deepcopy(CURRENT_CONFIG)
            candidate["SourceRoots"] = []
            if not save_runtime_config(candidate):
                return False
            reset_current_scan_state()
            return True
        try:
            choice = int(value)
        except ValueError:
            choice = 0
        if 1 <= choice <= len(roots):
            root = roots[choice - 1]
            write_sampler_log(f"Selection screen=REMOVE_SOURCE action=REMOVE path={root['Path']!r}", "INFO", "UI")
            remove_configured_source_root(CURRENT_CONFIG, str(root["RootId"]))
            continue
        write_status(t('common.invalid_selection'), 'WARNING', component='UI', log_message='Invalid selection')


def show_input_menu() -> None:
    while True:
        show_menu_screen("MEDIA SOURCES")
        show_configured_source_roots()
        print(f"[1] {t('menu.add_source')}")
        print(f"[2] {t('menu.remove_source')}")
        print(f"[3] {t('menu.import_sources')}")
        print(f"[4] {t('menu.export_sources')}")
        print()
        print(f"[0] {t('common.back')}")
        print()
        choice = read_submenu_choice((0, 1, 2, 3, 4), screen="MEDIA_SOURCES")
        if choice == 0:
            return
        actions = {1: "ADD_SOURCE", 2: "REMOVE_SOURCE", 3: "IMPORT_SOURCES", 4: "EXPORT_SOURCES"}
        write_sampler_log(f"Selection screen=MEDIA_SOURCES action={actions[choice]}", "INFO", "UI")
        if choice == 1:
            invoke_manual_source_root_input()
        elif choice == 2:
            invoke_remove_source_root()
        elif choice == 3:
            import_source_roots_from_text_file(CURRENT_CONFIG)
        else:
            export_source_roots_to_text_file(CURRENT_CONFIG)


def show_configured_outputs() -> None:
    assert CURRENT_CONFIG is not None
    outputs = CURRENT_CONFIG["Outputs"]
    print(t("output.list_heading", count=len(outputs)))
    print()
    for output in outputs:
        print(f"  {get_output_library_path(CURRENT_CONFIG, output)}")
    print()


def invoke_manual_output_input() -> bool:
    assert CURRENT_CONFIG is not None
    show_menu_screen("ADD OUTPUT")
    print(t("output.add.instructions"))
    print()
    paths: list[str] = []
    while True:
        value = ui_input(t("output.path_prompt"), "OUTPUT_PATH")
        if not value.strip():
            break
        paths.append(remove_outer_quotes(value))
    if not paths:
        return False
    added = add_configured_outputs(CURRENT_CONFIG, paths)
    if added:
        write_sampler_log(f"Outputs added: {added}", "INFO", "PATH")
        print(t("output.added", count=added))
        wait_for_continue()
    return bool(added)


def invoke_remove_output() -> bool:
    assert CURRENT_CONFIG is not None
    while True:
        show_menu_screen("REMOVE OUTPUT")
        outputs = list(CURRENT_CONFIG["Outputs"])
        if not outputs:
            print(t("output.none_configured"))
            print()
            print(f"[0] {t('common.back')}")
            print()
            read_submenu_choice((0,), screen="REMOVE_OUTPUT")
            return False
        for index, output in enumerate(outputs, 1):
            print(f"[{index}] {get_output_library_path(CURRENT_CONFIG, output)}")
        print(f"[A] {t('menu.remove_all')}")
        print(f"[0] {t('common.back')}")
        print()
        value = ui_input(t("common.choice_back"), "REMOVE_OUTPUT_MENU").strip()
        if not value or value == "0":
            write_sampler_log("Selection screen=REMOVE_OUTPUT action=BACK", "INFO", "UI")
            return False
        if value.casefold() == "a":
            write_sampler_log("Selection screen=REMOVE_OUTPUT action=REMOVE_ALL", "INFO", "UI")
            candidate = copy.deepcopy(CURRENT_CONFIG)
            candidate["Outputs"] = []
            if not save_runtime_config(candidate):
                return False
            continue
        try:
            choice = int(value)
        except ValueError:
            choice = 0
        if 1 <= choice <= len(outputs):
            output = outputs[choice - 1]
            write_sampler_log(f"Selection screen=REMOVE_OUTPUT action=REMOVE library={get_output_library_path(CURRENT_CONFIG, output)!r}", "INFO", "UI")
            remove_configured_output(CURRENT_CONFIG, str(output["LibraryId"]))
            continue
        write_status(t('common.invalid_selection'), 'WARNING', component='UI', log_message='Invalid selection')


def show_output_menu() -> None:
    while True:
        show_menu_screen("OUTPUTS")
        show_configured_outputs()
        print(f"[1] {t('menu.add_output')}")
        print(f"[2] {t('menu.remove_output')}")
        print()
        print(f"[0] {t('common.back')}")
        print()
        choice = read_submenu_choice((0, 1, 2), screen="OUTPUTS")
        if choice == 0:
            return
        action = "ADD_OUTPUT" if choice == 1 else "REMOVE_OUTPUT"
        write_sampler_log(f"Selection screen=OUTPUTS action={action}", "INFO", "UI")
        if choice == 1:
            invoke_manual_output_input()
        else:
            invoke_remove_output()


def show_settings_values() -> None:
    assert CURRENT_CONFIG is not None
    print(t('ffprobe.settings_workers', value=CURRENT_CONFIG['ProbeWorkers']))
    print(t('ffprobe.settings_timeout', value=CURRENT_CONFIG['FFprobeTimeoutSeconds']))
    print(t('ffprobe.settings_retries', value=CURRENT_CONFIG['ProbeRetries']))


def show_settings_menu() -> None:
    assert CURRENT_CONFIG is not None
    defaults = {
        1: ("Workers", "ProbeWorkers", 8, 1, 32),
        2: ("Timeout", "FFprobeTimeoutSeconds", 30, 1, 2_147_483_647),
        3: ("Retries", "ProbeRetries", 1, 0, 10),
    }
    while True:
        show_menu_screen("FFPROBE SETTINGS")
        show_settings_values()
        print(f"[0] {t('common.back')}")
        print()
        choice = read_submenu_choice((0, 1, 2, 3), screen="FFPROBE_SETTINGS")
        if choice == 0:
            return
        label, key, default, minimum, maximum = defaults[choice]
        write_sampler_log(f"Selection screen=FFPROBE_SETTINGS action=SET_{label.upper()}", "INFO", "UI")
        show_menu_screen("FFPROBE SETTINGS")
        current = int(CURRENT_CONFIG[key])
        display_label = t({"Workers": "config.label_workers", "Timeout": "config.label_timeout", "Retries": "config.label_retries"}[label])
        print(display_label)
        print()
        print(t("common.current_value_seconds", value=current) if label == "Timeout" else t("common.current_value", value=current))
        print()
        # ENTER keeps the current value (the factory default stays available by typing it).
        new = read_positive_integer(t("common.new_value"), min(max(current, minimum), maximum), minimum, maximum)
        candidate = copy.deepcopy(CURRENT_CONFIG)
        candidate[key] = new
        if save_runtime_config(candidate):
            write_sampler_log(f"Setting changed key={key} old={current} new={new}", "INFO", "CONFIG")


def show_language_menu() -> None:
    assert CURRENT_CONFIG is not None
    if localization_module.LOCALES is None:
        raise localization_module.LocaleError("Localization is not initialized.")
    while True:
        show_menu_screen("LANGUAGE")
        locales = localization_module.LOCALES.discover()
        print(t("language.current", name=localization_module.LOCALES.active_name))
        print()
        for index, item in enumerate(locales, 1):
            marker = t("language.active_marker") if item["Locale"] == localization_module.LOCALES.active_locale else ""
            print(f"[{index}] {item['Name']}{marker}")
        if len(locales) == 1:
            print()
            print(t("language.no_other"))
        print()
        print(f"[0] {t('common.back')}")
        print()
        choice = read_submenu_choice(tuple(range(0, len(locales) + 1)), screen="LANGUAGE")
        if choice == 0:
            return
        selected = locales[choice - 1]
        code = str(selected["Locale"])
        write_sampler_log(f"Selection screen=LANGUAGE action=SET_LOCALE locale={code}", "INFO", "UI")
        if code == str(CURRENT_CONFIG.get("Locale", localization_module.CANONICAL_LOCALE)):
            continue
        candidate = copy.deepcopy(CURRENT_CONFIG)
        candidate["Locale"] = code
        if save_runtime_config(candidate):
            localization_module.activate_configured_locale(code)


def show_configuration_menu() -> None:
    while True:
        show_menu_screen("CONFIGURATION")
        print(f"[1] {t('config.menu_media_sources')}")
        print(f"[2] {t('config.menu_outputs')}")
        print(f"[3] {t('config.menu_ffprobe')}")
        print(f"[4] {t('config.menu_language')}")
        print()
        print(f"[0] {t('common.back')}")
        print()
        choice = read_submenu_choice((0, 1, 2, 3, 4), screen="CONFIGURATION")
        if choice == 0:
            return
        actions = {1: "MEDIA_SOURCES", 2: "OUTPUTS", 3: "FFPROBE_SETTINGS", 4: "LANGUAGE"}
        write_sampler_log(f"Selection screen=CONFIGURATION action={actions[choice]}", "INFO", "UI")
        if choice == 1:
            show_input_menu()
        elif choice == 2:
            show_output_menu()
        elif choice == 3:
            show_settings_menu()
        else:
            show_language_menu()


def show_selection_menu(mode: str) -> None:
    screen = f"SELECTION_{mode.upper().replace(' ', '_')}"
    while True:
        show_menu_screen(f"Selection - {mode}")
        print(f"[1] {t('menu.preview')}")
        print(f"[2] {t('menu.build_resume')}")
        print()
        print(f"[0] {t('common.back')}")
        print()
        choice = read_submenu_choice((0, 1, 2), screen=screen)
        if choice == 0:
            return
        action = "PREVIEW" if choice == 1 else "BUILD_RESUME"
        write_sampler_log(f"Selection screen={screen} action={action}", "INFO", "UI")
        if choice == 1:
            invoke_preview_workflow(mode)
        else:
            show_menu_screen("BUILD")
            if invoke_build_resume_workflow(mode):
                wait_for_continue()


def show_selection_all_menu() -> None:
    show_selection_menu("ALL")


def show_main_menu() -> None:
    startup_notices = config_module.consume_startup_notices()
    if startup_notices:
        show_menu_screen("STARTUP RECOVERY")
        print(t("startup.recovery_completed"))
        print()
        for key, kwargs in startup_notices:
            print(t(key, **kwargs))
        wait_for_continue()
    actions = {1: "SCAN_MEDIA", 2: "SELECTION_ALL", 3: "SELECTION_SDR", 4: "SELECTION_HDR", 5: "SELECTION_UNCERTAIN", 6: "CONFIGURATION"}
    while True:
        show_menu_screen("MAIN MENU")
        print(f"[1] {t('menu.scan_media')}")
        print(f"[2] {t('menu.selection_all')}")
        print(f"[3] {t('menu.selection_sdr')}")
        print(f"[4] {t('menu.selection_hdr')}")
        print(f"[5] {t('menu.selection_uncertain')}")
        print(f"[6] {t('menu.configuration')}")
        print()
        print(f"[0] {t('common.exit')}")
        print()
        choice = read_main_menu_choice((0, 1, 2, 3, 4, 5, 6))
        if choice == -1:
            continue
        if choice == 0:
            write_sampler_log("Selection screen=MAIN_MENU action=EXIT", "INFO", "UI")
            return
        write_sampler_log(f"Selection screen=MAIN_MENU action={actions[choice]}", "INFO", "UI")
        if choice == 1:
            show_menu_screen("SCAN MEDIA")
            invoke_general_scan_workflow()
            wait_for_continue()
        elif choice == 2:
            show_selection_all_menu()
        elif choice == 3:
            show_selection_menu("SDR")
        elif choice == 4:
            show_selection_menu("HDR")
        elif choice == 5:
            show_selection_menu("Uncertain")
        else:
            show_configuration_menu()


# Lazy feature adapters: keep normal startup/Main Menu free of scan/selection/build imports.
def analyze_selection_diversity(classified_records: list[dict[str, Any]], selection_mode: str, *, live_preview: bool = False) -> list[dict[str, Any]]:
    from . import selection
    return selection.analyze_selection_diversity(classified_records, selection_mode, renderer=RENDERER, live_preview=live_preview)


def selection_default_count(context: dict[str, Any]) -> int:
    from . import selection
    return selection.selection_default_count(context)


def finalize_pool_selections(contexts: list[dict[str, Any]], requested: dict[str, int], *, quiet: bool = False) -> list[dict[str, Any]]:
    from . import selection
    return selection.finalize_pool_selections(contexts, requested, renderer=RENDERER, quiet=quiet)


def create_selection_preparation(selection_mode: str, pool_selections: list[dict[str, Any]], scan_result: dict[str, Any]) -> Optional[dict[str, Any]]:
    assert CURRENT_CONFIG is not None
    from . import build
    from . import selection
    preparation = build.create_selection_preparation(CURRENT_CONFIG, selection_mode, pool_selections, scan_result)
    if preparation is None:
        write_status(t("selection.none"), "WARNING", component="SELECTION", log_message="No files were selected.")
        return None
    CURRENT_SELECTION_SNAPSHOTS[selection_mode] = preparation
    selection.write_selection_details_to_log(pool_selections)
    return preparation


def preparation_total_bytes(preparation: dict[str, Any]) -> int:
    from . import build
    return build.preparation_total_bytes(preparation)


def format_byte_size(value: int) -> str:
    from . import build
    return build.format_byte_size(value)


def preflight_pool_rows(preparation: dict[str, Any]) -> list[tuple[str, int, int]]:
    from . import build
    return build.preflight_pool_rows(preparation)


def validate_selection_snapshot_sources(preparation: dict[str, Any]) -> bool:
    from . import build
    valid, _path = build.validate_selection_snapshot_sources(preparation)
    if not valid:
        CURRENT_SELECTION_SNAPSHOTS.pop(str(preparation["SelectionMode"]), None)
        write_status(t("preview.snapshot_invalid"), "WARNING", component="SELECTION", log_message="The selection snapshot is no longer valid because a selected source file is missing or changed. Create a new Preview/selection.")
    return valid


def preparation_requires_replace(preparation: dict[str, Any], ready_outputs: list[dict[str, Any]]) -> bool:
    from . import build
    return build.preparation_requires_replace(preparation, ready_outputs)


def _combined_preflight(preparation: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    assert CURRENT_CONFIG is not None
    from . import build
    return build.combined_preflight(CURRENT_CONFIG, preparation)


def recover_staged_transactions(config: dict[str, Any], *, report_errors: bool = False) -> bool:
    from . import build
    return build.recover_staged_transactions(config, report_errors=report_errors, renderer=RENDERER)


def _copy_manifest_group(manifests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from . import build
    return build.copy_manifest_group(manifests, RENDERER)


def _stage_and_copy_new_build(preparation: dict[str, Any], ready: list[dict[str, Any]]) -> None:
    from . import build
    result = build.stage_and_copy_new_build(preparation, ready, RENDERER)
    if not result["Success"]:
        write_status(t("build.commit_failed"), "ERROR", component="BUILD", log_message="The complete build plan could not be committed safely. No copy was started.")
        return
    show_build_final_summary(result["Summaries"], len(result["Manifests"]))


def find_incomplete_build_groups(selection_mode: Optional[str] = None) -> list[dict[str, Any]]:
    assert CURRENT_CONFIG is not None
    from . import build
    groups, schema2_modes = build.find_incomplete_build_groups(CURRENT_CONFIG, selection_mode)
    if schema2_modes:
        write_status(t("build.schema2_manifest"), "WARNING", component="BUILD", log_message="A schema-2 build manifest exists in this managed scope. It is not resumable in this version and may only be replaced by a new confirmed build.")
    return groups


def _resume_group_states(group: dict[str, Any]) -> list[dict[str, Any]]:
    assert CURRENT_CONFIG is not None
    from . import build
    return build.resume_group_states(CURRENT_CONFIG, group)


def destination_file_valid(path: str, expected_length: int) -> bool:
    from . import build
    return build.destination_file_valid(path, expected_length)


def get_manifest_item_destination_path(manifest: dict[str, Any], item: dict[str, Any]) -> str:
    from . import build
    return build.get_manifest_item_destination_path(manifest, item)


def selected_item_ux_lines(item: dict[str, Any]) -> list[str]:
    from . import build
    return build.selected_item_ux_lines(item)


def _compact_selected_item_lines(item: dict[str, Any], number: int) -> list[str]:
    from . import build
    return build._compact_selected_item_lines(item, number)


def get_output_library_path(config: dict[str, Any], output: dict[str, Any]) -> str:
    return paths_module.get_output_library_path(config, output)
