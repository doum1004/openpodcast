import hashlib
import json
import os
import re
import shutil
import time
import wave
from pathlib import Path
from dataclasses import dataclass

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

class EmptyResponseError(Exception):
    pass


@dataclass
class GeminiVoiceConfig:
    voice_name: str
    language: str
    description: str


AVAILABLE_VOICES = {
    "male": [
        GeminiVoiceConfig("Achird",  "ko-KR", "Calm and low male voice"),
        GeminiVoiceConfig("Algenib",  "ko-KR", "Deep and weighty male"),
        GeminiVoiceConfig("Algieba",  "ko-KR", "Warm and friendly male"),
        GeminiVoiceConfig("Alnilam",  "ko-KR", "Energetic male"),
        GeminiVoiceConfig("Schedar",    "ko-KR", "Soft and stable male"),
    ],
    "female": [
        GeminiVoiceConfig("Kore",    "ko-KR", "Bright and articulate female voice"),
        GeminiVoiceConfig("Aoede",   "ko-KR", "Soft and warm female voice"),
        GeminiVoiceConfig("Leda",    "ko-KR", "Calm and intellectual female voice"),
        GeminiVoiceConfig("Zephyr",  "ko-KR", "Cheerful and lively female voice"),
    ],
}

ROLE_VOICE_PREFERENCE = {
    "male": {
        "host":     ["Achird", "Schedar", "Algieba", "Algenib"],
        "analyst":  ["Algenib", "Achird", "Alnilam", "Algieba"],
        "debater":  ["Alnilam", "Algenib", "Algieba", "Achird"],
        "mediator": ["Algieba", "Schedar", "Achird", "Algenib"],
        "default":  ["Schedar", "Achird", "Algieba", "Algenib", "Alnilam"],
    },
    "female": {
        "host":     ["Leda", "Aoede", "Kore", "Zephyr"],
        "analyst":  ["Kore", "Leda", "Zephyr", "Aoede"],
        "debater":  ["Zephyr", "Kore", "Leda", "Aoede"],
        "mediator": ["Aoede", "Leda", "Kore", "Zephyr"],
        "default":  ["Kore", "Aoede", "Leda", "Zephyr"],
    },
}


RETRYABLE_ERRORS = [
    "429", "500", "503",
    "RESOURCE_EXHAUSTED", "INTERNAL", "UNAVAILABLE",
]


# ============================================
# Voice Assigner — gender-aware, no overlap
# ============================================

class VoiceAssigner:
    """
    Reads host info from JSON and assigns gender-appropriate voices.
    Guarantees no voice is assigned to more than one host.
    """

    def __init__(self):
        self.assignments: dict[str, GeminiVoiceConfig] = {}

    def assign_voices(self, hosts: dict | list) -> dict[str, GeminiVoiceConfig]:
        """
        Args:
            hosts: podcast.hosts from JSON
                List format: [{"id": "host_1", "name": "Alex", "gender": "male", ...}, ...]
                Dict format: {"host_1": {"name": "Alex", "gender": "male", ...}, ...}

        Returns:
            {"host_1": GeminiVoiceConfig(...), ...}
        """
        # Convert list to dict
        if isinstance(hosts, list):
            hosts_dict = {}
            for h in hosts:
                host_id = h.get("id")
                if not host_id:
                    raise ValueError(f"Host is missing 'id' field: {h}")
                hosts_dict[host_id] = h
        else:
            hosts_dict = hosts

        self.assignments = {}
        used_voices: set[str] = set()

        for host_id in sorted(hosts_dict.keys()):
            host_info = hosts_dict[host_id]
            gender = host_info.get("gender", "male").lower()
            role = host_info.get("role", "default")
            name = host_info.get("name", host_id)

            if gender not in AVAILABLE_VOICES:
                raise ValueError(
                    f"Unsupported gender: {gender} (host: {name}). "
                    f"Supported: {list(AVAILABLE_VOICES.keys())}"
                )

            preferences = ROLE_VOICE_PREFERENCE.get(gender, {})
            preferred_order = preferences.get(role, preferences.get("default", []))

            assigned = False
            for voice_name in preferred_order:
                if voice_name not in used_voices:
                    voice_cfg = next(
                        (v for v in AVAILABLE_VOICES[gender] if v.voice_name == voice_name),
                        None,
                    )
                    if voice_cfg:
                        self.assignments[host_id] = voice_cfg
                        used_voices.add(voice_name)
                        assigned = True
                        break

            if not assigned:
                for voice_cfg in AVAILABLE_VOICES[gender]:
                    if voice_cfg.voice_name not in used_voices:
                        self.assignments[host_id] = voice_cfg
                        used_voices.add(voice_cfg.voice_name)
                        assigned = True
                        break

            if not assigned:
                raise ValueError(
                    f"Not enough {gender} voices to assign to '{name}'."
                )

        return self.assignments

    def print_assignments(self, hosts: dict | list):
        """Print assignment results"""
        # Convert list to dict
        if isinstance(hosts, list):
            hosts_dict = {h["id"]: h for h in hosts}
        else:
            hosts_dict = hosts

        print("🎤 Voice assignment results:")
        print(f"{'─' * 55}")
        for host_id in sorted(self.assignments.keys()):
            host_info = hosts_dict[host_id]
            voice = self.assignments[host_id]
            gender_emoji = "👨" if host_info.get("gender") == "male" else "👩"
            print(
                f"  {gender_emoji} {host_info['name']:4s} ({host_info.get('role', '?'):4s}) "
                f"→ {voice.voice_name:8s} | {voice.description}"
            )
        print(f"{'─' * 55}")

        voice_names = [v.voice_name for v in self.assignments.values()]
        assert len(voice_names) == len(set(voice_names)), "❌ Voice duplication detected!"
        print("  ✅ No duplicates confirmed")


