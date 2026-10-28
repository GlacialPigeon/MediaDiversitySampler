# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import datetime as dt
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .constants import CACHE_SCHEMA_VERSION, PROBE_CACHE_DB_PATH, PROBE_CACHE_EXPORT_PATH, PROBE_SCHEMA_VERSION, APP_VERSION, RECOVERY_DIR
from .datetime_helpers import utc_now_o
from .file_io import write_json_file_atomically
from .logger import write_sampler_log
from .platform_api import relative_key



class ProbeCacheRecoveryError(RuntimeError):
    pass


class ProbeInfrastructureError(RuntimeError):
    pass


def is_confirmed_sqlite_corruption(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int) and (code & 0xFF) in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
        return True
    text = str(exc).casefold()
    markers = (
        "database disk image is malformed",
        "file is not a database",
        "malformed database schema",
        "database corruption",
    )
    return any(marker in text for marker in markers)


class ProbeCacheDB:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn: Optional[sqlite3.Connection] = None
        self.last_recovery: Optional[dict[str, Any]] = None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            return conn
        except Exception:
            conn.close()
            raise

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS probe_cache(
            storage_identity_key TEXT NOT NULL,
            storage_relative_key TEXT NOT NULL,
            path TEXT NOT NULL,
            length INTEGER NOT NULL,
            last_write_time_utc TEXT NOT NULL,
            probe_status TEXT NOT NULL,
            probe_error_kind TEXT,
            probe_error_message TEXT,
            probed_at_utc TEXT NOT NULL,
            probe_data_json TEXT,
            PRIMARY KEY(storage_identity_key,storage_relative_key)
        );
        CREATE INDEX IF NOT EXISTS idx_probe_cache_path ON probe_cache(path);
        """)
        conn.commit()

    def _metadata(self, key: str) -> Optional[str]:
        assert self.conn is not None
        row = self.conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def _set_metadata(self, key: str, value: Any) -> None:
        assert self.conn is not None
        self.conn.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def _close_without_commit(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None

    def _artifact_plan(self, kind: str) -> tuple[str, list[tuple[Path, Path]]]:
        RECOVERY_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        base = f"probe_cache_{kind}_{stamp}"
        sources = (
            (self.path, RECOVERY_DIR / f"{base}.db"),
            (Path(str(self.path) + "-wal"), RECOVERY_DIR / f"{base}.db-wal"),
            (Path(str(self.path) + "-shm"), RECOVERY_DIR / f"{base}.db-shm"),
            (PROBE_CACHE_EXPORT_PATH, RECOVERY_DIR / f"{base}.json"),
        )
        return stamp, [(source, target) for source, target in sources if source.exists()]

    def _preserve_cache_set(self, kind: str, reason: str) -> dict[str, Any]:
        self._close_without_commit()
        try:
            stamp, plan = self._artifact_plan(kind)
        except Exception as exc:
            raise ProbeCacheRecoveryError(f"Could not prepare cache recovery directory: {exc}") from exc

        copied: list[tuple[Path, Path]] = []
        try:
            for source, target in plan:
                shutil.copy2(source, target)
                copied.append((source, target))
        except Exception as exc:
            for _, target in copied:
                try:
                    target.unlink(missing_ok=True)
                except Exception:
                    pass
            raise ProbeCacheRecoveryError(f"Could not preserve the complete cache set: {exc}") from exc

        removed: list[tuple[Path, Path]] = []
        try:
            removal_order = sorted(copied, key=lambda pair: 1 if pair[0] == self.path else 0)
            for source, target in removal_order:
                source.unlink(missing_ok=True)
                removed.append((source, target))
        except Exception as exc:
            restore_errors: list[str] = []
            for source, target in removed:
                if source.exists() or not target.exists():
                    continue
                try:
                    shutil.copy2(target, source)
                except Exception as restore_exc:
                    restore_errors.append(f"{source}: {restore_exc}")
            detail = f"Could not move the preserved cache set safely: {exc}"
            if restore_errors:
                detail += "; restore errors=" + " | ".join(restore_errors)
            raise ProbeCacheRecoveryError(detail) from exc

        preserved = [str(target) for _, target in copied]
        for source, target in copied:
            write_sampler_log(
                f"Preserved cache artifact source={source} backup={target} reason={reason}",
                "WARNING", "CACHE", force=True,
            )
        return {"Kind": kind, "Stamp": stamp, "Reason": reason, "Preserved": preserved}

    def _create_fresh(self) -> None:
        self.conn = self._connect()
        self._create_schema(self.conn)
        self.conn.execute(f"PRAGMA user_version={int(CACHE_SCHEMA_VERSION)}")
        self._set_metadata("cache_schema_version", CACHE_SCHEMA_VERSION)
        self._set_metadata("probe_schema_version", PROBE_SCHEMA_VERSION)
        self._set_metadata("created_by_version", APP_VERSION)
        self._set_metadata("created_at_utc", utc_now_o())
        self._set_metadata("last_opened_by_version", APP_VERSION)
        self._set_metadata("last_opened_at_utc", utc_now_o())
        self.conn.commit()
        check = self.conn.execute("PRAGMA quick_check").fetchone()
        if not check or str(check[0]).casefold() != "ok":
            raise ProbeCacheRecoveryError(f"Replacement probe cache failed quick_check: {check[0] if check else 'NO_RESULT'}")

    def recover_confirmed_corruption(self, exc: BaseException) -> dict[str, Any]:
        reason = str(exc) or exc.__class__.__name__
        write_sampler_log(f"Confirmed probe cache corruption detected: {reason}", "ERROR", "CACHE", force=True)
        info = self._preserve_cache_set("invalid", reason)
        try:
            self._create_fresh()
        except Exception as create_exc:
            self._close_without_commit()
            raise ProbeCacheRecoveryError(f"Could not create a replacement probe cache: {create_exc}") from create_exc
        self.last_recovery = info
        write_sampler_log(f"Created replacement probe cache after corruption recovery path={self.path}", "WARNING", "CACHE", force=True)
        return info

    def _reset_incompatible(self, reason: str) -> None:
        write_sampler_log(f"Probe cache is incompatible and will be reset: {reason}", "WARNING", "CACHE", force=True)
        self._preserve_cache_set("incompatible", reason)
        try:
            self._create_fresh()
        except Exception as create_exc:
            self._close_without_commit()
            raise ProbeCacheRecoveryError(f"Could not create a replacement probe cache: {create_exc}") from create_exc
        write_sampler_log(f"Created replacement probe cache after compatibility reset path={self.path}", "WARNING", "CACHE", force=True)
        self.last_recovery = None

    def open(self) -> "ProbeCacheDB":
        try:
            self.conn = self._connect()
            self._create_schema(self.conn)
            check = self.conn.execute("PRAGMA quick_check").fetchone()
            if not check or str(check[0]).casefold() != "ok":
                raise sqlite3.DatabaseError(f"database disk image is malformed (quick_check={check[0] if check else 'NO_RESULT'})")
            cache_schema = self._metadata("cache_schema_version")
            probe_schema = self._metadata("probe_schema_version")
            if cache_schema is None:
                self.conn.execute(f"PRAGMA user_version={int(CACHE_SCHEMA_VERSION)}")
                self._set_metadata("cache_schema_version", CACHE_SCHEMA_VERSION)
                self._set_metadata("probe_schema_version", PROBE_SCHEMA_VERSION)
                self._set_metadata("created_by_version", APP_VERSION)
                self._set_metadata("created_at_utc", utc_now_o())
            elif int(cache_schema) != CACHE_SCHEMA_VERSION or int(probe_schema or -1) != PROBE_SCHEMA_VERSION:
                raise ValueError(f"Incompatible cache schema cache={cache_schema} probe={probe_schema}")
            self._set_metadata("last_opened_by_version", APP_VERSION)
            self._set_metadata("last_opened_at_utc", utc_now_o())
            self.conn.commit()
            return self
        except sqlite3.DatabaseError as exc:
            if is_confirmed_sqlite_corruption(exc):
                self.recover_confirmed_corruption(exc)
                return self
            self._close_without_commit()
            raise
        except (ValueError, TypeError) as exc:
            self._reset_incompatible(str(exc))
            return self
        except Exception:
            self._close_without_commit()
            raise

    def get_current_success(self, file: dict[str, Any]) -> Optional[dict[str, Any]]:
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT * FROM probe_cache WHERE storage_identity_key=? AND storage_relative_key=?",
            (str(file["StorageIdentityKey"]), relative_key(str(file["StorageRelativePath"]))),
        ).fetchone()
        if row is None or str(row["probe_status"]) != "Success" or int(row["length"]) != int(file["Length"]) or str(row["last_write_time_utc"]) != str(file["LastWriteTimeUtc"]):
            return None
        try:
            data = json.loads(str(row["probe_data_json"])) if row["probe_data_json"] else None
        except Exception:
            return None
        if not test_probe_data_shape(data):
            return None
        return {
            "Path": str(row["path"]), "Length": int(row["length"]), "LastWriteTimeUtc": str(row["last_write_time_utc"]),
            "ProbeStatus": "Success", "ProbeErrorKind": None, "ProbeErrorMessage": None,
            "ProbedAtUtc": str(row["probed_at_utc"]), "ProbeData": data,
        }

    def upsert(self, file: dict[str, Any], entry: dict[str, Any]) -> None:
        assert self.conn is not None
        data_json = json.dumps(entry.get("ProbeData"), ensure_ascii=False, separators=(",", ":")) if entry.get("ProbeData") is not None else None
        self.conn.execute("""INSERT INTO probe_cache(storage_identity_key,storage_relative_key,path,length,last_write_time_utc,probe_status,probe_error_kind,probe_error_message,probed_at_utc,probe_data_json)
        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(storage_identity_key,storage_relative_key) DO UPDATE SET path=excluded.path,length=excluded.length,last_write_time_utc=excluded.last_write_time_utc,probe_status=excluded.probe_status,probe_error_kind=excluded.probe_error_kind,probe_error_message=excluded.probe_error_message,probed_at_utc=excluded.probed_at_utc,probe_data_json=excluded.probe_data_json""", (
            str(file["StorageIdentityKey"]), relative_key(str(file["StorageRelativePath"])), str(entry["Path"]), int(entry["Length"]),
            str(entry["LastWriteTimeUtc"]), str(entry["ProbeStatus"]), entry.get("ProbeErrorKind"), entry.get("ProbeErrorMessage"),
            str(entry.get("ProbedAtUtc") or utc_now_o()), data_json,
        ))

    def commit(self) -> None:
        if self.conn is not None:
            self._set_metadata("updated_at_utc", utc_now_o())
            self.conn.commit()

    def export_json(self) -> None:
        assert self.conn is not None
        rows = self.conn.execute("SELECT * FROM probe_cache ORDER BY path COLLATE NOCASE").fetchall()
        entries = []
        for row in rows:
            try:
                data = json.loads(str(row["probe_data_json"])) if row["probe_data_json"] else None
            except Exception:
                data = None
            entries.append({
                "StorageIdentityKey": str(row["storage_identity_key"]), "StorageRelativePath": str(row["storage_relative_key"]),
                "Path": str(row["path"]), "Length": int(row["length"]), "LastWriteTimeUtc": str(row["last_write_time_utc"]),
                "ProbeStatus": str(row["probe_status"]), "ProbeErrorKind": row["probe_error_kind"],
                "ProbeErrorMessage": row["probe_error_message"], "ProbedAtUtc": str(row["probed_at_utc"]), "ProbeData": data,
            })
        payload = {
            "ProgramVersion": APP_VERSION, "CacheSchemaVersion": CACHE_SCHEMA_VERSION, "ProbeSchemaVersion": PROBE_SCHEMA_VERSION,
            "ExportedAtUtc": utc_now_o(), "EntryCount": len(entries), "Entries": entries,
        }
        write_json_file_atomically(PROBE_CACHE_EXPORT_PATH, payload)
        write_sampler_log(f"Probe cache JSON export completed entries={len(entries)} path={PROBE_CACHE_EXPORT_PATH}", "INFO", "CACHE")

    def close(self, *, commit: bool = True) -> None:
        if self.conn is not None:
            try:
                if commit:
                    self.commit()
            finally:
                self.conn.close()
                self.conn = None


def test_probe_data_shape(data: Any) -> bool:
    if not isinstance(data, dict) or not isinstance(data.get("Stream"), dict) or not isinstance(data.get("Format"), dict):
        return False
    stream_names = ["Index","CodecName","CodecLongName","CodecTag","CodecTagString","Profile","Level","Width","Height","CodedWidth","CodedHeight","PixelFormat","BitsPerRawSample","AverageFrameRate","RealFrameRate","SampleAspectRatio","DisplayAspectRatio","FieldOrder","Duration","BitRate","ColorRange","ColorSpace","ColorTransfer","ColorPrimaries","DispositionDefault","DispositionAttachedPicture","Tags","SideDataList"]
    format_names = ["FormatName","FormatLongName","Duration","Size","BitRate","Tags"]
    return all(n in data["Stream"] for n in stream_names) and all(n in data["Format"] for n in format_names) and isinstance(data["Stream"]["SideDataList"], list)


def load_probe_cache() -> ProbeCacheDB:
    return ProbeCacheDB(PROBE_CACHE_DB_PATH).open()
