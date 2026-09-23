# CLAUDE.md — CapCut Bridge Kit

Two scripts now: [`capcut-bridge-macos.py`](capcut-bridge-macos.py) (original,
macOS only, both lanes) and [`capcut-bridge-windows.py`](capcut-bridge-windows.py)
(Windows port added 2026-09-23, file lane only — no live-editor control, see
that file's header for exactly what didn't port and what's unverified).

Read [`README.md`](README.md) first — it covers everything: the two lanes
(file vs. live), the full command list, and the seven hard-won rules that
break silently if skipped (quit CapCut before a file-lane write, hardlink
footage under `~/Movies` [macOS] / anywhere then hardlinked into the draft's
own `Resources/` [Windows], wipe `Timelines/` before re-editing an opened
draft [macOS-verified; unverified on Windows], only ADD to a hand-edited
draft, repair null `source_timerange`, never click through the export share
screen [macOS live lane only], call Kie AI's REST API directly instead of
its MCP tools).

For the two input JSON files (`cuts.json`, `graphics-plan.json`) that
`replay` and `graphics` expect to already exist, see
[`INPUT-CONTRACT.md`](INPUT-CONTRACT.md). The Windows script looks for them
under `<script dir>/projects/<job>/` by default (override with `--jobs-dir`
or `CAPCUT_KIT_JOBS_DIR`).

Everything else here is content, not code: `presets/` are the two caption
looks the templates implement, `assets/fonts/` are the fonts those looks
assume are installed. `capcut-templates/` (raw material stubs `add-text`/
`graphics` fill in) is **not included** — see README for how to source it.

Run macOS commands via `uv run capcut-bridge-macos.py <command> ...`; Windows
via `python capcut-bridge-windows.py <command> ...` (needs `ffmpeg`/`ffprobe`
on PATH, nothing else). See README for the full command list per platform.
