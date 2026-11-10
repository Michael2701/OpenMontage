# DaVinci Resolve — NLE Hand-off, Grading and Finishing

> Tool: `davinci_resolve` (`tools/video/davinci_resolve.py`, capability `nle_finishing`)
> Layer 3: `.agents/skills/davinci-resolve/SKILL.md` — read it before the first call.

## What it is for

`davinci_resolve` turns OpenMontage's canonical edit into a **real, editable
Resolve timeline**, optionally grades it with a LUT, and renders through
Resolve's encoders. Use it when a human will keep working in Resolve or when
Resolve's grading/encoding is wanted for delivery:

- "Open this in DaVinci", "give me a Resolve timeline", "I want to finish/grade it myself"
- colorist hand-off, LUT-based looks, NVENC/ProRes/DNxHR delivery
- footage-led pipelines (`talking-head`, `documentary-montage`, `cinematic`,
  `hybrid`, `clip-factory`) where cuts are real media files

It is **not** a `render_runtime`. `edit_decisions.render_runtime` stays
`remotion | hyperframes | ffmpeg`; Resolve is an additional finishing path.

## Pipeline placement

| When | Operation | Inputs |
|---|---|---|
| After the **edit** stage checkpoint (edit_decisions + asset_manifest exist) | `build_timeline` | artifacts by path, `project_dir` |
| Grading pass (optional) | `apply_lut` | `lut` |
| **compose** stage, when the user chose Resolve for delivery | `render` or `finish` | `output_dir` = `projects/<p>/renders` |

`finish` = build + optional LUT + render in one call.

### Decision communication (binding)

Before the first call, announce: tool `davinci_resolve`, provider DaVinci
Resolve Studio (local, $0), what will be created (project/timeline names) and
whether it renders. Log a `decision_log` entry `category: "nle_handoff"`,
`subject: "Resolve hand-off"`, listing the alternatives considered
(`video_compose` only, Resolve timeline only, Resolve finish). Switching the
final deliverable from `video_compose` to a Resolve render is a major change —
ask first.

## Templated vs footage edits

Resolve receives **media files**, not React/HTML scenes. Cuts whose `source`
does not resolve to a file (text cards, charts, component scenes) become gaps
and are reported in `warnings`. For templated/atelier pieces:

1. Render with `video_compose` as usual.
2. Hand off the render plus stems with an explicit `clips` plan, e.g. the
   composed video on V1 and narration/music WAVs on A2/A3, so the editor can
   still rebalance audio and grade.

## Track layout

| Track | Content |
|---|---|
| V1 | `cuts[]` with layer `primary`/`background`, end-to-end in array order (same cursor as `video_compose`) |
| V2 | `cuts[]` with layer `overlay` |
| V3 | `overlays[]` at `start_seconds` |
| A1 | source audio of video cuts (`include_source_audio`) |
| A2 | `audio.narration.segments[]` |
| A3 | `audio.music` from 0, trimmed to programme length |

## What is reproduced and what is not

Reproduced exactly: order, trims, record positions, stills as exact-length
clips (`fit` pad/cover like `video_compose`), timeline fps/resolution
(`compose_target` honoured), LUT on node 1.

Not settable over the API — reported in `warnings` for the editor to finish:
speed changes, transitions, transforms/Ken Burns, overlay position/opacity,
music volume/ducking/fades, subtitles (import the `.srt` via File > Import >
Subtitle). Never claim these were applied.

## Calls

```python
from tools.tool_registry import registry
registry.discover()
davinci_resolve = registry.get("davinci_resolve")

davinci_resolve.execute({"operation": "status"})          # connection, edition, codecs

davinci_resolve.execute({
    "operation": "build_timeline",
    "project_name": "<project>", "timeline_name": "<project> v1",
    "project_dir": "projects/<project>",
    "edit_decisions": "projects/<project>/artifacts/edit_decisions.json",
    "asset_manifest": "projects/<project>/artifacts/asset_manifest.json",
    "overwrite": True,                              # rebuild after re-edits
})

davinci_resolve.execute({
    "operation": "finish", "project_name": "<project>", "timeline_name": "<project> v1",
    "project_dir": "projects/<project>", "edit_decisions": "...", "asset_manifest": "...",
    "lut": "Film Looks/Rec709 Kodak 2383 D65.cube",  # or an absolute .cube path
    "output_dir": "projects/<project>/renders", "filename": "<project>_resolve",
})
```

Prepared media (stills → H.264 clips, AAC/MP3 → PCM WAV) is cached under
`<project_dir>/resolve/media/` and reused on rebuilds.

## Review checklist

- `data.warnings` relayed to the user verbatim (gaps, unsupported features).
- `timeline.duration_seconds` matches the edit's planned runtime.
- Render: `ffprobe` the artifact; on Linux re-mux audio to AAC before
  platform upload (the tool warns when needed).
- Feed the render into `render_report` like any other compose output.

## Setup and troubleshooting

| Symptom | Fix |
|---|---|
| `status` → tool unavailable | Resolve Studio not found. Native installs are auto-detected; for distrobox/podman set `RESOLVE_RUNNER=podman`, `RESOLVE_CONTAINER=<name>` |
| "Could not connect" | Resolve not running, free edition, or External scripting ≠ Local. `launch_if_needed: true` + `RESOLVE_LAUNCH_COMMAND` starts it |
| UI calls fail / render job not created | A modal dialog is open in Resolve — ask the user to dismiss it; Project Manager overlay is recovered automatically |
| Media imports but is silent | Compressed audio on Linux — the tool transcodes automatically; check `prepared_media` |
| "GPU memory full" with free VRAM (hybrid laptops) | Launch Resolve with NVIDIA PRIME offload (see Layer 3 skill) |
| Paths not found inside container | Media and `output_dir` must live under a path the container mounts (e.g. `$HOME`) |
| "Please select a render path from within the media storage" | Resolve only renders inside Media Storage roots. The tool stages renders in the first root and moves them to `output_dir` (`render.staged_via`); `wait: false` needs `output_dir` inside Media Storage |
