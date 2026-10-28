# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

"""Regression tests for issues found in the v2.0.1 code review.

Run from the repository root:  python3 -m unittest discover -s tests -v
These tests exercise the Linux adapter and are skipped on other platforms.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

LINUX = sys.platform == "linux"

# The test suite is not part of the application, so it may pick the adapter itself
# (the "sys.platform only in main.py" rule applies to the application modules).
if LINUX:
    from modules import platform_api
    from modules.platform_linux import LinuxPlatform

    platform_api.configure_platform(LinuxPlatform())
elif sys.platform == "win32":
    from modules import platform_api
    from modules.platform_windows import WindowsPlatform

    platform_api.configure_platform(WindowsPlatform())


class _Renderer:
    def __init__(self) -> None:
        self.messages: list[tuple] = []

    def status(self, key, type_="INFO", component="APP", *, log_message="", **kwargs) -> None:
        self.messages.append((key, type_, log_message))


def _enumerate(root_dir: Path):
    import modules.scan as scan

    stats = {"FilesFound": 0, "Excluded": 0, "VideoCandidates": 0}
    config = {"Outputs": [], "SourceRoots": [], "LibraryName": "Lib"}
    root = {"Path": str(root_dir), "RootId": "11111111-1111-1111-1111-111111111111", "Label": "lib"}
    renderer = _Renderer()
    files = scan.get_media_files_for_root(root, config, stats, renderer)
    return files, stats, renderer


@unittest.skipUnless(LINUX, "Linux adapter test")
class ScanEnumerationTests(unittest.TestCase):
    def test_h1_non_utf8_filename_is_skipped_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / "lib"
            lib.mkdir()
            (lib / "ok.mkv").write_bytes(b"x")
            fd = os.open(os.fsencode(str(lib)) + b"/Film \xe8\x9a.mkv", os.O_CREAT | os.O_WRONLY)
            os.close(fd)
            files, stats, renderer = _enumerate(lib)
            self.assertEqual([f["RelativePath"] for f in files], ["ok.mkv"])
            self.assertEqual(stats["FilesFound"], 2)
            self.assertTrue(any(m[0] == "source.file_inspect_failed" for m in renderer.messages))
            for f in files:  # everything handed to the probe cache must be UTF-8 encodable
                f["Path"].encode("utf-8")
                f["RelativePath"].encode("utf-8")

    def test_h2_source_below_a_folder_named_samples_is_not_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / "Samples" / "lib"
            lib.mkdir(parents=True)
            (lib / "a.mkv").write_bytes(b"x")
            (lib / "b.mkv").write_bytes(b"x")
            files, _stats, _ = _enumerate(lib)
            self.assertEqual(sorted(f["RelativePath"] for f in files), ["a.mkv", "b.mkv"])

    def test_h2_sample_and_trailer_inside_the_source_are_still_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / "lib"
            (lib / "Sample").mkdir(parents=True)
            (lib / "Sample" / "x.mkv").write_bytes(b"x")
            (lib / "movie-sample.mkv").write_bytes(b"x")
            (lib / "movie.mkv").write_bytes(b"x")
            files, _stats, _ = _enumerate(lib)
            self.assertEqual([f["RelativePath"] for f in files], ["movie.mkv"])


@unittest.skipUnless(LINUX, "Linux adapter test")
class InstanceLockTests(unittest.TestCase):
    def _lock_in(self, directory: str):
        from modules.platform_linux import _LinuxInstanceLock

        old = {k: os.environ.get(k) for k in ("TMPDIR", "XDG_RUNTIME_DIR")}
        os.environ["TMPDIR"] = directory
        os.environ.pop("XDG_RUNTIME_DIR", None)
        tempfile.tempdir = None  # force gettempdir() to re-read TMPDIR
        try:
            return _LinuxInstanceLock()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            tempfile.tempdir = None

    def test_h3_symlinked_lock_file_is_not_followed_or_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            victim = Path(tmp) / "victim.txt"
            victim.write_text("IMPORTANT USER DATA\n")
            lock = self._lock_in(tmp)
            Path(lock.path).symlink_to(victim)
            self.assertFalse(lock.acquire())
            self.assertEqual(victim.read_text(), "IMPORTANT USER DATA\n")
            self.assertNotIn(lock.failure, (None, "busy"))  # reported as a lock error, not as "already running"

    def test_h3_second_instance_is_refused_and_release_allows_reacquire(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first, second = self._lock_in(tmp), self._lock_in(tmp)
            self.assertTrue(first.acquire())
            try:
                self.assertEqual(Path(first.path).read_text(), str(os.getpid()))
                self.assertEqual(oct(os.stat(first.path).st_mode & 0o777), "0o600")
                self.assertFalse(second.acquire())
                self.assertEqual(second.failure, "busy")
            finally:
                first.release()
            self.assertTrue(second.acquire())
            second.release()


@unittest.skipUnless(LINUX, "Linux adapter test")
class CopyTests(unittest.TestCase):
    def _copy(self, file_name: str):
        import uuid

        import modules.build as build
        import modules.config as config_module
        import modules.paths as paths
        import modules.scan as scan
        from modules.datetime_helpers import utc_now_o

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            # save_config() persists to DATA_DIR: keep the tests away from a real installation's data/.
            for attr, value in (("CONFIG_PATH", work / "data" / "config.json"), ("DATA_DIR", work / "data")):
                patcher = mock.patch.object(config_module, attr, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            previous_config = config_module.CURRENT_CONFIG
            self.addCleanup(setattr, config_module, "CURRENT_CONFIG", previous_config)
            out_base, src_dir = work / "out", work / "src"
            out_base.mkdir()
            src_dir.mkdir()
            src = src_dir / file_name
            src.write_bytes(b"x" * 321)
            cfg = {"Outputs": [], "SourceRoots": [], "LibraryName": "Lib"}
            paths.add_configured_outputs(cfg, [str(out_base)])
            outputs = cfg["Outputs"] or (config_module.CURRENT_CONFIG or {}).get("Outputs")
            cfg["Outputs"] = outputs
            init = build.initialize_new_output_library(cfg, outputs[0])
            self.assertTrue(init["Success"], init)
            lib = init["LibraryPath"]
            lib_id = build.test_output_library_marker(lib, "")["Marker"]["LibraryId"]
            snap = scan.source_snapshot(str(src))
            now = utc_now_o()
            item = {"SelectionOrder": 1, "Pool": "SDR", "SourcePath": str(src), "SourceLength": snap["Length"],
                    "SourceLastWriteTimeUtc": snap["LastWriteTimeUtc"], "DestinationRelativePath": f"SDR/Src/{file_name}",
                    "CopyStatus": "Pending", "CopyAttempts": 0, "LastCopyExitCode": None, "LastCopyMessage": None,
                    "CompletedAtUtc": None, "Media": {}, "ReasonForSelection": {}}
            manifest = {"ProgramVersion": "2.0.1", "ManifestSchemaVersion": 3, "SelectionId": str(uuid.uuid4()),
                        "BuildId": str(uuid.uuid4()), "SelectionMode": "SDR", "BuildMode": "SDR",
                        "SelectionCreatedAtUtc": now, "BuildCreatedAtUtc": now, "UpdatedAtUtc": now,
                        "LibraryId": lib_id, "OutputBasePath": str(out_base), "LibraryPath": lib,
                        "BuildStatus": "Incomplete", "RequestedCounts": {}, "RecommendedMinimums": {},
                        "SourceRoots": [], "SelectionParameters": {}, "BuildModes": ["SDR"],
                        "BuildLibraryIds": [lib_id], "SelectedFiles": [item]}
            build.write_build_manifest(manifest)  # B1 regression: manifest contract must accept a valid manifest
            ok = build.invoke_manifest_item_copy(manifest, item, 1, 1)
            dest = Path(build.get_manifest_item_destination_path(manifest, item))
            leftovers = [p.name for p in dest.parent.iterdir() if p.name.endswith(".part")]
            return ok, item, dest, leftovers

    def test_m6_same_size_but_different_content_is_detected(self) -> None:
        import modules.build as build

        real_copy = build.copy_single_file

        def corrupting_copy(source, destination):
            result = real_copy(source, destination)
            with open(destination, "r+b") as fh:  # same size, one byte flipped
                fh.seek(160)
                fh.write(b"Z")
            return result

        with mock.patch.object(build, "copy_single_file", corrupting_copy):
            ok, item, dest, leftovers = self._copy("a.mkv")
        self.assertFalse(ok)
        self.assertEqual(item["CopyStatus"], "Failed")
        self.assertIn("content differs", item["LastCopyMessage"])
        self.assertFalse(dest.exists())
        self.assertEqual(leftovers, [])  # the bad temporary copy is removed

    def test_m6_sampled_comparison_checks_start_middle_and_end(self) -> None:
        import modules.build as build

        size = 5 * 1024 * 1024
        data = bytes(range(256)) * (size // 256)
        with tempfile.TemporaryDirectory() as tmp:
            src, dup = Path(tmp) / "s.bin", Path(tmp) / "d.bin"
            src.write_bytes(data)
            dup.write_bytes(data)
            self.assertTrue(build.sampled_content_matches(str(src), str(dup), size))
            for position in (0, size // 2 - 10, size - 1):  # first, middle, last sampled region
                broken = bytearray(data)
                broken[position] ^= 0xFF
                dup.write_bytes(bytes(broken))
                self.assertFalse(build.sampled_content_matches(str(src), str(dup), size), position)
            dup.write_bytes(data[:-1])  # truncated copy
            self.assertFalse(build.sampled_content_matches(str(src), str(dup), size))
            self.assertTrue(build.sampled_content_matches(str(src), str(dup), 0))
            self.assertFalse(build.sampled_content_matches(str(src), str(Path(tmp) / "missing"), size))

    def test_normal_copy_completes_without_leftovers(self) -> None:
        ok, item, dest, leftovers = self._copy("a.mkv")
        self.assertTrue(ok, item)
        self.assertEqual(item["CopyStatus"], "Completed")
        self.assertEqual(leftovers, [])

    def test_h4_name_of_254_bytes_can_be_copied(self) -> None:
        name = "a" * 250 + ".mkv"
        self.assertEqual(len(name.encode()), 254)
        ok, item, dest, leftovers = self._copy(name)
        self.assertTrue(ok, item)
        self.assertEqual(item["CopyStatus"], "Completed")
        self.assertEqual(dest.name, name)
        self.assertEqual(leftovers, [])

    def test_h4_partial_name_is_unchanged_for_normal_names_and_short_for_long_ones(self) -> None:
        import modules.build as build

        self.assertEqual(build.partial_copy_path("/x/y/movie.mkv"), "/x/y/movie.mkv.part")
        long_part = build.partial_copy_path("/x/y/" + "a" * 250 + ".mkv")
        self.assertTrue(long_part.startswith("/x/y/.mds-") and long_part.endswith(".part"))
        self.assertLessEqual(len(os.path.basename(long_part).encode()), 255)
        self.assertEqual(long_part, build.partial_copy_path("/x/y/" + "a" * 250 + ".mkv"))  # deterministic


_SIGNAL_SCRIPT = r"""
import os, signal, sys, tempfile
from pathlib import Path
sys.path.insert(0, {repo!r})
import modules.logger as logger
import modules.interrupts as interrupts
logger.LOG_PATH = Path({log!r})
logger.ACTIVE_LOG_LEVEL = "debug"
with interrupts.scoped_graceful_stop("SCAN") as ctl:
    {body}
