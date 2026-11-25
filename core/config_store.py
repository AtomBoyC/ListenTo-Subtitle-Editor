"""Persistent local settings store and LLM provider presets."""

import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from core.models import APISettings, APISettingsPublic, APISettingsUpdate, ProviderPreset

IS_FROZEN = bool(getattr(sys, "frozen", False))
IS_DESKTOP = IS_FROZEN or os.environ.get("LISTENTO_DESKTOP") == "1"
_IN_DOCKER = os.environ.get("LISTENTO_IN_DOCKER") == "1"

if IS_FROZEN:
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS"))
else:
    RESOURCE_DIR = Path(__file__).resolve().parent.parent

ROOT_DIR = RESOURCE_DIR


def _default_data_dir() -> Path:
    if os.environ.get("LISTENTO_DATA_DIR"):
        return Path(os.environ["LISTENTO_DATA_DIR"]).expanduser()
    if IS_DESKTOP:
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "ListenToStudio"
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA") or str(Path.home())
            return Path(appdata) / "ListenToStudio"
        return Path.home() / ".config" / "listento-studio"
    return ROOT_DIR / ".data"


def default_exports_dir() -> Path:
    if os.environ.get("LISTENTO_EXPORTS_DIR"):
        return Path(os.environ["LISTENTO_EXPORTS_DIR"]).expanduser()
    if IS_DESKTOP:
        return Path.home() / "Downloads" / "ListenTo-Subtitles"
    return ROOT_DIR / "exports"


DATA_DIR = _default_data_dir()
SETTINGS_FILE = DATA_DIR / "settings.json"
PROJECTS_DIR = DATA_DIR / "projects"

