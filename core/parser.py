"""Lossless-aware subtitle parser, language detector, and multi-format exporter."""

import html
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pysubs2

from core.models import (
    AssStylePresetId,
    ExportTrackMode,
    SubtitleCue,
    SubtitleDocument,
    SubtitleFormat,
)

SUPPORTED_INPUT_FORMATS = ("srt", "ass", "ssa", "vtt")
SUPPORTED_OUTPUT_FORMATS = ("srt", "ass", "ssa", "vtt", "txt")

_SRT_BLOCK_RE = re.compile(
    r"(?:^|\n)\s*(?:(\d+)\s*\n)?\s*"
    r"(\d{1,3}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{1,3}:\d{2}:\d{2}[,.]\d{3})(?:[ \t]+[^\n]*)?\n"
    r"([\s\S]*?)(?=\n{2,}\s*(?:\d+\s*\n\s*)?\d{1,3}:\d{2}:\d{2}[,.]\d{3}\s*-->|\Z)",
    re.MULTILINE,
)
_VTT_CUE_RE = re.compile(
    r"^(?:([^\n]+)\n)?"
    r"((?:\d{1,3}:)?\d{2}:\d{2}\.\d{3})\s*-->\s*((?:\d{1,3}:)?\d{2}:\d{2}\.\d{3})(?:[ \t]+([^\n]*))?\n"
    r"([\s\S]+)$"
)
_ASS_OVERRIDE_RE = re.compile(r"\{[^{}]*\}")
_LEADING_ASS_TAGS_RE = re.compile(r"^(?:\{[^{}]*\})+")
_HTML_TAG_RE = re.compile(r"</?(?:b|i|u|s|font|span|c|v|ruby|rt|lang)(?:\s[^>]*)?>", re.IGNORECASE)
_VTT_VOICE_RE = re.compile(r"^<v(?:\.[^\s>]+)?\s+([^>]+)>([\s\S]*?)(?:</v>)?$", re.IGNORECASE)


class SubtitleParseError(ValueError):
    """Raised when a subtitle file is unreadable or contains no valid cues."""


def decode_subtitle_bytes(data: bytes, encoding: str = "auto") -> Tuple[str, str]:
    """Decode raw bytes with automatic multi-encoding detection for CJK & Western subtitles."""
    if not data:
        raise SubtitleParseError("字幕文件为空。")

    if encoding and encoding != "auto":
        try:
            text = data.decode(encoding, errors="strict")
            return _normalize_newlines(text.lstrip("\ufeff")), encoding
        except UnicodeError as exc:
            raise SubtitleParseError(f"无法按指定编码 {encoding} 解码字幕文件：{exc}") from exc

    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16", errors="strict")
        return _normalize_newlines(text.lstrip("\ufeff")), "utf-16"
    if data.startswith(b"\xef\xbb\xbf"):
        text = data.decode("utf-8-sig", errors="strict")
        return _normalize_newlines(text), "utf-8-sig"

    for candidate in ("utf-8", "gb18030", "big5", "shift_jis", "cp1252"):
        try:
            text = data.decode(candidate, errors="strict")
            if "\x00" in text:
                continue
            return _normalize_newlines(text.lstrip("\ufeff")), candidate
        except UnicodeError:
            continue

    text = data.decode("utf-8", errors="replace")
    return _normalize_newlines(text.lstrip("\ufeff")), "utf-8"


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_timestamp_ms(ts: str) -> int:
    """Parse SRT/VTT/ASS timestamp into integer milliseconds."""
    cleaned = ts.strip().replace(",", ".")
    parts = cleaned.split(":")
    if len(parts) == 2:
        hours = 0
        minutes = int(parts[0])
        sec_part = parts[1]
    elif len(parts) == 3:
        hours = int(parts[0])
        minutes = int(parts[1])
        sec_part = parts[2]
    else:
        raise SubtitleParseError(f"无效的时间戳格式：{ts}")

    if "." in sec_part:
        sec_str, frac_str = sec_part.split(".", 1)
        seconds = int(sec_str)
        frac_str = (frac_str + "000")[:3]
        millis = int(frac_str)
    else:
        seconds = int(sec_part)
        millis = 0

    return ((hours * 3600) + (minutes * 60) + seconds) * 1000 + millis