"""


@unittest.skipIf(sys.platform == "win32", "uses os.kill(SIGINT) on the current process")
class SignalHandlerTests(unittest.TestCase):
    """H5: the SIGINT handler runs on the main thread, which may already hold the locks it needs."""

    def _run(self, body: str):
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "t.log"
            code = _SIGNAL_SCRIPT.format(repo=str(REPO), log=str(log), body=body)
            proc = subprocess.run([sys.executable, "-B", "-I", "-c", code], capture_output=True, text=True, timeout=20)
            return proc, (log.read_text(encoding="utf-8") if log.exists() else "")

    def test_h5_first_press_while_main_thread_holds_log_lock(self) -> None:
        proc, log = self._run(
            "with logger._LOG_LOCK:\n"
            "        os.kill(os.getpid(), signal.SIGINT)\n"
            "    assert ctl.stop_event.is_set()\n"
            "    import time; time.sleep(0.5)  # a real run keeps going; let the helper thread write its line"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("count=1 action=GRACEFUL_STOP", log)

    def test_h5_first_press_inside_handler_critical_section(self) -> None:
        proc, _ = self._run("with ctl._lock:\n        os.kill(os.getpid(), signal.SIGINT)")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_h5_force_exit_while_main_thread_holds_process_lock(self) -> None:
        proc, log = self._run(
            "os.kill(os.getpid(), signal.SIGINT)\n"
            "    with interrupts._ACTIVE_PROCESS_LOCK:\n"
            "        os.kill(os.getpid(), signal.SIGINT)"
        )
        self.assertEqual(proc.returncode, 130, proc.stderr)
        self.assertIn("count=2 action=FORCE_EXIT", log)

    def test_h5_force_exit_while_main_thread_holds_log_lock(self) -> None:
        proc, _ = self._run(
            "os.kill(os.getpid(), signal.SIGINT)\n"
            "    with logger._LOG_LOCK:\n"
            "        os.kill(os.getpid(), signal.SIGINT)"
        )
        self.assertEqual(proc.returncode, 130, proc.stderr)


class LocalizationTests(unittest.TestCase):
    def test_m5_shipped_locales_load(self) -> None:
        from modules.localization import LocaleManager

        manager = LocaleManager(REPO / "locales")
        manager.load_canonical()
        for entry in manager.discover():
            self.assertTrue(manager.activate(entry["Locale"]), entry)

    def test_m5_positional_placeholders_are_rejected_when_loading(self) -> None:
        import json
        import shutil

        from modules.localization import LocaleError, LocaleManager

        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(REPO / "locales" / "en-US.json", Path(tmp) / "en-US.json")
            canonical = json.loads((Path(tmp) / "en-US.json").read_text(encoding="utf-8-sig"))
            key = next(k for k, v in canonical.items()
                       if isinstance(v, str) and not k.startswith("_meta.") and "{" not in v)
            for bad in ("{}", "{0}", "x {} y"):
                (Path(tmp) / "xx-XX.json").write_text(
                    json.dumps({"_meta.code": "xx", "_meta.locale": "xx-XX", "_meta.name": "Test", key: bad}),
                    encoding="utf-8")
                manager = LocaleManager(Path(tmp))
                manager.load_canonical()
                with self.assertRaises(LocaleError, msg=bad):
                    manager.activate("xx-XX")


class TimestampTests(unittest.TestCase):
    def test_m8_seven_digit_timestamps_parse_on_every_supported_python(self) -> None:
        from modules.datetime_helpers import parse_utc_timestamp, utc_now_o

        parsed = parse_utc_timestamp("2026-10-26T12:00:00.1234567Z")
        self.assertEqual((parsed.year, parsed.microsecond), (2026, 123456))
        self.assertIsNotNone(parse_utc_timestamp(utc_now_o()).tzinfo)
        for text in ("2026-10-26T12:00:00Z", "2026-10-26T12:00:00.5Z", "2026-10-26T12:00:00.12Z"):
            parse_utc_timestamp(text)
        for bad in ("", "not a date", "2026-13-40T00:00:00.1234567Z"):
            with self.assertRaises(ValueError, msg=bad):
                parse_utc_timestamp(bad)


class FileIoTests(unittest.TestCase):
    def test_m2_atomic_write_fsyncs_the_directory_after_rename(self) -> None:
        import stat

        import modules.file_io as file_io

        if not hasattr(os, "O_DIRECTORY"):
            self.skipTest("directory fsync is POSIX only")
        synced_dirs: list[int] = []
        real_fsync = os.fsync

        def recording_fsync(fd: int) -> None:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                synced_dirs.append(fd)
            real_fsync(fd)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sub" / "x.json"
            with mock.patch.object(file_io.os, "fsync", recording_fsync):
                file_io.write_json_file_atomically(target, {"a": 1})
            self.assertEqual(target.read_text(encoding="utf-8").replace(" ", "").replace("\n", ""), '{"a":1}')
            self.assertEqual(len(synced_dirs), 1)
            self.assertEqual([p.name for p in target.parent.iterdir()], ["x.json"])  # no temp leftovers


@unittest.skipUnless(LINUX, "Linux adapter test")
class ExcludedDirectoryTests(unittest.TestCase):
    def test_m3_nas_metadata_and_recycle_folders_are_not_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / "lib"
            for folder in ("@eaDir", "#recycle", "@Recycle", "#snapshot", "System Volume Information", "$RECYCLE.BIN", "real"):
                (lib / folder).mkdir(parents=True)
                (lib / folder / "v.mkv").write_bytes(b"x")
            files, _stats, _ = _enumerate(lib)
            self.assertEqual([f["RelativePath"] for f in files], ["real/v.mkv"])


class SelectionEquivalenceTests(unittest.TestCase):
    """M7: the optimised balanced fill must pick exactly what the v2.0.1 algorithm picked."""

    # (model, candidates, requested, seed) -> (sha, recommended minimum, selected), from the v2.0.1 code.
    EXPECTED = {
        ("SDR", 700, 60, 1): ("57ae9bb6160680f2", 24, 60),
        ("SDR", 400, 400, 2): ("9f585c713546563c", 24, 400),
        ("HDR", 500, 80, 3): ("bf90ff2bcb49986c", 24, 80),
        ("HDR", 300, 5, 4): ("095dacc3a9c93312", 24, 5),
        ("SDR", 150, 3, 5): ("13ae18668bab3932", 28, 3),
    }

    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))  # for selection_fixtures

    def test_m7_selection_is_identical_to_the_v2_0_1_algorithm(self) -> None:
        import modules.selection as selection
        import selection_fixtures

        for case, expected in self.EXPECTED.items():
            with self.subTest(case=case):
                self.assertEqual(selection_fixtures.fingerprint(selection, *case), expected)

    def test_m7_balance_score_matches_a_naive_reference(self) -> None:
        import modules.selection as selection
        import selection_fixtures
        from modules.classification import reliable_diversity_value

        def naive(info, already, model, index, pool_size):
            weights = selection._balance_weights(model)
            base, reasons = 0.0, []
            for name, weight in weights.items():
                value = selection.diversity_property_value(info["Record"], name)
                if not reliable_diversity_value(value):
                    continue
                count = sum(1 for s in already
                            if reliable_diversity_value(selection.diversity_property_value(s["CandidateInfo"]["Record"], name))
                            and str(selection.diversity_property_value(s["CandidateInfo"]["Record"], name)).lower() == str(value).lower())
                base += float(weight) / (1.0 + count)
                if count == 0:
                    reasons.append(f"Underrepresented {name}: {value}")
            bonus = min(base * selection.RARITY_MAXIMUM_FRACTION * selection.rarity_factor(info, index, pool_size),
                        base * selection.RARITY_MAXIMUM_FRACTION)
            return {"Score": base + bonus, "BaseScore": base, "RarityBonus": bonus, "BalanceReasons": reasons}

        for model in ("SDR", "HDR"):
            plan = selection.new_diversity_selection_plan(selection_fixtures.build_records(model, 120, 11), model)
            infos = plan["CandidateInfos"]
            already = [{"CandidateInfo": i} for i in infos[:25]]
            for info in infos[25:60]:
                self.assertEqual(selection.balance_score(info, already, model, plan["RarityIndex"], len(infos)),
                                 naive(info, already, model, plan["RarityIndex"], len(infos)))


@unittest.skipUnless(LINUX, "Linux adapter test")
class SymlinkAncestorTests(unittest.TestCase):
    """M1: administrator symlinks in the path (/home -> /var/home) must not block a source or output."""

    def _layout(self, tmp: str):
        real = Path(tmp) / "var" / "home" / "gregor" / "media"
        real.mkdir(parents=True)
        (Path(tmp) / "home").symlink_to(Path(tmp) / "var" / "home")  # like /home -> /var/home
        return Path(tmp) / "home" / "gregor" / "media", real

    def test_m1_system_owned_symlink_in_a_protected_directory_is_accepted(self) -> None:
        import modules.platform_linux as pl

        with tempfile.TemporaryDirectory() as tmp:
            via_link, _real = self._layout(tmp)
            with mock.patch.object(pl, "_TRUSTED_LINK_UID", os.getuid()):  # pretend the current user is "root"
                self.assertTrue(platform_api.no_link_ancestors(str(via_link)))

    def test_m1_user_owned_symlink_is_still_rejected(self) -> None:
        import modules.platform_linux as pl

        with tempfile.TemporaryDirectory() as tmp:
            via_link, _real = self._layout(tmp)
            with mock.patch.object(pl, "_TRUSTED_LINK_UID", os.getuid() + 1):  # link owner is not the trusted uid
                self.assertFalse(platform_api.no_link_ancestors(str(via_link)))

    def test_m1_symlink_in_a_group_or_world_writable_directory_is_rejected(self) -> None:
        import modules.platform_linux as pl

        with tempfile.TemporaryDirectory() as tmp:
            via_link, _real = self._layout(tmp)
            os.chmod(tmp, 0o777)  # the directory holding the link can be modified by others
            with mock.patch.object(pl, "_TRUSTED_LINK_UID", os.getuid()):
                self.assertFalse(platform_api.no_link_ancestors(str(via_link)))

    def test_m1_a_link_inside_the_source_tree_is_still_skipped_by_the_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / "lib"
            lib.mkdir()
            (lib / "real.mkv").write_bytes(b"x")
            (Path(tmp) / "elsewhere.mkv").write_bytes(b"x")
            (lib / "linked.mkv").symlink_to(Path(tmp) / "elsewhere.mkv")
            files, _stats, _ = _enumerate(lib)
            self.assertEqual([f["RelativePath"] for f in files], ["real.mkv"])


@unittest.skipUnless(LINUX, "Linux adapter test")
class StorageIdentityTests(unittest.TestCase):
    """M4: bind mounts of one filesystem are one storage; NFS export case differences are not."""

    @staticmethod
    def _mount(root, mount_point, filesystem, source, major_minor="8:1"):
        from modules.platform_linux import _MountInfo

        return _MountInfo(1, 0, major_minor, root, mount_point, filesystem, source, "rw", "rw")

    def test_m4_bind_mount_and_direct_mount_agree_on_key_and_relative_path(self) -> None:
        from modules.platform_linux import _filesystem_relative_path, _identity_for_mount

        direct = self._mount("/", "/data", "ext4", "/dev/sda1")
        bind = self._mount("/media", "/mnt/media", "ext4", "/dev/sda1")
        self.assertEqual(_identity_for_mount(direct)[1], _identity_for_mount(bind)[1])
        # /data/media/film.mkv and /mnt/media/film.mkv are the same file
        self.assertEqual(_filesystem_relative_path(direct, "media/film.mkv"), _filesystem_relative_path(bind, "film.mkv"))
        self.assertEqual(_filesystem_relative_path(direct, ""), "")
        self.assertEqual(_filesystem_relative_path(bind, ""), "media")

    def test_m4_existing_keys_for_ordinary_mounts_are_unchanged(self) -> None:
        from modules.platform_linux import _identity_for_mount

        self.assertEqual(_identity_for_mount(self._mount("/", "/mnt/movies", "cifs", "//192.168.1.9/Movies"))[1],
                         "LINUX_CIFS://192.168.1.9/movies|ROOT:/")
        self.assertEqual(_identity_for_mount(self._mount("/", "/mnt/x", "nfs4", "srv:/export"))[1],
                         "LINUX_NFS:srv:/export|ROOT:/")

    def test_m4_nfs_exports_that_differ_only_in_case_stay_distinct_under_casefold(self) -> None:
        from modules.platform_linux import _identity_for_mount

        upper = _identity_for_mount(self._mount("/", "/mnt/a", "nfs4", "srv:/Export"))[1]
        lower = _identity_for_mount(self._mount("/", "/mnt/b", "nfs4", "srv:/export"))[1]
        again = _identity_for_mount(self._mount("/", "/mnt/c", "nfs4", "srv:/Export"))[1]
        self.assertNotEqual(upper.casefold(), lower.casefold())
        self.assertEqual(upper, again)  # same export mounted twice is still one storage

    def test_m4_identity_of_a_real_path_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desc = platform_api.resolve_storage(tmp, True)
            self.assertTrue(desc["StorageIdentityKey"].endswith("|ROOT:/"), desc["StorageIdentityKey"])
            again = platform_api.resolve_storage(tmp, True)
            self.assertEqual(desc["StorageIdentityKey"], again["StorageIdentityKey"])


class ClassificationTests(unittest.TestCase):
    MOV_FAMILY = "mov,mp4,m4a,3gp,3g2,mj2"

    def test_mov_family_is_split_by_major_brand(self) -> None:
        from modules.classification import container_class

        self.assertEqual(container_class(self.MOV_FAMILY, "isom"), "mp4")
        self.assertEqual(container_class(self.MOV_FAMILY, "mp42"), "mp4")
        self.assertEqual(container_class(self.MOV_FAMILY, "qt  "), "mov")
        self.assertEqual(container_class(self.MOV_FAMILY, "M4V "), "m4v")
        self.assertEqual(container_class(self.MOV_FAMILY, "3gp4"), "3gp")
        self.assertEqual(container_class(self.MOV_FAMILY, "3g2a"), "3g2")

    def test_mov_family_falls_back_to_extension_then_stays_unknown(self) -> None:
        from modules.classification import container_class

        self.assertEqual(container_class(self.MOV_FAMILY, None, ".MP4"), "mp4")
        self.assertEqual(container_class(self.MOV_FAMILY, None, ".mov"), "mov")
        self.assertIsNone(container_class(self.MOV_FAMILY, None, ".xyz"))
        self.assertIsNone(container_class(self.MOV_FAMILY))

    def test_other_containers_are_unchanged(self) -> None:
        from modules.classification import container_class

        self.assertEqual(container_class("matroska,webm", "isom", ".mp4"), "matroska")
        self.assertEqual(container_class("avi"), "avi")
        self.assertEqual(container_class("mpegts"), "mpegts")
        self.assertIsNone(container_class(None))
        self.assertIsNone(container_class("  "))

    def test_fps_class_standard_rates_unchanged(self) -> None:
        from modules.classification import fps_class

        self.assertEqual(fps_class(23.976), "23.976/24")
        self.assertEqual(fps_class(24.0), "23.976/24")
        self.assertEqual(fps_class(25.0), "25")
        self.assertEqual(fps_class(29.97), "29.97/30")
        self.assertEqual(fps_class(50.0), "50")
        self.assertEqual(fps_class(59.94), "59.94/60")
        self.assertEqual(fps_class(12.0), "<=15")
        self.assertEqual(fps_class(120.0), ">60")
        self.assertEqual(fps_class(20.0), "Other")
        self.assertEqual(fps_class(27.0), "Other")
        self.assertIsNone(fps_class(None))
        self.assertIsNone(fps_class(0.0))

    def test_fps_class_accepts_one_millisecond_rounded_rates(self) -> None:
        from modules.classification import fps_class, rational_to_double

        self.assertEqual(fps_class(rational_to_double("500/21")), "23.976/24")  # 42 ms (seen in a real Matroska file)
        self.assertEqual(fps_class(1000 / 33), "29.97/30")
        self.assertEqual(fps_class(1000 / 17), "59.94/60")
        self.assertEqual(fps_class(23.81), "23.976/24")
        # Neighbouring rates must not be pulled into a standard class.
        self.assertEqual(fps_class(1000 / 43), "Other")   # 23.26
        self.assertEqual(fps_class(1000 / 41), "Other")   # 24.39
        self.assertEqual(fps_class(1000 / 34), "Other")   # 29.41
        self.assertEqual(fps_class(1000 / 32), "Other")   # 31.25

    def test_normalize_probe_media_uses_brand_extension_and_rounded_rate(self) -> None:
        from modules.classification import normalize_probe_media

        def scan_file(path: str, tags, avg: str, real: str) -> dict:
            return {
                "Path": path, "RootId": "r", "RootLabel": "l", "RootPath": "/lib", "RelativePath": path,
                "Length": 1000, "LastWriteTimeUtc": "2026-01-01T00:00:00.0000000Z",
                "ProbeEntry": {"ProbeStatus": "Success", "ProbeData": {
                    "Stream": {"CodecName": "h264", "Width": 1920, "Height": 1080, "AverageFrameRate": avg,
                               "RealFrameRate": real, "Duration": "100.0", "SideDataList": []},
                    "Format": {"FormatName": self.MOV_FAMILY, "Tags": tags, "Duration": "100.0"}}},
            }

        mp4 = normalize_probe_media(scan_file("/lib/a.mp4", {"major_brand": "isom"}, "500/21", "500/21"))
        self.assertEqual(mp4["Container"], "mp4")
        self.assertEqual(mp4["FpsClass"], "23.976/24")
        self.assertAlmostEqual(mp4["Fps"], 500 / 21)
        mov = normalize_probe_media(scan_file("/lib/a.mov", {"Major_Brand": "qt  "}, "25/1", "25/1"))
        self.assertEqual(mov["Container"], "mov")
        no_tags = normalize_probe_media(scan_file("/lib/b.mp4", None, "25/1", "25/1"))
        self.assertEqual(no_tags["Container"], "mp4")


class CleanupTests(unittest.TestCase):
    def test_low_settings_prompt_offers_the_current_value_as_default(self) -> None:
        import modules.ui as ui

        captured: dict = {}

        def fake_read(prompt, default=None, minimum=1, maximum=2_147_483_647):
            captured.update(default=default, minimum=minimum, maximum=maximum)
            return default

        config = {"ProbeWorkers": 5, "FFprobeTimeoutSeconds": 45, "ProbeRetries": 2}
        saved: list = []
        choices = iter([1, 0])
        with mock.patch.object(ui, "CURRENT_CONFIG", config), \
                mock.patch.object(ui, "show_menu_screen"), mock.patch.object(ui, "show_settings_values"), \
                mock.patch.object(ui, "read_submenu_choice", lambda *a, **k: next(choices)), \
                mock.patch.object(ui, "read_positive_integer", fake_read), \
                mock.patch.object(ui, "save_runtime_config", lambda c: saved.append(c) or True), \
                mock.patch.object(ui, "t", lambda key, **kw: key), \
                mock.patch.object(ui, "write_sampler_log"), mock.patch("builtins.print"):
            ui.show_settings_menu()
        self.assertEqual(captured["default"], 5)  # was the factory default (8) before
        self.assertEqual(saved[0]["ProbeWorkers"], 5)

    def test_low_no_stale_version_text_and_no_duplicate_scan_state_in_selection(self) -> None:
        import modules.selection as selection

        for path in (REPO / "modules").glob("*.py"):
            self.assertNotIn("v1.1.6", path.read_text(encoding="utf-8"), path.name)
        for name in ("CURRENT_CLASSIFIED_RECORDS", "HAS_CURRENT_SCAN", "reset_selection_state",
                     "get_current_classified_records", "has_current_scan"):
            self.assertFalse(hasattr(selection, name), name)  # ui.py owns the current scan state


class AstGuardTests(unittest.TestCase):
    def test_b1_b2_no_undefined_names_or_missing_module_attributes(self) -> None:
        import ast
        import builtins

        pkg = REPO / "modules"
        tops: dict[str, set[str]] = {}
        trees = {p.stem: ast.parse(p.read_text(encoding="utf-8")) for p in pkg.glob("*.py")}
        for m, tree in trees.items():
            names: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Module):
                    for n in node.body:
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            names.add(n.name)
                        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                            names.update(x.id for t in targets for x in ast.walk(t) if isinstance(x, ast.Name))
                        elif isinstance(n, (ast.Import, ast.ImportFrom)):
                            names.update((a.asname or a.name).split(".")[0] for a in n.names)
                        else:
                            for x in ast.walk(n):
                                if isinstance(x, (ast.FunctionDef, ast.ClassDef)):
                                    names.add(x.name)
                                elif isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store):
                                    names.add(x.id)
                                elif isinstance(x, (ast.Import, ast.ImportFrom)):
                                    names.update((a.asname or a.name).split(".")[0] for a in x.names)
            tops[m] = names
        problems: list[str] = []
        for m, tree in trees.items():
            bound = set(dir(builtins)) | {"__file__", "__name__"}
            aliases: dict[str, str] = {}
            for n in ast.walk(tree):
                if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                    bound.add(n.id)
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    bound.add(n.name)
                elif isinstance(n, ast.arg):
                    bound.add(n.arg)
                elif isinstance(n, ast.ExceptHandler) and n.name:
                    bound.add(n.name)
                elif isinstance(n, (ast.Import, ast.ImportFrom)):
                    for a in n.names:
                        bound.add((a.asname or a.name).split(".")[0])
                    if isinstance(n, ast.ImportFrom) and n.level == 1 and n.module is None:
                        aliases.update({(a.asname or a.name): a.name for a in n.names})
            for n in ast.walk(tree):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in bound:
                    problems.append(f"{m}.py:{n.lineno} undefined name {n.id}")
                if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                        and aliases.get(n.value.id) in tops and n.attr not in tops[aliases[n.value.id]]):
                    problems.append(f"{m}.py:{n.lineno} missing attribute {n.value.id}.{n.attr}")
                if isinstance(n, ast.ImportFrom) and n.level == 1 and n.module in tops:
                    problems += [f"{m}.py:{n.lineno} cannot import {a.name} from .{n.module}"
                                 for a in n.names if a.name not in tops[n.module]]
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
