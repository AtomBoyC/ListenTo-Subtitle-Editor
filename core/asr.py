"""Dual-engine Speech-to-Subtitle (ASR) module supporting OpenAI-compatible Audio APIs & local faster-whisper."""

from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import httpx

from core.models import APISettings, ProviderPreset, SubtitleCue, SubtitleDocument
from core.parser import detect_subtitle_language, parse_subtitle_content
from core.translator import LLMTranslationError, _is_local_url

_SENSEVOICE_TAG_RE = re.compile(r"<\|[^|>]+\|>")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;\n])\s*|(?<=[，,])\s+(?=[A-Z0-9])")


def has_local_faster_whisper() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def get_asr_presets(in_docker: bool = False) -> List[ProviderPreset]:
    local_whisper_url = "http://host.docker.internal:8000/v1" if in_docker else "http://localhost:8000/v1"
    presets = [
        ProviderPreset(
            id="siliconflow",
            name="硅基流动 (SenseVoice 极速识别)",
            badge="国内免费/极速首选",
            base_url="https://api.siliconflow.cn/v1",
            default_model="FunAudioLLM/SenseVoiceSmall",
            models=[
                "FunAudioLLM/SenseVoiceSmall",
                "TeleAI/TeleSpeechASR",
            ],
            requires_key=True,
            doc_hint="阿里 SenseVoiceSmall 在硅基流动免费/极低价开放，支持中英日韩粤极速高精度转写（可复用硅基流动 API Key）。",
        ),
        ProviderPreset(
            id="groq",
            name="Groq Cloud (Whisper Large V3 Turbo)",
            badge="秒级时间轴识别",
            base_url="https://api.groq.com/openai/v1",
            default_model="whisper-large-v3-turbo",
            models=[
                "whisper-large-v3-turbo",
                "whisper-large-v3",
            ],
            requires_key=True,
            doc_hint="返回原生逐句精确时间轴（verbose_json），10 分钟音视频仅需数秒即可转出完整时间轴字幕。",
        ),
        ProviderPreset(
            id="openai",
            name="OpenAI Audio (Whisper / GPT-4o)",
            badge="高精度多语种",
            base_url="https://api.openai.com/v1",
            default_model="whisper-1",
            models=[
                "whisper-1",
                "gpt-4o-mini-transcribe",
                "gpt-4o-transcribe",
            ],
            requires_key=True,
            doc_hint="OpenAI 官方语音识别接口（whisper-1 原生支持逐段精确毫秒时间轴）。",
        ),
        ProviderPreset(
            id="local_api",
            name="本地 Whisper API (Speaches / vLLM)",
            badge="本地服务免 Key",
            base_url=local_whisper_url,
            default_model="Systran/faster-whisper-large-v3",
            models=[
                "Systran/faster-whisper-large-v3",
                "Systran/faster-whisper-small",
                "whisper-1",
            ],
            requires_key=False,
            doc_hint="连接本机或局域网部署的兼容 /v1/audio/transcriptions 服务（如 speaches / faster-whisper-server）。",
        ),
    ]
    if has_local_faster_whisper():
        presets.append(
            ProviderPreset(
                id="local_faster_whisper",
                name="本机离线 Faster-Whisper 引擎",
                badge="纯离线内置",
                base_url="local://faster-whisper",
                default_model="base",
                models=["tiny", "base", "small", "medium", "large-v3-turbo"],
                requires_key=False,
                doc_hint="已检测到本机安装 faster-whisper，可完全断网使用本地 CPU/GPU 提取字幕。",
            )
        )
    return presets


