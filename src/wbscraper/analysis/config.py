"""Loads config/analysis/*.yaml."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import deep_merge, load_yaml

LABELS = ("negative", "neutral", "positive")


@dataclass
class AnalysisConfig:
    settings: dict
    lexicon: dict
    tasks: list[dict]
    pain_points: list[dict]
    config_dir: Path
    data_dir: Path = field(default_factory=lambda: Path("data"))

    # -- sentiment ----------------------------------------------------------------------------------------
    @property
    def sentiment(self) -> dict:
        return self.settings.get("sentiment") or {}

    @property
    def thresholds(self) -> tuple[float, float]:
        t = self.sentiment.get("thresholds") or {}
        neg, pos = float(t.get("negative", -0.05)), float(t.get("positive", 0.05))
        if neg >= pos:
            raise ValueError(f"sentiment thresholds must satisfy negative < positive (got {neg} and {pos})")
        return neg, pos

    # -- tags -------------------------------------------------------------------------------------------
    @property
    def tagging(self) -> dict:
        return self.settings.get("tagging") or {}

    @property
    def task_names(self) -> list[str]:
        return [t["name"] for t in self.tasks]

    @property
    def pain_point_names(self) -> list[str]:
        return [p["name"] for p in self.pain_points]

    def path(self, key: str) -> Path:
        rel = (self.settings.get("paths") or {}).get(key)
        if not rel:
            raise KeyError(f"no analysis path configured for {key!r}")
        p = Path(rel)
        return p if p.is_absolute() else self.data_dir / p


def load_analysis_config(config_dir: Path, data_dir: Path | None = None, overrides: dict | None = None) -> AnalysisConfig:
    base = Path(config_dir) / "analysis"
    settings = load_yaml(base / "settings.yaml") if (base / "settings.yaml").exists() else {}
    if overrides:
        settings = deep_merge(settings, overrides)
    lexicon = load_yaml(base / "lexicon.yaml") if (base / "lexicon.yaml").exists() else {}
    tags = load_yaml(base / "tags.yaml") if (base / "tags.yaml").exists() else {}
    return AnalysisConfig(settings=settings, lexicon=lexicon, tasks=list(tags.get("tasks") or []),
                          pain_points=list(tags.get("pain_points") or []), config_dir=Path(config_dir),
                          data_dir=Path(data_dir) if data_dir else Path("data"))