def format_timestamp_srt(ms: int, sep: str = ",") -> str:
    ms = max(0, int(round(ms)))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{sep}{millis:03d}"


def format_timestamp_ass(ms: int) -> str:
    ms = max(0, int(round(ms)))
    cs = (ms + 5) // 10
    hours, rem = divmod(cs, 360_000)
    minutes, rem = divmod(rem, 6000)
    seconds, centis = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centis:02d}"


def detect_subtitle_language(cues: List[SubtitleCue]) -> str:
    """Heuristic language detector across CJK, Japanese, Korean, Russian, and European scripts."""
    sample = " ".join(c.source_text for c in cues[:80] if not c.is_special)[:4000]
    if not sample.strip():
        return "en"

    hiragana_katakana = len(re.findall(r"[\u3040-\u30ff]", sample))
    hangul = len(re.findall(r"[\uac00-\ud7af]", sample))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", sample))
    cyrillic = len(re.findall(r"[\u0400-\u04ff]", sample))
    latin = len(re.findall(r"[A-Za-z]", sample))

    if hiragana_katakana >= 3:
        return "ja"
    if hangul >= 3:
        return "ko"
    if cjk >= 4 and cjk >= latin * 0.25:
        return "zh"
    if cyrillic >= 5:
        return "ru"

    lower = f" {sample.lower()} "
    lang_markers = {
        "fr": (" le ", " la ", " les ", " des ", " est ", " pas ", " vous ", " nous ", " c'est "),
        "es": (" el ", " la ", " los ", " las ", " que ", " por ", " para ", " como ", " pero "),
        "de": (" der ", " die ", " das ", " und ", " ich ", " nicht ", " ist ", " wir ", " sie "),
    }
    best_lang, best_hits = "en", 0
    for code, words in lang_markers.items():
        hits = sum(lower.count(w) for w in words)
        if hits > best_hits and hits >= 3:
            best_lang, best_hits = code, hits
    return best_lang


def _clean_html_cue_text(raw: str) -> Tuple[str, str]:
    speaker = ""
    stripped = raw.strip()
    voice_match = _VTT_VOICE_RE.match(stripped)
    if voice_match:
        speaker = voice_match.group(1).strip()
        stripped = voice_match.group(2).strip()
    stripped = re.sub(r"<br\s*/?>", "\n", stripped, flags=re.IGNORECASE)
    stripped = _HTML_TAG_RE.sub("", stripped)
    stripped = html.unescape(stripped)
    lines = [line.strip() for line in stripped.split("\n")]
    return "\n".join(line for line in lines if line != ""), speaker


def _clean_ass_text(raw: str) -> Tuple[str, str, bool]:
    leading_match = _LEADING_ASS_TAGS_RE.match(raw)
    leading_tags = leading_match.group(0) if leading_match else ""

    has_drawing = bool(re.search(r"\\p[1-9]", raw))
    has_karaoke = bool(re.search(r"\\k[fot]?\d+", raw, re.IGNORECASE))
    text_no_tags = _ASS_OVERRIDE_RE.sub("", raw)
    plain = (
        text_no_tags.replace(r"\N", "\n")
        .replace(r"\n", "\n")
        .replace(r"\h", " ")
        .strip()
    )
    is_special = has_drawing or (has_karaoke and not plain)
    return plain, leading_tags, is_special


