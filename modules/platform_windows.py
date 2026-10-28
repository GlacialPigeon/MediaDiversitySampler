# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import ctypes
import ntpath
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .constants import FFMPEG_BIN_DIR

_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime_dwLowDateTime", ctypes.c_uint32),
        ("ftCreationTime_dwHighDateTime", ctypes.c_uint32),
        ("ftLastAccessTime_dwLowDateTime", ctypes.c_uint32),
        ("ftLastAccessTime_dwHighDateTime", ctypes.c_uint32),
        ("ftLastWriteTime_dwLowDateTime", ctypes.c_uint32),
        ("ftLastWriteTime_dwHighDateTime", ctypes.c_uint32),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8),
    ]


class _GlobalInstanceLock:
    ERROR_ALREADY_EXISTS = 183

    def __init__(self) -> None:
        self.handle: Any = None

    def acquire(self) -> bool:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, "Global\\MediaDiversitySampler_GlobalInstance")
        if not handle:
            return False
        if ctypes.get_last_error() == self.ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle:
            try:
                ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self.handle)
            except Exception:
                pass
        self.handle = None


class WindowsPlatform:
    def __init__(self, log_callback: Optional[Callable[[str, str, str], Any]] = None) -> None:
        self._log_callback = log_callback

    def set_log_callback(self, callback: Optional[Callable[[str, str, str], Any]]) -> None:
        self._log_callback = callback

    def _log(self, message: str, level: str = "INFO", component: str = "PATH") -> None:
        if self._log_callback is not None:
            try:
                self._log_callback(message, level, component)
            except Exception:
                pass

    def configure_console(self) -> None:
        if not sys.stdout.isatty():
            return
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass

    def clear_screen(self) -> None:
        os.system("cls")

    def create_instance_lock(self, path: Path) -> _GlobalInstanceLock:
        # The Windows lock is one machine-wide named mutex and ignores the data-folder path.
        return _GlobalInstanceLock()

    def ffprobe_path(self, app_root: Path) -> Path:
        return FFMPEG_BIN_DIR / "ffprobe.exe"

    def subprocess_process_options(self) -> dict[str, Any]:
        return {"creationflags": subprocess.CREATE_NO_WINDOW}

    def default_excluded_directories(self) -> set[str]:
        # Also skip Synology/QNAP metadata and recycle folders that appear on mapped/UNC shares.
        return {"$recycle.bin", "system volume information", "@eadir", "#recycle", "#snapshot", "@recycle", ".@__thumb"}

    def remove_outer_quotes(self, value: str | None) -> str:
        if value is None:
            return ""
        text = value.strip()
        pairs = {'"': '"', "'": "'", "\u201c": "\u201d", "\u2018": "\u2019"}
        while len(text) >= 2 and text[0] in pairs and text[-1] == pairs[text[0]]:
            text = text[1:-1].strip()
        return text

    def path_key(self, value: str) -> str:
        return ntpath.normpath(str(value).replace("/", "\\")).casefold()

    def relative_key(self, value: str) -> str:
        value = str(value).replace("/", "\\").strip("\\")
        return "" if not value else ntpath.normpath(value).casefold()

    @staticmethod
    def _relative_parts(value: str) -> tuple[str, ...]:
        value = str(value).replace("/", "\\").strip("\\")
        return tuple(part.casefold() for part in value.split("\\") if part and part != ".")

    def relative_is_ancestor(self, parent: str, child: str, allow_equal: bool = False) -> bool:
        p, c = self._relative_parts(parent), self._relative_parts(child)
        return (len(p) <= len(c) if allow_equal else len(p) < len(c)) and c[:len(p)] == p

    @staticmethod
    def _split_unc(path: str) -> tuple[str, str]:
        normalized = ntpath.normpath(path.replace("/", "\\"))
        if not normalized.startswith("\\\\"):
            raise ValueError(f"Not a UNC path: {path}")
        parts = normalized[2:].split("\\")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise ValueError(f"Invalid UNC path: {path}")
        return f"\\\\{parts[0]}\\{parts[1]}", "\\".join(parts[2:]).strip("\\")

    @staticmethod
    def _resolve_mapped_drive(path: str) -> Optional[str]:
        match = re.match(r"^([A-Za-z]:)(?:\\|/|$)", path)
        if not match:
            return None
        try:
            mpr = ctypes.WinDLL("mpr", use_last_error=True)
            fn = mpr.WNetGetConnectionW
            fn.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
            fn.restype = ctypes.c_uint32
            size = ctypes.c_uint32(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if fn(match.group(1), buf, ctypes.byref(size)) != 0:
                return None
            tail = path[len(match.group(1)):].lstrip("\\/")
            root = buf.value.rstrip("\\/")
            return root if not tail else root + "\\" + tail.replace("/", "\\")
        except Exception:
            return None

    @staticmethod
    def _guid_from_string(value: str) -> _GUID:
        guid = _GUID()
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        fn = ole32.CLSIDFromString
        fn.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(_GUID)]
        fn.restype = ctypes.c_long
        hr = fn(value, ctypes.byref(guid))
        if hr < 0:
            raise OSError(f"CLSIDFromString failed: 0x{hr & 0xFFFFFFFF:08X}")
        return guid

    @staticmethod
    def _com_method(instance: ctypes.c_void_p, index: int, restype: Any, *argtypes: Any) -> Any:
        vtable = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])

    def _resolve_lnk_target_ctypes(self, link_path: str) -> Optional[str]:
        try:
            ole32 = ctypes.WinDLL("ole32", use_last_error=True)
            coinit = ole32.CoInitializeEx
            coinit.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            coinit.restype = ctypes.c_long
            uninit = ole32.CoUninitialize
            create = ole32.CoCreateInstance
            create.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
            create.restype = ctypes.c_long
            shell_guid = self._guid_from_string("{00021401-0000-0000-C000-000000000046}")
            link_guid = self._guid_from_string("{000214F9-0000-0000-C000-000000000046}")
            persist_guid = self._guid_from_string("{0000010B-0000-0000-C000-000000000046}")
            hr_init = coinit(None, 0x2)
            changed_mode = -2147417850
            if hr_init < 0 and hr_init != changed_mode:
                return None
            should_uninit = hr_init in (0, 1)
            shell = ctypes.c_void_p()
            persist = ctypes.c_void_p()
            try:
                if create(ctypes.byref(shell_guid), None, 0x1, ctypes.byref(link_guid), ctypes.byref(shell)) < 0 or not shell:
                    return None
                query = self._com_method(shell, 0, ctypes.c_long, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))
                if query(shell, ctypes.byref(persist_guid), ctypes.byref(persist)) < 0 or not persist:
                    return None
                load = self._com_method(persist, 5, ctypes.c_long, ctypes.c_wchar_p, ctypes.c_uint32)
                if load(persist, link_path, 0) < 0:
                    return None
                get_path = self._com_method(shell, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_wchar), ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
                for flags in (0, 0x4):
                    buf = ctypes.create_unicode_buffer(32768)
                    hr = get_path(shell, buf, len(buf), None, flags)
                    if hr >= 0 and buf.value.strip():
                        return buf.value.strip()
                return None
            finally:
                for instance in (persist, shell):
                    if instance:
                        try:
                            self._com_method(instance, 2, ctypes.c_ulong)(instance)
                        except Exception:
                            pass
                if should_uninit:
                    try:
                        uninit()
                    except Exception:
                        pass
        except Exception as exc:
            self._log(f"Native .lnk resolution failed for {link_path}: {exc}", "WARNING", "PATH")
            return None

    def _resolve_windows_network_shortcut_folder(self, path: str) -> str:
        p = Path(path)
        link: Optional[Path] = None
        try:
            if p.is_file() and p.suffix.casefold() == ".lnk":
                link = p
            elif p.is_dir() and (p / "target.lnk").is_file():
                link = p / "target.lnk"
        except Exception:
            link = None
        if link is None:
            return path
        target = self.remove_outer_quotes(self._resolve_lnk_target_ctypes(str(link)))
        if target:
            self._log(f"Resolve method=NETWORK_SHORTCUT_LNK input={path!r} target={target!r}", "INFO", "PATH")
            return target
        raise ValueError(f"Windows Network Shortcut target could not be resolved: {path}")

    @staticmethod
    def _get_volume_descriptor(path: str) -> Optional[dict[str, Any]]:
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            get_path = kernel32.GetVolumePathNameW
            get_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
            get_path.restype = ctypes.c_bool
            mount = ctypes.create_unicode_buffer(32768)
            if not get_path(path, mount, len(mount)):
                return None
            mount_path = mount.value
            get_name = kernel32.GetVolumeNameForVolumeMountPointW
            get_name.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
            get_name.restype = ctypes.c_bool
            volbuf = ctypes.create_unicode_buffer(32768)
            if not get_name(mount_path, volbuf, len(volbuf)):
                return None
            volume_name = ctypes.create_unicode_buffer(1024)
            filesystem = ctypes.create_unicode_buffer(1024)
            serial = ctypes.c_uint32()
            max_component = ctypes.c_uint32()
            flags = ctypes.c_uint32()
            info = kernel32.GetVolumeInformationW
            info.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32), ctypes.c_wchar_p, ctypes.c_uint32]
            info.restype = ctypes.c_bool
            ok = info(mount_path, volume_name, len(volume_name), ctypes.byref(serial), ctypes.byref(max_component), ctypes.byref(flags), filesystem, len(filesystem))
            return {"mount_path": mount_path, "volume_guid": volbuf.value, "volume_serial": f"{serial.value:08X}" if ok else None, "filesystem": filesystem.value if ok else None, "volume_label": volume_name.value if ok else None}
        except Exception:
            return None

    @staticmethod
    def _get_directory_file_identity(path: str) -> Optional[tuple[int, str]]:
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create = kernel32.CreateFileW
            create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
            create.restype = ctypes.c_void_p
            handle = create(path, _FILE_READ_ATTRIBUTES, _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE, None, _OPEN_EXISTING, _FILE_FLAG_BACKUP_SEMANTICS, None)
            if handle == _INVALID_HANDLE_VALUE or not handle:
                return None
            try:
                info = _BY_HANDLE_FILE_INFORMATION()
                fn = kernel32.GetFileInformationByHandle
                fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
                fn.restype = ctypes.c_bool
                if not fn(handle, ctypes.byref(info)):
                    return None
                file_index = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
                return None if file_index == 0 else (int(info.dwVolumeSerialNumber), f"{file_index:016X}")
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None

    def normalize_path(self, path: str) -> str:
        value = self.remove_outer_quotes(path)
        if not value.strip():
            raise ValueError("Path is empty.")
        value = os.path.expanduser(value).replace("/", "\\")
        value = self._resolve_windows_network_shortcut_folder(value)
        value = self.remove_outer_quotes(value).replace("/", "\\")
        mapped = self._resolve_mapped_drive(value)
        if mapped:
            self._log(f"Resolve method=MAPPED_DRIVE input={value!r} target={mapped!r}", "INFO", "PATH")
            value = mapped
        if re.fullmatch(r"[A-Za-z]:\\*", value):
            value = value[0].upper() + ":\\"
        if not re.match(r"^(?:[A-Za-z]:\\|\\\\[^\\:?*]+\\[^\\:?*]+(?:\\|$))", value) or value.startswith("\\\\?\\") or value.startswith("\\\\.\\"):
            raise ValueError(f"Path must be absolute: {value}")
        full = ntpath.abspath(ntpath.normpath(value))
        drive = ntpath.splitdrive(full)[0]
        root = drive + "\\" if drive and not drive.startswith("\\\\") else ("\\\\" + "\\".join(full.strip("\\").split("\\")[:2]) + "\\" if full.startswith("\\\\") else "")
        return root if root and ntpath.normcase(full.rstrip("\\")) == ntpath.normcase(root.rstrip("\\")) else full.rstrip("\\")

    def resolve_storage(self, path: str, require_directory: bool = False) -> dict[str, Any]:
        original = self.remove_outer_quotes(path)
        normalized = self.normalize_path(original)
        if require_directory and not os.path.isdir(normalized):
            raise FileNotFoundError(normalized)
        if normalized.startswith("\\\\"):
            root, rel = self._split_unc(normalized)
            remote = self._get_directory_file_identity(root) if os.path.isdir(root) else None
            return {"UserPath": original, "Path": normalized, "StorageIdentityType": "UNC", "StorageIdentityKey": "UNC:" + self.path_key(root), "StorageRoot": root, "StorageRelativePath": rel, "RemoteVolumeSerial": remote[0] if remote else None, "RemoteFileId": remote[1] if remote else None, "VolumeGuid": None}
        volume = self._get_volume_descriptor(normalized)
        if volume and volume.get("volume_guid"):
            mount = ntpath.normpath(str(volume["mount_path"]))
            rel = ntpath.relpath(normalized, mount)
            if rel == ".":
                rel = ""
            return {"UserPath": original, "Path": normalized, "StorageIdentityType": "VOLUME_GUID", "StorageIdentityKey": "VOLUME:" + str(volume["volume_guid"]).casefold(), "StorageRoot": mount, "StorageRelativePath": rel, "RemoteVolumeSerial": None, "RemoteFileId": None, "VolumeGuid": str(volume["volume_guid"])}
        return {"UserPath": original, "Path": normalized, "StorageIdentityType": "PATH_FALLBACK", "StorageIdentityKey": "PATH:" + self.path_key(normalized), "StorageRoot": normalized, "StorageRelativePath": "", "RemoteVolumeSerial": None, "RemoteFileId": None, "VolumeGuid": None}

    def canonical_existing_path(self, path: str) -> str:
        return str(self.resolve_storage(path, True)["Path"])

    def paths_equal(self, a: str, b: str) -> bool:
        try:
            na, nb = self.normalize_path(a), self.normalize_path(b)
            if os.path.exists(na) and os.path.exists(nb):
                try:
                    return os.path.samefile(na, nb)
                except OSError:
                    pass
            return ntpath.normcase(na.rstrip("\\")) == ntpath.normcase(nb.rstrip("\\"))
        except Exception:
            return False

    def is_path_ancestor(self, child: str, parent: str, allow_equal: bool = False) -> bool:
        c = self.normalize_path(child)
        p = self.normalize_path(parent)
        if self.paths_equal(c, p):
            return allow_equal
        try:
            return ntpath.normcase(ntpath.commonpath([c, p])) == ntpath.normcase(p)
        except ValueError:
            return False

    def storage_relative_path(self, full_path: str, root_path: str) -> str:
        full = self.normalize_path(full_path)
        root = self.normalize_path(root_path)
        if self.paths_equal(full, root):
            return ""
        if not self.is_path_ancestor(full, root):
            raise ValueError(f"Path is not inside root: {full_path}")
        return ntpath.relpath(full, root).lstrip("\\")

    def leaf_label(self, path: str) -> str:
        normalized = self.normalize_path(path)
        leaf = ntpath.basename(normalized.rstrip("\\"))
        if leaf:
            return leaf
        drive, _ = ntpath.splitdrive(normalized)
        label = drive.replace("\\", "_").replace(":", "")
        return label or "Root"

    @staticmethod
    def is_link_or_reparse_point(path: str) -> bool:
        try:
            st = os.stat(path, follow_symlinks=False)
            attrs = getattr(st, "st_file_attributes", 0)
            return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except OSError:
            return False

    def no_link_ancestors(self, path: str) -> bool:
        current = self.normalize_path(path)
        while current:
            if os.path.exists(current) and self.is_link_or_reparse_point(current):
                return False
            parent = ntpath.dirname(current.rstrip("\\"))
            if not parent or ntpath.normcase(parent) == ntpath.normcase(current):
                break
            current = parent
        return True

    def path_parts(self, path: str) -> tuple[str, ...]:
        return tuple(part for part in str(path).replace("/", "\\").split("\\") if part)

    def relative_parts(self, value: str) -> tuple[str, ...]:
        return tuple(part for part in str(value).replace("/", "\\").strip("\\").split("\\") if part and part != ".")

    def ensure_trailing_separator(self, path: str) -> str:
        text = str(path)
        return text if not text or text.endswith("\\") else text + "\\"

    def valid_managed_relative_path(self, path: str) -> bool:
        rel = str(path)
        if not rel.strip() or ntpath.isabs(rel):
            return False
        if re.search(r'[:*?"<>|]', rel) or re.search(r'(^|[\\/])\.\.?([\\/]|$)', rel) or re.search(r'[. ]([\\/]|$)', rel):
            return False
        return True

    def source_review_alias(self, raw: str) -> tuple[str, str]:
        value = self.remove_outer_quotes(str(raw)).replace("/", "\\")
        folded = value.casefold()
        if "\\microsoft\\windows\\network shortcuts\\" in folded:
            return "Shortcut", ntpath.basename(value.rstrip("\\"))
        if re.fullmatch(r"[A-Za-z]:\\?", value):
            drive = value[:2] + "\\"
            return "Drive", drive
        return "Path", value

    @staticmethod
    def source_alias_group_key(item: dict[str, Any]) -> str | None:
        if str(item.get("StorageIdentityType")) != "UNC":
            return None
        key = str(item.get("StorageIdentityKey") or "").casefold()
        return key or None

    @staticmethod
    def source_alias_render_spec(kind: str) -> tuple[int, str]:
        return {
            "Drive": (0, "source.review.alias_drive"),
            "Shortcut": (1, "source.review.alias_shortcut"),
            "Path": (2, "source.review.alias_path"),
        }.get(str(kind), (9, "source.review.alias_path"))

    def adopt_storage_identity(self, desc: dict[str, Any], existing_items: Iterable[dict[str, Any]]) -> dict[str, Any]:
        if str(desc.get("StorageIdentityType")) != "UNC":
            return desc
        items = [item for item in existing_items if str(item.get("StorageIdentityType")) == "UNC"]
        base_key = str(desc.get("StorageIdentityKey") or "")
        current_serial = desc.get("RemoteVolumeSerial")
        current_file = str(desc.get("RemoteFileId") or "")
        canonical = str(desc.get("StorageRoot") or "")
        exact = next((item for item in items if item.get("StorageRoot") and self.path_key(str(item.get("StorageRoot"))) == self.path_key(canonical)), None)
        if exact is not None:
            stored_serial = exact.get("RemoteVolumeSerial")
            stored_file = str(exact.get("RemoteFileId") or "")
            have_current = current_serial is not None and bool(current_file)
            have_stored = stored_serial is not None and bool(stored_file)
            identity_matches = have_current and have_stored and int(stored_serial) == int(current_serial) and stored_file.casefold() == current_file.casefold()
            if not (have_current and have_stored) or identity_matches:
                adopted = str(exact.get("StorageIdentityKey") or base_key)
                if adopted:
                    desc = dict(desc); desc["StorageIdentityKey"] = adopted
                return desc
            desc = dict(desc)
            desc["StorageIdentityKey"] = f"{base_key}|REMOTE:{int(current_serial):08X}:{current_file.casefold()}"
            self._log(f"UNC canonical root reports a different storage identity; using new storage key root={canonical}", "WARNING", "PATH")
            return desc
        if current_serial is None or not current_file:
            return desc
        matches = [item for item in items if item.get("RemoteVolumeSerial") is not None and str(item.get("RemoteFileId") or "") and int(item.get("RemoteVolumeSerial")) == int(current_serial) and str(item.get("RemoteFileId")).casefold() == current_file.casefold()]
        identities = {str(item.get("StorageIdentityKey") or "") for item in matches if str(item.get("StorageIdentityKey") or "")}
        if len(identities) == 1:
            adopted = next(iter(identities))
            original = str(desc["StorageIdentityKey"])
            desc = dict(desc); desc["StorageIdentityKey"] = adopted
            canonical_existing = next((str(item.get("StorageRoot")) for item in matches if str(item.get("StorageIdentityKey")) == adopted and item.get("StorageRoot")), str(desc["StorageRoot"]))
            desc["StorageRoot"] = canonical_existing
            if original.casefold() != adopted.casefold():
                self._log(f"Storage identity matched by READ_ONLY_DIRECTORY_ID original={original} adopted={adopted}", "INFO", "PATH")
        return desc

    @staticmethod
    def descriptor_persistence_fields(desc: dict[str, Any]) -> dict[str, Any]:
        return {"RemoteVolumeSerial": desc.get("RemoteVolumeSerial"), "RemoteFileId": desc.get("RemoteFileId"), "VolumeGuid": desc.get("VolumeGuid")}

    @staticmethod
    def empty_descriptor_persistence_fields() -> dict[str, Any]:
        return {"RemoteVolumeSerial": None, "RemoteFileId": None, "VolumeGuid": None}

    @staticmethod
    def link_violation_message(code: str, path: str | None = None) -> str:
        messages = {
            "source_traverses": "Media source traverses a reparse point: {path}",
            "skipped_link": "Skipped reparse point: {path}",
            "output_library_traverses": "Output library traverses a reparse point.",
            "output_library_root": "Output library root is a reparse point, symlink, or junction.",
            "marker": "Marker is a reparse point.",
            "output_path_traverses": "Output path traverses a reparse point.",
            "mode_path_traverses": "Mode path traverses a reparse point.",
            "mode_folder": "Mode folder is a reparse point, symlink, or junction.",
            "mode_tree": "Mode tree contains a reparse point; replacement is blocked.",
            "managed_artifact": "Managed artifact is a reparse point.",
            "managed_library": "Managed library contains a reparse point: {path}",
            "destination_file": "destination file is a reparse point",
        }
        template = messages.get(code)
        if template is None:
            raise KeyError(code)
        return template.format(path=path or "")

    @staticmethod
    def platform_reason_locale_key(reason: str) -> str | None:
        return {
            "Output library root is a reparse point, symlink, or junction.": "build.reason.output_library_reparse",
            "Marker is a reparse point.": "build.reason.marker_reparse",
            "Mode folder is a reparse point, symlink, or junction.": "build.reason.mode_reparse",
        }.get(str(reason))

    @staticmethod
    def valid_filename_component(text: str) -> bool:
        if not text or re.search(r'[<>:"/\\|?*\x00-\x1f]', text) or re.search(r'[. ]$', text):
            return False
        return not bool(re.match(r'^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', text))

    @staticmethod
    def join_path(*parts: str) -> str: return ntpath.join(*parts)
    @staticmethod
    def basename(path: str) -> str: return ntpath.basename(path)
    @staticmethod
    def dirname(path: str) -> str: return ntpath.dirname(path)
    @staticmethod
    def splitext(path: str) -> tuple[str, str]: return ntpath.splitext(path)
    @staticmethod
    def normpath(path: str) -> str: return ntpath.normpath(path)
    @staticmethod
    def normcase(path: str) -> str: return ntpath.normcase(path)
    @staticmethod
    def isabs(path: str) -> bool: return ntpath.isabs(path)
