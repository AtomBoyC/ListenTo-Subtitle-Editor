"""Translate subtitle cues locally using official Argos model data.

Only CTranslate2 and SentencePiece are needed: each subtitle cue already is a
short utterance, so Argos' sentence-boundary libraries are unnecessary. Downloads
contain model data only; nothing from a downloaded package is imported or run.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import threading
from typing import Callable, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import uuid
import zipfile

from subtitle_core import ProgressUpdate, TranscriptionCancelled


INDEX_URL = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"
LANGUAGES = ("zh", "en", "ja", "ko", "fr", "de", "es")
LANGUAGE_NAMES = {
    "zh": "中文", "en": "英语", "ja": "日语", "ko": "韩语",
    "fr": "法语", "de": "德语", "es": "西班牙语",
}
_CHUNK = 1024 * 1024
_MAX_ARCHIVE = 2 * 1024 * 1024 * 1024
_MAX_EXPANDED = 4 * 1024 * 1024 * 1024
_LOCK = threading.Lock()
ProgressCallback = Callable[[ProgressUpdate], None]


class OfflineTranslationError(RuntimeError):
    """A model is unavailable, incomplete, or cannot translate this pair."""


class _UnsupportedTokenizer(OfflineTranslationError):
    pass


def default_model_cache() -> Path:
    from runtime_config import model_cache
    return model_cache("translation")


def _cancel(cancel_event: Optional[threading.Event]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise TranscriptionCancelled("已取消字幕翻译，未保存字幕文件。")


def _notify(progress: Optional[ProgressCallback], message: str, percent=None) -> None:
    if progress is not None:
        try:
            progress(ProgressUpdate("translating", message, percent))
        except Exception:
            logging.getLogger(__name__).exception("Translation progress callback failed")


def _language(value: str) -> str:
    value = str(value).strip().lower().replace("_", "-")
    value = {"zh-cn": "zh", "zh-hans": "zh", "zh-tw": "zh",
             "zh-hant": "zh", "eng": "en"}.get(value, value)
    if value not in LANGUAGES:
        raise OfflineTranslationError(
            f"离线翻译暂不支持语言 {value!r}；可选中文、英语、日语、韩语、法语、德语、西班牙语。"
        )
    return value


def _version(metadata: dict) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", str(metadata.get("package_version", "0"))))


def _metadata(package_dir: Path, source: str, target: str) -> dict:
    try:
        raw = (package_dir / "metadata.json").read_bytes()
        if len(raw) > _CHUNK:
            raise ValueError("oversized metadata")
        metadata = json.loads(raw.decode("utf-8"))
        if not isinstance(metadata, dict):
            raise ValueError("invalid metadata")
        if metadata.get("from_code") != source or metadata.get("to_code") != target:
            raise ValueError("language codes do not match")
        if metadata.get("type", "translate") != "translate":
            raise ValueError("not a translation model")
        if not (package_dir / "sentencepiece.model").is_file():
            raise _UnsupportedTokenizer("该模型使用其他分词方式，正在尝试 SentencePiece 模型。")
        for relative in ("model/model.bin", "model/config.json", "sentencepiece.model"):
            if (package_dir / relative).stat().st_size <= 0:
                raise ValueError(f"empty model file: {relative}")
        prefix = metadata.get("target_prefix", "")
        if not isinstance(prefix, str) or len(prefix) > 256:
            raise ValueError("invalid target prefix")
        return metadata
    except _UnsupportedTokenizer:
        raise
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        raise OfflineTranslationError(f"翻译模型不完整或语言代码不符：{package_dir}") from exc


def _local_models(cache: Path) -> dict[tuple[str, str], tuple[Path, dict]]:
    models: dict[tuple[str, str], tuple[Path, dict]] = {}
    for metadata_file in cache.glob("*/*/metadata.json"):
        try:
            preliminary = json.loads(metadata_file.read_text(encoding="utf-8"))
            if not isinstance(preliminary, dict):
                continue
            source, target = preliminary.get("from_code"), preliminary.get("to_code")
            if source not in LANGUAGES or target not in LANGUAGES:
                continue
            metadata = _metadata(metadata_file.parent, source, target)
            pair = (source, target)
            if pair not in models or _version(metadata) > _version(models[pair][1]):
                models[pair] = (metadata_file.parent, metadata)
        except (OfflineTranslationError, OSError, ValueError, TypeError, UnicodeError):
            continue
    return models


def _route(source: str, target: str, pairs) -> Optional[list[tuple[str, str]]]:
    direct = (source, target)
    if direct in pairs:
        return [direct]
    if source != "en" and target != "en":
        through_english = [(source, "en"), ("en", target)]
        if all(pair in pairs for pair in through_english):
            return through_english
    return None


def _https_open(url: str):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise OfflineTranslationError("模型下载地址必须为 HTTPS。")
    response = urlopen(Request(url, headers={"User-Agent": "LocalSubtitleGenerator/2.0"}), timeout=30)
    if urlparse(response.geturl()).scheme != "https":
        response.close()
        raise OfflineTranslationError("模型下载不能重定向到非 HTTPS 地址。")
    return response


def _read_index(cache: Path, cancel_event) -> list[dict]:
    _cancel(cancel_event)
    index_file = cache / "index.json"
    try:
        with _https_open(INDEX_URL) as response:
            raw = response.read(8 * _CHUNK + 1)
        _cancel(cancel_event)
        if len(raw) > 8 * _CHUNK:
            raise ValueError("oversized model index")
        index = json.loads(raw.decode("utf-8"))
        if not isinstance(index, list) or not all(isinstance(item, dict) for item in index):
            raise ValueError("invalid model index")
        # The downloaded index is data, never Python or a package installer.
        with tempfile.NamedTemporaryFile(dir=cache, prefix=".index-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
        try:
            _cancel(cancel_event)
            os.replace(temporary, index_file)
        finally:
            temporary.unlink(missing_ok=True)
        return index
    except TranscriptionCancelled:
        raise
    except Exception as exc:
        try:
            index = json.loads(index_file.read_text(encoding="utf-8"))
            if isinstance(index, list) and all(isinstance(item, dict) for item in index):
                return index
        except (OSError, ValueError, UnicodeError):
            pass
        raise OfflineTranslationError("尚未缓存此语言的翻译模型，首次使用请联网下载。") from exc


def _index_models(index: list[dict]) -> dict[tuple[str, str], list[dict]]:
    result: dict[tuple[str, str], list[dict]] = {}
    for item in index:
        pair = (item.get("from_code"), item.get("to_code"))
        if pair[0] not in LANGUAGES or pair[1] not in LANGUAGES:
            continue
        if item.get("type", "translate") != "translate":
            continue
        if not isinstance(item.get("links"), list):
            continue
        if not any(isinstance(link, str) and link.startswith("https://") for link in item["links"]):
            continue
        result.setdefault(pair, []).append(item)
    for packages in result.values():
        packages.sort(key=_version, reverse=True)
    return result


def _download(url: str, destination: Path, pair, progress, cancel_event) -> None:
    source, target = pair
    name = f"{LANGUAGE_NAMES[source]} → {LANGUAGE_NAMES[target]}"
    _notify(progress, f"首次使用：正在下载 {name} 离线翻译模型……", 0)
    _cancel(cancel_event)
    try:
        with _https_open(url) as response, destination.open("wb") as handle:
            length = response.headers.get("Content-Length")
            total = int(length) if length and length.isdigit() else 0
            if total > _MAX_ARCHIVE:
                raise OfflineTranslationError("翻译模型文件超过允许大小。")
            received = 0
            next_update = 0
            while True:
                _cancel(cancel_event)
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                received += len(chunk)
                if received > _MAX_ARCHIVE:
                    raise OfflineTranslationError("翻译模型文件超过允许大小。")
                handle.write(chunk)
                if received >= next_update:
                    amount = f"{received / _CHUNK:.1f} MB"
                    if total:
                        amount += f" / {total / _CHUNK:.1f} MB"
                    _notify(progress, f"下载 {name} 模型：{amount}", 0)
                    next_update = received + 4 * _CHUNK
            _cancel(cancel_event)
            if total and received != total:
                raise OfflineTranslationError("翻译模型下载不完整，请重新运行。")
    except (TranscriptionCancelled, OfflineTranslationError):
        raise
    except Exception as exc:
        raise OfflineTranslationError(f"下载 {name} 翻译模型失败，请检查网络后重试。") from exc


def _extract(archive: Path, destination: Path, source: str, target: str, cancel_event) -> Path:
    """Verify every ZIP member's CRC and extract only inference data safely."""
    _cancel(cancel_event)
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > 10000 or sum(member.file_size for member in members) > _MAX_EXPANDED:
                raise OfflineTranslationError("翻译模型压缩包大小或文件数量异常。")
            safe_names = {}
            root = destination.resolve()
            for member in members:
                name = member.filename.replace("\\", "/")
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or any(":" in part for part in path.parts):
                    raise OfflineTranslationError("翻译模型压缩包包含不安全路径。")
                if not path.parts:
                    raise OfflineTranslationError("翻译模型压缩包包含空路径。")
                mode = (member.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise OfflineTranslationError("翻译模型压缩包不允许符号链接。")
                target_path = destination.joinpath(*path.parts).resolve()
                if not target_path.is_relative_to(root):
                    raise OfflineTranslationError("翻译模型压缩包路径超出缓存目录。")
                canonical = str(target_path).casefold()
                if canonical in safe_names:
                    raise OfflineTranslationError("翻译模型压缩包包含重复路径。")
                safe_names[canonical] = (member, path, target_path)
            metadata_paths = [path for _, path, _ in safe_names.values() if path.name == "metadata.json"]
            if len(metadata_paths) != 1:
                raise OfflineTranslationError("翻译模型压缩包缺少唯一 metadata.json。")
            package_root = metadata_paths[0].parent
            for member, path, target_path in safe_names.values():
                _cancel(cancel_event)
                if member.is_dir():
                    continue
                relative = path.relative_to(package_root) if path.is_relative_to(package_root) else None
                needed = relative is not None and (
                    relative.parts[0] == "model" or
                    str(relative) in ("metadata.json", "sentencepiece.model", "bpe.model", "README.md", "LICENSE", "LICENSE.txt")
                )
                output = None
                try:
                    if needed:
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        output = target_path.open("wb")
                    # Reading to EOF validates CRC even for sentence-splitter data
                    # that is not needed or saved by this cue-based translator.
                    with bundle.open(member) as input_file:
                        while True:
                            _cancel(cancel_event)
                            chunk = input_file.read(_CHUNK)
                            if not chunk:
                                break
                            if output is not None:
                                output.write(chunk)
                finally:
                    if output is not None:
                        output.close()
            directory = destination.joinpath(*package_root.parts)
            _metadata(directory, source, target)
            return directory
    except (TranscriptionCancelled, OfflineTranslationError):
        raise
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        raise OfflineTranslationError("翻译模型压缩包不完整或已损坏，请重试下载。") from exc


def _install(pair, packages, cache: Path, progress, cancel_event) -> tuple[Path, dict]:
    source, target = pair
    last_error = None
    for package in packages:
        for url in package["links"]:
            if not isinstance(url, str) or not url.startswith("https://"):
                continue
            _cancel(cancel_event)
            with tempfile.TemporaryDirectory(prefix=".download-", dir=cache) as temporary:
                temp_dir = Path(temporary)
                try:
                    archive = temp_dir / "model.argosmodel"
                    _download(url, archive, pair, progress, cancel_event)
                    _notify(progress, "正在校验离线翻译模型……", 0)
                    unpacked = _extract(archive, temp_dir / "unpacked", source, target, cancel_event)
                    metadata = _metadata(unpacked, source, target)
                    if str(metadata.get("package_version")) != str(package.get("package_version")):
                        raise OfflineTranslationError("翻译模型版本与官方索引不符。")
                    parent = cache / f"{source}-{target}"
                    if not parent.resolve().is_relative_to(cache.resolve()):
                        raise OfflineTranslationError("模型缓存目标超出允许目录。")
                    parent.mkdir(parents=True, exist_ok=True)
                    version = re.sub(r"[^\w.-]", "_", str(metadata.get("package_version", "model"))).strip(".") or "model"
                    final = parent / version
                    if final.exists():
                        try:
                            existing = _metadata(final, source, target)
                            return final, existing
                        except OfflineTranslationError:
                            # Keep an incomplete older directory for diagnosis;
                            # never delete or overwrite a cached valid package.
                            final = parent / f"{version}-{uuid.uuid4().hex[:8]}"
                    _cancel(cancel_event)
                    if not unpacked.resolve().is_relative_to(temp_dir.resolve()):
                        raise OfflineTranslationError("模型临时目录超出本次下载范围。")
                    if not final.resolve().is_relative_to(cache.resolve()):
                        raise OfflineTranslationError("模型缓存目标超出允许目录。")
                    shutil.move(str(unpacked), str(final))
                    return final, metadata
                except TranscriptionCancelled:
                    raise
                except OfflineTranslationError as exc:
                    last_error = exc
                    if isinstance(exc, _UnsupportedTokenizer):
                        break
    name = f"{LANGUAGE_NAMES[source]} → {LANGUAGE_NAMES[target]}"
    raise OfflineTranslationError(f"无法准备 {name} 的 SentencePiece 离线模型：{last_error}") from last_error


def translate_texts(
    texts: list[str], source_language: str, target_language: str,
    model_cache: Optional[Path] = None, progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> list[str]:
    """Return one translation for every cue; preserve order and blank cues.

    A complete local direct/pivot route performs no network requests. The first
    use of a new route fetches the official package index and needed model data.
    """
    source, target = _language(source_language), _language(target_language)
    _cancel(cancel_event)
    if source == target or not texts:
        return list(texts)
    if not all(isinstance(text, str) for text in texts):
        raise ValueError("字幕文本必须是字符串列表。")
    try:
        import ctranslate2
        import sentencepiece
    except ImportError as exc:
        raise OfflineTranslationError("缺少离线翻译依赖，请通过 Start.cmd 启动并完成依赖安装。") from exc
    cache = (Path(model_cache) if model_cache is not None else default_model_cache()).expanduser().resolve()
    cache.mkdir(parents=True, exist_ok=True)
    # The GUI serializes jobs. This also avoids duplicate downloads from callers
    # in the same process while allowing cancellation during lock acquisition.
    while not _LOCK.acquire(timeout=0.2):
        _cancel(cancel_event)
    try:
        _cancel(cancel_event)
        local = _local_models(cache)
        route = _route(source, target, local)
        if route is None:
            _notify(progress, "正在查询官方离线翻译模型索引……", 0)
            available = _index_models(_read_index(cache, cancel_event))
            route = _route(source, target, set(local) | set(available))
            if route is None:
                raise OfflineTranslationError(
                    f"官方模型暂时没有 {LANGUAGE_NAMES[source]} → {LANGUAGE_NAMES[target]} 的直接或英文中转路径。"
                )
            for pair in route:
                if pair not in local:
                    local[pair] = _install(pair, available[pair], cache, progress, cancel_event)
        result = list(texts)
        total_steps = len(route) * len(texts)
        completed = 0
        for pair in route:
            _cancel(cancel_event)
            package_dir, metadata = local[pair]
            name = f"{LANGUAGE_NAMES[pair[0]]} → {LANGUAGE_NAMES[pair[1]]}"
            _notify(progress, f"正在加载 {name} 本地翻译模型……", completed / total_steps * 100)
            try:
                translator = ctranslate2.Translator(
                    str(package_dir / "model"), device="cpu", compute_type="int8",
                    inter_threads=1, intra_threads=max(1, min(os.cpu_count() or 1, 4)),
                )
                tokenizer = sentencepiece.SentencePieceProcessor(model_file=str(package_dir / "sentencepiece.model"))
            except Exception as exc:
                raise OfflineTranslationError(f"无法加载本地翻译模型：{package_dir}") from exc
            try:
                prefix = metadata.get("target_prefix", "")
                converted = []
                for index, text in enumerate(result):
                    _cancel(cancel_event)
                    if not text.strip():
                        translated = text
                    else:
                        tokens = tokenizer.encode(" ".join(text.split()), out_type=str)
                        # Avoid silently truncating long imported subtitle cues.
                        if len(tokens) > 4096:
                            raise OfflineTranslationError(f"第 {index + 1} 条字幕过长，请分成更短的句子后再翻译。")
                        kwargs = {"target_prefix": [[prefix]]} if prefix else {}
                        hypotheses = translator.translate_batch(
                            [tokens], beam_size=4, num_hypotheses=1,
                            replace_unknowns=True, length_penalty=0.2,
                            max_input_length=4096, max_decoding_length=4096,
                            **kwargs,
                        )
                        _cancel(cancel_event)
                        output_tokens = hypotheses[0].hypotheses[0]
                        if prefix and output_tokens and output_tokens[0] == prefix:
                            output_tokens = output_tokens[1:]
                        translated = " ".join(tokenizer.decode_pieces(output_tokens).replace("▁", " ").split())
                        if prefix and translated.startswith(prefix):
                            translated = translated[len(prefix):].strip()
                        if not translated:
                            raise OfflineTranslationError(f"第 {index + 1} 条字幕未得到译文，请重试或使用在线翻译。")
                    converted.append(translated)
                    completed += 1
                    _notify(progress, f"离线翻译 {name}：{index + 1}/{len(result)} 条", completed / total_steps * 100)
                result = converted
            except (TranscriptionCancelled, OfflineTranslationError):
                raise
            except Exception as exc:
                raise OfflineTranslationError(f"本地 {name} 翻译失败，请重试或改用在线翻译。") from exc
            finally:
                # Pivot models are loaded one at a time to keep CPU RAM modest.
                translator.unload_model()
                del translator
                del tokenizer
        return result
    finally:
        _LOCK.release()
