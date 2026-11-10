"""DaVinci Resolve Studio integration — NLE hand-off, grading and finishing.

Builds a real, editable Resolve timeline from OpenMontage's canonical
``edit_decisions`` + ``asset_manifest`` artifacts, applies LUTs and renders
through Resolve's own encoder, all over the official external scripting API.

Architecture
------------
Resolve's ``fusionscript`` library only loads in an interpreter that matches
the Resolve install (on Linux distrobox setups: the container's ``python3``),
so the API calls live in a stdlib-only runtime script
(``templates/davinci-resolve-runtime.py``) executed out-of-process. This
module owns everything OpenMontage-specific and stays testable without
Resolve:

1. **Planning** (pure): artifacts -> canonical clip plan with track layout and
   record positions in seconds. Mirrors ``video_compose`` sequencing: cuts are
   laid end-to-end in array order, each advancing the cursor by
   ``(out - in) / speed``.
2. **Media preparation** (ffmpeg): Resolve on Linux cannot decode AAC/MP3
   audio and ignores still-image durations set over the API, so stills become
   exact-length H.264 clips and compressed audio becomes 48 kHz PCM WAV.
   Results are content-addressed and cached.
3. **Runner**: executes the runtime natively or inside a podman/docker
   container (``RESOLVE_RUNNER``), exchanging JSON request/response files.

Track layout: V1 primary/background cuts, V2 overlay-layer cuts, V3
``overlays[]``; A1 source audio, A2 narration, A3 music.

Requires DaVinci Resolve **Studio** (the free edition refuses external
scripting connections) with Preferences > System > General > External
scripting using = Local.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

RUNTIME_SCRIPT = Path(__file__).resolve().parent / "templates" / "davinci-resolve-runtime.py"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "openmontage" / "davinci_resolve"

NATIVE_FUSIONSCRIPT = (
    Path("/opt/resolve/libs/Fusion/fusionscript.so"),
    Path("/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"),
    Path(r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"),
)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".aif", ".aiff"}
# Audio codecs Resolve decodes on every platform. Everything else is transcoded.
RESOLVE_SAFE_AUDIO = {"pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_s16be", "pcm_s24be", "flac"}

TRACK_PRIMARY, TRACK_OVERLAY_CUTS, TRACK_OVERLAYS = 1, 2, 3
TRACK_SOURCE_AUDIO, TRACK_NARRATION, TRACK_MUSIC = 1, 2, 3

OPERATIONS = ("status", "build_timeline", "apply_lut", "render", "finish")


class ResolveToolError(RuntimeError):
    """Expected, user-actionable failure."""


# ==========================================================================
# 1. Planning
# ==========================================================================


@dataclass
class PlannedClip:
    """One placement on the Resolve timeline. Times are seconds."""

    path: str
    media: str  # "video" | "audio"
    track: int
    record_seconds: float
    duration_seconds: float
    in_seconds: float = 0.0
    id: str = ""
    role: str = ""  # primary | overlay | narration | music | source_audio
    still: bool = False  # set during planning, consumed by media preparation


@dataclass
class TimelinePlan:
    clips: list[PlannedClip] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return max((c.record_seconds + c.duration_seconds for c in self.clips), default=0.0)


def load_artifact(value: Any, label: str) -> Optional[dict[str, Any]]:
    """Accept an artifact as a dict or a path to a JSON file."""
    if value is None or isinstance(value, dict):
        return value
    path = Path(str(value)).expanduser()
    if not path.is_file():
        raise ResolveToolError(f"{label} not found: {path}")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


class AssetResolver:
    """Map ``cut.source`` / ``asset_id`` values to absolute media paths."""

    def __init__(self, asset_manifest: Optional[dict[str, Any]], base_dirs: list[Path]):
        self.assets = {a["id"]: a for a in (asset_manifest or {}).get("assets", []) if "id" in a}
        self.base_dirs = [d for d in base_dirs if d]

    def resolve(self, ref: str) -> Optional[Path]:
        if not ref:
            return None
        if ref in self.assets:
            ref = str(self.assets[ref].get("path") or "")
            if not ref:
                return None
        ref = ref.removeprefix("file://")
        candidate = Path(ref).expanduser()
        if candidate.is_absolute():
            return candidate if candidate.exists() else None
        for base in self.base_dirs:
            joined = (base / candidate).resolve()
            if joined.exists():
                return joined
        return None


def plan_from_edit_decisions(
    edit_decisions: dict[str, Any],
    resolver: AssetResolver,
    *,
    include_source_audio: bool = True,
    probe_duration=None,
) -> TimelinePlan:
    """Translate canonical edit_decisions into a Resolve clip plan.

    ``probe_duration(path) -> float | None`` supplies media lengths for music
    beds; it is injectable so planning stays pure in tests.
    """
    plan = TimelinePlan()
    cursor = 0.0

    for index, cut in enumerate(edit_decisions.get("cuts") or []):
        cut_id = str(cut.get("id") or f"cut-{index + 1}")
        try:
            source_in = float(cut.get("in_seconds", 0.0))
            source_out = float(cut.get("out_seconds", source_in))
            speed = max(float(cut.get("speed", 1.0) or 1.0), 0.1)
        except (TypeError, ValueError):
            plan.warnings.append(f"{cut_id}: invalid timing, skipped")
            continue
        duration = max(0.0, (source_out - source_in) / speed)
        if duration <= 0:
            plan.warnings.append(f"{cut_id}: zero duration, skipped")
            continue

        source = resolver.resolve(str(cut.get("source") or ""))
        if source is None:
            plan.warnings.append(
                f"{cut_id}: no media file for source '{cut.get('source')}' "
                f"(component/text scene?) — left a {duration:.2f}s gap"
            )
            cursor += duration
            continue
        if abs(speed - 1.0) > 1e-6:
            plan.warnings.append(
                f"{cut_id}: speed {speed}x is not settable over the Resolve API — "
                "placed at 1x for the cut's timeline length; retime in Resolve"
            )

        is_still = source.suffix.lower() in IMAGE_EXTS
        layer = cut.get("layer") or "primary"
        track = TRACK_OVERLAY_CUTS if layer == "overlay" else TRACK_PRIMARY
        plan.clips.append(
            PlannedClip(
                path=str(source), media="video", track=track, record_seconds=cursor,
                duration_seconds=duration, in_seconds=0.0 if is_still else source_in,
                id=cut_id, role=layer, still=is_still,
            )
        )
        if include_source_audio and not is_still:
            plan.clips.append(
                PlannedClip(
                    path=str(source), media="audio", track=TRACK_SOURCE_AUDIO,
                    record_seconds=cursor, duration_seconds=duration, in_seconds=source_in,
                    id=f"{cut_id}:audio", role="source_audio",
                )
            )
        if cut.get("transform"):
            plan.warnings.append(f"{cut_id}: transform/animation not reproduced; adjust in Resolve")
        cursor += duration

    for index, overlay in enumerate(edit_decisions.get("overlays") or []):
        ov_id = str(overlay.get("asset_id") or f"overlay-{index + 1}")
        path = resolver.resolve(str(overlay.get("asset_id") or ""))
        start = float(overlay.get("start_seconds", 0.0))
        duration = float(overlay.get("end_seconds", start)) - start
        if path is None or duration <= 0:
            plan.warnings.append(f"overlay {ov_id}: unresolved or empty, skipped")
            continue
        plan.clips.append(
            PlannedClip(
                path=str(path), media="video", track=TRACK_OVERLAYS, record_seconds=start,
                duration_seconds=duration, id=f"overlay:{ov_id}", role="overlay",
                still=path.suffix.lower() in IMAGE_EXTS,
            )
        )
        plan.warnings.append(f"overlay {ov_id}: position/opacity not reproduced; set in Inspector")

    audio = edit_decisions.get("audio") or {}
    for index, segment in enumerate((audio.get("narration") or {}).get("segments") or []):
        seg_id = str(segment.get("asset_id") or f"narration-{index + 1}")
        path = resolver.resolve(str(segment.get("asset_id") or ""))
        if path is None:
            plan.warnings.append(f"narration {seg_id}: unresolved, skipped")
            continue
        start = float(segment.get("start_seconds", 0.0))
        end = segment.get("end_seconds")
        length = (float(end) - start) if end is not None else (probe_duration(path) if probe_duration else None)
        if not length or length <= 0:
            plan.warnings.append(f"narration {seg_id}: unknown duration, skipped")
            continue
        plan.clips.append(
            PlannedClip(
                path=str(path), media="audio", track=TRACK_NARRATION, record_seconds=start,
                duration_seconds=length, id=f"narration:{seg_id}", role="narration",
            )
        )

    music = audio.get("music") or edit_decisions.get("music") or {}
    if music.get("asset_id"):
        path = resolver.resolve(str(music["asset_id"]))
        if path is None:
            plan.warnings.append(f"music {music['asset_id']}: unresolved, skipped")
        else:
            program = plan.duration_seconds
            length = probe_duration(path) if probe_duration else None
            length = min(length, program) if length and program else (length or program)
            if length and length > 0:
                plan.clips.append(
                    PlannedClip(
                        path=str(path), media="audio", track=TRACK_MUSIC, record_seconds=0.0,
                        duration_seconds=length, id=f"music:{music['asset_id']}", role="music",
                    )
                )
                mix = {k: music[k] for k in ("volume", "fade_in_seconds", "fade_out_seconds", "ducking") if k in music}
                if mix:
                    plan.warnings.append(f"music mix {mix} not applied over the API; set on A{TRACK_MUSIC} in Fairlight")

    subtitles = edit_decisions.get("subtitles") or {}
    if subtitles.get("enabled") or subtitles.get("source"):
        plan.warnings.append(
            "subtitles are not imported over the API; use File > Import > Subtitle with "
            f"{subtitles.get('source') or 'the project .srt'}"
        )
    return plan


def plan_from_clips(clips: list[dict[str, Any]], resolver: AssetResolver) -> TimelinePlan:
    """Validate an explicit clip list (escape hatch for non-artifact layouts)."""
    plan = TimelinePlan()
    for index, raw in enumerate(clips):
        clip_id = str(raw.get("id") or f"clip-{index + 1}")
        path = resolver.resolve(str(raw.get("path") or raw.get("source") or ""))
        if path is None:
            raise ResolveToolError(f"{clip_id}: media not found: {raw.get('path') or raw.get('source')}")
        media = raw.get("media", "audio" if path.suffix.lower() in AUDIO_EXTS else "video")
        if media not in ("video", "audio"):
            raise ResolveToolError(f"{clip_id}: media must be 'video' or 'audio'")
        duration = float(raw["duration_seconds"])
        if duration <= 0:
            raise ResolveToolError(f"{clip_id}: duration_seconds must be > 0")
        plan.clips.append(
            PlannedClip(
                path=str(path), media=media, track=int(raw.get("track", 1)),
                record_seconds=float(raw.get("record_seconds", 0.0)), duration_seconds=duration,
                in_seconds=float(raw.get("in_seconds", 0.0)), id=clip_id, role=raw.get("role", ""),
                still=media == "video" and path.suffix.lower() in IMAGE_EXTS,
            )
        )
    return plan


# ==========================================================================
# 2. Media preparation
# ==========================================================================


def ffprobe_streams(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,codec_name:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise ResolveToolError(f"ffprobe failed for {path}: {result.stderr.strip()[:300]}")
    return json.loads(result.stdout or "{}")


def probe_duration(path: Path) -> Optional[float]:
    try:
        value = ffprobe_streams(path).get("format", {}).get("duration")
        return float(value) if value else None
    except (ResolveToolError, ValueError, subprocess.SubprocessError):
        return None


class MediaPreparer:
    """Make media Resolve-safe; outputs are content-addressed and reused."""

    def __init__(self, cache_dir: Path, fps: float, width: int, height: int, fit: str = "pad"):
        self.cache_dir = cache_dir
        self.fps, self.width, self.height, self.fit = fps, width, height, fit
        self.converted: list[dict[str, str]] = []
        self._probe_cache: dict[str, dict[str, Any]] = {}

    def _key(self, path: Path, *extra: Any) -> str:
        stat = path.stat()
        raw = json.dumps([str(path.resolve()), stat.st_size, stat.st_mtime_ns, *extra])
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _probe(self, path: Path) -> dict[str, Any]:
        key = str(path)
        if key not in self._probe_cache:
            self._probe_cache[key] = ffprobe_streams(path)
        return self._probe_cache[key]

    def _run(self, cmd: list[str], output: Path) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        tmp = output.with_name(f".{output.stem}.{uuid.uuid4().hex[:6]}{output.suffix}")
        result = subprocess.run(cmd[:-1] + [str(tmp)], capture_output=True, text=True, timeout=1800)
        if result.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise ResolveToolError(f"ffmpeg failed for {output.name}: {result.stderr.strip()[-400:]}")
        tmp.replace(output)
        return output

    def still_to_clip(self, path: Path, duration: float) -> Path:
        key = self._key(path, "still", self.fps, self.width, self.height, self.fit, round(duration, 4))
        output = self.cache_dir / "stills" / f"{path.stem}-{key}.mp4"
        if not output.exists():
            w, h = self.width, self.height
            if self.fit == "cover":
                geometry = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
            else:
                geometry = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")
            frames = max(1, int(round(duration * self.fps)))
            self._run(
                ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(path),
                 "-vf", f"{geometry},setsar=1,format=yuv420p", "-r", f"{self.fps:g}",
                 "-frames:v", str(frames), "-c:v", "libx264", "-preset", "medium",
                 "-crf", "12", "-tune", "stillimage", "-movflags", "+faststart", str(output)],
                output,
            )
            self.converted.append({"source": str(path), "output": str(output), "reason": "still image"})
        return output

    def audio_safe(self, path: Path) -> Path:
        """Return a path whose first audio stream Resolve can decode."""
        streams = self._probe(path).get("streams", [])
        audio = [s for s in streams if s.get("codec_type") == "audio"]
        if not audio:
            raise ResolveToolError(f"No audio stream in {path}")
        if audio[0].get("codec_name") in RESOLVE_SAFE_AUDIO:
            return path
        key = self._key(path, "pcm48k")
        output = self.cache_dir / "audio" / f"{path.stem}-{key}.wav"
        if not output.exists():
            self._run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-map", "0:a:0",
                 "-vn", "-ar", "48000", "-c:a", "pcm_s24le", str(output)],
                output,
            )
            self.converted.append(
                {"source": str(path), "output": str(output), "reason": f"{audio[0].get('codec_name')} audio"}
            )
        return output

    def has_audio(self, path: Path) -> bool:
        return any(s.get("codec_type") == "audio" for s in self._probe(path).get("streams", []))

    def prepare(self, plan: TimelinePlan) -> TimelinePlan:
        prepared = TimelinePlan(warnings=list(plan.warnings))
        for clip in plan.clips:
            path = Path(clip.path)
            if clip.media == "video" and clip.still:
                new = PlannedClip(**{**asdict(clip), "path": str(self.still_to_clip(path, clip.duration_seconds)),
                                     "in_seconds": 0.0, "still": False})
                prepared.clips.append(new)
            elif clip.media == "audio":
                if path.suffix.lower() not in AUDIO_EXTS and not self.has_audio(path):
                    continue  # silent video: nothing to place on the audio track
                prepared.clips.append(PlannedClip(**{**asdict(clip), "path": str(self.audio_safe(path))}))
            else:
                prepared.clips.append(clip)
        return prepared


# ==========================================================================
# 3. Runner
# ==========================================================================


@dataclass
class RunnerConfig:
    mode: str  # native | podman | docker
    python: str
    container: str = ""
    engine: str = ""

    def command(self, script: Path, request: Path, response: Path) -> list[str]:
        tail = [self.python, str(script), str(request), str(response)]
        if self.mode == "native":
            return tail
        return [self.engine, "exec", "-u", getpass.getuser(), self.container, *tail]


def _container_exists(engine: str, name: str) -> bool:
    if not shutil.which(engine):
        return False
    try:
        result = subprocess.run([engine, "container", "exists", name], capture_output=True, timeout=15)
        if result.returncode == 0:
            return True
        if engine == "docker":
            result = subprocess.run([engine, "inspect", name], capture_output=True, timeout=15)
            return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def detect_runner() -> Optional[RunnerConfig]:
    """Resolve the runner from RESOLVE_RUNNER (auto|native|podman|docker)."""
    mode = (os.environ.get("RESOLVE_RUNNER") or "auto").strip().lower()
    python = os.environ.get("RESOLVE_PYTHON") or ("python3" if os.name != "nt" else sys.executable)
    container = os.environ.get("RESOLVE_CONTAINER") or "davincibox"

    if mode == "native":
        return RunnerConfig("native", python)
    if mode in ("podman", "docker"):
        return RunnerConfig(mode, python, container, mode)
    if mode != "auto":
        raise ResolveToolError(f"RESOLVE_RUNNER must be auto|native|podman|docker, got '{mode}'")

    if os.environ.get("RESOLVE_SCRIPT_LIB") or any(p.exists() for p in NATIVE_FUSIONSCRIPT):
        return RunnerConfig("native", python)
    for engine in ("podman", "docker"):
        if _container_exists(engine, container):
            return RunnerConfig(engine, python, container, engine)
    return None


def run_runtime(
    runner: RunnerConfig, request: dict[str, Any], work_dir: Path, timeout: float
) -> dict[str, Any]:
    """Execute one runtime operation and return its parsed response."""
    exchange = work_dir / "exchange"
    exchange.mkdir(parents=True, exist_ok=True)
    stem = f"{request['op']}-{uuid.uuid4().hex[:8]}"
    req_path, resp_path = exchange / f"{stem}.request.json", exchange / f"{stem}.response.json"
    req_path.write_text(json.dumps(request, indent=2), encoding="utf-8")
    cmd = runner.command(RUNTIME_SCRIPT, req_path, resp_path)
    try:
        process = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ResolveToolError(f"Resolve runtime timed out after {timeout:.0f}s ({request['op']})") from exc
    except FileNotFoundError as exc:
        raise ResolveToolError(f"Runner executable not found: {cmd[0]}") from exc
    try:
        if not resp_path.is_file():
            raise ResolveToolError(
                f"Resolve runtime produced no response (exit {process.returncode}): "
                f"{(process.stderr or process.stdout).strip()[-600:]}"
            )
        return json.loads(resp_path.read_text(encoding="utf-8"))
    finally:
        req_path.unlink(missing_ok=True)
        resp_path.unlink(missing_ok=True)


# ==========================================================================
# 4. Tool
# ==========================================================================


class DavinciResolve(BaseTool):
    name = "davinci_resolve"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "nle_finishing"
    provider = "davinci_resolve"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL_GPU

    dependencies = ["cmd:ffmpeg", "cmd:ffprobe"]
    install_instructions = (
        "Install DaVinci Resolve Studio (the free edition blocks external scripting) and set "
        "Preferences > System > General > External scripting using = Local. Native installs "
        "are auto-detected. For a distrobox/podman install set RESOLVE_RUNNER=podman and "
        "RESOLVE_CONTAINER=<name> (default 'davincibox'). Optional: RESOLVE_LAUNCH_COMMAND to "
        "let the tool start Resolve. See skills/core/davinci-resolve.md."
    )
    agent_skills = ["davinci-resolve"]

    capabilities = [
        "build_timeline_from_edit_decisions", "apply_lut", "render_via_resolve",
        "nle_handoff", "color_grading_handoff",
    ]
    supports = {
        "editable_timeline": True,
        "multi_track_audio": True,
        "lut_application": True,
        "gpu_encode": True,
        "transitions": False,
        "speed_ramps": False,
        "overlay_positioning": False,
        "subtitle_import": False,
    }
    best_for = [
        "handing a finished OpenMontage edit to a human editor/colorist as a real Resolve timeline",
        "grading with LUTs and finishing in Resolve before final delivery",
        "rendering through Resolve's encoders (NVENC H.264/H.265, ProRes, DNxHR)",
    ]
    not_good_for = [
        "templated component scenes (text cards, charts) — render those with video_compose first",
        "fully unattended pipelines on machines without Resolve Studio running",
        "per-cut transitions, speed changes, overlay transforms (not exposed by the API)",
    ]
    input_schema = {
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {
                "type": "string", "enum": list(OPERATIONS),
                "description": "status | build_timeline | apply_lut | render | finish (build + optional LUT + render).",
            },
            "project_name": {"type": "string", "description": "Resolve project (created if missing on build)."},
            "timeline_name": {"type": "string", "description": "Timeline to build/use. Defaults to project_name."},
            "edit_decisions": {"description": "edit_decisions artifact (dict) or path to its JSON."},
            "asset_manifest": {"description": "asset_manifest artifact (dict) or path; resolves asset IDs to files."},
            "clips": {
                "type": "array",
                "description": "Explicit clip plan instead of edit_decisions: "
                               "{path, media: video|audio, track, record_seconds, duration_seconds, in_seconds}.",
                "items": {"type": "object"},
            },
            "project_dir": {"type": "string", "description": "Base for relative asset paths and the prepared-media cache."},
            "fps": {"type": "number", "default": 30},
            "width": {"type": "integer", "default": 1920},
            "height": {"type": "integer", "default": 1080},
            "fit": {"type": "string", "enum": ["pad", "cover"], "default": "pad"},
            "include_source_audio": {"type": "boolean", "default": True},
            "overwrite": {"type": "boolean", "default": False, "description": "Rebuild an existing timeline."},
            "lut": {"type": "string", "description": "Absolute .cube path or path relative to Resolve's LUT tree."},
            "lut_node_index": {"type": "integer", "default": 1},
            "lut_tracks": {"type": "array", "items": {"type": "integer"}},
            "output_dir": {"type": "string", "description": "Render target dir (must be visible to Resolve)."},
            "filename": {"type": "string", "description": "Render file name without extension."},
            "preset": {"type": "string", "description": "Resolve render preset name (overrides format/codec)."},
            "format": {"type": "string", "default": "mp4"},
            "codec": {"type": "string", "description": "Resolve codec id; default H264_NVIDIA > H264 > H265."},
            "wait": {"type": "boolean", "default": True},
            "render_timeout_seconds": {"type": "number", "default": 3600},
            "launch_if_needed": {"type": "boolean", "default": False,
                                 "description": "Start Resolve with RESOLVE_LAUNCH_COMMAND if it is not running."},
        },
    }
    output_schema = {
        "type": "object",
        "properties": {
            "operation": {"type": "string"},
            "status": {"type": "object"},
            "timeline": {"type": "object"},
            "lut": {"type": "object"},
            "render": {"type": "object"},
            "prepared_media": {"type": "array"},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=4, ram_mb=4096, vram_mb=4096, disk_mb=2048)
    side_effects = [
        "creates/modifies projects and timelines in the Resolve project library",
        "copies custom LUTs into Resolve's master LUT folder (OpenMontage/)",
        "writes prepared media to the cache dir and renders to output_dir",
    ]
    user_visible_verification = [
        "Open the timeline in Resolve and scrub it against the OpenMontage preview",
        "Check render_report output with ffprobe before publishing",
    ]

    # ---- status ----

    def get_status(self) -> ToolStatus:
        if not RUNTIME_SCRIPT.is_file() or not shutil.which("ffmpeg"):
            return ToolStatus.UNAVAILABLE
        try:
            return ToolStatus.AVAILABLE if detect_runner() else ToolStatus.UNAVAILABLE
        except ResolveToolError:
            return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        op = inputs.get("operation")
        return {"status": 3.0, "apply_lut": 10.0, "build_timeline": 60.0}.get(op, 180.0)

    # ---- execution ----

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = time.monotonic()
        operation = inputs.get("operation")
        if operation not in OPERATIONS:
            return ToolResult(success=False, error=f"operation must be one of {OPERATIONS}")
        try:
            runner = detect_runner()
            if runner is None:
                raise ResolveToolError("DaVinci Resolve not found. " + self.install_instructions)
            work_dir = self._work_dir(inputs)
            warnings: list[str] = []
            data: dict[str, Any] = {"operation": operation, "runner": runner.mode, "warnings": warnings}
            artifacts: list[str] = []

            self._ensure_running(runner, work_dir, inputs, warnings)

            if operation == "status":
                data["status"] = self._call(runner, work_dir, {"op": "status"}, warnings, timeout=60)
            if operation in ("build_timeline", "finish"):
                data.update(self._build(runner, work_dir, inputs, warnings))
            if operation == "apply_lut" or (operation == "finish" and inputs.get("lut")):
                data["lut"] = self._apply_lut(runner, work_dir, inputs, warnings)
            if operation in ("render", "finish"):
                data["render"] = self._render(runner, work_dir, inputs, warnings)
                output = data["render"].get("output_path")
                if output and data["render"].get("status") == "Complete":
                    artifacts.append(output)
        except ResolveToolError as exc:
            return ToolResult(success=False, error=str(exc), duration_seconds=time.monotonic() - started)

        return ToolResult(
            success=True, data=data, artifacts=artifacts, cost_usd=0.0,
            duration_seconds=time.monotonic() - started,
        )

    # ---- helpers ----

    @staticmethod
    def _project_name(inputs: dict[str, Any]) -> str:
        name = inputs.get("project_name")
        if not name:
            raise ResolveToolError("project_name is required for this operation")
        return str(name)

    def _timeline_name(self, inputs: dict[str, Any]) -> str:
        return str(inputs.get("timeline_name") or self._project_name(inputs))

    @staticmethod
    def _work_dir(inputs: dict[str, Any]) -> Path:
        if inputs.get("project_dir"):
            return Path(inputs["project_dir"]).expanduser().resolve() / "resolve"
        return DEFAULT_CACHE_DIR

    def _call(self, runner, work_dir, request, warnings, timeout) -> dict[str, Any]:
        response = run_runtime(runner, request, work_dir, timeout)
        warnings.extend(response.get("warnings") or [])
        if not response.get("ok"):
            raise ResolveToolError(response.get("error") or "Resolve runtime failed")
        return response.get("data") or {}

    def _ensure_running(self, runner, work_dir, inputs, warnings) -> None:
        if not inputs.get("launch_if_needed"):
            return
        probe = run_runtime(runner, {"op": "status", "connect_timeout": 0}, work_dir, 60)
        if probe.get("ok"):
            return
        command = os.environ.get("RESOLVE_LAUNCH_COMMAND")
        if not command:
            raise ResolveToolError("Resolve is not running and RESOLVE_LAUNCH_COMMAND is not set")
        log = work_dir / "resolve-launch.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as handle:
            subprocess.Popen(  # operator-configured command
                command, shell=True, stdout=handle, stderr=handle,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
        warnings.append(f"Launched Resolve via RESOLVE_LAUNCH_COMMAND (log: {log})")
        self._call(runner, work_dir, {"op": "status", "connect_timeout": 180}, warnings, timeout=240)

    def _build(self, runner, work_dir, inputs, warnings) -> dict[str, Any]:
        project_dir = Path(inputs["project_dir"]).expanduser() if inputs.get("project_dir") else None
        edit_decisions = load_artifact(inputs.get("edit_decisions"), "edit_decisions")
        asset_manifest = load_artifact(inputs.get("asset_manifest"), "asset_manifest")
        base_dirs = [d for d in (
            project_dir,
            Path(inputs["edit_decisions"]).expanduser().parent if isinstance(inputs.get("edit_decisions"), str) else None,
            Path(inputs["asset_manifest"]).expanduser().parent if isinstance(inputs.get("asset_manifest"), str) else None,
            Path.cwd(),
        ) if d]
        resolver = AssetResolver(asset_manifest, base_dirs)

        if inputs.get("clips"):
            plan = plan_from_clips(inputs["clips"], resolver)
        elif edit_decisions:
            plan = plan_from_edit_decisions(
                edit_decisions, resolver,
                include_source_audio=inputs.get("include_source_audio", True),
                probe_duration=probe_duration,
            )
        else:
            raise ResolveToolError("build_timeline needs edit_decisions (+ asset_manifest) or clips")
        if not plan.clips:
            raise ResolveToolError("Nothing to place: " + "; ".join(plan.warnings[:5]))

        target = ((edit_decisions or {}).get("metadata") or {}).get("compose_target") or {}
        fps = float(inputs.get("fps") or 30)
        width = int(inputs.get("width") or target.get("width") or 1920)
        height = int(inputs.get("height") or target.get("height") or 1080)
        fit = inputs.get("fit") or target.get("fit") or "pad"

        preparer = MediaPreparer(work_dir / "media", fps, width, height, fit)
        prepared = preparer.prepare(plan)
        warnings.extend(prepared.warnings)

        request = {
            "op": "build_timeline",
            "project_name": self._project_name(inputs),
            "timeline_name": self._timeline_name(inputs),
            "overwrite": bool(inputs.get("overwrite")),
            "settings": {"fps": fps, "width": width, "height": height},
            "clips": [asdict(c) for c in prepared.clips],
        }
        timeline = self._call(runner, work_dir, request, warnings, timeout=900)
        return {"timeline": timeline, "prepared_media": preparer.converted}

    def _apply_lut(self, runner, work_dir, inputs, warnings) -> dict[str, Any]:
        lut = inputs.get("lut")
        if not lut:
            raise ResolveToolError("lut is required for apply_lut")
        lut_path = Path(str(lut)).expanduser()
        request = {
            "op": "apply_lut",
            "project_name": self._project_name(inputs),
            "timeline_name": self._timeline_name(inputs),
            "lut": str(lut_path.resolve()) if lut_path.is_absolute() or lut_path.exists() else str(lut),
            "node_index": int(inputs.get("lut_node_index", 1)),
            "tracks": inputs.get("lut_tracks"),
        }
        return self._call(runner, work_dir, request, warnings, timeout=300)

    def _render(self, runner, work_dir, inputs, warnings) -> dict[str, Any]:
        output_dir = inputs.get("output_dir")
        if not output_dir:
            base = Path(inputs["project_dir"]).expanduser() if inputs.get("project_dir") else DEFAULT_CACHE_DIR
            output_dir = base / "renders"
        output_dir = Path(output_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        timeout = float(inputs.get("render_timeout_seconds", 3600))
        request = {
            "op": "render",
            "project_name": self._project_name(inputs),
            "timeline_name": self._timeline_name(inputs),
            "output_dir": str(output_dir),
            "filename": inputs.get("filename") or f"{self._timeline_name(inputs)}-resolve",
            "preset": inputs.get("preset"),
            "format": inputs.get("format") or "mp4",
            "codec": inputs.get("codec"),
            "wait": inputs.get("wait", True),
            "timeout_seconds": timeout,
        }
        result = self._call(runner, work_dir, request, warnings, timeout=timeout + 120)
        if result.get("status") == "Complete":
            output = Path(result.get("output_path") or "")
            if not output.is_file():
                raise ResolveToolError(f"Resolve reported success but output is missing: {output}")
            audio = [s.get("codec_name") for s in ffprobe_streams(output).get("streams", [])
                     if s.get("codec_type") == "audio"]
            result["audio_codec"] = audio[0] if audio else None
            if output.suffix.lower() == ".mp4" and audio and audio[0] != "aac":
                warnings.append(
                    f"Render audio is {audio[0]} (Resolve on Linux cannot encode AAC). Re-mux with "
                    "ffmpeg (-c:v copy -c:a aac -b:a 320k) before uploading to platforms that require AAC."
                )
        return result
