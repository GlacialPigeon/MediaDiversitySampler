# Media Diversity Sampler
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import json
import string
from pathlib import Path
from typing import Any, Optional

from .constants import LOCALES_DIR, APP_VERSION
from .logger import write_sampler_log

CANONICAL_LOCALE = "en-US"
LOCALES: Optional["LocaleManager"] = None
_PLURAL_CATEGORIES = {"one", "two", "few", "many", "other"}


class LocaleError(RuntimeError):
    pass


def _load_locale_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise LocaleError(f"Locale file not found: {path}")

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise LocaleError(f"Duplicate locale key: {key}")
            result[key] = value
        return result

    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh, object_pairs_hook=no_duplicates)
    except LocaleError:
        raise
    except Exception as exc:
        raise LocaleError(f"Could not read locale file {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise LocaleError(f"Locale root must be a JSON object: {path.name}")
    return data


def _template_fields(value: str) -> set[str]:
    fields: set[str] = set()
    try:
        for _, field_name, _, _ in string.Formatter().parse(value):
            if field_name is None:
                continue  # literal text without a replacement field
            base = field_name.split(".", 1)[0].split("[", 1)[0]
            if not base or base.isdigit():
                # t() only passes keyword arguments, so "{}" / "{0}" would fail at runtime.
                raise LocaleError(f"Positional placeholders are not allowed (use named fields): {value!r}")
            fields.add(base)
    except LocaleError:
        raise
    except Exception as exc:
        raise LocaleError(f"Invalid format template: {value!r}: {exc}") from exc
    return fields


def _validate_locale_entry(key: str, value: Any) -> None:
    if key.startswith("_meta."):
        if not isinstance(value, str) or not value.strip():
            raise LocaleError(f"Locale metadata must be a non-empty string: {key}")
        return
    if isinstance(value, str):
        _template_fields(value)
        return
    if isinstance(value, dict):
        if not value:
            raise LocaleError(f"Plural locale entry is empty: {key}")
        reference_fields: Optional[set[str]] = None
        for category, template in value.items():
            if category not in _PLURAL_CATEGORIES:
                raise LocaleError(f"Invalid plural category {category!r} for key {key}")
            if not isinstance(template, str):
                raise LocaleError(f"Plural value must be a string: {key}.{category}")
            fields = _template_fields(template)
            if reference_fields is None:
                reference_fields = fields
            elif fields != reference_fields:
                raise LocaleError(f"Plural placeholder mismatch inside locale key: {key}")
        if "other" not in value:
            raise LocaleError(f"Plural locale entry requires an 'other' form: {key}")
        return
    raise LocaleError(f"Locale value must be a string or plural object: {key}")


def _plural_category(locale_code: str, count: int | float) -> str:
    try:
        n = int(count)
    except Exception:
        return "other"
    language = str(locale_code).split("-", 1)[0].casefold()
    if language == "sl":
        mod100 = n % 100
        if mod100 == 1:
            return "one"
        if mod100 == 2:
            return "two"
        if mod100 in {3, 4}:
            return "few"
        return "other"
    if language == "pl":
        if n == 1:
            return "one"
        mod10, mod100 = n % 10, n % 100
        if mod10 in {2, 3, 4} and mod100 not in {12, 13, 14}:
            return "few"
        return "many"
    if language in {"hr", "bs", "sr"}:
        mod10, mod100 = n % 10, n % 100
        if mod10 == 1 and mod100 != 11:
            return "one"
        if mod10 in {2, 3, 4} and mod100 not in {12, 13, 14}:
            return "few"
        return "other"
    return "one" if n == 1 else "other"


class LocaleManager:
    def __init__(self, locales_dir: Path, canonical_locale: str = CANONICAL_LOCALE) -> None:
        self.locales_dir = Path(locales_dir)
        self.canonical_locale = canonical_locale
        self.canonical: dict[str, Any] = {}
        self.active: dict[str, Any] = {}
        self.active_locale = canonical_locale
        self.active_name = "English"

    @property
    def canonical_path(self) -> Path:
        return self.locales_dir / f"{self.canonical_locale}.json"

    def load_canonical(self) -> None:
        data = _load_locale_json(self.canonical_path)
        for key, value in data.items():
            _validate_locale_entry(str(key), value)
        if data.get("_meta.locale") != self.canonical_locale:
            raise LocaleError(f"Canonical locale metadata mismatch: {data.get('_meta.locale')!r}")
        if data.get("_meta.version") != APP_VERSION:
            raise LocaleError(f"Canonical locale version mismatch: expected {APP_VERSION}, got {data.get('_meta.version')!r}")
        if not isinstance(data.get("_meta.name"), str) or not str(data.get("_meta.name")).strip():
            raise LocaleError("Canonical locale is missing _meta.name")
        self.canonical = data
        self.active = data
        self.active_locale = self.canonical_locale
        self.active_name = str(data["_meta.name"])

    def _validate_override(self, code: str, data: dict[str, Any]) -> None:
        if data.get("_meta.locale") != code:
            raise LocaleError(f"Locale metadata mismatch in {code}.json")
        if not isinstance(data.get("_meta.name"), str) or not str(data.get("_meta.name")).strip():
            raise LocaleError(f"Locale {code} is missing _meta.name")
        for key, value in data.items():
            _validate_locale_entry(str(key), value)
            if key.startswith("_meta."):
                continue
            if key not in self.canonical:
                raise LocaleError(f"Unknown locale key {key!r} in {code}.json")
            canonical_value = self.canonical[key]
            if isinstance(canonical_value, str):
                if not isinstance(value, str):
                    raise LocaleError(f"Locale key type mismatch: {key}")
                if _template_fields(value) != _template_fields(canonical_value):
                    raise LocaleError(f"Placeholder mismatch for locale key: {key}")
            elif isinstance(canonical_value, dict):
                if not isinstance(value, dict):
                    raise LocaleError(f"Locale key type mismatch: {key}")
                reference_fields = _template_fields(str(canonical_value.get("other", "")))
                for template in value.values():
                    if _template_fields(str(template)) != reference_fields:
                        raise LocaleError(f"Placeholder mismatch for plural locale key: {key}")

    def activate(self, code: str) -> bool:
        requested = str(code or self.canonical_locale)
        if requested == self.canonical_locale:
            self.active = self.canonical
            self.active_locale = self.canonical_locale
            self.active_name = str(self.canonical.get("_meta.name", "English"))
            return True
        path = self.locales_dir / f"{requested}.json"
        if not path.is_file():
            self.active = self.canonical
            self.active_locale = self.canonical_locale
            self.active_name = str(self.canonical.get("_meta.name", "English"))
            return False
        data = _load_locale_json(path)
        self._validate_override(requested, data)
        self.active = data
        self.active_locale = requested
        self.active_name = str(data["_meta.name"])
        return True

    def discover(self) -> list[dict[str, str]]:
        locales: list[dict[str, str]] = []
        if not self.locales_dir.is_dir():
            return locales
        for path in sorted(self.locales_dir.glob("*.json"), key=lambda p: p.name.casefold()):
            code = path.stem
            try:
                if code == self.canonical_locale:
                    data = self.canonical
                else:
                    data = _load_locale_json(path)
                    self._validate_override(code, data)
                locales.append({"Locale": code, "Name": str(data.get("_meta.name") or code)})
            except Exception as exc:
                write_sampler_log(f"Ignoring invalid locale file path={path} error={exc}", "WARNING", "LOCALE", force=True)
        if not any(item["Locale"] == self.canonical_locale for item in locales):
            locales.insert(0, {"Locale": self.canonical_locale, "Name": str(self.canonical.get("_meta.name", "English"))})
        return locales

    def _value(self, key: str) -> Any:
        if key in self.active and not key.startswith("_meta."):
            return self.active[key]
        if key in self.canonical:
            return self.canonical[key]
        raise LocaleError(f"Missing canonical locale key: {key}")

    def text(self, key: str, **kwargs: Any) -> str:
        value = self._value(key)
        if not isinstance(value, str):
            raise LocaleError(f"Locale key requires plural(): {key}")
        try:
            return value.format(**kwargs)
        except Exception as exc:
            raise LocaleError(f"Could not format locale key {key}: {exc}") from exc

    def plural(self, key: str, count: int | float, **kwargs: Any) -> str:
        active_value = self.active.get(key)
        canonical_value = self.canonical.get(key)
        if not isinstance(canonical_value, dict):
            raise LocaleError(f"Locale key is not plural: {key}")
        category = _plural_category(self.active_locale, count)
        template: Optional[str] = None
        if isinstance(active_value, dict):
            template = active_value.get(category) or active_value.get("other")
        if template is None:
            template = canonical_value.get(category) or canonical_value.get("other")
        if not isinstance(template, str):
            raise LocaleError(f"No plural form available for locale key: {key}")
        values = dict(kwargs)
        values.setdefault("count", count)
        try:
            return template.format(**values)
        except Exception as exc:
            raise LocaleError(f"Could not format plural locale key {key}: {exc}") from exc


def initialize_localization() -> None:
    global LOCALES
    manager = LocaleManager(LOCALES_DIR)
    manager.load_canonical()
    LOCALES = manager


def activate_configured_locale(code: str) -> None:
    if LOCALES is None:
        raise LocaleError("Localization is not initialized.")
    requested = str(code or CANONICAL_LOCALE)
    try:
        activated = LOCALES.activate(requested)
    except LocaleError as exc:
        LOCALES.activate(CANONICAL_LOCALE)
        write_sampler_log(f"Configured locale invalid; fallback to {CANONICAL_LOCALE}: requested={requested} error={exc}", "WARNING", "LOCALE", force=True)
        activated = False
    if not activated and requested != CANONICAL_LOCALE:
        write_sampler_log(f"Configured locale unavailable; fallback to {CANONICAL_LOCALE}: requested={requested}", "WARNING", "LOCALE", force=True)
    write_sampler_log(f"Active locale={LOCALES.active_locale} name={LOCALES.active_name!r}", "INFO", "LOCALE", force=True)


def t(key: str, **kwargs: Any) -> str:
    if LOCALES is None:
        raise LocaleError("Localization is not initialized.")
    return LOCALES.text(key, **kwargs)


def tp(key: str, count: int | float, **kwargs: Any) -> str:
    if LOCALES is None:
        raise LocaleError("Localization is not initialized.")
    return LOCALES.plural(key, count, **kwargs)


def display_mode_name(mode: str) -> str:
    key = {"ALL": "mode.all", "SDR": "mode.sdr", "HDR": "mode.hdr", "UNCERTAIN": "mode.uncertain"}.get(str(mode).upper())
    return t(key) if key else str(mode)


def display_pool_name(pool: str) -> str:
    key = {"SDR": "pool.sdr", "HDR": "pool.hdr", "SDR UNCERTAIN": "pool.sdr_uncertain", "HDR UNCERTAIN": "pool.hdr_uncertain"}.get(str(pool).upper())
    return t(key) if key else str(pool)
