#!/usr/bin/env python3
"""
render_podcast_video.py

Renders a podcast video from JSON metadata + WAV audio files using FFmpeg.
Each host is displayed in a corner, and when they speak, their card glows
and their script text appears as a subtitle. Overlapping speakers are handled
with split/stacked subtitle layouts.

Usage:
    python render_podcast_video.py --json output/jidaenanto_ep01/podcast_data.json

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
    """
    Remove parenthesized stage directions from display text.
    Examples:
        "(잠깐 침묵)" → removed
        "(웃음)" → removed
        "(한숨)" → removed
    Handles both () and （） full-width parentheses.
    """
    # Remove (content) — half-width
    text = re.sub(r'\([^)]*\)', '', text)
    # Remove （content） — full-width
    text = re.sub(r'（[^）]*）', '', text)
    # Clean up leftover double spaces and leading/trailing whitespace
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip()

# ══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════════

VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
FPS = 30
CARD_W = 320
CARD_H = 280  # Taller to fit larger avatar + name + wave inside
MARGIN = 80
BG_COLOR = "#1a1a2e"

# Avatar size: 2.5x the original 70px
AVATAR_SIZE = 175

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
    image_path: str = ""  # original image from JSON
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
    speakers: list  # list of DialogueEvent


@dataclass
class SectionInfo:
    title: str
    start_ms: int
    end_ms: int


# ══════════════════════════════════════════════════════════════
#  FONT RESOLVER
# ══════════════════════════════════════════════════════════════


def find_font(size: int = 28) -> ImageFont.FreeTypeFont:
    """Find an available Korean font."""
    for path in FONT_PATHS:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    print("⚠️  No Korean font found. Using default font.")
    return ImageFont.load_default()


def find_font_path() -> str:
    """Return the first available font file path for FFmpeg/ASS."""
    for path in FONT_PATHS:
        if os.path.exists(path):
            return path
    return "sans-serif"


# ══════════════════════════════════════════════════════════════
#  PARSING — use hosts array from JSON
# ══════════════════════════════════════════════════════════════


def parse_podcast_json(json_path: str):
    """Parse the podcast JSON and extract hosts, events, sections."""
    json_path = Path(json_path).resolve()

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    base_dir = str(json_path.parent)

    # ── Hosts — prefer "hosts" array, fallback to analysis ──
    hosts: dict[str, HostInfo] = {}

    hosts_array = data.get("hosts", [])
    if hosts_array:
        for h in hosts_array:
            key = h["id"]
            c = HOST_COLORS.get(key, HOST_COLORS["host_1"])

            # Resolve image path
            image_path = h.get("image", "")
            if image_path:
                image_path = image_path.replace("\\", os.sep)
                if not os.path.isabs(image_path):
                    # Try relative to JSON dir
                    candidate = os.path.join(base_dir, os.path.basename(image_path))
                    if os.path.exists(candidate):
                        image_path = candidate
                    else:
                        candidate = str(Path(json_path).parent.parent / image_path)
                        if os.path.exists(candidate):
                            image_path = candidate
                        else:
                            # Try from project root
                            candidate = str(Path.cwd() / image_path.replace("\\", os.sep))
                            if os.path.exists(candidate):
                                image_path = candidate

            hosts[key] = HostInfo(
                key=key,
                name=h["name"],
                color_hex=c["hex"],
                color_rgb=c["rgb"],
                image_path=image_path,
            )
            print(f"    👤 {h['name']} ({key}): image={'✅' if os.path.exists(image_path) else '❌'} {image_path}")
    else:
        # Fallback: from analysis.dialogue_distribution
        dist = data.get("analysis", {}).get("dialogue_distribution", {})
        for key, info in dist.items():
            c = HOST_COLORS.get(key, HOST_COLORS["host_1"])
            hosts[key] = HostInfo(
                key=key,
                name=info["name"],
                color_hex=c["hex"],
                color_rgb=c["rgb"],
            )

    # Fallback: from dialogues
    if not hosts:
        for d in data.get("dialogues_timeline", []):
            key = d["speaker"]
            if key not in hosts:
                c = HOST_COLORS.get(key, HOST_COLORS["host_1"])
                hosts[key] = HostInfo(
                    key=key,
                    name=d["name"],
                    color_hex=c["hex"],
                    color_rgb=c["rgb"],
                )

    # ── Section map ──
    sections: list[SectionInfo] = []
    section_map: dict[int, str] = {}
    for sec in data.get("sections_timeline", []):
        sections.append(SectionInfo(
            title=sec["section_title"],
            start_ms=sec["audio_start_ms"],
            end_ms=sec["audio_end_ms"],
        ))
        for script in sec.get("scripts", []):
            section_map[script["dialogue_id"]] = sec["section_title"]

    # ── Dialogue events ──
    events: list[DialogueEvent] = []
    for d in data.get("dialogues_timeline", []):
        events.append(DialogueEvent(
            dialogue_id=d["dialogue_id"],
            index=d.get("index", 0),
            speaker=d["speaker"],
            name=d["name"],
            text=d["text"],
            emotion=d.get("emotion", ""),
            interrupt_type=d.get("interrupt_type", "none"),
            start_ms=d["audio_start_ms"],
            end_ms=d["audio_end_ms"],
            duration_ms=d["audio_duration_ms"],
            section_title=section_map.get(d["dialogue_id"], ""),
            markers=d.get("markers", {}),
            overlap_ms=d.get("audio_overlap_ms", 0),
        ))

    events.sort(key=lambda e: (e.start_ms, e.dialogue_id))

    # ── Audio path ──
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

    return data, hosts, events, sections, mix_path, total_duration_ms, base_dir


# ══════════════════════════════════════════════════════════════
#  OVERLAP DETECTION
# ══════════════════════════════════════════════════════════════


def detect_overlaps(events: list[DialogueEvent]) -> list[OverlapZone]:
    """Detect all time zones where multiple speakers overlap."""
    if not events:
        return []

    # Collect all boundary timestamps
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

        # Which events are active in [t_start, t_end)?
        mid = (t_start + t_end) / 2
        active = [e for e in events if e.start_ms <= mid < e.end_ms]

        if len(active) >= 2:
            zones.append(OverlapZone(
                start_ms=t_start,
                end_ms=t_end,
                duration_ms=t_end - t_start,
                speakers=active,
            ))

    # Merge contiguous zones with same speaker set
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


def get_active_events_at(events: list[DialogueEvent], time_ms: float) -> list[DialogueEvent]:
    """Return all events active at a given timestamp."""
    return [e for e in events if e.start_ms <= time_ms < e.end_ms]


# ══════════════════════════════════════════════════════════════
#  HOST CARD POSITIONS
# ══════════════════════════════════════════════════════════════


def calculate_positions(hosts: dict[str, HostInfo]):
    """Assign corner positions based on host count."""
    n = len(hosts)

    TOP_OFFSET = VIDEO_HEIGHT * 0.1

    corners = {
        1: [
            (MARGIN, MARGIN + TOP_OFFSET),
        ],
        2: [
            (MARGIN, MARGIN + TOP_OFFSET),
            (VIDEO_WIDTH - MARGIN - CARD_W, MARGIN + TOP_OFFSET),
        ],
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
#  HOST CARD GENERATION — uses host image if available
# ══════════════════════════════════════════════════════════════


def make_circle_mask(size: int) -> Image.Image:
    """Create a circular alpha mask."""
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse([(0, 0), (size - 1, size - 1)], fill=255)
    return mask


def load_host_avatar(image_path: str, size: int = AVATAR_SIZE) -> Optional[Image.Image]:
    """Load and crop host image into a circle."""
    if not image_path or not os.path.exists(image_path):
        return None

    try:
        img = Image.open(image_path).convert("RGBA")

        # Crop to square from center
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))

        # Resize to target
        img = img.resize((size, size), Image.LANCZOS)

        # Apply circular mask
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
):
    """
    Generate a PNG host card with photo avatar or initial letter.
    
    Layout (all inside the card box):
      - Large circular avatar (175px, 2.5x original)
      - Host name below avatar (no overlap)
      - Sound wave bars below name (active only, inside card)
    """
    w, h = size
    pad = 30 if active else 0
    canvas_w = w + pad * 2
    canvas_h = h + pad * 2

    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    if active:
        glow = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.rounded_rectangle(
            [(pad - 10, pad - 10), (pad + w + 10, pad + h + 10)],
            radius=25,
            fill=(*host.color_rgb, 90),
        )
        glow = glow.filter(ImageFilter.GaussianBlur(radius=18))
        img = Image.alpha_composite(img, glow)

    draw = ImageDraw.Draw(img)

    # Card background
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

    # ── Layout calculations ──
    # Vertical spacing inside card:
    #   top_padding -> avatar -> gap -> name -> gap -> wave (active) -> bottom_padding
    avatar_size = AVATAR_SIZE
    r = avatar_size // 2

    top_padding = 20
    avatar_name_gap = 12
    name_wave_gap = 10
    wave_height = 24  # max bar height for wave

    # Center X of the card content area
    cx = pad + w // 2

    # Avatar center Y
    avatar_cy = pad + top_padding + r

    # Name Y (top of text, anchored at middle-center)
    font_name = find_font(24)
    name_y = avatar_cy + r + avatar_name_gap

    # Wave base Y (active only)
    # Measure name text height for proper positioning
    name_bbox = font_name.getbbox(host.name)
    name_text_h = name_bbox[3] - name_bbox[1] if name_bbox else 24
    wave_base_y = name_y + name_text_h + name_wave_gap + wave_height

    # ── Draw avatar ──
    avatar_img = load_host_avatar(host.image_path, avatar_size)

    if avatar_img:
        # Paste photo avatar
        ax = cx - r
        ay = avatar_cy - r
        img.paste(avatar_img, (ax, ay), avatar_img)

        # Draw circle border around photo
        draw = ImageDraw.Draw(img)  # refresh draw after paste
        border_col = host.color_rgb + (255,) if active else (255, 255, 255, 120)
        draw.ellipse(
            [(cx - r - 2, avatar_cy - r - 2), (cx + r + 2, avatar_cy + r + 2)],
            outline=border_col,
            width=3 if active else 2,
        )
    else:
        # Fallback: colored circle with initial letter
        avatar_fill = host.color_rgb + (255,) if active else host.color_rgb + (160,)
        draw.ellipse(
            [(cx - r, avatar_cy - r), (cx + r, avatar_cy + r)],
            fill=avatar_fill,
            outline=(255, 255, 255, 200) if active else (255, 255, 255, 80),
            width=2,
        )
        font_initial = find_font(72)
        draw.text((cx, avatar_cy), host.name[0], fill="white", font=font_initial, anchor="mm")

    # ── Draw host name (below avatar, no overlap) ──
    name_color = "white" if active else (200, 200, 200, 200)
    # anchor "mt" = middle-top, so name_y is the top of the text
    draw.text(
        (cx, name_y),
        host.name,
        fill=name_color,
        font=font_name,
        anchor="mt",
    )

    # ── Sound wave bars (active only, inside card, below name) ──
    if active:
        bar_w = 5
        bar_gap = 4
        num_bars = 9
        total_bar_w = num_bars * bar_w + (num_bars - 1) * bar_gap
        bar_start_x = cx - total_bar_w // 2

        for i in range(num_bars):
            bar_h = 8 + int(16 * abs(math.sin(i * 0.8)))
            bx = bar_start_x + i * (bar_w + bar_gap)
            draw.rounded_rectangle(
                [(bx, wave_base_y - bar_h), (bx + bar_w, wave_base_y)],
                radius=2,
                fill=host.color_rgb + (200,),
            )

    img.save(output_path, "PNG")
    return output_path




def generate_all_host_cards(hosts: dict[str, HostInfo], temp_dir: str):
    """Generate normal + active cards for every host."""
    os.makedirs(temp_dir, exist_ok=True)

    for key, host in hosts.items():
        normal_path = os.path.join(temp_dir, f"{key}_normal.png")
        active_path = os.path.join(temp_dir, f"{key}_active.png")

        generate_host_card(host, active=False, output_path=normal_path)
        generate_host_card(host, active=True, output_path=active_path)

        host.card_normal_path = normal_path
        host.card_active_path = active_path

        print(f"  🎨 {host.name}: {normal_path}, {active_path}")


# ══════════════════════════════════════════════════════════════
#  ASS SUBTITLE GENERATION (with overlap handling)
# ══════════════════════════════════════════════════════════════


def ms_to_ass(ms: int) -> str:
    """Convert milliseconds to ASS time format H:MM:SS.cc"""
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
    """Convert RGB to ASS color format &HAABBGGRR"""
    return f"&H{a:02X}{b:02X}{g:02X}{r:02X}"


def escape_ass_text(text: str) -> str:
    """Escape special characters for ASS subtitle format.
    Preserves \\N line breaks."""
    # Temporarily replace our line breaks
    text = text.replace("\\N", "\x00LINEBREAK\x00")
    text = text.replace("\\", "\\\\")
    text = text.replace("{", "\\{")
    text = text.replace("}", "\\}")
    # Restore line breaks
    text = text.replace("\x00LINEBREAK\x00", "\\N")
    return text


def get_marker_text(markers: dict) -> str:
    icons = []
    if markers.get("is_hook"):
        icons.append("🎣")
    if markers.get("is_key_point"):
        icons.append("💡")
    if markers.get("triggers_conflict"):
        icons.append("⚡")
    if markers.get("is_funny"):
        icons.append("😂")
    return " ".join(icons)


def get_interrupt_label(itype: str) -> str:
    labels = {
        "cut_in": "끼어들기",
        "overlap": "동시발언",
        "piggyback": "이어받기",
        "challenge": "반박",
        "support": "동조",
        "redirect": "화제전환",
    }
    return labels.get(itype, "")

# ══════════════════════════════════════════════════════════════
#  BACKGROUND IMAGE HANDLING
# ══════════════════════════════════════════════════════════════


def resolve_background_image(bg_value: str, base_dir: str) -> Optional[str]:
    """
    Resolve background_image from JSON.
    Could be:
      - A file path (relative or absolute)
      - A name that matches a file in the output dir
      - None/empty
    """
    if not bg_value:
        return None

    # Common image extensions to try
    extensions = ["", ".png", ".jpg", ".jpeg", ".webp"]

    # Search locations
    search_dirs = [
        base_dir,
        os.path.join(base_dir, ".."),
        str(Path.cwd()),
        str(Path.cwd() / "assets"),
        str(Path.cwd() / "images"),
    ]

    for search_dir in search_dirs:
        for ext in extensions:
            candidate = os.path.join(search_dir, bg_value + ext)
            candidate = candidate.replace("\\", os.sep)
            if os.path.exists(candidate):
                return str(Path(candidate).resolve())

    # Try as direct path
    direct = bg_value.replace("\\", os.sep)
    if os.path.exists(direct):
        return str(Path(direct).resolve())

    return None


def prepare_background_image(
    image_path: str,
    output_path: str,
    width: int = VIDEO_WIDTH,
    height: int = VIDEO_HEIGHT,
    darken: float = 0.4,
) -> str:
    """
    Resize and darken background image to fit video dimensions.
    Returns path to the prepared image.
    """
    try:
        img = Image.open(image_path).convert("RGBA")

        # Resize to cover (maintain aspect ratio, crop to fill)
        img_ratio = img.width / img.height
        target_ratio = width / height

        if img_ratio > target_ratio:
            # Image is wider — fit height, crop width
            new_h = height
            new_w = int(height * img_ratio)
        else:
            # Image is taller — fit width, crop height
            new_w = width
            new_h = int(width / img_ratio)

        img = img.resize((new_w, new_h), Image.LANCZOS)

        # Center crop
        left = (new_w - width) // 2
        top = (new_h - height) // 2
        img = img.crop((left, top, left + width, top + height))

        # Darken overlay for readability
        dark_overlay = Image.new("RGBA", (width, height), (0, 0, 0, int(255 * darken)))
        img = Image.alpha_composite(img, dark_overlay)

        # Convert to RGB (FFmpeg input)
        img = img.convert("RGB")
        img.save(output_path, "PNG")

        return output_path

    except Exception as e:
        print(f"    ⚠️  Failed to prepare background: {e}")
        return ""

# ══════════════════════════════════════════════════════════════
#  ASS SUBTITLES — REMOVED SECTION (handled by FFmpeg drawtext)
#  FIXED: speaker name no longer overlaps multi-line text
# ══════════════════════════════════════════════════════════════
def format_subtitle_text(text: str, max_lines: int = 3, chars_per_line: int = 50) -> str:
    """
    Break text into lines and cap at max_lines.
    Uses char count to estimate line breaks since ASS wraps automatically
    but we want to control it explicitly.
    """
    if not text:
        return text

    # Split into chunks of chars_per_line
    lines = []
    while text:
        if len(text) <= chars_per_line:
            lines.append(text)
            break
        # Find a good break point (space or punctuation)
        break_at = chars_per_line
        # Look for space/punctuation near the limit
        for i in range(chars_per_line, max(chars_per_line - 10, 0), -1):
            if i < len(text) and text[i] in " ,，.。!！?？、":
                break_at = i + 1
                break
        lines.append(text[:break_at].rstrip())
        text = text[break_at:].lstrip()

    # Cap at max_lines
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        # Add ellipsis to last line
        lines[-1] = lines[-1].rstrip()
        if not lines[-1].endswith("..."):
            lines[-1] += "..."

    return "\\N".join(lines)

def generate_ass_subtitles(
    events: list[DialogueEvent],
    hosts: dict[str, HostInfo],
    output_path: str,
    font_path: str,
):
    """
    Generate ASS subtitle file.
    Subtitle text only — no speaker name displayed.
    """

    font_name = Path(font_path).stem if os.path.exists(font_path) else "Arial"

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
                if e.dialogue_id != event.dialogue_id
                and e.start_ms <= mid < e.end_ms
            ]

            all_active = [event] + concurrent
            all_active.sort(key=lambda e: (e.start_ms, e.dialogue_id))
            pos_idx = next(
                i for i, e in enumerate(all_active)
                if e.dialogue_id == event.dialogue_id
            )

            segments.append(SubSegment(
                start_ms=seg_start,
                end_ms=seg_end,
                event=event,
                concurrent=concurrent,
                position_index=pos_idx,
                total_concurrent=len(all_active),
            ))

        return segments

    all_segments: list[SubSegment] = []
    for event in events:
        all_segments.extend(compute_segments(event))
    all_segments.sort(key=lambda s: (s.start_ms, s.position_index))

    white = rgb_to_ass_color(255, 255, 255)

    solo_text_v = 80
    overlap_text_v = 80
    stack_text_margins = [80, 160, 240, 320]

    header = f"""[Script Info]
