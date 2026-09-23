#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pyobjc-framework-ApplicationServices",
#   "pyobjc-framework-Quartz",
# ]
# ///
# CapCut bridge: drive the CapCut Mac app live + replay a job's EDL into a new
# CapCut draft. macOS only. Two lanes, validated 2026-07-27 on CapCut 8.9.0:
#
#   FILE lane  - CapCut has no API; drafts are plaintext JSON in
#                ~/Movies/CapCut/User Data/Projects/com.lveditor.draft/.
#                `replay` writes a new draft (draft_info.json + meta + root
#                registry entry) straight from projects/<job>/transcript/cuts.json,
#                one timeline segment per EDL cut, source times in microseconds.
#                The app must be CLOSED while writing (it rewrites the registry
#                on quit); the bridge quits/relaunches it around the write.
#
#   LIVE lane  - the QML UI ships ByteDance's internal automation IDs in the
#                macOS accessibility tree (PlayerPlayBtn, MTLSVideoP:<clip>,
#                currentProgress|HH:MM:SS:FF, ...). Elements are found by name
#                via the AX API and clicked with synthesized CGEvents at their
#                screen-point centers. AX IDs are test hooks, not a contract:
#                re-verify with `dump` after CapCut updates.
#
# FILE-LANE ops relaunch the app (structural, batched); LIVE-lane ops act on the
# open editor with no restart at all.
#
# Usage — file lane (build/structural):
#   replay <job> [--name <draft>]            # EDL -> new draft, one clip per cut
#   add-overlay <draft> <mov> --at <s> [--layer N] [--dur <s>]
#   add-text <draft> "<text>" --at <s> [--dur <s>]
#   graphics <draft> <job>                   # place a job's whole graphics plan
#   transform <draft> [--track main|text|overlay] [--index N] [--scale S] [--x X] [--y Y] [--rotate R] [--opacity O]
#   ls                                       # drafts in the registry
#
# Usage — live lane (open editor, no restart):
#   open <draft> · launch · quit
#   seek <seconds> [--draft <name>]          # frame-exact, closed-loop
#   select <i> · split [seconds] · delete <i>
#   trim-left · trim-right · undo · redo · marker · zoomfit · save
#   play · playhead · clips · state          # state = JSON timeline snapshot
#   shot [out.png]                           # window grab for QA
#   dump [needle] · click <name> · key <combo>

USAGE = """Usage — file lane (structural builds; quits + relaunches the app around the write):
  replay <job> [--name <draft>]            # EDL -> new draft, one clip per cut
  add-overlay <draft> <mov> --at <s> [--layer N] [--dur <s>] [--src <s>] [--ri N] [--mute] [--force]
  add-text <draft> "<text>" --at <s> [--dur <s>] [--ri N] [--force]
  graphics <draft> <job>                   # place a job's whole graphics plan
  transform <draft> [--track main|text|overlay] [--index N] [--scale S] [--x X] [--y Y] [--rotate R] [--opacity O]
  remove <draft> [--track main|text|overlay] [--index N]
  keyframe <draft> [--track ...] [--index N] --at <s> [--scale S] [--x X] [--y Y] [--rotate R] [--opacity O]
  clear-keyframes <draft> [--track ...] [--index N]
  ls                                       # drafts in the registry

Usage — live lane (open editor, no restart):
  open <draft> · launch · quit
  seek <seconds> [--draft <name>]          # frame-exact, closed-loop
  select <i> · split [seconds] · delete <i>
  trim-left · trim-right · undo · redo · marker · zoomfit · save
  play · playhead · clips · state          # state = JSON timeline snapshot
  export [--to <dir>] [--timeout <s>]      # drive CapCut's export dialog, verify the file
  shot [out.png]                           # window grab for QA
  dump [needle] · click <name> · clickxy <x> <y> · key <combo>"""
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

DRAFT_ROOT = Path.home() / "Movies/CapCut/User Data/Projects/com.lveditor.draft"
APP = "CapCut"

# ---------------------------------------------------------------- app lifecycle


def app_pid():
    try:
        return int(subprocess.check_output(["pgrep", "-x", APP]).split()[0])
    except subprocess.CalledProcessError:
        return None


def quit_app(timeout=20):
    if app_pid() is None:
        return
    subprocess.run(["osascript", "-e", f'tell application "{APP}" to quit'], check=False)
    t0 = time.time()
    while app_pid() is not None:
        if time.time() - t0 > timeout:
            sys.exit(f"CapCut did not quit within {timeout}s, close it and re-run")
        time.sleep(0.5)


def launch_app():
    subprocess.run(["open", "-a", APP], check=True)
    subprocess.run(["osascript", "-e", f'tell application "{APP}" to activate'], check=False)


# ---------------------------------------------------------------- AX driver

from ApplicationServices import (  # noqa: E402
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
    AXValueGetValue,
    kAXValueCGPointType,
    kAXValueCGSizeType,
)
from Quartz import (  # noqa: E402
    CGEventCreateKeyboardEvent,
    CGEventCreateMouseEvent,
    CGEventPost,
    CGEventSetFlags,
    CGEventSetIntegerValueField,
    kCGEventFlagMaskCommand,
    kCGEventFlagMaskShift,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseUp,
    kCGEventMouseMoved,
    kCGHIDEventTap,
    kCGMouseButtonLeft,
    kCGMouseEventClickState,
)

KEYCODES = {"space": 49, "return": 36, "escape": 53, "delete": 51, "tab": 48,
            "left": 123, "right": 124, "down": 125, "up": 126,
            "home": 115, "end": 119,
            "a": 0, "b": 11, "c": 8, "s": 1, "v": 9, "z": 6}


def ax_app():
    pid = app_pid()
    if pid is None:
        sys.exit("CapCut is not running (use: launch)")
    return AXUIElementCreateApplication(pid)


def attr(el, name):
    err, val = AXUIElementCopyAttributeValue(el, name, None)
    return val if err == 0 else None


def geometry(el):
    ok, p = AXValueGetValue(attr(el, "AXPosition"), kAXValueCGPointType, None)
    ok2, s = AXValueGetValue(attr(el, "AXSize"), kAXValueCGSizeType, None)
    return (p.x, p.y, s.width, s.height) if ok and ok2 else None


def walk(el, visit, depth=0):
    if depth > 18:
        return
    visit(el, depth)
    for k in attr(el, "AXChildren") or []:
        walk(k, visit, depth + 1)


def elements(needle=None):
    """All (element, name, geometry) in all CapCut windows, name = title or description."""
    out = []

    def visit(el, depth):
        name = attr(el, "AXTitle") or attr(el, "AXDescription") or ""
        if needle is None or needle.lower() in name.lower():
            out.append((el, name, geometry(el)))

    for w in attr(ax_app(), "AXWindows") or []:
        walk(w, visit)
    return out


def find_one(needle, timeout=0):
    t0 = time.time()
    while True:
        hits = [h for h in elements(needle) if h[2]]
        if hits:
            return hits[0]
        if time.time() - t0 >= timeout:
            return None
        time.sleep(0.5)


_fronted = False


def ensure_front():
    """Activate CapCut before any synthesized input. Keystrokes go to whatever app
    is frontmost, so they need this outright. Clicks need it too, for a subtler
    reason that caused days of phantom flakiness: when the app is not frontmost,
    macOS spends the FIRST click activating the window and the app never sees it.
    In `calibrate` that meant probe A silently no-op'd, `ta` read a stale playhead,
    and the seconds<->pixels fit came out garbage — seek landing 40s off target,
    but only when some earlier action had not already fronted the app."""
    global _fronted
    if not _fronted:
        subprocess.run(["osascript", "-e", f'tell application "{APP}" to activate'],
                       check=False)
        time.sleep(0.4)
        _fronted = True


