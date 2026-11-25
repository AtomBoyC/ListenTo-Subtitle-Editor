"""Comprehensive automated unit & functional integration tests for ListenTo Studio v2.0."""

import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from core import config_store
from core.models import APISettingsUpdate, SubtitleCue
from core.parser import (
    decode_subtitle_bytes,
    export_document_format,
    parse_subtitle_content,
    shift_cues_timestamps,
    split_bilingual_cues,
)
from core.translator import _is_local_url, extract_json_payload, partition_scene_batches
from server import app


def _mock_openai_handler(request: httpx.Request) -> httpx.Response:
    if request.method == "GET" and request.url.path.endswith("/models"):
        if "generativelanguage.googleapis.com" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "models/gemini-3.8-flash"},
                        {"name": "models/gemini-3.5-flash"},
                        {"name": "models/text-embedding-004"},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-6.2-sol"},
                    {"id": "gpt-6.2-astra"},
                    {"id": "claude-sonnet-5-5"},
                    {"id": "deepseek-v4-pro"},
                    {"id": "text-embedding-3-large"},
                    {"id": "whisper-1"},
                ]
            },
        )

    body = json.loads(request.content.decode("utf-8"))
    user_msg = body["messages"][-1]["content"]
    sys_msg = body["messages"][0]["content"]

    if user_msg == "ping":
        raw_content = json.dumps({"ok": True, "greeting": "pong"})
    elif "scene_summary" in sys_msg and "glossary" in sys_msg and "to_translate" not in user_msg:
        raw_content = json.dumps(
            {
                "scene_summary": "一段关于字幕工作台升级的产品技术演示。",
                "genre": "tech",
                "glossary": [{"source": "ListenTo Studio", "target": "听见 Studio", "note": "产品名"}],
            },
            ensure_ascii=False,
        )
    elif "current_cue" in user_msg:
        payload = json.loads(user_msg)
        cid = payload["current_cue"]["id"]
        raw_content = (
            "<think>Let me polish this line naturally.</think>\n```json\n"
            + json.dumps(
                {
                    "best": f"欢迎使用听见字幕工作台（精修 #{cid}）",
                    "alternatives": ["欢迎来到听见字幕工作室", "大家好，欢迎体验听见 Studio"],
                    "reason": "消除生硬直译，符合中文产品演示习惯",
                },
                ensure_ascii=False,
            )
            + "\n```"
        )
    else:
        payload = json.loads(user_msg)
        items = payload.get("to_translate", [])
        raw_content = (
            "```json\n"
            + json.dumps(
                {
                    "reflection": "注意第3与第4条为跨时间轴长句，按中文语序顺畅衔接。",
                    "translations": [
                        {"id": it["id"], "text": f"地道中文译文 #{it['id']}。"}
                        for it in items
                    ],
                },
                ensure_ascii=False,
            )
            + "\n```"
        )

    envelope = {
        "id": "chatcmpl-mock",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": raw_content}, "finish_reason": "stop"}],
    }
    return httpx.Response(200, json=envelope)


class StudioTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="listento-ut-")
        cls.orig_data_dir = config_store.DATA_DIR
        cls.orig_settings_file = config_store.SETTINGS_FILE
        cls.orig_projects_dir = config_store.PROJECTS_DIR

        test_data = Path(cls.temp_dir.name) / "data"
        config_store.DATA_DIR = test_data
        config_store.SETTINGS_FILE = test_data / "settings.json"
        config_store.PROJECTS_DIR = test_data / "projects"
        import server
        server.PROJECTS_DIR = config_store.PROJECTS_DIR
        server.EXPORTS_DIR = Path(cls.temp_dir.name) / "exports"

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        config_store.DATA_DIR = cls.orig_data_dir
        config_store.SETTINGS_FILE = cls.orig_settings_file
        config_store.PROJECTS_DIR = cls.orig_projects_dir
        cls.temp_dir.cleanup()

    def test_encoding_detection_gb18030_and_utf8(self):
        gbk_bytes = "1\n00:00:01,000 --> 00:00:02,500\n你好，旧版中文字幕\n".encode("gb18030")
        decoded, enc = decode_subtitle_bytes(gbk_bytes, "auto")
        self.assertEqual(enc, "gb18030")
        doc = parse_subtitle_content(decoded, "legacy.srt", enc)
        self.assertEqual(doc.source_language, "zh")
        self.assertEqual(doc.cues[0].source_text, "你好，旧版中文字幕")

    def test_vtt_upload_parse_and_roundtrip_export(self):
        vtt_text = (
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:03.200 align:center\n"
            "<v SpeakerA>Welcome to <b>ListenTo</b> WebVTT test!</v>\n"
        )
        resp = self.client.post(
            "/api/subtitles/parse",
            files={"file": ("demo.vtt", vtt_text.encode("utf-8"), "text/vtt")},
            data={"encoding": "auto"},
        )
        self.assertEqual(resp.status_code, 200)
        doc_json = resp.json()
        self.assertEqual(doc_json["source_format"], "vtt")
        self.assertEqual(doc_json["cues"][0]["speaker"], "SpeakerA")
        self.assertEqual(doc_json["cues"][0]["source_text"], "Welcome to ListenTo WebVTT test!")

    def test_ass_lossless_tags_and_bilingual_export(self):
        ass_path = Path(__file__).resolve().parent.parent / "samples" / "Anime_Dialogue_Styled_JA.ass"
        raw, enc = decode_subtitle_bytes(ass_path.read_bytes(), "auto")
        doc = parse_subtitle_content(raw, ass_path.name, enc)
        self.assertEqual(doc.source_language, "ja")
        self.assertTrue(doc.cues[0].is_special)
        self.assertEqual(doc.cues[1].ass_tags, r"{\an8\pos(960,90)}")

        doc.cues[1].target_text = "第三音乐教室·放学后"
        doc.cues[2].target_text = "你好！真没想到今天也是我第一个到呢"
        rendered_ass = export_document_format(doc, "ass", track_mode="bilingual_tgt_top", ass_secondary_scale=72)
        self.assertIn("Comment: 0,0:00:00.00,0:00:05.00", rendered_ass)
        self.assertIn(r"{\an8\pos(960,90)}第三音乐教室·放学后\N{\fscx72\fscy72", rendered_ass)

        rendered_srt = export_document_format(doc, "srt", track_mode="bilingual_tgt_top")
        self.assertNotIn("OP FX template line", rendered_srt)
        self.assertIn("第三音乐教室·放学后\n第三音楽室・放課後", rendered_srt)

        rendered_ssa = export_document_format(doc, "ssa", track_mode="target_only")
        self.assertIn("[V4 Styles]", rendered_ssa)

    def test_split_bilingual_and_time_shift(self):
        sample = Path(__file__).resolve().parent.parent / "samples" / "Legacy_Machine_Translated.srt"
        raw, enc = decode_subtitle_bytes(sample.read_bytes(), "auto")
        doc = parse_subtitle_content(raw, sample.name, enc)
        split_cues, count = split_bilingual_cues(doc.cues)
        self.assertEqual(count, 4)
        self.assertEqual(split_cues[0].source_text, "Hello, welcome to our subtitle editor.")
        self.assertIn("欢迎收看", split_cues[0].target_text)

        shifted = shift_cues_timestamps(split_cues, 500)
        self.assertEqual(shifted[0].start_ms, 1623)

    def test_docker_health_and_desktop_security_guards(self):
        h_resp = self.client.get("/api/health")
        self.assertEqual(h_resp.status_code, 200)
        self.assertEqual(h_resp.json()["status"], "ok")
        self.assertTrue(_is_local_url("http://host.docker.internal:11434/v1"))
        self.assertTrue(_is_local_url("http://ollama:11434/v1"))
        self.assertFalse(_is_local_url("https://api.deepseek.com/v1"))

        # Verify Docker mode blocks /api/desktop/open-exports
        import server
        with patch.object(server, "_IN_DOCKER", True):
            blocked = self.client.post("/api/desktop/open-exports")
            self.assertEqual(blocked.status_code, 400)

        # Verify desktop path resolution across macOS and Windows
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LISTENTO_DATA_DIR", None)
            with patch.object(config_store, "IS_DESKTOP", True):
                with patch("sys.platform", "darwin"):
                    mac_dir = config_store._default_data_dir()
                    self.assertIn("Application Support", str(mac_dir))
                with patch("sys.platform", "win32"):
                    win_dir = config_store._default_data_dir()
                    self.assertTrue(str(win_dir).endswith("ListenToStudio"))

    def test_settings_persistence_and_project_storage(self):
        s1 = self.client.post(
            "/api/settings",
            json={
                "provider_id": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "api_key": "sk-testsecret123456",
                "batch_size": 15,
            },
        )
        self.assertEqual(s1.status_code, 200)
        self.assertTrue(s1.json()["has_api_key"])
        self.assertEqual(s1.json()["masked_api_key"], "sk-t••••••••3456")

        # Subsequent save without api_key keeps existing secret intact
        s2 = self.client.post("/api/settings", json={"batch_size": 10})
        self.assertEqual(s2.status_code, 200)
        self.assertTrue(s2.json()["has_api_key"])
        self.assertEqual(s2.json()["batch_size"], 10)

        # Project save & list & load
        sample_doc = self.client.get("/api/samples/tech_keynote").json()
        sample_doc["doc_id"] = "proj_ut_01"
        sample_doc["cues"][0]["target_text"] = "大家好，欢迎来到听见 Studio"
        save_r = self.client.post("/api/projects/save", json=sample_doc)
        self.assertEqual(save_r.status_code, 200)

        list_r = self.client.get("/api/projects")
        self.assertEqual(list_r.status_code, 200)
        self.assertTrue(any(p["doc_id"] == "proj_ut_01" for p in list_r.json()["projects"]))

        load_r = self.client.get("/api/projects/proj_ut_01")
        self.assertEqual(load_r.status_code, 200)
        self.assertEqual(load_r.json()["cues"][0]["target_text"], "大家好，欢迎来到听见 Studio")

        del_r = self.client.delete("/api/projects/proj_ut_01")
        self.assertEqual(del_r.status_code, 200)
        list_after = self.client.get("/api/projects").json()["projects"]
        self.assertFalse(any(p["doc_id"] == "proj_ut_01" for p in list_after))

    def test_qa_doctor_split_cue_and_ass_style_presets(self):
        cues = [
            {
                "id": 1,
                "start_ms": 1000,
                "end_ms": 4200,
                "source_text": "First clause here, and second clause right after.",
                "target_text": "第一分句在这里，  紧接着第二分句。",
                "speaker": "",
                "style": "Default",
                "ass_tags": "",
                "raw_event_meta": {},
                "is_special": False,
                "status": "translated",
                "note": "",
            },
            {
                "id": 2,
                "start_ms": 3800,
                "end_ms": 6000,
                "source_text": "Overlapping next cue.",
                "target_text": "",
                "speaker": "",
                "style": "Default",
                "ass_tags": "",
                "raw_event_meta": {},
                "is_special": False,
                "status": "untranslated",
                "note": "",
            },
        ]
        # 1. QA check detects overlap, punctuation, and untranslated
        qa_r = self.client.post("/api/subtitles/qa-check", json=cues)
        self.assertEqual(qa_r.status_code, 200)
        rep = qa_r.json()
        self.assertEqual(rep["overlap_ids"], [1])
        self.assertEqual(rep["punctuation_ids"], [1])
        self.assertEqual(rep["untranslated_ids"], [2])

        # 2. QA auto-fix resolves overlap & cleans trailing period / double spaces
        fix_r = self.client.post(
            "/api/subtitles/qa-fix",
            json={"cues": cues, "fix_overlaps": True, "clean_punctuation": True, "target_language": "zh"},
        )
        self.assertEqual(fix_r.status_code, 200)
        fixed_data = fix_r.json()
        self.assertEqual(fixed_data["summary"]["fixed_overlaps"], 1)
        self.assertEqual(fixed_data["summary"]["cleaned_punctuation"], 1)
        self.assertLess(fixed_data["cues"][0]["end_ms"], fixed_data["cues"][1]["start_ms"])
        self.assertEqual(fixed_data["cues"][0]["target_text"], "第一分句在这里， 紧接着第二分句")

        # 3. Smart split single cue into two cues
        sp_r = self.client.post("/api/subtitles/split-cue", json={"cues": fixed_data["cues"], "cue_id": 1})
        self.assertEqual(sp_r.status_code, 200)
        self.assertEqual(len(sp_r.json()["cues"]), 3)

        # 4. Preview export with Netflix ASS box preset
        doc = self.client.get("/api/samples/tech_keynote").json()
        doc["cues"][0]["target_text"] = "欢迎体验听见 Studio 2.3"
        prev_r = self.client.post(
            "/api/subtitles/preview-export",
            json={
                "document": doc,
                "formats": ["ass"],
                "track_mode": "bilingual_tgt_top",
                "ass_secondary_scale": 72,
                "ass_style_preset": "netflix_box",
                "fallback_to_source": True,
            },
        )
        self.assertEqual(prev_r.status_code, 200)
        self.assertIn("netflix_box", prev_r.json()["preview"])

    def test_asr_transcription_whisper_segments_and_sensevoice_tags(self):
        orig_async_client = httpx.AsyncClient

        def asr_mock_handler(request: httpx.Request) -> httpx.Response:
            body_str = request.content.decode("latin-1")
            if "SenseVoiceSmall" in body_str:
                return httpx.Response(
                    200,
                    json={"text": "<|zh|><|NEUTRAL|><|Speech|>大家好，欢迎使用听见语音识别！今天我们来演示自动断句功能。"},
                )
            return httpx.Response(
                200,
                json={
                    "language": "en",
                    "segments": [
                        {"start": 0.2, "end": 2.6, "text": "Hello from Whisper Turbo!"},
                        {"start": 2.8, "end": 5.4, "text": "Subtitles are generated with exact timestamps."},
                    ],
                },
            )

        def mocked_asr_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(asr_mock_handler)
            return orig_async_client(*args, **kwargs)

        with patch("core.asr.httpx.AsyncClient", side_effect=mocked_asr_client):
            # Test SenseVoice plain text + emotion tag stripping + proportional segmentation
            r1 = self.client.post(
                "/api/asr/transcribe",
                files={"file": ("demo.mp3", b"FAKE_MP3_DATA", "audio/mpeg")},
                data={
                    "language": "auto",
                    "duration_sec": "8.0",
                    "asr_provider_id": "siliconflow",
                    "asr_base_url": "https://api.siliconflow.cn/v1",
                    "asr_api_key": "sk-mock-sf",
                    "asr_model": "FunAudioLLM/SenseVoiceSmall",
                },
            )
            self.assertEqual(r1.status_code, 200)
            d1 = r1.json()
            self.assertEqual(d1["source_language"], "zh")
            self.assertGreaterEqual(len(d1["cues"]), 2)
            self.assertNotIn("<|zh|>", d1["cues"][0]["source_text"])

            # Test Groq/OpenAI Whisper verbose_json segments
            r2 = self.client.post(
                "/api/asr/transcribe",
                files={"file": ("talk.mp4", b"FAKE_MP4_DATA", "video/mp4")},
                data={
                    "language": "en",
                    "duration_sec": "6.0",
                    "asr_provider_id": "groq",
                    "asr_base_url": "https://api.groq.com/openai/v1",
                    "asr_api_key": "gsk-mock",
                    "asr_model": "whisper-large-v3-turbo",
                },
            )
            self.assertEqual(r2.status_code, 200)
            d2 = r2.json()
            self.assertEqual(len(d2["cues"]), 2)
            self.assertEqual(d2["cues"][0]["start_ms"], 200)
            self.assertEqual(d2["cues"][0]["source_text"], "Hello from Whisper Turbo!")

    def test_json_extractor_strips_reasoning_and_fences(self):
        messy = "<think>analyzing context...</think>\nHere is JSON:\n```json\n{\"ok\": true, \"items\": [1, 2,]}\n```"
        parsed = extract_json_payload(messy)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["items"], [1, 2])

    def test_end_to_end_api_stream_fast_and_reflective_plus_export(self):
        orig_async_client = httpx.AsyncClient

        def mocked_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_mock_openai_handler)
            return orig_async_client(*args, **kwargs)

        override = {
            "provider_id": "custom",
            "base_url": "http://localhost:11434/v1",
            "model": "mock-gpt",
            "batch_size": 3,
            "context_window": 2,
            "concurrency": 2,
        }

        with patch("core.translator.httpx.AsyncClient", side_effect=mocked_client):
            # 1. Test connection endpoint & live /models discovery
            m_resp = self.client.post("/api/settings/models", json=override)
            self.assertEqual(m_resp.status_code, 200)
            fetched = m_resp.json()["models"]
            self.assertIn("gpt-6.2-sol", fetched)
            self.assertIn("claude-sonnet-5-5", fetched)
            self.assertNotIn("text-embedding-3-large", fetched)
            self.assertNotIn("whisper-1", fetched)

            gem_resp = self.client.post(
                "/api/settings/models",
                json={
                    "provider_id": "gemini",
                    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                    "api_key": "AIzaSyMockKey",
                },
            )
            self.assertEqual(gem_resp.status_code, 200)
            self.assertIn("gemini-3.8-flash", gem_resp.json()["models"])

            meta_resp = self.client.get("/api/meta")
            self.assertGreaterEqual(len(meta_resp.json()["presets"]), 12)

            t_resp = self.client.post("/api/settings/test", json=override)
            self.assertEqual(t_resp.status_code, 200)
            self.assertTrue(t_resp.json()["ok"])

            # 2. Load sample
            doc = self.client.get("/api/samples/tech_keynote").json()

            # 3. Extract glossary
            g_resp = self.client.post(
                "/api/ai/extract-glossary",
                json={
                    "cues": doc["cues"],
                    "source_language": "en",
                    "target_language": "zh",
                    "override_settings": override,
                },
            )
            self.assertEqual(g_resp.status_code, 200)
            self.assertEqual(g_resp.json()["glossary"][0]["target"], "听见 Studio")

            # 4. Stream reflective mode AND fast concurrent mode
            for mode in ("reflective", "fast"):
                stream_resp = self.client.post(
                    "/api/ai/translate-stream",
                    json={
                        "cues": doc["cues"],
                        "source_language": "en",
                        "target_language": "zh",
                        "mode": mode,
                        "genre": "tech",
                        "override_settings": override,
                    },
                )
                self.assertEqual(stream_resp.status_code, 200)
                events = [
                    json.loads(line[5:].strip())
                    for line in stream_resp.text.splitlines()
                    if line.startswith("data:")
                ]
                self.assertEqual(events[-1]["event"], "complete")
                self.assertEqual(events[-1]["completed"], len(doc["cues"]))

            # 5. Single cue AI rewrite
            rw_resp = self.client.post(
                "/api/ai/rewrite-cue",
                json={
                    "cue_id": 1,
                    "cues": doc["cues"],
                    "source_language": "en",
                    "target_language": "zh",
                    "action": "natural",
                    "override_settings": override,
                },
            )
            self.assertEqual(rw_resp.status_code, 200)
            self.assertIn("听见字幕工作台", rw_resp.json()["best"])

            # 6. Multi-format lossless export (all 5 output formats)
            doc["cues"][0]["target_text"] = rw_resp.json()["best"]
            ex_resp = self.client.post(
                "/api/subtitles/export",
                json={
                    "document": doc,
                    "formats": ["srt", "ass", "ssa", "vtt", "txt"],
                    "track_mode": "bilingual_tgt_top",
                    "ass_secondary_scale": 72,
                    "fallback_to_source": True,
                },
            )
            self.assertEqual(ex_resp.status_code, 200)
            self.assertEqual(len(ex_resp.json()["files"]), 5)

    def test_sloppy_srt_parsing_missing_blank_lines_and_short_ms(self):
        sloppy_srt = (
            "1\n"
            "00:00:01,5 --> 00:00:03.25\n"
            "First line with 1-digit and 2-digit ms\n"
            "2\n"
            "00:00:03,400 --> 00:00:05,8\n"
            "Second cue immediately without blank line!\n"
        )
        doc = parse_subtitle_content(sloppy_srt, "sloppy.srt", "utf-8")
        self.assertEqual(len(doc.cues), 2)
        self.assertEqual(doc.cues[0].start_ms, 1500)
        self.assertEqual(doc.cues[0].end_ms, 3250)
        self.assertEqual(doc.cues[1].start_ms, 3400)
        self.assertEqual(doc.cues[1].end_ms, 5800)

    def test_scene_aware_silence_and_punctuation_batching(self):
        # Cue #4 ends mid-clause with comma & tiny 50ms gap; Cue #5 ends sentence with period & 2.2s silence gap.
        cues = [
            SubtitleCue(id=1, start_ms=0, end_ms=1500, source_text="Welcome back everyone."),
            SubtitleCue(id=2, start_ms=1600, end_ms=3000, source_text="Today we test scene batching."),
            SubtitleCue(id=3, start_ms=3100, end_ms=4500, source_text="When dialogue runs fast,"),
            SubtitleCue(id=4, start_ms=4550, end_ms=5900, source_text="splitting right in a comma,"),
            SubtitleCue(id=5, start_ms=5950, end_ms=7400, source_text="would break context badly."),
            SubtitleCue(id=6, start_ms=9600, end_ms=11200, source_text="After a two second pause, a new scene begins!"),
            SubtitleCue(id=7, start_ms=11400, end_ms=13000, source_text="And continues smoothly here."),
            SubtitleCue(id=8, start_ms=13200, end_ms=14800, source_text="Final line of scene two."),
        ]
        batches = partition_scene_batches(cues, target_size=4)
        self.assertEqual(len(batches), 2)
        # First batch should cut cleanly after cue #5 (silence gap 2200ms + period) instead of cue #4
        self.assertEqual([c.id for c in batches[0]], [1, 2, 3, 4, 5])
        self.assertEqual([c.id for c in batches[1]], [6, 7, 8])

    def test_api_exponential_retry_on_429_and_503(self):
        orig_async_client = httpx.AsyncClient
        attempts = {"count": 0}

        def flaky_handler(request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            if attempts["count"] == 1:
                return httpx.Response(429, json={"error": "rate limit"})
            if attempts["count"] == 2:
                return httpx.Response(503, json={"error": "bad gateway"})
            return _mock_openai_handler(request)

        def mocked_flaky_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(flaky_handler)
            return orig_async_client(*args, **kwargs)

        override = {
            "provider_id": "custom",
            "base_url": "http://localhost:11434/v1",
            "model": "mock-gpt",
        }
        with patch("core.translator.httpx.AsyncClient", side_effect=mocked_flaky_client), patch(
            "core.translator.asyncio.sleep", new=AsyncMock()
        ):
            resp = self.client.post("/api/settings/test", json=override)
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["ok"])
            self.assertEqual(attempts["count"], 3)

    def test_batch_zip_export_and_safe_download(self):
        ep1 = self.client.get("/api/samples/tech_keynote").json()
        ep1["filename"] = "Episode_01.srt"
        ep1["cues"][0]["target_text"] = "第一集首句双语译文"

        ep2 = self.client.get("/api/samples/tech_keynote").json()
        ep2["filename"] = "Episode_02.srt"
        ep2["cues"][0]["target_text"] = "第二集首句双语译文"

        z_resp = self.client.post(
            "/api/subtitles/export-batch-zip",
            json={
                "documents": [ep1, ep2],
                "formats": ["srt", "vtt"],
                "track_mode": "bilingual_tgt_top",
            },
        )
        self.assertEqual(z_resp.status_code, 200)
        z_data = z_resp.json()
        self.assertEqual(z_data["file_count"], 4)

        dl_resp = self.client.get(z_data["download_url"])
        self.assertEqual(dl_resp.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(dl_resp.content), "r") as zf:
            names = sorted(zf.namelist())
            self.assertEqual(len(names), 4)
            ep1_srt = zf.read("Episode_01.en-zh.srt").decode("utf-8")
            self.assertIn("第一集首句双语译文", ep1_srt)

        # Path traversal attempt must fail
        bad_resp = self.client.get("/api/subtitles/download-export/..%2Fsettings.json")
        self.assertIn(bad_resp.status_code, (400, 404))


if __name__ == "__main__":
    unittest.main()