Title: Podcast Video Subtitles
ScriptType: v4.00+
PlayResX: {VIDEO_WIDTH}
PlayResY: {VIDEO_HEIGHT}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Solo,{font_name},42,{white},&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,4,3,2,2,100,100,{solo_text_v},1
Style: OverlapL,{font_name},34,{white},&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,4,2,1,1,100,{VIDEO_WIDTH // 2 + 20},{overlap_text_v},1
Style: OverlapR,{font_name},34,{white},&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,4,2,1,3,{VIDEO_WIDTH // 2 + 20},100,{overlap_text_v},1
Style: Stack0,{font_name},30,{white},&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,4,2,1,2,100,100,{stack_text_margins[0]},1
Style: Stack1,{font_name},30,{white},&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,4,2,1,2,100,100,{stack_text_margins[1]},1
Style: Stack2,{font_name},30,{white},&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,4,2,1,2,100,100,{stack_text_margins[2]},1
Style: Stack3,{font_name},30,{white},&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,4,2,1,2,100,100,{stack_text_margins[3]},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    dialogue_lines = []

    for seg in all_segments:
        evt = seg.event
        start = ms_to_ass(seg.start_ms)
        end = ms_to_ass(seg.end_ms)

        host_info = hosts.get(evt.speaker)
        if host_info:
            ass_color = rgb_to_ass_color(*host_info.color_rgb)
        else:
            ass_color = white

        # Clean stage directions
        cleaned_text = clean_display_text(evt.text)

        if not cleaned_text.strip():
            continue

        # Format with line breaks and max line cap
        if seg.total_concurrent == 1:
            # Solo: 3 lines, ~30 chars each
            display_text = format_subtitle_text(cleaned_text)
            display_text = escape_ass_text(display_text)

            dialogue_lines.append(
                f"Dialogue: 0,{start},{end},Solo,,0,0,0,,"
                f"{{\\c{ass_color}}}{display_text}"
            )

        elif seg.total_concurrent == 2:
            # Overlap: 3 lines, ~20 chars each (narrower)
            display_text = format_subtitle_text(cleaned_text)
            display_text = escape_ass_text(display_text)

            style = "OverlapL" if seg.position_index == 0 else "OverlapR"
            dialogue_lines.append(
                f"Dialogue: 0,{start},{end},{style},,0,0,0,,"
                f"{{\\c{ass_color}}}{display_text}"
            )

        else:
            # Stacked: 2 lines, ~25 chars each
            display_text = format_subtitle_text(cleaned_text)
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

    print(f"  📝 ASS subtitles: {output_path}")
    print(f"     {len(dialogue_lines)} dialogue lines")
    return output_path



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
):
    """
    Build FFmpeg command.
    Uses background image if provided, otherwise solid color.
    """

    total_duration_sec = total_duration_ms / 1000.0

    # ── Collect inputs ──
    inputs = []
    input_map = {}
    idx = 0

    # [0] Audio
    inputs.append(audio_path)
    audio_idx = idx
    idx += 1

    # [1] Background image (optional)
    bg_input_idx = None
    if background_image and os.path.exists(background_image):
        inputs.append(background_image)
        bg_input_idx = idx
        idx += 1

    # Host card images
    for key, host in hosts.items():
        inputs.append(host.card_normal_path)
        input_map[f"{key}_normal"] = idx
        idx += 1

        inputs.append(host.card_active_path)
        input_map[f"{key}_active"] = idx
        idx += 1

    # ── Font path for drawtext ──
    if font_path and os.path.exists(font_path):
        ff_font = font_path.replace("\\", "/")
        if len(ff_font) >= 2 and ff_font[1] == ":":
            ff_font = ff_font[0] + "\\:" + ff_font[2:]
        fontfile_opt = f":fontfile='{ff_font}'"
    else:
        fontfile_opt = ""

    def dt_escape(text: str) -> str:
        return (
            text
            .replace("\\", "\\\\")
            .replace("'", "\u2019")
            .replace(":", "\\:")
            .replace("%", "%%")
        )

    # ── Build filter_complex ──
    filters = []

    # Background — image or solid color
    if bg_input_idx is not None:
        # Loop the image for the duration, scale to exact size
        filters.append(
            f"[{bg_input_idx}:v]loop=loop=-1:size=1"
            f",setpts=N/{FPS}/TB"
            f",scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}"
            f",trim=duration={total_duration_sec:.3f}"
            f",setpts=PTS-STARTPTS"
            f",fps={FPS}"
            f"[bg]"
        )
    else:
        # Solid color fallback
        filters.append(
            f"color=c={BG_COLOR}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}"
            f":d={total_duration_sec:.3f}:r={FPS}"
            f"[bg]"
        )

    # Main title — y=20
    safe_title = dt_escape(title)
    filters.append(
        f"[bg]drawtext=text='{safe_title}'"
        f":fontsize=50:fontcolor=white"
        f":x=(w-text_w)/2:y=20"
        f"{fontfile_opt}"
        f":shadowcolor=black:shadowx=2:shadowy=2"
        f"[bg_t]"
    )

    current_label = "bg_t"

    # Section titles — y=65
    for i, sec in enumerate(sections):
        s = sec.start_ms / 1000.0
        e = sec.end_ms / 1000.0
        safe_sec = dt_escape(sec.title)
        next_label = f"sec{i}"
        filters.append(
            f"[{current_label}]drawtext=text='{safe_sec}'"
            f":fontsize=40:fontcolor=#00CCFF"
            f":x=(w-text_w)/2:y=90"
            f"{fontfile_opt}"
            f":shadowcolor=black:shadowx=1:shadowy=1"
            f":enable='between(t\\,{s:.3f}\\,{e:.3f})'"
            f"[{next_label}]"
        )
        current_label = next_label

    # ── Overlay host cards ──
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
            f"x={x}:y={y}"
            f":enable='{not_active_enable}'"
            f"[{next_label}]"
        )
        current_label = next_label

        glow_pad = 30
        next_label = f"a{key[-1]}"
        filters.append(
            f"[{current_label}][{active_idx}:v]overlay="
            f"x={x - glow_pad}:y={y - glow_pad}"
            f":enable='{active_enable}'"
            f"[{next_label}]"
        )
        current_label = next_label

    # ── ASS subtitles ──
    ass_ffmpeg = ass_path.replace("\\", "/")
    if len(ass_ffmpeg) >= 2 and ass_ffmpeg[1] == ":":
        ass_ffmpeg = ass_ffmpeg[0] + "\\:" + ass_ffmpeg[2:]

    final_label = "outv"
    filters.append(
        f"[{current_label}]ass='{ass_ffmpeg}'"
        f"[{final_label}]"
    )

    filter_str = ";".join(filters)

    # ── Command as list ──
    cmd_list = ["ffmpeg", "-y"]

    for inp in inputs:
        cmd_list.extend(["-i", inp])

    cmd_list.extend([
        "-filter_complex", filter_str,
        "-map", f"[{final_label}]",
        "-map", f"{audio_idx}:a",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        "-movflags", "+faststart",
        output_path,
    ])

    return cmd_list




