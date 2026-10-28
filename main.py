# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import sys

from modules.application import run_application


def main() -> int:
    if sys.platform == "win32":
        from modules.platform_windows import WindowsPlatform
        platform = WindowsPlatform()
        return run_application(platform, platform_name=sys.platform)

    if sys.platform == "linux":
        from modules.platform_linux import LinuxPlatform
        platform = LinuxPlatform()
        return run_application(platform, platform_name=sys.platform)

    # Platform selection belongs only in this entry point. Additional adapters
    # can be added without changing application/core modules.
    raise RuntimeError(f"Unsupported platform: {sys.platform}")


if __name__ == "__main__":
    raise SystemExit(main())
