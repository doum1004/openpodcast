# src/openpodcast_tts/gemini_tts.py

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


# ✅ 1. DATACLASS FIRST
@dataclass
class GeminiVoiceConfig:
    voice_name: str
    language: str
    description: str


# ✅ 2. THEN USE IT
AVAILABLE_VOICES = {
    "male": [
        GeminiVoiceConfig("Kore",    "ko-KR", "차분하고 낮은 남성 음성"),
        GeminiVoiceConfig("Charon",  "ko-KR", "에너지 넘치는 남성 음성"),
        GeminiVoiceConfig("Fenrir",  "ko-KR", "깊고 무게감 있는 남성 음성"),
        GeminiVoiceConfig("Orus",    "ko-KR", "밝고 친근한 남성 음성"),
    ],
    "female": [
        GeminiVoiceConfig("Puck",    "ko-KR", "밝고 또렷한 여성 음성"),
        GeminiVoiceConfig("Aoede",   "ko-KR", "부드럽고 따뜻한 여성 음성"),
        GeminiVoiceConfig("Leda",    "ko-KR", "차분하고 지적인 여성 음성"),
        GeminiVoiceConfig("Zephyr",  "ko-KR", "경쾌하고 활기찬 여성 음성"),
    ],
}

ROLE_VOICE_PREFERENCE = {
    "male": {
        "진행자":  ["Kore", "Orus", "Charon", "Fenrir"],
        "분석가":  ["Fenrir", "Kore", "Orus", "Charon"],
        "토론자":  ["Charon", "Fenrir", "Orus", "Kore"],
        "중재자":  ["Orus", "Kore", "Charon", "Fenrir"],
        "default": ["Kore", "Charon", "Fenrir", "Orus"],
    },
    "female": {
        "진행자":  ["Leda", "Aoede", "Puck", "Zephyr"],
        "분석가":  ["Puck", "Leda", "Zephyr", "Aoede"],
        "토론자":  ["Zephyr", "Puck", "Leda", "Aoede"],
        "중재자":  ["Aoede", "Leda", "Puck", "Zephyr"],
        "default": ["Puck", "Aoede", "Leda", "Zephyr"],
    },
}

EMOTION_PROMPTS = {
    "neutral":     "",
    "excited":     "매우 흥분하고 열정적인 톤으로",
    "passionate":  "열정적이고 확신에 찬 톤으로",
    "curious":     "호기심 가득한 톤으로",
    "skeptical":   "의심스럽고 회의적인 톤으로",
    "frustrated":  "답답하고 짜증스러운 톤으로",
    "amused":      "재미있어하며 웃음이 섞인 톤으로",
    "sarcastic":   "비꼬는 듯한 톤으로",
    "defensive":   "방어적이고 단호한 톤으로",
    "aggressive":  "공격적이고 강한 톤으로",
    "reflective":  "차분하고 사려 깊은 톤으로",
    "surprised":   "놀란 톤으로",
    "dismissive":  "무시하는 듯한 톤으로",
    "empathetic":  "공감하는 따뜻한 톤으로",
    "triumphant":  "승리감에 찬 톤으로",
    "conceding":   "인정하며 양보하는 톤으로",
    "warm":        "따뜻하고 친근한 톤으로",
    "teasing":     "놀리는 듯한 장난스러운 톤으로",
}

