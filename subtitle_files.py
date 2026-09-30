"""Strict subtitle-file translation and conversion, without speech recognition.

Same-format writes retain the source document and replace only cue text ranges.
Parsing deliberately rejects broken cues instead of silently dropping them.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import html
import os
from pathlib import Path
import re
import sys
import threading
from typing import Optional, TYPE_CHECKING

from subtitle_core import (
    ProgressCallback, SubtitleSegment, TranscriptionCancelled,
    _check_cancel, _notify, _save_outputs, _timestamp,
)

if TYPE_CHECKING:
    from translation_core import TranslationOptions

INPUT_FORMATS = ("srt", "ass", "ssa", "vtt")
OUTPUT_FORMATS = (*INPUT_FORMATS, "txt")
ENCODINGS = ("auto", "utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "gb18030")


class SubtitleFileError(ValueError):
    """The file is malformed or cannot be converted without losing meaning."""


@dataclass(frozen=True)
class SubtitleFileOptions:
    output_dir: Optional[Path] = None
    formats: tuple[str, ...] = ("srt",)
    source_language: str = "auto"
    encoding: str = "auto"
    translation: Optional["TranslationOptions"] = None


@dataclass(frozen=True)
class SubtitleFileResult:
    input_path: Path
    output_paths: dict[str, Path]
    language: str
    output_language: str
    duration: float
    segment_count: int
    text: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Cue:
    start_ms: int
    end_ms: int
    text: str
    text_start: int
    text_end: int
    number: str = ""
    kind: str = "dialogue"
    drawing: bool = False
    karaoke: bool = False


@dataclass(frozen=True)
class _Document:
    format: str
    content: str
    cues: tuple[_Cue, ...]


_SRT_TIME = r"\d{2,}:\d{2}:\d{2}[,.]\d{3}"
_VTT_TIME = r"(?:\d{2,}:)?\d{2}:\d{2}\.\d{3}"
_ASS_TIME = r"\d+:\d{2}:\d{2}\.\d{2}"
_PROTECTED = re.compile(r"\{[^{}]*\}|<[^>\n]+>|&(?:#[0-9]+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+);|\\[Nnh]|\n|[{}<>]")
_ASS_OVERRIDE = re.compile(r"\{[^{}]*\}")
_SUBTITLE_HTML = re.compile(r"<(?:/?(?:b|i|u|s|strong|em|font|span|br|v|c|lang|ruby|rt)(?=[\s./>])[^>\n]*|\d{2}:\d{2}(?::\d{2})?\.\d{3})>", re.I)


def _failure(line: int, detail: str) -> SubtitleFileError:
    return SubtitleFileError(f"第 {line} 行字幕格式有误：{detail}。未生成输出文件。")


def _decode(data: bytes, encoding: str) -> str:
    if encoding not in ENCODINGS:
        raise SubtitleFileError("请选择支持的字幕编码。")
    chosen = encoding
    if chosen == "auto":
        chosen = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        text = data.decode(chosen, errors="strict")
    except UnicodeError:
        raise SubtitleFileError(
            "字幕解码失败。自动模式仅接受 UTF-8 或带 BOM 的 UTF-16；"
            "旧中文字幕可显式选择 GB18030，无 BOM 的 UTF-16 请指定 LE 或 BE。"
        ) from None
    if text.startswith("\ufeff"):
        text = text[1:]
    if "\x00" in text:
        raise SubtitleFileError("字幕含 NUL 字符，可能选择了错误编码；UTF-16 无 BOM 时请指定 LE 或 BE。")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _rows(content: str):
    offset = 0
    for line_number, line in enumerate(content.split("\n"), 1):
        yield line_number, offset, line
        offset += len(line) + 1


def _blocks(content: str):
    rows = []
    for row in _rows(content):
        if not row[2].strip():
            if rows:
                yield rows
                rows = []
        else:
            rows.append(row)
    if rows:
        yield rows


def _time(value: str, format_name: str, line: int) -> int:
    pattern = _ASS_TIME if format_name in ("ass", "ssa") else _VTT_TIME if format_name == "vtt" else _SRT_TIME
    if not re.fullmatch(pattern, value):
        raise _failure(line, "时间戳格式不正确")
    components = value.replace(",", ".").split(":")
    if len(components) == 2:
        hours = 0
        minutes, tail = components
    else:
        hours, minutes, tail = components
    seconds, fraction = tail.split(".")
    hours, minutes, seconds = int(hours), int(minutes), int(seconds)
    if minutes > 59 or seconds > 59:
        raise _failure(line, "分钟或秒超出 0 至 59")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + int(fraction) * (10 if format_name in ("ass", "ssa") else 1)


def _range(line: str, format_name: str, line_number: int) -> tuple[int, int]:
    pattern = _VTT_TIME if format_name == "vtt" else _SRT_TIME
    match = re.fullmatch(rf"[ \t]*({pattern})[ \t]+-->[ \t]+({pattern})(?:[ \t]+(.*))?[ \t]*", line)
    if not match:
        raise _failure(line_number, "应为起始时间 --> 结束时间")
    settings = (match.group(3) or "").strip()
    if settings:
        if format_name == "srt":
            if not all(re.fullmatch(r"(?:X1|X2|Y1|Y2):[0-9]+", item, re.I) for item in settings.split()):
                raise _failure(line_number, "SRT 时间行含未知设置")
        elif not all(item.partition(":")[0] in ("vertical", "line", "position", "size", "align", "region") and ":" in item for item in settings.split()):
            raise _failure(line_number, "WebVTT 时间行含未知设置")
    start, end = _time(match.group(1), format_name, line_number), _time(match.group(2), format_name, line_number)
    if end <= start:
        raise _failure(line_number, "结束时间必须大于起始时间")
    return start, end


def _parse_srt(content: str) -> tuple[_Cue, ...]:
    cues = []
    for rows in _blocks(content):
        if len(rows) < 3 or not re.fullmatch(r"[ \t]*[0-9]+[ \t]*", rows[0][2]):
            raise _failure(rows[0][0], "SRT 条目必须包含编号、时间行和非空正文")
        start, end = _range(rows[1][2], "srt", rows[1][0])
        for index in range(2, len(rows) - 1):
            if re.fullmatch(r"[ \t]*[0-9]+[ \t]*", rows[index][2]) and re.match(rf"[ \t]*{_SRT_TIME}[ \t]+-->", rows[index + 1][2]):
                raise _failure(rows[index][0], "字幕条目之间缺少空行")
        text_start, text_end = rows[2][1], rows[-1][1] + len(rows[-1][2])
        cues.append(_Cue(start, end, content[text_start:text_end], text_start, text_end, rows[0][2].strip()))
    return tuple(cues)


def _parse_vtt(content: str) -> tuple[_Cue, ...]:
    blocks = list(_blocks(content))
    if not blocks or not re.fullmatch(r"WEBVTT(?:[ \t].*)?", blocks[0][0][2]):
        raise _failure(1, "WebVTT 文件必须以 WEBVTT 开头")
    if any("-->" in row[2] for row in blocks[0][1:]):
        raise _failure(blocks[0][0][0], "WEBVTT 头部与字幕之间缺少空行")
    cues = []
    for rows in blocks[1:]:
        if re.match(r"NOTE(?:[ \t]|$)", rows[0][2]) or rows[0][2] in ("STYLE", "REGION"):
            continue
        timing_index = 0 if "-->" in rows[0][2] else 1
        if len(rows) <= timing_index + 1:
            raise _failure(rows[0][0], "WebVTT 条目缺少时间行或非空正文")
        start, end = _range(rows[timing_index][2], "vtt", rows[timing_index][0])
        if any(re.match(rf"[ \t]*{_VTT_TIME}[ \t]+-->", row[2]) for row in rows[timing_index + 1:]):
            raise _failure(rows[timing_index][0], "字幕条目之间缺少空行")
        text_start, text_end = rows[timing_index + 1][1], rows[-1][1] + len(rows[-1][2])
        cues.append(_Cue(start, end, content[text_start:text_end], text_start, text_end))
    return tuple(cues)


def _parse_ass(content: str, format_name: str) -> tuple[_Cue, ...]:
    cues = []
    section = ""
    fields = None
    for line_number, offset, line in _rows(content):
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue
        if stripped.startswith("["):
            if not re.fullmatch(r"\[[^\[\]]+\]", stripped):
                raise _failure(line_number, "节标题格式不正确")
            section = stripped[1:-1].lower()
            if section == "events":
                fields = None
            continue
        event = re.match(r"^(Dialogue|Comment):(.*)$", line, re.I)
        if section != "events":
            if event:
                raise _failure(line_number, "字幕事件必须位于 [Events] 节")
            continue
        if stripped.lower().startswith("format:"):
            fields = [item.strip().lower() for item in stripped.split(":", 1)[1].split(",")]
            if len(fields) != len(set(fields)) or not {"start", "end", "text"}.issubset(fields):
                raise _failure(line_number, "Events Format 缺少 Start、End、Text 或含重复字段")
            continue
        if not event or fields is None:
            raise _failure(line_number, "未知事件行或事件前缺少 Format")
        value = event.group(2)
        text_index = fields.index("text")
        left = value.split(",", text_index)
        if len(left) != text_index + 1:
            raise _failure(line_number, "事件字段数量不足")
        tail_count = len(fields) - text_index - 1
        right = left.pop().rsplit(",", tail_count) if tail_count else [left.pop()]
        values = left + right
        if len(values) != len(fields):
            raise _failure(line_number, "事件字段数量与 Format 不一致")
        start = _time(values[fields.index("start")].strip(), format_name, line_number)
        end = _time(values[fields.index("end")].strip(), format_name, line_number)
        if end < start or (end == start and event.group(1).lower() != "comment"):
            raise _failure(line_number, "结束时间必须大于起始时间")
        text = values[text_index]
        if not text.strip() and event.group(1).lower() == "dialogue":
            raise _failure(line_number, "Dialogue 正文不能为空")
        text_start = offset + event.start(2) + sum(len(item) + 1 for item in values[:text_index])
        drawing = any(int(value) > 0 for tag in _ASS_OVERRIDE.findall(text) for value in re.findall(r"\\p([0-9]+)(?![0-9A-Za-z])", tag))
        karaoke = any(re.search(r"\\k[fot]?[0-9]+", tag, re.I) for tag in _ASS_OVERRIDE.findall(text))
        if "effect" in fields and "karaoke" in values[fields.index("effect")].lower():
            karaoke = True
        cues.append(_Cue(start, end, text, text_start, text_start + len(text), kind=event.group(1).lower(), drawing=drawing, karaoke=karaoke))
    return tuple(cues)


def read_subtitle_file(path: Path, encoding: str = "auto") -> _Document:
    """Read and validate every cue without loading inference libraries."""
    source = Path(path).expanduser().resolve()
    format_name = source.suffix.lower().lstrip(".")
    if format_name not in INPUT_FORMATS:
        raise SubtitleFileError("支持的字幕输入格式为 SRT、ASS、SSA 和 VTT。")
    if not source.is_file():
        raise SubtitleFileError("字幕输入文件不存在。")
    content = _decode(source.read_bytes(), encoding)
    if format_name == "srt":
        cues = _parse_srt(content)
    elif format_name == "vtt":
        cues = _parse_vtt(content)
    else:
        cues = _parse_ass(content, format_name)
    if not cues:
        raise SubtitleFileError("字幕文件没有有效条目，未生成空文件。")
    return _Document(format_name, content, cues)


def _plain_text(text: str, format_name: str) -> str:
    if format_name in ("ass", "ssa"):
        parts = []
        drawing = False
        position = 0
        for match in _ASS_OVERRIDE.finditer(text):
            if not drawing:
                parts.append(text[position:match.start()])
            modes = re.findall(r"\\p([0-9]+)(?![0-9A-Za-z])", match.group())
            if modes:
                drawing = int(modes[-1]) > 0
            position = match.end()
        if not drawing:
            parts.append(text[position:])
        text = "".join(parts).replace(r"\N", "\n").replace(r"\n", "\n").replace(r"\h", "\u00a0")
        return text
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    return html.unescape(_SUBTITLE_HTML.sub("", text))


def _convert_inline(text: str, source_format: str, target_format: str) -> str:
    """Carry basic bold/italic/underline while escaping visible literal text."""
    parts = []
    states = {"b": 0, "i": 0, "u": 0}
    drawing = False

    def add_plain(plain):
        if not plain or drawing:
            return
        if target_format in ("ass", "ssa"):
            if "{" in plain or "}" in plain or re.search(r"\\[Nnh]", plain):
                raise SubtitleFileError("正文含字面 ASS 控制字符，无法安全转换；请保留原格式。")
            plain = plain.replace("\n", r"\N").replace("\u00a0", r"\h")
            active = [tag for tag, value in states.items() if value]
            if active:
                plain = "{" + "".join("\\" + tag + "1" for tag in active) + "}" + plain + "{" + "".join("\\" + tag + "0" for tag in active) + "}"
        else:
            plain = html.escape(plain, quote=False)
            active = [tag for tag, value in states.items() if value]
            plain = "".join("<" + tag + ">" for tag in active) + plain + "".join("</" + tag + ">" for tag in reversed(active))
        parts.append(plain)

    if source_format in ("ass", "ssa"):
        token_pattern = re.compile(r"\{[^{}]*\}|\\[Nnh]")
        position = 0
        for match in token_pattern.finditer(text):
            add_plain(text[position:match.start()])
            token = match.group()
            if token.startswith("{"):
                if re.search(r"\\r(?:[^\\}]*)", token):
                    states = {"b": 0, "i": 0, "u": 0}
                for tag, value in re.findall(r"\\([biu])(-?[0-9]+)", token):
                    states[tag] = int(value) != 0
                modes = re.findall(r"\\p([0-9]+)(?![0-9A-Za-z])", token)
                if modes:
                    drawing = int(modes[-1]) > 0
            else:
                add_plain("\u00a0" if token == r"\h" else "\n")
            position = match.end()
        add_plain(text[position:])
    else:
        position = 0
        for match in _SUBTITLE_HTML.finditer(text):
            add_plain(html.unescape(text[position:match.start()]))
            tag = match.group()
            name = re.match(r"</?([A-Za-z]+)", tag)
            if name:
                name = {"strong": "b", "em": "i"}.get(name.group(1).lower(), name.group(1).lower())
                if name in states:
                    states[name] = max(0, states[name] + (-1 if tag.startswith("</") else 1))
                elif name == "br":
                    add_plain("\n")
            position = match.end()
        add_plain(html.unescape(text[position:]))
    return "".join(parts)


def _translate_document(document: _Document, options: SubtitleFileOptions, progress, cancel_event) -> tuple[list[str], list[str]]:
    from translation_core import normalize_language, translate_segments, validate_options

    translation = options.translation
    source = normalize_language(options.source_language)
    target = normalize_language(translation.target_language)
    if source == target:
        return [cue.text for cue in document.cues], []
    validate_options(translation)
    pieces = []
    requests = []
    request_cues = []
    preserved_requests = set()
    warnings = []
    skipped = {"comment": 0, "drawing": 0, "karaoke": 0}
    for cue_index, cue in enumerate(document.cues, 1):
        _check_cancel(cancel_event)
        special = "comment" if cue.kind == "comment" else "drawing" if cue.drawing else "karaoke" if cue.karaoke else None
        if special:
            skipped[special] += 1
            pieces.append(None)
            continue
        tokens = []
        position = 0
        for match in _PROTECTED.finditer(cue.text):
            if match.start() > position:
                tokens.append((False, cue.text[position:match.start()]))
            tokens.append((True, match.group()))
            position = match.end()
        if position < len(cue.text):
            tokens.append((False, cue.text[position:]))
        prepared = []
        for protected, part in tokens:
            if protected or not any(character.isalnum() for character in part):
                prepared.append(part)
                continue
            left = len(part) - len(part.lstrip())
            right = len(part) - len(part.rstrip())
            content = part.strip()
            request_index = len(requests)
            requests.append(SubtitleSegment(cue.start_ms, cue.end_ms, content))
            request_cues.append((cue_index, cue.number))
            prepared.append((request_index, part[:left], part[len(part) - right:] if right else ""))
        pieces.append(prepared)
    if requests:
        def translation_progress(update):
            if update.stage == "translation_warning":
                message = update.message
                fragment = re.match(r"片段 (\d+)：", message)
                if fragment and 1 <= int(fragment.group(1)) <= len(request_cues):
                    request_index = int(fragment.group(1)) - 1
                    if message.endswith("已保留最初原文。"):
                        preserved_requests.add(request_index)
                    cue_index, number = request_cues[request_index]
                    identifier = f"（编号 {number}）" if number else ""
                    message = f"原字幕第 {cue_index} 条{identifier}：" + message[fragment.end():]
                    update = replace(update, message=message)
                warnings.append(message)
            if progress is not None:
                progress(update)

        returned = translate_segments(requests, source, replace(translation, bilingual=False), progress=translation_progress, cancel_event=cancel_event)
        if len(returned) != len(requests) or any((item.start_ms, item.end_ms) != (original.start_ms, original.end_ms) for item, original in zip(returned, requests)):
            raise SubtitleFileError("翻译结果条数或时间轴不一致，未生成输出文件。")
        translated = [requests[index].text if index in preserved_requests else item.text
                      for index, item in enumerate(returned)]
        if any(not isinstance(text, str) or not text.strip() or "\n" in text or "\r" in text or "\x00" in text for text in translated):
            raise SubtitleFileError("翻译结果含空白正文或意外换行，未生成输出文件。")
        if any(_PROTECTED.search(text) or any(character in text for character in "{}<>") for text in translated):
            raise SubtitleFileError("翻译结果意外增加了格式标记，未生成输出文件。")
    else:
        translated = []
    result = []
    for cue, prepared in zip(document.cues, pieces):
        if prepared is None:
            result.append(cue.text)
            continue
        rebuilt = "".join(part if isinstance(part, str) else part[1] + translated[part[0]] + part[2] for part in prepared)
        if translation.bilingual and rebuilt != cue.text and any(not isinstance(part, str) for part in prepared):
            rebuilt = cue.text + (r"\N" if document.format in ("ass", "ssa") else "\n") + rebuilt
        result.append(rebuilt)
    for kind, count in skipped.items():
        if count:
            label = {"comment": "ASS 注释", "drawing": "ASS 绘图（含混合绘图）", "karaoke": "卡拉 OK"}[kind]
            warnings.append(f"{count} 条{label}事件已保留原文，没有送入翻译模型。")
    return result, warnings


def _same_format(document: _Document, texts: list[str]) -> str:
    parts = []
    position = 0
    for cue, text in zip(document.cues, texts):
        parts.append(document.content[position:cue.text_start])
        parts.append(text)
        position = cue.text_end
    parts.append(document.content[position:])
    return "".join(parts)


def _ass_timestamp(milliseconds: int) -> str:
    centiseconds = (milliseconds + 5) // 10
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{fraction:02d}"


def _render_ass(cues: list[tuple[_Cue, str]], format_name: str, raw_ass: bool = False) -> str:
    if format_name == "ssa":
        header = "[Script Info]\nScriptType: v4.00\n\n[V4 Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, TertiaryColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, AlphaLevel, Encoding\nStyle: Default,Arial,20,16777215,16777215,0,0,0,0,1,2,0,2,10,10,10,0,1\n\n[Events]\nFormat: Marked, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        prefix = "Marked=0"
    else:
        header = "[Script Info]\nScriptType: v4.00+\nPlayResX: 1280\nPlayResY: 720\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Default,Arial,32,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,20,20,20,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        prefix = "0"
    rows = []
    for cue, text in cues:
        if cue.end_ms > 35_999_990:
            raise SubtitleFileError("当前工具的 ASS/SSA 跨格式导出支持至 9:59:59.99，请保留 SRT 或 VTT 格式。")
        if not raw_ass and ("{" in text or "}" in text or re.search(r"\\[Nnh]", text)):
            raise SubtitleFileError("跨格式转换的正文含 ASS 控制字符，无法安全表达；请保留原格式或先调整这些文字。")
        rounded_start = (cue.start_ms + 5) // 10 * 10
        rounded_end = (cue.end_ms + 5) // 10 * 10
        if cue.kind != "comment":
            rounded_end = max(rounded_end, rounded_start + 10)
        if rounded_end > 35_999_990:
            raise SubtitleFileError("ASS/SSA 精度转换后的时间超过 9:59:59.99，无法安全导出。")
        start = _ass_timestamp(rounded_start)
        # Very short cues must remain visible after ASS centisecond rounding.
        end = _ass_timestamp(rounded_end)
        ass_text = text.replace("\n", r"\N")
        kind = cue.kind.title() if raw_ass else "Dialogue"
        rows.append(f"{kind}: {prefix},{start},{end},Default,,0,0,0,,{ass_text}\n")
    return header + "".join(rows)


def _cross_format(document: _Document, texts: list[str], format_name: str) -> tuple[str, list[str]]:
    warnings = []
    if document.format in ("ass", "ssa") and format_name in ("ass", "ssa"):
        try:
            from ass_format_convert import convert_ass_format
            converted = convert_ass_format(_same_format(document, texts), document.format, format_name)
            warnings.append("ASS 与 SSA 互转已保留可转换样式和事件字段；目标格式不支持的扩展样式可能丢失。")
            return converted, warnings
        except (ImportError, ValueError) as failure:
            warnings.append(f"ASS/SSA 格式无法完整转换（{failure}），已保留事件文字、注释、绘图和时间，使用默认样式，原样式表及扩展字段可能丢失。")
        warnings.append("ASS/SSA 时间精度为 10 毫秒。")
        return _render_ass(list(zip(document.cues, texts)), format_name, raw_ass=True), warnings
    visible = []
    comments, drawings = 0, 0
    for cue, text in zip(document.cues, texts):
        if cue.kind == "comment":
            comments += 1
            continue
        plain = _plain_text(text, document.format)
        if cue.drawing and not plain.strip():
            drawings += 1
            continue
        if not plain.strip():
            raise SubtitleFileError("条目只有格式标记且没有可见文字，不能安全转换到目标格式。")
        visible.append((cue, plain if format_name == "txt" else _convert_inline(text, document.format, format_name)))
    if comments or drawings:
        warnings.append(f"目标格式无法表达 {comments} 条 ASS 注释和 {drawings} 条纯绘图事件，已跳过；原输入文件保留这些内容。")
    if any(cue.drawing for cue in document.cues):
        warnings.append("混合 ASS 绘图只导出可见文字，绘图路径不会写成普通字幕。")
    if any(cue.karaoke for cue in document.cues):
        warnings.append("跨格式转换会失去卡拉 OK 的逐字时间和效果。")
    if not visible:
        raise SubtitleFileError("目标格式没有可显示的文字字幕，未生成空文件；请使用原 ASS/SSA 格式保留特殊事件。")
    if document.format in ("ass", "ssa", "vtt") or format_name in ("ass", "ssa") or re.search(r"<[^>]+>|\{[^{}]*\}", document.content):
        warnings.append("TXT 只导出可见文字，不保留样式、定位或格式专用设置。" if format_name == "txt" else "跨格式转换保留条目时间和基础粗体、斜体、下划线；高级样式、定位、说话人字段和格式专用设置可能丢失。")
    if format_name == "txt":
        warnings.append("TXT 不包含时间轴，无法据此还原字幕时间。")
        return "\n\n".join(text for _, text in visible) + "\n", warnings
    if format_name in ("ass", "ssa"):
        warnings.append("ASS/SSA 时间精度为 10 毫秒，已四舍五入；不足 10 毫秒的条目至少保留 10 毫秒。")
        return _render_ass(visible, format_name, raw_ass=True), warnings
    if format_name == "vtt":
        return "WEBVTT\n\n" + "\n\n".join(f"{_timestamp(cue.start_ms, '.')} --> {_timestamp(cue.end_ms, '.')}\n{text}" for cue, text in visible) + "\n", warnings
    if any(cue.end_ms >= 360_000_000 for cue, _ in visible):
        raise SubtitleFileError("当前工具的 SRT 跨格式导出支持至 99:59:59.999，请保留 VTT。")
    return "\n\n".join(f"{index}\n{_timestamp(cue.start_ms)} --> {_timestamp(cue.end_ms)}\n{text}" for index, (cue, text) in enumerate(visible, 1)) + "\n", warnings


def process_subtitle_file(input_path: Path, options: SubtitleFileOptions, progress: Optional[ProgressCallback] = None, cancel_event: Optional[threading.Event] = None) -> SubtitleFileResult:
    """Translate and/or convert a file atomically, never overwriting its source."""
    _check_cancel(cancel_event)
    source = Path(input_path).expanduser().resolve()
    if isinstance(options.formats, str):
        raise SubtitleFileError("输出格式应为列表，例如 ('srt', 'ass')。")
    formats = tuple(dict.fromkeys(options.formats))
    if not formats or any(format_name not in OUTPUT_FORMATS for format_name in formats):
        raise SubtitleFileError("请选择 SRT、ASS、SSA、VTT、TXT 中的至少一种输出格式。")
    source_language = str(options.source_language or "auto").strip().lower().replace("_", "-")
    if source_language not in ("auto", "und") and not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", source_language):
        raise SubtitleFileError("原字幕语言应为 auto、zh、en 等语言代码。")
    options = replace(options, source_language=source_language)
    if options.translation is not None and source_language in ("auto", "und"):
        raise SubtitleFileError("翻译已有字幕时请明确选择原字幕语言；纯格式转换可以使用自动语言。")
    _notify(progress, "reading", "正在严格读取字幕文件……", 0)
    document = read_subtitle_file(source, options.encoding)
    _check_cancel(cancel_event)
    warnings = []
    if options.translation is not None:
        texts, translation_warnings = _translate_document(document, options, progress, cancel_event)
        warnings.extend(translation_warnings)
        from translation_core import normalize_language
        language = normalize_language(options.source_language)
        target = normalize_language(options.translation.target_language)
        output_language = f"{language}-{target}" if options.translation.bilingual and language != target else target
    else:
        texts = [cue.text for cue in document.cues]
        language = "und" if options.source_language in ("auto", "und", "", None) else options.source_language
        output_language = language
    contents = {}
    for format_name in formats:
        _check_cancel(cancel_event)
        if format_name == document.format:
            contents[format_name] = _same_format(document, texts)
        else:
            contents[format_name], format_warnings = _cross_format(document, texts, format_name)
            warnings.extend(format_warnings)
    _check_cancel(cancel_event)
    _notify(progress, "exporting", "正在保存字幕……", 98)
    name_language = output_language if output_language != "und" else "converted"
    output_dir = Path(options.output_dir).expanduser().resolve() if options.output_dir is not None else source.parent
    outputs = _save_outputs(source, output_dir, name_language, contents, cancel_event)
    visible_texts = [_plain_text(text, document.format) for cue, text in zip(document.cues, texts) if cue.kind != "comment"]
    visible_texts = [text for text in visible_texts if text.strip()]
    _notify(progress, "complete", "字幕处理完成。", 100)
    return SubtitleFileResult(source, outputs, language, output_language, max(cue.end_ms for cue in document.cues) / 1000, len(visible_texts), "\n".join(visible_texts), tuple(dict.fromkeys(warnings)))


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="已有 SRT/ASS/SSA/VTT 字幕翻译与格式转换")
    parser.add_argument("input", type=Path)
    parser.add_argument("--format", dest="formats", nargs="+", choices=OUTPUT_FORMATS, default=["srt"])
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--source-language", default="auto")
    parser.add_argument("--encoding", choices=ENCODINGS, default="auto")
    parser.add_argument("--target-language")
    parser.add_argument("--bilingual", action="store_true")
    parser.add_argument("--translation-mode", choices=("offline", "online"), default="offline")
    parser.add_argument("--translation-cache", type=Path)
    parser.add_argument("--api-base-url", default="https://api.openai.com/v1")
    parser.add_argument("--api-model", default="")
    args = parser.parse_args(argv)
    try:
        translation = None
        if args.target_language:
            from runtime_config import model_cache
            from translation_core import TranslationOptions
            translation = TranslationOptions(backend=args.translation_mode, target_language=args.target_language, bilingual=args.bilingual, model_cache=args.translation_cache or model_cache("translation"), online_base_url=args.api_base_url, online_model=args.api_model, api_key=os.environ.get("SUBTITLE_API_KEY", ""))
        options = SubtitleFileOptions(args.output_dir, tuple(args.formats), args.source_language, args.encoding, translation)
        result = process_subtitle_file(args.input, options, progress=lambda update: print(update.message, file=sys.stderr))
    except (KeyboardInterrupt, TranscriptionCancelled):
        print("已取消。", file=sys.stderr)
        return 130
    except Exception as failure:
        print(f"处理失败：{failure}", file=sys.stderr)
        return 1
    for warning in result.warnings:
        print(f"提示：{warning}", file=sys.stderr)
    for format_name, path in result.output_paths.items():
        print(f"{format_name.upper()}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
