"""Convert ASS/SSA styles and event metadata through pysubs2, without sorting."""
from __future__ import annotations

import re


class AssFormatConversionError(ValueError):
    """A nonstandard layout cannot safely use the library conversion path."""


def _canonical_document(content: str, source_format: str) -> tuple[str, list[str]]:
    from pysubs2.formats.substation import EVENT_FORMAT_LINE, STYLE_FORMAT_LINE

    event_fields = EVENT_FORMAT_LINE[source_format].split(":", 1)[1].strip().split(", ")
    style_fields = STYLE_FORMAT_LINE[source_format].split(":", 1)[1].strip().split(", ")
    expected_events = [name.lower() for name in event_fields]
    expected_styles = [name.lower() for name in style_fields]
    style_section = "v4+ styles" if source_format == "ass" else "v4 styles"
    known_sections = {"script info", "events", style_section, "fonts", "graphics", "aegisub project garbage"}
    section = ""
    current_styles = None
    converted = []
    extras = []
    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        if re.fullmatch(r"\[[^\[\]]+\]", stripped):
            section = stripped[1:-1].lower()
            current_styles = None
        if section and section not in known_sections:
            if section in {"v4 styles", "v4+ styles"}:
                raise AssFormatConversionError("样式节类型与文件格式不一致，不能安全转换样式。")
            extras.append(line)
            continue
        if section == "events" and stripped.lower().startswith("format:"):
            names = [item.strip().lower() for item in stripped.split(":", 1)[1].split(",")]
            if names != expected_events:
                raise AssFormatConversionError("非标准 Events 字段顺序需要使用兼容转换。")
        if section == style_section:
            if stripped.lower().startswith("format:"):
                current_styles = [item.strip().lower() for item in stripped.split(":", 1)[1].split(",")]
                if len(current_styles) != len(set(current_styles)) or set(current_styles) != set(expected_styles):
                    raise AssFormatConversionError("样式 Format 字段不完整或含扩展字段，不能安全转换样式。")
                converted.append(STYLE_FORMAT_LINE[source_format] + "\n")
                continue
            if stripped.lower().startswith("style:"):
                if current_styles is None:
                    raise AssFormatConversionError("Style 行之前缺少样式 Format。")
                values = stripped.split(":", 1)[1].lstrip().split(",")
                if len(values) != len(current_styles):
                    raise AssFormatConversionError("样式字段数量不完整，不能安全转换样式。")
                by_name = dict(zip(current_styles, values))
                converted.append("Style: " + ",".join(by_name[name] for name in expected_styles) + "\n")
                continue
        converted.append(line)
    return "".join(converted), extras


def convert_ass_format(content: str, source_format: str, target_format: str) -> str:
    """Retain styles, actor/margins/effects, comments and opaque attachments.

    ASS features outside SSA's capabilities follow the library's conversion.
    Unexpected event field layouts are rejected so the caller can explicitly
    use a fallback based on its own strict parser, with a compatibility warning.
    """
    if source_format not in ("ass", "ssa") or target_format not in ("ass", "ssa"):
        raise AssFormatConversionError("格式必须为 ASS 或 SSA。")
    if source_format == target_format:
        return content
    import pysubs2

    canonical, extra_lines = _canonical_document(content, source_format)
    expected_count = len(re.findall(r"^\s*(?:Dialogue|Comment):", canonical, re.M | re.I))
    try:
        subtitles = pysubs2.SSAFile.from_string(canonical, format_=source_format)
        if len(subtitles.events) != expected_count:
            raise AssFormatConversionError("样式转换解析的事件数量不一致，不能安全转换。")
        if any(event.start < 0 or event.end < event.start or event.end > 35_999_990 for event in subtitles.events):
            raise AssFormatConversionError("ASS/SSA 时间超出可转换范围。")
        identity = [(event.start, event.end, event.text, event.type, event.style, event.name,
                     event.marginl, event.marginr, event.marginv, event.effect)
                    for event in subtitles.events]
        rendered = subtitles.to_string(target_format)
        check = pysubs2.SSAFile.from_string(rendered, format_=target_format)
        converted_identity = [(event.start, event.end, event.text, event.type, event.style, event.name,
                               event.marginl, event.marginr, event.marginv, event.effect)
                              for event in check.events]
        if converted_identity != identity:
            raise AssFormatConversionError("样式转换改变了事件字段，不能安全转换。")
    except AssFormatConversionError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
        raise AssFormatConversionError("ASS/SSA 样式解析失败，不能安全转换样式。") from exc
    # Keep sections unsupported by the library (for example Aegisub Extradata).
    if extra_lines:
        rendered = rendered.rstrip("\n") + "\n\n" + "".join(extra_lines)
    return rendered