def click_at(x, y, clicks=1):
    ensure_front()
    e = CGEventCreateMouseEvent(None, kCGEventMouseMoved, (x, y), kCGMouseButtonLeft)
    CGEventPost(kCGHIDEventTap, e)
    time.sleep(0.05)
    for n in range(1, clicks + 1):
        for kind in (kCGEventLeftMouseDown, kCGEventLeftMouseUp):
            e = CGEventCreateMouseEvent(None, kind, (x, y), kCGMouseButtonLeft)
            CGEventSetIntegerValueField(e, kCGMouseEventClickState, n)
            CGEventPost(kCGHIDEventTap, e)
            time.sleep(0.05)


def click_element(needle, clicks=1, timeout=5):
    hit = find_one(needle, timeout)
    if hit is None:
        sys.exit(f"element not found: {needle}")
    el, name, (x, y, w, h) = hit
    click_at(x + w / 2, y + h / 2, clicks)
    return name


def send_key(combo):
    ensure_front()
    parts = combo.lower().split("+")
    key = parts[-1]
    if key not in KEYCODES:
        sys.exit(f"unknown key: {key} (known: {', '.join(sorted(KEYCODES))})")
    flags = 0
    if "cmd" in parts:
        flags |= kCGEventFlagMaskCommand
    if "shift" in parts:
        flags |= kCGEventFlagMaskShift
    for down in (True, False):
        e = CGEventCreateKeyboardEvent(None, KEYCODES[key], down)
        if flags:
            CGEventSetFlags(e, flags)
        CGEventPost(kCGHIDEventTap, e)
        time.sleep(0.03)


def read_playhead():
    cur = find_one("currentProgress")
    tot = find_one("totalProgress")
    d = lambda h: (attr(h[0], "AXDescription") or "").split("|")[-1] if h else "?"
    return d(cur), d(tot)


# ---------------------------------------------------------------- live editing

RULER_DY = 33  # ruler row sits this far below the timeline toolbar's top edge


def parse_tc(tc, fps):
    h, m, s, f = (int(x) for x in tc.split(":"))
    return h * 3600 + m * 60 + s + f / fps


def draft_fps(name=None):
    if name:
        try:
            return json.loads((DRAFT_ROOT / name / "draft_info.json").read_text())["fps"]
        except (OSError, KeyError, json.JSONDecodeError):
            pass
    return 30.0


def playhead_seconds(fps):
    cur, _ = read_playhead()
    return parse_tc(cur, fps) if cur != "?" else None


def timeline_clips():
    """Main + overlay clips, left-to-right, as (name, x, y, w, h)."""
    return sorted(((n, *g) for _, n, g in elements("MTLSVideoP") if g),
                  key=lambda c: (c[2], c[1]))


def ruler_y():
    hit = find_one("cutoff") or find_one("undo")
    if hit is None:
        sys.exit("timeline toolbar not found (is a draft open?)")
    return hit[2][1] + RULER_DY


def click_ruler(x, y=None):
    click_at(x, y if y is not None else ruler_y())
    time.sleep(0.35)


def visible_ruler_span():
    """Clickable x range of the ruler. Clip geometry alone is NOT usable: zoomed
    in, a long clip's AX box runs thousands of px off-screen in both directions
    (measured x=-2389 w=12646), so probe points derived from it land nowhere and
    the fit comes out garbage. Clamp to the timeline viewport, past the track
    headers on the left."""
    root = find_one("MainTimeLineRoot")
    if root is None:
        sys.exit("timeline not found (is a draft open?)")
    rx, _, rw, _ = root[2]
    left, right = rx + 180, rx + rw - 20
    clips = timeline_clips()
    if clips:                       # never probe past the end of the content
        right = min(right, max(c[1] + c[3] for c in clips))
        left = max(left, min(c[1] for c in clips))
    if right - left < 80:
        left, right = rx + 180, rx + rw - 20
    return left, right


def calibrate(fps):
    """Fit seconds<->pixels from the app's OWN playhead readout: click two ruler
    points and read where it landed. Self-calibrating, so it survives any zoom
    or scroll state instead of assuming the timeline origin."""
    left, right = visible_ruler_span()
    span = right - left
    xa, xb = left + span * 0.30, left + span * 0.75  # clear of the dead strip
    click_ruler(xa)
    ta = playhead_seconds(fps)
    click_ruler(xb)
    tb = playhead_seconds(fps)
    if ta is None or tb is None or abs(tb - ta) < 1e-6:
        sys.exit("calibration failed (playhead did not move) — check for a "
                 "yellow onboarding tooltip over the ruler: it swallows clicks "
                 "and is invisible to the AX tree. Run `shot` and look.")
    sec_per_px = (tb - ta) / (xb - xa)
    return lambda t: xa + (t - ta) / sec_per_px, sec_per_px


def seek(seconds, fps=30.0, tolerance_frames=0):
    """Park the playhead on an exact frame: click the mapped ruler x, then close
    the rest with arrow-key frame steps, re-reading between rounds.

    The arrow convergence is not a garnish, it is what makes this reliable. The
    ruler has a dead strip roughly 90px wide at its left edge that silently eats
    clicks (measured at two zoom levels, boundary fixed in screen space), so any
    target in the first few seconds cannot be clicked to directly — seek lands
    wherever it can and steps in. Steps must also be PACED: sent back to back
    CapCut drops most of them (195 sent, 28 applied)."""
    x_of, _ = calibrate(fps)
    click_ruler(x_of(seconds))
    for _ in range(8):
        now = playhead_seconds(fps)
        if now is None:
            break
        err = round((seconds - now) * fps)
        if abs(err) <= tolerance_frames:
            return now
        key = "right" if err > 0 else "left"
        for _ in range(min(abs(err), 400)):
            send_key(key)
            time.sleep(0.03)
        time.sleep(0.3)
    return playhead_seconds(fps)


def select_clip(index):
    clips = [c for c in timeline_clips()]
    if index >= len(clips):
        sys.exit(f"clip index {index} out of range (timeline has {len(clips)})")
    _, x, y, w, h = clips[index]
    click_at(x + w / 2, y + h / 2)
    time.sleep(0.5)
    return clips[index][0]


def window_id():
    from Quartz import (CGWindowListCopyWindowInfo, kCGNullWindowID,
                        kCGWindowListOptionOnScreenOnly)
    best = None
    for w in CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID):
        if w.get("kCGWindowOwnerName") != APP:
            continue
        b = w["kCGWindowBounds"]
        area = b["Width"] * b["Height"]
        if best is None or area > best[1]:
            best = (w["kCGWindowNumber"], area)
    return best[0] if best else None


def grab(out_path):
    """Window screenshot — the QA loop for checking a graphic actually landed."""
    wid = window_id()
    if wid is None:
        sys.exit("no CapCut window on screen")
    out = Path(out_path).expanduser().resolve()
    subprocess.run(["screencapture", "-x", "-o", "-l", str(wid), str(out)], check=True)
    return out


# ---------------------------------------------------------------- draft writing


