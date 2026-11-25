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
        badge="高性价比首选",
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        models=["deepseek-chat", "deepseek-reasoner"],
        requires_key=True,
        doc_hint="中文语感极佳、价格极低，非常适合影视/技术双语字幕精翻。",
    ),
    ProviderPreset(
        id="openai",
        name="OpenAI",
        badge="通用标杆",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        models=["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"],
        requires_key=True,
        doc_hint="结构化 JSON 稳定，适合多语种互译与高要求术语对齐。",
    ),
    ProviderPreset(
        id="gemini",
        name="Google Gemini",
        badge="长上下文",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        default_model="gemini-2.5-flash",
        models=["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"],
        requires_key=True,
        doc_hint="响应速度快，日英中多语种理解准确，支持 Google AI Studio Key。",
    ),
    ProviderPreset(
        id="siliconflow",
        name="SiliconFlow (硅基流动)",
        badge="国内直连",
        base_url="https://api.siliconflow.cn/v1",
        default_model="deepseek-ai/DeepSeek-V3",
        models=[
            "deepseek-ai/DeepSeek-V3",
            "Qwen/Qwen2.5-72B-Instruct",
            "Qwen/Qwen2.5-32B-Instruct",
            "Qwen/Qwen2.5-7B-Instruct",
        ],
        requires_key=True,
        doc_hint="国内高速访问，聚合 DeepSeek-V3 与通义千问开源旗舰模型。",
    ),
    ProviderPreset(
        id="dashscope",
        name="阿里云百炼 (通义千问)",
        badge="国内稳定",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen-plus",
        models=["qwen-plus", "qwen-max", "qwen-turbo"],
        requires_key=True,
        doc_hint="阿里云官方兼容接口，多语种与专业术语表现扎实。",
    ),
    ProviderPreset(
        id="openrouter",
        name="OpenRouter",
        badge="全模型聚合",
        base_url="https://openrouter.ai/api/v1",
        default_model="anthropic/claude-3.5-sonnet",
        models=[
            "anthropic/claude-3.5-sonnet",
            "deepseek/deepseek-chat",
            "google/gemini-2.5-flash",
            "openai/gpt-4o-mini",
        ],
        requires_key=True,
        doc_hint="一个 Key 切换 Claude / GPT / Gemini / DeepSeek 等全系模型。",
    ),
    ProviderPreset(
        id="ollama",
        name="Ollama (本地离线模型)",
        badge="本地免 Key",
        base_url="http://host.docker.internal:11434/v1" if _IN_DOCKER else "http://localhost:11434/v1",
        default_model="qwen2.5:7b",
        models=["qwen2.5:7b", "qwen2.5:14b", "gemma2:9b", "llama3.1:8b"],
        requires_key=False,
        doc_hint="数据不出本机；Docker 部署下已自动指向宿主机 host.docker.internal:11434。",
    ),
    ProviderPreset(
        id="custom",
        name="自定义 OpenAI 兼容接口",
        badge="任意中转/私有化",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        models=[],
        requires_key=True,
        doc_hint="支持任意兼容 /v1/chat/completions 协议的代理、vLLM 或 LM Studio。",
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
