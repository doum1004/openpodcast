#!/usr/bin/env python3
"""
render_podcast_video.py

Renders a podcast video from JSON metadata + WAV audio files using FFmpeg.
Each host is displayed in a corner, and when they speak, their card glows
and their script text appears as a subtitle. Overlapping speakers are handled
with split/stacked subtitle layouts.

Supports --highlights-only mode to render vertical (9:16) short-form highlight clips.

Usage:
    python render_podcast_video.py output/jidaenanto_ep01/podcast_data.json
    python render_podcast_video.py output/jidaenanto_ep01/podcast_data.json --highlights-only
    python render_podcast_video.py output/jidaenanto_ep01/podcast_data.json --speed 1.25

Requirements:
    - Python 3.10+
    - Pillow (pip install Pillow)
    - FFmpeg with libass support (system install)
    - Korean font file (e.g., NanumGothicBold.ttf)
"""

import json
import math
import os
import subprocess
import sys
import textwrap
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageFilter

def clean_display_text(text: str) -> str:
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'（[^）]*）', '', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip()

# ══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════════

VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
FPS = 30
SPEED = 1.0
CARD_W = 320
CARD_H = 280
MARGIN = 80
BG_COLOR = "#1a1a2e"
AVATAR_SIZE = 175

# ── Shorts (vertical 9:16) defaults ──
SHORTS_WIDTH = 1080
SHORTS_HEIGHT = 1920
SHORTS_CARD_W = 280
SHORTS_CARD_H = 250
SHORTS_AVATAR_SIZE = 150
SHORTS_MARGIN = 30

FONT_PATHS = [
    "NanumGothicBold.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "C:/Windows/Fonts/NanumGothicBold.ttf",
    "C:/Windows/Fonts/malgunbd.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
]

HOST_COLORS = {
    "host_1": {"hex": "#4A90D9", "rgb": (74, 144, 217)},
    "host_2": {"hex": "#D94A4A", "rgb": (217, 74, 74)},
    "host_3": {"hex": "#4AD97A", "rgb": (74, 217, 122)},
    "host_4": {"hex": "#D9D94A", "rgb": (217, 217, 74)},
}

# ══════════════════════════════════════════════════════════════
#  DATA CLASSES
# ══════════════════════════════════════════════════════════════

@dataclass
class HostInfo:
    key: str
    name: str
    color_hex: str
    color_rgb: tuple
    image_path: str = ""
    position: tuple = (0, 0)
    card_normal_path: str = ""
    card_active_path: str = ""


@dataclass
class DialogueEvent:
    dialogue_id: int
    index: int
    speaker: str
    name: str
    text: str
    emotion: str
    interrupt_type: str
    start_ms: int
    end_ms: int
    duration_ms: int
    section_title: str
    markers: dict
    overlap_ms: int = 0


@dataclass
class OverlapZone:
    start_ms: int
    end_ms: int
    duration_ms: int
    speakers: list


@dataclass
class SectionInfo:
    title: str
    start_ms: int
    end_ms: int


@dataclass
class HighlightInfo:
    ids: list
    title: str
    description: str
    tags: list
    start_ms: int = 0
    end_ms: int = 0
    events: list = field(default_factory=list)


# ══════════════════════════════════════════════════════════════
#  FONT RESOLVER
# ══════════════════════════════════════════════════════════════

def find_font(size: int = 28) -> ImageFont.FreeTypeFont:
    for path in FONT_PATHS:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    print("⚠️  No Korean font found. Using default font.")
    return ImageFont.load_default()


def find_font_path() -> str:
    for path in FONT_PATHS:
        if os.path.exists(path):
            return path
    return "sans-serif"


# ══════════════════════════════════════════════════════════════
#  AUDIO DURATION PROBE
# ══════════════════════════════════════════════════════════════

def probe_audio_duration_ms(audio_path: str) -> Optional[int]:
    if not audio_path or not os.path.exists(audio_path):
        return None
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", audio_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        info = json.loads(result.stdout)
        fmt_duration = info.get("format", {}).get("duration")
        if fmt_duration:
            return int(float(fmt_duration) * 1000)
        for stream in info.get("streams", []):
            if stream.get("codec_type") == "audio":
                dur = stream.get("duration")
                if dur:
                    return int(float(dur) * 1000)
        return None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError, OSError) as e:
        print(f"    ⚠️  ffprobe failed for {audio_path}: {e}")
        return None


# ══════════════════════════════════════════════════════════════
#  SPEED HELPERS
# ══════════════════════════════════════════════════════════════

def apply_speed_to_ms(ms: int, speed: float) -> int:
    """Convert an original timestamp (ms) to its sped-up equivalent."""
    if speed == 1.0:
        return ms
    return int(ms / speed)


def scale_events_for_speed(events: list[DialogueEvent], speed: float) -> list[DialogueEvent]:
    """Return a new list of events with timestamps scaled by speed."""
    if speed == 1.0:
        return events
    scaled = []
    for e in events:
        scaled.append(DialogueEvent(
            dialogue_id=e.dialogue_id, index=e.index, speaker=e.speaker,
            name=e.name, text=e.text, emotion=e.emotion,
            interrupt_type=e.interrupt_type,
            start_ms=apply_speed_to_ms(e.start_ms, speed),
            end_ms=apply_speed_to_ms(e.end_ms, speed),
            duration_ms=apply_speed_to_ms(e.duration_ms, speed),
            section_title=e.section_title,
            markers=e.markers, overlap_ms=apply_speed_to_ms(e.overlap_ms, speed),
        ))
    return scaled


def scale_sections_for_speed(sections: list[SectionInfo], speed: float) -> list[SectionInfo]:
    """Return a new list of sections with timestamps scaled by speed."""
    if speed == 1.0:
        return sections
    return [
        SectionInfo(
            title=s.title,
            start_ms=apply_speed_to_ms(s.start_ms, speed),
            end_ms=apply_speed_to_ms(s.end_ms, speed),
        )
        for s in sections
    ]


# ══════════════════════════════════════════════════════════════
#  PARSING
# ══════════════════════════════════════════════════════════════

