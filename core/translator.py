"""Context-aware & Reflective LLM Subtitle Translation Engine (inspired by VideoLingo & VideoCaptioner)."""

import asyncio
import ipaddress
import json
import re
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from core.models import (
    APISettings,
    GlossaryItem,
    SingleCueRewriteRequest,
    SubtitleCue,
    TranslateBatchRequest,
)

LANGUAGE_LABELS: Dict[str, str] = {
    "auto": "自动检测",
    "zh": "简体中文 (Simplified Chinese)",
    "zh-tw": "繁體中文 (Traditional Chinese)",
    "en": "英语 (English)",
    "ja": "日语 (Japanese)",
    "ko": "韩语 (Korean)",
    "fr": "法语 (French)",
    "de": "德语 (German)",
    "es": "西班牙语 (Spanish)",
    "ru": "俄语 (Russian)",
}

GENRE_GUIDES: Dict[str, str] = {
    "general": "通用自然口吻：准确传达原意，语言自然流畅，去除翻译腔，符合母语观看习惯。",
    "movie": "影视剧对白：注重人物性格、情绪张力与潜台词，台词简练上口，符合影视字幕阅读节奏。",
    "tech": "技术教程/演讲：专业术语准确规范（必要时保留通用英文缩写如 API/GPU），逻辑清晰易懂。",
    "anime": "动漫/番剧/游戏：贴合二次元与角色口癖语境，称呼与语气词自然生动，不生搬硬套。",
    "documentary": "纪录片/科普：用词考究、严谨克制、富有叙事质感。",
}

_THINK_TAG_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


class LLMTranslationError(RuntimeError):
    """User-facing error raised during LLM API calls or response parsing."""


