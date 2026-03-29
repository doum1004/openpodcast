import warnings
warnings.filterwarnings("ignore", category=SyntaxWarning, module="pydub")

import json
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from .gemini_tts import GeminiTTSClient, AudioCache
from .mixer import AudioMixer

EMOJI = {"host_1": "🔵", "host_2": "🔴", "host_3": "🟢", "host_4": "🟡"}

# Engine labels for display
ENGINE_LABELS = {
    "standard": "Gemini Flash TTS",
    "hd": "Chirp 3 HD",
}


def create_tts_client(hosts, api_key=None, quality="standard", cache_dir="./tts_cache"):
    """Create the appropriate TTS client based on quality/engine setting"""
    if quality == "hd":
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


class OpenpodcastTTS:
    def __init__(
        self,
        json_path: str,
        output_dir: str = "./openpodcast_output",
        api_key: str | None = None,
        quality: str = "standard",
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

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

        self.quality = quality

        # Use different client based on quality/engine
        self.tts = create_tts_client(
            hosts=self.hosts,
            api_key=api_key,
            quality=quality,
        )
        self.mixer = AudioMixer(output_dir=output_dir)

    def _get_host_info(self, speaker: str) -> dict:
        if isinstance(self.hosts, list):
            for h in self.hosts:
                if h.get("id") == speaker:
                    return h
            return {}
        return self.hosts.get(speaker, {})

    def generate_individual_audio(self) -> dict[int, str]:
        engine_label = ENGINE_LABELS.get(self.quality, self.quality)
        print(f"\n🎙️  {self.podcast['show_name']} - {self.podcast['title']}")
        print(f"🔥 Heat Level: {self.podcast['heat_level']}")
        print(f"🎚️  Engine: {engine_label}")
        print(f"📝 Total dialogues: {len(self.all_dialogues)}")
        print("=" * 60)

        audio_files: dict[int, str] = {}
        current_section = ""
        success = 0
        failed = 0

        for i, d in enumerate(self.all_dialogues):
            for section in self.podcast["sections"]:
                if d in section["dialogues"] and section["section_title"] != current_section:
                    current_section = section["section_title"]
                    corner = section.get("corner_name", "")
                    print(f"\n{'─' * 60}")
                    print(f"📌 {corner} {current_section}")
                    print(f"   Mood: {section['section_mood']} | Formation: {section['debate_formation']}")
                    print(f"{'─' * 60}")
                    break

            d_id = d["id"]
            speaker = d["speaker"]
            text = d["text"]
            emotion = d["emotion"]
            interrupt = d["interrupt_type"]

            output_path = self.output_dir / f"d_{d_id:04d}.wav"

            result = self.tts.synthesize(
                text=text,
                speaker=speaker,
                emotion=emotion,
                output_path=output_path,
            )

            if result:
                audio_files[d_id] = str(output_path)
                status = "✅"
                success += 1
            else:
                status = "❌"
                failed += 1

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
            print(
                f"  {status} {progress} {EMOJI.get(speaker, '⚪')} "
                f"{d['name']}({voice_name}){tag} "
                f"[{emotion}]: {text[:55]}{'...' if len(text) > 55 else ''} {marker_str}"
            )

        if hasattr(self.tts, 'save_failed_log'):
            self.tts.save_failed_log()

        print(f"\n{'=' * 60}")
        print(f"✅ Success: {success} | ❌ Failed: {failed} | Total: {len(self.all_dialogues)}")

        if failed > 0:
            print(f"💡 Retry failures: uv run openpodcast <json> --retry-failed")

        return audio_files

    def retry_failed(self) -> dict[int, str]:
        failed_path = Path("./tts_cache/failed.json")
        if not failed_path.exists():
            print("✅ No failure log found. All succeeded!")
            return {}

        with open(failed_path, "r", encoding="utf-8") as f:
            failed_items = json.load(f)

        print(f"\n🔄 Retrying failures: {len(failed_items)} entries")
        print("=" * 60)

        audio_files: dict[int, str] = {}
        still_failed = []

        for item in failed_items:
            result = self.tts.synthesize(
                text=item["text"],
                speaker=item["speaker"],
                emotion=item["emotion"],
                output_path=item["output_path"],
            )
            if result:
                fname = Path(item["output_path"]).stem
                d_id = int(fname.split("_")[1])
                audio_files[d_id] = item["output_path"]
                print(f"  ✅ Recovery succeeded: {fname}")
            else:
                still_failed.append(item)
                print(f"  ❌ Still failing: {item['text'][:50]}...")

        if still_failed:
            with open(failed_path, "w", encoding="utf-8") as f:
                json.dump(still_failed, f, ensure_ascii=False, indent=2)
            print(f"\n❌ Still failing: {len(still_failed)} entries")
        else:
            failed_path.unlink()
            print("\n🎉 All failures recovered!")

        return audio_files

    def build_timeline(self, audio_files: dict[int, str]) -> list[dict]:
        return self.mixer.build_timeline(
            dialogues=self.all_dialogues,
            audio_files=audio_files,
            get_duration_fn=self.tts.get_audio_duration_ms,
        )

    def mix_audio(self, timeline=None, output_file="openpodcast_episode.mp3") -> Path:
        return self.mixer.mix(timeline=timeline, output_file=output_file)

    def print_analysis(self):
        meta = self.podcast["metadata"]
        print("\n" + "=" * 60)
        print("🌪️  Openpodcast Analysis Report")
        print(f"📻 {self.podcast['title']}")
        print("=" * 60)
        print(f"\n📝 Total dialogues: {meta['total_dialogues']}")
        print(f"🔥 Interrupt ratio: {meta['interrupt_ratio']}")

        print("\n👥 Dialogue distribution:")
        for host_id, count in meta["dialogue_distribution"].items():
            host = self._get_host_info(host_id)
            name = host.get("name", host_id)
            bar = "█" * count + "░" * (30 - count)
            print(f"  {EMOJI.get(host_id, '⚪')} {name:4s}: {bar} ({count})")

        print("\n💥 Interrupt types:")
        for itype, count in meta["interrupt_distribution"].items():
            if itype == "none": continue
            print(f"  {itype:12s}: {'🔸' * count} ({count})")

    def run(self, output_file="openpodcast_episode.mp3"):
        audio_files = self.generate_individual_audio()
        timeline = self.build_timeline(audio_files)
        self.print_analysis()
        if audio_files:
            self.mix_audio(timeline=timeline, output_file=output_file)
        else:
            print("\n⚠️  No audio generated. Skipping mixing.")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Openpodcast TTS Pipeline")
    parser.add_argument("json_path", help="Path to episode JSON file")
    parser.add_argument("-o", "--output", default="openpodcast_episode.mp3")
    parser.add_argument("-d", "--output-dir", default="./openpodcast_output")
    parser.add_argument("-k", "--api-key", default=None,
                        help="API key (Google for standard/hd)")
    parser.add_argument(
        "-q", "--quality",
        choices=["standard", "hd"],
        default=None,
        help=(
            "standard: Gemini Flash TTS | "
            "hd: Chirp 3 HD (Google Cloud)"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")

    args = parser.parse_args()
    quality = args.quality or os.getenv("TTS_QUALITY", "standard")

    pipeline = OpenpodcastTTS(
        json_path=args.json_path,
        output_dir=args.output_dir,
        api_key=args.api_key,
        quality=quality,
    )

    if args.dry_run:
        pipeline.print_analysis()
    elif args.retry_failed:
        recovered = pipeline.retry_failed()
        if recovered:
            all_audio = {}
            for d in pipeline.all_dialogues:
                wav = pipeline.output_dir / f"d_{d['id']:04d}.wav"
                if wav.exists() and wav.stat().st_size > 1000:
                    all_audio[d["id"]] = str(wav)
            timeline = pipeline.build_timeline(all_audio)
            pipeline.mix_audio(timeline=timeline, output_file=args.output)
    else:
        pipeline.run(output_file=args.output)


if __name__ == "__main__":
    main()
