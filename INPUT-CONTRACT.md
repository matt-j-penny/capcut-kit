# Input files the bridge reads

Neither of these is produced by anything in this kit — they're expected to
already exist under `projects/<job>/` before you run `replay` or `graphics`.

## `transcript/cuts.json` — read by `replay`

The edit decision list: which spans of the raw clip to keep, in order. Each
entry becomes one timeline segment.

```json
{
  "clip": "Free Buff Raw.mp4",
  "fps": 30.0,
  "segments": [
    {
      "clip": "Free Buff Raw.mp4",
      "start": 146.6,
      "end": 154.566667,
      "note": "HOOK - most people don't know / literally $0"
    },
    {
      "clip": "Free Buff Raw.mp4",
      "start": 174.866667,
      "end": 180.166667,
      "note": "no Claude, no ChatGPT, no Groq - truly $0"
    }
  ]
}
```

- `start` / `end` — seconds into the SOURCE clip (not the timeline). One
  segment per kept span; gaps between spans are the cuts.
- `note` — human-readable description of the kept line, for review — not
  read programmatically by the bridge.
- `clip` (top-level) — the raw file `replay` hardlinks into the draft's
  `Resources/`; must exist under the job's `raw/` folder.
- `clip` (per segment, optional) — lets segments come from different raw
  files (several takes of one video). Each one must exist under `raw/`;
  segments without it use the top-level `clip`.
- The draft canvas takes the first clip's resolution, so vertical (9:16)
  footage gives a vertical draft.

## `graphics-plan.json` — read by `graphics <draft> <job>`

A list of graphic overlays to place on the timeline, one per beat. The
bridge accepts either key name below (`graphics` or `beats`).

```json
{
  "graphics": [
    { "id": "hook-card", "file": "hook-card.mov", "start": 0.0 },
    { "id": "stat-1",    "file": "stat-1.mov",    "t": 12.4 }
  ]
}
```

- `file` (or `id` + `.mov` inferred) — a rendered clip under
  `projects/<job>/assets/`. Must already be rendered — `graphics` places
  clips, it doesn't render them. Anything missing is skipped with a
  `skip <file> (not rendered yet)` message, not an error.
- `start` / `t` / `time` (any one) — seconds into the TIMELINE (not the raw
  clip) where the overlay begins.