def parse_subtitle_content(
    content: str,
    filename: str = "subtitle.srt",
    encoding_used: str = "utf-8",
) -> SubtitleDocument:
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext not in SUPPORTED_INPUT_FORMATS:
        raise SubtitleParseError(f"不支持的字幕后缀 .{ext}（支持 SRT、ASS、SSA、VTT）。")

    warnings: List[str] = []
    cues: List[SubtitleCue] = []
    ass_header = ""
    vtt_header = ""

    if ext == "srt":
        for idx, match in enumerate(_SRT_BLOCK_RE.finditer(content), start=1):
            start_ms = parse_timestamp_ms(match.group(2))
            end_ms = parse_timestamp_ms(match.group(3))
            if end_ms <= start_ms:
                end_ms = start_ms + 500
            clean_text, speaker = _clean_html_cue_text(match.group(4))
            if not clean_text:
                continue
            cues.append(
                SubtitleCue(
                    id=len(cues) + 1,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    source_text=clean_text,
                    speaker=speaker,
                )
            )
    elif ext == "vtt":
        blocks = re.split(r"\n{2,}", content.strip())
        if not blocks or not blocks[0].lstrip().startswith("WEBVTT"):
            raise SubtitleParseError("WebVTT 文件必须以 WEBVTT 头部开头。")
        vtt_header = blocks[0].strip()
        for block in blocks[1:]:
            b = block.strip()
            if not b or b.startswith(("NOTE", "STYLE", "REGION")):
                continue
            m = _VTT_CUE_RE.match(b)
            if not m:
                continue
            start_ms = parse_timestamp_ms(m.group(2))
            end_ms = parse_timestamp_ms(m.group(3))
            if end_ms <= start_ms:
                end_ms = start_ms + 500
            clean_text, speaker = _clean_html_cue_text(m.group(5))
            if not clean_text:
                continue
            cues.append(
                SubtitleCue(
                    id=len(cues) + 1,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    source_text=clean_text,
                    speaker=speaker,
                    raw_event_meta={"vtt_settings": (m.group(4) or "").strip()},
                )
            )
    else:
        # ASS / SSA lossless-aware parsing
        header_lines: List[str] = []
        in_events = False
        event_fields: Optional[List[str]] = None

        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.lower() == "[events]":
                in_events = True
                header_lines.append(line)
                continue
            if not in_events:
                header_lines.append(line)
                continue
            if stripped.startswith("[") and stripped.endswith("]"):
                in_events = False
                continue
            if stripped.lower().startswith("format:"):
                event_fields = [f.strip().lower() for f in stripped.split(":", 1)[1].split(",")]
                header_lines.append(line)
                continue
            event_match = re.match(r"^(Dialogue|Comment)\s*:(.*)$", line, flags=re.IGNORECASE)
            if not event_match or not event_fields:
                continue
            kind = event_match.group(1).capitalize()
            payload = event_match.group(2)
            parts = payload.split(",", len(event_fields) - 1)
            if len(parts) < len(event_fields):
                continue
            field_map = {name: val for name, val in zip(event_fields, parts)}
            start_ms = parse_timestamp_ms(field_map.get("start", "0:00:00.00"))
            end_ms = parse_timestamp_ms(field_map.get("end", "0:00:00.50"))
            raw_text = field_map.get("text", "")
            plain_text, leading_tags, special_flag = _clean_ass_text(raw_text)
            is_comment = kind.lower() == "comment"
            is_special = is_comment or special_flag

            cues.append(
                SubtitleCue(
                    id=len(cues) + 1,
                    start_ms=start_ms,
                    end_ms=max(start_ms + 10, end_ms),
                    source_text=plain_text if plain_text else raw_text,
                    speaker=field_map.get("name", "").strip(),
                    style=field_map.get("style", "Default").strip() or "Default",
                    ass_tags=leading_tags,
                    raw_event_meta={
                        "kind": kind,
                        "layer": field_map.get("layer", field_map.get("marked", "0")).strip(),
                        "marginl": field_map.get("marginl", "0").strip(),
                        "marginr": field_map.get("marginr", "0").strip(),
                        "marginv": field_map.get("marginv", "0").strip(),
                        "effect": field_map.get("effect", "").strip(),
                        "raw_text": raw_text,
                    },
                    is_special=is_special,
                    status="skipped" if is_special else "untranslated",
                )
            )
        ass_header = "\n".join(header_lines).strip()
        special_count = sum(1 for c in cues if c.is_special)
        if special_count:
            warnings.append(f"检测到 {special_count} 条 ASS 注释/特效绘图行，导出 ASS 时将原样保留，不参与翻译。")

    if not cues:
        raise SubtitleParseError("未解析到任何有效字幕行，请检查文件内容与格式。")

    detected_lang = detect_subtitle_language(cues)
    target_default = "en" if detected_lang == "zh" else "zh"

    return SubtitleDocument(
        doc_id=uuid.uuid4().hex[:12],
        filename=Path(filename).name,
        source_format=ext,  # type: ignore[arg-type]
        encoding=encoding_used,
        source_language=detected_lang,
        target_language=target_default,
        cues=cues,
        ass_header=ass_header,
        vtt_header=vtt_header,
        warnings=warnings,
    )


