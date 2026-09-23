#!/usr/bin/env python3
# CapCut bridge (WINDOWS PORT — file lane only). Adapted from the original
# macOS kit's capcut-bridge-macos.py. Ported 2026-09-23, validated against a
# real local install (CapCut Windows, drafts under
# %LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft\).
#
# Only pure-Python stdlib is used (json, subprocess, pathlib, uuid) — no uv,
# no pyobjc. ffmpeg/ffprobe must be on PATH (installed separately).
#
# WHAT DID AND DIDN'T PORT:
#
#   FILE lane (this script) — CapCut has no API; drafts are plain JSON. On
#   Windows the per-draft file is named `draft_content.json` (the macOS kit
#   calls it `draft_info.json` — same schema, different filename). `replay`
#   writes a brand-new draft from a job's cuts.json, one timeline segment per
#   EDL cut. The app must be CLOSED while writing (it rewrites its own
#   registry, root_meta_info.json, on quit) — this script quits/relaunches
#   CapCut.exe around every write, same as the macOS version.
#
#   LIVE lane (NOT ported) — the macOS kit drives the open editor by walking
#   the accessibility (AX) tree and synthesizing CGEvents/keystrokes. That is
#   a macOS-only API with no equivalent here. A real Windows port of that lane
#   would use UI Automation (e.g. the `pywinauto`/`uiautomation` packages)
#   against CapCut's Qt/QML window, which is a from-scratch reverse-engineering
#   job (different automation IDs, different window internals) — out of scope
#   for this pass. Every live-lane command (seek, select, split, delete, trim,
#   undo/redo, marker, zoomfit, save, export, shot, dump, click, key, play,
#   playhead, clips, open, launch's live-check) exits with a clear "not
#   supported on Windows" message instead of silently doing nothing.
#
# UNVERIFIED, TEST BEFORE TRUSTING WITH REAL WORK:
#   The macOS kit's hardest-won rule is that CapCut caches a draft's timeline
#   natively once opened (in a `Timelines/` folder) and silently ignores any
#   on-disk edit unless that cache is wiped first. No such folder exists in
#   any draft on this Windows install (checked several real drafts) — Windows
#   CapCut may just re-read draft_content.json every time, or it may cache
#   somewhere else entirely. `edit_draft` below still tries to clear a
#   `Timelines/` folder if present (harmless no-op otherwise), but additive
#   edits to an ALREADY-HAND-EDITED draft (add-overlay/add-text/graphics/
#   transform/keyframe/remove) are UNVERIFIED on Windows — test on a
#   throwaway draft first. `replay` (building a brand-new draft) does not
#   depend on this at all and is the safer, better-tested path.

USAGE = """Usage (Windows, file lane only — quits + relaunches CapCut.exe around each write):
  replay <job> [--name <draft>] [--jobs-dir <dir>]  # EDL -> new draft, one clip per cut
  add-overlay <draft> <mov> --at <s> [--layer N] [--dur <s>] [--src <s>] [--ri N] [--mute] [--force]
  add-text <draft> "<text>" --at <s> [--dur <s>] [--ri N] [--force]
  graphics <draft> <job> [--jobs-dir <dir>]         # place a job's whole graphics plan
  transform <draft> [--track main|text|overlay] [--index N] [--scale S] [--x X] [--y Y] [--rotate R] [--opacity O]
  remove <draft> [--track main|text|overlay] [--index N]
  keyframe <draft> [--track ...] [--index N] --at <s> [--scale S] [--x X] [--y Y] [--rotate R] [--opacity O]
  clear-keyframes <draft> [--track ...] [--index N]
  state [--draft <name>]                            # saved timeline snapshot (JSON)
  ls                                                 # drafts in the registry
  launch · quit                                      # start/stop CapCut.exe

Live-lane commands from the macOS kit (seek, select, split, delete, trim-left/
right, undo, redo, marker, zoomfit, save, export, shot, dump, click, clickxy,
key, play, playhead, clips, open) are NOT ported — see the file header."""

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

DRAFT_ROOT = (Path(os.environ["LOCALAPPDATA"]) / "CapCut" / "User Data"
              / "Projects" / "com.lveditor.draft")
