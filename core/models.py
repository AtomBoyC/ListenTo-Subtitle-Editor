"""Pydantic data models for ListenTo Studio v2.3."""

from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field


CueStatus = Literal["untranslated", "translated", "edited", "polished", "skipped", "error"]
SubtitleFormat = Literal["srt", "ass", "ssa", "vtt", "txt"]
ExportTrackMode = Literal[
    "bilingual_tgt_top",
    "bilingual_src_top",
    "target_only",
    "source_only",
]
TranslationMode = Literal["reflective", "fast"]
AssStylePresetId = Literal["cinema_outline", "netflix_box", "classic_yellow", "tech_cyan"]


class GlossaryItem(BaseModel):
    source: str = Field(..., min_length=1, max_length=200)
    target: str = Field(..., min_length=1, max_length=200)
    note: str = Field(default="", max_length=300)
    enabled: bool = True


class SubtitleCue(BaseModel):
    id: int = Field(..., ge=1)
    start_ms: int = Field(..., ge=0)
    end_ms: int = Field(..., ge=0)
    source_text: str = ""
    target_text: str = ""
    speaker: str = ""
    style: str = "Default"
    ass_tags: str = ""
    raw_event_meta: Dict[str, str] = Field(default_factory=dict)
    is_special: bool = False
    status: CueStatus = "untranslated"
    note: str = ""


class SubtitleDocument(BaseModel):
    doc_id: str = ""
    filename: str = "untitled.srt"
    source_format: Literal["srt", "ass", "ssa", "vtt"] = "srt"
    encoding: str = "utf-8"
    source_language: str = "auto"
    target_language: str = "zh"
    scene_summary: str = ""
    genre: str = "general"
    custom_prompt: str = ""
    glossary: List[GlossaryItem] = Field(default_factory=list)
    cues: List[SubtitleCue] = Field(default_factory=list)
    ass_header: str = ""
    vtt_header: str = ""
    warnings: List[str] = Field(default_factory=list)


class ProviderPreset(BaseModel):
    id: str
    name: str
    badge: str
    base_url: str
    default_model: str
    models: List[str]
    requires_key: bool = True
    doc_hint: str = ""


class APISettings(BaseModel):
    provider_id: str = "deepseek"
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-flash"
    temperature: float = Field(default=0.25, ge=0.0, le=1.5)
    batch_size: int = Field(default=12, ge=1, le=30)
    context_window: int = Field(default=4, ge=0, le=10)
    concurrency: int = Field(default=2, ge=1, le=6)
    timeout: float = Field(default=90.0, ge=10.0, le=300.0)
    default_mode: TranslationMode = "reflective"
    default_target_lang: str = "zh"
    asr_provider_id: str = "siliconflow"
    asr_base_url: str = "https://api.siliconflow.cn/v1"
    asr_api_key: str = ""
    asr_model: str = "FunAudioLLM/SenseVoiceSmall"


class APISettingsPublic(BaseModel):
    provider_id: str
    base_url: str
    has_api_key: bool
    masked_api_key: str
    model: str
    temperature: float
    batch_size: int
    context_window: int
    concurrency: int
    timeout: float
    default_mode: TranslationMode
    default_target_lang: str
    asr_provider_id: str
    asr_base_url: str
    has_asr_api_key: bool
    masked_asr_api_key: str
    asr_model: str


class APISettingsUpdate(BaseModel):
    provider_id: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    clear_api_key: bool = False
    model: Optional[str] = None
    temperature: Optional[float] = None
    batch_size: Optional[int] = None
    context_window: Optional[int] = None
    concurrency: Optional[int] = None
    timeout: Optional[float] = None
    default_mode: Optional[TranslationMode] = None
    default_target_lang: Optional[str] = None
    asr_provider_id: Optional[str] = None
    asr_base_url: Optional[str] = None
    asr_api_key: Optional[str] = None
    clear_asr_api_key: bool = False
    asr_model: Optional[str] = None


class TranslateBatchRequest(BaseModel):
    cues: List[SubtitleCue]
    cue_ids: Optional[List[int]] = None
    source_language: str = "auto"
    target_language: str = "zh"
    mode: TranslationMode = "reflective"
    genre: str = "general"
    scene_summary: str = ""
    custom_prompt: str = ""
    glossary: List[GlossaryItem] = Field(default_factory=list)
    override_settings: Optional[APISettingsUpdate] = None


class SingleCueRewriteRequest(BaseModel):
    cue_id: int
    cues: List[SubtitleCue]
    source_language: str = "auto"
    target_language: str = "zh"
    action: Literal["retranslate", "natural", "shorten", "formal", "custom"] = "retranslate"
    instruction: str = ""
    scene_summary: str = ""
    glossary: List[GlossaryItem] = Field(default_factory=list)
    override_settings: Optional[APISettingsUpdate] = None


class GlossaryExtractRequest(BaseModel):
    cues: List[SubtitleCue]
    source_language: str = "auto"
    target_language: str = "zh"
    override_settings: Optional[APISettingsUpdate] = None


class ExportRequest(BaseModel):
    document: SubtitleDocument
    formats: List[SubtitleFormat] = Field(default_factory=lambda: ["srt"])
    track_mode: ExportTrackMode = "bilingual_tgt_top"
    ass_secondary_scale: int = Field(default=72, ge=40, le=100)
    ass_style_preset: AssStylePresetId = "cinema_outline"
    fallback_to_source: bool = True


class TimeShiftRequest(BaseModel):
    cues: List[SubtitleCue]
    offset_ms: int
    cue_ids: Optional[List[int]] = None


class SplitCueRequest(BaseModel):
    cues: List[SubtitleCue]
    cue_id: int


class QAFixRequest(BaseModel):
    cues: List[SubtitleCue]
    fix_overlaps: bool = True
    clean_punctuation: bool = True
    target_language: str = "zh"


class BatchZipExportRequest(BaseModel):
    documents: List[SubtitleDocument]
    formats: List[SubtitleFormat] = Field(default_factory=lambda: ["srt"])
    track_mode: ExportTrackMode = "bilingual_tgt_top"
    ass_secondary_scale: int = Field(default=72, ge=40, le=100)
    ass_style_preset: AssStylePresetId = "cinema_outline"
    fallback_to_source: bool = True
