"""Local audio/video transcription with faster-whisper.

Importing this module does not import the inference runtime or download a model.
The first transcription downloads the selected public model if it is not cached.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from translation_core import TranslationOptions


MODELS = ("tiny", "base", "small", "medium", "large-v3", "turbo")
LANGUAGES = ("auto", "zh", "en", "ja", "ko", "fr", "de", "es")
FORMATS = ("srt", "vtt", "txt")


@dataclass(frozen=True)
class TranscriptionOptions:
    model: str = "base"
    language: str = "auto"
    device: str = "cpu"
    compute_type: str = "int8"
    output_dir: Optional[Path] = None
    formats: tuple[str, ...] = ("srt", "txt")
    model_cache: Optional[Path] = None
    beam_size: int = 5
    translation: Optional["TranslationOptions"] = None


@dataclass(frozen=True)
class ProgressUpdate:
    stage: str
    message: str
    percent: Optional[float] = None


@dataclass(frozen=True)
class TranscriptionResult:
    input_path: Path
    output_paths: dict[str, Path]
    language: str
    duration: float
    segment_count: int
    text: str
    output_language: Optional[str] = None


@dataclass(frozen=True)
class SubtitleSegment:
    start_ms: int
    end_ms: int
    text: str


class TranscriptionCancelled(Exception):
    """The user cancelled; no subtitle outputs were saved."""


class NoSpeechError(ValueError):
    """No nonempty speech segments were returned by the recognizer."""


ProgressCallback = Callable[[ProgressUpdate], None]


def _check_cancel(cancel_event: Optional[threading.Event]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise TranscriptionCancelled("已取消，未保存字幕文件。")


def _notify(
    callback: Optional[ProgressCallback],
    stage: str,
    message: str,
    percent: Optional[float] = None,
) -> None:
    if callback is not None:
        try:
            callback(ProgressUpdate(stage, message, percent))
        except Exception:
            # A display failure must not turn a successful export into a failure.
            logging.getLogger(__name__).exception("Progress callback failed")


def _finite_seconds(value: object) -> Optional[float]:
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def normalize_segments(
    segments: Iterable[object], duration: Optional[float] = None
) -> list[SubtitleSegment]:
    """Sort timestamps, remove blank/invalid cues, and guarantee positive lengths.

    Sub-millisecond and zero-length cues become one millisecond long. Cues
    outside a known positive media duration are omitted. Text is kept in Unicode
    and collapsed to one line, so model output cannot break SRT cue boundaries.
    """
    duration_seconds = _finite_seconds(duration)
    duration_ms = (
        max(1, round(duration_seconds * 1000))
        if duration_seconds is not None and duration_seconds > 0
        else None
    )
    normalized = []
    for segment in segments:
        text = " ".join(str(getattr(segment, "text", "") or "").split())
        if not text:
            continue
        start = _finite_seconds(getattr(segment, "start", None))
        end = _finite_seconds(getattr(segment, "end", None))
        if start is None or end is None or end < 0:
            continue
        start_ms = max(0, round(start * 1000))
        if duration_ms is not None and start_ms >= duration_ms:
            continue
        end_ms = max(start_ms + 1, round(end * 1000))
        if duration_ms is not None:
            end_ms = min(end_ms, duration_ms)
        normalized.append(SubtitleSegment(start_ms, end_ms, text))
    normalized.sort(key=lambda item: (item.start_ms, item.end_ms))
    return normalized


def _timestamp(milliseconds: int, decimal: str = ",") -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{decimal}{millis:03d}"


def render_subtitles(segments: list[SubtitleSegment], format_name: str) -> str:
    """Render SRT, WebVTT, or a plain transcript, without touching files."""
    if not segments:
        raise NoSpeechError("没有识别到可用语音，未生成空白字幕。")
    if format_name == "txt":
        return "\n".join(segment.text for segment in segments) + "\n"
    if format_name == "srt":
        return "\n\n".join(
            f"{index}\n{_timestamp(segment.start_ms)} --> "
            f"{_timestamp(segment.end_ms)}\n{segment.text}"
            for index, segment in enumerate(segments, 1)
        ) + "\n"
    if format_name == "vtt":
        # WebVTT cue text uses HTML entities for literal markup characters.
        def escape(text: str) -> str:
            return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        return "WEBVTT\n\n" + "\n\n".join(
            f"{_timestamp(segment.start_ms, '.')} --> "
            f"{_timestamp(segment.end_ms, '.')}\n{escape(segment.text)}"
            for segment in segments
        ) + "\n"
    raise ValueError(f"不支持的输出格式：{format_name}")


def _validate_options(options: TranscriptionOptions) -> tuple[str, ...]:
    if options.model not in MODELS:
        raise ValueError(f"不支持的模型：{options.model}")
    if options.language not in LANGUAGES:
        raise ValueError(f"不支持的识别语言：{options.language}")
    if options.device not in ("cpu", "cuda", "auto"):
        raise ValueError("device 必须为 cpu、cuda 或 auto。")
    if not options.compute_type:
        raise ValueError("compute_type 不能为空。")
    if not isinstance(options.beam_size, int) or options.beam_size < 1:
        raise ValueError("beam_size 必须为正整数。")
    if isinstance(options.formats, str):
        raise ValueError("formats 应为格式列表，例如 ('srt', 'txt')。")
    formats = tuple(dict.fromkeys(options.formats))
    if not formats or any(item not in FORMATS for item in formats):
        raise ValueError("请选择 srt、vtt、txt 中的至少一种输出格式。")
    return formats


def _language_code(language: object, requested: str) -> str:
    candidate = str(language or "").lower()
    if re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2,4})?", candidate):
        return candidate
    return requested if requested != "auto" else "und"


def _missing_model_files(directory: Path) -> list[str]:
    """Check inference/tokenizer assets before allowing a cache-only load.

    Hub snapshot directories can exist after a partially interrupted download.
    preprocessor_config.json is optional in faster-whisper and uses defaults.
    """
    required = ("model.bin", "config.json", "tokenizer.json")
    missing = [
        name for name in required
        if not (directory / name).is_file() or (directory / name).stat().st_size == 0
    ]
    if not any(path.is_file() and path.stat().st_size > 0 for path in directory.glob("vocabulary.*")):
        missing.append("vocabulary.*")
    return missing


def _save_outputs(
    source: Path,
    output_dir: Path,
    language: str,
    contents: dict[str, str],
    cancel_event: Optional[threading.Event],
) -> dict[str, Path]:
    """Exclusively create a matching set of files, adding a shared suffix as needed."""
    _check_cancel(cancel_event)
    output_dir.mkdir(parents=True, exist_ok=True)
    base = f"{source.stem}.{language}"
    sequence = 1
    while True:
        _check_cancel(cancel_event)
        suffix = "" if sequence == 1 else f".{sequence}"
        paths = {
            format_name: output_dir / f"{base}{suffix}.{format_name}"
            for format_name in contents
        }
        # lexists also recognizes broken symlinks, which must not be replaced.
        if any(
            os.path.lexists(path)
            or os.path.normcase(str(path.resolve())) == os.path.normcase(str(source))
            for path in paths.values()
        ):
            sequence += 1
            continue
        opened = []
        try:
            for format_name, path in paths.items():
                handle = path.open("x", encoding="utf-8", newline="\n")
                opened.append((format_name, path, handle))
            for format_name, path, handle in opened:
                _check_cancel(cancel_event)
                handle.write(contents[format_name])
                handle.flush()
            _check_cancel(cancel_event)
        except BaseException as error:
            for _, path, handle in opened:
                handle.close()
                path.unlink(missing_ok=True)
            if isinstance(error, FileExistsError):
                sequence += 1
                continue
            raise
        else:
            for _, _, handle in opened:
                handle.close()
            return paths


def transcribe_media(
    input_path: str | Path,
    options: Optional[TranscriptionOptions] = None,
    progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> TranscriptionResult:
    """Transcribe one local media file and save selected UTF-8 subtitle formats.

    Cancellation is cooperative: checks happen before/after loading the model,
    between generated segments, and while exporting. A model download or one
    inference segment cannot be interrupted until its underlying call returns.
    Recognition uses the original language; it does not translate subtitles.
    """
    options = options or TranscriptionOptions()
    formats = _validate_options(options)
    if options.translation is not None:
        from translation_core import validate_options
        validate_options(options.translation)
    source = Path(input_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"找不到音频或视频文件：{source}")
    if source.stat().st_size == 0:
        raise ValueError("输入文件为空，请选择包含语音的音频或视频。")
    output_dir = (
        Path(options.output_dir).expanduser().resolve()
        if options.output_dir is not None
        else source.parent
    )
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError(f"输出位置不是文件夹：{output_dir}")
    _check_cancel(cancel_event)
    _notify(progress, "loading", "正在加载模型；首次使用会下载模型，请保持联网。")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    try:
        from faster_whisper import WhisperModel
        from faster_whisper.utils import download_model
        from huggingface_hub.errors import LocalEntryNotFoundError
    except ImportError as error:
        raise RuntimeError(
            "未找到 faster-whisper 运行依赖，请先使用安装脚本完成安装。"
        ) from error
    _check_cancel(cancel_event)
    cache_dir = (
        str(Path(options.model_cache).expanduser().resolve())
        if options.model_cache is not None else None
    )
    try:
        model_directory = Path(download_model(options.model, cache_dir=cache_dir, local_files_only=True))
    except LocalEntryNotFoundError:
        model_directory = None
    _check_cancel(cancel_event)
    if model_directory is None or _missing_model_files(model_directory):
        _check_cancel(cancel_event)
        _notify(progress, "loading", "模型尚未缓存完整，正在联网下载或补全；下载完成后可离线使用。")
        model_directory = Path(download_model(options.model, cache_dir=cache_dir, local_files_only=False))
        _check_cancel(cancel_event)
        missing = _missing_model_files(model_directory)
        if missing:
            raise RuntimeError("模型下载不完整，缺少文件：" + "、".join(missing))
    model = WhisperModel(
        str(model_directory),
        local_files_only=True,
        device=options.device,
        compute_type=options.compute_type,
    )
    _check_cancel(cancel_event)
    _notify(progress, "transcribing", "正在读取音轨并识别语音…", 0.0)
    raw_segments, info = model.transcribe(
        str(source),
        language=None if options.language == "auto" else options.language,
        task="transcribe",
        beam_size=options.beam_size,
        vad_filter=True,
    )
    _check_cancel(cancel_event)
    duration = _finite_seconds(getattr(info, "duration", None)) or 0.0
    duration = max(0.0, duration)
    language = _language_code(getattr(info, "language", None), options.language)
    raw = []
    last_percent = 0.0
    try:
        iterator = iter(raw_segments)
        while True:
            _check_cancel(cancel_event)
            try:
                segment = next(iterator)
            except StopIteration:
                break
            _check_cancel(cancel_event)
            raw.append(segment)
            end = _finite_seconds(getattr(segment, "end", None)) or 0.0
            if duration > 0:
                last_percent = max(last_percent, min(99.0, max(0.0, end / duration * 100)))
            _notify(
                progress,
                "transcribing",
                f"已识别 {len(raw)} 个片段（{max(0.0, end):.1f} 秒）",
                last_percent * 0.70 if options.translation is not None and duration > 0 else (last_percent if duration > 0 else None),
            )
    finally:
        close = getattr(raw_segments, "close", None)
        if callable(close):
            close()
    _check_cancel(cancel_event)
    segments = normalize_segments(raw, duration)
    if not segments:
        raise NoSpeechError("没有识别到可用语音，未生成空白字幕。请检查音轨和识别语言。")
    output_language = language
    if options.translation is not None:
        from translation_core import translate_segments
        def translation_progress(update):
            mapped = None if update.percent is None else 70.0 + min(100.0, max(0.0, update.percent)) * 0.29
            _notify(progress, update.stage, update.message, mapped)
        segments = translate_segments(segments, language, options.translation, progress=translation_progress,
                                      cancel_event=cancel_event)
        _check_cancel(cancel_event)
        target = options.translation.target_language
        output_language = f"{language}-{target}" if options.translation.bilingual and target != language else target
    contents = {format_name: render_subtitles(segments, format_name) for format_name in formats}
    _notify(progress, "saving", "正在保存字幕…", 99.0)
    output_paths = _save_outputs(source, output_dir, output_language, contents, cancel_event)
    result = TranscriptionResult(
        input_path=source,
        output_paths=output_paths,
        language=language,
        duration=duration,
        segment_count=len(segments),
        text=render_subtitles(segments, "txt"),
        output_language=output_language,
    )
    _notify(progress, "done", f"完成，共保存 {len(output_paths)} 个文件。", 100.0)
    return result


def main(argv: Optional[list[str]] = None) -> int:
    from runtime_config import model_cache
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="将本地视频或音频中的语音识别为 SRT / VTT / TXT 字幕。首次使用会下载模型。"
    )
    parser.add_argument("input", type=Path, help="本地音频或视频文件路径")
    parser.add_argument("--model", choices=MODELS, default="base", help="识别模型（默认 base）")
    parser.add_argument("--language", choices=LANGUAGES, default="auto", help="语音语言（默认自动检测）")
    parser.add_argument("--device", choices=("cpu", "cuda", "auto"), default="cpu")
    parser.add_argument("--compute-type", default="int8", help="推理精度（默认 int8）")
    parser.add_argument("--output-dir", type=Path, help="输出文件夹（默认输入文件所在文件夹）")
    parser.add_argument("--format", dest="formats", nargs="+", choices=FORMATS, default=["srt", "txt"])
    parser.add_argument("--model-cache", type=Path, default=model_cache("subtitle"), help="模型缓存文件夹")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--target-language", choices=LANGUAGES[1:], help="翻译目标语言；省略时不翻译")
    parser.add_argument("--translation-mode", choices=("offline", "online"), default="offline")
    parser.add_argument("--bilingual", action="store_true", help="导出原文与译文两行字幕")
    parser.add_argument("--translation-cache", type=Path, help="离线翻译模型缓存目录")
    parser.add_argument("--api-base-url", default="https://api.openai.com/v1")
    parser.add_argument("--api-model", default="", help="在线服务商的模型名称；密钥从 SUBTITLE_API_KEY 环境变量读取")
    args = parser.parse_args(argv)
    translation = None
    if args.target_language:
        from translation_core import TranslationOptions
        translation = TranslationOptions(backend=args.translation_mode, target_language=args.target_language,
            bilingual=args.bilingual, model_cache=args.translation_cache, online_base_url=args.api_base_url,
            online_model=args.api_model, api_key=os.environ.get("SUBTITLE_API_KEY", ""))
    options = TranscriptionOptions(
        model=args.model,
        language=args.language,
        device=args.device,
        compute_type=args.compute_type,
        output_dir=args.output_dir,
        formats=tuple(args.formats),
        model_cache=args.model_cache,
        beam_size=args.beam_size,
        translation=translation,
    )

    def report(update: ProgressUpdate) -> None:
        prefix = f"[{update.percent:5.1f}%] " if update.percent is not None else ""
        print(prefix + update.message, file=sys.stderr)

    try:
        result = transcribe_media(args.input, options, progress=report)
    except (KeyboardInterrupt, TranscriptionCancelled):
        print("已取消。", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"处理失败：{error}", file=sys.stderr)
        return 1
    for format_name, path in result.output_paths.items():
        print(f"{format_name.upper()}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
