"""CastleDB sheet loader.

The game's data tables were exported to data/sheets/<name>.json, each shaped
`{"lines": [ {row}, ... ]}` (the CastleDB sheet format). This is the single
reader every data module uses, so sheet access is cached and consistent.

Also loads the wiki data layer (htdocs/assets/data/{items,enemies}.json), which
carries display names, aptitudes, and drop attributions the raw CDB lacks.
"""
from __future__ import annotations

import json
from functools import lru_cache

from .. import paths


@lru_cache(maxsize=None)
def sheet(name: str) -> dict:
    """Raw sheet dict for `name` (without the .json), e.g. sheet("item")."""
    path = paths.sheets_dir() / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def lines(name: str) -> list[dict]:
    """The rows of a sheet."""
    return sheet(name).get("lines", [])


@lru_cache(maxsize=None)
def by_id(name: str, key: str = "id") -> dict[str, dict]:
    """Rows of a sheet indexed by their id (or another key)."""
    return {r[key]: r for r in lines(name) if key in r}


@lru_cache(maxsize=None)
def wiki(name: str) -> list[dict]:
    """A wiki data file (items / enemies) as a flat list of rows. Tolerates
    either a top-level list or a dict wrapping one list value."""
    try:
        d = json.loads((paths.wiki_data_dir() / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(d, list):
        return d
    for v in d.values():
        if isinstance(v, list):
            return v
    return []