def probe(path):
    out = json.loads(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height,duration", "-of", "json", str(path)]))
    v = next(s for s in out["streams"] if s["codec_type"] == "video")
    has_audio = any(s["codec_type"] == "audio" for s in out["streams"])
    return int(v["width"]), int(v["height"]), int(float(v["duration"]) * 1e6), has_audio


def uid():
    return str(uuid.uuid4()).upper()


def platform_block():
    # Lift device ids from an existing local draft so the new one reads as native.
    for p in sorted(DRAFT_ROOT.glob("*/draft_info.json")):
        try:
            return json.loads(p.read_text())["platform"]
        except (KeyError, json.JSONDecodeError):
            continue
    return {"os": "mac", "os_version": "", "app_id": 359289, "app_version": "8.9.0",
            "app_source": "cc", "device_id": "", "hard_disk_id": "", "mac_address": ""}


def video_material(mid, path, w, h, dur_us, has_audio):
    return {
        "id": mid, "unique_id": "", "type": "video", "duration": dur_us,
        "path": str(path), "media_path": "", "local_id": "", "has_audio": has_audio,
        "reverse_path": "", "intensifies_path": "", "reverse_intensifies_path": "",
        "intensifies_audio_path": "", "cartoon_path": "", "width": w, "height": h,
        "category_id": "", "category_name": "", "material_id": "",
        "material_name": Path(path).name, "material_url": "",
        "crop": {"upper_left_x": 0.0, "upper_left_y": 0.0, "upper_right_x": 1.0,
                 "upper_right_y": 0.0, "lower_left_x": 0.0, "lower_left_y": 1.0,
                 "lower_right_x": 1.0, "lower_right_y": 1.0},
        "crop_ratio": "free", "audio_fade": None, "crop_scale": 1.0,
        "extra_type_option": 0,
        "stable": {"stable_level": 0, "matrix_path": "",
                   "time_range": {"start": 0, "duration": 0}},
        "matting": {"flag": 0, "path": "", "interactiveTime": [],
                    "has_use_quick_brush": False, "strokes": [],
                    "has_use_quick_eraser": False, "expansion": 0, "feather": 0,
                    "reverse": False, "custom_matting_id": "",
                    "enable_matting_stroke": False, "is_clould": False,
                    "mask_video_path": "", "cloud_product_fps": 0.0},
        "source": 0, "source_platform": 0, "formula_id": "", "check_flag": 62978047,
        "video_algorithm": {"algorithms": [], "time_range": None, "path": "",
                            "gameplay_configs": [], "ai_in_painting_config": [],
                            "complement_frame_config": None, "motion_blur_config": None,
                            "deflicker": None, "noise_reduction": None,
                            "quality_enhance": None, "super_resolution": None,
                            "ai_background_configs": [], "smart_complement_frame": None,
                            "aigc_generate": None, "aigc_generate_list": [],
                            "mouth_shape_driver": None, "ai_expression_driven": None,
                            "ai_motion_driven": None, "image_interpretation": None,
                            "story_video_modify_video_config": {
                                "task_id": "", "is_overwrite_last_video": False,
                                "tracker_task_id": "", "generate_id": "",
                                "generate_card_id": ""},
                            "skip_algorithm_index": []},
        "is_unified_beauty_mode": False, "is_set_beauty_mode": False,
        "object_locked": None, "smart_motion": None, "multi_camera_info": None,
        "freeze": None, "picture_from": "none", "picture_set_category_id": "",
        "picture_set_category_name": "", "team_id": "", "local_material_id": "",
        "origin_material_id": "", "request_id": "", "has_sound_separated": False,
        "is_text_edit_overdub": False, "is_ai_generate_content": False,
        "aigc_type": "none", "is_copyright": False, "aigc_history_id": "",
        "aigc_item_id": "", "local_material_from": "", "smart_match_info": None,
        "beauty_face_preset_infos": [], "beauty_body_preset_id": "",
        "beauty_face_auto_preset": {"preset_id": "", "name": "", "rate_map": "",
                                    "scene": ""},
        "beauty_face_auto_preset_infos": [], "beauty_body_auto_preset": None,
        "live_photo_timestamp": -1, "live_photo_cover_path": "",
        "content_feature_info": None, "corner_pin": None, "surface_trackings": [],
        "video_mask_stroke": {"resource_id": "", "path": "", "type": "", "color": "",
                              "size": 0.0, "alpha": 0.0, "distance": 0.0,
                              "texture": 0.0, "horizontal_shift": 0.0,
                              "vertical_shift": 0.0},
        "video_mask_shadow": {"resource_id": "", "path": "", "color": "",
                              "alpha": 0.0, "blur": 0.0, "distance": 0.0,
                              "angle": 0.0},
    }


def segment(mid, src_start, dur, tgt_start, refs):
    return {
        "id": uid(),
        "source_timerange": {"start": src_start, "duration": dur},
        "target_timerange": {"start": tgt_start, "duration": dur},
        "render_timerange": {"start": 0, "duration": 0},
        "desc": "", "state": 0, "speed": 1.0, "is_loop": False,
        "is_tone_modify": False, "reverse": False, "intensifies_audio": False,
        "cartoon": False, "volume": 1.0, "last_nonzero_volume": 1.0,
        "clip": {"scale": {"x": 1.0, "y": 1.0}, "rotation": 0.0,
                 "transform": {"x": 0.0, "y": 0.0},
                 "flip": {"vertical": False, "horizontal": False}, "alpha": 1.0},
        "uniform_scale": {"on": True, "value": 1.0},
        "material_id": mid, "extra_material_refs": refs, "render_index": 0,
        "keyframe_refs": [], "enable_lut": True, "enable_adjust": True,
        "enable_hsl": False, "visible": True, "group_id": "",
        "enable_color_curves": True, "enable_hsl_curves": True,
        "track_render_index": 0,
        "hdr_settings": {"mode": 1, "intensity": 1.0, "nits": 1000},
        "enable_color_wheels": True, "track_attribute": 0, "is_placeholder": False,
        "template_id": "", "enable_smart_color_adjust": False,
        "template_scene": "default", "common_keyframes": [], "caption_info": None,
        "responsive_layout": {"enable": False, "target_follow": "",
                              "size_layout": 0, "horizontal_pos_layout": 0,
                              "vertical_pos_layout": 0},
        "enable_color_match_adjust": False, "enable_color_correct_adjust": False,
        "enable_adjust_mask": False, "raw_segment_id": "", "lyric_keyframes": None,
        "enable_video_mask": True, "digital_human_template_group_id": "",
        "color_correct_alg_result": "", "source": "segmentsourcenormal",
        "enable_mask_stroke": False, "enable_mask_shadow": False,
        "enable_color_adjust_pro": False,
    }


def segment_extras(materials):
    """Per-segment helper materials, the six-ref set CapCut itself writes."""
    ids = []
    for cat, entry in [
        ("speeds", {"type": "speed", "mode": 0, "speed": 1.0, "curve_speed": None}),
        ("placeholder_infos", {"type": "placeholder_info", "meta_type": "none",
                               "res_path": "", "res_text": "", "error_path": "",
                               "error_text": ""}),
        ("canvases", {"type": "canvas_color", "color": "", "blur": 0.0, "image": "",
                      "album_image": "", "image_id": "", "image_name": "",
                      "source_platform": 0, "team_id": ""}),
        ("sound_channel_mappings", {"type": "", "audio_channel_mapping": 0,
                                    "is_config_open": False}),
        ("material_colors", {"is_color_clip": False, "is_gradient": False,
                             "solid_color": "", "gradient_colors": [],
                             "gradient_percents": [], "gradient_angle": 90.0,
                             "width": 0.0, "height": 0.0}),
        ("vocal_separations", {"type": "vocal_separation", "choice": 0,
                               "removed_sounds": [], "time_range": None,
                               "production_path": "", "final_algorithm": "",
                               "enter_from": ""}),
    ]:
        eid = uid()
        materials[cat].append({"id": eid, **entry})
        ids.append(eid)
    return ids


EMPTY_MATERIAL_CATS = [
    "flowers", "tail_leaders", "audios", "images", "texts", "effects", "stickers",
    "transitions", "audio_effects", "audio_fades", "beats", "material_animations",
    "placeholders", "common_mask", "chromas", "text_templates", "realtime_denoises",
    "audio_pannings", "audio_pitch_shifts", "video_trackings", "hsl", "drafts",
    "color_curves", "hsl_curves", "primary_color_wheels", "log_color_wheels",
    "video_effects", "ai_text_effects", "audio_balances", "handwrites",
    "manual_deformations", "manual_beautys", "plugin_effects", "green_screens",
    "shapes", "digital_humans", "digital_human_model_dressing", "smart_crops",
    "ai_translates", "audio_track_indexes", "loudnesses", "vocal_beautifys",
    "smart_relights", "time_marks", "multi_language_refs", "video_shadows",
    "video_strokes", "video_radius", "videos", "canvases", "speeds",
    "placeholder_infos", "sound_channel_mappings", "material_colors",
    "vocal_separations",
]


def build_draft(name, raw_path, cuts, canvas_w, canvas_h):
    w, h, raw_dur_us, has_audio = probe(raw_path)
    plat = platform_block()
    materials = {cat: [] for cat in EMPTY_MATERIAL_CATS}
    mid = uid()
    materials["videos"].append(video_material(mid, raw_path, w, h, raw_dur_us, has_audio))

    segments = []
    cursor = 0
    for s in cuts["segments"]:
        src = round(s["start"] * 1e6)
        dur = round((s["end"] - s["start"]) * 1e6)
        segments.append(segment(mid, src, dur, cursor, segment_extras(materials)))
        cursor += dur

    return {
        "id": uid(), "version": 360000, "new_version": "175.0.0", "name": "",
        "duration": cursor, "create_time": 0, "update_time": 0, "fps": 30.0,
        "is_drop_frame_timecode": False, "color_space": 0,
        "config": {"video_mute": False, "record_audio_last_index": 1,
                   "extract_audio_last_index": 1, "original_sound_last_index": 1,
                   "subtitle_recognition_id": "", "subtitle_taskinfo": [],
                   "lyrics_recognition_id": "", "lyrics_taskinfo": [],
                   "subtitle_sync": True, "lyrics_sync": True,
                   "voice_change_sync": False, "sticker_max_index": 1,
                   "adjust_max_index": 1, "material_save_mode": 0,
                   "export_range": None, "maintrack_adsorb": True,
                   "combination_max_index": 1, "attachment_info": [],
                   "zoom_info_params": None, "system_font_list": [],
                   "multi_language_mode": "none", "multi_language_main": "none",
                   "multi_language_current": "none", "multi_language_list": [],
                   "subtitle_keywords_config": None, "use_float_render": False},
        "canvas_config": {"ratio": "original", "width": canvas_w,
                          "height": canvas_h, "background": None},
        "tracks": [{"id": uid(), "type": "video", "segments": segments,
                    "flag": 0, "attribute": 0, "name": "", "is_default_name": True}],
        "group_container": None, "materials": materials,
        "keyframes": {"videos": [], "audios": [], "texts": [], "stickers": [],
                      "filters": [], "adjusts": [], "handwrites": [], "effects": []},
        "keyframe_graph_list": [], "platform": plat,
        "last_modified_platform": plat, "mutable_config": None, "cover": None,
        "retouch_cover": None, "extra_info": None, "relationships": [],
        "mixed_track_mode_on": False, "render_index_track_mode_on": True,
        "free_render_index_mode_on": False, "static_cover_image_path": "",
        "source": "default", "time_marks": None, "path": "", "lyrics_effects": [],
        "uneven_animation_template_info": {"composition": "", "content": "",
                                           "order": "", "sub_template_info_list": []},
        "draft_type": "video",
        "smart_ads_info": {"page_from": "", "routine": "", "draft_url": ""},
    }, raw_dur_us, w, h


def meta_entry(name, folder, draft_id, raw_path, raw_dur_us, w, h, total_us, now_us):
    now_s = now_us // 1_000_000
    return {
        "cloud_draft_cover": False, "cloud_draft_sync": False,
        "cloud_package_completed_time": "", "draft_cloud_capcut_purchase_info": "",
        "draft_cloud_last_action_download": False, "draft_cloud_package_type": "",
        "draft_cloud_purchase_info": "", "draft_cloud_template_id": "",
        "draft_cloud_tutorial_info": "", "draft_cloud_videocut_purchase_info": "",
        "draft_cover": "draft_cover.jpg", "draft_deeplink_url": "",
        "draft_enterprise_info": {"draft_enterprise_extra": "",
                                  "draft_enterprise_id": "",
                                  "draft_enterprise_name": "",
                                  "enterprise_material": []},
        "draft_fold_path": str(folder), "draft_id": draft_id,
        "draft_is_ae_produce": False, "draft_is_ai_packaging_used": False,
        "draft_is_ai_shorts": False, "draft_is_ai_translate": False,
        "draft_is_article_video_draft": False, "draft_is_cloud_temp_draft": False,
        "draft_is_from_deeplink": "false", "draft_is_invisible": False,
        "draft_is_pippit_draft": False, "draft_is_web_article_video": False,
        "draft_materials": [
            {"type": 0, "value": [{
                "ai_group_type": "", "create_time": now_s, "duration": raw_dur_us,
                "enter_from": 0, "extra_info": Path(raw_path).name,
                "file_Path": str(raw_path), "height": h,
                "id": str(uuid.uuid4()), "import_time": now_s,
                "import_time_ms": now_us, "item_source": 1, "md5": "",
                "metetype": "video",
                "roughcut_time_range": {"duration": raw_dur_us, "start": 0},
                "sub_time_range": {"duration": -1, "start": -1},
                "type": 0, "width": w}]},
            {"type": 1, "value": []}, {"type": 2, "value": []},
            {"type": 3, "value": []}, {"type": 6, "value": []},
            {"type": 7, "value": []},
        ],
        "draft_materials_copied_info": [], "draft_name": name,
        "draft_need_rename_folder": False, "draft_new_version": "",
        "draft_removable_storage_device": "", "draft_root_path": str(DRAFT_ROOT),
        "draft_segment_extra_info": [], "draft_timeline_materials_size_": 0,
        "draft_type": "", "draft_web_article_video_enter_from": "",
        "pippit_avatar_url": "", "pippit_extra_info": "", "pippit_id": "",
        "pippit_user_name": "", "tm_draft_cloud_completed": "",
        "tm_draft_cloud_entry_id": -1, "tm_draft_cloud_modified": 0,
        "tm_draft_cloud_parent_entry_id": -1, "tm_draft_cloud_space_id": -1,
        "tm_draft_cloud_user_id": -1, "tm_draft_create": now_us,
        "tm_draft_modified": now_us, "tm_draft_removed": 0,
        "tm_duration": total_us,
    }


def registry_entry(name, folder, draft_id, total_us, now_us):
    return {
        "cloud_draft_cover": False, "cloud_draft_sync": False,
        "draft_cloud_last_action_download": False, "draft_cloud_purchase_info": "",
        "draft_cloud_template_id": "", "draft_cloud_tutorial_info": "",
        "draft_cloud_videocut_purchase_info": "",
        "draft_cover": str(folder / "draft_cover.jpg"),
        "draft_fold_path": str(folder), "draft_id": draft_id,
        "draft_is_ai_shorts": False, "draft_is_cloud_temp_draft": False,
        "draft_is_invisible": False, "draft_is_pippit_draft": False,
        "draft_is_web_article_video": False,
        "draft_json_file": str(folder / "draft_info.json"), "draft_name": name,
        "draft_new_version": "", "draft_root_path": str(DRAFT_ROOT),
        "draft_timeline_materials_size": 0, "draft_type": "",
        "draft_web_article_video_enter_from": "", "pippit_avatar_url": "",
        "pippit_extra_info": "", "pippit_id": "", "pippit_user_name": "",
        "streaming_edit_draft_ready": True, "tm_draft_cloud_completed": "",
        "tm_draft_cloud_entry_id": -1, "tm_draft_cloud_modified": 0,
        "tm_draft_cloud_parent_entry_id": -1, "tm_draft_cloud_space_id": -1,
        "tm_draft_cloud_user_id": -1, "tm_draft_create": now_us,
        "tm_draft_modified": now_us, "tm_draft_removed": 0,
        "tm_duration": total_us,
    }


def cmd_replay(job, draft_name=None):
    repo = Path(__file__).resolve().parent.parent
    jobdir = repo / "projects" / job
    cuts_path = jobdir / "transcript" / "cuts.json"
    if not cuts_path.exists():
        sys.exit(f"no EDL at {cuts_path}, run rough-cut (RENDER=0) first")
    cuts = json.loads(cuts_path.read_text())
    raw = jobdir / "raw" / cuts["segments"][0]["clip"]
    if not raw.exists():
        sys.exit(f"raw footage missing: {raw}")
    name = draft_name or job
    folder = DRAFT_ROOT / name
    if folder.exists():
        sys.exit(f"draft folder already exists: {folder} (pick another --name)")

    was_running = app_pid() is not None
    if was_running:
        print("quitting CapCut (registry is rewritten on quit)...")
        quit_app()

    folder.mkdir(parents=True)
    (folder / "Resources").mkdir()
    # CapCut is sandboxed with only assets.movies.read-write: it cannot read repo
    # paths it was never handed, so the raw must live under ~/Movies. Hardlink
    # (same APFS volume, zero bytes) into the draft folder; copy if that fails.
    local_raw = folder / "Resources" / raw.name
    try:
        local_raw.hardlink_to(raw)
    except OSError:
        import shutil
        shutil.copy2(raw, local_raw)

    draft, raw_dur_us, w, h = build_draft(name, local_raw, cuts, 1920, 1080)
    now_us = time.time_ns() // 1000
    draft_id = uid()
    (folder / "draft_info.json").write_text(
        json.dumps(draft, ensure_ascii=False, separators=(",", ":")))
    (folder / "draft_meta_info.json").write_text(json.dumps(
        meta_entry(name, folder, draft_id, local_raw, raw_dur_us, w, h,
                   draft["duration"], now_us),
        ensure_ascii=False, separators=(",", ":")))

    subprocess.run(  # home-screen tile cover, first kept frame; cosmetic only
        ["ffmpeg", "-v", "error", "-ss", str(cuts["segments"][0]["start"]),
         "-i", str(raw), "-frames:v", "1", "-vf", "scale=480:-2",
         str(folder / "draft_cover.jpg")], check=False)

    root_path = DRAFT_ROOT / "root_meta_info.json"
    root = json.loads(root_path.read_text())
    root["all_draft_store"].insert(
        0, registry_entry(name, folder, draft_id, draft["duration"], now_us))
    root["draft_ids"] = root.get("draft_ids", 0) + 1
    root_path.write_text(json.dumps(root, ensure_ascii=False, separators=(",", ":")))

    n = len(cuts["segments"])
    print(f"draft '{name}': {n} segments, {draft['duration'] / 1e6:.2f}s, {folder}")
    if was_running:
        launch_app()
        print("CapCut relaunched")


TEMPLATES = Path(__file__).resolve().parent / "capcut-templates"


def edit_draft(name, mutate, reopen=True):
    """Additive file-lane edit of an EXISTING draft. Loads the draft as it stands
    (so hand edits made in the app, saved on quit, are preserved and built upon), applies
    mutate(draft, folder), then wipes the Timelines/ native cache — CapCut only
    imports draft_info.json when that cache is absent, otherwise it silently
    keeps its own state and the edit vanishes."""
    folder = DRAFT_ROOT / name
    if not folder.exists():
        sys.exit(f"no such draft: {name}")
    was_running = app_pid() is not None
    quit_app()

    d = json.loads((folder / "draft_info.json").read_text())
    mutate(d, folder)
    # CapCut's own save normalizes text segments to source_timerange: null. That
    # is fine while IT owns the timeline state, but we wipe Timelines/ below to
    # force a re-import, and a null survives into the reimported draft. Give
    # every segment a real source range so the importer never sees one.
    for t in d["tracks"]:
        for s in t["segments"]:
            if s.get("source_timerange") is None:
                s["source_timerange"] = {"start": 0,
                                         "duration": s["target_timerange"]["duration"]}
    d["duration"] = max((s["target_timerange"]["start"] + s["target_timerange"]["duration"]
                         for t in d["tracks"] for s in t["segments"]), default=0)
    (folder / "draft_info.json").write_text(
        json.dumps(d, ensure_ascii=False, separators=(",", ":")))
    (folder / "draft_info.json.bak").unlink(missing_ok=True)
    shutil.rmtree(folder / "Timelines", ignore_errors=True)

    if was_running:
        launch_app()
        if reopen:
            time.sleep(8)
            cmd_open(name)
    return d


def local_media(folder, src):
    """CapCut's sandbox only grants ~/Movies, so media must live under the draft."""
    src = Path(src).expanduser().resolve()
    if not src.exists():
        sys.exit(f"media not found: {src}")
    dst = folder / "Resources" / src.name
    if not dst.exists():
        dst.parent.mkdir(exist_ok=True)
        try:
            dst.hardlink_to(src)
        except OSError:
            shutil.copy2(src, dst)
    return dst


def overlay_track(d, level):
    """Video overlay tracks are flag=2; level maps to render_index (1 = first
    layer above the main track)."""
    existing = [t for t in d["tracks"] if t["type"] == "video" and t["flag"] == 2]
    if len(existing) >= level:
        return existing[level - 1]
    t = {"id": uid(), "type": "video", "segments": [], "flag": 2,
         "attribute": 0, "name": "", "is_default_name": True}
    d["tracks"].append(t)
    return t


def add_overlay(name, media, at, level=1, duration=None, force=False,
                src=0.0, render_index=None, mute=False):
    """Place an alpha graphic (ProRes 4444 .mov) over the cut — the step-3
    graphics move, validated 2026-07-27.

    `src` takes a source in-point, so a slice of the raw can be re-laid above the
    main track (the cutout copy in the text-behind-subject build). `render_index`
    overrides the layer-derived z-order: CapCut reserves a high band for text
    (14000+), so a video layer only composites ABOVE text with an explicit index
    past it. `mute` is mandatory on a duplicate of footage already on the main
    track — an overlay segment plays its own audio and would double the voice."""
    def mutate(d, folder):
        if not force and already_at(d, round(at * 1e6), media_name=Path(media).name):
            sys.exit(f"{Path(media).name} is already at {at}s — pass --force to add a second copy")
        path = local_media(folder, media)
        w, h, dur_us, has_audio = probe(path)
        mid = uid()
        d["materials"]["videos"].append(
            video_material(mid, path, w, h, dur_us, has_audio))
        use = round(duration * 1e6) if duration else dur_us
        seg = segment(mid, round(src * 1e6), use, round(at * 1e6),
                      segment_extras(d["materials"]))
        ri = level if render_index is None else render_index
        seg["render_index"] = ri
        seg["track_render_index"] = level
        if mute:
            seg["volume"] = 0.0
            seg["last_nonzero_volume"] = 0.0
        overlay_track(d, level)["segments"].append(seg)
    edit_draft(name, mutate)
    print(f"overlay: {Path(media).name} at {at:.2f}s on layer {level}"
          f"{'' if render_index is None else f' (render_index {render_index})'}")


def add_text(name, text, at, duration=3.0, level=1, force=False,
             render_index=None):
    """Text graphic built from a template lifted from a real CapCut draft — the
    `content` field is JSON-in-JSON carrying the style run, so the styled range
    must be re-pointed at the new string or CapCut renders it unstyled."""
    tm_path, seg_path = TEMPLATES / "text-material.json", TEMPLATES / "text-segment.json"
    anim_path = TEMPLATES / "text-ref-material_animations.json"
    if not tm_path.exists():
        sys.exit(f"missing text template: {tm_path}")

    def mutate(d, folder):
        if not force and already_at(d, round(at * 1e6), text=text):
            sys.exit(f"{text!r} is already at {at}s — pass --force to add a second copy")
        tm = json.loads(tm_path.read_text())
        content = json.loads(tm["content"])
        content["text"] = text
        for style in content.get("styles", []):
            style["range"] = [0, len(text)]
        tm["id"] = uid()
        tm["content"] = json.dumps(content, ensure_ascii=False)
        d["materials"]["texts"].append(tm)

        anim = json.loads(anim_path.read_text())
        anim["id"] = uid()
        d["materials"]["material_animations"].append(anim)

        seg = json.loads(seg_path.read_text())
        seg["id"] = uid()
        seg["material_id"] = tm["id"]
        seg["extra_material_refs"] = [anim["id"]]
        seg["source_timerange"] = {"start": 0, "duration": round(duration * 1e6)}
        seg["target_timerange"] = {"start": round(at * 1e6),
                                   "duration": round(duration * 1e6)}
        if render_index is not None:
            seg["render_index"] = render_index
        track = next((t for t in d["tracks"] if t["type"] == "text"), None)
        if track is None:
            track = {"id": uid(), "type": "text", "segments": [], "flag": 1,
                     "attribute": 0, "name": "", "is_default_name": True}
            d["tracks"].append(track)
        track["segments"].append(seg)
    edit_draft(name, mutate)
    print(f"text: {text!r} at {at:.2f}s for {duration:.2f}s")


def remove_segment(name, track_role, index):
    """Drop a segment and any material it alone referenced. The file-lane
    counterpart to the live `delete` — use this for precise/batch removal."""
    flag = {"main": 0, "text": 1, "overlay": 2}[track_role]

    def mutate(d, folder):
        # Index runs across every track of this role: CapCut splits overlapping
        # segments onto separate tracks, so "text[1]" is not always track 1.
        flat = [(t, i) for t in d["tracks"] if t["flag"] == flag
                for i in range(len(t["segments"]))]
        if index >= len(flat):
            sys.exit(f"only {len(flat)} {track_role} segment(s), no index {index}")
        track, local = flat[index]
        seg = track["segments"].pop(local)
        still_used = {s["material_id"] for t in d["tracks"] for s in t["segments"]}
        for cat in ("videos", "texts"):
            d["materials"][cat] = [m for m in d["materials"][cat]
                                   if m["id"] in still_used]
        if not track["segments"] and flag != 0:
            d["tracks"].remove(track)
    edit_draft(name, mutate)
    print(f"removed {track_role}[{index}]")


def already_at(d, start_us, text=None, media_name=None):
    """Guard against double-applying: an interrupted file-lane run may have
    already written its edit before the app relaunch was cut short."""
    for t in d["tracks"]:
        for s in t["segments"]:
            if s["target_timerange"]["start"] != start_us:
                continue
            if text is not None:
                m = next((m for m in d["materials"]["texts"]
                          if m["id"] == s["material_id"]), None)
                if m and json.loads(m["content"]).get("text") == text:
                    return True
            if media_name is not None:
                m = next((m for m in d["materials"]["videos"]
                          if m["id"] == s["material_id"]), None)
                if m and m["material_name"] == media_name:
                    return True
    return False


def set_transform(name, track_role, index, **vals):
    """Exact scale/position/rotation/opacity on one segment. This is FILE-lane on
    purpose: the inspector's QML text fields take focus but ignore synthesized
    keystrokes (measured 2026-07-27), so writing the numbers into the draft is
    both the precise path and the only reliable one. Position is normalized to
    the canvas (x=0.5 shifts a full half-frame right), scale is a multiplier."""
    flag = {"main": 0, "text": 1, "overlay": 2}[track_role]

    def mutate(d, folder):
        # Flattened across every track of this role, matching `remove`/`keyframe`
        # — CapCut splits overlapping segments onto separate tracks, so a single
        # track's local index is not the addressing anything else uses.
        flat = [(t, i) for t in d["tracks"] if t["flag"] == flag
                for i in range(len(t["segments"]))]
        if index >= len(flat):
            sys.exit(f"only {len(flat)} {track_role} segment(s), no index {index}")
        track, local = flat[index]
        segs, index_ = track["segments"], local
        clip = segs[index_]["clip"]
        if vals.get("scale") is not None:
            clip["scale"] = {"x": vals["scale"], "y": vals["scale"]}
            segs[index_]["uniform_scale"] = {"on": True, "value": vals["scale"]}
        if vals.get("x") is not None:
            clip["transform"]["x"] = vals["x"]
        if vals.get("y") is not None:
            clip["transform"]["y"] = vals["y"]
        if vals.get("rotation") is not None:
            clip["rotation"] = vals["rotation"]
        if vals.get("opacity") is not None:
            clip["alpha"] = vals["opacity"]
    edit_draft(name, mutate)
    applied = {k: v for k, v in vals.items() if v is not None}
    print(f"{track_role}[{index}] transform: {applied}")


KF_TYPES = {"scale": ("KFTypeScaleX", "KFTypeScaleY"), "x": ("KFTypePositionX",),
            "y": ("KFTypePositionY",), "rotation": ("KFTypeRotation",),
            "opacity": ("KFTypeAlpha",), "volume": ("KFTypeVolume",)}


def set_keyframe(name, track_role, index, at, curve="Line", **vals):
    """Animate a segment property — motion keyframes, the CapCut counterpart to
    Premiere's Effect Controls. `common_keyframes` holds one entry per property,
    each with a `keyframe_list` of points.

    `time_offset` is microseconds on the SOURCE media timeline, NOT from the
    segment start — so a point is `source_timerange.start + (t - target start)`.
    That distinction is the whole ballgame and it is easy to get wrong: a first
    probe on a clip whose source in-point was 0 made the two domains look
    identical. On a real cut (source in-point 22.133s) segment-relative offsets
    land outside the segment's source window, and CapCut silently collapses the
    whole animation to one static value instead of erroring. `--at` here is
    timeline seconds; the conversion happens below.

    File-lane on purpose, same reason as `transform`: the inspector's spin boxes
    ignore synthesized keystrokes, so exact values only go in through the JSON."""
    flag = {"main": 0, "text": 1, "overlay": 2}[track_role]
    vals = {k: v for k, v in vals.items() if v is not None}
    if not vals:
        sys.exit("nothing to animate — pass --scale/--x/--y/--rotate/--opacity")

    def mutate(d, folder):
        flat = [(t, i) for t in d["tracks"] if t["flag"] == flag
                for i in range(len(t["segments"]))]
        if index >= len(flat):
            sys.exit(f"only {len(flat)} {track_role} segment(s), no index {index}")
        track, local = flat[index]
        seg = track["segments"][local]
        start, dur = (seg["target_timerange"][k] for k in ("start", "duration"))
        rel = round(at * 1e6) - start
        if not 0 <= rel <= dur:
            sys.exit(f"{at}s is outside the segment "
                     f"({start / 1e6:.2f}s–{(start + dur) / 1e6:.2f}s)")
        src = (seg.get("source_timerange") or {}).get("start", 0)
        offset = src + rel
        kfs = seg.setdefault("common_keyframes", [])
        for prop, value in vals.items():
            for ptype in KF_TYPES[prop]:
                entry = next((e for e in kfs if e["property_type"] == ptype), None)
                if entry is None:
                    entry = {"id": uid(), "material_id": "", "property_type": ptype,
                             "keyframe_list": []}
                    kfs.append(entry)
                point = {"id": uid(), "curveType": curve, "time_offset": offset,
                         "left_control": {"x": 0.0, "y": 0.0},
                         "right_control": {"x": 0.0, "y": 0.0},
                         "values": [float(value)], "string_value": "", "graphID": ""}
                entry["keyframe_list"] = sorted(
                    [p for p in entry["keyframe_list"] if p["time_offset"] != offset]
                    + [point], key=lambda p: p["time_offset"])
    edit_draft(name, mutate)
    print(f"keyframe {track_role}[{index}] @ {at:.2f}s: {vals}")


def clear_keyframes(name, track_role, index):
    flag = {"main": 0, "text": 1, "overlay": 2}[track_role]

    def mutate(d, folder):
        flat = [(t, i) for t in d["tracks"] if t["flag"] == flag
                for i in range(len(t["segments"]))]
        flat[index][0]["segments"][flat[index][1]]["common_keyframes"] = []
    edit_draft(name, mutate)
    print(f"cleared keyframes on {track_role}[{index}]")


def cmd_graphics(name, job):
    """Batch-place every graphic a job's plan calls for, in ONE cache cycle."""
    repo = Path(__file__).resolve().parent.parent
    plan_path = repo / "projects" / job / "graphics-plan.json"
    if not plan_path.exists():
        sys.exit(f"no graphics plan at {plan_path}")
    plan = json.loads(plan_path.read_text())
    beats = plan.get("graphics") or plan.get("beats") or []
    assets = repo / "projects" / job / "assets"
    queued = []
    for b in beats:
        mov = b.get("file") or (f"{b['id']}.mov" if b.get("id") else None)
        at = b.get("start") or b.get("t") or b.get("time")
        if not mov or at is None:
            continue
        p = assets / mov
        if p.exists():
            queued.append((p, float(at)))
        else:
            print(f"  skip {mov} (not rendered yet)")
    if not queued:
        sys.exit("nothing in the plan is renderable yet")

    def mutate(d, folder):
        for p, at in queued:
            path = local_media(folder, p)
            w, h, dur_us, has_audio = probe(path)
            mid = uid()
            d["materials"]["videos"].append(
                video_material(mid, path, w, h, dur_us, has_audio))
            seg = segment(mid, 0, dur_us, round(at * 1e6), segment_extras(d["materials"]))
            seg["render_index"] = seg["track_render_index"] = 1
            overlay_track(d, 1)["segments"].append(seg)
    edit_draft(name, mutate)
    print(f"placed {len(queued)} graphic(s) from {job}'s plan")


def cmd_open(name):
    if app_pid() is None:
        launch_app()
    subprocess.run(["osascript", "-e", f'tell application "{APP}" to activate'], check=False)
    hit = find_one(f"HomePageDraftTitle:{name}", timeout=15)
    if hit is None:
        send_key("escape")  # a promo modal can cover the home screen on launch
        hit = find_one(f"HomePageDraftTitle:{name}", timeout=15)
    if hit is None:
        sys.exit(f"draft tile not found on home screen: {name}")
    time.sleep(1.5)  # the window repositions after launch, re-find before clicking
    hit = find_one(f"HomePageDraftTitle:{name}", timeout=10) or hit
    _, _, (tx, ty, tw, th) = hit
    # The clickable tile is the HomePageDraft element whose x-span contains the title.
    tile = next((g for _, n, g in elements("HomePageDraft")
                 if n == "HomePageDraft" and g and g[0] <= tx <= g[0] + g[2]
                 and g[1] <= ty), None)
    x, y = (tile[0] + tile[2] / 2, tile[1] + tile[3] / 2) if tile else (tx, ty - 60)
    click_at(x, y, clicks=2)
    if find_one("MainTimeLineRoot", timeout=25) is None:
        sys.exit("editor did not open")
    cur, tot = read_playhead()
    print(f"opened '{name}', duration {tot}")


# Export dialog controls, as offsets from the CapCut window's CENTER. The dialog
# is a fixed-size centered QML overlay that exposes NO AX internals (same as Link
# media), so positional clicking is the only way in. Measured on 8.9.0.
EXPORT_BTN_OFFSET = (308, 302)
SYNC_CHECKBOX_OFFSET = (6, 239)


def cmd_export(out_dir=None, timeout=900, toggle_sync=False):
    """Drive CapCut's export end to end and verify the file that lands.

    Defaults in the dialog are the draft name into ~/Downloads, which already
    matches this repo's export convention. "Sync exported videos to space"
    uploads the render to CapCut's cloud — it was switched OFF on 2026-07-27 and
    the setting is sticky, so this does NOT touch it by default (a blind click
    would switch it back ON). Pass --toggle-sync only to flip it deliberately."""
    downloads = Path(out_dir).expanduser() if out_dir else Path.home() / "Downloads"
    before = {p: p.stat().st_mtime for p in downloads.glob("*.mp4")}

    # CapCut leaves a modal "share to TikTok/YouTube" screen up after a finished
    # export, and it BLOCKS the next one — the title-bar click is swallowed and
    # the run polls forever for a file that is never written (hit 2026-07-27).
    # Escape clears any lingering modal before starting.
    send_key("escape")
    time.sleep(1.0)

    win = find_one("MainWindowTitleBarExportBtn")
    if win is None:
        sys.exit("export button not found (is a draft open?)")
    w = next((g for _, n, g in elements("CapCut") if g and g[2] > 1000), None)
    if w is None:
        sys.exit("could not locate the CapCut window rect")
    cx, cy = w[0] + w[2] / 2, w[1] + w[3] / 2

    click_element("MainWindowTitleBarExportBtn")
    time.sleep(6)  # the dialog paints well after the click returns
    if toggle_sync:
        click_at(cx + SYNC_CHECKBOX_OFFSET[0], cy + SYNC_CHECKBOX_OFFSET[1])
        time.sleep(0.6)
    click_at(cx + EXPORT_BTN_OFFSET[0], cy + EXPORT_BTN_OFFSET[1])

    t0, last, stable, target = time.time(), -1, 0, None
    while time.time() - t0 < timeout:
        time.sleep(4)
        fresh = [p for p in downloads.glob("*.mp4")
                 if p not in before or p.stat().st_mtime > before[p]]
        if not fresh:
            continue
        target = max(fresh, key=lambda p: p.stat().st_mtime)
        size = target.stat().st_size
        stable = stable + 1 if size == last and size > 0 else 0
        last = size
        if stable >= 2:
            break
    # Clear the post-export share screen so the app is left ready for the next
    # run. Never click through it: it offers TikTok/YouTube publishing.
    send_key("escape")
    time.sleep(0.8)

    if target is None:
        sys.exit("export produced no file — the dialog may still be open")

    probe_out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(target)], capture_output=True, text=True)
    dur = probe_out.stdout.strip() or "?"
    print(f"exported: {target}  {last / 1048576:.1f} MB  {dur}s")
    return target