# ============================================
# Content-Based Audio Cache
# ============================================

class AudioCache:
    def __init__(self, cache_dir: str | Path = "./tts_cache"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.cache_dir / "cache_index.json"
        self.index: dict[str, dict] = self._load_index()

    def _load_index(self) -> dict:
        if self.index_path.exists():
            with open(self.index_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_index(self):
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(self.index, f, ensure_ascii=False, indent=2)

    def make_key(self, text: str, voice_name: str, emotion: str) -> str:
        """Hash from text + actual voice name + emotion"""
        content = f"{voice_name}|{emotion}|{text}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    def get(self, text: str, voice_name: str, emotion: str) -> Path | None:
        key = self.make_key(text, voice_name, emotion)
        if key in self.index:
            cached_path = Path(self.index[key]["path"])
            if cached_path.exists() and cached_path.stat().st_size > 1000:
                return cached_path
            else:
                del self.index[key]
                self._save_index()
        return None

    def put(self, text: str, voice_name: str, emotion: str, wav_path: Path) -> Path:
        key = self.make_key(text, voice_name, emotion)
        cache_file = self.cache_dir / f"{key}.wav"
        if wav_path != cache_file:
            shutil.copy2(str(wav_path), str(cache_file))
        self.index[key] = {
            "path": str(cache_file),
            "voice_name": voice_name,
            "emotion": emotion,
            "text": text[:80] + ("..." if len(text) > 80 else ""),
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._save_index()
        return cache_file

    def stats(self) -> dict:
        total = len(self.index)
        total_size = sum(
            Path(v["path"]).stat().st_size
            for v in self.index.values()
            if Path(v["path"]).exists()
        )
        return {"entries": total, "size_mb": round(total_size / 1024 / 1024, 1)}


# ============================================
# Rate Limiter
# ============================================

class RateLimiter:
    def __init__(self, max_per_minute: int = 9, max_retries: int = 8):
        self.max_per_minute = max_per_minute
        self.max_retries = max_retries
        self.request_times: list[float] = []
        self.min_interval = 60.0 / max_per_minute

    def wait_if_needed(self):
        now = time.time()
        self.request_times = [t for t in self.request_times if now - t < 60.0]
        if len(self.request_times) >= self.max_per_minute:
            oldest = self.request_times[0]
            wait_time = 60.0 - (now - oldest) + 1.0
            if wait_time > 0:
                print(f"    ⏳ Per-minute limit reached. Waiting {wait_time:.1f}s...")
                time.sleep(wait_time)
        if self.request_times:
            elapsed = time.time() - self.request_times[-1]
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
        self.request_times.append(time.time())

    def parse_retry_delay(self, error_message: str) -> float:
        match = re.search(r'retry in (\d+\.?\d*)s', str(error_message))
        if match:
            return float(match.group(1))
        match = re.search(r'retryDelay.*?(\d+\.?\d*)s', str(error_message))
        if match:
            return float(match.group(1))
        return 15.0


# ============================================
# Gemini TTS Client
# ============================================

class GeminiTTSClient:
    SAMPLE_RATE = 24000

    def __init__(
        self,
        hosts: dict,
        api_key: str | None = None,
        cache_dir: str | Path = "./tts_cache",
    ):
        """
        Args:
            hosts: podcast.hosts dict from JSON
            api_key: Google AI API key
            cache_dir: Cache directory
        """
        resolved_key = api_key or os.getenv("GOOGLE_API_KEY")
        if not resolved_key:
            raise ValueError(
                "API key not found.\n"
                "  1. Add GOOGLE_API_KEY=... to your .env file\n"
                "  2. Run: export GOOGLE_API_KEY=...\n"
                "  3. Pass directly: GeminiTTSClient(api_key='...')"
            )

        self.model = "gemini-2.5-flash-preview-tts"
        self.hosts = hosts

        self.client = genai.Client(api_key=resolved_key)
        self.rate_limiter = RateLimiter(max_per_minute=9, max_retries=8)
        self.cache = AudioCache(cache_dir=cache_dir)
        self.failed: list[dict] = []

        # Gender-based automatic voice assignment
        self.assigner = VoiceAssigner()
        self.voice_map = self.assigner.assign_voices(hosts)

        cache_stats = self.cache.stats()
        print(f"🔑 API key loaded (last 4: ...{resolved_key[-4:]})")
        print(f"🤖 Model: {self.model}")
        print(
            f"📦 Cache: {cache_stats['entries']} entries, "
            f"{cache_stats['size_mb']}MB"
        )
        self.assigner.print_assignments(hosts)

    def _get_voice(self, speaker: str) -> GeminiVoiceConfig:
        """Return the assigned voice for a host ID"""
        if speaker not in self.voice_map:
            raise ValueError(
                f"Unknown speaker: {speaker}. "
                f"Registered speakers: {list(self.voice_map.keys())}"
            )
        return self.voice_map[speaker]

    def _build_prompt(self, text: str, speaker: str, emotion: str) -> str:
        """
        Build TTS prompt using the Korean emotion tag directly.
        """
        if emotion:
            return f"{emotion} 다음을 말하세요: {text}"
        return text

    def synthesize(
        self,
        text: str,
        speaker: str,
        emotion: str = "neutral",
        output_path: str | Path = "output.wav",
    ) -> Path | None:
        output_path = Path(output_path)
        voice_cfg = self._get_voice(speaker)

        # 1. Check cache (based on actual voice_name)
        cached = self.cache.get(text, voice_cfg.voice_name, emotion)
        if cached:
            if cached != output_path:
                shutil.copy2(str(cached), str(output_path))
            key_short = self.cache.make_key(text, voice_cfg.voice_name, emotion)[:8]
            print(f"    ♻️  Cache hit: {key_short}...")
            return output_path

        # 2. Build prompt
        prompt = self._build_prompt(text, speaker, emotion)

        last_error = None

        for attempt in range(1, self.rate_limiter.max_retries + 1):
            try:
                self.rate_limiter.wait_if_needed()

                response = self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_modalities=["AUDIO"],
                        speech_config=types.SpeechConfig(
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                    voice_name=voice_cfg.voice_name,
                                )
                            )
                        ),
                    ),
                )

                # Validate response
                if (
                    not response.candidates
                    or not response.candidates[0].content
                    or not response.candidates[0].content.parts
                    or not response.candidates[0].content.parts[0].inline_data
                    or not response.candidates[0].content.parts[0].inline_data.data
                ):
                    raise EmptyResponseError("Empty response received")

                audio_data = (
                    response.candidates[0]
                    .content.parts[0]
                    .inline_data.data
                )

                if len(audio_data) < 1000:
                    raise EmptyResponseError(f"Audio too small ({len(audio_data)}B)")

                self._save_wav(audio_data, output_path)
                self.cache.put(text, voice_cfg.voice_name, emotion, output_path)
                return output_path

            except (EmptyResponseError, AttributeError, TypeError, IndexError) as e:
                last_error = e
                wait = min(5 * (2 ** (attempt - 1)), 60) + attempt
                print(
                    f"    🔄 Empty/parse error (attempt {attempt}/"
                    f"{self.rate_limiter.max_retries}): {e}. "
                    f"Waiting {wait:.0f}s..."
                )
                time.sleep(wait)

            except Exception as e:
                last_error = e
                error_str = str(e)
                is_retryable = any(code in error_str for code in RETRYABLE_ERRORS)

                if is_retryable:
                    if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                        base_delay = self.rate_limiter.parse_retry_delay(error_str)
                        error_type = "429 Rate Limit"
                    else:
                        base_delay = min(10 * (2 ** (attempt - 1)), 120)
                        error_type = "Server error"

                    wait = base_delay + attempt
                    print(
                        f"    🔄 {error_type} (attempt {attempt}/"
                        f"{self.rate_limiter.max_retries}). "
                        f"Waiting {wait:.0f}s..."
                    )
                    time.sleep(wait)
                else:
                    print(f"    ❌ Non-retryable error: {e}")
                    self.failed.append({
                        "text": text, "speaker": speaker,
                        "voice_name": voice_cfg.voice_name,
                        "emotion": emotion,
                        "output_path": str(output_path),
                        "error": str(e),
                    })
                    return None

        print(f"    ❌ All {self.rate_limiter.max_retries} retries exhausted. Skipping.")
        self.failed.append({
            "text": text, "speaker": speaker,
            "voice_name": voice_cfg.voice_name,
            "emotion": emotion,
            "output_path": str(output_path),
            "error": str(last_error),
        })
        return None

    def save_failed_log(self, path: str | Path = "./tts_cache/failed.json"):
        path = Path(path)
        if self.failed:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.failed, f, ensure_ascii=False, indent=2)
            print(f"\n💾 Failed log saved: {path} ({len(self.failed)} entries)")

    def _save_wav(self, pcm_data: bytes, path: Path):
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.SAMPLE_RATE)
            wf.writeframes(pcm_data)

    def get_audio_duration_ms(self, wav_path: str | Path) -> int:
        with wave.open(str(wav_path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            return int(frames / rate * 1000)
