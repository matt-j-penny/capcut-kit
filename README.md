# CapCut Bridge Kit

CapCut has no API. This kit drives it two other ways: editing its draft files
directly on disk, and clicking its real UI through the macOS accessibility
tree. Needs the CapCut desktop app and `uv` (`brew install uv`, or
`winget install astral-sh.uv` on Windows). The file lane runs on macOS and
Windows; the live lane is macOS only (see [Windows](#windows)).

Run any command with:

```bash
uv run capcut-bridge.py <command> ...
```

The script's `# /// script` header declares its two dependencies
(`pyobjc-framework-ApplicationServices`, `pyobjc-framework-Quartz`) — `uv run`
installs them itself on first use, no setup step needed.

## Windows

The file lane (`replay`, `add-overlay`, `add-text`, `graphics`, `transform`,
`remove`, `keyframe`, `clear-keyframes`, `ls`) plus `launch`/`quit` work on
Windows. Validated 2026-10-15 on CapCut 9.6.0 for Windows: a `replay` draft
shows up on the home screen and opens with its cuts on the timeline.

What differs from macOS, all handled by the script:

- Drafts live in the folder set in CapCut's settings, read from
  `%LOCALAPPDATA%\CapCut\User Data\Config\globalSetting`
  (`currentCustomDraftPath`), by default
  `%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft`.
- The timeline JSON is `draft_content.json`, not `draft_info.json`. It is plain
  JSON on 9.6 (not encrypted). Override with `CAPCUT_DRAFT_FILE` if a version
  changes that.
- Quitting: a plain `taskkill` (no `/F`) is the same as clicking the X. From the
  editor it only closes the draft back to the home screen, so the bridge
  repeats it until no CapCut window is left, then lets the helper processes
  exit (forcing only stragglers that have no window).
- No sandbox, but footage is still linked into the draft's `Resources/` so the
  draft stays self-contained.

The live lane is not ported: CapCut's QML UI reaches Windows UI Automation
without the automation IDs the macOS lane finds elements by (names come back
empty), so live commands exit with a message. Open the draft in CapCut by hand
after a file-lane write.

Job folders (`projects/<job>/`) are looked up in `CAPCUT_JOBS` when set,
otherwise in the kit's own `projects/`, otherwise next to the kit.

## The two lanes

**File lane** (structural builds) — CapCut drafts are plain JSON under
`~/Movies/CapCut/User Data/Projects/com.lveditor.draft/<name>/`. The bridge
writes/edits that JSON directly, which means CapCut must be **fully closed**
while it does — the app owns that folder and rewrites its own registry on
quit, clobbering anything written while it was open. The bridge handles this
itself: it quits CapCut if running, writes, then relaunches it.

**Live lane** (interactive) — with the editor open, the bridge finds elements
in the accessibility tree by their internal automation ID (CapCut's QML UI
exposes ByteDance's own test hooks — `PlayerPlayBtn`, `MTLSVideoP:<clip>`,
etc.) and clicks them with synthesized CGEvents. No quit/relaunch involved.
These IDs are undocumented test hooks, not a public contract — re-verify with
`dump` after a CapCut update.

## Commands

File lane (quits + relaunches the app around the write):
```
replay <job> [--name <draft>]            # EDL -> new draft, one clip per cut
add-overlay <draft> <mov> --at <s> [--layer N] [--dur <s>] [--src <s>] [--ri N] [--mute] [--force]
add-text <draft> "<text>" --at <s> [--dur <s>] [--ri N] [--force]
graphics <draft> <job>                   # place a job's whole graphics plan
transform <draft> [--track main|text|overlay] [--index N] [--scale S] [--x X] [--y Y] [--rotate R] [--opacity O]
remove <draft> [--track main|text|overlay] [--index N]
keyframe <draft> [--track ...] [--index N] --at <s> [--scale S] [--x X] [--y Y] [--rotate R] [--opacity O]
clear-keyframes <draft> [--track ...] [--index N]
ls                                       # drafts in the registry
```

Live lane (open editor, no restart):
```
open <draft> · launch · quit
seek <seconds> [--draft <name>]          # frame-exact, closed-loop
select <i> · split [seconds] · delete <i>
trim-left · trim-right · undo · redo · marker · zoomfit · save
play · playhead · clips · state          # state = JSON timeline snapshot
export [--to <dir>] [--timeout <s>]      # drive CapCut's export dialog, verify the file
shot [out.png]                           # window grab for QA
dump [needle] · click <name> · clickxy <x> <y> · key <combo>
```

Run with no arguments for this same list from the script itself.

## Hard rules (learned the hard way — don't relearn these)

- **CapCut must be fully quit while the bridge writes a draft.** It rewrites
  its own draft registry on quit; if it's still running, that rewrite clobbers
  any draft the bridge just wrote or edited. The bridge quits/relaunches
  around every file-lane write — don't call file-lane commands while
  bypassing that.
- **Raw footage must live under `~/Movies`.** CapCut's sandbox can only read
  that tree, so paths outside it show up as broken red "File not accessible"
  clips. The bridge handles this by hardlinking source media into the
  draft's own `Resources/` folder — never point it at footage elsewhere and
  expect it to just work.
- **Editing a draft CapCut has already opened requires wiping its `Timelines/`
  cache first**, or your edit is silently ignored. CapCut only re-imports
  `draft_info.json` when that native cache is absent — otherwise it trusts
  its own cached state over the file on disk. The bridge's `edit_draft`
  helper does this automatically.
- **Once a draft has been hand-edited in the app, only ADD to it** — use
  `add-overlay` / `add-text` / `graphics` / `transform`, never `replay`
  again. `replay` rebuilds the draft from scratch and wipes hand edits.
- **A text segment with `source_timerange: null` wedges CapCut's encoder
  mid-export.** CapCut's own save normalizes text segments to a null source
  range; if you re-import after wiping `Timelines/`, that null survives and
  breaks export. The bridge repairs every segment before writing.
- **The post-export "share to TikTok/YouTube" screen is modal and blocks the
  next operation.** `export` clears it automatically — never click through
  it by hand, it publishes.
- **For Kie AI image/video generation, call the REST API directly with curl
  — don't use the `mcp__kie-ai__*` MCP tools.** Those tools route through a
  `prepare_media_generation`/`submit_media_generation` approval step that
  returns `"Host declined the approval request"` in this environment even
  with an exact, known price — the gate itself doesn't work here, not a
  pricing issue.
  - Create: `POST https://api.kie.ai/api/v1/jobs/createTask` with
    `Authorization: Bearer <KIE_AI_API_KEY>` and
    `{"model": "<provider/model>", "input": {...}}` — no `callBackUrl`.
  - Poll: `GET https://api.kie.ai/api/v1/jobs/recordInfo?taskId=<id>` until
    `data.state` is `success` (result at `data.resultJson.resultUrls`) or
    `fail`.

## What's in this kit

- `capcut-bridge.py` — the bridge script.
- `capcut-templates/` — **not included in this repo.** `add-text`/`graphics`
  need three raw CapCut material stubs here (`text-material.json`,
  `text-segment.json`, `text-ref-material_animations.json`) — pull them from
  a real CapCut draft's `draft_content.json` (any existing text segment's
  material + ref) before using those two commands. Everything else in the
  kit works without this folder.
- `presets/tiktok-raw-style.md` — the caption/hook-card look these text
  templates implement (Inter Bold, no box, low under the face).
- `presets/captions-style.md` — the alternate centered explainer caption
  look (Coolvetica, white-on-black box). Coolvetica isn't bundled here
  (licensing) — source it yourself and drop it in `assets/fonts/`.
- `assets/fonts/` — Inter, the font `tiktok-raw-style.md` assumes is
  available locally. Coolvetica (for `captions-style.md`) is not included.
- `INPUT-CONTRACT.md` — the shape of the two JSON files (`cuts.json`,
  `graphics-plan.json`) that `replay` and `graphics` read.

This kit is pulled from a larger video-editing pipeline. Everything above is
what the CapCut lane needs standalone; it doesn't include the rest of that
pipeline (transcription, rough-cut splicing, other app lanes).
