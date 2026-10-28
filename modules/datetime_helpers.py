# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import datetime as dt
import os
import re
import time


def utc_now_o() -> str:
    ns = time.time_ns()
    sec, rem = divmod(ns, 1_000_000_000)
    base = dt.datetime.fromtimestamp(sec, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{rem // 100:07d}Z"


def parse_utc_timestamp(value: str) -> dt.datetime:
    """Parse the 7-fractional-digit 'O' timestamps written by utc_now_o()/stat_mtime_o().

    datetime.fromisoformat() only accepts 3 or 6 fractional digits before Python 3.11, so the
    fraction is normalised to exactly 6 digits first. Raises ValueError for invalid values.
    """
    text = str(value).strip().replace("Z", "+00:00")
    text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    return dt.datetime.fromisoformat(text)


def stat_mtime_o(st: os.stat_result) -> str:
    ns = st.st_mtime_ns
    sec, rem = divmod(ns, 1_000_000_000)
    base = dt.datetime.fromtimestamp(sec, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{rem // 100:07d}Z"