def split_bilingual_cues(cues: List[SubtitleCue]) -> Tuple[List[SubtitleCue], int]:
    """Split two-line bilingual source cues into source_text + target_text tracks."""
    split_count = 0
    updated: List[SubtitleCue] = []
    for cue in cues:
        if cue.is_special or cue.target_text.strip():
            updated.append(cue)
            continue
        lines = [ln.strip() for ln in cue.source_text.split("\n") if ln.strip()]
        if len(lines) >= 2:
            mid = len(lines) // 2
            first_half = "\n".join(lines[:mid])
            second_half = "\n".join(lines[mid:])
            c_copy = cue.model_copy(deep=True)
            c_copy.source_text = first_half
            c_copy.target_text = second_half
            c_copy.status = "translated"
            updated.append(c_copy)
            split_count += 1
        else:
            updated.append(cue)
    return updated, split_count


def shift_cues_timestamps(
    cues: List[SubtitleCue],
    offset_ms: int,
    cue_ids: Optional[List[int]] = None,
) -> List[SubtitleCue]:
    target_set = set(cue_ids) if cue_ids else None
    result: List[SubtitleCue] = []
    for cue in cues:
        if target_set is not None and cue.id not in target_set:
            result.append(cue)
            continue
        shifted = cue.model_copy(deep=True)
        duration = max(50, shifted.end_ms - shifted.start_ms)
        shifted.start_ms = max(0, shifted.start_ms + offset_ms)
        shifted.end_ms = max(shifted.start_ms + duration if shifted.start_ms == 0 and cue.start_ms + offset_ms < 0 else shifted.start_ms + 50, cue.end_ms + offset_ms)
        result.append(shifted)
    return result


def _compose_cue_lines(
    cue: SubtitleCue,
    track_mode: ExportTrackMode,
    fallback_to_source: bool = True,
) -> Tuple[str, Optional[str]]:
    """Return (primary_line, optional_secondary_line) according to track_mode."""
    src = cue.source_text.strip()
    tgt = cue.target_text.strip()

    if track_mode == "source_only":
        return src, None
    if track_mode == "target_only":
        return (tgt if tgt else (src if fallback_to_source else "")), None
    if track_mode == "bilingual_tgt_top":
        if tgt and tgt != src:
            return tgt, src
        return (tgt or src), None
    # bilingual_src_top
    if tgt and tgt != src:
        return src, tgt
    return src, None


def _split_text_smart(text: str) -> Tuple[str, str]:
    cleaned = (text or "").strip()
    if not cleaned:
        return "", ""
    lines = [ln.strip() for ln in cleaned.split("\n") if ln.strip()]
    if len(lines) >= 2:
        mid = len(lines) // 2
        return "\n".join(lines[:mid]), "\n".join(lines[mid:])

    mid_idx = len(cleaned) // 2
    best_pos = -1
    best_dist = len(cleaned)
    for m in re.finditer(r"[，,。！？!?；;：:、]\s*|\s+", cleaned):
        pos = m.end()
        if 2 <= pos <= len(cleaned) - 2:
            dist = abs(pos - mid_idx)
            if dist < best_dist:
                best_dist = dist
                best_pos = pos

    if best_pos == -1:
        best_pos = max(1, mid_idx)
    left = cleaned[:best_pos].strip().rstrip("，,")
    right = cleaned[best_pos:].strip()
    return (left or cleaned), right


