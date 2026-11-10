# Series Production — multi-episode shows with a shared bible

Use when the user works on a **series**: several episodes that share
characters, voices, references, look and a season-long story.
API: `lib/series.py`. Schemas: `schemas/series/`.

## Layout

```
projects/_series/<series>/          series home (Backlot ignores "_" dirs)
  series.json                       identity, seasons, episode index
  bible/season-01.md                season description: premise, arcs, tone
  bible/season-01-script.md         season-long script / beat outline
  bible/characters/<name>.json      character_design artifact per character
  shared/references/                character + location reference images
  shared/voices/voices.json         locked voice per character
  shared/voices/samples/            approved voice samples
  shared/music/  shared/luts/       themes, series grade
  episodes/s01e01 -> ../../<series>-s01e01
projects/<series>-s01e01/           the episode: a normal pipeline project
```

Each episode is an ordinary project with `"series"` in `project.json`, so the
pipeline, checkpoints, tools and the Backlot board (grouped under the series)
work unchanged.

## Commands

```python
from lib import series
series.init_series("night-shift", title="Night Shift", pipeline_type="animation",
                   style_playbook="anime-ghibli")
series.add_season("night-shift", 1, title="First winter")
ep = series.init_episode("night-shift", 1, 1, title="Pilot")   # -> projects/night-shift-s01e01
ctx = series.episode_context(ep)                               # what to read before working
series.set_voice("night-shift", "nadia", provider="elevenlabs_tts", voice_id="...",
                 model="...", settings={...}, sample="/abs/path/sample.wav")
series.voice_for("night-shift", "nadia")
```

Then open the board as usual: `python -m backlot open <series>-s01e01`.

## Rules (binding)

1. **Read the series before every episode stage.** Before `idea`/`script`/
   `scene_plan` of an episode, call `episode_context()` and read the season
   description, season script, every character file, `voices.json`, and the
   scripts of `previous_episodes` (their `artifacts/script.json`). An episode
   must not contradict the bible or earlier episodes.
2. **Shared assets are referenced, never copied.** Use the absolute paths
   from `episode_context()` in `asset_manifest` and tool inputs
   (reference images for image/video generation, LUTs for grading, music).
   A file generated for one episode that the whole show will reuse (a new
   location, a theme) goes into `shared/`, not the episode.
3. **Voices are locked series-wide.** TTS for a character uses exactly the
   provider, voice_id, model and settings from `voices.json`. Changing a
   character's voice is a major decision: ask the user, update with
   `set_voice()`, and log `category: "voice_selection"` with
   `subject: "<character> voice"` in the episode's `decision_log`. If a
   character has no locked voice yet, pick one with the user in the first
   episode they speak in, and lock it before generating narration.
4. **Bible changes are user decisions.** Propose edits to `bible/` and
   `characters/`; write them only after approval. Episode-specific facts that
   future episodes depend on (a death, a new ally) get added to the season
   description once the episode script is approved.
5. **Keep look consistent.** The series `style_playbook` is the default for
   every episode; per-episode changes need approval.
6. Episode project ids are `<series>-sXXeYY`; never rename episode folders
   (checkpoints and the board key on the id).

## Git

`projects/` is gitignored by OpenMontage. The series home holds valuable
text (bible, scripts, voices.json). If the user wants it versioned, make
`projects/_series/<series>/` its own git repo and ignore `shared/` media
other than `*.json`/`*.md` — renders and generated media never go in git.