# ══════════════════════════════════════════════════════════════
#  ALTERNATIVE: CONCAT INDIVIDUAL WAV → SINGLE AUDIO
# ══════════════════════════════════════════════════════════════


def build_audio_mix_if_needed(
    events: list[DialogueEvent],
    total_duration_ms: int,
    base_dir: str,
    output_path: str,
) -> str:
    """
    If the mixed MP3 doesn't exist, build it from individual WAV files
    using FFmpeg amerge/amix with correct timing.
    """
    if os.path.exists(output_path):
        print(f"  🎧 Using existing audio mix: {output_path}")
        return output_path

    print(f"  🎧 Building audio mix from {len(events)} WAV files...")

    # Use FFmpeg adelay to position each WAV at its start_ms
    inputs = []
    filter_parts = []

    for i, evt in enumerate(events):
        wav_path = evt.__dict__.get("output_path", "")
        if not wav_path:
            # Try to reconstruct from dialogue_id
            wav_path = os.path.join(base_dir, f"d_{evt.dialogue_id:04d}.wav")

        if not os.path.exists(wav_path):
            print(f"    ⚠️  Missing WAV: {wav_path}")
            continue

        inputs.append(f'-i "{wav_path}"')
        delay_ms = evt.start_ms
        filter_parts.append(
            f"[{i}:a]adelay={delay_ms}|{delay_ms}[a{i}]"
        )

    if not inputs:
        print("    ❌ No WAV files found!")
        return ""

    # Mix all delayed streams
    mix_inputs = "".join(f"[a{i}]" for i in range(len(inputs)))
    filter_parts.append(
        f"{mix_inputs}amix=inputs={len(inputs)}:duration=longest[aout]"
    )

    filter_str = ";".join(filter_parts)
    cmd = (
        f'ffmpeg -y {" ".join(inputs)} '
        f'-filter_complex "{filter_str}" '
        f'-map "[aout]" -c:a aac -b:a 192k '
        f'"{output_path}"'
    )

    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    ❌ Audio mix failed:\n{result.stderr[:500]}")
    else:
        print(f"    ✅ Audio mix created: {output_path}")

    return output_path


