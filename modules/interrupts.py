# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import os
import signal
import subprocess
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

from .logger import write_sampler_log

_ACTIVE_PROCESS_LOCK = threading.Lock()
_ACTIVE_PROCESSES: set[subprocess.Popen[Any]] = set()
_ACTIVE_STOP_CONTROLLER: Optional["GracefulStopController"] = None
_INTERRUPT_NOTICE_CALLBACK: Optional[Callable[[str], None]] = None



def set_interrupt_notice_callback(callback: Optional[Callable[[str], None]]) -> None:
    global _INTERRUPT_NOTICE_CALLBACK
    _INTERRUPT_NOTICE_CALLBACK = callback


def register_active_process(process: subprocess.Popen[Any]) -> None:
    with _ACTIVE_PROCESS_LOCK:
        _ACTIVE_PROCESSES.add(process)


def unregister_active_process(process: subprocess.Popen[Any]) -> None:
    with _ACTIVE_PROCESS_LOCK:
        _ACTIVE_PROCESSES.discard(process)


def kill_active_subprocesses() -> None:
    # Runs from the signal handler on the main thread, which may already hold this lock
    # (inside register/unregister_active_process). Never block on it: bounded wait, then best effort.
    acquired = _ACTIVE_PROCESS_LOCK.acquire(timeout=0.5)
    try:
        for _ in range(3):
            try:
                processes = list(_ACTIVE_PROCESSES)
                break
            except RuntimeError:  # set changed size during iteration (lock not held)
                processes = []
    finally:
        if acquired:
            _ACTIVE_PROCESS_LOCK.release()
    for process in processes:
        try:
            if process.poll() is None:
                process.kill()
        except Exception:
            pass


def _log_from_signal_handler(message: str, level: str, component: str, force: bool = False, wait: float = 0.0) -> None:
    """Log without ever blocking the signal handler.

    The handler runs on the main thread, which may be inside write_sampler_log() holding the
    (non-re-entrant) log lock. Writing from a helper thread lets the main thread finish and
    release the lock; `wait` bounds how long we give the line to reach the file (force exit).
    """
    try:
        worker = threading.Thread(target=write_sampler_log, args=(message, level, component, force), daemon=True)
        worker.start()
        if wait > 0:
            worker.join(wait)
    except Exception:
        pass


class GracefulStopController:
    def __init__(self, component: str) -> None:
        self.component = str(component).upper()
        self.stop_event = threading.Event()
        self.emergency_event = threading.Event()
        self._press_count = 0
        # Re-entrant: a second SIGINT can be delivered while the first handler is still running.
        self._lock = threading.RLock()
        self._installed = False
        self._old_handler: Any = None

    def install(self) -> None:
        if self._installed:
            return
        self._old_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self._handle)
        self._installed = True

    def restore(self) -> None:
        if self._installed:
            try:
                signal.signal(signal.SIGINT, self._old_handler)
            except Exception:
                pass
        self._installed = False

    def _handle(self, signum: int, frame: Any) -> None:
        with self._lock:
            self._press_count += 1
            count = self._press_count
        if count == 1:
            self.stop_event.set()
            callback = _INTERRUPT_NOTICE_CALLBACK
            if callback is not None:
                try:
                    callback(self.component)
                except Exception:
                    pass
            _log_from_signal_handler("Interrupt received count=1 action=GRACEFUL_STOP", "WARNING", self.component)
            try:
                signal.signal(signal.SIGINT, self._force_handle)
            except Exception:
                pass
            return
        self._force_handle(signum, frame)

    def _force_handle(self, signum: int, frame: Any) -> None:
        self.emergency_event.set()
        _log_from_signal_handler("Interrupt received count=2 action=FORCE_EXIT", "CRITICAL", self.component, force=True, wait=1.0)
        kill_active_subprocesses()
        os._exit(130)


def graceful_stop_requested() -> bool:
    return bool(_ACTIVE_STOP_CONTROLLER is not None and _ACTIVE_STOP_CONTROLLER.stop_event.is_set())


@contextmanager
def scoped_graceful_stop(component: str) -> Iterator[GracefulStopController]:
    global _ACTIVE_STOP_CONTROLLER
    if _ACTIVE_STOP_CONTROLLER is not None:
        yield _ACTIVE_STOP_CONTROLLER
        return
    controller = GracefulStopController(component)
    _ACTIVE_STOP_CONTROLLER = controller
    controller.install()
    try:
        yield controller
    finally:
        controller.restore()
        _ACTIVE_STOP_CONTROLLER = None
