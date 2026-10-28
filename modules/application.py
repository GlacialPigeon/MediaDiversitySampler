# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import traceback
from typing import Any

from . import config as config_module
from . import localization as localization_module
from . import logger as logger_module
from . import ui
from .constants import (
    APP_NAME, APP_ROOT, CACHE_SCHEMA_VERSION, CONFIG_SCHEMA_VERSION, DATA_DIR, MANIFEST_SCHEMA_VERSION,
    MARKER_SCHEMA_VERSION, PROBE_SCHEMA_VERSION, APP_VERSION,
)
from .localization import CANONICAL_LOCALE, LocaleError, activate_configured_locale, initialize_localization, t
from .interrupts import set_interrupt_notice_callback
from .logger import format_elapsed, write_run_environment_header, write_sampler_log
from .platform_api import PlatformAdapter, configure_platform


def _bootstrap_locale_failure() -> int:
    print("ERROR: Default language file en-US.json is missing or invalid.")
    print()
    print("Please download Media Diversity Sampler again.")
    try:
        input("Press ENTER to exit.")
    except (EOFError, KeyboardInterrupt):
        pass
    return 1


def _write_startup_environment(config: dict[str, Any], platform_name: str) -> None:
    write_run_environment_header(
        config,
        platform_name=platform_name,
        config_schema_version=CONFIG_SCHEMA_VERSION,
        cache_schema_version=CACHE_SCHEMA_VERSION,
        probe_schema_version=PROBE_SCHEMA_VERSION,
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        marker_schema_version=MARKER_SCHEMA_VERSION,
        canonical_locale=CANONICAL_LOCALE,
    )


def run_application(platform: PlatformAdapter, *, platform_name: str) -> int:
    configure_platform(platform)
    platform.configure_console()
    instance_lock = platform.create_instance_lock(DATA_DIR / "application.lock")
    if not instance_lock.acquire():
        # Bootstrap exception: locale is intentionally not loaded yet.
        failure = getattr(instance_lock, "failure", None)
        if failure in (None, "busy"):
            print(f"{APP_NAME} is already running.")
            print("Only one instance can run at a time.")
        else:
            # The lock could not be created safely (for example an unexpected file or a symlink at the lock path).
            print(f"{APP_NAME} could not create its instance lock: {failure}")
            print(f"Lock file: {getattr(instance_lock, 'path', '?')}")
        return 1

    exit_status = "NORMAL"
    logger_ready = False
    try:
        try:
            initialize_localization()
        except LocaleError:
            return _bootstrap_locale_failure()

        logger_module.initialize_logger()
        logger_ready = True
        platform.set_log_callback(write_sampler_log)
        write_sampler_log(f"{APP_NAME} {APP_VERSION} starting. APP_ROOT={APP_ROOT}", "INFO", "STARTUP", force=True)

        locales = localization_module.LOCALES
        assert locales is not None
        write_sampler_log(
            f"Canonical validation passed path={locales.canonical_path} locale={CANONICAL_LOCALE} "
            f"meta_version={APP_VERSION} keys={len(locales.canonical)}",
            "INFO", "LOCALE", force=True,
        )

        config = config_module.load_config(status_callback=ui.write_status)
        ui.set_current_config(config)
        activate_configured_locale(str(config.get("Locale", CANONICAL_LOCALE)))
        set_interrupt_notice_callback(ui.show_graceful_interrupt_notice)
        _write_startup_environment(config, platform_name)

        # Frozen v1.2.0 performs staged-build recovery before Main Menu.
        ui.recover_staged_transactions(config, report_errors=True)

        active, ui_wait, wall = ui.session_timing()
        write_sampler_log(
            f"Startup completed active_elapsed={format_elapsed(active)} ui_wait={format_elapsed(ui_wait)} "
            f"wall_elapsed={format_elapsed(wall)}",
            "INFO", "STARTUP", force=True,
        )
        ui.show_main_menu()
        return 0

    except (KeyboardInterrupt, EOFError):
        exit_status = "INTERRUPTED"
        print()
        write_sampler_log("Program interrupted by user.", "INFO", "APP", force=True)
        return 130
    except Exception:
        exit_status = "ERROR"
        print()
        print(t("fatal.unexpected", app_name=APP_NAME))
        print(t("fatal.operation_failed"))
        print()
        details_written = write_sampler_log(
            f"Unhandled exception:\n{traceback.format_exc()}", "ERROR", "APP", force=True
        ) if logger_ready else False
        if details_written and logger_module.LOG_PATH is not None and logger_module.LOG_PATH.is_file():
            print(t("fatal.details_written"))
            print()
            print(t("common.log_path", path=logger_module.LOG_PATH))
        else:
            print(t("fatal.log_failed"))
        try:
            ui.ui_input(t("common.press_enter_exit"), "FATAL_EXIT_ACK")
        except (EOFError, KeyboardInterrupt):
            pass
        return 1
    finally:
        if logger_ready:
            try:
                active, ui_wait, wall = ui.session_timing()
                write_sampler_log(
                    f"End status={exit_status} session_id={logger_module.RUN_ID} active_elapsed={format_elapsed(active)} "
                    f"ui_wait={format_elapsed(ui_wait)} wall_elapsed={format_elapsed(wall)}",
                    "INFO", "SESSION", force=True,
                )
            except Exception:
                pass
        try:
            instance_lock.release()
        except Exception:
            pass