def _is_local_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            return False
        if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal") or "." not in host or host.endswith((".local", ".internal")):
            return True
        return ipaddress.ip_address(host).is_loopback or ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def build_chat_endpoint(base_url: str) -> str:
    cleaned = (base_url or "").strip().rstrip("/")
    if not cleaned:
        raise LLMTranslationError("请先在「AI 引擎设置」中填写 API Base URL。")
    parsed = urlsplit(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise LLMTranslationError("API Base URL 格式不正确，需以 https:// 或 http:// 开头。")
    if cleaned.endswith("/chat/completions"):
        return cleaned
    return f"{cleaned}/chat/completions"


def build_models_endpoint(base_url: str) -> str:
    cleaned = (base_url or "").strip().rstrip("/")
    if not cleaned:
        raise LLMTranslationError("请先填写 API Base URL。")
    parsed = urlsplit(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise LLMTranslationError("API Base URL 格式不正确，需以 https:// 或 http:// 开头。")
    if cleaned.endswith("/chat/completions"):
        cleaned = cleaned[: -len("/chat/completions")]
    if cleaned.endswith("/models"):
        return cleaned
    return f"{cleaned}/models"


def _build_auth_headers(endpoint: str, api_key: str) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    key = (api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
        if "anthropic.com" in endpoint.lower():
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
    return headers


_NON_CHAT_MODEL_KEYWORDS = (
    "embedding",
    "whisper",
    "tts-",
    "tts1",
    "dall-e",
    "moderation",
    "rerank",
    "bge-",
    "canary-",
    "imagen-",
    "veo-",
)


async def fetch_available_models(settings: APISettings) -> Dict[str, Any]:
    """Query the provider's /models endpoint to discover all live available LLM models."""
    models_url = build_models_endpoint(settings.base_url)
    is_local = _is_local_url(models_url)
    is_public_catalog = "openrouter.ai" in models_url.lower()
    if not is_local and not is_public_catalog and not settings.api_key.strip():
        raise LLMTranslationError("请先填入 API Key，再点击「实时拉取模型」。")

    headers = _build_auth_headers(models_url, settings.api_key)
    headers.pop("Content-Type", None)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=10.0),
        trust_env=not is_local,
        follow_redirects=True,
    ) as client:
        try:
            resp = await client.get(models_url, headers=headers)
        except httpx.RequestError as exc:
            raise LLMTranslationError(f"无法连接模型列表接口（{models_url}）：{exc}") from exc

    if resp.status_code in (401, 403):
        raise LLMTranslationError("API Key 验证失败或无权限访问 /models 接口（HTTP 401/403）。")
    if resp.status_code >= 400:
        raise LLMTranslationError(f"服务商未返回模型列表（HTTP {resp.status_code}），你仍可直接在输入框手动填写任意模型名。")

    try:
        payload = resp.json()
    except Exception as exc:
        raise LLMTranslationError("模型列表接口返回的不是有效 JSON。") from exc

    raw_items: List[Any] = []
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            raw_items = payload["data"]
        elif isinstance(payload.get("models"), list):
            raw_items = payload["models"]
    elif isinstance(payload, list):
        raw_items = payload

    extracted: List[str] = []
    seen = set()
    for item in raw_items:
        mid = ""
        if isinstance(item, str):
            mid = item.strip()
        elif isinstance(item, dict):
            mid = str(item.get("id") or item.get("name") or "").strip()
        if not mid:
            continue
        if mid.startswith("models/") and "generativelanguage.googleapis.com" in models_url:
            mid = mid[len("models/") :]
        lower = mid.lower()
        if any(skip in lower for skip in _NON_CHAT_MODEL_KEYWORDS):
            continue
        if mid not in seen:
            seen.add(mid)
            extracted.append(mid)

    if not extracted:
        raise LLMTranslationError("接口返回了空的对话模型列表，请手动输入模型名称。")

    return {
        "models": extracted,
        "count": len(extracted),
        "endpoint": models_url,
    }


def extract_json_payload(raw_text: str) -> Any:
    """Robustly extract JSON object or array even when wrapped in <think> blocks or markdown fences."""
    if not raw_text or not raw_text.strip():
        raise LLMTranslationError("大模型返回了空内容。")

    cleaned = _THINK_TAG_RE.sub("", raw_text).strip()
    fence = _CODE_FENCE_RE.search(cleaned)
    if fence:
        cleaned = fence.group(1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Locate outermost JSON object or array
    obj_start = cleaned.find("{")
    obj_end = cleaned.rfind("}")
    if obj_start != -1 and obj_end > obj_start:
        candidate = cleaned[obj_start : obj_end + 1]
        candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    arr_start = cleaned.find("[")
    arr_end = cleaned.rfind("]")
    if arr_start != -1 and arr_end > arr_start:
        candidate = cleaned[arr_start : arr_end + 1]
        candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    raise LLMTranslationError(f"无法解析模型返回的 JSON 结构，请确认模型支持指令遵循。片段预览：{cleaned[:140]}")


async def _call_chat_completion(
    messages: List[Dict[str, str]],
    settings: APISettings,
    temperature: Optional[float] = None,
    use_json_mode: bool = True,
) -> str:
    endpoint = build_chat_endpoint(settings.base_url)
    is_local = _is_local_url(endpoint)
    if not is_local and not settings.api_key.strip():
        raise LLMTranslationError("尚未配置 API Key，请点击右上角「AI 引擎设置」填入你的密钥。")
    if not settings.model.strip():
        raise LLMTranslationError("尚未填写模型名称（例如 deepseek-chat 或 gpt-4o-mini）。")

    headers = _build_auth_headers(endpoint, settings.api_key)

    payload: Dict[str, Any] = {
        "model": settings.model.strip(),
        "messages": messages,
        "temperature": settings.temperature if temperature is None else temperature,
        "stream": False,
    }
    # Reasoning models or some local gateways reject response_format; we gracefully retry without it if 400.
    model_lower = settings.model.lower()
    supports_json_mode = use_json_mode and not any(
        tag in model_lower for tag in ("reasoner", "r1", "qwq", "thinking", "fable", "o1-", "o3", "o4", "grok-420")
    )
    if supports_json_mode and "anthropic.com" not in endpoint.lower():
        payload["response_format"] = {"type": "json_object"}

    resp: Optional[httpx.Response] = None
    last_exc: Optional[Exception] = None
    max_attempts = 3

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(settings.timeout, connect=15.0),
        trust_env=not is_local,
        follow_redirects=True,
    ) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                resp = await client.post(endpoint, headers=headers, json=payload)
                if resp.status_code in (400, 422) and "response_format" in payload:
                    payload.pop("response_format", None)
                    resp = await client.post(endpoint, headers=headers, json=payload)

                if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                    await asyncio.sleep(1.2 * (2 ** (attempt - 1)))
                    continue
                break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < max_attempts:
                    await asyncio.sleep(1.2 * (2 ** (attempt - 1)))
                    continue
                if isinstance(exc, httpx.TimeoutException):
                    raise LLMTranslationError("请求大模型多次超时，请检查网络连接或适当调小单批条数。") from exc
                raise LLMTranslationError(f"无法连接 API 服务（{endpoint}）：{exc}") from exc
            except httpx.RequestError as exc:
                raise LLMTranslationError(f"无法连接 API 服务（{endpoint}）：{exc}") from exc

    assert resp is not None

    if resp.status_code in (401, 403):
        raise LLMTranslationError("API Key 验证失败或无权限（HTTP 401/403），请检查密钥与账户余额。")
    if resp.status_code == 404:
        raise LLMTranslationError(f"接口路径或模型 {settings.model!r} 不存在（HTTP 404），请检查 Base URL 与模型名。")
    if resp.status_code == 429:
        raise LLMTranslationError("触发 API 速率或额度限制（HTTP 429），请稍后重试或降低并发数。")
    if resp.status_code >= 400:
        detail = resp.text[:240]
        raise LLMTranslationError(f"API 请求异常（HTTP {resp.status_code}）：{detail}")

    try:
        data = resp.json()
        choice = data["choices"][0]
        content = choice["message"].get("content") or ""
        return str(content)
    except Exception as exc:
        raise LLMTranslationError("API 响应结构不符合 OpenAI Chat Completions 规范。") from exc


async def test_api_connection(settings: APISettings) -> Dict[str, Any]:
    """Send a minimal health-check prompt to verify credentials, model, and latency."""
    t0 = time.perf_counter()
    reply = await _call_chat_completion(
        messages=[
            {"role": "system", "content": 'Reply strictly in JSON: {"ok": true, "greeting": "pong"}'},
            {"role": "user", "content": "ping"},
        ],
        settings=settings,
        temperature=0.0,
        use_json_mode=True,
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    parsed = extract_json_payload(reply)
    return {
        "ok": True,
        "latency_ms": latency_ms,
        "model": settings.model,
        "sample": parsed,
    }


def _format_glossary_prompt(glossary: List[GlossaryItem]) -> str:
    active = [g for g in glossary if g.enabled and g.source.strip() and g.target.strip()]
    if not active:
        return "（暂无预设术语表，请保持专有名词、人名译法前后一致）"
    lines = [
        f"- {item.source.strip()} => {item.target.strip()}"
        + (f" （备注：{item.note.strip()}）" if item.note.strip() else "")
        for item in active
    ]
    return "\n".join(lines)


def _postprocess_subtitle_line(text: str, target_lang: str) -> str:
    cleaned = text.strip()
    if target_lang in ("zh", "zh-tw"):
        # Follow professional Chinese subtitling convention: strip trailing single period on short lines
        lines = [ln.strip() for ln in cleaned.split("\n") if ln.strip()]
        normalized: List[str] = []
        for ln in lines:
            if ln.endswith("。") and not ln.endswith("。。") and "。" not in ln[:-1]:
                ln = ln[:-1]
            normalized.append(ln)
        return "\n".join(normalized)
    return cleaned


async def extract_glossary_and_summary(
    cues: List[SubtitleCue],
    source_lang: str,
    target_lang: str,
    settings: APISettings,
) -> Dict[str, Any]:
    """Analyze full subtitle script to extract global scene summary, genre, and proper-noun glossary."""
    dialogue = [c for c in cues if not c.is_special and c.source_text.strip()]
    if not dialogue:
        raise LLMTranslationError("没有可分析的有效字幕内容。")

    # Sample up to 120 representative cues across start, middle, and end
    if len(dialogue) <= 120:
        sampled = dialogue
    else:
        step = max(1, len(dialogue) // 120)
        sampled = dialogue[::step][:120]

    script_preview = "\n".join(
        f"[{c.id}] " + (f"({c.speaker}) " if c.speaker else "") + c.source_text.replace("\n", " ")
        for c in sampled
    )[:10000]

    target_label = LANGUAGE_LABELS.get(target_lang, target_lang)
    system_prompt = (
        "你是一位资深影视与多领域本地化字幕总监。请阅读以下视频字幕抽样文本，提取：\n"
        "1. `scene_summary`：用 1~2 句简明中文概括视频主题、场景背景、人物关系及整体语气基调（不超过 120 字）。\n"
        "2. `genre`：从 [\"general\", \"movie\", \"tech\", \"anime\", \"documentary\"] 中选择最贴切的一项。\n"
        f"3. `glossary`：提取最多 15 个关键人名、产品名、地名或高频专业术语，给出地道统一的 {target_label} 译名（若按惯例应保留英文原词如 API、GitHub，target 填原词即可）。\n"
        "严格仅输出合法 JSON 对象，格式如下：\n"
        '{"scene_summary": "...", "genre": "tech", "glossary": [{"source": "原文词", "target": "统一译名", "note": "角色/术语说明"}]}'
    )

    raw = await _call_chat_completion(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": script_preview},
        ],
        settings=settings,
        temperature=0.2,
        use_json_mode=True,
    )
    parsed = extract_json_payload(raw)
    if not isinstance(parsed, dict):
        raise LLMTranslationError("术语提取返回结构异常。")

    glossary_out: List[Dict[str, Any]] = []
    for item in parsed.get("glossary", [])[:20]:
        if isinstance(item, dict) and item.get("source") and item.get("target"):
            glossary_out.append(
                {
                    "source": str(item["source"]).strip(),
                    "target": str(item["target"]).strip(),
                    "note": str(item.get("note", "")).strip(),
                    "enabled": True,
                }
            )
    genre = str(parsed.get("genre", "general")).strip()
    if genre not in GENRE_GUIDES:
        genre = "general"

    return {
        "scene_summary": str(parsed.get("scene_summary", "")).strip(),
        "genre": genre,
        "glossary": glossary_out,
    }


_SENTENCE_END_CHARS = (".", "!", "?", "。", "！", "？", "…", "\"", "”", "'", "」", "』", ")")


def partition_scene_batches(cues: List[SubtitleCue], target_size: int = 12) -> List[List[SubtitleCue]]:
    """Split subtitle cues into batches at natural silence gaps (>=1.2s) and sentence endings."""
    n = len(cues)
    if n == 0:
        return []
    target_size = max(1, min(30, target_size))
    if target_size <= 2 or n <= target_size:
        return [cues[i : i + target_size] for i in range(0, n, target_size)]

    min_step = max(3, int(round(target_size * 0.65)))
    max_step = max(target_size + 1, int(round(target_size * 1.35)))

    batches: List[List[SubtitleCue]] = []
    idx = 0
    while idx < n:
        remaining = n - idx
        if remaining <= max_step:
            batches.append(cues[idx:])
            break

        best_cut = idx + target_size
        best_score = -1e9
        low = idx + min_step
        high = min(n - 1, idx + max_step)

        for cut in range(low, high + 1):
            prev_c = cues[cut - 1]
            next_c = cues[cut]
            gap_ms = max(0, next_c.start_ms - prev_c.end_ms)
            txt = prev_c.source_text.strip()

            # Score silence gap (cap at 4 seconds so huge pauses don't distort everything)
            score = min(gap_ms, 4000) / 100.0  # 0 .. +40 pts
            if gap_ms >= 1500:
                score += 28.0
            elif gap_ms >= 800:
                score += 12.0
            elif gap_ms < 120:
                score -= 15.0  # Likely continuous speech!

            if txt.endswith(_SENTENCE_END_CHARS):
                score += 22.0
            elif txt.endswith((",", "，", ";", "；", "—", "-")):
                score -= 18.0  # Mid-clause continuation!

            # Slight preference toward requested target_size
            score -= abs((cut - idx) - target_size) * 2.2

            if score > best_score:
                best_score = score
                best_cut = cut

        batches.append(cues[idx:best_cut])
        idx = best_cut

    return batches


async def _translate_single_batch(
    batch_cues: List[SubtitleCue],
    context_before: List[SubtitleCue],
    context_after: List[SubtitleCue],
    request: TranslateBatchRequest,
    settings: APISettings,
    rolling_note: str = "",
) -> Tuple[Dict[int, str], str]:
    src_label = LANGUAGE_LABELS.get(request.source_language, request.source_language)
    tgt_label = LANGUAGE_LABELS.get(request.target_language, request.target_language)
    genre_guide = GENRE_GUIDES.get(request.genre, GENRE_GUIDES["general"])
    glossary_block = _format_glossary_prompt(request.glossary)

    before_list = [
        {
            "id": c.id,
            "src": c.source_text,
            **({"translated": c.target_text} if c.target_text.strip() else {}),
        }
        for c in context_before
    ]
    after_list = [{"id": c.id, "src": c.source_text} for c in context_after]
    target_items = [
        {
            "id": c.id,
            **({"speaker": c.speaker} if c.speaker else {}),
            "src": c.source_text,
        }
        for c in batch_cues
    ]

    custom_rule = f"\n额外用户要求：{request.custom_prompt.strip()}" if request.custom_prompt.strip() else ""
    summary_rule = f"\n全片语境背景：{request.scene_summary.strip()}" if request.scene_summary.strip() else ""
    rolling_rule = f"\n上一幕衔接状态：{rolling_note.strip()}" if rolling_note.strip() else ""

    if request.mode == "reflective":
        system_prompt = (
            f"你是顶尖的 Netflix 级双语字幕翻译与本地化审校专家，负责将字幕从「{src_label}」精翻为「{tgt_label}」。\n"
            f"风格定位：{genre_guide}{summary_rule}{rolling_rule}{custom_rule}\n"
            f"强制术语表：\n{glossary_block}\n\n"
            "【核心翻译准则（直译→反思→地道意译）】\n"
            "1. 结合 `context_before`（上文及已译参考）与 `context_after`（下文预读）理解完整句子，妥善处理跨时间轴断句！若一句话拆在相邻两条字幕中，请按目标语言自然语序顺畅衔接，同时保证每条字幕对应自身时间段语义合理。\n"
            "2. 彻底消除机翻腔与生硬词对词直译（例如英语 welcome to our subtitle editor 应译为「欢迎使用我们的字幕编辑器」而非「欢迎收看我们的字幕编辑」；日语「こんにちは」应译为「你好」而非「哈罗」）。\n"
            "3. 字幕精炼易读：单条字幕内部如需换行可用 `\\n`，但绝不能合并、拆分、遗漏或新增 `id`，`translations` 数组必须包含 `to_translate` 中的每一个整数 `id`。\n"
            "4. 先在 `reflection` 字段用一句话简要审校本批难点（跨行断句、代词指代、口吻），再在 `translations` 中输出最终精修译文。\n\n"
            "严格仅输出如下 JSON 对象（严禁任何额外解释）：\n"
            '{"reflection": "简短上下文与断句审校说明", "translations": [{"id": 1, "text": "地道精修译文"}]}'
        )
    else:
        system_prompt = (
            f"你是专业影视与视频字幕译员，请将 `to_translate` 中的字幕从「{src_label}」准确自然地翻译为「{tgt_label}」。\n"
            f"风格定位：{genre_guide}{summary_rule}{custom_rule}\n"
            f"强制术语表：\n{glossary_block}\n\n"
            "要求：\n"
            "1. 参考 `context_before` 与 `context_after` 保持指代、语气与跨行断句连贯，拒绝生硬机翻。\n"
            "2. 严格保留 `to_translate` 中每个整数 `id` 一一对应，不得合并、拆分或遗漏。\n"
            "严格仅输出 JSON 对象：\n"
            '{"translations": [{"id": 1, "text": "译文"}]}'
        )

    user_payload = json.dumps(
        {
            "context_before": before_list,
            "to_translate": target_items,
            "context_after": after_list,
        },
        ensure_ascii=False,
    )

    raw = await _call_chat_completion(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_payload},
        ],
        settings=settings,
        use_json_mode=True,
    )
    parsed = extract_json_payload(raw)
    reflection = ""
    entries: List[Any] = []
    if isinstance(parsed, dict):
        reflection = str(parsed.get("reflection", "")).strip()
        entries = parsed.get("translations", [])
    elif isinstance(parsed, list):
        entries = parsed

    expected_ids = {c.id for c in batch_cues}
    mapped: Dict[int, str] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id")
        try:
            cid = int(raw_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        txt = str(item.get("text", "")).strip()
        if cid in expected_ids and txt:
            mapped[cid] = _postprocess_subtitle_line(txt, request.target_language)

    # Targeted fallback retry if LLM missed a few IDs in this batch
    missing_cues = [c for c in batch_cues if c.id not in mapped]
    if missing_cues:
        retry_items = [{"id": c.id, "src": c.source_text} for c in missing_cues]
        retry_raw = await _call_chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"Translate every subtitle item from {src_label} to {tgt_label} naturally. "
                        'Return JSON only: {"translations": [{"id": 1, "text": "..."}]}'
                    ),
                },
                {"role": "user", "content": json.dumps({"to_translate": retry_items}, ensure_ascii=False)},
            ],
            settings=settings,
            temperature=0.1,
            use_json_mode=True,
        )
        retry_parsed = extract_json_payload(retry_raw)
        retry_entries = retry_parsed.get("translations", []) if isinstance(retry_parsed, dict) else retry_parsed
        if isinstance(retry_entries, list):
            for item in retry_entries:
                if isinstance(item, dict) and item.get("text"):
                    try:
                        cid = int(item["id"])
                        if cid in expected_ids:
                            mapped[cid] = _postprocess_subtitle_line(str(item["text"]), request.target_language)
                    except (KeyError, TypeError, ValueError):
                        continue

    return mapped, reflection


