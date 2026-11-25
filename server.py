"""FastAPI Backend Server for ListenTo Studio v2.0 (听见 · AI 字幕精修工作台)."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from core.config_store import (
    IS_DESKTOP,
    PROJECTS_DIR,
    PROVIDER_PRESETS,
    RESOURCE_DIR,
    _IN_DOCKER,
    default_exports_dir,
    load_settings,
    resolve_effective_settings,
    save_settings,
    to_public_settings,
)
from core.models import (
    APISettingsUpdate,
    ExportRequest,
    GlossaryExtractRequest,
    SingleCueRewriteRequest,
    SubtitleCue,
    SubtitleDocument,
    TimeShiftRequest,
    TranslateBatchRequest,
)
from core.parser import (
    SubtitleParseError,
    decode_subtitle_bytes,
    export_document_format,
    parse_subtitle_content,
    shift_cues_timestamps,
    split_bilingual_cues,
)
from core.translator import (
    GENRE_GUIDES,
    LANGUAGE_LABELS,
    LLMTranslationError,
    extract_glossary_and_summary,
    rewrite_single_cue,
    stream_translate_cues,
    test_api_connection,
)

STATIC_DIR = RESOURCE_DIR / "static"
SAMPLES_DIR = RESOURCE_DIR / "samples"
EXPORTS_DIR = default_exports_dir()

BUILTIN_SAMPLES: List[Dict[str, str]] = [
    {
        "id": "tech_keynote",
        "title": "英文技术演讲 · 跨行断句与术语演示 (SRT)",
        "filename": "AI_Product_Keynote_EN.srt",
        "badge": "英文 → 中文",
        "desc": "包含跨时间轴长句拆分、专有名词及原版易翻错句式对比。",
    },
    {
        "id": "anime_ja_ass",
        "title": "日文带样式番剧对白 · ASS 特效保留演示 (ASS)",
        "filename": "Anime_Dialogue_Styled_JA.ass",
        "badge": "日文 → 中文",
        "desc": "包含顶部注释特效、说话人字段、坐标定位标签与日常口语对白。",
    },
    {
        "id": "bilingual_fix",
        "title": "旧版机翻双语字幕 · 智能分轨与精修演示 (SRT)",
        "filename": "Legacy_Machine_Translated.srt",
        "badge": "双语分轨精修",
        "desc": "演示一键将已有双语字幕拆分为原文轨+译文轨，并针对性重写错译。",
    },
]

app = FastAPI(
    title="ListenTo Studio API",
    version="2.0.0",
    description="Modern AI Subtitle Translation & Bilingual Visual Editor",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health_check() -> Dict[str, str]:
    return {"status": "ok", "version": "2.0.0"}


@app.get("/api/meta")
async def get_meta() -> Dict[str, Any]:
    settings = load_settings()
    runtime = "docker" if _IN_DOCKER else ("desktop" if IS_DESKTOP else "local")
    return {
        "runtime": runtime,
        "exports_dir": str(EXPORTS_DIR),
        "presets": [p.model_dump() for p in PROVIDER_PRESETS],
        "languages": [{"code": k, "label": v} for k, v in LANGUAGE_LABELS.items()],
        "genres": [{"id": k, "desc": v} for k, v in GENRE_GUIDES.items()],
        "samples": BUILTIN_SAMPLES,
        "settings": to_public_settings(settings).model_dump(),
    }


@app.post("/api/desktop/open-exports")
async def open_exports_folder(request: Request) -> Dict[str, Any]:
    if _IN_DOCKER:
        raise HTTPException(status_code=400, detail="服务端容器模式不支持打开宿主机文件夹，请直接通过浏览器下载。")
    client_host = (request.client.host if request.client else "") or ""
    if client_host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(status_code=403, detail="仅允许本机桌面会话打开本地目录。")
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(EXPORTS_DIR)])
        elif sys.platform == "win32":
            os.startfile(str(EXPORTS_DIR))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(EXPORTS_DIR)])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"无法打开导出目录：{exc}") from exc
    return {"ok": True, "path": str(EXPORTS_DIR)}


@app.get("/api/settings")
async def get_settings() -> Dict[str, Any]:
    return to_public_settings(load_settings()).model_dump()


@app.post("/api/settings")
async def update_settings(payload: APISettingsUpdate) -> Dict[str, Any]:
    saved = save_settings(payload)
    return to_public_settings(saved).model_dump()


@app.post("/api/settings/test")
async def test_settings(payload: APISettingsUpdate) -> Dict[str, Any]:
    effective = resolve_effective_settings(payload)
    try:
        result = await test_api_connection(effective)
        return result
    except LLMTranslationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/subtitles/parse")
async def upload_and_parse(
    file: UploadFile = File(...),
    encoding: str = Form("auto"),
) -> Dict[str, Any]:
    raw_bytes = await file.read()
    if len(raw_bytes) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="字幕文件超过 25MB 限制。")
    try:
        content, used_enc = decode_subtitle_bytes(raw_bytes, encoding=encoding)
        doc = parse_subtitle_content(
            content,
            filename=file.filename or "subtitle.srt",
            encoding_used=used_enc,
        )
        return doc.model_dump()
    except SubtitleParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/samples/{sample_id}")
async def load_sample(sample_id: str) -> Dict[str, Any]:
    meta = next((s for s in BUILTIN_SAMPLES if s["id"] == sample_id), None)
    if not meta:
        raise HTTPException(status_code=404, detail="未找到对应示例字幕。")
    sample_path = SAMPLES_DIR / meta["filename"]
    if not sample_path.is_file():
        raise HTTPException(status_code=404, detail="示例文件尚未生成。")
    content, used_enc = decode_subtitle_bytes(sample_path.read_bytes(), "auto")
    doc = parse_subtitle_content(content, filename=meta["filename"], encoding_used=used_enc)
    if sample_id == "bilingual_fix":
        split_cues, count = split_bilingual_cues(doc.cues)
        doc.cues = split_cues
        doc.source_language = "en"
        doc.target_language = "zh"
        if count:
            doc.warnings.append(f"已自动将 {count} 条双语字幕拆分为「原文 + 待校对译文」双轨，可直接点击单句润色或修改。")
    return doc.model_dump()


@app.post("/api/subtitles/split-bilingual")
async def api_split_bilingual(cues: List[SubtitleCue]) -> Dict[str, Any]:
    updated, count = split_bilingual_cues(cues)
    return {
        "split_count": count,
        "cues": [c.model_dump() for c in updated],
    }


@app.post("/api/subtitles/shift")
async def api_shift_timestamps(payload: TimeShiftRequest) -> Dict[str, Any]:
    shifted = shift_cues_timestamps(payload.cues, payload.offset_ms, payload.cue_ids)
    return {"cues": [c.model_dump() for c in shifted]}


@app.post("/api/ai/extract-glossary")
async def api_extract_glossary(payload: GlossaryExtractRequest) -> Dict[str, Any]:
    effective = resolve_effective_settings(payload.override_settings)
    try:
        return await extract_glossary_and_summary(
            payload.cues,
            payload.source_language,
            payload.target_language,
            effective,
        )
    except LLMTranslationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/ai/translate-stream")
async def api_translate_stream(payload: TranslateBatchRequest) -> StreamingResponse:
    effective = resolve_effective_settings(payload.override_settings)

    async def event_generator():
        try:
            async for event in stream_translate_cues(payload, effective):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except LLMTranslationError as exc:
            err_evt = {"event": "error", "message": str(exc)}
            yield f"data: {json.dumps(err_evt, ensure_ascii=False)}\n\n"
        except Exception as exc:
            err_evt = {"event": "error", "message": f"翻译意外中断：{exc}"}
            yield f"data: {json.dumps(err_evt, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/ai/rewrite-cue")
async def api_rewrite_cue(payload: SingleCueRewriteRequest) -> Dict[str, Any]:
    effective = resolve_effective_settings(payload.override_settings)
    try:
        return await rewrite_single_cue(payload, effective)
    except LLMTranslationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/subtitles/export")
async def api_export_subtitles(payload: ExportRequest) -> Dict[str, Any]:
    doc = payload.document
    stem = Path(doc.filename).stem
    # Strip previous language suffixes like .en-zh or .zh if present
    stem = re.sub(r"\.(?:[a-z]{2}(?:-[a-z]{2})?|converted)$", "", stem, flags=re.IGNORECASE)

    lang_tag = doc.target_language
    if payload.track_mode in ("bilingual_tgt_top", "bilingual_src_top"):
        lang_tag = f"{doc.source_language}-{doc.target_language}"
    elif payload.track_mode == "source_only":
        lang_tag = doc.source_language

    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    exported_files: List[Dict[str, str]] = []

    try:
        for fmt in dict.fromkeys(payload.formats):
            rendered = export_document_format(
                doc,
                target_format=fmt,
                track_mode=payload.track_mode,
                ass_secondary_scale=payload.ass_secondary_scale,
                fallback_to_source=payload.fallback_to_source,
            )
            out_name = f"{stem}.{lang_tag}.{fmt}"
            disk_path = EXPORTS_DIR / out_name
            disk_path.write_bytes(rendered.encode("utf-8"))
            exported_files.append(
                {
                    "format": fmt,
                    "filename": out_name,
                    "saved_path": str(disk_path),
                    "content": rendered,
                }
            )
    except SubtitleParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"files": exported_files, "exports_dir": str(EXPORTS_DIR)}


@app.post("/api/projects/save")
async def save_project(doc: SubtitleDocument) -> Dict[str, Any]:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", doc.doc_id or "default")[:24] or "default"
    doc.doc_id = safe_id
    target = PROJECTS_DIR / f"{safe_id}.json"
    target.write_text(
        json.dumps(doc.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"ok": True, "doc_id": safe_id, "path": str(target)}


@app.get("/api/projects")
async def list_projects() -> Dict[str, Any]:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    items: List[Dict[str, Any]] = []
    for f in sorted(PROJECTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            cues = raw.get("cues", [])
            translated = sum(1 for c in cues if c.get("target_text", "").strip())
            items.append(
                {
                    "doc_id": raw.get("doc_id", f.stem),
                    "filename": raw.get("filename", f.name),
                    "source_format": raw.get("source_format", "srt"),
                    "cue_count": len(cues),
                    "translated_count": translated,
                    "updated_at": int(f.stat().st_mtime),
                }
            )
        except Exception:
            continue
    return {"projects": items}


@app.get("/api/projects/{doc_id}")
async def load_project(doc_id: str) -> Dict[str, Any]:
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", doc_id)[:24]
    target = PROJECTS_DIR / f"{safe_id}.json"
    if not target.is_file():
        raise HTTPException(status_code=404, detail="未找到保存的项目。")
    return json.loads(target.read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index_page() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )
