from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Sequence

from .constants import DEFAULT_MEAL_API, DEFAULT_WEBETU_API

SCRIPT_DIR = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Invalid or missing local configuration."""


class Config:
    def __init__(self) -> None:
        self.username: str = ""
        self.password: str = ""
        self.depot: str = ""
        self.residence: Any = None
        self.wilaya: Any = None
        self.dates: list[str] | None = None
        self.offsets: list[int] | None = None
        self.meals: list[int] | None = None
        self.dry_run: bool = False
        self.force: bool = False
        self.sign: bool = True
        self.timeout: float = 30.0
        self.retries: int = 0
        self.webetu_api: str = DEFAULT_WEBETU_API
        self.meal_api: str = DEFAULT_MEAL_API
        self.log_file: str = ""
        self.verbose: int = 0
        self.list_depots: bool = False
        self.list_reservations: bool = False


def load_dotenv_file(path: Path) -> None:
    """Load KEY=VALUE lines without overriding existing environment variables."""
    try:
        if not path.is_file():
            return
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_dotenv_files() -> None:
    load_dotenv_file(Path.cwd() / ".env")
    if SCRIPT_DIR != Path.cwd():
        load_dotenv_file(SCRIPT_DIR / ".env")


def env_first(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip() != "":
            return value
    return None


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def coerce_id(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        return text
    return value


def parse_int_list(
    value: Any,
    *,
    name: str,
    allowed: set[int] | None = None,
    minimum: int | None = None,
) -> list[int]:
    if value is None:
        raise ConfigError(f"{name} is missing")
    if isinstance(value, (list, tuple)):
        items: list[Any] = list(value)
    else:
        text = str(value).strip()
        if not text:
            raise ConfigError(f"{name} must contain at least one value")
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError as exc:
                raise ConfigError(f"{name} is not a valid JSON list: {exc}") from exc
            if not isinstance(parsed, list):
                raise ConfigError(f"{name} must be a list")
            items = parsed
        else:
            items = [part.strip() for part in text.split(",") if part.strip()]
    result: list[int] = []
    for item in items:
        try:
            if isinstance(item, bool):
                raise ValueError
            number = int(item)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{name} contains a non-integer value: {item!r}") from exc
        if minimum is not None and number < minimum:
            raise ConfigError(f"{name} value {number} is below {minimum}")
        if allowed is not None and number not in allowed:
            allowed_text = ", ".join(str(v) for v in sorted(allowed))
            raise ConfigError(f"{name} value {number} is not one of: {allowed_text}")
        result.append(number)
    if not result:
        raise ConfigError(f"{name} must contain at least one value")
    return list(dict.fromkeys(result))


def parse_dates(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        items: list[Any] = list(value)
    else:
        text = str(value).strip()
        if not text:
            raise ConfigError("dates must contain at least one date")
        items = [part.strip() for part in text.split(",") if part.strip()]
    result: list[str] = []
    for item in items:
        try:
            parsed = dt.date.fromisoformat(str(item).strip())
        except ValueError as exc:
            raise ConfigError(f"invalid date {item!r}; expected YYYY-MM-DD") from exc
        result.append(parsed.isoformat())
    if not result:
        raise ConfigError("dates must contain at least one date")
    return list(dict.fromkeys(result))


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold().strip()


def build_config(args: argparse.Namespace) -> Config:
    cfg = Config()
    data: dict[str, Any] = {}
    config_path = args.config or env_first("PROGRES_CONFIG")
    if config_path is None:
        default_path = SCRIPT_DIR / "progres_reserver.json"
        if default_path.is_file():
            config_path = str(default_path)
    if config_path:
        path = Path(config_path).expanduser()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"cannot read config file {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"config file {path} must contain a JSON object")
        data = loaded

    def first(cli_value: Any, env_names: Sequence[str], key: str, default: Any) -> Any:
        if cli_value is not None:
            return cli_value
        env_value = env_first(*env_names)
        if env_value is not None:
            return env_value
        if key in data and data[key] is not None:
            return data[key]
        return default

    cfg.username = str(first(args.username, ("WEBETU_USERNAME",), "username", "")).strip()
    cfg.password = str(first(args.password, ("WEBETU_PASSWORD",), "password", ""))
    cfg.depot = str(first(args.depot, ("RESTO_DEPOT", "MEAL_DEPOT"), "depot", "")).strip()
    cfg.residence = coerce_id(first(args.residence, ("WEBETU_RESIDENCE", "MEAL_RESIDENCE"), "residence", None))
    cfg.wilaya = coerce_id(first(args.wilaya, ("WEBETU_WILAYA", "MEAL_WILAYA"), "wilaya", None))

    dates_raw = args.dates
    offsets_raw = args.offsets
    if dates_raw is None and offsets_raw is None:
        dates_raw = env_first("RESERVE_DATES")
        offsets_raw = env_first("RESERVE_OFFSETS")
    if dates_raw is None and offsets_raw is None:
        dates_raw = data.get("dates")
        offsets_raw = data.get("offsets")
    if dates_raw not in (None, "", []):
        cfg.dates = parse_dates(dates_raw)
    if offsets_raw not in (None, "", []):
        cfg.offsets = parse_int_list(offsets_raw, name="offsets", minimum=0)
    if cfg.dates and cfg.offsets:
        raise ConfigError("use either dates or offsets, not both")
    if not cfg.dates and not cfg.offsets:
        cfg.offsets = [1, 2, 3]

    meals_raw = first(args.meals, ("RESERVE_MEALS",), "meals", None)
    cfg.meals = parse_int_list(meals_raw if meals_raw is not None else [1, 2, 3], name="meals", allowed={1, 2, 3})

    dry_raw = args.dry_run
    if dry_raw is None:
        env_value = os.environ.get("DRY_RUN")
        dry_raw = as_bool(env_value) if env_value is not None else data.get("dry_run", False)
    cfg.dry_run = as_bool(dry_raw, False)

    force_raw = os.environ.get("FORCE_RESERVATION")
    if force_raw is None:
        force_raw = data.get("force", False)
    cfg.force = bool(args.force) if args.force is not None else as_bool(force_raw, False)

    if args.no_sign:
        cfg.sign = False
    else:
        sign_raw = os.environ.get("WEBETU_SIGN")
        if sign_raw is None:
            sign_raw = data.get("sign", True)
        cfg.sign = as_bool(sign_raw, True)

    timeout_raw = first(args.timeout, ("REQUEST_TIMEOUT",), "timeout", 30.0)
    try:
        cfg.timeout = float(timeout_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid timeout: {timeout_raw!r}") from exc
    if cfg.timeout <= 0:
        raise ConfigError("timeout must be greater than zero")

    retries_raw = first(args.retries, ("REQUEST_RETRIES",), "retries", 0)
    try:
        cfg.retries = int(retries_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid retries value: {retries_raw!r}") from exc
    if cfg.retries < 0:
        raise ConfigError("retries cannot be negative")

    cfg.webetu_api = str(first(None, ("WEBETU_API_BASE",), "webetu_api", DEFAULT_WEBETU_API)).rstrip("/")
    cfg.meal_api = str(first(None, ("MEAL_API_BASE",), "meal_api", DEFAULT_MEAL_API)).rstrip("/")
    for label, url in (("webetu_api", cfg.webetu_api), ("meal_api", cfg.meal_api)):
        if not re.match(r"^https?://", url):
            raise ConfigError(f"{label} must be an http(s) URL: {url!r}")

    if args.no_log_file:
        cfg.log_file = ""
    else:
        log_raw = args.log_file
        if log_raw is None:
            log_raw = env_first("PROGRES_LOG_FILE")
        if log_raw is None:
            log_raw = data.get("log_file", str(SCRIPT_DIR / "progres_reserver.log"))
        cfg.log_file = str(log_raw or "")

    cfg.verbose = args.verbose or 0
    cfg.list_depots = bool(args.list_depots)
    cfg.list_reservations = bool(args.list_reservations)
    return cfg
