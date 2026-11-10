"""Tests for the davinci_resolve tool.

Everything except the final live test runs without DaVinci Resolve: planning
is pure, the runner is exercised through its command construction and a fake
subprocess, and the runtime script's JSON protocol is checked by running it
with an operation that never touches the Resolve API.

Live end-to-end test (needs Resolve Studio running):
    OPENMONTAGE_RESOLVE_LIVE=1 pytest tests/tools/test_davinci_resolve.py -k live
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools.video import davinci_resolve as dr

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture()
def media(tmp_path: Path) -> Path:
    """Create placeholder files; planning only checks existence."""
    for name in ("clip1.mp4", "clip2.mp4", "card.png", "vo.mp3", "music.m4a", "logo.png"):
        (tmp_path / name).write_bytes(b"x")
    return tmp_path


@pytest.fixture()
def manifest(media: Path) -> dict:
    return {
        "version": "1.0",
        "assets": [
            {"id": "a-clip1", "type": "video", "path": "clip1.mp4", "source_tool": "t", "scene_id": "s1"},
            {"id": "a-card", "type": "image", "path": str(media / "card.png"), "source_tool": "t", "scene_id": "s2"},
            {"id": "a-vo", "type": "audio", "path": "vo.mp3", "source_tool": "t", "scene_id": "s1"},
            {"id": "a-music", "type": "music", "path": "music.m4a", "source_tool": "t", "scene_id": "all"},
            {"id": "a-logo", "type": "image", "path": "logo.png", "source_tool": "t", "scene_id": "all"},
        ],
    }


def _by_id(plan: dr.TimelinePlan) -> dict[str, dr.PlannedClip]:
    return {c.id: c for c in plan.clips}


# --------------------------------------------------------------------------
# asset resolution
# --------------------------------------------------------------------------


def test_resolver_maps_asset_ids_relative_and_absolute_paths(media: Path, manifest: dict) -> None:
    resolver = dr.AssetResolver(manifest, [media])
    assert resolver.resolve("a-clip1") == (media / "clip1.mp4").resolve()
    assert resolver.resolve("a-card") == media / "card.png"
    assert resolver.resolve("clip2.mp4") == (media / "clip2.mp4").resolve()
    assert resolver.resolve(f"file://{media / 'clip2.mp4'}") == media / "clip2.mp4"
    assert resolver.resolve("missing.mp4") is None
    assert resolver.resolve("") is None


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------


def test_cuts_are_sequential_like_video_compose(media: Path, manifest: dict) -> None:
    ed = {
        "version": "1.0", "render_runtime": "ffmpeg",
        "cuts": [
            {"id": "c1", "source": "a-clip1", "in_seconds": 2.0, "out_seconds": 5.0},
            {"id": "c2", "source": "a-card", "in_seconds": 0, "out_seconds": 2.5},
            {"id": "c3", "source": "clip2.mp4", "in_seconds": 1.0, "out_seconds": 5.0, "speed": 2.0},
        ],
    }
    plan = dr.plan_from_edit_decisions(ed, dr.AssetResolver(manifest, [media]))
    clips = _by_id(plan)

    assert (clips["c1"].record_seconds, clips["c1"].duration_seconds, clips["c1"].in_seconds) == (0.0, 3.0, 2.0)
    assert clips["c1"].track == dr.TRACK_PRIMARY and clips["c1"].media == "video"
    assert clips["c2"].record_seconds == 3.0 and clips["c2"].still and clips["c2"].in_seconds == 0.0
    # speed 2x: 4 s of source occupies 2 s of programme time, like video_compose
    assert clips["c3"].record_seconds == 5.5 and clips["c3"].duration_seconds == 2.0
    assert any("speed 2.0x" in w for w in plan.warnings)
    assert plan.duration_seconds == 7.5


def test_source_audio_follows_video_cuts_but_not_stills(media: Path, manifest: dict) -> None:
    ed = {"cuts": [
        {"id": "c1", "source": "a-clip1", "in_seconds": 1, "out_seconds": 2},
        {"id": "c2", "source": "a-card", "in_seconds": 0, "out_seconds": 1},
    ]}
    clips = _by_id(dr.plan_from_edit_decisions(ed, dr.AssetResolver(manifest, [media])))
    audio = clips["c1:audio"]
    assert (audio.media, audio.track, audio.record_seconds, audio.in_seconds) == ("audio", dr.TRACK_SOURCE_AUDIO, 0.0, 1.0)
    assert "c2:audio" not in clips

    no_audio = dr.plan_from_edit_decisions(ed, dr.AssetResolver(manifest, [media]), include_source_audio=False)
    assert all(c.media == "video" for c in no_audio.clips)


def test_unresolved_cut_leaves_gap_and_warns(media: Path, manifest: dict) -> None:
    ed = {"cuts": [
        {"id": "title", "source": "", "in_seconds": 0, "out_seconds": 2},
        {"id": "c1", "source": "a-clip1", "in_seconds": 0, "out_seconds": 1},
    ]}
    plan = dr.plan_from_edit_decisions(ed, dr.AssetResolver(manifest, [media]))
    assert _by_id(plan)["c1"].record_seconds == 2.0
    assert any(w.startswith("title:") and "gap" in w for w in plan.warnings)


def test_layers_overlays_narration_and_music_tracks(media: Path, manifest: dict) -> None:
    ed = {
        "cuts": [
            {"id": "c1", "source": "a-clip1", "in_seconds": 0, "out_seconds": 4},
            {"id": "c2", "source": "clip2.mp4", "in_seconds": 0, "out_seconds": 2, "layer": "overlay"},
        ],
        "overlays": [{"asset_id": "a-logo", "start_seconds": 1, "end_seconds": 3, "position": {"x": 0, "y": 0}}],
        "audio": {
            "narration": {"segments": [{"asset_id": "a-vo", "start_seconds": 0.5, "end_seconds": 2.5}]},
            "music": {"asset_id": "a-music", "volume": 0.2},
        },
        "subtitles": {"enabled": True, "source": "subs.srt"},
    }
    plan = dr.plan_from_edit_decisions(ed, dr.AssetResolver(manifest, [media]), probe_duration=lambda p: 60.0)
    clips = _by_id(plan)

    assert clips["c2"].track == dr.TRACK_OVERLAY_CUTS
    overlay = clips["overlay:a-logo"]
    assert (overlay.track, overlay.record_seconds, overlay.duration_seconds, overlay.still) == (dr.TRACK_OVERLAYS, 1.0, 2.0, True)
    narration = clips["narration:a-vo"]
    assert (narration.track, narration.record_seconds, narration.duration_seconds) == (dr.TRACK_NARRATION, 0.5, 2.0)
    music = clips["music:a-music"]
    # 60 s music bed is trimmed to the 6 s programme
    assert (music.track, music.record_seconds, music.duration_seconds) == (dr.TRACK_MUSIC, 0.0, 6.0)
    joined = " ".join(plan.warnings)
    assert "music mix" in joined and "subtitles" in joined and "position/opacity" in joined


def test_narration_without_end_uses_probe(media: Path, manifest: dict) -> None:
    ed = {"cuts": [], "audio": {"narration": {"segments": [{"asset_id": "a-vo", "start_seconds": 1}]}}}
    plan = dr.plan_from_edit_decisions(ed, dr.AssetResolver(manifest, [media]), probe_duration=lambda p: 3.25)
    assert _by_id(plan)["narration:a-vo"].duration_seconds == 3.25


def test_explicit_clip_plan_validation(media: Path) -> None:
    resolver = dr.AssetResolver(None, [media])
    plan = dr.plan_from_clips(
        [{"path": "clip1.mp4", "track": 1, "duration_seconds": 5},
         {"path": "vo.mp3", "track": 2, "record_seconds": 1, "duration_seconds": 2}],
        resolver,
    )
    assert [c.media for c in plan.clips] == ["video", "audio"]
    with pytest.raises(dr.ResolveToolError):
        dr.plan_from_clips([{"path": "nope.mp4", "duration_seconds": 1}], resolver)
    with pytest.raises(dr.ResolveToolError):
        dr.plan_from_clips([{"path": "clip1.mp4", "duration_seconds": 0}], resolver)


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------


def test_runner_command_shapes(tmp_path: Path) -> None:
    native = dr.RunnerConfig("native", "python3")
    assert native.command(Path("/s.py"), Path("/q"), Path("/r")) == ["python3", "/s.py", "/q", "/r"]
    box = dr.RunnerConfig("podman", "python3", "davincibox", "podman")
    cmd = box.command(Path("/s.py"), Path("/q"), Path("/r"))
    assert cmd[:3] == ["podman", "exec", "-u"] and cmd[4:] == ["davincibox", "python3", "/s.py", "/q", "/r"]


def test_detect_runner_respects_env(monkeypatch) -> None:
    monkeypatch.setenv("RESOLVE_RUNNER", "podman")
    monkeypatch.setenv("RESOLVE_CONTAINER", "mybox")
    runner = dr.detect_runner()
    assert (runner.mode, runner.container, runner.engine) == ("podman", "mybox", "podman")

    monkeypatch.setenv("RESOLVE_RUNNER", "native")
    assert dr.detect_runner().mode == "native"

    monkeypatch.setenv("RESOLVE_RUNNER", "bogus")
    with pytest.raises(dr.ResolveToolError):
        dr.detect_runner()


def test_run_runtime_round_trip_and_cleanup(tmp_path: Path, monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        request = json.loads(Path(cmd[-2]).read_text())
        Path(cmd[-1]).write_text(json.dumps({"ok": True, "data": {"echo": request["op"]}, "warnings": ["w"]}))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(dr.subprocess, "run", fake_run)
    response = dr.run_runtime(dr.RunnerConfig("native", "python3"), {"op": "status"}, tmp_path, 10)
    assert response["data"] == {"echo": "status"}
    assert list((tmp_path / "exchange").iterdir()) == []


def test_run_runtime_reports_missing_response(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        dr.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom: no module")
    )
    with pytest.raises(dr.ResolveToolError, match="boom: no module"):
        dr.run_runtime(dr.RunnerConfig("native", "python3"), {"op": "status"}, tmp_path, 10)


# --------------------------------------------------------------------------
# runtime script protocol (no Resolve needed)
# --------------------------------------------------------------------------


def test_runtime_script_is_stdlib_only_and_reports_errors_as_json(tmp_path: Path) -> None:
    request, response = tmp_path / "req.json", tmp_path / "resp.json"
    request.write_text(json.dumps({"op": "does_not_exist"}))
    process = subprocess.run(
        [sys.executable, "-I", str(dr.RUNTIME_SCRIPT), str(request), str(response)],
        capture_output=True, text=True, timeout=30,
    )
    assert process.returncode == 1
    payload = json.loads(response.read_text())
    assert payload["ok"] is False and "Unknown operation" in payload["error"]
    assert set(payload) >= {"ok", "data", "warnings", "error"}


def test_runtime_operations_match_tool_operations() -> None:
    source = dr.RUNTIME_SCRIPT.read_text(encoding="utf-8")
    for op in ("status", "build_timeline", "apply_lut", "render"):
        assert f'"{op}": op_' in source


# --------------------------------------------------------------------------
# tool contract
# --------------------------------------------------------------------------


def test_tool_contract_and_validation(monkeypatch) -> None:
    tool = dr.DavinciResolve()
    info = tool.get_info()
    assert info["name"] == "davinci_resolve" and info["capability"] == "nle_finishing"
    assert tool.estimate_cost({"operation": "render"}) == 0.0
    assert not tool.execute({"operation": "explode"}).success

    monkeypatch.setattr(dr, "detect_runner", lambda: None)
    result = tool.execute({"operation": "status"})
    assert not result.success and "not found" in result.error
    assert tool.get_status().value == "unavailable"


def test_build_requires_project_name_and_plan(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dr, "detect_runner", lambda: dr.RunnerConfig("native", "python3"))
    tool = dr.DavinciResolve()
    result = tool.execute({"operation": "build_timeline", "project_dir": str(tmp_path)})
    assert not result.success and "edit_decisions" in result.error


# --------------------------------------------------------------------------
# media preparation (real ffmpeg)
# --------------------------------------------------------------------------


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_media_preparer_converts_stills_and_compressed_audio(tmp_path: Path) -> None:
    def make(args, name):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args, str(tmp_path / name)], check=True)
        return tmp_path / name

    still = make(["-f", "lavfi", "-i", "color=c=red:size=320x240", "-frames:v", "1"], "s.png")
    mp3 = make(["-f", "lavfi", "-i", "sine=frequency=440", "-t", "1", "-c:a", "libmp3lame"], "a.mp3")
    wav = make(["-f", "lavfi", "-i", "sine=frequency=440", "-t", "1", "-c:a", "pcm_s16le"], "b.wav")

    plan = dr.TimelinePlan(clips=[
        dr.PlannedClip(str(still), "video", 1, 0.0, 1.5, still=True, id="s"),
        dr.PlannedClip(str(mp3), "audio", 2, 0.0, 1.0, id="a"),
        dr.PlannedClip(str(wav), "audio", 2, 1.0, 1.0, id="b"),
    ])
    preparer = dr.MediaPreparer(tmp_path / "cache", 25, 640, 360)
    prepared = {c.id: c for c in preparer.prepare(plan).clips}

    clip = Path(prepared["s"].path)
    probe = dr.ffprobe_streams(clip)
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert video["codec_name"] == "h264" and abs(float(probe["format"]["duration"]) - 1.5) < 0.05
    assert Path(prepared["a"].path).suffix == ".wav"
    assert prepared["b"].path == str(wav)  # PCM is already Resolve-safe
    assert len(preparer.converted) == 2

    # second pass is served from the content-addressed cache
    again = dr.MediaPreparer(tmp_path / "cache", 25, 640, 360)
    again.prepare(plan)
    assert again.converted == []


# --------------------------------------------------------------------------
# live
# --------------------------------------------------------------------------


@pytest.mark.skipif(os.environ.get("OPENMONTAGE_RESOLVE_LIVE") != "1",
                    reason="needs DaVinci Resolve Studio running; set OPENMONTAGE_RESOLVE_LIVE=1")
def test_live_finish_round_trip(tmp_path_factory) -> None:
    # Resolve must be able to write the render target; use a dir under $HOME.
    root = Path.home() / ".cache" / "openmontage" / "davinci_resolve" / "live-test"
    shutil.rmtree(root, ignore_errors=True)
    assets = root / "assets"
    assets.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "3", "-c:v", "libx264", "-pix_fmt",
                    "yuv420p", "-c:a", "aac", str(assets / "clip.mp4")], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:size=800x800",
                    "-frames:v", "1", str(assets / "card.png")], check=True)
    # Custom (non-indexed) LUT: exercises the copy-into-master-LUT-tree path.
    lut = root / "om-live-test.cube"
    lut.write_text("LUT_3D_SIZE 2\n" + "".join(
        f"{r} {g} {b}\n" for b in (0.0, 0.8) for g in (0.0, 0.9) for r in (0.0, 1.0)))
    ed = {"version": "1.0", "render_runtime": "ffmpeg", "cuts": [
        {"id": "c1", "source": "assets/clip.mp4", "in_seconds": 0.5, "out_seconds": 2.5},
        {"id": "c2", "source": "assets/card.png", "in_seconds": 0, "out_seconds": 1.0},
    ]}
    result = dr.DavinciResolve().execute({
        "operation": "finish", "project_name": "OpenMontage Live Test", "timeline_name": "live",
        "overwrite": True, "project_dir": str(root), "edit_decisions": ed, "fps": 30,
        "width": 1280, "height": 720, "filename": "live", "lut": str(lut),
    })
    assert result.success, result.error
    assert result.data["lut"]["applied"] == 2 and not result.data["lut"]["failed"]
    assert result.data["timeline"]["duration_seconds"] == pytest.approx(3.0, abs=0.04)
    output = Path(result.artifacts[0])
    duration = float(dr.ffprobe_streams(output)["format"]["duration"])
    assert duration == pytest.approx(3.0, abs=0.05)