def _build_transcription_endpoint(base_url: str) -> str:
    cleaned = (base_url or "").strip().rstrip("/")
    if not cleaned:
        raise LLMTranslationError("请填写语音识别 API Base URL。")
    parsed = urlsplit(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise LLMTranslationError("语音识别 API Base URL 必须以 https:// 或 http:// 开头。")
    if cleaned.endswith("/audio/transcriptions"):
        return cleaned
    if cleaned.endswith("/chat/completions"):
        cleaned = cleaned[: -len("/chat/completions")]
    return f"{cleaned}/audio/transcriptions"


def _clean_asr_text(raw: str) -> str:
    cleaned = _SENSEVOICE_TAG_RE.sub("", raw or "")
    return re.sub(r"[ \t]+", " ", cleaned).strip()


def _segment_plain_transcript(text: str, total_duration_sec: float = 0.0) -> List[SubtitleCue]:
    """Split unsegmented ASR transcript into natural subtitle cues with proportional timestamps."""
    cleaned = _clean_asr_text(text)
    if not cleaned:
        return []

    # Split on major sentence boundaries first, then further split overly long clauses
    rough_parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(cleaned) if p and p.strip()]
    chunks: List[str] = []
    for part in rough_parts:
        if len(part) <= 42:
            chunks.append(part)
        else:
            sub_parts = re.split(r"(?<=[，,、：:])\s*", part)
            buf = ""
            for sp in sub_parts:
                if len(buf) + len(sp) <= 38:
                    buf += sp
                else:
                    if buf.strip():
                        chunks.append(buf.strip())
                    buf = sp
            if buf.strip():
                chunks.append(buf.strip())

    if not chunks:
        chunks = [cleaned]

    total_chars = max(1, sum(len(c) for c in chunks))
    est_total_ms = int(total_duration_sec * 1000) if total_duration_sec > 1.0 else max(3000, int(total_chars * 220))

    cues: List[SubtitleCue] = []
    cursor_ms = 200
    usable_span_ms = max(2000, est_total_ms - 300)

    for idx, chunk in enumerate(chunks, start=1):
        weight = len(chunk) / total_chars
        dur_ms = max(900, int(usable_span_ms * weight))
        start_ms = cursor_ms
        end_ms = min(est_total_ms, start_ms + dur_ms - 60) if total_duration_sec > 1.0 else (start_ms + dur_ms - 60)
        if end_ms <= start_ms:
            end_ms = start_ms + 800
        # Strip trailing single Chinese period for clean subtitle aesthetic
        clean_line = chunk[:-1] if (chunk.endswith("。") and not chunk.endswith("。。")) else chunk
        cues.append(
            SubtitleCue(
                id=idx,
                start_ms=start_ms,
                end_ms=end_ms,
                source_text=clean_line.strip() or chunk,
            )
        )
        cursor_ms = end_ms + 60
    return cues


def _transcribe_local_faster_whisper(
    media_bytes: bytes,
    filename: str,
    model_name: str,
    language: str,
) -> List[SubtitleCue]:
    from faster_whisper import WhisperModel  # type: ignore

    suffix = Path(filename).suffix or ".mp3"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(media_bytes)
        tmp_path = Path(tmp.name)
    try:
        model = WhisperModel(model_name or "base", device="cpu", compute_type="int8")
        lang_arg = None if language in ("", "auto") else language
        segments, _ = model.transcribe(str(tmp_path), language=lang_arg, beam_size=5, vad_filter=True)
        cues: List[SubtitleCue] = []
        for seg in segments:
            txt = _clean_asr_text(seg.text)
            if not txt:
                continue
            start_ms = max(0, int(round(seg.start * 1000)))
            end_ms = max(start_ms + 200, int(round(seg.end * 1000)))
            cues.append(SubtitleCue(id=len(cues) + 1, start_ms=start_ms, end_ms=end_ms, source_text=txt))
        return cues
    finally:
        tmp_path.unlink(missing_ok=True)


