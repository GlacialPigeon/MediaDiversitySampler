# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def fsync_directory(directory: Path) -> None:
    """Make a rename inside `directory` durable (POSIX). Best effort: no-op where directories
    cannot be opened (Windows) or the filesystem rejects fsync on them (some network shares)."""
    flag = getattr(os, "O_DIRECTORY", None)
    if flag is None:
        return
    try:
        fd = os.open(directory, os.O_RDONLY | flag)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_text_file_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".mediadiversity-write-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
        fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def write_json_file_atomically(path: Path, obj: Any) -> None:
    write_text_file_atomically(path, json.dumps(obj, ensure_ascii=False, indent=2))


def read_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as fh:
        return json.load(fh)
