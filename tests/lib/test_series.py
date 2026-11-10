"""Tests for lib.series — series home, episodes, shared voices."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backlot.state import list_projects
from lib import series as s


@pytest.fixture
def projects(tmp_path: Path) -> Path:
    return tmp_path / "projects"


def _series(projects: Path) -> Path:
    return s.init_series("night-shift", title="Night Shift", pipeline_type="animation",
                         projects_dir=projects)


def test_init_series_layout_and_idempotence(projects: Path) -> None:
    root = _series(projects)
    assert root == projects / "_series" / "night-shift"
    for sub in s.SERIES_SUBDIRS:
        assert (root / sub).is_dir()
    first = json.loads((root / "series.json").read_text())
    s.init_series("night-shift", title="Night Shift 2", projects_dir=projects)
    again = json.loads((root / "series.json").read_text())
    assert again["created_at"] == first["created_at"]
    assert again["title"] == "Night Shift 2" and again["pipeline_type"] == "animation"


def test_bad_series_id_rejected(projects: Path) -> None:
    with pytest.raises(s.SeriesError):
        s.init_series("Night Shift", title="x", projects_dir=projects)


def test_add_season_creates_stubs_without_overwriting(projects: Path) -> None:
    root = _series(projects)
    s.add_season("night-shift", 1, title="Pilot season", projects_dir=projects)
    desc = root / "bible" / "season-01.md"
    assert desc.is_file() and (root / "bible" / "season-01-script.md").is_file()
    desc.write_text("my notes")
    s.add_season("night-shift", 1, projects_dir=projects)
    assert desc.read_text() == "my notes"
    assert s.load_series("night-shift", projects)["seasons"][0]["title"] == "Pilot season"


def test_init_episode_is_a_regular_labelled_project(projects: Path) -> None:
    root = _series(projects)
    ep = s.init_episode("night-shift", 1, 2, title="The Lamp", projects_dir=projects)
    assert ep == projects / "night-shift-s01e02"
    marker = json.loads((ep / "project.json").read_text())
    assert marker["series"] == "night-shift" and marker["pipeline_type"] == "animation"
    assert marker["episode"]["code"] == "s01e02"
    assert (ep / "artifacts").is_dir()
    link = root / "episodes" / "s01e02"
    assert link.is_symlink() and link.resolve() == ep.resolve()
    index = s.load_series("night-shift", projects)
    assert [e["code"] for e in index["episodes"]] == ["s01e02"]
    assert index["seasons"][0]["number"] == 1  # season auto-registered
    s.init_episode("night-shift", 1, 2, title="The Lamp (v2)", projects_dir=projects)
    assert len(s.load_series("night-shift", projects)["episodes"]) == 1


def test_backlot_sees_episodes_grouped_and_hides_series_home(projects: Path) -> None:
    _series(projects)
    s.init_episode("night-shift", 1, 1, title="Pilot", projects_dir=projects)
    s.init_episode("night-shift", 1, 2, title="The Lamp", projects_dir=projects)
    listed = list_projects(projects)
    assert sorted(p["project_id"] for p in listed) == ["night-shift-s01e01", "night-shift-s01e02"]
    assert {p["series"] for p in listed} == {"night-shift"}


def test_episode_context_points_at_shared_material(projects: Path) -> None:
    root = _series(projects)
    (root / "bible" / "characters" / "nadia.json").write_text("{}")
    s.init_episode("night-shift", 1, 1, title="Pilot", projects_dir=projects)
    ep2 = s.init_episode("night-shift", 1, 2, title="The Lamp", projects_dir=projects)
    ctx = s.episode_context(ep2, projects)
    assert ctx["series_id"] == "night-shift"
    assert ctx["season_description"].endswith("bible/season-01.md")
    assert ctx["characters"] == [str((root / "bible/characters/nadia.json").resolve())]
    assert ctx["voices_file"].endswith("shared/voices/voices.json")
    assert [e["code"] for e in ctx["previous_episodes"]] == ["s01e01"]
    assert Path(ctx["references_dir"]).is_absolute()


def test_episode_context_rejects_loose_project(tmp_path: Path) -> None:
    (tmp_path / "loose").mkdir()
    with pytest.raises(s.SeriesError):
        s.episode_context(tmp_path / "loose", tmp_path)


def test_voices_lock_and_lookup(projects: Path) -> None:
    _series(projects)
    s.set_voice("night-shift", "nadia", provider="elevenlabs_tts", voice_id="abc123",
                model="eleven_v3", settings={"stability": 0.4}, projects_dir=projects)
    voice = s.voice_for("night-shift", "nadia", projects)
    assert voice["voice_id"] == "abc123" and voice["settings"] == {"stability": 0.4}
    with pytest.raises(s.SeriesError, match="no voice locked"):
        s.voice_for("night-shift", "ghost", projects)


def test_episode_requires_pipeline(projects: Path) -> None:
    s.init_series("bare", title="Bare", projects_dir=projects)
    with pytest.raises(s.SeriesError, match="pipeline_type"):
        s.init_episode("bare", 1, 1, title="x", projects_dir=projects)