EMOTION_PROMPTS_HD = {
    "neutral":     "자연스럽고 편안한 톤으로",
    "excited":     "정말 흥분되어서 목소리가 높아지고 빠르게, 열정이 넘치는 톤으로",
    "passionate":  "깊은 확신을 가지고 힘차게, 청중을 설득하듯이",
    "curious":     "고개를 갸웃거리며 정말 궁금해하는 듯한 톤으로, 약간 올라가는 억양으로",
    "skeptical":   "눈을 가늘게 뜨고 정말 의심스럽다는 듯이, 천천히 따져묻듯이",
    "frustrated":  "답답해서 한숨이 나오는 듯한 톤으로, 약간 짜증이 섞인 목소리로",
    "amused":      "웃음이 터지기 직전인 것처럼, 즐거워하며 가볍게",
    "sarcastic":   "비꼬는 듯한 톤으로, 약간 느리게 강조하면서",
    "defensive":   "자기 입장을 지키려고 단호하게, 약간 긴장된 톤으로",
    "aggressive":  "강하게 밀어붙이듯이, 목소리에 힘을 주고 빠르게",
    "reflective":  "깊이 생각하면서 천천히, 사려 깊고 차분한 톤으로",
    "surprised":   "정말 놀라서 눈이 커진 것처럼, 갑자기 목소리가 높아지며",
    "dismissive":  "별것 아니라는 듯이 가볍게 무시하는 톤으로",
    "empathetic":  "상대방의 마음을 이해하며 따뜻하고 부드럽게",
    "triumphant":  "이겼다는 듯이 자신만만하게, 승리감에 찬 톤으로",
    "conceding":   "인정하며 약간 아쉬운 듯이, 양보하는 부드러운 톤으로",
    "warm":        "따뜻한 미소가 느껴지는 친근하고 포근한 톤으로",
    "teasing":     "장난스럽게 놀리듯이, 웃음이 섞인 가벼운 톤으로",
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
    JSON의 hosts 정보를 읽어서 성별에 맞는 음성을 배정.
    같은 음성이 두 호스트에 배정되지 않도록 보장.
    """

    def __init__(self):
        self.assignments: dict[str, GeminiVoiceConfig] = {}

    def assign_voices(self, hosts: dict | list) -> dict[str, GeminiVoiceConfig]:
        """
        Args:
            hosts: JSON의 podcast.hosts
                리스트 형식: [{"id": "host_1", "name": "민수", "gender": "male", ...}, ...]
                딕셔너리 형식: {"host_1": {"name": "민수", "gender": "male", ...}, ...}

        Returns:
            {"host_1": GeminiVoiceConfig(...), ...}
        """
        # ✅ 리스트 → 딕셔너리 변환
        if isinstance(hosts, list):
            hosts_dict = {}
            for h in hosts:
                host_id = h.get("id")
                if not host_id:
                    raise ValueError(f"호스트에 'id' 필드가 없습니다: {h}")
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
                    f"지원하지 않는 성별: {gender} (host: {name}). "
                    f"지원: {list(AVAILABLE_VOICES.keys())}"
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
                    f"'{name}'에게 배정할 {gender} 음성이 부족합니다."
                )

        return self.assignments

    def print_assignments(self, hosts: dict | list):
        """배정 결과 출력"""
        # ✅ 리스트 → 딕셔너리 변환
        if isinstance(hosts, list):
            hosts_dict = {h["id"]: h for h in hosts}
        else:
            hosts_dict = hosts

        print("🎤 음성 배정 결과:")
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
        assert len(voice_names) == len(set(voice_names)), "❌ 음성 중복 발견!"
        print("  ✅ 중복 없음 확인 완료")

    def print_assignments(self, hosts):
        """배정 결과 출력"""
        if isinstance(hosts, list):
            hosts_dict = {h["id"]: h for h in hosts}
        else:
            hosts_dict = hosts

        print("🎤 음성 배정 결과:")
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
        assert len(voice_names) == len(set(voice_names)), "❌ 음성 중복 발견!"
        print("  ✅ 중복 없음 확인 완료")


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

    def make_key(self, text: str, voice_name: str, emotion: str, quality: str) -> str:
        """텍스트 + 실제 음성 이름 + 감정 + 품질로 해시"""
        content = f"{quality}|{voice_name}|{emotion}|{text}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    def get(self, text: str, voice_name: str, emotion: str, quality: str) -> Path | None:
        key = self.make_key(text, voice_name, emotion, quality)
        if key in self.index:
            cached_path = Path(self.index[key]["path"])
            if cached_path.exists() and cached_path.stat().st_size > 1000:
                return cached_path
            else:
                del self.index[key]
                self._save_index()
        return None

    def put(self, text: str, voice_name: str, emotion: str, quality: str, wav_path: Path) -> Path:
        key = self.make_key(text, voice_name, emotion, quality)
        cache_file = self.cache_dir / f"{key}.wav"
        if wav_path != cache_file:
            shutil.copy2(str(wav_path), str(cache_file))
        self.index[key] = {
            "path": str(cache_file),
            "voice_name": voice_name,
            "emotion": emotion,
            "quality": quality,
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
                print(f"    ⏳ 분당 제한 도달. {wait_time:.1f}초 대기중...")
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
        quality: str = "standard",
    ):
        """
        Args:
            hosts: JSON의 podcast.hosts 딕셔너리
            api_key: Google AI API 키
            cache_dir: 캐시 디렉토리
            quality: "standard" or "hd"
        """
        resolved_key = api_key or os.getenv("GOOGLE_API_KEY")
        if not resolved_key:
            raise ValueError(
                "API 키를 찾을 수 없습니다.\n"
                "  1. .env 파일에 GOOGLE_API_KEY=... 추가\n"
                "  2. export GOOGLE_API_KEY=... 실행\n"
                "  3. GeminiTTSClient(api_key='...') 직접 전달"
            )

        self.quality = quality or os.getenv("TTS_QUALITY", "standard")
        self.model = "gemini-2.5-flash-preview-tts"
        self.hosts = hosts

        self.client = genai.Client(api_key=resolved_key)
        self.rate_limiter = RateLimiter(max_per_minute=9, max_retries=8)
        self.cache = AudioCache(cache_dir=cache_dir)
        self.failed: list[dict] = []

        # ✅ 성별 기반 음성 자동 배정
        self.assigner = VoiceAssigner()
        self.voice_map = self.assigner.assign_voices(hosts)

        cache_stats = self.cache.stats()
        print(f"🔑 API 키 로드 완료 (끝 4자리: ...{resolved_key[-4:]})")
        print(f"🎚️  음질: {'HD' if self.quality == 'hd' else 'Standard'}")
        print(f"🤖 모델: {self.model}")
        print(
            f"📦 캐시: {cache_stats['entries']}개 항목, "
            f"{cache_stats['size_mb']}MB"
        )
        self.assigner.print_assignments(hosts)

    def _get_voice(self, speaker: str) -> GeminiVoiceConfig:
        """호스트 ID로 배정된 음성 반환"""
        if speaker not in self.voice_map:
            raise ValueError(
                f"알 수 없는 화자: {speaker}. "
                f"등록된 화자: {list(self.voice_map.keys())}"
            )
        return self.voice_map[speaker]

    def _get_host_info(self, speaker: str) -> dict:
        """호스트 정보 조회 (리스트/딕셔너리 모두 지원)"""
        if isinstance(self.hosts, list):
            for h in self.hosts:
                if h.get("id") == speaker:
                    return h
            return {}
        else:
            return self.hosts.get(speaker, {})

    def _build_prompt(self, text: str, speaker: str, emotion: str) -> str:
        host_info = self._get_host_info(speaker)
        name = host_info.get("name", speaker)

        if self.quality == "hd":
            gender_word = "남성" if host_info.get("gender") == "male" else "여성"
            role = host_info.get("role", "패널")
            personality = host_info.get("personality", "")
            debate_style = host_info.get("debate_style", "")
            expertise = ", ".join(host_info.get("expertise", []))

            parts = []

            # 캐릭터 설정
            char_desc = f"당신은 '{name}'입니다. {gender_word} {role}."
            if debate_style:
                char_desc += f" 토론 스타일: {debate_style}."
            if expertise:
                char_desc += f" 전문 분야: {expertise}."
            if personality:
                char_desc += f" {personality}"
            parts.append(char_desc)

            # 감정
            emotion_instruction = EMOTION_PROMPTS_HD.get(emotion, "")
            if emotion_instruction:
                parts.append(f"다음 대사를 {emotion_instruction} 말하세요.")

            parts.append(f'대사: "{text}"')
            return "\n".join(parts)
        else:
            emotion_instruction = EMOTION_PROMPTS.get(emotion, "")
            if emotion_instruction:
                return f"{emotion_instruction} 다음을 말하세요: {text}"
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

        # 1. 캐시 확인 (실제 voice_name 기반)
        cached = self.cache.get(text, voice_cfg.voice_name, emotion, self.quality)
        if cached:
            if cached != output_path:
                shutil.copy2(str(cached), str(output_path))
            key_short = self.cache.make_key(text, voice_cfg.voice_name, emotion, self.quality)[:8]
            print(f"    ♻️  캐시 재사용: {key_short}...")
            return output_path

        # 2. 프롬프트 구성
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

                # 응답 검증
                if (
                    not response.candidates
                    or not response.candidates[0].content
                    or not response.candidates[0].content.parts
                    or not response.candidates[0].content.parts[0].inline_data
                    or not response.candidates[0].content.parts[0].inline_data.data
                ):
                    raise EmptyResponseError("빈 응답 수신")

                audio_data = (
                    response.candidates[0]
                    .content.parts[0]
                    .inline_data.data
                )

                if len(audio_data) < 1000:
                    raise EmptyResponseError(f"오디오 너무 작음 ({len(audio_data)}B)")

                self._save_wav(audio_data, output_path)
                self.cache.put(text, voice_cfg.voice_name, emotion, self.quality, output_path)
                return output_path

            except (EmptyResponseError, AttributeError, TypeError, IndexError) as e:
                last_error = e
                wait = min(5 * (2 ** (attempt - 1)), 60) + attempt
                print(
                    f"    🔄 빈/파싱 에러 (시도 {attempt}/"
                    f"{self.rate_limiter.max_retries}): {e}. "
                    f"{wait:.0f}초 대기..."
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
                        error_type = "서버 에러"

                    wait = base_delay + attempt
                    print(
                        f"    🔄 {error_type} (시도 {attempt}/"
                        f"{self.rate_limiter.max_retries}). "
                        f"{wait:.0f}초 대기..."
                    )
                    time.sleep(wait)
                else:
                    print(f"    ❌ 복구 불가: {e}")
                    self.failed.append({
                        "text": text, "speaker": speaker,
                        "voice_name": voice_cfg.voice_name,
                        "emotion": emotion, "quality": self.quality,
                        "output_path": str(output_path),
                        "error": str(e),
                    })
                    return None

        print(f"    ❌ {self.rate_limiter.max_retries}회 재시도 모두 실패. 스킵.")
        self.failed.append({
            "text": text, "speaker": speaker,
            "voice_name": voice_cfg.voice_name,
            "emotion": emotion, "quality": self.quality,
            "output_path": str(output_path),
            "error": str(last_error),
        })
        return None

    def save_failed_log(self, path: str | Path = "./tts_cache/failed.json"):
        path = Path(path)
        if self.failed:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.failed, f, ensure_ascii=False, indent=2)
            print(f"\n💾 실패 로그 저장: {path} ({len(self.failed)}건)")

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
