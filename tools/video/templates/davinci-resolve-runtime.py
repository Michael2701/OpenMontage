"""DaVinci Resolve scripting runtime for the ``davinci_resolve`` tool.

This file is executed by the Python interpreter that can load Resolve's
``fusionscript`` library — on Linux distrobox installs that is the container's
``python3``, never the OpenMontage virtualenv. It therefore uses the standard
library only and must stay importable on any Python >= 3.8.

Protocol (one operation per process):

    python3 davinci-resolve-runtime.py <request.json> <response.json>

The request is ``{"op": "<name>", ...}``. The response is always written,
even on failure, as ``{"ok": bool, "data": {...}, "warnings": [...],
"error": str | None}``. The host tool owns all OpenMontage semantics
(artifact parsing, asset resolution, transcoding); this runtime only speaks
the Resolve API on a canonical, already-resolved clip plan.

Resolve API behaviour this runtime relies on (verified on Studio 21.2):

* ``MediaPool.AppendToTimeline`` clip-info ``endFrame`` is EXCLUSIVE.
* Still images ignore ``startFrame``/``endFrame`` — the host converts stills
  to video clips before they reach this runtime.
* ``SetLUT`` only resolves LUTs inside Resolve's LUT tree; custom LUTs are
  copied into ``<master LUT dir>/OpenMontage`` and the list is refreshed.
* While the Project Manager overlay is up, UI-bound calls (``OpenPage``,
  ``SetRenderSettings``…) silently return ``None``; reloading the project
  restores the main window.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import traceback

DEFAULT_MODULE_DIRS = (
    "/opt/resolve/Developer/Scripting/Modules",
    "/opt/resolve/libs/Fusion/Modules",
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules",
    r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules",
)
DEFAULT_LUT_DIRS = (
    "/opt/resolve/LUT",
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT",
    r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\LUT",
)
LUT_SUBDIR = "OpenMontage"
MEDIA_TYPE = {"video": 1, "audio": 2}


class ResolveError(RuntimeError):
    """A failure that should be reported to the agent verbatim."""


# --------------------------------------------------------------------------
# Connection and session helpers
# --------------------------------------------------------------------------


def _import_dvr():
    module_dir = os.environ.get("RESOLVE_SCRIPT_API")
    candidates = []
    if module_dir:
        candidates.append(os.path.join(module_dir, "Modules"))
        candidates.append(module_dir)
    candidates.extend(DEFAULT_MODULE_DIRS)
    for path in candidates:
        if os.path.isfile(os.path.join(path, "DaVinciResolveScript.py")) and path not in sys.path:
            sys.path.append(path)
    try:
        import DaVinciResolveScript  # type: ignore
    except ImportError as exc:
        raise ResolveError(
            "DaVinciResolveScript module not found; set RESOLVE_SCRIPT_API to the "
            "Resolve 'Developer/Scripting' directory."
        ) from exc
    return DaVinciResolveScript


def connect(timeout_seconds: float):
    dvr = _import_dvr()
    deadline = time.monotonic() + max(timeout_seconds, 0)
    while True:
        resolve = dvr.scriptapp("Resolve")
        if resolve:
            return resolve
        if time.monotonic() >= deadline:
            raise ResolveError(
                "Could not connect to DaVinci Resolve. Make sure Resolve Studio is "
                "running and Preferences > System > General > External scripting "
                "using is set to 'Local'. The free edition does not accept external "
                "scripting connections."
            )
        time.sleep(2)


def ensure_main_window(resolve, project_manager, project, warnings):
    """Recover from the Project Manager overlay, where UI calls return None."""
    if resolve.GetCurrentPage():
        return project
    name = project.GetName()
    project_manager.SaveProject()
    project_manager.CloseProject(project)
    reopened = project_manager.LoadProject(name)
    if not reopened:
        raise ResolveError(f"Could not reopen project '{name}' to restore the main window")
    warnings.append("Resolve was showing the Project Manager; reopened the project to restore the main window.")
    return reopened


def open_project(resolve, name, create, settings, warnings):
    pm = resolve.GetProjectManager()
    current = pm.GetCurrentProject()
    created = False
    if current and current.GetName() == name:
        project = current
    else:
        project = pm.LoadProject(name)
        if not project and create:
            project = pm.CreateProject(name)
            created = bool(project)
        if not project:
            raise ResolveError(
                f"Project '{name}' could not be {'created or ' if create else ''}loaded "
                "in the current Resolve project library."
            )
    project = ensure_main_window(resolve, pm, project, warnings)

    if settings:
        apply_project_settings(project, settings, created, warnings)
    return pm, project, created


def apply_project_settings(project, settings, created, warnings):
    """Timeline format can only change before the project holds timelines."""
    wanted = {
        "timelineFrameRate": settings.get("fps"),
        "timelineResolutionWidth": settings.get("width"),
        "timelineResolutionHeight": settings.get("height"),
    }
    can_change = created or project.GetTimelineCount() == 0
    for key, value in wanted.items():
        if value is None:
            continue
        value = _fmt_setting(value)
        current = _fmt_setting(project.GetSetting(key))
        if current == value:
            continue
        if can_change:
            if not project.SetSetting(key, value):
                warnings.append(f"Resolve rejected project setting {key}={value}")
        else:
            warnings.append(
                f"Project already has timelines; keeping {key}={current} (requested {value})."
            )


def _fmt_setting(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else str(number)


def find_timeline(project, name):
    for index in range(1, project.GetTimelineCount() + 1):
        timeline = project.GetTimelineByIndex(index)
        if timeline and timeline.GetName() == name:
            return timeline
    return None


def require_timeline(project, name):
    if name:
        timeline = find_timeline(project, name)
        if not timeline:
            raise ResolveError(f"Timeline '{name}' not found in project '{project.GetName()}'")
        project.SetCurrentTimeline(timeline)
        return timeline
    timeline = project.GetCurrentTimeline()
    if not timeline:
        raise ResolveError("No timeline name given and the project has no current timeline")
    return timeline


def timeline_fps(project, timeline):
    value = timeline.GetSetting("timelineFrameRate") or project.GetSetting("timelineFrameRate")
    return float(value)


# --------------------------------------------------------------------------
# Media pool
# --------------------------------------------------------------------------


def media_folder(media_pool, path_parts):
    folder = media_pool.GetRootFolder()
    for part in path_parts:
        match = next((sub for sub in folder.GetSubFolderList() or [] if sub.GetName() == part), None)
        folder = match or media_pool.AddSubFolder(folder, part)
        if not folder:
            raise ResolveError(f"Could not create media pool folder '{part}'")
    return folder


def import_media(media_pool, folder, paths):
    """Import each distinct path once; reuse clips already in the folder."""
    media_pool.SetCurrentFolder(folder)
    existing = {}
    for clip in folder.GetClipList() or []:
        existing[os.path.realpath(clip.GetClipProperty("File Path") or "")] = clip
    missing = [p for p in dict.fromkeys(paths) if os.path.realpath(p) not in existing]
    if missing:
        for clip in media_pool.ImportMedia(missing) or []:
            existing[os.path.realpath(clip.GetClipProperty("File Path") or "")] = clip
    result = {}
    for path in paths:
        clip = existing.get(os.path.realpath(path))
        if not clip:
            raise ResolveError(f"Resolve could not import media: {path}")
        result[path] = clip
    return result


def clip_fps(clip, fallback):
    try:
        value = float(clip.GetClipProperty("FPS"))
        return value if value > 0 else fallback
    except (TypeError, ValueError):
        return fallback


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


def op_status(request, warnings):
    resolve = connect(request.get("connect_timeout", 5))
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject()
    data = {
        "connected": True,
        "product": resolve.GetProductName(),
        "version": resolve.GetVersionString(),
        "studio": "Studio" in (resolve.GetProductName() or ""),
        "current_page": resolve.GetCurrentPage(),
        "current_project": project.GetName() if project else None,
        "python": sys.version.split()[0],
    }
    if project:
        timeline = project.GetCurrentTimeline()
        data["current_timeline"] = timeline.GetName() if timeline else None
        data["render_presets"] = project.GetRenderPresetList() or []
        data["mp4_codecs"] = project.GetRenderCodecs("mp4") or {}
    if not data["current_page"]:
        warnings.append("Project Manager overlay is open; operations will reopen the project automatically.")
    return data


def op_build_timeline(request, warnings):
    resolve = connect(request.get("connect_timeout", 5))
    settings = request.get("settings") or {}
    pm, project, created = open_project(
        resolve, request["project_name"], True, settings, warnings
    )
    media_pool = project.GetMediaPool()
    name = request["timeline_name"]

    existing = find_timeline(project, name)
    if existing:
        if not request.get("overwrite"):
            raise ResolveError(
                f"Timeline '{name}' already exists in project '{project.GetName()}'. "
                "Pass overwrite=true to rebuild it."
            )
        if not media_pool.DeleteTimelines([existing]):
            raise ResolveError(f"Could not delete existing timeline '{name}'")

    clips = request.get("clips") or []
    if not clips:
        raise ResolveError("Clip plan is empty")
    folder = media_folder(media_pool, ["OpenMontage", name])
    pool_items = import_media(media_pool, folder, [c["path"] for c in clips])

    timeline = media_pool.CreateEmptyTimeline(name)
    if not timeline:
        raise ResolveError(f"Could not create timeline '{name}'")
    project.SetCurrentTimeline(timeline)
    fps = timeline_fps(project, timeline)
    start = timeline.GetStartFrame()

    for kind in ("video", "audio"):
        needed = max([c["track"] for c in clips if c["media"] == kind] or [0])
        while timeline.GetTrackCount(kind) < needed:
            ok = timeline.AddTrack(kind, "stereo") if kind == "audio" else timeline.AddTrack(kind)
            if not ok:
                raise ResolveError(f"Could not add {kind} track {timeline.GetTrackCount(kind) + 1}")

    placed = []
    # Resolve places clips more predictably when appended in record order.
    for clip in sorted(clips, key=lambda c: (c["record_seconds"], c["media"], c["track"])):
        item = pool_items[clip["path"]]
        src_fps = clip_fps(item, fps)
        source_in = int(round(clip.get("in_seconds", 0.0) * src_fps))
        length = max(1, int(round(clip["duration_seconds"] * src_fps)))
        record = start + int(round(clip["record_seconds"] * fps))
        info = {
            "mediaPoolItem": item,
            "startFrame": source_in,
            "endFrame": source_in + length,  # exclusive
            "recordFrame": record,
            "trackIndex": clip["track"],
            "mediaType": MEDIA_TYPE[clip["media"]],
        }
        result = media_pool.AppendToTimeline([info]) or []
        if not result:
            raise ResolveError(
                f"Resolve refused to place {os.path.basename(clip['path'])} "
                f"({clip['media']} track {clip['track']} at {clip['record_seconds']:.3f}s)"
            )
        timeline_item = result[0]
        expected = int(round(clip["duration_seconds"] * fps))
        actual = timeline_item.GetDuration()
        if abs(actual - expected) > 1:
            warnings.append(
                f"{clip.get('id') or os.path.basename(clip['path'])}: placed {actual} frames, "
                f"expected {expected} (source shorter than requested?)"
            )
        placed.append(
            {
                "id": clip.get("id"),
                "media": clip["media"],
                "track": clip["track"],
                "start_frame": timeline_item.GetStart(),
                "duration_frames": actual,
            }
        )

    if not pm.SaveProject():
        warnings.append("Project save returned False")
    return {
        "project": project.GetName(),
        "project_created": created,
        "timeline": timeline.GetName(),
        "fps": fps,
        "width": int(float(project.GetSetting("timelineResolutionWidth"))),
        "height": int(float(project.GetSetting("timelineResolutionHeight"))),
        "duration_seconds": round((timeline.GetEndFrame() - start) / fps, 3),
        "video_tracks": timeline.GetTrackCount("video"),
        "audio_tracks": timeline.GetTrackCount("audio"),
        "placed": placed,
    }


def _master_lut_dir():
    configured = os.environ.get("RESOLVE_LUT_DIR")
    for path in ([configured] if configured else []) + list(DEFAULT_LUT_DIRS):
        if path and os.path.isdir(path):
            return path
    raise ResolveError("Resolve master LUT directory not found; set RESOLVE_LUT_DIR.")


def install_lut(project, lut):
    """Return a LUT path Resolve can resolve, installing custom files if needed."""
    if not os.path.isabs(lut):
        return lut  # already relative to the master LUT tree, e.g. "Film Looks/x.cube"
    if not os.path.isfile(lut):
        raise ResolveError(f"LUT file not found: {lut}")
    master = _master_lut_dir()
    real_master = os.path.realpath(master)
    if os.path.realpath(lut).startswith(real_master + os.sep):
        return os.path.relpath(os.path.realpath(lut), real_master)
    target_dir = os.path.join(master, LUT_SUBDIR)
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, os.path.basename(lut))
    shutil.copyfile(lut, target)
    project.RefreshLUTList()
    return os.path.join(LUT_SUBDIR, os.path.basename(lut))


def op_apply_lut(request, warnings):
    resolve = connect(request.get("connect_timeout", 5))
    pm, project, _ = open_project(resolve, request["project_name"], False, None, warnings)
    timeline = require_timeline(project, request.get("timeline_name"))
    lut = install_lut(project, request["lut"])
    node = int(request.get("node_index", 1))
    tracks = request.get("tracks") or list(range(1, timeline.GetTrackCount("video") + 1))

    applied, failed = 0, []
    for track in tracks:
        for item in timeline.GetItemListInTrack("video", track) or []:
            graph = item.GetNodeGraph() if hasattr(item, "GetNodeGraph") else None
            ok = bool(graph and graph.SetLUT(node, lut)) or bool(item.SetLUT(node, lut))
            if ok:
                applied += 1
            else:
                failed.append(f"V{track}:{item.GetName()}")
    if failed:
        warnings.append(f"LUT not applied to {len(failed)} item(s): {', '.join(failed[:10])}")
    if applied == 0:
        raise ResolveError(f"LUT '{lut}' was not applied to any clip")
    pm.SaveProject()
    return {"timeline": timeline.GetName(), "lut": lut, "node_index": node, "applied": applied, "failed": failed}


def _pick_codec(project, fmt, codec):
    codecs = project.GetRenderCodecs(fmt) or {}
    available = set(codecs.values())
    if codec:
        if codec not in available:
            raise ResolveError(f"Codec '{codec}' not available for '{fmt}'. Available: {sorted(available)}")
        return codec
    for candidate in ("H264_NVIDIA", "H264", "H265_NVIDIA", "H265"):
        if candidate in available:
            return candidate
    raise ResolveError(f"No H.264/H.265 encoder available for '{fmt}'. Available: {sorted(available)}")


CONFIG_CANDIDATES = (
    "~/.local/share/DaVinciResolve/configs/config.dat",
    "~/Library/Preferences/Blackmagic Design/DaVinci Resolve/config.dat",
    os.path.join(os.environ.get("APPDATA", ""), "Blackmagic Design", "DaVinci Resolve", "Preferences", "config.dat"),
)


def media_storage_roots():
    """Media Storage locations from Resolve's config (Preferences > Media Storage).

    Resolve only renders into these trees; any other TargetDir makes
    AddRenderJob fail and pops a "select a render path from within the media
    storage" dialog that blocks further UI calls.
    """
    roots = []
    for candidate in CONFIG_CANDIDATES:
        path = os.path.expanduser(candidate)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if key.startswith("Site.") and ".FS." in key and key.endswith(".Root") and os.path.isabs(value):
                    roots.append(os.path.realpath(value))
        break
    return roots


def _inside(path, roots):
    real = os.path.realpath(path)
    return any(real == root or real.startswith(root + os.sep) for root in roots)


def op_render(request, warnings):
    resolve = connect(request.get("connect_timeout", 5))
    _, project, _ = open_project(resolve, request["project_name"], False, None, warnings)
    timeline = require_timeline(project, request.get("timeline_name"))
    if project.IsRenderingInProgress():
        raise ResolveError("Resolve is already rendering; wait for it to finish")
    resolve.OpenPage("deliver")

    final_dir = request["output_dir"]
    os.makedirs(final_dir, exist_ok=True)
    if not os.access(final_dir, os.W_OK):
        raise ResolveError(f"Output directory is not writable from Resolve: {final_dir}")

    roots = media_storage_roots()
    target_dir = final_dir
    if roots and not _inside(final_dir, roots):
        if not request.get("wait", True):
            raise ResolveError(
                f"{final_dir} is outside Resolve's Media Storage ({', '.join(roots)}); "
                "use wait=true (renders are staged and moved) or add the folder in "
                "Preferences > System > Media Storage."
            )
        target_dir = os.path.join(roots[0], ".openmontage-render-staging", request["filename"])
        os.makedirs(target_dir, exist_ok=True)

    preset = request.get("preset")
    if preset:
        if not project.LoadRenderPreset(preset):
            raise ResolveError(f"Render preset '{preset}' not found. Available: {project.GetRenderPresetList()}")
        fmt_codec = project.GetCurrentRenderFormatAndCodec() or {}
    else:
        fmt = request.get("format") or "mp4"
        codec = _pick_codec(project, fmt, request.get("codec"))
        if not project.SetCurrentRenderFormatAndCodec(fmt, codec):
            raise ResolveError(f"Resolve rejected render format {fmt}/{codec}")
        fmt_codec = {"format": fmt, "codec": codec}

    settings = {
        "SelectAllFrames": True,
        "TargetDir": target_dir,
        "CustomName": request["filename"],
    }
    settings.update(request.get("extra_settings") or {})
    if not project.SetRenderSettings(settings):
        raise ResolveError(f"Resolve rejected render settings: {sorted(settings)}")

    job_id = project.AddRenderJob()
    if not job_id:
        raise ResolveError(
            "Resolve did not create a render job. Check that the target is inside Media Storage "
            f"({', '.join(roots) or 'unknown'}) and dismiss any dialog open in Resolve."
        )
    job = next((j for j in project.GetRenderJobList() or [] if j.get("JobId") == job_id), {})
    output_path = os.path.join(job.get("TargetDir", target_dir), job.get("OutputFilename", ""))

    if not project.StartRendering([job_id], False):
        raise ResolveError("Resolve refused to start rendering")
    data = {
        "job_id": job_id,
        "timeline": timeline.GetName(),
        "format": fmt_codec.get("format"),
        "codec": fmt_codec.get("codec"),
        "output_path": output_path,
    }
    if not request.get("wait", True):
        data["status"] = "Rendering"
        return data

    deadline = time.monotonic() + float(request.get("timeout_seconds", 3600))
    while project.IsRenderingInProgress():
        if time.monotonic() > deadline:
            project.StopRendering()
            raise ResolveError(f"Render exceeded timeout and was stopped (job {job_id})")
        time.sleep(1)
    status = project.GetRenderJobStatus(job_id) or {}
    data["status"] = status.get("JobStatus")
    data["render_ms"] = status.get("TimeTakenToRenderInMs")
    if request.get("cleanup_job", True):
        project.DeleteRenderJob(job_id)
    if data["status"] != "Complete":
        raise ResolveError(f"Render finished with status {data['status']}: {status.get('Error', '')}".strip())
    if target_dir != final_dir:
        final_path = os.path.join(final_dir, os.path.basename(output_path))
        shutil.move(output_path, final_path)
        shutil.rmtree(target_dir, ignore_errors=True)
        try:
            os.rmdir(os.path.dirname(target_dir))  # only succeeds once the staging root is empty
        except OSError:
            pass
        data["staged_via"] = target_dir
        data["output_path"] = final_path
    return data


OPERATIONS = {
    "status": op_status,
    "build_timeline": op_build_timeline,
    "apply_lut": op_apply_lut,
    "render": op_render,
}


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: davinci-resolve-runtime.py <request.json> <response.json>\n")
        return 2
    warnings = []
    response = {"ok": False, "data": {}, "warnings": warnings, "error": None}
    try:
        with open(argv[1], encoding="utf-8") as handle:
            request = json.load(handle)
        handler = OPERATIONS.get(request.get("op"))
        if not handler:
            raise ResolveError(f"Unknown operation '{request.get('op')}'. Valid: {sorted(OPERATIONS)}")
        response["data"] = handler(request, warnings)
        response["ok"] = True
    except ResolveError as exc:
        response["error"] = str(exc)
    except Exception as exc:  # report unexpected API failures with context
        response["error"] = f"{type(exc).__name__}: {exc}"
        response["traceback"] = traceback.format_exc()
    with open(argv[2], "w", encoding="utf-8") as handle:
        json.dump(response, handle, indent=2)
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