# ══════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ══════════════════════════════════════════════════════════════


def render_podcast_video(json_path: str, output_video: str = ""):
    """Main entry point: JSON → Video."""

    print("=" * 60)
    print("🎬 Podcast Video Renderer (FFmpeg)")
    print("=" * 60)

    # ── 1. Parse ──
    print("\n📂 Parsing JSON...")
    data, hosts, events, sections, audio_path, total_duration_ms, base_dir = \
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
    print(f"  ⏱️  Duration: {total_duration_ms / 1000:.1f}s")

    # ── 2. Detect overlaps ──
    print("\n🔍 Detecting overlaps...")
    overlaps = detect_overlaps(events)
    if overlaps:
        print(f"  ⚡ Found {len(overlaps)} overlap zones:")
        for ov in overlaps:
            speakers = ", ".join(e.name for e in ov.speakers)
            print(f"     {ov.start_ms}ms → {ov.end_ms}ms ({ov.duration_ms}ms) — {speakers}")
    else:
        print("  ✅ No overlaps detected")

    # ── 3. Calculate positions ──
    print("\n📐 Calculating host positions...")
    calculate_positions(hosts)
    for key, host in hosts.items():
        print(f"  {host.name}: position={host.position}")

    # ── 4. Generate host cards ──
    temp_dir = os.path.join(base_dir, "_temp_render")
    print(f"\n🎨 Generating host cards → {temp_dir}")
    generate_all_host_cards(hosts, temp_dir)

    # ── 5. Prepare background image ──
    bg_image_value = podcast_info.get("background_image", "")
    bg_prepared_path = ""

    if bg_image_value:
        print(f"\n🖼️  Background image: {bg_image_value}")
        bg_source = resolve_background_image(bg_image_value, base_dir)

        if bg_source:
            print(f"  📁 Found: {bg_source}")
            bg_prepared_path = os.path.join(temp_dir, "background.png")
            bg_prepared_path = prepare_background_image(
                bg_source,
                bg_prepared_path,
                VIDEO_WIDTH,
                VIDEO_HEIGHT,
                darken=0.4,
            )
            if bg_prepared_path:
                print(f"  ✅ Prepared: {bg_prepared_path}")
            else:
                print(f"  ⚠️  Failed to prepare, using solid color")
        else:
            print(f"  ⚠️  Not found, using solid color fallback")

    # ── 6. Generate ASS subtitles ──
    font_path = find_font_path()
    ass_path = os.path.join(temp_dir, "subtitles.ass")
    print(f"\n📝 Generating ASS subtitles...")
    generate_ass_subtitles(events, hosts, ass_path, font_path)

    # ── 7. Ensure audio exists ──
    print(f"\n🎧 Audio: {audio_path}")
    if not audio_path or not os.path.exists(audio_path):
        print("  ⚠️  Mixed audio not found, attempting to build from WAVs...")
        audio_path = os.path.join(base_dir, "mixed_audio.aac")
        audio_path = build_audio_mix_if_needed(
            events, total_duration_ms, base_dir, audio_path
        )
        if not audio_path or not os.path.exists(audio_path):
            print("  ❌ Cannot proceed without audio!")
            return None

    # ── 8. Build FFmpeg command ──
    if not output_video:
        output_video = os.path.join(base_dir, "podcast_video.mp4")

    print(f"\n🔧 Building FFmpeg command...")
    cmd_list = build_ffmpeg_command(
        hosts=hosts,
        events=events,
        sections=sections,
        audio_path=audio_path,
        ass_path=ass_path,
        total_duration_ms=total_duration_ms,
        output_path=output_video,
        font_path=font_path,
        title=title,
        heat_level=heat_level,
        background_image=bg_prepared_path,
    )

    # Save command for debugging
    cmd_path = os.path.join(temp_dir, "ffmpeg_command.txt")
    with open(cmd_path, "w", encoding="utf-8") as f:
        f.write("Command as list:\n")
        for i, part in enumerate(cmd_list):
            f.write(f"  [{i}] {part}\n")
    print(f"  💾 Command saved: {cmd_path}")

    # ── 9. Execute FFmpeg ──
    print(f"\n🚀 Rendering video → {output_video}")
    print(f"   This may take a while...")

    result = subprocess.run(
        cmd_list,
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        size_mb = os.path.getsize(output_video) / (1024 * 1024)
        print(f"\n{'=' * 60}")
        print(f"✅ Video rendered successfully!")
        print(f"   📁 {output_video}")
        print(f"   📦 {size_mb:.1f} MB")
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
    """CLI entry point for the render command."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Render podcast video from JSON metadata using FFmpeg"
    )
    parser.add_argument(
        "json_path",
        help="Path to podcast JSON file",
    )
    parser.add_argument(
        "--output", "-o",
        default="",
        help="Output video path (default: same dir as JSON)",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=1920,
        help="Video width (default: 1920)",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=1080,
        help="Video height (default: 1080)",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=30,
        help="Frames per second (default: 30)",
    )

    args = parser.parse_args()

    # Update globals if custom size
    global VIDEO_WIDTH, VIDEO_HEIGHT, FPS
    VIDEO_WIDTH = args.width
    VIDEO_HEIGHT = args.height
    FPS = args.fps

    # Resolve to absolute path from current working directory
    json_path = Path(args.json_path).resolve()

    if not json_path.exists():
        print(f"❌ File not found: {json_path}")
        print(f"   CWD: {Path.cwd()}")
        print(f"   Raw input: {args.json_path}")
        sys.exit(1)

    output = args.output
    if output:
        output = str(Path(output).resolve())

    render_podcast_video(str(json_path), output)


if __name__ == "__main__":
    main()