def split_single_cue(cues: List[SubtitleCue], cue_id: int) -> List[SubtitleCue]:
    """Split a single cue into two sequential cues with proportional timestamps."""
    out: List[SubtitleCue] = []
    for cue in cues:
        if cue.id != cue_id or cue.is_special:
            out.append(cue.model_copy(deep=True))
            continue

        src1, src2 = _split_text_smart(cue.source_text)
        tgt1, tgt2 = _split_text_smart(cue.target_text) if cue.target_text.strip() else ("", "")
        if not src2:
            src2 = src1

        total_dur = max(400, cue.end_ms - cue.start_ms)
        ratio = len(src1) / max(1, len(src1) + len(src2))
        ratio = min(0.75, max(0.25, ratio))
        split_ms = cue.start_ms + int(total_dur * ratio)

        first = cue.model_copy(deep=True)
        first.end_ms = max(first.start_ms + 180, split_ms - 10)
        first.source_text = src1
        first.target_text = tgt1

        second = cue.model_copy(deep=True)
        second.start_ms = first.end_ms + 20
        second.end_ms = max(second.start_ms + 180, cue.end_ms)
        second.source_text = src2
        second.target_text = tgt2

        out.append(first)
        out.append(second)

    for idx, c in enumerate(out, start=1):
        c.id = idx
    return out


def diagnose_subtitles(cues: List[SubtitleCue]) -> Dict[str, Any]:
    """Inspect subtitle timeline & translations for overlaps, high CPS, missing translations, and punctuation."""
    dialogue = [c for c in cues if not c.is_special]
    overlap_ids: List[int] = []
    untranslated_ids: List[int] = []
    high_cps_ids: List[int] = []
    punctuation_ids: List[int] = []

    for i, cue in enumerate(dialogue):
        if i + 1 < len(dialogue) and cue.end_ms > dialogue[i + 1].start_ms:
            overlap_ids.append(cue.id)

        tgt = (cue.target_text or "").strip()
        if not tgt:
            untranslated_ids.append(cue.id)
            continue

        dur_sec = max(0.4, (cue.end_ms - cue.start_ms) / 1000.0)
        char_len = len(re.sub(r"\s+", "", tgt))
        max_line = max(len(ln.strip()) for ln in tgt.split("\n"))
        if (char_len / dur_sec) > 16.0 or max_line > 34:
            high_cps_ids.append(cue.id)

        if (tgt.endswith("。") and not tgt.endswith("。。")) or "  " in tgt or "\r" in tgt:
            punctuation_ids.append(cue.id)

    total_issues = len(overlap_ids) + len(untranslated_ids) + len(high_cps_ids) + len(punctuation_ids)
    return {
        "total_cues": len(dialogue),
        "total_issues": total_issues,
        "overlap_ids": overlap_ids,
        "untranslated_ids": untranslated_ids,
        "high_cps_ids": high_cps_ids,
        "punctuation_ids": punctuation_ids,
    }


