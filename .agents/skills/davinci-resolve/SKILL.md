---
name: davinci-resolve
description: DaVinci Resolve Studio external scripting API — connection, projects, media pool, timeline assembly, LUTs and render jobs, plus the verified behavioural quirks (exclusive endFrame, still durations, LUT tree, Project Manager overlay, Linux codec gaps). Use before driving Resolve through the davinci_resolve tool or writing Resolve automation.
---

# DaVinci Resolve Scripting API

Technology-level knowledge for automating DaVinci Resolve. OpenMontage usage
conventions live in `skills/core/davinci-resolve.md`; this file covers how the
API itself behaves. Facts marked *verified* were confirmed against Resolve
Studio 21.2 on Linux.

## Editions and connection

- **Studio only for external scripting.** The free edition runs scripts only
  from inside the app (Workspace > Scripts / Console). `scriptapp("Resolve")`
  from another process returns `None` on the free edition.
- Preferences > System > General > **External scripting using = Local**
  (stored as `System.Scripting.Mode = 1` in `config.dat`).
- The API connects over loopback to the running Resolve UI. Resolve must be
  running; there is no headless mode for the Python API (`-nogui` exists but
  is unsupported for most workflows).
- Module: `Developer/Scripting/Modules/DaVinciResolveScript.py`, which loads
  `fusionscript.so/.dll` (`RESOLVE_SCRIPT_LIB`). The library links against
  Resolve's private libc++/TBB/LuaJIT, so it only loads in an environment that
  can see Resolve's libs — for container installs, run Python *inside* the
  container.

| OS | API dir | fusionscript |
|---|---|---|
| Linux | `/opt/resolve/Developer/Scripting` | `/opt/resolve/libs/Fusion/fusionscript.so` |
| macOS | `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting` | `.../DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so` |
| Windows | `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting` | `C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll` |

## Object model

```
Resolve
 ├─ GetProjectManager() → ProjectManager
 │    ├─ CreateProject(name) → Project | None (None if it exists)
 │    ├─ LoadProject(name) / GetCurrentProject() / SaveProject() / CloseProject(p)
 ├─ OpenPage("media|cut|edit|fusion|color|fairlight|deliver") / GetCurrentPage()
Project
 ├─ GetMediaPool() → MediaPool
 ├─ Get/SetSetting("timelineFrameRate" | "timelineResolutionWidth" | ...)
 ├─ GetTimelineCount() / GetTimelineByIndex(i) (1-based) / SetCurrentTimeline(t)
 ├─ render: SetCurrentRenderFormatAndCodec, SetRenderSettings, LoadRenderPreset,
 │          AddRenderJob, StartRendering([ids], interactive), IsRenderingInProgress,
 │          GetRenderJobStatus(id), GetRenderJobList(), DeleteRenderJob(id)
 └─ RefreshLUTList()
MediaPool
 ├─ GetRootFolder() / AddSubFolder(parent, name) / SetCurrentFolder(f)
 ├─ ImportMedia([paths]) → [MediaPoolItem]
 ├─ CreateEmptyTimeline(name) / DeleteTimelines([t])
 └─ AppendToTimeline([clipInfo]) → [TimelineItem]
Timeline
 ├─ GetStartFrame() (default 01:00:00:00 → 3600*fps) / GetEndFrame()
 ├─ AddTrack("video" | "audio", "stereo") / GetTrackCount(type)
 └─ GetItemListInTrack(type, index)
TimelineItem
 ├─ GetStart() / GetEnd() / GetDuration() / GetLeftOffset()
 ├─ GetNodeGraph() → Graph (SetLUT(node, path), GetLUT(node))
 └─ SetLUT(node, path)   (older entry point; prefer the graph)
```

## Verified behaviour and quirks

1. **`AppendToTimeline` clipInfo `endFrame` is exclusive.** `startFrame=30,
   endFrame=59` yields 29 frames. Use `endFrame = startFrame + length`.
   Frames are in the *clip's* frame rate (`GetClipProperty("FPS")`);
   `recordFrame` is absolute timeline frames (`GetStartFrame() + seconds*fps`).
   `mediaType`: 1 = video only, 2 = audio only.
