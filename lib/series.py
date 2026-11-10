"""Multi-episode series: one series folder, shared assets, one project per episode.

Layout (all under ``PROJECTS_DIR``)::

    projects/
    ├── _series/<series_id>/            series home — hidden from Backlot ("_")
    │   ├── series.json                 identity, seasons, episode index
    │   ├── bible/                      season descriptions, season scripts
    │   │   └── characters/             one character_design JSON per character
    │   ├── shared/
    │   │   ├── references/             character / location reference images
    │   │   ├── voices/voices.json      character -> TTS provider, voice id, settings
    │   │   ├── voices/samples/         reference voice samples
    │   │   ├── music/                  themes, intro/outro
    │   │   └── luts/                   series grade
    │   └── episodes/s01e01 -> ../../<series_id>-s01e01   (symlinks, for browsing)
    └── <series_id>-s01e01/             the real episode = a normal OpenMontage project
        └── project.json                {"series": "<series_id>", "episode": {...}}

Why episodes are real top-level projects: Backlot, checkpoints and every tool
already treat ``projects/<id>/`` as the unit of work, and Backlot groups
projects by the ``series`` label. Keeping that contract means this module
needs no changes to any existing file.

Shared assets are referenced by absolute path from an episode's
``asset_manifest`` — never copied — so a new reference or voice setting
reaches every episode.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import jsonschema

from lib.paths import PROJECTS_DIR

SERIES_DIRNAME = "_series"
SERIES_FILE = "series.json"
VOICES_FILE = Path("shared") / "voices" / "voices.json"
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas" / "series"

SERIES_SUBDIRS = (
    "bible/characters",
    "shared/references",
    "shared/voices/samples",
    "shared/music",
    "shared/luts",
    "episodes",
)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class SeriesError(ValueError):
    """Invalid series operation or data."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _projects(base: Optional[Path]) -> Path:
    return Path(base) if base else PROJECTS_DIR


def _schema(name: str) -> dict[str, Any]:
    with open(SCHEMA_DIR / f"{name}.schema.json", encoding="utf-8") as f:
        return json.load(f)


def _validate(data: dict[str, Any], schema: str) -> None:
    try:
        jsonschema.validate(instance=data, schema=_schema(schema))
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        raise SeriesError(f"{schema} invalid at {where}: {exc.message}") from exc


def _read(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_id(series_id: str) -> None:
    if not _ID_RE.match(series_id or ""):
        raise SeriesError(f"series_id must be kebab-case [a-z0-9-], got {series_id!r}")


def episode_code(season: int, episode: int) -> str:
    if season < 1 or episode < 1:
        raise SeriesError("season and episode numbers start at 1")
    return f"s{season:02d}e{episode:02d}"


def episode_project_id(series_id: str, season: int, episode: int) -> str:
    return f"{series_id}-{episode_code(season, episode)}"


# ---------------------------------------------------------------------------
# series
# ---------------------------------------------------------------------------


def series_dir(series_id: str, projects_dir: Optional[Path] = None) -> Path:
    _check_id(series_id)
    return _projects(projects_dir) / SERIES_DIRNAME / series_id


def init_series(
    series_id: str,
    *,
    title: str,
    logline: str = "",
    style_playbook: Optional[str] = None,
    pipeline_type: Optional[str] = None,
    projects_dir: Optional[Path] = None,
) -> Path:
    """Create (or idempotently refresh) a series home. Returns its directory."""
    root = series_dir(series_id, projects_dir)
    for sub in SERIES_SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)

    path = root / SERIES_FILE
    data = _read(path) if path.exists() else {
        "version": "1.0", "series_id": series_id, "created_at": _now(),
        "seasons": [], "episodes": [],
    }
    data["title"] = title
    if logline:
        data["logline"] = logline
    if style_playbook is not None:
        data["style_playbook"] = style_playbook
    if pipeline_type is not None:
        data["pipeline_type"] = pipeline_type
    _validate(data, "series")
    _write(path, data)

    voices = root / VOICES_FILE
    if not voices.exists():
        _write(voices, {"version": "1.0", "series_id": series_id, "voices": {}})
    return root


def load_series(series_id: str, projects_dir: Optional[Path] = None) -> dict[str, Any]:
    path = series_dir(series_id, projects_dir) / SERIES_FILE
    if not path.is_file():
        raise SeriesError(f"unknown series {series_id!r} (no {path})")
    data = _read(path)
    _validate(data, "series")
    return data


