"""Optional local paths, with portable defaults and no stored API credentials."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _configured_path(key: str, environment_variable: str) -> Path | None:
    value = os.environ.get(environment_variable)
    if value is None:
        configuration = ROOT / "local_paths.json"
        if configuration.is_file():
            try:
                data = json.loads(configuration.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("local_paths.json 无法读取，请检查本机路径配置。") from exc
            if not isinstance(data, dict):
                raise ValueError("local_paths.json 应为包含本机路径的 JSON 对象。")
            value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"本机路径配置 {key} 不能为空。")
    path = Path(os.path.expandvars(value)).expanduser()
    return (ROOT / path).resolve() if not path.is_absolute() else path.resolve()


def environment_path() -> Path:
    configured = _configured_path("python_environment", "SUBTITLE_PYTHON_ENV")
    if configured is not None:
        return configured
    task_environment = ROOT.parent.parent / "work" / "subtitle-env"
    if ROOT.parent.name == "outputs" and task_environment.is_dir():
        return task_environment
    return ROOT / ".venv"


def model_cache(kind: str) -> Path:
    if kind not in ("subtitle", "translation"):
        raise ValueError("Unknown model cache kind")
    key, variable = (
        ("whisper_models", "SUBTITLE_MODEL_CACHE") if kind == "subtitle"
        else ("translation_models", "SUBTITLE_TRANSLATION_CACHE")
    )
    configured = _configured_path(key, variable)
    if configured is not None:
        return configured
    task_work = ROOT.parent.parent / "work"
    if ROOT.parent.name == "outputs" and task_work.is_dir():
        return task_work / ("subtitle-models" if kind == "subtitle" else "translation-models")
    return ROOT / ".cache" / ("models" if kind == "subtitle" else "translation")
