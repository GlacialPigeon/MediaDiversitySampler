# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import fcntl
import hashlib
import os
import posixpath
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Optional, Sequence


_NETWORK_FILESYSTEMS = {"cifs", "smb3", "nfs", "nfs4"}
_AUTOFS_FILESYSTEMS = {"autofs"}


@dataclass(frozen=True)
class _MountInfo:
    mount_id: int
    parent_id: int
    major_minor: str
    root: str
    mount_point: str
    filesystem: str
    source: str
    mount_options: str
    super_options: str


_TRUSTED_LINK_UID = 0  # symbolic links owned by this uid (root) are treated as part of the system layout


def _trusted_system_link(path: str) -> bool:
    """True for an administrator-managed symlink such as /home -> /var/home or /media -> /run/media.

    It must be owned by root and sit in a root-owned directory that nobody else can write to, so an
    ordinary user cannot create or swap it. Links created by users stay rejected by the safety rules.
    """
    try:
        info = os.lstat(path)
        if not stat.S_ISLNK(info.st_mode) or info.st_uid != _TRUSTED_LINK_UID:
            return False
        parent = os.lstat(posixpath.dirname(path) or "/")
        return parent.st_uid == _TRUSTED_LINK_UID and (parent.st_mode & 0o022) == 0
    except OSError:
        return False


class _LinuxInstanceLock:
    def __init__(self) -> None:
        uid = getattr(os, "getuid", lambda: 0)()
        self.uid = uid
        self.path = self._lock_directory(uid) / f"MediaDiversitySampler_{uid}.lock"
        self.handle: Optional[Any] = None
        self.failure: Optional[str] = None  # "busy" = another instance runs; otherwise a short error text

    @staticmethod
    def _lock_directory(uid: int) -> Path:
        # Prefer the per-user runtime directory (private, mode 0700); fall back to the shared temp dir.
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime:
            try:
                st = os.stat(runtime)
                if os.path.isdir(runtime) and st.st_uid == uid and (st.st_mode & 0o022) == 0:
                    return Path(runtime)
            except OSError:
                pass
        return Path(tempfile.gettempdir())

    def acquire(self) -> bool:
        handle: Optional[Any] = None
        fd: Optional[int] = None
        try:
            # O_NOFOLLOW: never follow a pre-planted symlink, so another user cannot make us
            # truncate and overwrite an arbitrary file through the predictable lock name.
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != self.uid:
                raise OSError("Instance lock file is not a regular file owned by the current user.")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle = os.fdopen(fd, "r+", encoding="utf-8")
            fd = None
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
            self.handle = handle
            return True
        except Exception as exc:
            # BlockingIOError means another instance holds the lock; any other error fails closed.
            self.failure = "busy" if isinstance(exc, BlockingIOError) else f"{type(exc).__name__}: {exc}"
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
            elif fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
            return False

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self.handle.close()
        except Exception:
            pass
        self.handle = None


