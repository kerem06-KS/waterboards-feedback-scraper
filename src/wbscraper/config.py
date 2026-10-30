"""Settings and term-library loading. All tunables live in YAML under config/."""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .dates import parse_date_to_epoch


def find_config_dir() -> Path:
    env = os.environ.get("WBSCRAPER_CONFIG_DIR")
    if env:
        return Path(env)
    cwd_cfg = Path.cwd() / "config"
    if (cwd_cfg / "settings.yaml").exists():
        return cwd_cfg
    return Path(__file__).resolve().parents[2] / "config"


def load_yaml(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_dotenv(path: Path | None = None) -> None:
    """Tiny .env reader (KEY=VALUE lines). Existing environment variables win."""
    path = path or Path.cwd() / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and val and key not in os.environ:
            os.environ[key] = val


@dataclass
class Settings:
    raw: dict
    config_dir: Path
    data_dir: Path
    since_ts: int
    until_ts: int | None

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict):
                return default
            if part in cur:
                cur = cur[part]
            elif part.isdigit() and int(part) in cur:      # YAML integer keys (e.g. tier numbers)
                cur = cur[int(part)]
            else:
                return default
        return cur

    # convenient paths -------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "state" / "collection.sqlite3"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "output"

    @property
    def csv_path(self) -> Path:
        return self.output_dir / "waterboards_feedback.csv"

    def domain_rate(self, domain: str) -> tuple[float, float]:
        rates = self.get("domains", {})
        entry = rates.get(domain) or rates.get("default") or {"interval": 3.0, "jitter": 0.5}
        return float(entry.get("interval", 3.0)), float(entry.get("jitter", 0.5))

    def source_enabled(self, name: str) -> bool:
        return bool(self.get(f"sources.{name}.enabled", True))


def load_settings(config_dir: Path | None = None, overrides: dict | None = None) -> Settings:
    config_dir = Path(config_dir) if config_dir else find_config_dir()
    raw = load_yaml(config_dir / "settings.yaml") if (config_dir / "settings.yaml").exists() else {}
    env_dir = os.environ.get("WBSCRAPER_DATA_DIR")
    if env_dir:                       # environment is a default; explicit overrides (CLI flags) win
        raw = deep_merge(raw, {"paths": {"data_dir": env_dir}})
    if overrides:
        raw = deep_merge(raw, overrides)
    data_dir = Path(raw.get("paths", {}).get("data_dir", "data"))
    dr = raw.get("date_range", {}) or {}
    since = parse_date_to_epoch(dr.get("since") or "2005-06-01") or 1117584000
    until = parse_date_to_epoch(dr.get("until")) if dr.get("until") else None
    forums_path = config_dir / "sources" / "forums.yaml"
    if forums_path.exists():          # Phase 2 per-source sections are exposed through Settings.get() as well
        for key, val in (load_yaml(forums_path) or {}).items():
            raw.setdefault(key, val)
    return Settings(raw=raw, config_dir=config_dir, data_dir=data_dir, since_ts=since, until_ts=until)
