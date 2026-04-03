import warnings
warnings.filterwarnings("ignore", category=SyntaxWarning, module="pydub")

import json
import os
import time
import sys
import io
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from .gemini_tts import GeminiTTSClient, AudioCache
from .mixer import AudioMixer

EMOJI = {"host_1": "🔵", "host_2": "🔴", "host_3": "🟢", "host_4": "🟡"}

# Engine labels for display
ENGINE_LABELS = {
    "gemini": "Gemini Flash TTS",
    "hd": "Chirp 3 HD",
}


def create_tts_client(hosts, api_key=None, tts="hd", cache_dir="./tts_cache"):
    """Create the appropriate TTS client based on engine setting"""
    if tts == "hd":
        from .chirp3_tts import Chirp3HDClient
        return Chirp3HDClient(
            hosts=hosts,
            cache_dir=cache_dir,
        )
    else:
        return GeminiTTSClient(
            hosts=hosts,
            api_key=api_key,
            cache_dir=cache_dir,
            quality="standard",
        )


def _fmt_ms(ms: int) -> str:
    """Format milliseconds as mm:ss.mmm for readable display."""
    total_sec = ms / 1000
    minutes = int(total_sec // 60)
    seconds = total_sec % 60
    return f"{minutes:02d}:{seconds:06.3f}"


def _fmt_ms_hms(ms: int) -> str:
    """Format milliseconds as hh:mm:ss for chapter/section markers."""
    total_sec = int(ms // 1000)
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    seconds = total_sec % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class ConsoleCapturer:
    """Captures all console output while still printing to stdout."""

    def __init__(self):
        self.lines = []

    def capture_print(self, *args, **kwargs):
        """Drop-in replacement for print() that also records."""
        output = io.StringIO()
        print(*args, file=output, **kwargs)
        text = output.getvalue()
        self.lines.append(text.rstrip("\n"))
        # Still print to real stdout
        sys.stdout.write(text)
        sys.stdout.flush()

    def get_all(self) -> list[str]:
        return list(self.lines)


class OpenpodcastTTS:
    def __init__(
        self,
        json_path: str,
        output_dir: str = "./output",
        api_key: str | None = None,
        tts: str = "hd",
    ):
        json_path_base_name = Path(json_path).stem
        self.json_dir = Path(json_path).parent
        self.output_dir = Path(output_dir) / json_path_base_name
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.tts_dir = self.output_dir / "tts"
        self.tts_dir.mkdir(parents=True, exist_ok=True)

        with open(json_path, "r", encoding="utf-8") as f:
            self.data = json.load(f)

        self.podcast = self.data["podcast"]
        self.hosts = self.podcast.get("hosts", [])
        if not self.hosts:
            raise ValueError("No 'podcast.hosts' information found in JSON.")

        self.all_dialogues = []
        for section in self.podcast["sections"]:
            for d in section["dialogues"]:
                self.all_dialogues.append(d)

        self.tts = tts
        print(f"Initialized OpenpodcastTTS with engine: {ENGINE_LABELS.get(tts, tts)}")

        # Resolve intro/outro music and background image paths relative to JSON file location
        self.intro_music_path = self._resolve_asset_path(
            self.podcast.get("intro_music")
        )
        self.outro_music_path = self._resolve_asset_path(
            self.podcast.get("outro_music")
        )
        self.background_image_path = self._resolve_asset_path(
            self.podcast.get("background_image")
        )

        # Use different client based on tts engine
        self.tts = create_tts_client(
            hosts=self.hosts,
            api_key=api_key,
            tts=tts,
            cache_dir=os.path.join(self.output_dir, "tts_cache")
        )
        self.mixer = AudioMixer(output_dir=self.output_dir)

        # Console capturer & output report
        self.console = ConsoleCapturer()
        self.output_report = {
            "podcast": {
                "show_name": self.podcast.get("show_name", ""),
                "title": self.podcast.get("title", ""),
                "heat_level": self.podcast.get("heat_level", ""),
                "background_image": str(self.background_image_path) if self.background_image_path else None,
                "total_dialogues": len(self.all_dialogues),
                "intro_music": str(self.intro_music_path) if self.intro_music_path else None,
                "outro_music": str(self.outro_music_path) if self.outro_music_path else None,
            },
            "hosts": self.podcast.get("hosts", ""),
            "sections_timeline": [],
            "summary": {},
            "console_output": [],
        }

    def _resolve_asset_path(self, filename: str | None) -> Path | None:
        """Resolve an asset filename to a full path.

        Searches in order:
        1. As an absolute path or relative to CWD
        2. Relative to the JSON file's directory
        3. Relative to the output directory
        """
        if not filename:
            return None

        candidate = Path(filename)
        if candidate.is_absolute() and candidate.exists():
            return candidate

        # Relative to CWD
        if candidate.exists():
            return candidate.resolve()

        # Relative to JSON directory
        json_relative = self.json_dir / filename
        if json_relative.exists():
            return json_relative.resolve()

        # Relative to output directory
        output_relative = self.output_dir / filename
        if output_relative.exists():
            return output_relative.resolve()

        return None

    def _log(self, *args, **kwargs):
        """Print and capture simultaneously."""
        self.console.capture_print(*args, **kwargs)

    def _get_host_info(self, speaker: str) -> dict:
        if isinstance(self.hosts, list):
            for h in self.hosts:
                if h.get("id") == speaker:
                    return h
            return {}
        return self.hosts.get(speaker, {})

    def _find_section_for_dialogue(self, d: dict) -> dict | None:
        """Return the section dict that contains dialogue d."""
        for section in self.podcast["sections"]:
            if d in section["dialogues"]:
                return section
        return None

    def generate_individual_audio(self) -> dict[int, str]:
        script_lines = []
        script_lines.append(f"{self.podcast['show_name']} - {self.podcast['title']}")
        
        engine_label = ENGINE_LABELS.get(self.tts, self.tts)
        self._log(f"\n🎙️  {self.podcast['show_name']} - {self.podcast['title']}")
        self._log(f"🔥 Heat Level: {self.podcast['heat_level']}")
        self._log(f"🎚️  Engine: {engine_label}")
        self._log(f"📝 Total dialogues: {len(self.all_dialogues)}")

        # Log music info
        if self.intro_music_path:
            self._log(f"🎵 Intro music: {self.intro_music_path.name} (overlaps with dialogue id:1)")
        else:
            intro_cfg = self.podcast.get("intro_music")
            if intro_cfg:
                self._log(f"⚠️  Intro music configured as '{intro_cfg}' but file not found")

        if self.outro_music_path:
            self._log(f"🎵 Outro music: {self.outro_music_path.name} (appended at end)")
        else:
            outro_cfg = self.podcast.get("outro_music")
            if outro_cfg:
                self._log(f"⚠️  Outro music configured as '{outro_cfg}' but file not found")

        self._log("=" * 60)

        audio_files: dict[int, str] = {}
        current_section = ""
        success = 0
        failed = 0

        # Track section-level timing
        section_start_time = None
        section_record = None
        section_dialogues_records = []

        pipeline_start = time.time()

        for i, d in enumerate(self.all_dialogues):
            # Detect section change
            for section in self.podcast["sections"]:
                if d in section["dialogues"] and section["section_title"] != current_section:
                    # Close previous section record
                    if section_record is not None:
                        section_end = time.time()
                        section_record["gen_end_time"] = section_end
                        section_record["gen_duration_sec"] = round(section_end - section_record["_start"], 3)
                        section_record["scripts"] = section_dialogues_records
                        del section_record["_start"]
                        self.output_report["sections_timeline"].append(section_record)

                    current_section = section["section_title"]
                    corner = section.get("corner_name", "")
                    self._log(f"\n{'─' * 60}")
                    self._log(f"📌 {corner} {current_section}")
                    self._log(f"   Mood: {section['section_mood']} | Formation: {section['debate_formation']}")
                    self._log(f"{'─' * 60}")

                    script_lines.append(f"section: {current_section} (Mood: {section['section_mood']}")

                    section_start_time = time.time()
                    section_record = {
                        "section_title": current_section,
                        "section_mood": section.get("section_mood", ""),
                        "debate_formation": section.get("debate_formation", ""),
                        "gen_start_time": section_start_time,
                        "_start": section_start_time,
                        "gen_end_time": None,
                        "gen_duration_sec": None,
                        "scripts": [],
                    }
                    section_dialogues_records = []
                    break

            d_id = d["id"]
            speaker = d["speaker"]
            text = d["text"]
            emotion = d["emotion"]
            interrupt = d["interrupt_type"]

            output_path = self.tts_dir / f"d_{d_id:04d}.wav"

            script_start = time.time()

            result = self.tts.synthesize(
                text=text,
                speaker=speaker,
                emotion=emotion,
                output_path=output_path,
            )

            script_end = time.time()
            script_duration = round(script_end - script_start, 3)

            if result:
                audio_files[d_id] = str(output_path)
                status = "✅"
                success += 1
                script_status = "success"
            else:
                status = "❌"
                failed += 1
                script_status = "failed"

            tag = f" [{interrupt}]" if interrupt != "none" else ""
            markers = []
            if d.get("is_key_point"): markers.append("💡")
            if d.get("is_funny"): markers.append("😂")
            if d.get("triggers_conflict"): markers.append("⚡")
            if d.get("is_hook"): markers.append("🎣")
            marker_str = " ".join(markers)

            voice_name = "?"
            if hasattr(self.tts, 'voice_map') and speaker in self.tts.voice_map:
                v = self.tts.voice_map[speaker]
                voice_name = getattr(v, 'short_name', None) or getattr(v, 'voice_name', '?')

            progress = f"[{i+1}/{len(self.all_dialogues)}]"
            log_line = (
                f"  {status} {progress} {EMOJI.get(speaker, '⚪')} "
                f"{d['name']}({voice_name}){tag} "
                f"[{emotion}]: {text} {marker_str}"
            )
            self._log(log_line)

            script_lines.append(f"[{i+1}] {d['name']}: {emotion}: {text} ({marker_str})")

            # Build per-script record (audio positions filled later in build_timeline)
            script_record = {
                "dialogue_id": d_id,
                "index": i + 1,
                "speaker": speaker,
                "name": d.get("name", ""),
                "voice_name": voice_name,
                "emotion": emotion,
                "interrupt_type": interrupt,
                "text": text,
                "text_length": len(text),
                "status": script_status,
                "gen_start_time": script_start,
                "gen_end_time": script_end,
                "gen_duration_sec": script_duration,
                "output_path": str(output_path) if result else None,
                "markers": {
                    "is_key_point": d.get("is_key_point", False),
                    "is_funny": d.get("is_funny", False),
                    "triggers_conflict": d.get("triggers_conflict", False),
                    "is_hook": d.get("is_hook", False),
                },
                # Placeholders — populated by build_timeline
                "audio_start_ms": None,
                "audio_end_ms": None,
                "audio_duration_ms": None,
                "audio_overlap_ms": None,
            }
            section_dialogues_records.append(script_record)

        # Close the last section record
        if section_record is not None:
            section_end = time.time()
            section_record["gen_end_time"] = section_end
            section_record["gen_duration_sec"] = round(section_end - section_record["_start"], 3)
            section_record["scripts"] = section_dialogues_records
            del section_record["_start"]
            self.output_report["sections_timeline"].append(section_record)

        pipeline_end = time.time()

        if hasattr(self.tts, 'save_failed_log'):
            self.tts.save_failed_log()

        self._log(f"\n{'=' * 60}")
        self._log(f"✅ Success: {success} | ❌ Failed: {failed} | Total: {len(self.all_dialogues)}")

        if failed > 0:
            self._log(f"💡 Retry failures: uv run openpodcast <json> --retry-failed")

        # Update summary
        self.output_report["summary"]["success"] = success
        self.output_report["summary"]["failed"] = failed
        self.output_report["summary"]["total"] = len(self.all_dialogues)
        self.output_report["summary"]["pipeline_start_time"] = pipeline_start
        self.output_report["summary"]["pipeline_end_time"] = pipeline_end
        self.output_report["summary"]["pipeline_duration_sec"] = round(pipeline_end - pipeline_start, 3)

        self.output_report["script_lines"] = script_lines

        return audio_files

    def retry_failed(self) -> dict[int, str]:
        failed_path = self.output_dir / "tts_cache" / "failed.json"
        if not failed_path.exists():
            self._log("✅ No failure log found. All succeeded!")
            return {}

        with open(failed_path, "r", encoding="utf-8") as f:
            failed_items = json.load(f)

        self._log(f"\n🔄 Retrying failures: {len(failed_items)} entries")
        self._log("=" * 60)

        audio_files: dict[int, str] = {}
        still_failed = []

        for item in failed_items:
            retry_start = time.time()
            result = self.tts.synthesize(
                text=item["text"],
                speaker=item["speaker"],
                emotion=item["emotion"],
                output_path=item["output_path"],
            )
            retry_end = time.time()

            if result:
                fname = Path(item["output_path"]).stem
                d_id = int(fname.split("_")[1])
                audio_files[d_id] = item["output_path"]
                self._log(f"  ✅ Recovery succeeded: {fname} ({round(retry_end - retry_start, 3)}s)")
            else:
                still_failed.append(item)
                self._log(f"  ❌ Still failing: {item['text'][:50]}... ({round(retry_end - retry_start, 3)}s)")

        if still_failed:
            with open(failed_path, "w", encoding="utf-8") as f:
                json.dump(still_failed, f, ensure_ascii=False, indent=2)
            self._log(f"\n❌ Still failing: {len(still_failed)} entries")
        else:
            failed_path.unlink()
            self._log("\n🎉 All failures recovered!")

        return audio_files

    def build_timeline(self, audio_files: dict[int, str]) -> list[dict]:
        timeline = self.mixer.build_timeline(
            dialogues=self.all_dialogues,
            audio_files=audio_files,
            get_duration_fn=self.tts.get_audio_duration_ms,
        )

        # Build a lookup: dialogue_id -> computed audio position
        audio_lookup: dict[int, dict] = {}
        for entry in timeline:
            d_id = entry.get("id")
            start_ms = entry.get("start_ms", 0)
            duration_ms = entry.get("duration_ms", 0)
            overlap_ms = entry.get("overlap_ms", 0)
            end_ms = start_ms + duration_ms

            audio_lookup[d_id] = {
                "dialogue_id": d_id,
                "speaker": entry.get("speaker", ""),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "duration_ms": duration_ms,
                "file": entry.get("file", ""),
                "overlap_ms": overlap_ms,
            }

        # Compute per-section audio timeline
        for sec_record in self.output_report["sections_timeline"]:
            sec_dialogue_ids = {s["dialogue_id"] for s in sec_record["scripts"]}
            sec_audio_entries = [
                audio_lookup[d_id] for d_id in sec_dialogue_ids if d_id in audio_lookup
            ]

            if sec_audio_entries:
                sec_start = min(e["start_ms"] for e in sec_audio_entries)
                sec_end = max(e["end_ms"] for e in sec_audio_entries)
                sec_record["audio_start_ms"] = sec_start
                sec_record["audio_end_ms"] = sec_end
                sec_record["audio_duration_ms"] = sec_end - sec_start
            else:
                sec_record["audio_start_ms"] = 0
                sec_record["audio_end_ms"] = 0
                sec_record["audio_duration_ms"] = 0

            # Update scripts with audio positions
            for script in sec_record["scripts"]:
                d_id = script["dialogue_id"]
                if d_id in audio_lookup:
                    info = audio_lookup[d_id]
                    script["audio_start_ms"] = info["start_ms"]
                    script["audio_end_ms"] = info["end_ms"]
                    script["audio_duration_ms"] = info["duration_ms"]
                    script["audio_overlap_ms"] = info["overlap_ms"]

        return timeline
    
    def mix_audio(self, timeline=None, output_file="openpodcast_episode.mp3") -> Path:
        mix_start = time.time()

        # First, do the normal dialogue mix
        result = self.mixer.mix(timeline=timeline, output_file=output_file)

        if not result:
            self.output_report["mix"] = {
                "output_file": None,
                "mix_duration_sec": round(time.time() - mix_start, 3),
                "intro_music": None,
                "outro_music": None,
            }
            return result

        needs_reexport = False
        music_lead_in_ms = 0

        try:
            from pydub import AudioSegment

            mixed = AudioSegment.from_file(str(result))

            # ============================================================
            # INTRO FLOW:
            #   0:00        → Music starts at full volume
            #   0:05        → Music ducks down (volume drop)
            #   0:05        → ID:1 speech begins (over ducked music)
            #   ID:1 ends   → Music fades out completely
            #   ID:2 starts → Clean speech, no music
            # ============================================================
            if self.intro_music_path and self.intro_music_path.exists():
                intro_music_raw = AudioSegment.from_file(str(self.intro_music_path))

                MUSIC_SOLO_MS = 10000       # 10s music plays alone at full volume
                DUCK_DB = -14              # how much to lower music under speech
                DUCK_FADE_MS = 800         # fade duration into ducked level
                MUSIC_FADEOUT_MS = 2000    # fade-out duration after ID:1 ends

                # --- Find ID:1 duration from timeline ---
                id1_duration_ms = 0
                for entry in timeline or []:
                    if entry.get("id") == 1:
                        id1_duration_ms = entry.get("duration_ms", 0)
                        break

                # Shift ALL dialogue audio forward by MUSIC_SOLO_MS
                music_lead_in_ms = MUSIC_SOLO_MS
                lead_in_silence = AudioSegment.silent(duration=music_lead_in_ms)
                mixed = lead_in_silence + mixed

                # ----------------------------------------------------------
                # Update ALL timeline offsets across every report structure
                # ----------------------------------------------------------
                self._shift_all_timeline_offsets(music_lead_in_ms)

                # --- Build the intro music track with volume automation ---
                total_intro_music_ms = MUSIC_SOLO_MS + id1_duration_ms + MUSIC_FADEOUT_MS

                # Trim or loop the raw music to fit
                if len(intro_music_raw) < total_intro_music_ms:
                    loops_needed = (total_intro_music_ms // len(intro_music_raw)) + 1
                    intro_music_raw = intro_music_raw * loops_needed
                intro_music_full = intro_music_raw[:total_intro_music_ms]

                # Part 1: Full volume solo section (0 → MUSIC_SOLO_MS)
                part_solo = intro_music_full[:MUSIC_SOLO_MS]

                # Part 2: Ducked section under ID:1 speech
                duck_section_ms = id1_duration_ms
                part_ducked_raw = intro_music_full[MUSIC_SOLO_MS:MUSIC_SOLO_MS + duck_section_ms]
                if len(part_ducked_raw) > 0:
                    part_ducked = part_ducked_raw + DUCK_DB
                    if len(part_ducked) > DUCK_FADE_MS:
                        fade_in_portion = part_ducked_raw[:DUCK_FADE_MS].fade(
                            from_gain=0, to_gain=DUCK_DB, start=0, duration=DUCK_FADE_MS
                        )
                        rest_portion = part_ducked_raw[DUCK_FADE_MS:] + DUCK_DB
                        part_ducked = fade_in_portion + rest_portion
                else:
                    part_ducked = AudioSegment.silent(duration=0)

                # Part 3: Fade-out section after ID:1 ends
                part_fadeout_raw = intro_music_full[MUSIC_SOLO_MS + duck_section_ms:]
                if len(part_fadeout_raw) > 0:
                    part_fadeout = (part_fadeout_raw + DUCK_DB).fade_out(len(part_fadeout_raw))
                else:
                    part_fadeout = AudioSegment.silent(duration=0)

                # Combine all parts
                intro_music_shaped = part_solo + part_ducked + part_fadeout

                # Extend canvas if needed
                if len(intro_music_shaped) > len(mixed):
                    pad = AudioSegment.silent(duration=len(intro_music_shaped) - len(mixed))
                    mixed = mixed + pad

                # Overlay intro music starting at position 0
                mixed = mixed.overlay(intro_music_shaped, position=0)
                needs_reexport = True

                id1_start = music_lead_in_ms
                id1_end = music_lead_in_ms + id1_duration_ms
                music_end = len(intro_music_shaped)

                self._log(f"  🎵 Intro music flow:")
                self._log(f"     {_fmt_ms(0)} → {_fmt_ms(MUSIC_SOLO_MS)}  "
                        f"Music solo (full volume)")
                self._log(f"     {_fmt_ms(MUSIC_SOLO_MS)} → duck transition ({DUCK_FADE_MS}ms, {DUCK_DB}dB)")
                self._log(f"     {_fmt_ms(id1_start)} → {_fmt_ms(id1_end)}  "
                        f"ID:1 speech (music ducked underneath)")
                self._log(f"     {_fmt_ms(id1_end)} → {_fmt_ms(music_end)}  "
                        f"Music fade-out ({MUSIC_FADEOUT_MS}ms)")
                self._log(f"     {_fmt_ms(id1_end)} → ID:2 starts (clean, no music)")

            # ============================================================
            # OUTRO FLOW:
            #   Final sentence ends
            #   → 0ms silence
            #   → Music fades in (starts quiet, rises to full)
            #   → Plays at full volume
            #   → Fades out to silence
            # ============================================================
            if self.outro_music_path and self.outro_music_path.exists():
                outro_music_raw = AudioSegment.from_file(str(self.outro_music_path))

                OUTRO_GAP_MS = 0
                OUTRO_FADE_IN_MS = 2000
                OUTRO_FADE_OUT_MS = 3000

                outro_music = outro_music_raw
                if len(outro_music) > OUTRO_FADE_IN_MS:
                    outro_music = outro_music.fade_in(OUTRO_FADE_IN_MS)
                else:
                    outro_music = outro_music.fade_in(len(outro_music))

                if len(outro_music) > OUTRO_FADE_OUT_MS:
                    outro_music = outro_music.fade_out(OUTRO_FADE_OUT_MS)
                else:
                    outro_music = outro_music.fade_out(len(outro_music))

                gap = AudioSegment.silent(duration=OUTRO_GAP_MS)
                outro_start_ms = len(mixed) + OUTRO_GAP_MS

                mixed = mixed + gap + outro_music
                needs_reexport = True

                outro_end_ms = len(mixed)
                self._log(f"  🎵 Outro music flow:")
                self._log(f"     {_fmt_ms(outro_start_ms - OUTRO_GAP_MS)} → "
                        f"{_fmt_ms(outro_start_ms)}  Silence gap ({OUTRO_GAP_MS}ms)")
                self._log(f"     {_fmt_ms(outro_start_ms)} → "
                        f"{_fmt_ms(outro_start_ms + OUTRO_FADE_IN_MS)}  "
                        f"Fade in ({OUTRO_FADE_IN_MS}ms)")
                self._log(f"     {_fmt_ms(outro_end_ms - OUTRO_FADE_OUT_MS)} → "
                        f"{_fmt_ms(outro_end_ms)}  "
                        f"Fade out ({OUTRO_FADE_OUT_MS}ms)")
                self._log(f"     Total outro: {_fmt_ms(len(outro_music))}")

            # Re-export if we added music
            if needs_reexport:
                ext = Path(output_file).suffix.lower().lstrip(".")
                export_format = ext if ext in ("mp3", "wav", "ogg", "flac") else "mp3"
                export_params = {}
                if export_format == "mp3":
                    export_params["bitrate"] = "192k"

                mixed.export(str(result), format=export_format, **export_params)
                self._log(f"  ✅ Re-exported with music: {result}")
                self._log(f"  🎧 Final duration: {_fmt_ms(len(mixed))}")

        except ImportError:
            self._log("  ⚠️  pydub not available — skipping music overlay")
        except Exception as e:
            self._log(f"  ⚠️  Music mixing failed: {e}")
            import traceback
            self._log(f"     {traceback.format_exc()}")

        mix_end = time.time()

        self.output_report["mix"] = {
            "output_file": str(result) if result else None,
            "mix_duration_sec": round(mix_end - mix_start, 3),
            "intro_music": str(self.intro_music_path) if self.intro_music_path else None,
            "outro_music": str(self.outro_music_path) if self.outro_music_path else None,
            "music_lead_in_ms": music_lead_in_ms,
        }
        return result

    def _shift_all_timeline_offsets(self, shift_ms: int):
        """Shift every audio_start_ms / audio_end_ms / start_ms / end_ms
        across all output_report structures by shift_ms.

        This is called when intro music prepends silence, pushing all
        dialogue audio forward in time.
        """
        if shift_ms <= 0:
            return

        # 2) sections_timeline — top-level section boundaries
        for sec_record in self.output_report.get("sections_timeline", []):
            if sec_record.get("audio_start_ms") is not None:
                sec_record["audio_start_ms"] += shift_ms
            if sec_record.get("audio_end_ms") is not None:
                sec_record["audio_end_ms"] += shift_ms

            # 3) sections_timeline[].scripts[] — nested per-dialogue records
            #    These MAY be the same dicts as dialogues_timeline (shared refs)
            #    or they may be copies. We track what we've already shifted
            #    by checking a sentinel to avoid double-shifting.
            for script in sec_record.get("scripts", []):
                if script.get("_intro_shifted"):
                    continue  # already shifted via dialogues_timeline reference
                if script.get("audio_start_ms") is not None:
                    script["audio_start_ms"] += shift_ms
                if script.get("audio_end_ms") is not None:
                    script["audio_end_ms"] += shift_ms
                script["_intro_shifted"] = True

        for sec_record in self.output_report.get("sections_timeline", []):
            for script in sec_record.get("scripts", []):
                script.pop("_intro_shifted", None)

    def print_analysis(self):
        meta = self.podcast["metadata"]
        self._log("\n" + "=" * 60)
        self._log("🌪️  Openpodcast Analysis Report")
        self._log(f"📻 {self.podcast['title']}")
        self._log("=" * 60)
        self._log(f"\n📝 Total dialogues: {meta['total_dialogues']}")
        self._log(f"🔥 Interrupt ratio: {meta['interrupt_ratio']}")

        self._log("\n👥 Dialogue distribution:")
        dialogue_dist = {}
        for host_id, count in meta["dialogue_distribution"].items():
            host = self._get_host_info(host_id)
            name = host.get("name", host_id)
            bar = "█" * count + "░" * (30 - count)
            self._log(f"  {EMOJI.get(host_id, '⚪')} {name:4s}: {bar} ({count})")
            dialogue_dist[host_id] = {"name": name, "count": count}

        self._log("\n💥 Interrupt types:")
        interrupt_dist = {}
        for itype, count in meta["interrupt_distribution"].items():
            if itype == "none": continue
            self._log(f"  {itype:12s}: {'🔸' * count} ({count})")
            interrupt_dist[itype] = count

        self.output_report["analysis"] = {
            "total_dialogues": meta["total_dialogues"],
            "interrupt_ratio": meta["interrupt_ratio"],
            "dialogue_distribution": dialogue_dist,
            "interrupt_distribution": interrupt_dist,
        }

    def _save_sections_txt(self):
        """Save section start times as a simple text chapter list."""
        sections = self.output_report.get("sections_timeline", [])
        if not sections:
            return

        lines = []
        for sec in sections:
            start_ms = sec.get("audio_start_ms", 0)
            title = sec.get("section_title", "")
            lines.append(f"{_fmt_ms_hms(start_ms)} {title}")

        txt_content = "\n".join(lines)
        self.output_report["timeline_table"] = txt_content

        # Also print to console
        self._log(f"\n{'─' * 60}")
        self._log("📑 Sections Timeline (chapters)")
        self._log(f"{'─' * 60}")
        for line in lines:
            self._log(f"  {line}")

    def _save_output_json(self):
        """Persist the full output report to output.json."""
        self.output_report["console_output"] = self.console.get_all()

        # Print timeline summary to console
        self._log("\n" + "=" * 60)
        self._log("📊 Timeline Summary")
        self._log("=" * 60)

        total_audio_duration_ms = 0

        for sec in self.output_report.get("sections_timeline", []):
            sec_audio_start = sec.get("audio_start_ms", 0)
            sec_audio_end = sec.get("audio_end_ms", 0)
            sec_audio_dur = sec.get("audio_duration_ms", 0)
            gen_dur_s = sec.get("gen_duration_sec", 0)

            if sec_audio_end > total_audio_duration_ms:
                total_audio_duration_ms = sec_audio_end

            self._log(
                f"\n📌 {sec.get('corner_name', '')} {sec['section_title']}"
            )
            self._log(
                f"   Generation: {gen_dur_s}s | "
                f"Audio: {_fmt_ms(sec_audio_start)} → {_fmt_ms(sec_audio_end)} "
                f"(duration: {_fmt_ms(sec_audio_dur)} / {round(sec_audio_dur / 1000, 2)}s)"
            )

            for script in sec.get("scripts", []):
                s_status = "✅" if script["status"] == "success" else "❌"
                a_start = script.get("audio_start_ms", 0) or 0
                a_end = script.get("audio_end_ms", 0) or 0
                a_dur = script.get("audio_duration_ms", 0) or 0
                a_overlap = script.get("audio_overlap_ms", 0) or 0
                overlap_tag = f" ↔{a_overlap}ms" if a_overlap else ""

                self._log(
                    f"   {s_status} d_{script['dialogue_id']:04d} "
                    f"{script['name']:8s} | "
                    f"gen: {script['gen_duration_sec']}s | "
                    f"audio: {_fmt_ms(a_start)} → {_fmt_ms(a_end)} "
                    f"({round(a_dur / 1000, 2)}s){overlap_tag}"
                )

        # Account for outro music in total duration
        outro_duration_ms = 0
        if self.outro_music_path:
            try:
                outro_duration_ms = self.tts.get_audio_duration_ms(str(self.outro_music_path))
                self._log(f"\n🎵 Outro music duration: {_fmt_ms(outro_duration_ms)} ({round(outro_duration_ms / 1000, 2)}s)")
            except Exception:
                pass

        total_with_outro = total_audio_duration_ms + outro_duration_ms

        # Total episode duration
        if total_with_outro > 0:
            total_sec = round(total_with_outro / 1000, 2)
            total_min = round(total_with_outro / 60000, 2)
            self._log(f"\n🎧 Total episode audio: {_fmt_ms(total_with_outro)} ({total_sec}s / {total_min}min)")
            if outro_duration_ms > 0:
                self._log(f"   (dialogue: {_fmt_ms(total_audio_duration_ms)} + outro: {_fmt_ms(outro_duration_ms)})")
            self.output_report["summary"]["total_audio_duration_ms"] = total_with_outro
            self.output_report["summary"]["total_audio_duration_sec"] = total_sec
            self.output_report["summary"]["dialogue_audio_duration_ms"] = total_audio_duration_ms
            self.output_report["summary"]["outro_music_duration_ms"] = outro_duration_ms

        # Save sections timeline text file
        self._save_sections_txt()

        # Final console capture update
        self.output_report["console_output"] = self.console.get_all()

        output_path = self.output_dir / "output.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.output_report, f, ensure_ascii=False, indent=2, default=str)

        self._log(f"\n💾 Output report saved: {output_path}")

    def run(self, output_file="openpodcast_episode.mp3"):
        run_start = time.time()

        audio_files = self.generate_individual_audio()
        timeline = self.build_timeline(audio_files)
        self.print_analysis()
        if audio_files:
            self.mix_audio(timeline=timeline, output_file=output_file)
        else:
            self._log("\n⚠️  No audio generated. Skipping mixing.")

        run_end = time.time()
        self.output_report["summary"]["total_run_duration_sec"] = round(run_end - run_start, 3)

        self._save_output_json()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Openpodcast TTS Pipeline")
    parser.add_argument("json_path", help="Path to episode JSON file")
    parser.add_argument("-o", "--output", default="openpodcast_episode.mp3")
    parser.add_argument("-d", "--output-dir", default="./output")
    parser.add_argument("-k", "--api-key", default=None,
                        help="API key (Google for hd/gemini)")
    parser.add_argument(
        "-e", "--engine",
        choices=["hd", "gemini"],
        default=None,
        help=(
            "hd: Chirp 3 HD (Google Cloud)"
            "gemini: Gemini Flash TTS | "
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")

    args = parser.parse_args()
    tts = args.engine or os.getenv("TTS_ENGINE", "hd")
    
    pipeline = OpenpodcastTTS(
        json_path=args.json_path,
        output_dir=args.output_dir,
        api_key=args.api_key,
        tts=tts,
    )

    if args.dry_run:
        pipeline.print_analysis()
        pipeline._save_output_json()
    elif args.retry_failed:
        recovered = pipeline.retry_failed()
        if recovered:
            all_audio = {}
            for d in pipeline.all_dialogues:
                wav = pipeline.tts_dir / f"d_{d['id']:04d}.wav"
                if wav.exists() and wav.stat().st_size > 1000:
                    all_audio[d["id"]] = str(wav)
            timeline = pipeline.build_timeline(all_audio)
            pipeline.mix_audio(timeline=timeline, output_file=args.output)
        pipeline._save_output_json()
    else:
        pipeline.run(output_file=args.output)


if __name__ == "__main__":
    main()
