"""Comprehensive automated unit & functional integration tests for ListenTo Studio v2.0."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from core import config_store
from core.models import APISettingsUpdate
from core.parser import (
    decode_subtitle_bytes,
    export_document_format,
    parse_subtitle_content,
    shift_cues_timestamps,
    split_bilingual_cues,
)
from core.translator import _is_local_url, extract_json_payload
from server import app


def _mock_openai_handler(request: httpx.Request) -> httpx.Response:
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
            # 1. Test connection endpoint
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


if __name__ == "__main__":
    unittest.main()