async def transcribe_media_to_document(
    media_bytes: bytes,
    filename: str,
    settings: APISettings,
    language: str = "auto",
    duration_sec: float = 0.0,
    asr_base_url: Optional[str] = None,
    asr_api_key: Optional[str] = None,
    asr_model: Optional[str] = None,
    asr_provider_id: Optional[str] = None,
) -> SubtitleDocument:
    """Transcribe uploaded audio/video bytes into a timed SubtitleDocument."""
    if not media_bytes:
        raise LLMTranslationError("上传的音视频文件为空。")

    provider = (asr_provider_id or settings.asr_provider_id or "siliconflow").strip()
    model = (asr_model or settings.asr_model or "FunAudioLLM/SenseVoiceSmall").strip()

    if provider == "local_faster_whisper" or (asr_base_url or settings.asr_base_url).startswith("local://"):
        if not has_local_faster_whisper():
            raise LLMTranslationError("当前环境未安装 faster-whisper 库，请切换为硅基流动 / Groq / OpenAI 云端极速识别。")
        cues = _transcribe_local_faster_whisper(media_bytes, filename, model, language)
    else:
        base_url = (asr_base_url or settings.asr_base_url or "https://api.siliconflow.cn/v1").strip()
        endpoint = _build_transcription_endpoint(base_url)
        is_local = _is_local_url(endpoint)

        # Smart key fallback: explicit form key -> saved asr_api_key -> main api_key if same host!
        key = (asr_api_key or "").strip() or settings.asr_api_key.strip()
        if not key and settings.api_key.strip():
            asr_host = (urlsplit(endpoint).hostname or "").lower()
            llm_host = (urlsplit(settings.base_url).hostname or "").lower()
            if asr_host and asr_host == llm_host:
                key = settings.api_key.strip()

        if not is_local and not key:
            raise LLMTranslationError("请填写语音识别服务的 API Key（若使用硅基流动，可直接复用硅基流动密钥）。")

        headers: Dict[str, str] = {}
        if key:
            headers["Authorization"] = f"Bearer {key}"

        is_sensevoice = "sensevoice" in model.lower() or "telespeech" in model.lower()
        formats_to_try = ["json"] if is_sensevoice else ["verbose_json", "srt", "json"]

        resp: Optional[httpx.Response] = None
        used_fmt = "json"
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(180.0, connect=15.0),
            trust_env=not is_local,
            follow_redirects=True,
        ) as client:
            for fmt in formats_to_try:
                used_fmt = fmt
                form_data: Dict[str, str] = {"model": model, "response_format": fmt}
                if language and language != "auto" and not is_sensevoice:
                    form_data["language"] = language.split("-")[0]
                files = {"file": (filename or "audio.mp3", media_bytes, "application/octet-stream")}
                try:
                    resp = await client.post(endpoint, headers=headers, data=form_data, files=files)
                except httpx.RequestError as exc:
                    raise LLMTranslationError(f"无法连接语音识别接口（{endpoint}）：{exc}") from exc

                if resp.status_code < 400:
                    break
                # Retry next response_format if provider doesn't support verbose_json / srt
                if resp.status_code in (400, 422) and fmt != formats_to_try[-1]:
                    continue
                break

        assert resp is not None
        if resp.status_code in (401, 403):
            raise LLMTranslationError("语音识别 API Key 无效或无权限（HTTP 401/403）。")
        if resp.status_code >= 400:
            raise LLMTranslationError(f"语音识别请求失败（HTTP {resp.status_code}）：{resp.text[:220]}")

        cues = []
        raw_text_body = resp.text.strip()
        if used_fmt == "srt" or raw_text_body.startswith("1\n") or "-->" in raw_text_body[:120]:
            parsed_doc = parse_subtitle_content(raw_text_body, f"{Path(filename).stem}.srt", "utf-8")
            cues = parsed_doc.cues
        else:
            payload = resp.json()
            segments = payload.get("segments") if isinstance(payload, dict) else None
            if isinstance(segments, list) and segments:
                for seg in segments:
                    if not isinstance(seg, dict):
                        continue
                    txt = _clean_asr_text(str(seg.get("text", "")))
                    if not txt:
                        continue
                    start_ms = max(0, int(round(float(seg.get("start", 0.0)) * 1000)))
                    end_ms = max(start_ms + 250, int(round(float(seg.get("end", start_ms / 1000 + 1.5)) * 1000)))
                    cues.append(
                        SubtitleCue(
                            id=len(cues) + 1,
                            start_ms=start_ms,
                            end_ms=end_ms,
                            source_text=txt,
                        )
                    )
            elif isinstance(payload, dict) and str(payload.get("text", "")).strip():
                cues = _segment_plain_transcript(str(payload["text"]), total_duration_sec=duration_sec)

    if not cues:
        raise LLMTranslationError("未在音视频中识别到有效人声对白。")

    detected = detect_subtitle_language(cues) if language in ("", "auto") else language
    target_default = "en" if detected == "zh" else "zh"
    warnings: List[str] = []
    if len(cues) >= 1 and "sensevoice" in model.lower():
        warnings.append("已由 SenseVoice 极速转写并按语义自动断句对齐时间轴；可边播放视频边微调起止时间。")

    return SubtitleDocument(
        doc_id=uuid.uuid4().hex[:12],
        filename=f"{Path(filename).stem}.srt",
        source_format="srt",
        encoding="utf-8",
        source_language=detected,
        target_language=target_default,
        cues=cues,
        warnings=warnings,
    )