def cmd_state(name=None):
    """Timeline snapshot. Exact segment times come from draft_info.json (the
    saved truth); the live AX clip count is compared against it, because live
    edits only reach disk on save/quit — a mismatch means unsaved changes."""
    cur, tot = read_playhead()
    live = timeline_clips()
    out = {"playhead": cur, "duration": tot, "live_clip_count": len(live)}

    path = DRAFT_ROOT / name / "draft_info.json" if name else None
    if path and path.exists():
        d = json.loads(path.read_text())
        tracks = []
        for t in d["tracks"]:
            segs = []
            for i, s in enumerate(t["segments"]):
                mat = next((m["material_name"] for m in d["materials"]["videos"]
                            if m["id"] == s["material_id"]), None)
                if mat is None:
                    mat = next((json.loads(m["content"]).get("text", "")[:40]
                                for m in d["materials"]["texts"]
                                if m["id"] == s["material_id"]), "?")
                src = s.get("source_timerange")  # null on text segs after app save
                segs.append({"i": i, "media": mat,
                             "start": round(s["target_timerange"]["start"] / 1e6, 3),
                             "dur": round(s["target_timerange"]["duration"] / 1e6, 3),
                             "src": round(src["start"] / 1e6, 3) if src else None})
            tracks.append({"type": t["type"],
                           "role": {0: "main", 1: "text", 2: "overlay"}.get(t["flag"], t["flag"]),
                           "segments": segs})
        out["saved"] = {"duration": round(d["duration"] / 1e6, 3), "tracks": tracks}
        saved_video = sum(len(t["segments"]) for t in d["tracks"] if t["type"] == "video")
        if saved_video != len(live):
            out["unsaved_edits"] = f"{len(live)} clips live vs {saved_video} saved"
    print(json.dumps(out, indent=1))