def auto_fix_subtitles(
    cues: List[SubtitleCue],
    fix_overlaps: bool = True,
    clean_punctuation: bool = True,
    target_language: str = "zh",
) -> Tuple[List[SubtitleCue], Dict[str, int]]:
    """Automatically fix timeline overlaps and normalize subtitle punctuation/spacing."""
    updated = [c.model_copy(deep=True) for c in cues]
    fixed_overlaps = 0
    cleaned_punct = 0

    dialogue_indices = [i for i, c in enumerate(updated) if not c.is_special]

    if fix_overlaps:
        for pos in range(len(dialogue_indices) - 1):
            curr_c = updated[dialogue_indices[pos]]
            next_c = updated[dialogue_indices[pos + 1]]
            if curr_c.end_ms > next_c.start_ms:
                fixed_overlaps += 1
                ideal_end = next_c.start_ms - 20
                if ideal_end >= curr_c.start_ms + 200:
                    curr_c.end_ms = ideal_end
                else:
                    curr_c.end_ms = curr_c.start_ms + 250
                    next_c.start_ms = curr_c.end_ms + 20
                    if next_c.end_ms <= next_c.start_ms:
                        next_c.end_ms = next_c.start_ms + 500

    if clean_punctuation:
        for idx in dialogue_indices:
            c = updated[idx]
            orig = c.target_text
            if not orig or not orig.strip():
                continue
            lines = [re.sub(r"[ \t]{2,}", " ", ln.strip()) for ln in orig.replace("\r\n", "\n").split("\n") if ln.strip()]
            if target_language in ("zh", "zh-tw", "ja"):
                cleaned_lines = []
                for ln in lines:
                    if ln.endswith("。") and not ln.endswith("。。") and "。" not in ln[:-1]:
                        ln = ln[:-1]
                    cleaned_lines.append(ln)
                lines = cleaned_lines
            new_txt = "\n".join(lines)
            if new_txt != orig:
                c.target_text = new_txt
                cleaned_punct += 1

    return updated, {
        "fixed_overlaps": fixed_overlaps,
        "cleaned_punctuation": cleaned_punct,
    }


ASS_STYLE_PRESETS: Dict[str, Dict[str, str]] = {
    "cinema_outline": {
        "name": "经典高清描边（白字深黑边 · 通用推荐）",
        "style_line": "Style: Default,PingFang SC,54,&H00FFFFFF,&H000000FF,&H00181C24,&H80000000,0,0,0,0,100,100,0,0,1,2.6,0.8,2,40,40,42,1",
        "sec_color": "&H00D0D8E2&",
    },
    "netflix_box": {
        "name": "Netflix 沉浸暗盒（半透黑底 · 复杂画面极清晰）",
        "style_line": "Style: Default,PingFang SC,52,&H00FFFFFF,&H000000FF,&H5810141E,&H5810141E,0,0,0,0,100,100,0,0,3,10,0,2,40,40,44,1",
        "sec_color": "&H00CBD5E1&",
    },
    "classic_yellow": {
        "name": "美剧经典暖黄（鹅黄主字 + 纯白副字 · 电影感）",
        "style_line": "Style: Default,PingFang SC,54,&H003CE0FF,&H000000FF,&H00101418,&H80000000,-1,0,0,0,100,100,0,0,1,2.8,1.0,2,40,40,42,1",
        "sec_color": "&H00FFFFFF&",
    },
    "tech_cyan": {
        "name": "现代极客浅金（清晰高对比 · 技术教程/发布会）",
        "style_line": "Style: Default,PingFang SC,52,&H00B0F3FF,&H000000FF,&H00221810,&H80000000,0,0,0,0,100,100,0.4,0,1,2.5,0.8,2,40,40,42,1",
        "sec_color": "&H00E2E8F0&",
    },
}


def build_ass_header(preset_id: str = "cinema_outline") -> str:
    preset = ASS_STYLE_PRESETS.get(preset_id, ASS_STYLE_PRESETS["cinema_outline"])
    return f"""[Script Info]
; Generated by ListenTo Studio v2.3 ({preset_id})
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{preset["style_line"]}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"""


DEFAULT_ASS_HEADER = build_ass_header("cinema_outline")


