# openpodcast-tts

Turn structured podcast episode JSON into synthesized multi-host audio, then optionally render a **landscape (16:9)** or **vertical shorts (9:16)** video with host cards, subtitles, and overlap-aware layout.

## What it does

1. **TTS pipeline** (`openpodcast`) — Parallel synthesis per dialogue line, timeline with interrupts/overlaps, final mix (MP3/WAV), optional intro/outro music, and a detailed `output.json` report.
2. **Video renderer** (`render`) — Reads the **pipeline output** JSON, builds ASS subtitles and FFmpeg filters, overlays host avatars that light up while speaking.

## Requirements

- **Python 3.12+**
- **FFmpeg** (and **ffprobe**) on your PATH  
  - Needed for final export and for the video renderer (`libx264`, **libass** for subtitles).
- **Pydub** uses FFmpeg under the hood for many formats; ensure FFmpeg is installed so mixing/export behaves reliably.

## Install

From the repo root:

```bash
uv sync
```

Or with pip:

```bash
pip install -e .
```

This installs the console scripts `openpodcast` and `render` (see `pyproject.toml`).

## Credentials and engines

| Engine | CLI / env | Authentication |
|--------|-----------|----------------|
| **hd** (default) | `--engine hd` or `TTS_ENGINE=hd` | Google Cloud Text-to-Speech — set **`GOOGLE_APPLICATION_CREDENTIALS`** to a service account JSON with Text-to-Speech access. |
| **gemini** | `--engine gemini` or `TTS_ENGINE=gemini` | **`GOOGLE_API_KEY`** in `.env` or environment (or `--api-key`). |

Optional: create a `.env` in the working directory; the app loads it via `python-dotenv`.

## Episode JSON (TTS input)

The pipeline expects a JSON file with at least:

- `podcast.hosts` — list of hosts with `id`, `name`, and optional `image`, `voice_config`, etc.
- `podcast.sections` — each section has `dialogues`: list of lines with `id`, `speaker`, `text`, `emotion`, `interrupt_type`, etc.
- Optional on `podcast`: `intro_music`, `outro_music`, `background_image` (paths resolved relative to the JSON file, then CWD, then output dir).

Example shapes live under [`samples/`](samples/).

## Run the TTS pipeline

```bash
uv run openpodcast path/to/episode.json
```

Useful flags:

| Flag | Meaning |
|------|---------|
| `-d`, `--output-dir` | Base output directory (default `./output`) |
| `-o`, `--output` | Final mixed filename (default `openpodcast_episode.mp3`) |
| `-e`, `--engine` | `hd` or `gemini` |
| `-k`, `--api-key` | API key when using Gemini |
| `--dry-run` | Load JSON, print analysis, write `output.json` without synthesis |
| `--retry-failed` | Re-run failed lines from `tts_cache/failed.json`, then remix |

Default engine is **`hd`**, or whatever you set in **`TTS_ENGINE`**.

Artifacts for `my_episode.json` go under:

`output/my_episode/`

including per-line WAVs under `tts/`, cache under `tts_cache/`, the mixed episode audio, and **`output.json`** (timelines, per-dialogue timing, mix path, console log).

## Render video

The renderer expects the **pipeline’s** `output.json` (it uses `hosts`, `sections_timeline`, `mix.output_file`, `summary`, and optional `highlights`).

**Full episode (1920×1080):**

```bash
uv run render output/my_episode/output.json
```

**Vertical highlight clips** — add a top-level **`highlights`** array to `output.json` (each item needs `ids`, `title`, etc.; the TTS pipeline does not copy this from the episode file). Then:

```bash
uv run render output/my_episode/output.json --highlights-only
```

**Playback speed** (audio + subtitle timing):

```bash
uv run render output/my_episode/output.json --speed 1.25
```

The renderer searches common paths for a CJK-friendly font (e.g. Nanum Gothic, Malgun Gothic). For best subtitles, install a suitable **.ttf** and/or place `NanumGothicBold.ttf` where the script can find it.

## Project layout

```
src/openpodcast_tts/
  pipeline.py           # CLI openpodcast, OpenpodcastTTS
  mixer.py              # Timeline + mix
  chirp3_tts.py         # Chirp 3 HD (Cloud TTS)
  gemini_tts.py         # Gemini Flash TTS
  render_podcast_video.py  # CLI render
samples/                # Example episode JSON files
```