def parse_podcast_json(json_path: str):
    json_path = Path(json_path).resolve()
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    base_dir = str(json_path.parent)

    hosts: dict[str, HostInfo] = {}
    hosts_array = data.get("hosts", [])
    if hosts_array:
        for h in hosts_array:
            key = h["id"]
            c = HOST_COLORS.get(key, HOST_COLORS["host_1"])
            image_path = h.get("image", "")
            if image_path:
                image_path = image_path.replace("\\", os.sep)
                if not os.path.isabs(image_path):
                    candidate = os.path.join(base_dir, os.path.basename(image_path))
                    if os.path.exists(candidate):
                        image_path = candidate
                    else:
                        candidate = str(Path(json_path).parent.parent / image_path)
                        if os.path.exists(candidate):
                            image_path = candidate
                        else:
                            candidate = str(Path.cwd() / image_path.replace("\\", os.sep))
                            if os.path.exists(candidate):
                                image_path = candidate
            hosts[key] = HostInfo(
                key=key, name=h["name"],
                color_hex=c["hex"], color_rgb=c["rgb"],
                image_path=image_path,
            )
            print(f"    👤 {h['name']} ({key}): image={'✅' if os.path.exists(image_path) else '❌'} {image_path}")
    else:
        dist = data.get("analysis", {}).get("dialogue_distribution", {})
        for key, info in dist.items():
            c = HOST_COLORS.get(key, HOST_COLORS["host_1"])
            hosts[key] = HostInfo(key=key, name=info["name"], color_hex=c["hex"], color_rgb=c["rgb"])

    if not hosts:
        raise ValueError("No hosts found in JSON data. Cannot render video without host information.")

    sections: list[SectionInfo] = []
    section_map: dict[int, str] = {}
    events: list[DialogueEvent] = []
    for sec in data.get("sections_timeline", []):
        sections.append(SectionInfo(title=sec["section_title"], start_ms=sec["audio_start_ms"], end_ms=sec["audio_end_ms"]))
        for script in sec.get("scripts", []):
            section_map[script["dialogue_id"]] = sec["section_title"]
            events.append(DialogueEvent(
                dialogue_id=script["dialogue_id"], index=script.get("index", 0),
                speaker=script["speaker"], name=script["name"], text=script["text"],
                emotion=script.get("emotion", ""), interrupt_type=script.get("interrupt_type", "none"),
                start_ms=script["audio_start_ms"], end_ms=script["audio_end_ms"],
                duration_ms=script["audio_duration_ms"],
                section_title=section_map.get(script["dialogue_id"], ""),
                markers=script.get("markers", {}), overlap_ms=script.get("audio_overlap_ms", 0),
            ))

    events.sort(key=lambda e: (e.start_ms, e.dialogue_id))

    highlights: list[HighlightInfo] = []
    for h in data.get("highlights", []):
        highlights.append(HighlightInfo(
            ids=h.get("ids", []), title=h.get("title", ""),
            description=h.get("description", ""), tags=h.get("tags", []),
        ))

    mix_path = data.get("mix", {}).get("output_file", "")
    if mix_path:
        mix_path = mix_path.replace("\\", os.sep)
        if not os.path.isabs(mix_path) or not os.path.exists(mix_path):
            candidate = os.path.join(base_dir, os.path.basename(mix_path))
            if os.path.exists(candidate):
                mix_path = candidate
            else:
                candidate = str(Path.cwd() / mix_path.replace("\\", os.sep))
                if os.path.exists(candidate):
                    mix_path = candidate

    total_duration_ms = data.get("summary", {}).get("total_audio_duration_ms", 0)
    if not total_duration_ms and events:
        total_duration_ms = max(e.end_ms for e in events)

    return data, hosts, events, sections, highlights, mix_path, total_duration_ms, base_dir


# ══════════════════════════════════════════════════════════════
#  HIGHLIGHT PROCESSING
# ══════════════════════════════════════════════════════════════

def resolve_highlights(highlights: list[HighlightInfo], events: list[DialogueEvent]) -> list[HighlightInfo]:
    event_map = {e.dialogue_id: e for e in events}
    resolved = []
    for hl in highlights:
        matched_events = []
        for did in hl.ids:
            if did in event_map:
                matched_events.append(event_map[did])
            else:
                print(f"    ⚠️  Highlight '{hl.title}': dialogue_id {did} not found")
        if not matched_events:
            print(f"    ⚠️  Highlight '{hl.title}': no matching events, skipping")
            continue
        matched_events.sort(key=lambda e: e.start_ms)
        hl.events = matched_events
        hl.start_ms = matched_events[0].start_ms
        hl.end_ms = matched_events[-1].end_ms
        resolved.append(hl)
    return resolved


def extract_highlight_audio(audio_path: str, highlight: HighlightInfo, output_path: str, padding_ms: int = 300) -> Optional[str]:
    start_sec = max(0, (highlight.start_ms - padding_ms)) / 1000.0
    end_sec = (highlight.end_ms + padding_ms) / 1000.0
    duration_sec = end_sec - start_sec
    cmd = ["ffmpeg", "-y", "-i", audio_path, "-ss", f"{start_sec:.3f}", "-t", f"{duration_sec:.3f}", "-c:a", "pcm_s16le", output_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    ❌ Audio extraction failed: {result.stderr[:300]}")
        return None
    return output_path


def shift_events_for_highlight(events: list[DialogueEvent], highlight_start_ms: int, padding_ms: int = 300) -> list[DialogueEvent]:
    offset = highlight_start_ms - padding_ms
    shifted = []
    for e in events:
        shifted.append(DialogueEvent(
            dialogue_id=e.dialogue_id, index=e.index, speaker=e.speaker,
            name=e.name, text=e.text, emotion=e.emotion,
            interrupt_type=e.interrupt_type,
            start_ms=max(0, e.start_ms - offset), end_ms=max(0, e.end_ms - offset),
            duration_ms=e.duration_ms, section_title=e.section_title,
            markers=e.markers, overlap_ms=e.overlap_ms,
        ))
    return shifted


# ══════════════════════════════════════════════════════════════
#  OVERLAP DETECTION
# ══════════════════════════════════════════════════════════════

def detect_overlaps(events: list[DialogueEvent]) -> list[OverlapZone]:
    if not events:
        return []
    boundaries = set()
    for e in events:
        boundaries.add(e.start_ms)
        boundaries.add(e.end_ms)
    boundaries = sorted(boundaries)
    zones: list[OverlapZone] = []
    for i in range(len(boundaries) - 1):
        t_start = boundaries[i]
        t_end = boundaries[i + 1]
        if t_start >= t_end:
            continue
        mid = (t_start + t_end) / 2
        active = [e for e in events if e.start_ms <= mid < e.end_ms]
        if len(active) >= 2:
            zones.append(OverlapZone(start_ms=t_start, end_ms=t_end, duration_ms=t_end - t_start, speakers=active))
    merged: list[OverlapZone] = []
    for z in zones:
        speaker_set = frozenset(e.dialogue_id for e in z.speakers)
        if merged:
            prev = merged[-1]
            prev_set = frozenset(e.dialogue_id for e in prev.speakers)
            if prev.end_ms == z.start_ms and prev_set == speaker_set:
                prev.end_ms = z.end_ms
                prev.duration_ms = prev.end_ms - prev.start_ms
                continue
        merged.append(z)
    return merged


# ══════════════════════════════════════════════════════════════
#  HOST CARD POSITIONS
# ══════════════════════════════════════════════════════════════

def calculate_positions(hosts: dict[str, HostInfo], is_shorts: bool = False):
    """Assign positions based on host count. Shorts = vertical layout centered."""
    n = len(hosts)

    if is_shorts:
        # ── Vertical (9:16) layout ──
        # Cards arranged horizontally, CENTERED in the middle vertical zone
        w = SHORTS_WIDTH
        h = SHORTS_HEIGHT
        cw = SHORTS_CARD_W
        ch = SHORTS_CARD_H
        m = SHORTS_MARGIN

        # Center cards vertically in the screen (middle area)
        center_y = (h // 2) - (ch // 2)

        if n == 1:
            positions = [(w // 2 - cw // 2, center_y)]
        elif n == 2:
            gap = (w - 2 * cw) // 3
            positions = [
                (gap, center_y),
                (gap * 2 + cw, center_y),
            ]
        elif n == 3:
            gap = (w - 3 * cw) // 4
            positions = [
                (gap, center_y),
                (gap * 2 + cw, center_y),
                (gap * 3 + cw * 2, center_y),
            ]
        else:
            # 4 hosts: 2x2 grid centered
            gap_x = (w - 2 * cw) // 3
            row_gap = 20
            total_h = 2 * ch + row_gap
            top_row_y = (h // 2) - (total_h // 2)
            bot_row_y = top_row_y + ch + row_gap
            positions = [
                (gap_x, top_row_y),
                (gap_x * 2 + cw, top_row_y),
                (gap_x, bot_row_y),
                (gap_x * 2 + cw, bot_row_y),
            ]
    else:
        # ── Landscape (16:9) layout ──
        TOP_OFFSET = VIDEO_HEIGHT * 0.1
        corners = {
            1: [(MARGIN, MARGIN + TOP_OFFSET)],
            2: [(MARGIN, MARGIN + TOP_OFFSET), (VIDEO_WIDTH - MARGIN - CARD_W, MARGIN + TOP_OFFSET)],
            3: [
                (MARGIN, MARGIN + TOP_OFFSET),
                (VIDEO_WIDTH - MARGIN - CARD_W, MARGIN + TOP_OFFSET),
                (VIDEO_WIDTH // 2 - CARD_W // 2, VIDEO_HEIGHT - MARGIN - CARD_H - 200),
            ],
            4: [
                (MARGIN, MARGIN + TOP_OFFSET),
                (VIDEO_WIDTH - MARGIN - CARD_W, MARGIN + TOP_OFFSET),
                (MARGIN, VIDEO_HEIGHT - MARGIN - CARD_H - 200),
                (VIDEO_WIDTH - MARGIN - CARD_W, VIDEO_HEIGHT - MARGIN - CARD_H - 200),
            ],
        }
        positions = corners.get(n, corners[4])

    for i, (key, host) in enumerate(hosts.items()):
        if i < len(positions):
            host.position = positions[i]
        else:
            host.position = positions[-1]


# ══════════════════════════════════════════════════════════════
#  HOST CARD GENERATION
# ══════════════════════════════════════════════════════════════

def make_circle_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse([(0, 0), (size - 1, size - 1)], fill=255)
    return mask


def load_host_avatar(image_path: str, size: int = AVATAR_SIZE) -> Optional[Image.Image]:
    if not image_path or not os.path.exists(image_path):
        return None
    try:
        img = Image.open(image_path).convert("RGBA")
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
        img = img.resize((size, size), Image.LANCZOS)
        mask = make_circle_mask(size)
        output = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        output.paste(img, (0, 0), mask)
        return output
    except Exception as e:
        print(f"    ⚠️  Failed to load avatar {image_path}: {e}")
        return None


def generate_host_card(
    host: HostInfo,
    active: bool,
    output_path: str,
    size: tuple = (CARD_W, CARD_H),
    avatar_size: int = AVATAR_SIZE,
    is_shorts: bool = False,
):
    w, h = size
    pad = 20 if active else 0
    canvas_w = w + pad * 2
    canvas_h = h + pad * 2

    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    if active:
        glow = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.rounded_rectangle(
            [(pad - 8, pad - 8), (pad + w + 8, pad + h + 8)],
            radius=20,
            fill=(*host.color_rgb, 90),
        )
        glow = glow.filter(ImageFilter.GaussianBlur(radius=14))
        img = Image.alpha_composite(img, glow)

    draw = ImageDraw.Draw(img)

    bg_alpha = 220 if active else 140
    border_color = host.color_rgb + (255,) if active else (255, 255, 255, 40)
    border_width = 3 if active else 2

    draw.rounded_rectangle(
        [(pad, pad), (pad + w - 1, pad + h - 1)],
        radius=18,
        fill=(25, 25, 40, bg_alpha),
        outline=border_color,
        width=border_width,
    )

    r = avatar_size // 2
    top_padding = 20
    avatar_name_gap = 12
    name_wave_gap = 10
    wave_height = 24

    cx = pad + w // 2
    avatar_cy = pad + top_padding + r

    font_size_name = 24
    font_name = find_font(font_size_name)
    name_y = avatar_cy + r + avatar_name_gap

    name_bbox = font_name.getbbox(host.name)
    name_text_h = name_bbox[3] - name_bbox[1] if name_bbox else font_size_name
    wave_base_y = name_y + name_text_h + name_wave_gap + wave_height

    avatar_img = load_host_avatar(host.image_path, avatar_size)

    if avatar_img:
        ax = cx - r
        ay = avatar_cy - r
        img.paste(avatar_img, (ax, ay), avatar_img)
        draw = ImageDraw.Draw(img)
        border_col = host.color_rgb + (255,) if active else (255, 255, 255, 120)
        draw.ellipse(
            [(cx - r - 2, avatar_cy - r - 2), (cx + r + 2, avatar_cy + r + 2)],
            outline=border_col, width=3 if active else 2,
        )
    else:
        avatar_fill = host.color_rgb + (255,) if active else host.color_rgb + (160,)
        draw.ellipse(
            [(cx - r, avatar_cy - r), (cx + r, avatar_cy + r)],
            fill=avatar_fill,
            outline=(255, 255, 255, 200) if active else (255, 255, 255, 80),
            width=2,
        )
        font_initial = find_font(72)
        draw.text((cx, avatar_cy), host.name[0], fill="white", font=font_initial, anchor="mm")

    name_color = "white" if active else (200, 200, 200, 200)
    draw.text((cx, name_y), host.name, fill=name_color, font=font_name, anchor="mt")

    if active:
        bar_w = 5
        bar_gap = 4
        num_bars = 9
        total_bar_w = num_bars * bar_w + (num_bars - 1) * bar_gap
        bar_start_x = cx - total_bar_w // 2
        for i in range(num_bars):
            bar_h = 6 + int(10 * abs(math.sin(i * 0.8)))
            bx = bar_start_x + i * (bar_w + bar_gap)
            draw.rounded_rectangle(
                [(bx, wave_base_y - bar_h), (bx + bar_w, wave_base_y)],
                radius=2, fill=host.color_rgb + (200,),
            )

    img.save(output_path, "PNG")
    return output_path


def generate_all_host_cards(hosts: dict[str, HostInfo], temp_dir: str, is_shorts: bool = False):
    os.makedirs(temp_dir, exist_ok=True)
    card_size = (SHORTS_CARD_W, SHORTS_CARD_H) if is_shorts else (CARD_W, CARD_H)
    av_size = SHORTS_AVATAR_SIZE if is_shorts else AVATAR_SIZE

    for key, host in hosts.items():
        normal_path = os.path.join(temp_dir, f"{key}_normal.png")
        active_path = os.path.join(temp_dir, f"{key}_active.png")
        generate_host_card(host, active=False, output_path=normal_path, size=card_size, avatar_size=av_size, is_shorts=is_shorts)
        generate_host_card(host, active=True, output_path=active_path, size=card_size, avatar_size=av_size, is_shorts=is_shorts)
        host.card_normal_path = normal_path
        host.card_active_path = active_path
        print(f"  🎨 {host.name}: {normal_path}, {active_path}")


# ══════════════════════════════════════════════════════════════
#  ASS SUBTITLE GENERATION
# ══════════════════════════════════════════════════════════════

def ms_to_ass(ms: int) -> str:
    if ms < 0:
        ms = 0
    total_cs = ms // 10
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def rgb_to_ass_color(r: int, g: int, b: int, a: int = 0) -> str:
    return f"&H{a:02X}{b:02X}{g:02X}{r:02X}"


def escape_ass_text(text: str) -> str:
    text = text.replace("\\N", "\x00LINEBREAK\x00")
    text = text.replace("\\", "\\\\")
    text = text.replace("{", "\\{")
    text = text.replace("}", "\\}")
    text = text.replace("\x00LINEBREAK\x00", "\\N")
    return text


def format_subtitle_text(text: str, max_lines: int = 3, chars_per_line: int = 50) -> str:
    if not text:
        return text
    lines = []
    while text:
        if len(text) <= chars_per_line:
            lines.append(text)
            break
        break_at = chars_per_line
        for i in range(chars_per_line, max(chars_per_line - 10, 0), -1):
            if i < len(text) and text[i] in " ,，.。!！?？、":
                break_at = i + 1
                break
        lines.append(text[:break_at].rstrip())
        text = text[break_at:].lstrip()
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip()
        if not lines[-1].endswith("..."):
            lines[-1] += "..."
    return "\\N".join(lines)


def generate_ass_subtitles(
    events: list[DialogueEvent],
    hosts: dict[str, HostInfo],
    output_path: str,
    font_path: str,
    highlight_title: str = "",
    highlight_title_duration_ms: int = 3000,
    is_shorts: bool = False,
):
    font_name = Path(font_path).stem if os.path.exists(font_path) else "Arial"

    res_x = SHORTS_WIDTH if is_shorts else VIDEO_WIDTH
    res_y = SHORTS_HEIGHT if is_shorts else VIDEO_HEIGHT

    # ── Font sizes and margins for shorts vs landscape ──
    if is_shorts:
        # Subtitles: larger font, use full width, more lines allowed
        solo_fontsize = 44
        overlap_fontsize = 38
        stack_fontsize = 32
        title_fontsize = 52

        # Subtitles positioned above bottom (not at very bottom)
        # MarginV from bottom edge — higher value = further from bottom
        solo_margin_v = 400
        overlap_line_height = 100
        overlap_base_margin = 180
        stack_line_height = 85
        stack_base_margin = 160

        # Title positioned below top but not at very top
        title_margin_v = 120

        # Use full width — minimal left/right margins
        subtitle_margin_lr = 20
        chars_per_line = 28  # Wider since using full width
        max_lines = 6  # No clamping — allow more lines
    else:
        solo_fontsize = 42
        overlap_fontsize = 36
        stack_fontsize = 30
        title_fontsize = 56
        solo_margin_v = 60
        overlap_line_height = 110
        overlap_base_margin = 50
        stack_line_height = 90
        stack_base_margin = 40
        title_margin_v = 40
        subtitle_margin_lr = 60
        chars_per_line = 50
        max_lines = 3

    @dataclass
    class SubSegment:
        start_ms: int
        end_ms: int
        event: DialogueEvent
        concurrent: list
        position_index: int = 0
        total_concurrent: int = 1

    def compute_segments(event: DialogueEvent) -> list[SubSegment]:
        boundaries = {event.start_ms, event.end_ms}
        for other in events:
            if other.dialogue_id == event.dialogue_id:
                continue
            if other.start_ms < event.end_ms and other.end_ms > event.start_ms:
                if event.start_ms < other.start_ms < event.end_ms:
                    boundaries.add(other.start_ms)
                if event.start_ms < other.end_ms < event.end_ms:
                    boundaries.add(other.end_ms)
        boundaries = sorted(boundaries)
        segments = []
        for i in range(len(boundaries) - 1):
            seg_start = boundaries[i]
            seg_end = boundaries[i + 1]
            if seg_start >= seg_end:
                continue
            mid = (seg_start + seg_end) / 2
            concurrent = [
                e for e in events
                if e.dialogue_id != event.dialogue_id and e.start_ms <= mid < e.end_ms
            ]
            all_active = [event] + concurrent
            all_active.sort(key=lambda e: (e.start_ms, e.dialogue_id))
            pos_idx = next(i for i, e in enumerate(all_active) if e.dialogue_id == event.dialogue_id)
            segments.append(SubSegment(
                start_ms=seg_start, end_ms=seg_end, event=event,
                concurrent=concurrent, position_index=pos_idx,
                total_concurrent=len(all_active),
            ))
        return segments

    all_segments: list[SubSegment] = []
    for event in events:
        all_segments.extend(compute_segments(event))
    all_segments.sort(key=lambda s: (s.start_ms, s.position_index))

    white = rgb_to_ass_color(255, 255, 255)

    header = f"""[Script Info]
Title: Podcast Video Subtitles
ScriptType: v4.00+
PlayResX: {res_x}
PlayResY: {res_y}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Solo,{font_name},{solo_fontsize},{white},&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,4,3,2,2,{subtitle_margin_lr},{subtitle_margin_lr},{solo_margin_v},1
Style: Overlap0,{font_name},{overlap_fontsize},{white},&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,4,2,1,2,{subtitle_margin_lr},{subtitle_margin_lr},{overlap_base_margin},1
Style: Overlap1,{font_name},{overlap_fontsize},{white},&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,4,2,1,2,{subtitle_margin_lr},{subtitle_margin_lr},{overlap_base_margin + overlap_line_height},1
Style: Stack0,{font_name},{stack_fontsize},{white},&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,4,2,1,2,{subtitle_margin_lr},{subtitle_margin_lr},{stack_base_margin},1
Style: Stack1,{font_name},{stack_fontsize},{white},&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,4,2,1,2,{subtitle_margin_lr},{subtitle_margin_lr},{stack_base_margin + stack_line_height},1
Style: Stack2,{font_name},{stack_fontsize},{white},&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,4,2,1,2,{subtitle_margin_lr},{subtitle_margin_lr},{stack_base_margin + stack_line_height * 2},1
Style: Stack3,{font_name},{stack_fontsize},{white},&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,4,2,1,2,{subtitle_margin_lr},{subtitle_margin_lr},{stack_base_margin + stack_line_height * 3},1
Style: HighlightTitle,{font_name},{title_fontsize},{rgb_to_ass_color(0, 204, 255)},&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,4,4,3,8,40,40,{title_margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    dialogue_lines = []

    if highlight_title and not is_shorts:
        # For shorts: wrap the title text for narrow screen
        ht_display = highlight_title
        ht_escaped = escape_ass_text(ht_display)
        ht_start = ms_to_ass(0)
        ht_end = ms_to_ass(highlight_title_duration_ms)
        dialogue_lines.append(
            f"Dialogue: 10,{ht_start},{ht_end},HighlightTitle,,0,0,0,,"
            f"{{\\fad(500,500)}}{ht_escaped}"
        )

    for seg in all_segments:
        evt = seg.event
        start = ms_to_ass(seg.start_ms)
        end = ms_to_ass(seg.end_ms)

        host_info = hosts.get(evt.speaker)
        if host_info:
            ass_color = rgb_to_ass_color(*host_info.color_rgb)
        else:
            ass_color = white

        cleaned_text = clean_display_text(evt.text)
        if not cleaned_text.strip():
            continue

        if seg.total_concurrent == 1:
            display_text = format_subtitle_text(cleaned_text, max_lines=max_lines, chars_per_line=chars_per_line)
            display_text = escape_ass_text(display_text)
            dialogue_lines.append(
                f"Dialogue: 0,{start},{end},Solo,,0,0,0,,"
                f"{{\\c{ass_color}}}{display_text}"
            )
        elif seg.total_concurrent == 2:
            display_text = format_subtitle_text(cleaned_text, max_lines=max_lines, chars_per_line=chars_per_line)
            display_text = escape_ass_text(display_text)
            style = f"Overlap{seg.position_index}"
            dialogue_lines.append(
                f"Dialogue: 0,{start},{end},{style},,0,0,0,,"
                f"{{\\c{ass_color}}}{display_text}"
            )
        else:
            display_text = format_subtitle_text(cleaned_text, max_lines=max_lines, chars_per_line=chars_per_line)
            display_text = escape_ass_text(display_text)
            idx = min(seg.position_index, 3)
            style = f"Stack{idx}"
            dialogue_lines.append(
                f"Dialogue: 0,{start},{end},{style},,0,0,0,,"
                f"{{\\c{ass_color}}}{display_text}"
            )

    full_content = header + "\n".join(dialogue_lines) + "\n"
    with open(output_path, "w", encoding="utf-8-sig") as f:
        f.write(full_content)
    print(f"  📝 ASS subtitles: {output_path} ({'shorts' if is_shorts else 'landscape'})")
    print(f"     {len(dialogue_lines)} dialogue lines")
    return output_path


# ══════════════════════════════════════════════════════════════
#  BACKGROUND IMAGE HANDLING
# ══════════════════════════════════════════════════════════════

def resolve_background_image(bg_value: str, base_dir: str) -> Optional[str]:
    if not bg_value:
        return None
    extensions = ["", ".png", ".jpg", ".jpeg", ".webp"]
    search_dirs = [base_dir, os.path.join(base_dir, ".."), str(Path.cwd()), str(Path.cwd() / "assets"), str(Path.cwd() / "images")]
    for search_dir in search_dirs:
        for ext in extensions:
            candidate = os.path.join(search_dir, bg_value + ext).replace("\\", os.sep)
            if os.path.exists(candidate):
                return str(Path(candidate).resolve())
    direct = bg_value.replace("\\", os.sep)
    if os.path.exists(direct):
        return str(Path(direct).resolve())
    return None


def prepare_background_image(image_path: str, output_path: str, width: int = VIDEO_WIDTH, height: int = VIDEO_HEIGHT, darken: float = 0.4) -> str:
    try:
        img = Image.open(image_path).convert("RGBA")
        img_ratio = img.width / img.height
        target_ratio = width / height
        if img_ratio > target_ratio:
            new_h = height
            new_w = int(height * img_ratio)
        else:
            new_w = width
            new_h = int(width / img_ratio)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        left = (new_w - width) // 2
        top = (new_h - height) // 2
        img = img.crop((left, top, left + width, top + height))
        dark_overlay = Image.new("RGBA", (width, height), (0, 0, 0, int(255 * darken)))
        img = Image.alpha_composite(img, dark_overlay)
        img = img.convert("RGB")
        img.save(output_path, "PNG")
        return output_path
    except Exception as e:
        print(f"    ⚠️  Failed to prepare background: {e}")
        return ""


# ══════════════════════════════════════════════════════════════
#  FFMPEG COMMAND BUILDER
# ══════════════════════════════════════════════════════════════

def build_ffmpeg_command(
    hosts: dict[str, HostInfo],
    events: list[DialogueEvent],
    sections: list[SectionInfo],
    audio_path: str,
    ass_path: str,
    total_duration_ms: int,
    output_path: str,
    font_path: str,
    title: str = "",
    heat_level: str = "",
    background_image: str = "",
    highlight_title: str = "",
    is_shorts: bool = False,
    speed: float = 1.0,
):
    total_duration_sec = total_duration_ms / 1000.0

    vid_w = SHORTS_WIDTH if is_shorts else VIDEO_WIDTH
    vid_h = SHORTS_HEIGHT if is_shorts else VIDEO_HEIGHT

    inputs = []
    input_map = {}
    idx = 0

    inputs.append(audio_path)
    audio_idx = idx
    idx += 1

    bg_input_idx = None
    if background_image and os.path.exists(background_image):
        inputs.append(background_image)
        bg_input_idx = idx
        idx += 1

    for key, host in hosts.items():
        inputs.append(host.card_normal_path)
        input_map[f"{key}_normal"] = idx
        idx += 1
        inputs.append(host.card_active_path)
        input_map[f"{key}_active"] = idx
        idx += 1

    if font_path and os.path.exists(font_path):
        ff_font = font_path.replace("\\", "/")
        if len(ff_font) >= 2 and ff_font[1] == ":":
            ff_font = ff_font[0] + "\\:" + ff_font[2:]
        fontfile_opt = f":fontfile='{ff_font}'"
    else:
        fontfile_opt = ""

    def dt_escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace("'", "\u2019").replace(":", "\\:").replace("%", "%%")

    filters = []

    # ── Audio speed adjustment ──
    # Build atempo chain (atempo only supports 0.5–100.0 per instance)
    audio_filters = []
    if speed != 1.0:
        remaining = speed
        while remaining > 100.0:
            audio_filters.append("atempo=100.0")
            remaining /= 100.0
        while remaining < 0.5:
            audio_filters.append("atempo=0.5")
            remaining /= 0.5
        audio_filters.append(f"atempo={remaining:.6f}")

    if audio_filters:
        atempo_chain = ",".join(audio_filters)
        filters.append(f"[{audio_idx}:a]{atempo_chain}[aout]")
        audio_out_label = "aout"
    else:
        audio_out_label = f"{audio_idx}:a"

    if bg_input_idx is not None:
        filters.append(
            f"[{bg_input_idx}:v]loop=loop=-1:size=1"
            f",setpts=N/{FPS}/TB"
            f",scale={vid_w}:{vid_h}"
            f",trim=duration={total_duration_sec:.3f}"
            f",setpts=PTS-STARTPTS"
            f",fps={FPS}"
            f"[bg]"
        )
    else:
        filters.append(
            f"color=c={BG_COLOR}:s={vid_w}x{vid_h}"
            f":d={total_duration_sec:.3f}:r={FPS}"
            f"[bg]"
        )

    # Title text — for shorts: moved lower, bigger font, bold
    display_title = highlight_title if highlight_title else title
    safe_title = dt_escape(display_title)

    if is_shorts:
        title_fontsize = 55
        title_y = 300
        padding = 10
    else:
        title_fontsize = 50
        title_y = 20
        padding = 20

    max_text_width = vid_w - padding
    max_chars_per_line = int(max_text_width / (title_fontsize * 1))

    import textwrap
    lines = textwrap.wrap(safe_title, width=max_chars_per_line)

    # Draw each line separately as its own drawtext filter
    prev_label = "bg"
    for i, line in enumerate(lines):
        line_y = title_y + (i * (title_fontsize + 10))  # 10px line spacing
        next_label = "bg_t" if i == len(lines) - 1 else f"bg_t{i}"
        
        filters.append(
            f"[{prev_label}]drawtext=text='{line}'"
            f":fontsize={title_fontsize}:fontcolor=white"
            f":x=(w-text_w)/2:y={line_y}"
            f"{fontfile_opt}"
            f":shadowcolor=black:shadowx=2:shadowy=2"
            f"[{next_label}]"
        )
        prev_label = next_label

    current_label = "bg_t"

    # Section titles — skip for shorts (no top/bottom clutter)
    if not is_shorts:
        for i, sec in enumerate(sections):
            s = sec.start_ms / 1000.0
            e = sec.end_ms / 1000.0
            safe_sec = dt_escape(sec.title)
            next_label = f"sec{i}"
            sec_fontsize = 40
            sec_y = 90
            filters.append(
                f"[{current_label}]drawtext=text='{safe_sec}'"
                f":fontsize={sec_fontsize}:fontcolor=#00CCFF"
                f":x=(w-text_w)/2:y={sec_y}"
                f"{fontfile_opt}"
                f":shadowcolor=black:shadowx=1:shadowy=1"
                f":enable='between(t\\,{s:.3f}\\,{e:.3f})'"
                f"[{next_label}]"
            )
            current_label = next_label

    # Host card overlays
    glow_pad = 30
    for key, host in hosts.items():
        x, y = host.position
        normal_idx = input_map[f"{key}_normal"]
        active_idx = input_map[f"{key}_active"]

        enable_parts = []
        for evt in events:
            if evt.speaker == key:
                s = evt.start_ms / 1000.0
                e = evt.end_ms / 1000.0
                enable_parts.append(f"between(t\\,{s:.3f}\\,{e:.3f})")

        if enable_parts:
            active_enable = "+".join(enable_parts)
            not_active_enable = f"not({active_enable})"
        else:
            active_enable = "0"
            not_active_enable = "1"

        next_label = f"n{key[-1]}"
        filters.append(
            f"[{current_label}][{normal_idx}:v]overlay="
            f"x={x}:y={y}:enable='{not_active_enable}'"
            f"[{next_label}]"
        )
        current_label = next_label

        next_label = f"a{key[-1]}"
        filters.append(
            f"[{current_label}][{active_idx}:v]overlay="
            f"x={x - glow_pad}:y={y - glow_pad}"
            f":enable='{active_enable}'"
            f"[{next_label}]"
        )
        current_label = next_label

    # ASS subtitles
    ass_ffmpeg = ass_path.replace("\\", "/")
    if len(ass_ffmpeg) >= 2 and ass_ffmpeg[1] == ":":
        ass_ffmpeg = ass_ffmpeg[0] + "\\:" + ass_ffmpeg[2:]

    final_label = "outv"
    filters.append(f"[{current_label}]ass='{ass_ffmpeg}'[{final_label}]")

    filter_str = ";".join(filters)

    cmd_list = ["ffmpeg", "-y"]
    for inp in inputs:
        cmd_list.extend(["-i", inp])
    cmd_list.extend([
        "-filter_complex", filter_str,
        "-map", f"[{final_label}]",
        "-map", f"[{audio_out_label}]" if speed != 1.0 else f"{audio_idx}:a",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        output_path,
    ])

    return cmd_list


# ══════════════════════════════════════════════════════════════
#  AUDIO MIX FALLBACK
# ══════════════════════════════════════════════════════════════

def build_audio_mix_if_needed(events, total_duration_ms, base_dir, output_path):
    if os.path.exists(output_path):
        print(f"  🎧 Using existing audio mix: {output_path}")
        return output_path
    print(f"  🎧 Building audio mix from {len(events)} WAV files...")
    inputs = []
    filter_parts = []
    for i, evt in enumerate(events):
        wav_path = evt.__dict__.get("output_path", "")
        if not wav_path:
            wav_path = os.path.join(base_dir, f"d_{evt.dialogue_id:04d}.wav")
        if not os.path.exists(wav_path):
            print(f"    ⚠️  Missing WAV: {wav_path}")
            continue
        inputs.append(f'-i "{wav_path}"')
        filter_parts.append(f"[{i}:a]adelay={evt.start_ms}|{evt.start_ms}[a{i}]")
    if not inputs:
        print("    ❌ No WAV files found!")
        return ""
    mix_inputs = "".join(f"[a{i}]" for i in range(len(inputs)))
    filter_parts.append(f"{mix_inputs}amix=inputs={len(inputs)}:duration=longest[aout]")
    filter_str = ";".join(filter_parts)
    cmd = f'ffmpeg -y {" ".join(inputs)} -filter_complex "{filter_str}" -map "[aout]" -c:a aac -b:a 192k "{output_path}"'
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    ❌ Audio mix failed:\n{result.stderr[:500]}")
    else:
        print(f"    ✅ Audio mix created: {output_path}")
    return output_path


# ══════════════════════════════════════════════════════════════
#  RENDER SINGLE HIGHLIGHT CLIP (SHORTS / VERTICAL)
# ══════════════════════════════════════════════════════════════

def render_highlight_clip(
    highlight: HighlightInfo,
    highlight_index: int,
    hosts: dict[str, HostInfo],
    all_events: list[DialogueEvent],
    audio_path: str,
    base_dir: str,
    temp_dir: str,
    font_path: str,
    title: str,
    background_image: str = "",
    padding_ms: int = 500,
    speed: float = 1.0,
) -> Optional[str]:
    print(f"\n  {'─' * 50}")
    print(f"  🎬 Highlight {highlight_index + 1}: {highlight.title}")
    print(f"     IDs: {highlight.ids}")
    print(f"     Time: {highlight.start_ms}ms → {highlight.end_ms}ms "
          f"({(highlight.end_ms - highlight.start_ms) / 1000:.1f}s)")
    print(f"     Tags: {', '.join(highlight.tags)}")
    print(f"     Format: 📱 Vertical {SHORTS_WIDTH}x{SHORTS_HEIGHT} (9:16 Shorts)")
    if speed != 1.0:
        print(f"     ⚡ Speed: {speed}x")

    # Extract audio
    clip_audio_path = os.path.join(temp_dir, f"highlight_{highlight_index:02d}_audio.wav")
    extracted = extract_highlight_audio(audio_path, highlight, clip_audio_path, padding_ms)
    if not extracted:
        print(f"     ❌ Failed to extract audio")
        return None

    clip_duration_ms = probe_audio_duration_ms(clip_audio_path)
    if clip_duration_ms is None:
        clip_duration_ms = (highlight.end_ms - highlight.start_ms) + padding_ms * 2

    # Apply speed to duration
    sped_clip_duration_ms = apply_speed_to_ms(clip_duration_ms, speed)
    print(f"     ⏱️  Clip duration: {clip_duration_ms / 1000:.1f}s"
          f"{f' → {sped_clip_duration_ms / 1000:.1f}s @ {speed}x' if speed != 1.0 else ''}")

    # Shift events then scale for speed
    shifted_events = shift_events_for_highlight(highlight.events, highlight.start_ms, padding_ms)
    scaled_events = scale_events_for_speed(shifted_events, speed)

    # Generate ASS subtitles (shorts mode) with speed-adjusted timestamps
    clip_ass_path = os.path.join(temp_dir, f"highlight_{highlight_index:02d}.ass")
    generate_ass_subtitles(
        scaled_events, hosts, clip_ass_path, font_path,
        highlight_title=highlight.title,
        highlight_title_duration_ms=apply_speed_to_ms(min(3000, clip_duration_ms // 3), speed),
        is_shorts=True,
    )

    # Prepare shorts-sized background
    shorts_bg = ""
    if background_image and os.path.exists(background_image):
        shorts_bg_path = os.path.join(temp_dir, f"highlight_{highlight_index:02d}_bg.png")
        shorts_bg = prepare_background_image(
            background_image, shorts_bg_path,
            SHORTS_WIDTH, SHORTS_HEIGHT, darken=0.5,
        )
    elif background_image == "":
        pass

    # Output path
    safe_title = re.sub(r'[^\w가-힣\s-]', '', highlight.title).strip()
    safe_title = re.sub(r'\s+', '_', safe_title)[:40]
    speed_suffix = f"_{speed}x" if speed != 1.0 else ""
    output_path = os.path.join(base_dir, f"highlight_{highlight_index + 1:02d}_{safe_title}{speed_suffix}.mp4")

    # Build FFmpeg command (shorts mode) with speed-scaled events/duration
    cmd_list = build_ffmpeg_command(
        hosts=hosts,
        events=scaled_events,
        sections=[],
        audio_path=clip_audio_path,
        ass_path=clip_ass_path,
        total_duration_ms=sped_clip_duration_ms,
        output_path=output_path,
        font_path=font_path,
        title=title,
        highlight_title=highlight.title,
        background_image=shorts_bg,
        is_shorts=True,
        speed=speed,
    )

    cmd_debug_path = os.path.join(temp_dir, f"highlight_{highlight_index:02d}_cmd.txt")
    with open(cmd_debug_path, "w", encoding="utf-8") as f:
        for i, part in enumerate(cmd_list):
            f.write(f"  [{i}] {part}\n")

    print(f"     🚀 Rendering...")
    result = subprocess.run(cmd_list, capture_output=True, text=True)

    if result.returncode == 0:
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"     ✅ {output_path} ({size_mb:.1f} MB)")
        return output_path
    else:
        print(f"     ❌ FFmpeg failed (exit code {result.returncode})")
        print(f"        {result.stderr[-500:]}")
        return None


# ══════════════════════════════════════════════════════════════
#  MAIN PIPELINE — HIGHLIGHTS ONLY (SHORTS)
# ══════════════════════════════════════════════════════════════

def render_highlights_only(json_path: str, output_dir: str = "", speed: float = 1.0):
    print("=" * 60)
    print("📱 Podcast Highlight Renderer (Vertical Shorts 9:16)")
    print("=" * 60)

    # Parse
    print("\n📂 Parsing JSON...")
    data, hosts, events, sections, highlights, audio_path, total_duration_ms, base_dir = \
        parse_podcast_json(json_path)

    podcast_info = data.get("podcast", {})
    title = podcast_info.get("title", "Podcast")

    print(f"  🎙️  {title}")
    print(f"  👥 Hosts: {len(hosts)}")
    print(f"  📝 Dialogues: {len(events)}")
    print(f"  🌟 Highlights: {len(highlights)}")
    print(f"  📱 Output format: {SHORTS_WIDTH}x{SHORTS_HEIGHT} (9:16 vertical)")
    if speed != 1.0:
        print(f"  ⚡ Speed: {speed}x")

    if not highlights:
        print("\n❌ No highlights found in JSON!")
        print("   Expected 'highlights' array with 'ids', 'title', etc.")
        return []

    # Resolve highlights
    print("\n🔍 Resolving highlights...")
    resolved = resolve_highlights(highlights, events)
    print(f"  ✅ {len(resolved)} highlights resolved")
    for i, hl in enumerate(resolved):
        dur = (hl.end_ms - hl.start_ms) / 1000.0
        sped_dur = dur / speed
        print(f"     [{i + 1}] {hl.title} ({dur:.1f}s"
              f"{f' → {sped_dur:.1f}s @ {speed}x' if speed != 1.0 else ''}"
              f", {len(hl.events)} events)")

    # Calculate positions (shorts layout)
    print("\n📐 Calculating host positions (vertical layout)...")
    calculate_positions(hosts, is_shorts=True)
    for key, host in hosts.items():
        print(f"  {host.name}: position={host.position}")

    # Generate host cards (shorts size)
    temp_dir = os.path.join(base_dir, "_temp_highlights")
    print(f"\n🎨 Generating host cards (shorts) → {temp_dir}")
    generate_all_host_cards(hosts, temp_dir, is_shorts=True)

    # Prepare background image
    bg_image_value = podcast_info.get("background_image", "")
    bg_source_path = ""

    if bg_image_value:
        print(f"\n🖼️  Background image: {bg_image_value}")
        bg_source = resolve_background_image(bg_image_value, base_dir)
        if bg_source:
            bg_source_path = bg_source
            print(f"  ✅ Found: {bg_source}")

    # Ensure audio
    print(f"\n🎧 Audio: {audio_path}")
    if not audio_path or not os.path.exists(audio_path):
        print("  ⚠️  Mixed audio not found, attempting to build from WAVs...")
        audio_path = os.path.join(base_dir, "mixed_audio.aac")
        audio_path = build_audio_mix_if_needed(events, total_duration_ms, base_dir, audio_path)
        if not audio_path or not os.path.exists(audio_path):
            print("  ❌ Cannot proceed without audio!")
            return []

    # Render each highlight
    font_path = find_font_path()
    output_paths = []

    print(f"\n{'=' * 60}")
    print(f"📱 Rendering {len(resolved)} vertical highlight clips...")
    print(f"{'=' * 60}")

    for i, hl in enumerate(resolved):
        result = render_highlight_clip(
            highlight=hl,
            highlight_index=i,
            hosts=hosts,
            all_events=events,
            audio_path=audio_path,
            base_dir=output_dir or base_dir,
            temp_dir=temp_dir,
            font_path=font_path,
            title=title,
            background_image=bg_source_path,
            speed=speed,
        )
        if result:
            output_paths.append(result)

    # Summary
    print(f"\n{'=' * 60}")
    print(f"✅ Highlight rendering complete!")
    print(f"   📱 Format: {SHORTS_WIDTH}x{SHORTS_HEIGHT} (9:16 vertical shorts)")
    if speed != 1.0:
        print(f"   ⚡ Speed: {speed}x")
    print(f"   📊 {len(output_paths)}/{len(resolved)} clips rendered successfully")
    for p in output_paths:
        size_mb = os.path.getsize(p) / (1024 * 1024)
        print(f"   📁 {p} ({size_mb:.1f} MB)")
    print(f"{'=' * 60}")

    return output_paths


# ══════════════════════════════════════════════════════════════
#  MAIN PIPELINE — FULL VIDEO (LANDSCAPE)
# ══════════════════════════════════════════════════════════════

def render_podcast_video(json_path: str, output_video: str = "", speed: float = 1.0):
    print("=" * 60)
    print("🎬 Podcast Video Renderer (FFmpeg)")
    print("=" * 60)

    print("\n📂 Parsing JSON...")
    data, hosts, events, sections, highlights, audio_path, total_duration_ms, base_dir = \
        parse_podcast_json(json_path)

    podcast_info = data.get("podcast", {})
    title = podcast_info.get("title", "Podcast")
    heat_level = podcast_info.get("heat_level", "")

    print(f"  🎙️  {title}")
    print(f"  🔥 {heat_level}")
    print(f"  👥 Hosts:")
    for key, host in hosts.items():
        img_status = "📷" if host.image_path and os.path.exists(host.image_path) else "🔤"
        print(f"     {img_status} {host.name} [{key}]")
    print(f"  📝 Dialogues: {len(events)}")
    print(f"  📌 Sections: {len(sections)}")
    print(f"  🌟 Highlights: {len(highlights)}")
    print(f"  ⏱️  Duration (from JSON): {total_duration_ms / 1000:.1f}s")
    if speed != 1.0:
        print(f"  ⚡ Speed: {speed}x → ~{total_duration_ms / speed / 1000:.1f}s")

    print("\n🔍 Detecting overlaps...")
    overlaps = detect_overlaps(events)
    if overlaps:
        print(f"  ⚡ Found {len(overlaps)} overlap zones:")
        for ov in overlaps:
            speakers = ", ".join(e.name for e in ov.speakers)
            print(f"     {ov.start_ms}ms → {ov.end_ms}ms ({ov.duration_ms}ms) — {speakers}")
    else:
        print("  ✅ No overlaps detected")

    print("\n📐 Calculating host positions...")
    calculate_positions(hosts, is_shorts=False)
    for key, host in hosts.items():
        print(f"  {host.name}: position={host.position}")

    temp_dir = os.path.join(base_dir, "_temp_render")
    print(f"\n🎨 Generating host cards → {temp_dir}")
    generate_all_host_cards(hosts, temp_dir, is_shorts=False)

    bg_image_value = podcast_info.get("background_image", "")
    bg_prepared_path = ""
    if bg_image_value:
        print(f"\n🖼️  Background image: {bg_image_value}")
        bg_source = resolve_background_image(bg_image_value, base_dir)
        if bg_source:
            print(f"  📁 Found: {bg_source}")
            bg_prepared_path = os.path.join(temp_dir, "background.png")
            bg_prepared_path = prepare_background_image(bg_source, bg_prepared_path, VIDEO_WIDTH, VIDEO_HEIGHT, darken=0.4)
            if bg_prepared_path:
                print(f"  ✅ Prepared: {bg_prepared_path}")
            else:
                print(f"  ⚠️  Failed to prepare, using solid color")
        else:
            print(f"  ⚠️  Not found, using solid color fallback")

    # Scale events and sections for speed
    scaled_events = scale_events_for_speed(events, speed)
    scaled_sections = scale_sections_for_speed(sections, speed)

    font_path = find_font_path()
    ass_path = os.path.join(temp_dir, "subtitles.ass")
    print(f"\n📝 Generating ASS subtitles...")
    generate_ass_subtitles(scaled_events, hosts, ass_path, font_path, is_shorts=False)

    print(f"\n🎧 Audio: {audio_path}")
    if not audio_path or not os.path.exists(audio_path):
        print("  ⚠️  Mixed audio not found, attempting to build from WAVs...")
        audio_path = os.path.join(base_dir, "mixed_audio.aac")
        audio_path = build_audio_mix_if_needed(events, total_duration_ms, base_dir, audio_path)
        if not audio_path or not os.path.exists(audio_path):
            print("  ❌ Cannot proceed without audio!")
            return None

    print(f"\n🔍 Probing actual audio duration...")
    actual_audio_ms = probe_audio_duration_ms(audio_path)
    if actual_audio_ms is not None:
        print(f"  📊 JSON duration:  {total_duration_ms / 1000:.1f}s")
        print(f"  📊 Audio duration: {actual_audio_ms / 1000:.1f}s")
        if actual_audio_ms > total_duration_ms:
            diff_sec = (actual_audio_ms - total_duration_ms) / 1000.0
            print(f"  🎵 Audio is {diff_sec:.1f}s longer (likely outro music)")
            total_duration_ms = actual_audio_ms
        elif actual_audio_ms < total_duration_ms:
            diff_sec = (total_duration_ms - actual_audio_ms) / 1000.0
            print(f"  ⚠️  Audio is {diff_sec:.1f}s shorter than JSON metadata")
            total_duration_ms = actual_audio_ms
        else:
            print(f"  ✅ Durations match")
    else:
        print(f"  ⚠️  Could not probe audio duration, using JSON value")

    # Apply speed to total duration
    sped_total_duration_ms = apply_speed_to_ms(total_duration_ms, speed)
    print(f"  ⏱️  Final video duration: {sped_total_duration_ms / 1000:.1f}s"
          f"{f' (original {total_duration_ms / 1000:.1f}s @ {speed}x)' if speed != 1.0 else ''}")

    if not output_video:
        speed_suffix = f"_{speed}x" if speed != 1.0 else ""
        output_video = os.path.join(base_dir, f"podcast_video{speed_suffix}.mp4")

    print(f"\n🔧 Building FFmpeg command...")
    cmd_list = build_ffmpeg_command(
        hosts=hosts, events=scaled_events, sections=scaled_sections,
        audio_path=audio_path, ass_path=ass_path,
        total_duration_ms=sped_total_duration_ms,
        output_path=output_video, font_path=font_path,
        title=title, heat_level=heat_level,
        background_image=bg_prepared_path, is_shorts=False,
        speed=speed,
    )

    cmd_path = os.path.join(temp_dir, "ffmpeg_command.txt")
    with open(cmd_path, "w", encoding="utf-8") as f:
        f.write("Command as list:\n")
        for i, part in enumerate(cmd_list):
            f.write(f"  [{i}] {part}\n")
    print(f"  💾 Command saved: {cmd_path}")

    print(f"\n🚀 Rendering video → {output_video}")
    print(f"   This may take a while...")

    result = subprocess.run(cmd_list, capture_output=True, text=True)

    if result.returncode == 0:
        size_mb = os.path.getsize(output_video) / (1024 * 1024)
        print(f"\n{'=' * 60}")
        print(f"✅ Video rendered successfully!")
        print(f"   📁 {output_video}")
        print(f"   📦 {size_mb:.1f} MB")
        if speed != 1.0:
            print(f"   ⚡ Speed: {speed}x")
        print(f"{'=' * 60}")
    else:
        print(f"\n❌ FFmpeg failed (exit code {result.returncode})")
        print(f"   STDERR (last 2000 chars):")
        print(result.stderr[-2000:])

    return output_video


# ══════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Render podcast video from JSON metadata using FFmpeg"
    )
    parser.add_argument("json_path", help="Path to podcast JSON file")
    parser.add_argument("--output", "-o", default="", help="Output video/dir path")
    parser.add_argument("--width", type=int, default=1920, help="Video width (default: 1920, ignored for highlights)")
    parser.add_argument("--height", type=int, default=1080, help="Video height (default: 1080, ignored for highlights)")
    parser.add_argument("--fps", type=int, default=30, help="Frames per second (default: 30)")
    parser.add_argument(
        "--speed", type=float, default=1.0,
        help="Playback speed multiplier (default: 1.0). "
             "Values > 1 speed up, < 1 slow down. "
             "E.g., --speed 1.25 for 25%% faster playback.",
    )
    parser.add_argument(
        "--highlights-only",
        action="store_true", default=False,
        help="Render only highlight clips as vertical shorts (1080x1920, 9:16). "
             "Each highlight becomes a separate video file.",
    )

    args = parser.parse_args()

    global VIDEO_WIDTH, VIDEO_HEIGHT, FPS, SPEED
    VIDEO_WIDTH = args.width
    VIDEO_HEIGHT = args.height
    FPS = args.fps
    SPEED = args.speed

    # Validate speed
    if args.speed <= 0:
        print(f"❌ Speed must be positive, got: {args.speed}")
        sys.exit(1)
    if args.speed < 0.25:
        print(f"⚠️  Very slow speed ({args.speed}x) may produce very long videos")
    if args.speed > 4.0:
        print(f"⚠️  Very fast speed ({args.speed}x) may make audio unintelligible")

    json_path = Path(args.json_path).resolve()

    if not json_path.exists():
        print(f"❌ File not found: {json_path}")
        print(f"   CWD: {Path.cwd()}")
        print(f"   Raw input: {args.json_path}")
        sys.exit(1)

    output = args.output
    if output:
        output = str(Path(output).resolve())

    if args.highlights_only:
        render_highlights_only(str(json_path), output, speed=args.speed)
    else:
        render_podcast_video(str(json_path), output, speed=args.speed)


if __name__ == "__main__":
    main()