def export_document_format(
    document: SubtitleDocument,
    target_format: SubtitleFormat,
    track_mode: ExportTrackMode = "bilingual_tgt_top",
    ass_secondary_scale: int = 72,
    fallback_to_source: bool = True,
    ass_style_preset: AssStylePresetId = "cinema_outline",
) -> str:
    """Render SubtitleDocument into SRT, VTT, ASS, SSA, or TXT."""
    if target_format not in SUPPORTED_OUTPUT_FORMATS:
        raise SubtitleParseError(f"不支持导出的字幕格式：{target_format}")

    if target_format == "txt":
        lines: List[str] = []
        for cue in document.cues:
            if cue.is_special:
                continue
            primary, secondary = _compose_cue_lines(cue, track_mode, fallback_to_source)
            if primary:
                lines.append(f"{primary}\n{secondary}" if secondary else primary)
        return "\n\n".join(lines) + "\n"

    if target_format == "srt":
        blocks: List[str] = []
        seq = 1
        for cue in document.cues:
            if cue.is_special:
                continue
            primary, secondary = _compose_cue_lines(cue, track_mode, fallback_to_source)
            if not primary:
                continue
            body = f"{primary}\n{secondary}" if secondary else primary
            blocks.append(
                f"{seq}\n{format_timestamp_srt(cue.start_ms, ',')} --> {format_timestamp_srt(cue.end_ms, ',')}\n{body}"
            )
            seq += 1
        return "\n\n".join(blocks) + "\n"

    if target_format == "vtt":
        header = document.vtt_header.strip() if document.vtt_header.strip() else "WEBVTT"
        blocks = [header]
        for cue in document.cues:
            if cue.is_special:
                continue
            primary, secondary = _compose_cue_lines(cue, track_mode, fallback_to_source)
            if not primary:
                continue
            body = f"{html.escape(primary)}\n{html.escape(secondary)}" if secondary else html.escape(primary)
            settings = cue.raw_event_meta.get("vtt_settings", "").strip()
            settings_suffix = f" {settings}" if settings else ""
            blocks.append(
                f"{format_timestamp_srt(cue.start_ms, '.')} --> {format_timestamp_srt(cue.end_ms, '.')}{settings_suffix}\n{body}"
            )
        return "\n\n".join(blocks) + "\n"

    # ASS / SSA rendering
    preset_cfg = ASS_STYLE_PRESETS.get(ass_style_preset, ASS_STYLE_PRESETS["cinema_outline"])
    header = (
        document.ass_header.strip()
        if (document.source_format in ("ass", "ssa") and document.ass_header.strip())
        else build_ass_header(ass_style_preset)
    )
    event_lines: List[str] = []
    sec_tag = rf"{{\fscx{ass_secondary_scale}\fscy{ass_secondary_scale}\1c{preset_cfg['sec_color']}}}"

    for cue in document.cues:
        start_ass = format_timestamp_ass(cue.start_ms)
        end_ass = format_timestamp_ass(max(cue.start_ms + 10, cue.end_ms))
        meta = cue.raw_event_meta or {}
        kind = meta.get("kind", "Dialogue")
        layer = meta.get("layer", "0")
        marginl = meta.get("marginl", "0")
        marginr = meta.get("marginr", "0")
        marginv = meta.get("marginv", "0")
        effect = meta.get("effect", "")
        style = cue.style or "Default"
        speaker = (cue.speaker or "").replace(",", " ")

        if cue.is_special and "raw_text" in meta:
            ass_text = meta["raw_text"]
        else:
            primary, secondary = _compose_cue_lines(cue, track_mode, fallback_to_source)
            if not primary:
                continue
            p_ass = primary.replace("\n", r"\N")
            if secondary:
                s_ass = secondary.replace("\n", r"\N")
                ass_text = f"{cue.ass_tags}{p_ass}\\N{sec_tag}{s_ass}"
            else:
                ass_text = f"{cue.ass_tags}{p_ass}"

        event_lines.append(
            f"{kind}: {layer},{start_ass},{end_ass},{style},{speaker},{marginl},{marginr},{marginv},{effect},{ass_text}"
        )

    ass_content = header + "\n" + "\n".join(event_lines) + "\n"
    if target_format == "ssa":
        try:
            ssa_doc = pysubs2.SSAFile.from_string(ass_content, format_="ass")
            return ssa_doc.to_string("ssa")
        except Exception:
            return ass_content
    return ass_content