def _unescape_mountinfo(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        try:
            return chr(int(match.group(1), 8))
        except Exception:
            return match.group(0)

    return re.sub(r"\\([0-7]{3})", repl, value)


def _read_mountinfo() -> list[_MountInfo]:
    mounts: list[_MountInfo] = []
    try:
        lines = Path("/proc/self/mountinfo").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except Exception:
        return mounts

    for line in lines:
        try:
            left, right = line.split(" - ", 1)
            left_fields = left.split()
            right_fields = right.split()
            if len(left_fields) < 6 or len(right_fields) < 3:
                continue
            mounts.append(
                _MountInfo(
                    mount_id=int(left_fields[0]),
                    parent_id=int(left_fields[1]),
                    major_minor=left_fields[2],
                    root=_unescape_mountinfo(left_fields[3]),
                    mount_point=_unescape_mountinfo(left_fields[4]),
                    mount_options=left_fields[5],
                    filesystem=right_fields[0],
                    source=_unescape_mountinfo(right_fields[1]),
                    super_options=" ".join(right_fields[2:]),
                )
            )
        except Exception:
            continue
    return mounts


def _is_path_within_mount(path: str, mount_point: str) -> bool:
    try:
        path_norm = posixpath.normpath(path)
        mount_norm = posixpath.normpath(mount_point)
        return (
            path_norm == mount_norm
            or posixpath.commonpath([path_norm, mount_norm]) == mount_norm
        )
    except Exception:
        return False


def _find_mount_for_path(
    path: str,
    mounts: Optional[Sequence[_MountInfo]] = None,
) -> Optional[_MountInfo]:
    candidates = mounts if mounts is not None else _read_mountinfo()
    matching = [
        mount for mount in candidates
        if _is_path_within_mount(path, mount.mount_point)
    ]
    if not matching:
        return None

    # systemd autofs may expose an autofs trigger and its backing filesystem at
    # the same mount point. Prefer the real backing filesystem.
    return max(
        matching,
        key=lambda item: (
            len(posixpath.normpath(item.mount_point)),
            item.filesystem.casefold() not in _AUTOFS_FILESYSTEMS,
            item.mount_id,
        ),
    )


def _directory_is_enumerable(path: str) -> bool:
    try:
        with os.scandir(path):
            return True
    except OSError:
        return False


def _resolve_backing_mount_for_path(
    path: str,
    *,
    trigger_automount: bool,
) -> Optional[_MountInfo]:
    mounts = _read_mountinfo()
    mount = _find_mount_for_path(path, mounts)
    if mount is None:
        return None

    if mount.filesystem.casefold() not in _AUTOFS_FILESYSTEMS:
        return mount

    if trigger_automount:
        _directory_is_enumerable(path)
        mounts = _read_mountinfo()
        mount = _find_mount_for_path(path, mounts)
        if mount is not None and mount.filesystem.casefold() not in _AUTOFS_FILESYSTEMS:
            return mount

    # autofs/systemd-1 is only a trigger layer and is not a stable storage
    # identity on its own.
    return None


def _device_link_name(device_source: str, directory: str) -> Optional[str]:
    if not device_source.startswith("/dev/"):
        return None
    root = Path(directory)
    try:
        target = os.path.realpath(device_source)
        if not root.is_dir():
            return None
        for entry in root.iterdir():
            try:
                if os.path.realpath(str(entry)) == target:
                    return entry.name
            except Exception:
                continue
    except Exception:
        return None
    return None


def _device_uuid(device_source: str) -> Optional[str]:
    return _device_link_name(device_source, "/dev/disk/by-uuid")


def _device_partuuid(device_source: str) -> Optional[str]:
    return _device_link_name(device_source, "/dev/disk/by-partuuid")


def _normalized_network_source(filesystem: str, source: str) -> str:
    fs = filesystem.casefold()
    text = source.strip()
    if fs in {"cifs", "smb3"}:
        text = text.replace("\\", "/")
        while text.startswith("////"):
            text = text[2:]
        return text.casefold()
    return text


def _filesystem_root(mount: _MountInfo) -> str:
    """Directory of the filesystem that is mounted (mountinfo 'root'); not '/' for bind mounts of a subdirectory."""
    return posixpath.normpath(mount.root or "/")


def _filesystem_relative_path(mount: _MountInfo, relative_to_mount_point: str) -> str:
    """Path relative to the root of the underlying filesystem.

    Two mounts of the same filesystem (a normal mount and a bind mount of one of its subdirectories)
    then give the same identity key and the same relative path for the same file, so overlap and
    duplicate detection work across them.
    """
    root = _filesystem_root(mount).strip("/")
    return posixpath.join(root, relative_to_mount_point).strip("/") if root else relative_to_mount_point


def _nfs_case_suffix(source: str) -> str:
    # The core compares identity keys case-insensitively (right for SMB and Windows). NFS export paths are
    # case-sensitive, so keep /Export and /export distinguishable; all-lower-case sources keep the plain key.
    if source == source.casefold():
        return ""
    return "|CASE:" + hashlib.sha1(source.encode("utf-8", "surrogateescape")).hexdigest()[:10]


def _identity_for_mount(
    mount: _MountInfo,
) -> tuple[str, str, Optional[str], Optional[str]]:
    filesystem = mount.filesystem.casefold()
    # The key keeps the historical '|ROOT:/' token so keys saved in existing configurations stay valid.
    # The real filesystem root (bind mounts) is carried in the relative path, see _filesystem_relative_path().

    if filesystem in {"cifs", "smb3"}:
        source = _normalized_network_source(filesystem, mount.source)
        return (
            "LINUX_CIFS",
            f"LINUX_CIFS:{source}|ROOT:/",
            None,
            None,
        )

    if filesystem in {"nfs", "nfs4"}:
        source = _normalized_network_source(filesystem, mount.source)
        return (
            "LINUX_NFS",
            f"LINUX_NFS:{source}{_nfs_case_suffix(source)}|ROOT:/",
            None,
            None,
        )

    volume_uuid = _device_uuid(mount.source)
    if volume_uuid:
        return (
            "LINUX_LOCAL",
            f"LINUX_LOCAL:UUID:{volume_uuid.casefold()}|ROOT:/",
            volume_uuid,
            mount.major_minor,
        )

    part_uuid = _device_partuuid(mount.source)
    if part_uuid:
        return (
            "LINUX_LOCAL",
            f"LINUX_LOCAL:PARTUUID:{part_uuid.casefold()}|ROOT:/",
            part_uuid,
            mount.major_minor,
        )

    source_key = mount.source if mount.source else mount.major_minor
    return (
        "LINUX_LOCAL",
        f"LINUX_LOCAL:DEV:{source_key}|MM:{mount.major_minor}|ROOT:/",
        None,
        mount.major_minor,
    )


def _mount_relative_path(path: str, mount_point: str) -> str:
    try:
        relative = posixpath.relpath(path, mount_point)
        return "" if relative == "." else relative.strip("/")
    except Exception:
        return ""


def _network_identity_type(identity_type: str) -> bool:
    return identity_type in {"LINUX_CIFS", "LINUX_NFS"}


class LinuxPlatform:
    def __init__(
        self,
        log_callback: Optional[Callable[[str, str, str], Any]] = None,
    ) -> None:
        self._log_callback = log_callback

    def set_log_callback(
        self,
        callback: Optional[Callable[[str, str, str], Any]],
    ) -> None:
        self._log_callback = callback

    def _log(
        self,
        message: str,
        level: str = "INFO",
        component: str = "PATH",
    ) -> None:
        if self._log_callback is not None:
            try:
                self._log_callback(message, level, component)
            except Exception:
                pass

    def configure_console(self) -> None:
        for stream_name in ("stdout", "stderr"):
            stream = getattr(sys, stream_name, None)
            try:
                if stream is not None and hasattr(stream, "reconfigure"):
                    stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    def clear_screen(self) -> None:
        if not sys.stdout.isatty():
            return
        try:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
        except Exception:
            pass

    def create_instance_lock(self, path: Path) -> _LinuxInstanceLock:
        # The Linux lock is per user (private runtime directory) and ignores the data-folder path.
        return _LinuxInstanceLock()

    def ffprobe_path(self, app_root: Path) -> Path:
        bundled = app_root / "ffmpeg" / "bin" / "ffprobe"
        if bundled.is_file():
            return bundled
        system_ffprobe = shutil.which("ffprobe")
        if system_ffprobe:
            return Path(system_ffprobe)
        return bundled

    def subprocess_process_options(self) -> dict[str, Any]:
        return {}

    def default_excluded_directories(self) -> set[str]:
        # Names are matched case-insensitively. Besides local trash, skip NAS metadata/recycle folders
        # (Synology @eaDir/#recycle/#snapshot, QNAP @Recycle/.@__thumb) and NTFS system folders seen on shares.
        excluded = {
            "lost+found", ".trash",
            "@eadir", "#recycle", "#snapshot", "@recycle", ".@__thumb",
            "$recycle.bin", "system volume information",
        }
        try:
            excluded.add(f".trash-{os.getuid()}".casefold())
        except Exception:
            pass
        return excluded

    def remove_outer_quotes(self, value: str | None) -> str:
        if value is None:
            return ""
        text = value.strip()
        pairs = {'"': '"', "'": "'", "\u201c": "\u201d", "\u2018": "\u2019"}
        while len(text) >= 2 and text[0] in pairs and text[-1] == pairs[text[0]]:
            text = text[1:-1].strip()
        return text

    def path_key(self, value: str) -> str:
        text = self.remove_outer_quotes(str(value))
        text = os.path.expanduser(text)
        return posixpath.normpath(text)

    def relative_key(self, value: str) -> str:
        text = str(value).strip("/")
        if not text:
            return ""
        normalized = posixpath.normpath(text)
        return "" if normalized == "." else normalized

    @staticmethod
    def _relative_parts(value: str) -> tuple[str, ...]:
        text = str(value).strip("/")
        if not text:
            return ()
        return tuple(
            part for part in PurePosixPath(text).parts
            if part not in ("/", "", ".")
        )

    def relative_is_ancestor(
        self,
        parent: str,
        child: str,
        allow_equal: bool = False,
    ) -> bool:
        p = self._relative_parts(parent)
        c = self._relative_parts(child)
        return (
            (len(p) <= len(c) if allow_equal else len(p) < len(c))
            and c[: len(p)] == p
        )

    def normalize_path(self, path: str) -> str:
        value = self.remove_outer_quotes(path)
        if not value.strip():
            raise ValueError("Path is empty.")
        value = os.path.expanduser(value)
        if not posixpath.isabs(value):
            raise ValueError(f"Path must be absolute: {value}")
        normalized = posixpath.abspath(posixpath.normpath(value))
        return "/" if normalized == "/" else normalized.rstrip("/")

    def resolve_storage(
        self,
        path: str,
        require_directory: bool = False,
    ) -> dict[str, Any]:
        original = self.remove_outer_quotes(path)
        normalized = self.normalize_path(original)
        if require_directory and not os.path.isdir(normalized):
            raise FileNotFoundError(normalized)

        # Resolve symlinks only for storage/mount identity. Keep Path itself as
        # the normalized user path so the core symlink-safety checks still see
        # and reject linked source/output paths when appropriate.
        identity_path = posixpath.normpath(os.path.realpath(normalized))
        mount = _resolve_backing_mount_for_path(identity_path, trigger_automount=True)
        if mount is None:
            return {
                "UserPath": original,
                "Path": normalized,
                "StorageIdentityType": "PATH_FALLBACK",
                "StorageIdentityKey": "PATH:" + self.path_key(normalized),
                "StorageRoot": normalized,
                "StorageRelativePath": "",
                "RemoteVolumeSerial": None,
                "RemoteFileId": None,
                "VolumeGuid": None,
            }

        identity_type, identity_key, volume_guid, major_minor = _identity_for_mount(mount)
        mount_point = posixpath.normpath(mount.mount_point)
        relative = _filesystem_relative_path(mount, _mount_relative_path(identity_path, mount_point))
        self._log(
            f"Source resolve method={identity_type} input={normalized!r} "
            f"target={identity_path!r} mount={mount_point!r} "
            f"filesystem={mount.filesystem!r} device={mount.source!r} "
            f"identity_key={identity_key!r}",
            "INFO",
            "PATH",
        )
        return {
            "UserPath": original,
            "Path": normalized,
            "StorageIdentityType": identity_type,
            "StorageIdentityKey": identity_key,
            "StorageRoot": mount_point,
            "StorageRelativePath": relative,
            "RemoteVolumeSerial": None,
            "RemoteFileId": major_minor,
            "VolumeGuid": volume_guid,
        }

    def canonical_existing_path(self, path: str) -> str:
        return str(self.resolve_storage(path, True)["Path"])

    def paths_equal(self, a: str, b: str) -> bool:
        try:
            na = self.normalize_path(a)
            nb = self.normalize_path(b)
            if os.path.exists(na) and os.path.exists(nb):
                try:
                    return os.path.samefile(na, nb)
                except OSError:
                    pass
            return na == nb
        except Exception:
            return False

    def is_path_ancestor(
        self,
        child: str,
        parent: str,
        allow_equal: bool = False,
    ) -> bool:
        c = self.normalize_path(child)
        p = self.normalize_path(parent)
        if self.paths_equal(c, p):
            return allow_equal
        try:
            return posixpath.commonpath([c, p]) == p
        except ValueError:
            return False

    def storage_relative_path(self, full_path: str, root_path: str) -> str:
        full = self.normalize_path(full_path)
        root = self.normalize_path(root_path)
        if self.paths_equal(full, root):
            return ""
        if not self.is_path_ancestor(full, root):
            raise ValueError(f"Path is not inside root: {full_path}")
        return posixpath.relpath(full, root).lstrip("/")

    def leaf_label(self, path: str) -> str:
        normalized = self.normalize_path(path)
        leaf = posixpath.basename(normalized.rstrip("/"))
        return leaf or "Root"

    @staticmethod
    def is_link_or_reparse_point(path: str) -> bool:
        try:
            return os.path.islink(path)
        except OSError:
            return False

    def no_link_ancestors(self, path: str) -> bool:
        current = self.normalize_path(path)
        while current:
            if os.path.lexists(current) and self.is_link_or_reparse_point(current) and not _trusted_system_link(current):
                return False
            parent = posixpath.dirname(current.rstrip("/")) or "/"
            if parent == current:
                break
            current = parent
        return True

    def path_parts(self, path: str) -> tuple[str, ...]:
        return tuple(
            part for part in PurePosixPath(str(path)).parts
            if part not in ("/", "")
        )

    def relative_parts(self, value: str) -> tuple[str, ...]:
        text = str(value).strip("/")
        if not text:
            return ()
        return tuple(
            part for part in PurePosixPath(text).parts
            if part not in ("/", "", ".")
        )

    def ensure_trailing_separator(self, path: str) -> str:
        text = str(path)
        return text if not text or text.endswith("/") else text + "/"

    def valid_managed_relative_path(self, path: str) -> bool:
        text = str(path)
        if not text.strip() or posixpath.isabs(text) or "\x00" in text:
            return False
        parts = PurePosixPath(text).parts
        if any(part in ("..",) for part in parts):
            return False
        normalized = posixpath.normpath(text)
        return normalized not in ("", ".", "..") and not normalized.startswith("../")

    def source_review_alias(self, raw: str) -> tuple[str, str]:
        value = self.remove_outer_quotes(str(raw))
        value = os.path.expanduser(value)
        if posixpath.isabs(value):
            value = posixpath.normpath(value)
        return "Path", value

    @staticmethod
    def source_alias_group_key(item: dict[str, Any]) -> str | None:
        if str(item.get("StorageIdentityType")) not in {"LINUX_CIFS", "LINUX_NFS"}:
            return None
        key = str(item.get("StorageIdentityKey") or "").casefold()
        return key or None

    @staticmethod
    def source_alias_render_spec(kind: str) -> tuple[int, str]:
        return {
            "Path": (0, "source.review.alias_path"),
            "Drive": (1, "source.review.alias_drive"),
            "Shortcut": (2, "source.review.alias_shortcut"),
        }.get(str(kind), (9, "source.review.alias_path"))

    def adopt_storage_identity(
        self,
        desc: dict[str, Any],
        existing_items: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        # Linux mount identity is already based on the backing mount source plus
        # mount root (CIFS/NFS), or UUID/PARTUUID/device identity for local
        # filesystems. Unlike Windows UNC handling, no additional identity
        # adoption is needed here.
        return desc

    @staticmethod
    def descriptor_persistence_fields(desc: dict[str, Any]) -> dict[str, Any]:
        return {
            "RemoteVolumeSerial": desc.get("RemoteVolumeSerial"),
            "RemoteFileId": desc.get("RemoteFileId"),
            "VolumeGuid": desc.get("VolumeGuid"),
        }

    @staticmethod
    def empty_descriptor_persistence_fields() -> dict[str, Any]:
        return {
            "RemoteVolumeSerial": None,
            "RemoteFileId": None,
            "VolumeGuid": None,
        }

    @staticmethod
    def link_violation_message(code: str, path: str | None = None) -> str:
        messages = {
            "source_traverses": "Media source traverses a symbolic link: {path}",
            "skipped_link": "Skipped symbolic link: {path}",
            "output_library_traverses": "Output library traverses a symbolic link.",
            "output_library_root": "Output library root is a symbolic link.",
            "marker": "Marker is a symbolic link.",
            "output_path_traverses": "Output path traverses a symbolic link.",
            "mode_path_traverses": "Mode path traverses a symbolic link.",
            "mode_folder": "Mode folder is a symbolic link.",
            "mode_tree": "Mode tree contains a symbolic link; replacement is blocked.",
            "managed_artifact": "Managed artifact is a symbolic link.",
            "managed_library": "Managed library contains a symbolic link: {path}",
            "destination_file": "destination file is a symbolic link",
        }
        template = messages.get(code)
        if template is None:
            raise KeyError(code)
        return template.format(path=path or "")

    @staticmethod
    def platform_reason_locale_key(reason: str) -> str | None:
        return {
            "Output library root is a symbolic link.": "build.reason.output_library_reparse",
            "Marker is a symbolic link.": "build.reason.marker_reparse",
            "Mode folder is a symbolic link.": "build.reason.mode_reparse",
        }.get(str(reason))

    @staticmethod
    def valid_filename_component(text: str) -> bool:
        value = str(text)
        if not value or value in {".", ".."}:
            return False
        return "/" not in value and "\x00" not in value

    @staticmethod
    def join_path(*parts: str) -> str:
        return posixpath.join(*parts)

    @staticmethod
    def basename(path: str) -> str:
        return posixpath.basename(path)

    @staticmethod
    def dirname(path: str) -> str:
        return posixpath.dirname(path)

    @staticmethod
    def splitext(path: str) -> tuple[str, str]:
        return posixpath.splitext(path)

    @staticmethod
    def normpath(path: str) -> str:
        return posixpath.normpath(path)

    @staticmethod
    def normcase(path: str) -> str:
        return posixpath.normcase(path)

    @staticmethod
    def isabs(path: str) -> bool:
        return posixpath.isabs(path)