async def stream_translate_cues(
    request: TranslateBatchRequest,
    settings: APISettings,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Stream batch-by-batch translation events with sliding context windows."""
    cue_map: Dict[int, int] = {c.id: idx for idx, c in enumerate(request.cues)}
    selected_ids = set(request.cue_ids) if request.cue_ids else None

    target_cues = [
        c
        for c in request.cues
        if not c.is_special
        and c.source_text.strip()
        and (selected_ids is None or c.id in selected_ids)
    ]

    total = len(target_cues)
    if total == 0:
        yield {"event": "complete", "completed": 0, "total": 0, "message": "没有需要翻译的字幕条目。"}
        return

    batch_size = max(1, min(30, settings.batch_size))
    ctx_win = max(0, min(10, settings.context_window))

    batches: List[List[SubtitleCue]] = partition_scene_batches(target_cues, target_size=batch_size)

    yield {
        "event": "start",
        "total": total,
        "batches": len(batches),
        "mode": request.mode,
        "model": settings.model,
    }

    working_cues = [c.model_copy(deep=True) for c in request.cues]
    completed = 0
    rolling_note = ""

    if request.mode == "reflective" or settings.concurrency <= 1:
        # Sequential sliding window + rolling scene context!
        for b_idx, batch in enumerate(batches, start=1):
            first_pos = cue_map[batch[0].id]
            last_pos = cue_map[batch[-1].id]
            before = [c for c in working_cues[max(0, first_pos - ctx_win) : first_pos] if not c.is_special]
            after = [c for c in working_cues[last_pos + 1 : last_pos + 1 + ctx_win] if not c.is_special]

            yield {
                "event": "batch_start",
                "batch_index": b_idx,
                "total_batches": len(batches),
                "from_id": batch[0].id,
                "to_id": batch[-1].id,
                "completed": completed,
                "total": total,
            }

            mapped, reflection = await _translate_single_batch(
                batch, before, after, request, settings, rolling_note=rolling_note
            )
            if reflection:
                rolling_note = reflection[:240]
            batch_updates: List[Dict[str, Any]] = []
            for cue in batch:
                if cue.id in mapped:
                    pos = cue_map[cue.id]
                    working_cues[pos].target_text = mapped[cue.id]
                    working_cues[pos].status = "translated"
                    batch_updates.append({"id": cue.id, "target_text": mapped[cue.id], "status": "translated"})
                else:
                    batch_updates.append({"id": cue.id, "target_text": cue.target_text, "status": "error"})

            completed += len(batch)
            yield {
                "event": "batch_done",
                "batch_index": b_idx,
                "total_batches": len(batches),
                "reflection": reflection,
                "updates": batch_updates,
                "completed": completed,
                "total": total,
                "percent": round(completed * 100 / total, 1),
            }
    else:
        # Concurrent fast context mode
        sem = asyncio.Semaphore(max(1, min(6, settings.concurrency)))
        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()

        async def worker(b_idx: int, batch: List[SubtitleCue]) -> None:
            async with sem:
                first_pos = cue_map[batch[0].id]
                last_pos = cue_map[batch[-1].id]
                before = [c for c in working_cues[max(0, first_pos - ctx_win) : first_pos] if not c.is_special]
                after = [c for c in working_cues[last_pos + 1 : last_pos + 1 + ctx_win] if not c.is_special]
                try:
                    mapped, reflection = await _translate_single_batch(batch, before, after, request, settings)
                    await queue.put({"ok": True, "b_idx": b_idx, "batch": batch, "mapped": mapped, "reflection": reflection})
                except Exception as exc:
                    await queue.put({"ok": False, "b_idx": b_idx, "error": str(exc)})

        tasks = [asyncio.create_task(worker(idx, b)) for idx, b in enumerate(batches, start=1)]
        try:
            for _ in range(len(batches)):
                item = await queue.get()
                if not item["ok"]:
                    raise LLMTranslationError(item["error"])
                batch = item["batch"]
                mapped = item["mapped"]
                batch_updates = []
                for cue in batch:
                    if cue.id in mapped:
                        batch_updates.append({"id": cue.id, "target_text": mapped[cue.id], "status": "translated"})
                    else:
                        batch_updates.append({"id": cue.id, "target_text": cue.target_text, "status": "error"})
                completed += len(batch)
                yield {
                    "event": "batch_done",
                    "batch_index": item["b_idx"],
                    "total_batches": len(batches),
                    "reflection": item["reflection"],
                    "updates": batch_updates,
                    "completed": completed,
                    "total": total,
                    "percent": round(completed * 100 / total, 1),
                }
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

    yield {
        "event": "complete",
        "completed": completed,
        "total": total,
        "percent": 100.0,
        "message": f"已完成 {completed} 条字幕的上下文精翻。",
    }


async def rewrite_single_cue(
    request: SingleCueRewriteRequest,
    settings: APISettings,
) -> Dict[str, Any]:
    """Retranslate or polish a single subtitle cue with surrounding ±5 lines of context."""
    idx_map = {c.id: i for i, c in enumerate(request.cues)}
    if request.cue_id not in idx_map:
        raise LLMTranslationError(f"未找到编号 #{request.cue_id} 的字幕。")

    pos = idx_map[request.cue_id]
    target_cue = request.cues[pos]
    before = [c for c in request.cues[max(0, pos - 5) : pos] if not c.is_special]
    after = [c for c in request.cues[pos + 1 : pos + 6] if not c.is_special]

    action_prompts = {
        "retranslate": "结合上下文重新精准翻译该句，确保语义地道自然、彻底消除机翻感。",
        "natural": "将译文润色得更口语化、更像真实母语人物对话，保留原意与情绪。",
        "shorten": "在保留核心含义的前提下精简压缩译文字数（适合快语速短时间轴字幕阅读）。",
        "formal": "将译文调整为专业、书面、得体的表达风格。",
        "custom": request.instruction.strip() or "按上下文优化译文。",
    }
    task_desc = action_prompts.get(request.action, action_prompts["retranslate"])
    if request.action != "custom" and request.instruction.strip():
        task_desc += f" 补充要求：{request.instruction.strip()}"

    tgt_label = LANGUAGE_LABELS.get(request.target_language, request.target_language)
    glossary_block = _format_glossary_prompt(request.glossary)

    system_prompt = (
        f"你是资深影视字幕精修审校专家。目标语言：{tgt_label}。\n"
        f"术语参考：\n{glossary_block}\n"
        f"任务要求：{task_desc}\n"
        "请给出最优译文 `best` 以及 2 个不同表达侧重的备选译文 `alternatives`，并用一句话说明修改理由 `reason`。\n"
        "严格仅输出 JSON 对象：\n"
        '{"best": "最优译文", "alternatives": ["备选1", "备选2"], "reason": "简短说明"}'
    )
    user_payload = json.dumps(
        {
            "scene_summary": request.scene_summary,
            "context_before": [{"id": c.id, "src": c.source_text, "tgt": c.target_text} for c in before],
            "current_cue": {
                "id": target_cue.id,
                "duration_sec": round((target_cue.end_ms - target_cue.start_ms) / 1000, 2),
                "src": target_cue.source_text,
                "current_tgt": target_cue.target_text,
            },
            "context_after": [{"id": c.id, "src": c.source_text, "tgt": c.target_text} for c in after],
        },
        ensure_ascii=False,
    )

    raw = await _call_chat_completion(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_payload},
        ],
        settings=settings,
        temperature=0.35,
        use_json_mode=True,
    )
    parsed = extract_json_payload(raw)
    if not isinstance(parsed, dict) or not parsed.get("best"):
        raise LLMTranslationError("单句润色未返回有效译文。")

    best = _postprocess_subtitle_line(str(parsed["best"]), request.target_language)
    alts = [
        _postprocess_subtitle_line(str(a), request.target_language)
        for a in (parsed.get("alternatives") or [])
        if str(a).strip()
    ]
    return {
        "cue_id": target_cue.id,
        "best": best,
        "alternatives": alts[:3],
        "reason": str(parsed.get("reason", "")).strip(),
    }
