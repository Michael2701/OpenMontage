# Fork additions (Michael2701/OpenMontage, branch `work`)

These features live only in this fork. They are added as NEW files so upstream
merges never conflict — do not edit upstream-owned files to register them;
register them here instead.

## Series / multi-episode shows
When the user works on a series, read `skills/meta/series-production.md`
before any episode stage. API: `lib/series.py`, schemas: `schemas/series/`.

## DaVinci Resolve (capability `nle_finishing`)
Hand-off to DaVinci Resolve Studio: builds an editable Resolve timeline from
`edit_decisions` + `asset_manifest`, applies LUTs, renders through Resolve's
encoders. Tool `davinci_resolve` (`tools/video/davinci_resolve.py`; API calls
run out-of-process via `tools/video/templates/davinci-resolve-runtime.py`).
Read `skills/core/davinci-resolve.md` and `.agents/skills/davinci-resolve/SKILL.md`
first. It is a finishing path, not a `render_runtime` — announce it and ask
before making a Resolve render the final deliverable.
Env (optional): `RESOLVE_RUNNER` auto|native|podman|docker, `RESOLVE_CONTAINER`
(default `davincibox`), `RESOLVE_PYTHON`, `RESOLVE_LAUNCH_COMMAND`, `RESOLVE_LUT_DIR`.