DRAFT_FILE = "draft_content.json"  # macOS kit's draft_info.json, renamed on Windows
APP_EXE_NAME = "CapCut.exe"

# ---------------------------------------------------------------- app lifecycle

_app_exe_cache = None


def find_app_exe():
    global _app_exe_cache
    if _app_exe_cache:
        return _app_exe_cache
    env = os.environ.get("CAPCUT_EXE")
    if env and Path(env).exists():
        _app_exe_cache = Path(env)
        return _app_exe_cache
    default = Path(os.environ.get("LOCALAPPDATA", "")) / "CapCut" / "Apps" / APP_EXE_NAME
    if default.exists():
        _app_exe_cache = default
        return _app_exe_cache
    sys.exit(f"{APP_EXE_NAME} not found at {default} -- set CAPCUT_EXE to its full path")


def app_pid():
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {APP_EXE_NAME}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True)
    line = out.stdout.strip()
    if APP_EXE_NAME not in line:
        return None
    try:
        return int(line.split('","')[1])
    except (IndexError, ValueError):
        return None


def quit_app(timeout=20):
    """Graceful close first (lets CapCut flush root_meta_info.json normally),
    force-kill only if it doesn't respond in time."""
    if app_pid() is None:
        return
    subprocess.run(["taskkill", "/IM", APP_EXE_NAME], capture_output=True)
    t0 = time.time()
    while app_pid() is not None:
        if time.time() - t0 > timeout:
            subprocess.run(["taskkill", "/IM", APP_EXE_NAME, "/F"], capture_output=True)
            time.sleep(1)
            if app_pid() is not None:
                sys.exit(f"CapCut did not quit within {timeout}s, close it and re-run")
            return
        time.sleep(0.5)


def launch_app():
    exe = find_app_exe()
    subprocess.Popen([str(exe)], cwd=str(exe.parent),
                      creationflags=subprocess.DETACHED_PROCESS
                      | subprocess.CREATE_NEW_PROCESS_GROUP)


def live_lane_unsupported(cmd):
    sys.exit(f"'{cmd}' is a live-lane (accessibility-tree) command from the macOS "
              f"kit and has not been ported to Windows — see the header of this "
              f"script. File-lane commands (replay, add-overlay, add-text, "
              f"graphics, transform, remove, keyframe, clear-keyframes, ls, "
              f"state) all work.")


# ---------------------------------------------------------------- draft writing

FFPROBE = shutil.which("ffprobe") or str(
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "ffprobe.exe")


def probe(path):
    out = json.loads(subprocess.check_output(
        [FFPROBE, "-v", "error", "-show_entries",
         "stream=codec_type,width,height,duration", "-of", "json", str(path)]))
    v = next(s for s in out["streams"] if s["codec_type"] == "video")
    has_audio = any(s["codec_type"] == "audio" for s in out["streams"])
    return int(v["width"]), int(v["height"]), int(float(v["duration"]) * 1e6), has_audio


def uid():
    return str(uuid.uuid4()).upper()