PROVIDER_PRESETS: List[ProviderPreset] = [
    ProviderPreset(
        id="deepseek",
        name="DeepSeek (深度求索)",
        badge="V4 旗舰 · 性价比首选",
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-flash",
        models=[
            "deepseek-flash",
            "deepseek-pro",
            "deepseek-v4.1-flash-1017",
            "deepseek-chat",
            "deepseek-reasoner",
        ],
        requires_key=True,
        doc_hint="支持最新 DeepSeek V4.1 Flash / V4 Pro 及经典 chat/reasoner 别名，中文影视与技术语感极佳。",
    ),
    ProviderPreset(
        id="openai",
        name="OpenAI",
        badge="GPT-6.2 / GPT-5 全系",
        base_url="https://api.openai.com/v1",
        default_model="gpt-6.2-sol",
        models=[
            "gpt-6.2-sol",
            "gpt-6.2-astra",
            "gpt-6.1-terra",
            "gpt-6-luna",
            "gpt-5",
            "gpt-5-mini",
            "gpt-5-nano",
            "o3",
            "o4-mini",
            "gpt-4.1",
            "gpt-4.1-mini",
            "gpt-4o",
            "gpt-4o-mini",
        ],
        requires_key=True,
        doc_hint="涵盖最新 GPT-6.2 (Astra/Sol)、GPT-5 系列与 o3/o4-mini 推理模型；也可点击「实时拉取模型」获取账号全部可用模型。",
    ),
    ProviderPreset(
        id="gemini",
        name="Google Gemini",
        badge="Gemini 3.8 / 3.5",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        default_model="gemini-3.5-flash",
        models=[
            "gemini-3.8-flash",
            "gemini-3.7-flash",
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-3-pro-preview",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ],
        requires_key=True,
        doc_hint="支持 Google AI Studio Key，涵盖最新 Gemini 3.8 / 3.5 Flash 与 3 Pro，日英中多语种与百万上下文表现优异。",
    ),
    ProviderPreset(
        id="anthropic",
        name="Anthropic Claude",
        badge="Claude 5.5 旗舰",
        base_url="https://api.anthropic.com/v1",
        default_model="claude-sonnet-5-5",
        models=[
            "claude-sonnet-5-5",
            "claude-opus-5-5",
            "claude-haiku-5-5",
            "claude-fable-5-2",
            "claude-fable-5-1",
            "claude-sonnet-4-5",
            "claude-opus-4-5",
        ],
        requires_key=True,
        doc_hint="涵盖最新 Claude 5.5 (Sonnet/Opus/Haiku) 与 Fable 5.2 系列，文学台词、潜台词与自然口吻细腻度顶级。",
    ),
    ProviderPreset(
        id="dashscope",
        name="阿里云百炼 (通义千问)",
        badge="Qwen4-Max 最新",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen4-max",
        models=[
            "qwen4-max",
            "qwen-max-latest",
            "qwen-plus-latest",
            "qwen-turbo-latest",
            "qwen3.5-plus",
            "qwen3-max",
            "qwq-plus",
            "qwen-plus",
            "qwen-max",
        ],
        requires_key=True,
        doc_hint="阿里云官方百炼接口，支持最新 Qwen4-Max 与 Qwen3.5 系列，国内直连稳定低延迟。",
    ),
    ProviderPreset(
        id="siliconflow",
        name="SiliconFlow (硅基流动)",
        badge="国内聚合直连",
        base_url="https://api.siliconflow.cn/v1",
        default_model="deepseek-ai/DeepSeek-V4-Pro",
        models=[
            "deepseek-ai/DeepSeek-V4-Pro",
            "deepseek-ai/DeepSeek-V4.1-Flash",
            "Qwen/Qwen4-Max",
            "Qwen/Qwen3.5-122B-A10B",
            "zai-org/GLM-5.1",
            "moonshotai/Kimi-K2-Instruct",
            "stepfun-ai/Step-3.5-Flash",
            "deepseek-ai/DeepSeek-V3",
            "deepseek-ai/DeepSeek-R1",
        ],
        requires_key=True,
        doc_hint="聚合 DeepSeek-V4、Qwen4-Max、GLM-5.1、Kimi、阶跃星辰等全系模型；推荐填入 Key 后点击「实时拉取模型」查看完整列表。",
    ),
    ProviderPreset(
        id="volcengine",
        name="火山方舟 (字节豆包)",
        badge="Doubao-Seed 2.1",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        default_model="doubao-seed-2.1-pro",
        models=[
            "doubao-seed-2.1-pro",
            "doubao-seed-2.1-lite",
            "doubao-seed-2.1-turbo",
            "doubao-seed-evolving",
            "doubao-seed-code",
        ],
        requires_key=True,
        doc_hint="字节跳动火山方舟平台，支持 Doubao-Seed 2.1 全系模型（也可直接填入你的推理接入点 ep-xxxx）。",
    ),
    ProviderPreset(
        id="kimi",
        name="Moonshot (月之暗面 Kimi)",
        badge="Kimi K3.1 / K2",
        base_url="https://api.moonshot.cn/v1",
        default_model="kimi-k3.1-pro",
        models=[
            "kimi-k3.1-pro",
            "kimi-latest",
            "kimi-k2-thinking",
            "kimi-k2-turbo-preview",
            "moonshot-v1-auto",
        ],
        requires_key=True,
        doc_hint="支持最新 Kimi K3.1 Pro 与 K2 Thinking 超长上下文模型，中英长篇演讲与纪录片翻译极稳。",
    ),
    ProviderPreset(
        id="zhipu",
        name="智谱 AI (GLM)",
        badge="GLM-5.1 最新",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-5.1",
        models=[
            "glm-5.1",
            "glm-5",
            "glm-4.5",
            "glm-4.5-air",
            "glm-4-flash",
        ],
        requires_key=True,
        doc_hint="智谱开放平台官方兼容接口，包含最新旗舰 GLM-5.1 以及免费高速模型 glm-4-flash。",
    ),
    ProviderPreset(
        id="xai",
        name="xAI (Grok)",
        badge="Grok 4.7",
        base_url="https://api.x.ai/v1",
        default_model="grok-4.7",
        models=[
            "grok-4.7",
            "grok-420-reasoning",
            "grok-4",
            "grok-3-mini",
        ],
        requires_key=True,
        doc_hint="xAI 官方接口，对美剧俚语、流行文化梗、脱口秀与口语化对白理解极强。",
    ),
    ProviderPreset(
        id="openrouter",
        name="OpenRouter",
        badge="全球全模型聚合",
        base_url="https://openrouter.ai/api/v1",
        default_model="anthropic/claude-sonnet-5.5",
        models=[
            "anthropic/claude-sonnet-5.5",
            "anthropic/claude-opus-5.5",
            "openai/gpt-6.2-sol",
            "openai/gpt-6.2-astra",
            "google/gemini-3.5-flash",
            "google/gemini-3.8-flash",
            "deepseek/deepseek-v4-pro",
            "qwen/qwen4-max",
            "x-ai/grok-4.7",
        ],
        requires_key=True,
        doc_hint="一个 Key 畅用全球数百款最新模型；点击「实时拉取模型」可直接搜索 OpenRouter 全部实时模型库！",
    ),
    ProviderPreset(
        id="ollama",
        name="Ollama / LM Studio",
        badge="本地免 Key",
        base_url="http://host.docker.internal:11434/v1" if _IN_DOCKER else "http://localhost:11434/v1",
        default_model="qwen3.5:14b",
        models=[
            "qwen3.5:32b",
            "qwen3.5:14b",
            "qwen3:14b",
            "qwen3:8b",
            "deepseek-r1:14b",
            "gemma3:12b",
            "glm4:9b",
        ],
        requires_key=False,
        doc_hint="本地离线运行（LM Studio 可改端口为 1234）；点击「实时拉取模型」可自动读取你本机已下载的全部模型。",
    ),
    ProviderPreset(
        id="custom",
        name="自定义 / 第三方中转站",
        badge="NewAPI / OneAPI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-6.2-sol",
        models=[],
        requires_key=True,
        doc_hint="兼容所有 NewAPI / OneAPI / VoAPI / vLLM 中转服务：填入地址和 Key 后点击「🔄 实时拉取模型」即可一键列出所有模型！",
    ),
]


def mask_key(key: str) -> str:
    cleaned = (key or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) <= 8:
        return "*" * len(cleaned)
    return f"{cleaned[:4]}{'•' * 8}{cleaned[-4:]}"


def _env_fallback_key() -> str:
    for var in ("SUBTITLE_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return ""


def load_settings() -> APISettings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SETTINGS_FILE.is_file():
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            settings = APISettings(**raw)
            if not settings.api_key:
                settings.api_key = _env_fallback_key()
            return settings
        except Exception:
            pass
    default = APISettings()
    env_key = _env_fallback_key()
    if env_key:
        default.api_key = env_key
    return default


def save_settings(update: APISettingsUpdate) -> APISettings:
    current = load_settings()
    data = current.model_dump()
    patch = update.model_dump(exclude_unset=True)
    clear_key = patch.pop("clear_api_key", False)
    new_key = patch.pop("api_key", None)

    for k, v in patch.items():
        if v is not None:
            data[k] = v

    if clear_key:
        data["api_key"] = ""
    elif new_key is not None and new_key.strip():
        data["api_key"] = new_key.strip()

    updated = APISettings(**data)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(updated.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        os.chmod(SETTINGS_FILE, 0o600)
    except OSError:
        pass
    return updated


def resolve_effective_settings(override: Optional[APISettingsUpdate] = None) -> APISettings:
    base = load_settings()
    if override is None:
        return base
    data = base.model_dump()
    patch = override.model_dump(exclude_unset=True)
    patch.pop("clear_api_key", None)
    api_key = patch.pop("api_key", None)
    for k, v in patch.items():
        if v is not None:
            data[k] = v
    if api_key is not None and api_key.strip():
        data["api_key"] = api_key.strip()
    return APISettings(**data)


def to_public_settings(settings: APISettings) -> APISettingsPublic:
    return APISettingsPublic(
        provider_id=settings.provider_id,
        base_url=settings.base_url,
        has_api_key=bool(settings.api_key.strip()),
        masked_api_key=mask_key(settings.api_key),
        model=settings.model,
        temperature=settings.temperature,
        batch_size=settings.batch_size,
        context_window=settings.context_window,
        concurrency=settings.concurrency,
        timeout=settings.timeout,
        default_mode=settings.default_mode,
        default_target_lang=settings.default_target_lang,
    )
