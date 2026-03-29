# src/openpodcast_tts/chirp3_tts.py

"""
Chirp 3: HD Voices — Google Cloud Text-to-Speech API
Completely separate from Gemini API.

Auth: GOOGLE_APPLICATION_CREDENTIALS env var pointing to service account JSON
"""

import re
import json
import hashlib
import os
import shutil
import time
import wave
from pathlib import Path
from dataclasses import dataclass

from dotenv import load_dotenv
from google.cloud import texttospeech

load_dotenv()
# Patterns that need SSML breaks
SSML_TRIGGERS = re.compile(r'[,，!！?？.。~…—–\-]|\.{2,}')

def prepare_text(text: str) -> tuple[str, bool]:
    """
    텍스트 정리 후, SSML이 필요하면 SSML 반환.
    
    Returns:
        (processed_text, is_ssml)
    """
    # 1. 괄호 지시어 제거
    clean = clean_text_for_tts(text)
    clean = re.sub(r'[,，]\s*', r', <break time="25ms"/> ', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean, False
    
    # 2. 구두점이 없으면 plain text 그대로
    if not SSML_TRIGGERS.search(clean):
        return clean, False
    
    # 3. 구두점 있으면 SSML break 삽입
    ssml = clean
    
    # 말줄임표 먼저 (. 치환보다 앞서야 함)
    ssml = re.sub(r'\.{2,}', r'<break time="60ms"/>', ssml)
    ssml = re.sub(r'…', r'<break time="60ms"/>', ssml)
    
    # 마침표
    ssml = re.sub(r'\.(\s)', r'.<break time="40ms"/>\1', ssml)
    ssml = re.sub(r'\.$', r'.<break time="40ms"/>', ssml)
    
    # 쉼표
    ssml = re.sub(r'[,，]\s*', r', <break time="25ms"/> ', ssml)
    
    # 물음표
    ssml = re.sub(r'[?？]\s*', r'? <break time="35ms"/> ', ssml)
    
    # 느낌표
    ssml = re.sub(r'[!！]\s*', r'! <break time="30ms"/> ', ssml)
    
    # 물결표
    ssml = re.sub(r'~', r'<break time="20ms"/>', ssml)
    
    # 대시
    ssml = re.sub(r'\s*[—–\-]{1,2}\s*', r' <break time="30ms"/> ', ssml)
    
    # 연속 공백 정리
    ssml = re.sub(r'\s+', ' ', ssml).strip()
    
    return f'<speak>{ssml}</speak>', True

def clean_text_for_tts(text: str) -> str:
    """
    TTS에 보내기 전 텍스트 정리.
    괄호 안 무음 지시어/이모티콘 제거, 발화 가능한 텍스트만 유지.
    
    제거 대상:
    (잠깐 침묵), (ㅋㅋㅋ), (하하), (웃음), (박수),
    (한숨), (사이), (비트), (pause), ...
    
    유지 대상:
    괄호 없는 일반 텍스트
    """
    # 1. 괄호 안 내용 제거: (잠깐 침묵), (ㅋㅋㅋ), (하하하), (웃음) 등
    text = re.sub(r'\([^)]*\)', '', text)
    
    # 2. 대괄호 안 내용 제거: [웃음], [박수] 등
    text = re.sub(r'\[[^\]]*\]', '', text)
    
    # 3. 중괄호 안 내용 제거: {효과음} 등
    text = re.sub(r'\{[^}]*\}', '', text)
    
    # 4. 연속 공백 정리
    text = re.sub(r'\s+', ' ', text)
    
    # 5. 앞뒤 공백 제거
    text = text.strip()
    
    # 6. 빈 문자열 방지
    if not text:
        text = "..."
    
    return text


@dataclass
class Chirp3VoiceConfig:
    voice_name: str       # Full name: "ko-KR-Chirp3-HD-Achird"
    language_code: str
    short_name: str       # Display: "Achird"
    description: str


CHIRP3_HD_VOICES = {
    "male": [
        Chirp3VoiceConfig("ko-KR-Chirp3-HD-Achird",  "ko-KR", "Achird",  "HD 차분하고 명확한 남성"),
        Chirp3VoiceConfig("ko-KR-Chirp3-HD-Algenib", "ko-KR", "Algenib", "HD 깊고 무게감 있는 남성"),
        Chirp3VoiceConfig("ko-KR-Chirp3-HD-Algieba", "ko-KR", "Algieba", "HD 따뜻하고 친근한 남성"),
        Chirp3VoiceConfig("ko-KR-Chirp3-HD-Alnilam", "ko-KR", "Alnilam", "HD 에너지 넘치는 남성"),
        Chirp3VoiceConfig("ko-KR-Chirp3-HD-Schedar", "ko-KR", "Schedar", "HD 부드럽고 안정적인 남성"),
        Chirp3VoiceConfig("ko-KR-Chirp3-HD-Zubenelgenubi", "ko-KR", "Zubenelgenubi", "HD 밝고 활기찬 남성"),
    ],
    "female": [
        Chirp3VoiceConfig("ko-KR-Chirp3-HD-Achernar",   "ko-KR", "Achernar",   "HD 밝고 또렷한 여성"),
        Chirp3VoiceConfig("ko-KR-Chirp3-HD-Alkes",      "ko-KR", "Alkes",      "HD 부드럽고 따뜻한 여성"),
        Chirp3VoiceConfig("ko-KR-Chirp3-HD-Autonoe",    "ko-KR", "Autonoe",    "HD 차분하고 지적인 여성"),
        Chirp3VoiceConfig("ko-KR-Chirp3-HD-Callirrhoe", "ko-KR", "Callirrhoe", "HD 경쾌하고 활기찬 여성"),
        Chirp3VoiceConfig("ko-KR-Chirp3-HD-Dione",      "ko-KR", "Dione",      "HD 깊고 안정적인 여성"),
        Chirp3VoiceConfig("ko-KR-Chirp3-HD-Erinome",    "ko-KR", "Erinome",    "HD 명확하고 자신감 있는 여성"),
        Chirp3VoiceConfig("ko-KR-Chirp3-HD-Laomedeia",  "ko-KR", "Laomedeia",  "HD 부드럽고 공감적인 여성"),
        Chirp3VoiceConfig("ko-KR-Chirp3-HD-Sulafat",    "ko-KR", "Sulafat",    "HD 밝고 친근한 여성"),
    ],
}

CHIRP3_ROLE_PREFERENCE = {
    "male": {
        "진행자":  ["Achird", "Schedar", "Algieba"],
        "분석가":  ["Algenib", "Achird", "Schedar"],
        "토론자":  ["Alnilam", "Zubenelgenubi", "Algenib"],
        "중재자":  ["Algieba", "Schedar", "Achird"],
        "default": ["Achird", "Algenib", "Alnilam", "Algieba", "Schedar", "Zubenelgenubi"],
    },
    "female": {
        "진행자":  ["Autonoe", "Dione", "Achernar"],
        "분석가":  ["Erinome", "Achernar", "Autonoe"],
        "토론자":  ["Callirrhoe", "Achernar", "Erinome"],
        "중재자":  ["Laomedeia", "Alkes", "Dione"],
        "default": ["Achernar", "Alkes", "Autonoe", "Callirrhoe", "Dione", "Erinome", "Laomedeia", "Sulafat"],
    },
}

RETRYABLE_ERRORS = ["500", "503", "INTERNAL", "UNAVAILABLE", "DEADLINE_EXCEEDED"]


class Chirp3VoiceAssigner:
    def __init__(self):
        self.assignments: dict[str, Chirp3VoiceConfig] = {}

    def assign_voices(self, hosts: dict | list) -> dict[str, Chirp3VoiceConfig]:
        if isinstance(hosts, list):
            hosts_dict = {h["id"]: h for h in hosts}
        else:
            hosts_dict = hosts

        self.assignments = {}
        used: set[str] = set()

        for host_id in sorted(hosts_dict.keys()):
            info = hosts_dict[host_id]
            gender = info.get("gender", "male").lower()
            role = info.get("role", "default")
            name = info.get("name", host_id)

            if gender not in CHIRP3_HD_VOICES:
                raise ValueError(f"지원하지 않는 성별: {gender} ({name})")

            prefs = CHIRP3_ROLE_PREFERENCE.get(gender, {})
            order = prefs.get(role, prefs.get("default", []))

            assigned = False
            for short_name in order:
                if short_name not in used:
                    voice = next(
                        (v for v in CHIRP3_HD_VOICES[gender] if v.short_name == short_name),
                        None,
                    )
                    if voice:
                        self.assignments[host_id] = voice
                        used.add(short_name)
                        assigned = True
                        break

            if not assigned:
                for voice in CHIRP3_HD_VOICES[gender]:
                    if voice.short_name not in used:
                        self.assignments[host_id] = voice
                        used.add(voice.short_name)
                        assigned = True
                        break

            if not assigned:
                raise ValueError(f"'{name}'에게 배정할 {gender} HD 음성 부족")

        return self.assignments

    def print_assignments(self, hosts):
        if isinstance(hosts, list):
            hosts_dict = {h["id"]: h for h in hosts}
        else:
            hosts_dict = hosts

        print("🎤 음성 배정 결과 [🔊 Chirp 3 HD]:")
        print(f"{'─' * 65}")
        for host_id in sorted(self.assignments.keys()):
            info = hosts_dict[host_id]
            voice = self.assignments[host_id]
            ge = "👨" if info.get("gender") == "male" else "👩"
            print(
                f"  {ge} {info['name']:4s} ({info.get('role','?'):4s}) "
                f"→ {voice.short_name:16s} | {voice.description}"
            )
        print(f"{'─' * 65}")
        names = [v.short_name for v in self.assignments.values()]
        assert len(names) == len(set(names)), "❌ 음성 중복!"
        print("  ✅ 중복 없음 확인 완료")


class Chirp3AudioCache:
    """Chirp 3 전용 캐시"""

    def __init__(self, cache_dir: str | Path = "./tts_cache"):
        self.cache_dir = Path(cache_dir) / "chirp3_hd"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.cache_dir / "cache_index.json"
        self.index = self._load_index()

    def _load_index(self) -> dict:
        if self.index_path.exists():
            with open(self.index_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_index(self):
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(self.index, f, ensure_ascii=False, indent=2)

    def make_key(self, text: str, voice_name: str) -> str:
        content = f"chirp3hd|{voice_name}|{text}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    def get(self, text: str, voice_name: str) -> Path | None:
        key = self.make_key(text, voice_name)
        if key in self.index:
            p = Path(self.index[key]["path"])
            if p.exists() and p.stat().st_size > 1000:
                return p
            del self.index[key]
            self._save_index()
        return None

    def put(self, text: str, voice_name: str, wav_path: Path) -> Path:
        key = self.make_key(text, voice_name)
        cache_file = self.cache_dir / f"{key}.wav"
        if wav_path != cache_file:
            shutil.copy2(str(wav_path), str(cache_file))
        self.index[key] = {
            "path": str(cache_file),
            "voice_name": voice_name,
            "text": text[:80] + ("..." if len(text) > 80 else ""),
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._save_index()
        return cache_file

    def stats(self) -> dict:
        total = len(self.index)
        size = sum(
            Path(v["path"]).stat().st_size
            for v in self.index.values()
            if Path(v["path"]).exists()
        )
        return {"entries": total, "size_mb": round(size / 1024 / 1024, 1)}


class Chirp3HDClient:
    """
    Google Cloud Text-to-Speech Chirp 3 HD 클라이언트
    
    Same interface as GeminiTTSClient:
      .synthesize(text, speaker, emotion, output_path) -> Path | None
      .voice_map: dict[str, config]
      .save_failed_log()
      .get_audio_duration_ms(path) -> int
    """

    MAX_RETRIES = 5

    def __init__(
        self,
        hosts: dict | list,
        cache_dir: str | Path = "./tts_cache",
    ):
        # Auth check
        creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if not creds:
            raise ValueError(
                "Chirp 3 HD에는 Google Cloud 인증이 필요합니다.\n"
                "  1. 서비스 계정 JSON 다운로드\n"
                "  2. .env에 GOOGLE_APPLICATION_CREDENTIALS=경로 추가\n"
                "  3. 또는: gcloud auth application-default login"
            )

        self.client = texttospeech.TextToSpeechClient()
        self.hosts = hosts
        self.cache = Chirp3AudioCache(cache_dir=cache_dir)
        self.failed: list[dict] = []

        # 음성 배정
        self.assigner = Chirp3VoiceAssigner()
        self.voice_map = self.assigner.assign_voices(hosts)

        cache_stats = self.cache.stats()
        print(f"☁️  Google Cloud Text-to-Speech (Chirp 3 HD)")
        print(f"🔐 인증: {Path(creds).name}")
        print(f"📦 캐시: {cache_stats['entries']}개 항목, {cache_stats['size_mb']}MB")
        self.assigner.print_assignments(hosts)

    def synthesize(
        self,
        text: str,
        speaker: str,
        emotion: str = "neutral",
        output_path: str | Path = "output.wav",
    ) -> Path | None:
        output_path = Path(output_path)
        voice_cfg = self.voice_map[speaker]

        # 캐시 확인
        cached = self.cache.get(text, voice_cfg.voice_name)
        if cached:
            if cached != output_path:
                shutil.copy2(str(cached), str(output_path))
            key_short = self.cache.make_key(text, voice_cfg.voice_name)[:8]
            print(f"    ♻️  캐시 재사용: {key_short}...")
            return output_path

        # ✅ 텍스트 준비 — 필요할 때만 SSML
        processed, is_ssml = prepare_text(text)

        if not processed or processed == "..." or len(processed) < 2:
            print(f"    ⏭️  빈 텍스트 스킵: '{text[:30]}...'")
            return None

        # ✅ SSML 또는 plain text 선택
        if is_ssml:
            synthesis_input = texttospeech.SynthesisInput(ssml=processed)
        else:
            synthesis_input = texttospeech.SynthesisInput(text=processed)

        voice_params = texttospeech.VoiceSelectionParams(
            language_code=voice_cfg.language_code,
            name=voice_cfg.voice_name,
        )

        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
            sample_rate_hertz=24000,
        )

        last_error = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                response = self.client.synthesize_speech(
                    input=synthesis_input,
                    voice=voice_params,
                    audio_config=audio_config,
                )

                if not response.audio_content or len(response.audio_content) < 1000:
                    raise ValueError(f"오디오 데이터 부족 ({len(response.audio_content or b'')}B)")

                with open(output_path, "wb") as f:
                    f.write(response.audio_content)

                self.cache.put(text, voice_cfg.voice_name, output_path)
                return output_path

            except Exception as e:
                last_error = e
                error_str = str(e)
                is_retryable = any(code in error_str for code in RETRYABLE_ERRORS)

                if is_retryable:
                    wait = min(5 * (2 ** (attempt - 1)), 60) + attempt
                    print(
                        f"    🔄 Cloud TTS 에러 (시도 {attempt}/{self.MAX_RETRIES}): "
                        f"{error_str[:80]}. {wait:.0f}초 대기..."
                    )
                    time.sleep(wait)
                else:
                    print(f"    ❌ 복구 불가: {e}")
                    self.failed.append({
                        "text": text, "processed": processed,
                        "is_ssml": is_ssml, "speaker": speaker,
                        "voice_name": voice_cfg.voice_name,
                        "output_path": str(output_path),
                        "error": str(e),
                    })
                    return None

        print(f"    ❌ {self.MAX_RETRIES}회 재시도 모두 실패. 스킵.")
        self.failed.append({
            "text": text, "processed": processed,
            "is_ssml": is_ssml, "speaker": speaker,
            "voice_name": voice_cfg.voice_name,
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

    def get_audio_duration_ms(self, wav_path: str | Path) -> int:
        with wave.open(str(wav_path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            return int(frames / rate * 1000)