def list_series(projects_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    root = _projects(projects_dir) / SERIES_DIRNAME
    if not root.is_dir():
        return []
    out = []
    for entry in sorted(root.iterdir()):
        if (entry / SERIES_FILE).is_file():
            out.append(_read(entry / SERIES_FILE))
    return out


def add_season(
    series_id: str, number: int, *, title: str = "", projects_dir: Optional[Path] = None
) -> dict[str, Any]:
    """Register a season and create its bible stubs (never overwrites them)."""
    root = series_dir(series_id, projects_dir)
    data = load_series(series_id, projects_dir)
    if number < 1:
        raise SeriesError("season numbers start at 1")
    tag = f"season-{number:02d}"
    season = next((s for s in data["seasons"] if s["number"] == number), None)
    if season is None:
        season = {"number": number}
        data["seasons"].append(season)
        data["seasons"].sort(key=lambda s: s["number"])
    if title:
        season["title"] = title
    season["description"] = f"bible/{tag}.md"
    season["script"] = f"bible/{tag}-script.md"
    stubs = {
        season["description"]: f"# {data['title']} — Season {number}{': ' + title if title else ''}\n\n"
                               "## Premise\n\n## Arcs\n\n## Tone and look\n\n## Episodes\n",
        season["script"]: f"# {data['title']} — Season {number} script\n\n",
    }
    for rel, text in stubs.items():
        stub = root / rel
        if not stub.exists():
            stub.write_text(text, encoding="utf-8")
    _validate(data, "series")
    _write(root / SERIES_FILE, data)
    return season


# ---------------------------------------------------------------------------
# episodes
# ---------------------------------------------------------------------------


def init_episode(
    series_id: str,
    season: int,
    episode: int,
    *,
    title: str,
    pipeline_type: Optional[str] = None,
    style_playbook: Optional[str] = None,
    projects_dir: Optional[Path] = None,
) -> Path:
    """Create an episode project, label it with the series, index it.

    The episode is an ordinary project at ``projects/<series>-sXXeYY/`` so
    the whole pipeline/Backlot stack works unchanged; a browsing symlink is
    added under the series' ``episodes/``. Idempotent.
    """
    from lib.checkpoint import PROJECT_MARKER_FILENAME, init_project

    base = _projects(projects_dir)
    root = series_dir(series_id, projects_dir)
    data = load_series(series_id, projects_dir)
    if not any(s["number"] == season for s in data["seasons"]):
        add_season(series_id, season, projects_dir=projects_dir)
        data = load_series(series_id, projects_dir)

    pipeline = pipeline_type or data.get("pipeline_type")
    if not pipeline:
        raise SeriesError("pipeline_type is required (or set it on the series)")
    code = episode_code(season, episode)
    project_id = episode_project_id(series_id, season, episode)

    project_dir = init_project(
        project_id, title=title, pipeline_type=pipeline,
        pipeline_dir=base, style_playbook=style_playbook or data.get("style_playbook"),
        series=series_id,
    )
    marker_path = project_dir / PROJECT_MARKER_FILENAME
    marker = _read(marker_path)
    marker["episode"] = {"season": season, "episode": episode, "code": code,
                         "series_dir": str(root.resolve())}
    _write(marker_path, marker)

    link = root / "episodes" / code
    if not link.exists() and not link.is_symlink():
        try:
            link.symlink_to(os.path.relpath(project_dir, link.parent), target_is_directory=True)
        except OSError:
            pass  # symlinks unavailable (e.g. Windows without dev mode): index still works

    entry = {"season": season, "episode": episode, "code": code,
             "project_id": project_id, "title": title}
    data["episodes"] = [e for e in data["episodes"] if e["code"] != code] + [entry]
    data["episodes"].sort(key=lambda e: (e["season"], e["episode"]))
    _validate(data, "series")
    _write(root / SERIES_FILE, data)
    return project_dir


def series_of_project(project_dir: Path) -> Optional[str]:
    """Series id of an episode project (from its project.json), else None."""
    marker = Path(project_dir) / "project.json"
    if not marker.is_file():
        return None
    value = _read(marker).get("series")
    return value.strip() if isinstance(value, str) and value.strip() else None


def episode_context(project_dir: Path, projects_dir: Optional[Path] = None) -> dict[str, Any]:
    """Everything an agent must read before working on an episode.

    Returns absolute paths, so they can go straight into asset_manifest
    entries and tool inputs.
    """
    project_dir = Path(project_dir)
    series_id = series_of_project(project_dir)
    if not series_id:
        raise SeriesError(f"{project_dir.name} is not part of a series")
    root = series_dir(series_id, projects_dir).resolve()
    data = load_series(series_id, projects_dir)
    marker = _read(project_dir / "project.json")
    season_no = (marker.get("episode") or {}).get("season")
    season = next((s for s in data["seasons"] if s["number"] == season_no), {})
    shared = root / "shared"
    return {
        "series_id": series_id,
        "series_title": data["title"],
        "series_dir": str(root),
        "episode": marker.get("episode"),
        "season_description": str(root / season["description"]) if season.get("description") else None,
        "season_script": str(root / season["script"]) if season.get("script") else None,
        "characters": sorted(str(p) for p in (root / "bible" / "characters").glob("*.json")),
        "voices_file": str(root / VOICES_FILE),
        "references_dir": str(shared / "references"),
        "music_dir": str(shared / "music"),
        "luts_dir": str(shared / "luts"),
        "previous_episodes": [
            e for e in data["episodes"]
            if (e["season"], e["episode"]) < (season_no or 0, (marker.get("episode") or {}).get("episode", 0))
        ],
    }


# ---------------------------------------------------------------------------
# voices
# ---------------------------------------------------------------------------


def load_voices(series_id: str, projects_dir: Optional[Path] = None) -> dict[str, Any]:
    path = series_dir(series_id, projects_dir) / VOICES_FILE
    if not path.is_file():
        return {"version": "1.0", "series_id": series_id, "voices": {}}
    data = _read(path)
    _validate(data, "voices")
    return data


def set_voice(
    series_id: str, character: str, *, provider: str, voice_id: str,
    model: Optional[str] = None, settings: Optional[dict[str, Any]] = None,
    sample: Optional[str] = None, notes: str = "", projects_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Lock a character's voice for the whole series."""
    data = load_voices(series_id, projects_dir)
    entry: dict[str, Any] = {"provider": provider, "voice_id": voice_id, "updated_at": _now()}
    if model:
        entry["model"] = model
    if settings:
        entry["settings"] = settings
    if sample:
        entry["sample"] = sample
    if notes:
        entry["notes"] = notes
    data["voices"][character] = entry
    _validate(data, "voices")
    _write(series_dir(series_id, projects_dir) / VOICES_FILE, data)
    return entry


def voice_for(series_id: str, character: str, projects_dir: Optional[Path] = None) -> dict[str, Any]:
    voices = load_voices(series_id, projects_dir)["voices"]
    if character not in voices:
        raise SeriesError(f"no voice locked for {character!r} in {series_id}; known: {sorted(voices)}")
    return voices[character]
