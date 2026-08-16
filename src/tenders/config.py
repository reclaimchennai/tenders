"""Configuration loading and path resolution.

Reads ``config.toml`` from the project root. Relative paths in the ``[paths]``
section are resolved against the project root so the tools work regardless of
the current working directory.
"""

from __future__ import annotations

import functools
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - fallback for very old runtimes
    import tomli as tomllib  # type: ignore


# Project root = two levels up from this file (src/tenders/config.py -> root).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.environ.get("TENDERS_CONFIG", PROJECT_ROOT / "config.toml"))


@dataclass(frozen=True)
class Config:
    raw: dict

    # --- site ---
    @property
    def base_url(self) -> str:
        return self.raw["site"]["base_url"]

    @property
    def host(self) -> str:
        return self.raw["site"]["host"]

    @property
    def active_page(self) -> str:
        return self.raw["site"]["active_page"]

    @property
    def archive_page(self) -> str:
        return self.raw["site"]["archive_page"]

    @property
    def active_org_url(self) -> str:
        return self.raw["site"]["active_org_page"]

    # --- paths (resolved absolute) ---
    def _path(self, key: str) -> Path:
        p = Path(self.raw["paths"][key])
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    @property
    def db_path(self) -> Path:
        return self._path("db")

    @property
    def docs_dir(self) -> Path:
        return self._path("docs")

    @property
    def html_dir(self) -> Path:
        return self._path("html")

    @property
    def captcha_dir(self) -> Path:
        return self._path("captcha")

    # --- sections passed through as dicts ---
    @property
    def scrape(self) -> dict:
        return self.raw["scrape"]

    @property
    def forward(self) -> dict:
        return self.raw["forward"]

    @property
    def ocr(self) -> dict:
        return self.raw["ocr"]

    @property
    def web(self) -> dict:
        return self.raw["web"]

    def ensure_dirs(self) -> None:
        for d in (self.db_path.parent, self.docs_dir, self.html_dir, self.captcha_dir):
            d.mkdir(parents=True, exist_ok=True)


@functools.lru_cache(maxsize=1)
def load_config() -> Config:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"config not found at {CONFIG_PATH}")
    with open(CONFIG_PATH, "rb") as f:
        return Config(raw=tomllib.load(f))


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