2. **Stills ignore `startFrame/endFrame`.** A PNG always lands with the
   project's standard still duration (5 s). `SetClipProperty("Duration")` is
   rejected. Convert stills to an exact-length video clip first.
3. **Timeline format is locked once a project has timelines.** Set
   `timelineFrameRate` / resolution right after `CreateProject`.
4. **Project Manager overlay blocks UI calls.** While it is shown,
   `GetCurrentPage()` is `None` and `OpenPage`, `SetRenderSettings`,
   `SetCurrentRenderFormatAndCodec`, `AddRenderJob` silently return `None`
   while read calls still work. Recover with `SaveProject → CloseProject →
   LoadProject`. Modal dialogs in the UI cause the same symptom — a human must
   dismiss them.
5. **LUT paths resolve against Resolve's LUT tree only.** System LUTs work
   absolute or relative (`"Film Looks/Rec709 Kodak 2383 D65.cube"`). A custom
   `.cube` elsewhere — even in the user LUT folder — is rejected until it is
   copied under the master LUT dir (`/opt/resolve/LUT` on Linux) and
   `project.RefreshLUTList()` is called. `GetLUT` returns the relative path.
6. **Render targets must be inside Media Storage.** Resolve only renders into
   the roots listed in Preferences > System > Media Storage (`Site.1.FS.N.Root`
   in `config.dat`). Any other `TargetDir` makes `AddRenderJob` return `""` and
   pops a modal "Please select a render path from within the media storage",
   which then blocks every UI call until dismissed. Render inside a storage
   root and move the file afterwards, or add the folder to Media Storage.
7. **`SetRenderSettings` is all-or-nothing on validation** — one unknown value
   (e.g. `AudioCodec: "aac"` on Linux) makes it return `False`.
8. **Linux codec gaps:** no AAC/MP3 decode (clips import but audio is silent
   and the log shows `Failed to decode clip`) and no AAC encode (mp4 renders
   carry FLAC/LPCM). Transcode inputs to PCM WAV; re-mux deliverables with
   ffmpeg when AAC is required. H.264/H.265 decode and NVENC encode
   (`H264_NVIDIA`, `H265_NVIDIA`, `AV1YUV420_*_NVIDIA`) work in Studio.
9. **Rendering is asynchronous.** Poll `IsRenderingInProgress()`, then read
   `GetRenderJobStatus(id)["JobStatus"]` (`Complete`, `Failed`, `Cancelled`).
10. **Speed, transitions, transforms, audio levels and subtitles** have no
    reliable setter in the scripting API. Leave them to the editor or bake
    them into media before import.

## Minimal external script

```python
import sys
sys.path.append("/opt/resolve/Developer/Scripting/Modules")
import DaVinciResolveScript as dvr

resolve = dvr.scriptapp("Resolve")          # None → not running / not Studio
pm = resolve.GetProjectManager()
project = pm.CreateProject("Demo") or pm.LoadProject("Demo")
project.SetSetting("timelineFrameRate", "30")
pool = project.GetMediaPool()
clip, = pool.ImportMedia(["/abs/path/shot.mp4"])
tl = pool.CreateEmptyTimeline("Main")
start = tl.GetStartFrame()
pool.AppendToTimeline([{"mediaPoolItem": clip, "startFrame": 0, "endFrame": 90,
                        "recordFrame": start, "trackIndex": 1, "mediaType": 1}])
project.SetCurrentRenderFormatAndCodec("mp4", "H264_NVIDIA")
project.SetRenderSettings({"SelectAllFrames": True, "TargetDir": "/abs/out", "CustomName": "demo"})
job = project.AddRenderJob()
project.StartRendering([job], False)
```

## Hybrid-GPU Linux laptops

If Resolve logs `Failed to register OpenGL object for CUDA interop` and shows
"GPU memory full" while VRAM is mostly free, the UI's OpenGL context is on the
iGPU (Mesa) while processing runs on CUDA. Launch with NVIDIA PRIME offload:
`__NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia`. With distrobox,
set them *inside* the container (`distrobox-enter -n box -- env VAR=... cmd`)
because host variables starting with `__` are not forwarded.
