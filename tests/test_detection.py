# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

"""Detection, version and platform-API naming checks (fast, no FFmpeg needed)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from modules import classification, constants  # noqa: E402

MOV_FAMILY = "mov,mp4,m4a,3gp,3g2,mj2"


class ContainerClassTests(unittest.TestCase):
    def test_known_brands(self) -> None:
        self.assertEqual(classification.container_class(MOV_FAMILY, "isom"), "mp4")
        self.assertEqual(classification.container_class(MOV_FAMILY, "mp42"), "mp4")
        self.assertEqual(classification.container_class(MOV_FAMILY, "qt  "), "mov")
        self.assertEqual(classification.container_class(MOV_FAMILY, "M4V "), "m4v")
        self.assertEqual(classification.container_class(MOV_FAMILY, "3gp4"), "3gp")

    def test_unknown_brand_and_extension_stay_unknown(self) -> None:
        self.assertIsNone(classification.container_class(MOV_FAMILY, None, ".xyz"))
        self.assertIsNone(classification.container_class(MOV_FAMILY, "weird", None))

    def test_extension_is_used_only_without_a_known_brand(self) -> None:
        self.assertEqual(classification.container_class(MOV_FAMILY, None, ".mov"), "mov")
        self.assertEqual(classification.container_class(MOV_FAMILY, "weird", ".mp4"), "mp4")

    def test_missing_format_name(self) -> None:
        self.assertIsNone(classification.container_class(None))
        self.assertIsNone(classification.container_class("  "))


class MissingColorMessageTests(unittest.TestCase):
    def test_missing_values_are_reported_as_unspecified_not_none(self) -> None:
        media = {"ColorTransfer": "bt2020-10", "ColorPrimaries": None, "ColorSpace": None,
                 "BitDepthNumeric": 8, "Codec": "h264", "ScanType": "Progressive"}
        result = classification.classify_normalized_media(media)
        self.assertEqual(result["Pool"], "SDR Uncertain")
        self.assertIn("reported unspecified", result["UnknownReason"])
        self.assertNotIn("reported none", result["UnknownReason"])

    def test_missing_transfer_is_not_invented_as_sdr(self) -> None:
        media = {"ColorTransfer": None, "ColorPrimaries": None, "ColorSpace": None,
                 "BitDepthNumeric": 8, "Codec": "h264", "ScanType": "Progressive"}
        result = classification.classify_normalized_media(media)
        self.assertEqual(result["Pool"], "SDR Uncertain")
        self.assertIn("MissingMetadata", result["Anomalies"])


class VersionTests(unittest.TestCase):
    def test_version_is_1_0_0_everywhere(self) -> None:
        self.assertEqual(constants.APP_VERSION, "1.0.0")
        for path in sorted((REPO / "locales").glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["_meta.version"], constants.APP_VERSION, path.name)

    def test_no_old_constant_name_left(self) -> None:
        for path in (REPO / "modules").glob("*.py"):
            self.assertNotIn("PROGRAM_VERSION", path.read_text(encoding="utf-8"), path.name)


class PlatformApiNamingTests(unittest.TestCase):
    NAMES = ("create_instance_lock", "subprocess_process_options", "is_link_or_reparse_point")

    def test_adapters_and_protocol_use_the_shared_names(self) -> None:
        sources = {name: (REPO / "modules" / name).read_text(encoding="utf-8")
                   for name in ("platform_api.py", "platform_linux.py", "platform_windows.py")}
        for name, text in sources.items():
            for func in self.NAMES:
                self.assertIn(f"def {func}", text, f"{name} lacks {func}")
            self.assertNotIn("subprocess_creation_kwargs", text, name)
            self.assertNotIn("is_path_link", text, name)

    def test_instance_lock_takes_a_path(self) -> None:
        import inspect
        from modules import platform_api
        signature = inspect.signature(platform_api.PlatformAdapter.create_instance_lock)
        self.assertIn("path", signature.parameters)


class HeaderTests(unittest.TestCase):
    def test_every_python_file_has_the_author_header(self) -> None:
        for path in sorted(REPO.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            head = path.read_text(encoding="utf-8").splitlines()[:3]
            self.assertEqual(head, ["# Media Diversity Sampler", "# Author: GlacialPigeon",
                                    "# GitHub: https://github.com/GlacialPigeon"], str(path.relative_to(REPO)))


if __name__ == "__main__":
    unittest.main()
