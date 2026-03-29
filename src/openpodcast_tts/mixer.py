"""
Audio mixer — interrupt/overlap handling + final mixdown
"""

import json
import wave
from pathlib import Path

from pydub import AudioSegment


# Interrupt timing (milliseconds)
INTERRUPT_TIMING = {
    "none":      {"overlap_ms": 0,    "gap_ms": 300},
    "cut_in":    {"overlap_ms": 200,  "gap_ms": 0},
    "overlap":   {"overlap_ms": 500,  "gap_ms": 0},
    "piggyback": {"overlap_ms": 100,  "gap_ms": 0},
    "redirect":  {"overlap_ms": 150,  "gap_ms": 0},
    "challenge": {"overlap_ms": 300,  "gap_ms": 0},
    "support":   {"overlap_ms": 400,  "gap_ms": 0},
}


class AudioMixer:
    """Overlap timeline-based audio mixer"""

    def __init__(self, output_dir: str | Path = "./openpodcast_output"):
        self.output_dir = Path(output_dir)

    def build_timeline(
        self,
        dialogues: list[dict],
        audio_files: dict[int, str],
        get_duration_fn=None,
    ) -> list[dict]:
        """
        Build an overlap timeline from dialogue list and audio files.

        Args:
            dialogues: Full dialogue list
            audio_files: {dialogue_id: wav_path}
            get_duration_fn: wav_path -> duration_ms function

        Returns:
            Timeline list
        """
        timeline = []
        current_time_ms = 0

        for d in dialogues:
            d_id = d["id"]
            interrupt = d["interrupt_type"]
            timing = INTERRUPT_TIMING[interrupt]

            # Calculate start time
            if timing["overlap_ms"] > 0 and current_time_ms > 0:
                start_time = max(0, current_time_ms - timing["overlap_ms"])
            else:
                start_time = current_time_ms + timing["gap_ms"]

            # Actual audio length or estimate
            wav_path = audio_files.get(d_id)
            if wav_path and get_duration_fn and Path(wav_path).exists():
                duration_ms = get_duration_fn(wav_path)
            else:
                # Rough estimate for Korean: ~75ms per character
                duration_ms = len(d["text"]) * 75

            timeline.append({
                "id": d_id,
                "speaker": d["speaker"],
                "name": d["name"],
                "start_ms": start_time,
                "duration_ms": duration_ms,
                "file": str(wav_path) if wav_path else None,
                "interrupt_type": d["interrupt_type"],
                "pause_after": d["pause_after"],
            })

            current_time_ms = (
                start_time
                + duration_ms
                + int(d["pause_after"] * 1000)
            )

        # Save timeline
        timeline_path = self.output_dir / "timeline.json"
        with open(timeline_path, "w", encoding="utf-8") as f:
            json.dump(timeline, f, ensure_ascii=False, indent=2)

        total_minutes = current_time_ms / 1000 / 60
        print(f"\n⏱️  Estimated total length: {total_minutes:.1f} min")

        return timeline

    def mix(
        self,
        timeline: list[dict] | None = None,
        output_file: str = "openpodcast_episode.mp3",
    ) -> Path:
        """
        Final mixdown based on timeline.

        Args:
            timeline: Timeline list (loads from file if None)
            output_file: Output filename

        Returns:
            Output file path
        """
        if timeline is None:
            timeline_path = self.output_dir / "timeline.json"
            with open(timeline_path, "r", encoding="utf-8") as f:
                timeline = json.load(f)

        # Calculate canvas length
        total_ms = max(
            t["start_ms"] + t["duration_ms"] for t in timeline
        ) + 3000  # 3s buffer

        canvas = AudioSegment.silent(duration=total_ms)

        mixed_count = 0
        for t in timeline:
            audio_path = Path(t["file"]) if t["file"] else None
            if audio_path and audio_path.exists():
                clip = AudioSegment.from_file(str(audio_path))
                canvas = canvas.overlay(clip, position=t["start_ms"])
                mixed_count += 1

        output_path = self.output_dir / output_file
        canvas.export(str(output_path), format="mp3", bitrate="192k")

        print(f"🎧 Mixing complete: {output_path} ({mixed_count} clips)")
        return output_path
