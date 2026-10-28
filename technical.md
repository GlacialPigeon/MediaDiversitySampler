# Media Diversity Sampler: technical reference

The short introduction and the setup are in the [README](README.md). This file holds the details: how files are classified, the platform design, what is saved, localization and the project layout.

> Version `1.0.0`.

## What it does, step by step

1. **Scan**: every configured media folder is listed, and each video file is probed with FFprobe. The results are kept in a SQLite cache (`data/probe_cache.db`), keyed by storage identity and path, with size and modification time as the check for a change, so a repeated scan probes only new and changed files. A damaged cache is moved to `data/recovery/` and rebuilt.
2. **Classify**: each file goes into one of four pools: `SDR`, `HDR`, `SDR Uncertain`, `HDR Uncertain`. The *Uncertain* selection covers both uncertain pools.
3. **Analyze diversity**: the program counts the distinct values and combinations that exist in the library (container, codecs, resolution class, bit depth, HDR type, audio and subtitle properties, ...) and computes the *recommended minimum*, the number of files needed to cover them.
4. **Select** exactly once, after you have entered the quantity. The selection is deterministic: the same library and the same quantity always give the same files. Candidates are scored by how many new values and combinations they add; only when none adds anything new does *balanced fill* spread the remaining count.
5. **Preview** (nothing is written) or **Build / Resume**: the selected files are copied with `shutil.copy2` into an output library. The build plan is written first as staged manifests and only activated when the whole plan is verified; each copied file is checked by its length, so an interrupted build can be resumed and a half-written file is copied again.

The original files are opened for reading only.

## No invented values

A value FFprobe does not report stays unknown. Examples:

* A missing colour primaries, transfer or matrix value is shown as *reported unspecified*, never as "none" or a default.
* A `bt2020` file with no transfer function is **HDR Uncertain** / **SDR Uncertain**, not SDR and not HDR.
* The container class of a QuickTime-family file comes from the `major_brand`. An unknown brand falls back to the file extension, and when that is unknown too, the class stays unknown. Known MP4 brands: `iso*`, `mp4*`, `mp7*`, `avc*`, `dash`, `cmf*`, `msnv`, `f4v`, `mmp4`, `xavc`, `nd*`.
* Bit depth is the explicit stream value, or the depth of the pixel format when there is none. When the two disagree (an explicit depth larger than the pixel format allows), the depth is unknown.
* Resolution classes (SD, 720p, 1080p, 1440p, 4K, 8K) look at both sides of the picture, so a cropped widescreen file still gets the class of its width; a missing or zero size gives no class.
* NAS metadata and recycle folders (`@eaDir`, `#recycle`, `#snapshot`, `@Recycle`, `.@__thumb`) are never scanned.

## Platforms

`main.py` is the only place that selects the platform adapter. Everything specific to an operating system lives in two modules; core modules do not implement WinAPI, `.lnk`/UNC/mapped-drive resolution, volume GUIDs, reparse points, `CREATE_NO_WINDOW`, `/proc/self/mountinfo` parsing or `flock`. A test enforces this.

```
modules/platform_api.py       the contract (a Protocol) and thin module-level functions
modules/platform_windows.py   Windows adapter
modules/platform_linux.py     Linux adapter
```

The adapters of the three tools (Media Cataloger, Media Diversity Sampler, Media Trickplay Generator) use the same names for what they share: `create_instance_lock(path)`, `subprocess_process_options()` and `is_link_or_reparse_point(path)`.

### Windows

* FFprobe only from `ffmpeg\bin\ffprobe.exe` next to `main.py`; `PATH` is never searched.
* UNC paths, mapped drives, `.lnk` Network Shortcuts and volume identity resolve to one canonical place, so `Z:\Movies` and `\\server\share\Movies` are the same source.
* Second instances are refused with a global named mutex (`Global\MediaDiversitySampler_GlobalInstance`).

### Linux

* FFprobe from `ffmpeg/bin/ffprobe` next to `main.py` first, then the system `ffprobe` (`sudo apt install ffmpeg`, `sudo dnf install ffmpeg-free`).
* Storage identity comes from `/proc/self/mountinfo`: local filesystems by UUID, CIFS and NFS by server and share (IPv6 aware, host names compared case-insensitively), bind mounts by the mount `root`, and systemd automounts are triggered before they are read.
* The instance lock is an `flock` on `MediaDiversitySampler_<uid>.lock`, opened with `O_NOFOLLOW` and checked to be a regular file owned by the user. It lives in `XDG_RUNTIME_DIR` when that is private to the user, otherwise in the temp folder. The Windows adapter ignores the path it is given, the Linux one too; the argument exists so that all three tools share one signature.
* POSIX path rules: case-sensitive, `~` expanded.
* The Linux adapter has been exercised by synthetic mountinfo tests; real CIFS and NFS shares and a real Debian run are still the acceptance step to do on your own system.

## Saved files

All of it next to `main.py`; nothing is written elsewhere.

```
data/config.json          settings, sources, outputs (atomic writes, a damaged file is recovered)
data/probe_cache.db       probe cache
data/recovery/            damaged files moved aside
pathlists/                exported source lists
logs/                     session logs and probe_failed.log
```

Writes are atomic: a temporary file in the same folder, `fsync`, `os.replace`, then an `fsync` of the folder. The permissions of a replaced file are kept.

Schema versions: config 4, cache 1, probe 1, manifest 3, library marker 1. Output libraries carry a marker file `.mediadiversity-library.json` so a folder that is not a library of this tool is never written into.

## Localization

`locales/en-US.json` is canonical and holds every text; `sl-SI.json` (Slovenščina) is included. The language menu shows only `_meta.name`.

To add a language, copy `en-US.json`, set `_meta.code`, `_meta.locale` and `_meta.name`, and translate the values, not the keys. `_meta.version` must equal the program version, `_meta.thousands` and `_meta.decimal` give the number format. Texts with a number have plural forms (`one`, `two`, `few`, `many`, `other`; `other` is required). An entry that does not fit (a changed `{placeholder}`, text where a plural form belongs) is ignored alone and shown in English.

Technical logs and the saved files always stay in English.

## Project layout

```
main.py                  entry point; selects the platform adapter
modules/
  application.py         startup, instance lock, shutdown
  ui.py                  console menus and orchestration
  scan.py ffprobe.py     inventory, FFprobe run and parsing
  probe_cache.py         SQLite probe cache and recovery
  classification.py      SDR / HDR / Uncertain and the descriptive classes
  selection.py           diversity analysis and the deterministic selection
  build.py               manifest, preflight, copy, validation, resume
  paths.py               source and output business flow
  config.py file_io.py   configuration and atomic file writes
  localization.py logger.py interrupts.py datetime_helpers.py constants.py
  platform_api.py platform_windows.py platform_linux.py
locales/                 en-US.json (canonical), sl-SI.json
tests/                   unittest suite
```

There is deliberately no catch-all `utils.py`. The build fingerprint written into logs covers `main.py`, `modules/**/*.py` and the locale files.

## Tests

```
python3 -m unittest discover -s tests -v
```

Fast, no FFmpeg needed (a few Linux adapter tests are skipped on other systems). They cover classification and the "unknown stays unknown" rules, the selection, builds and resume, persistence, localization and the platform design.

## Design notes

* [Final specification (v1.1.1)](Media_Diversity_Sampler_FINAL_SPECIFICATION_v1.1.1.md), the original functional specification the selection is frozen to.
* [Changes of v2.0.1](CHANGES_v2.0.1.md) and [its verification](VERIFICATION_v2.0.1.md), the history of the module split.

## License

See [LICENSE](LICENSE).