def platform_block():
    # Lift device ids from an existing local draft so the new one reads as
    # native — but only one that actually says "windows": a cloud-synced
    # draft last touched by CapCut on iOS/mac carries that platform's block
    # instead, and copying it here would mislabel this Windows-built draft.
    for p in sorted(DRAFT_ROOT.glob(f"*/{DRAFT_FILE}")):
        try:
            plat = json.loads(p.read_text(encoding="utf-8"))["platform"]
        except (KeyError, json.JSONDecodeError, OSError):
            continue
        if plat.get("os") == "windows":
            return plat
    return {"os": "windows", "os_version": "", "app_id": 359289, "app_version": "9.4.0",
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
        "draft_json_file": str(folder / DRAFT_FILE), "draft_name": name,
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


def jobs_dir(explicit=None):
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("CAPCUT_KIT_JOBS_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent / "projects"


def cmd_replay(job, draft_name=None, jobs=None):
    jobdir = jobs_dir(jobs) / job
    cuts_path = jobdir / "transcript" / "cuts.json"
    if not cuts_path.exists():
        sys.exit(f"no EDL at {cuts_path} — see INPUT-CONTRACT.md for the shape "
                 f"of cuts.json, or pass --jobs-dir to point at your pipeline's "
                 f"projects/ folder")
    cuts = json.loads(cuts_path.read_text(encoding="utf-8"))
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
    # CapCut can only read media under paths it was granted — safest to keep
    # everything inside the draft's own Resources/ folder. Hardlink (same
    # NTFS volume, zero bytes) when possible; copy if that fails (e.g. raw
    # footage on a different drive).
    local_raw = folder / "Resources" / raw.name
    try:
        local_raw.hardlink_to(raw)
    except OSError:
        shutil.copy2(raw, local_raw)

    draft, raw_dur_us, w, h = build_draft(name, local_raw, cuts, 1920, 1080)
    now_us = time.time_ns() // 1000
    draft_id = uid()
    (folder / DRAFT_FILE).write_text(
        json.dumps(draft, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (folder / "draft_meta_info.json").write_text(json.dumps(
        meta_entry(name, folder, draft_id, local_raw, raw_dur_us, w, h,
                   draft["duration"], now_us),
        ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    ffmpeg = shutil.which("ffmpeg") or str(Path(FFPROBE).parent / "ffmpeg.exe")
    subprocess.run(  # home-screen tile cover, first kept frame; cosmetic only
        [ffmpeg, "-v", "error", "-ss", str(cuts["segments"][0]["start"]),
         "-i", str(raw), "-frames:v", "1", "-vf", "scale=480:-2",
         str(folder / "draft_cover.jpg")], check=False)

    root_path = DRAFT_ROOT / "root_meta_info.json"
    root = json.loads(root_path.read_text(encoding="utf-8"))
    root["all_draft_store"].insert(
        0, registry_entry(name, folder, draft_id, draft["duration"], now_us))
    root["draft_ids"] = root.get("draft_ids", 0) + 1
    root_path.write_text(json.dumps(root, ensure_ascii=False, separators=(",", ":")),
                          encoding="utf-8")

    n = len(cuts["segments"])
    print(f"draft '{name}': {n} segments, {draft['duration'] / 1e6:.2f}s, {folder}")
    if was_running:
        launch_app()
        print("CapCut relaunched")


def edit_draft(name, mutate, reopen=True):
    """Additive file-lane edit of an EXISTING draft. UNVERIFIED cache behavior
    on Windows — see the file header. Loads the draft as it stands, applies
    mutate(draft, folder), then clears a Timelines/ cache if one exists
    (mirrors the macOS kit; no-op here if the folder isn't present)."""
    folder = DRAFT_ROOT / name
    if not folder.exists():
        sys.exit(f"no such draft: {name}")
    was_running = app_pid() is not None
    quit_app()

    d = json.loads((folder / DRAFT_FILE).read_text(encoding="utf-8"))
    mutate(d, folder)
    # CapCut's own save can normalize text segments to source_timerange: null.
    # Give every segment a real source range so a later re-import never sees one.
    for t in d["tracks"]:
        for s in t["segments"]:
            if s.get("source_timerange") is None:
                s["source_timerange"] = {"start": 0,
                                         "duration": s["target_timerange"]["duration"]}
    d["duration"] = max((s["target_timerange"]["start"] + s["target_timerange"]["duration"]
                         for t in d["tracks"] for s in t["segments"]), default=0)
    (folder / DRAFT_FILE).write_text(
        json.dumps(d, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (folder / f"{DRAFT_FILE}.bak").unlink(missing_ok=True)
    shutil.rmtree(folder / "Timelines", ignore_errors=True)

    if was_running:
        launch_app()
        if reopen:
            print("NOTE: CapCut relaunched, but opening a specific draft "
                  "automatically is a live-lane feature not ported to "
                  "Windows — open it by hand from the home screen.")
    return d


def local_media(folder, src):
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
    existing = [t for t in d["tracks"] if t["type"] == "video" and t["flag"] == 2]
    if len(existing) >= level:
        return existing[level - 1]
    t = {"id": uid(), "type": "video", "segments": [], "flag": 2,
         "attribute": 0, "name": "", "is_default_name": True}
    d["tracks"].append(t)
    return t


def add_overlay(name, media, at, level=1, duration=None, force=False,
                src=0.0, render_index=None, mute=False):
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
    TEMPLATES = Path(__file__).resolve().parent / "capcut-templates"
    tm_path, seg_path = TEMPLATES / "text-material.json", TEMPLATES / "text-segment.json"
    anim_path = TEMPLATES / "text-ref-material_animations.json"
    if not tm_path.exists():
        sys.exit(f"missing text template: {tm_path} — see README.md, "
                 f"'capcut-templates/ is not included in this repo'")

    def mutate(d, folder):
        if not force and already_at(d, round(at * 1e6), text=text):
            sys.exit(f"{text!r} is already at {at}s — pass --force to add a second copy")
        tm = json.loads(tm_path.read_text(encoding="utf-8"))
        content = json.loads(tm["content"])
        content["text"] = text
        for style in content.get("styles", []):
            style["range"] = [0, len(text)]
        tm["id"] = uid()
        tm["content"] = json.dumps(content, ensure_ascii=False)
        d["materials"]["texts"].append(tm)

        anim = json.loads(anim_path.read_text(encoding="utf-8"))
        anim["id"] = uid()
        d["materials"]["material_animations"].append(anim)

        seg = json.loads(seg_path.read_text(encoding="utf-8"))
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
    flag = {"main": 0, "text": 1, "overlay": 2}[track_role]

    def mutate(d, folder):
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
    flag = {"main": 0, "text": 1, "overlay": 2}[track_role]

    def mutate(d, folder):
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


def cmd_graphics(name, job, jobs=None):
    root = jobs_dir(jobs)
    plan_path = root / job / "graphics-plan.json"
    if not plan_path.exists():
        sys.exit(f"no graphics plan at {plan_path}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    beats = plan.get("graphics") or plan.get("beats") or []
    assets = root / job / "assets"
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


def cmd_state(name=None):
    """Saved timeline snapshot from draft_content.json. The macOS kit's `state`
    also compares against the LIVE editor's clip count — that requires the
    (unported) AX tree, so this is disk-only."""
    path = DRAFT_ROOT / name / DRAFT_FILE if name else None
    if not path or not path.exists():
        sys.exit(f"draft not found: {name}" if name else "pass --draft <name>")
    d = json.loads(path.read_text(encoding="utf-8"))
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
            src = s.get("source_timerange")
            segs.append({"i": i, "media": mat,
                         "start": round(s["target_timerange"]["start"] / 1e6, 3),
                         "dur": round(s["target_timerange"]["duration"] / 1e6, 3),
                         "src": round(src["start"] / 1e6, 3) if src else None})
        tracks.append({"type": t["type"],
                       "role": {0: "main", 1: "text", 2: "overlay"}.get(t["flag"], t["flag"]),
                       "segments": segs})
    out = {"duration": round(d["duration"] / 1e6, 3), "tracks": tracks}
    print(json.dumps(out, indent=1))


def opt(rest, flag, cast=str, default=None):
    return cast(rest[rest.index(flag) + 1]) if flag in rest else default


LIVE_LANE_COMMANDS = {
    "seek", "select", "split", "delete", "del", "trim-left", "trim-right",
    "undo", "redo", "marker", "zoomfit", "save", "export", "shot", "dump",
    "click", "clickxy", "key", "play", "playhead", "clips", "open",
}


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(USAGE)
    cmd, rest = args[0], args[1:]
    if cmd in LIVE_LANE_COMMANDS:
        live_lane_unsupported(cmd)
    elif cmd == "replay":
        cmd_replay(rest[0], opt(rest, "--name"), opt(rest, "--jobs-dir"))
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
        cmd_graphics(rest[0], rest[1], opt(rest, "--jobs-dir"))
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
    elif cmd == "state":
        cmd_state(opt(rest, "--draft"))
    elif cmd == "ls":
        root = json.loads((DRAFT_ROOT / "root_meta_info.json").read_text(encoding="utf-8"))
        for d in root["all_draft_store"]:
            print(f"{d['draft_name']}: {d['tm_duration'] / 1e6:.2f}s  {d['draft_fold_path']}")
    elif cmd == "launch":
        launch_app()
        print("CapCut launched")
    elif cmd == "quit":
        quit_app()
        print("CapCut quit")
    else:
        sys.exit(f"unknown command: {cmd}")


if __name__ == "__main__":
    main()