def cmd_clips():
    hits = [(n, g) for _, n, g in elements("MTLSVideoP") if g]
    for n, (x, y, w, h) in sorted(hits, key=lambda t: t[1][0]):
        print(f"{n}  x={x:.0f} y={y:.0f} w={w:.0f} h={h:.0f}")
    print(f"{len(hits)} clip(s) on timeline")


def opt(rest, flag, cast=str, default=None):
    return cast(rest[rest.index(flag) + 1]) if flag in rest else default


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(USAGE)
    cmd, rest = args[0], args[1:]
    if cmd == "replay":
        cmd_replay(rest[0], opt(rest, "--name"))
    elif cmd == "add-overlay":
        add_overlay(rest[0], rest[1], opt(rest, "--at", float, 0.0),
                    opt(rest, "--layer", int, 1), opt(rest, "--dur", float),
                    force="--force" in rest, src=opt(rest, "--src", float, 0.0),
                    render_index=opt(rest, "--ri", int), mute="--mute" in rest)
    elif cmd == "add-text":
        add_text(rest[0], rest[1], opt(rest, "--at", float, 0.0),
                 opt(rest, "--dur", float, 3.0), force="--force" in rest,
                 render_index=opt(rest, "--ri", int))
    elif cmd == "remove":
        remove_segment(rest[0], opt(rest, "--track", str, "main"),
                       opt(rest, "--index", int, 0))
    elif cmd == "graphics":
        cmd_graphics(rest[0], rest[1])
    elif cmd == "seek":
        fps = draft_fps(opt(rest, "--draft"))
        landed = seek(float(rest[0]), fps)
        print(f"playhead: {landed:.3f}s (target {float(rest[0]):.3f}s)")
    elif cmd == "select":
        print("selected:", select_clip(int(rest[0])))
    elif cmd == "split":
        if rest and not rest[0].startswith("--"):
            seek(float(rest[0]), draft_fps(opt(rest, "--draft")))
        click_element("cutoff")
        print("split at playhead")
    elif cmd in ("delete", "del"):
        select_clip(int(rest[0]))
        click_element("del")
        print(f"deleted clip {rest[0]}")
    elif cmd in ("trim-left", "trim-right"):
        click_element("cutLeft" if cmd == "trim-left" else "cutRight")
        print(f"{cmd} at playhead")
    elif cmd in ("undo", "redo"):
        click_element(cmd)
        print(cmd)
    elif cmd == "transform":
        set_transform(rest[0], opt(rest, "--track", str, "main"),
                      opt(rest, "--index", int, 0),
                      scale=opt(rest, "--scale", float),
                      x=opt(rest, "--x", float), y=opt(rest, "--y", float),
                      rotation=opt(rest, "--rotate", float),
                      opacity=opt(rest, "--opacity", float))
    elif cmd == "keyframe":
        set_keyframe(rest[0], opt(rest, "--track", str, "main"),
                     opt(rest, "--index", int, 0), opt(rest, "--at", float, 0.0),
                     curve=opt(rest, "--curve", str, "Line"),
                     scale=opt(rest, "--scale", float),
                     x=opt(rest, "--x", float), y=opt(rest, "--y", float),
                     rotation=opt(rest, "--rotate", float),
                     opacity=opt(rest, "--opacity", float))
    elif cmd == "clear-keyframes":
        clear_keyframes(rest[0], opt(rest, "--track", str, "main"),
                        opt(rest, "--index", int, 0))
    elif cmd == "marker":
        click_element("mark")
        print("marker added")
    elif cmd == "zoomfit":
        click_element("quicklyAdjustZoomFit")
        print("zoom fit")
    elif cmd == "save":
        send_key("cmd+s")
        time.sleep(1.5)
        print("saved")
    elif cmd == "export":
        cmd_export(opt(rest, "--to"), opt(rest, "--timeout", int, 900),
                   toggle_sync="--toggle-sync" in rest)
    elif cmd == "shot":
        print("wrote", grab(rest[0] if rest else "capcut.png"))
    elif cmd == "state":
        cmd_state(opt(rest, "--draft"))
    elif cmd == "ls":
        root = json.loads((DRAFT_ROOT / "root_meta_info.json").read_text())
        for d in root["all_draft_store"]:
            print(f"{d['draft_name']}: {d['tm_duration'] / 1e6:.2f}s  {d['draft_fold_path']}")
    elif cmd == "launch":
        launch_app()
    elif cmd == "quit":
        quit_app()
    elif cmd == "open":
        cmd_open(rest[0])
    elif cmd == "dump":
        needle = rest[0] if rest else None
        for _, name, g in elements(needle):
            if name:
                geo = f"  [{g[0]:.0f},{g[1]:.0f} {g[2]:.0f}x{g[3]:.0f}]" if g else ""
                print(f"{name}{geo}")
    elif cmd == "click":
        print("clicked:", click_element(rest[0]))
    elif cmd == "clickxy":
        # Modal dialogs (export, Link media) are QML overlays with no AX
        # internals, so their controls can only be reached positionally.
        click_at(float(rest[0]), float(rest[1]), clicks=opt(rest, "--clicks", int, 1))
        print(f"clicked ({rest[0]}, {rest[1]})")
    elif cmd == "clips":
        cmd_clips()
    elif cmd == "playhead":
        cur, tot = read_playhead()
        print(f"{cur} / {tot}")
    elif cmd == "play":
        # The transport button's AX id reflects state: PlayerPlayBtn when paused,
        # PlayerPauseBtn while playing. Match either so the toggle always lands.
        click_element("PlayerPlayBtn" if find_one("PlayerPlayBtn") else "PlayerPauseBtn")
    elif cmd == "key":
        # --times keeps a long run of frame steps inside ONE process: the ruler
        # can have dead regions (see `nudge`), so arrow stepping is the fallback
        # way to park the playhead and 60 separate invocations would crawl.
        times = opt(rest, "--times", int, 1)
        for i in range(times):
            send_key(rest[0])
            if i + 1 < times:
                time.sleep(0.03)   # CapCut drops frame steps sent back-to-back
    else:
        sys.exit(f"unknown command: {cmd}")


if __name__ == "__main__":
    main()
