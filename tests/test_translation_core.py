"""Translation boundary tests; all API traffic stays on a loopback fake server."""

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
import subtitle_core as core
import translation_core as translation


def cues(count=2):
    return [core.SubtitleSegment(3000 + index * 500, 3400 + index * 500, f"Original {index + 1}") for index in range(count)]


class LocalAPI:
    """Configurable OpenAI-compatible fake; it cannot reach external services."""

    def __init__(self):
        self.requests = []
        self.status = 200
        self.redirect = None
        self.response_factory = self.valid_response
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
                api.requests.append({"path": self.path, "authorization": self.headers.get("Authorization"), "body": body})
                document = api.response_factory(body)
                encoded = json.dumps(document, ensure_ascii=False).encode("utf-8")
                self.send_response(api.status)
                if api.redirect:
                    self.send_header("Location", api.redirect)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(encoded)
                self.wfile.flush()
                # Wait for the client's EOF after a FIN. Closing a Windows
                # loopback socket immediately can instead produce a TCP RST
                # before urllib has read the response status line.
                self.connection.shutdown(socket.SHUT_WR)
                self.connection.settimeout(0.2)
                try:
                    self.connection.recv(1)
                except (OSError, socket.timeout):
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    @staticmethod
    def entries(body):
        return json.loads(body["messages"][1]["content"])["subtitles"]

    @classmethod
    def valid_response(cls, body):
        rows = [{"id": item["id"], "text": f"译文 {item['id']}"} for item in reversed(cls.entries(body))]
        return cls.envelope(rows)

    @staticmethod
    def envelope(rows, finish_reason="stop"):
        return {"choices": [{"finish_reason": finish_reason, "message": {"content": json.dumps({"translations": rows}, ensure_ascii=False)}}]}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class TranslationBoundaryTests(unittest.TestCase):
    def test_language_alias_and_same_language_no_model_or_api(self):
        segments = cues()
        options = translation.TranslationOptions(backend="online", target_language="zh-Hans", bilingual=True)
        with patch.object(translation, "_online_translate", side_effect=AssertionError("unexpected API")):
            result = translation.translate_segments(segments, "zh_CN", options)
        self.assertEqual(result, segments)
        self.assertNotIn("\n", result[0].text)

    def test_offline_receives_cache_and_bilingual_keeps_timestamps(self):
        segments = cues()
        cache = Path("sample-cache")
        cancel = threading.Event()
        options = translation.TranslationOptions(target_language="zh", model_cache=cache, bilingual=True)
        callback = lambda update: None
        calls = []

        def fake(texts, source, target, **kwargs):
            calls.append((texts, source, target, kwargs))
            return ["第一句", "第二句"]

        with patch.dict(sys.modules, {"offline_translate": SimpleNamespace(translate_texts=fake)}):
            result = translation.translate_segments(segments, "en-US", options, callback, cancel)
        self.assertEqual([item.text for item in result], ["Original 1\n第一句", "Original 2\n第二句"])
        self.assertEqual([(item.start_ms, item.end_ms) for item in result], [(item.start_ms, item.end_ms) for item in segments])
        self.assertEqual(calls[0][1:3], ("en", "zh"))
        self.assertEqual(calls[0][3], {"model_cache": cache, "progress": callback, "cancel_event": cancel})
        self.assertIn("Original 1\n第一句", core.render_subtitles(result, "srt"))

    def test_offline_rejects_lost_or_blank_entries(self):
        for returned in (["one"], ["one", "   "], ["one", None]):
            with self.subTest(returned=returned):
                with patch.dict(sys.modules, {"offline_translate": SimpleNamespace(translate_texts=lambda *args, **kwargs: returned)}):
                    with self.assertRaises(translation.TranslationError):
                        translation.translate_segments(cues(), "en", translation.TranslationOptions())

    def test_remote_plain_http_and_credentials_in_url_are_rejected(self):
        invalid = (
            "http://api.example.com/v1", "http://localhost.evil.invalid/v1", "http://0.0.0.0/v1",
            "https://user:secret@example.com/v1", "https://example.com/v1?api_key=secret",
            "https://example.com/v1#secret", "https://example.com:invalid/v1", "https://example.com /v1",
        )
        for address in invalid:
            with self.subTest(address=address):
                with self.assertRaises(translation.TranslationError):
                    translation.validate_options(translation.TranslationOptions(backend="online", online_base_url=address, online_model="test", api_key="secret"))
        self.assertEqual(translation._endpoint("http://[::1]:5555/v1/"), "http://[::1]:5555/v1/chat/completions")
        self.assertEqual(translation._endpoint("https://example.com/v1/chat/completions"), "https://example.com/v1/chat/completions")

    def test_secret_not_in_options_repr_or_validation_errors(self):
        secret = "SUPER_SECRET_TEST_KEY"
        options = translation.TranslationOptions(backend="online", api_key=secret)
        self.assertNotIn(secret, repr(options))
        with self.assertRaises(translation.TranslationError) as failure:
            translation.validate_options(options)
        self.assertNotIn(secret, str(failure.exception))
        for invalid in (replace(options, online_model="test", api_key="line\nbreak"), replace(options, online_model="test", timeout=61), replace(options, online_model="test", batch_size=21)):
            with self.assertRaises(translation.TranslationError):
                translation.validate_options(invalid)

    def test_pre_cancel_makes_no_translation_attempt(self):
        event = threading.Event()
        event.set()
        with patch.object(translation, "_online_translate", side_effect=AssertionError("unexpected API")):
            with self.assertRaises(core.TranscriptionCancelled):
                translation.translate_segments(cues(), "en", translation.TranslationOptions(backend="online"), cancel_event=event)


class OnlineTranslationTests(unittest.TestCase):
    def setUp(self):
        self.api = LocalAPI()
        self.addCleanup(self.api.close)
        self.options = translation.TranslationOptions(backend="online", target_language="zh", online_base_url=self.api.base_url, online_model="fake-test-model", api_key="FAKE_TEST_KEY")

    def test_batching_reorders_ids_and_keeps_every_timestamp(self):
        source = cues(35)
        updates = []
        result = translation.translate_segments(source, "en", self.options, progress=updates.append)
        self.assertEqual([item.text for item in result], [f"译文 {index}" for index in range(1, 36)])
        self.assertEqual([(item.start_ms, item.end_ms) for item in result], [(item.start_ms, item.end_ms) for item in source])
        self.assertEqual([len(LocalAPI.entries(call["body"])) for call in self.api.requests], [16, 16, 3])
        self.assertTrue(all(call["path"] == "/v1/chat/completions" for call in self.api.requests))
        self.assertTrue(all(call["authorization"] == "Bearer FAKE_TEST_KEY" for call in self.api.requests))
        self.assertTrue(all(call["body"]["model"] == "fake-test-model" for call in self.api.requests))
        self.assertEqual(updates[-1].percent, 100)

    def test_loopback_api_bypasses_system_proxy_configuration(self):
        with patch.object(translation.request, "getproxies", side_effect=AssertionError("local API must not read system proxies")):
            result = translation.translate_segments(cues(), "en", self.options)
        self.assertEqual([item.text for item in result], ["译文 1", "译文 2"])

    def test_rejects_missing_duplicate_added_and_invalid_ids_or_text(self):
        documents = (
            [{"id": 1, "text": "one"}],
            [{"id": 1, "text": "one"}, {"id": 1, "text": "duplicate"}],
            [{"id": 1, "text": "one"}, {"id": 2, "text": "two"}, {"id": 3, "text": "extra"}],
            [{"id": True, "text": "one"}, {"id": 2, "text": "two"}],
            [{"id": "1", "text": "one"}, {"id": 2, "text": "two"}],
            [{"id": 1, "text": "one"}, {"id": 2, "text": " \n "}],
            [{"id": 1, "text": "one"}, {"id": 2, "text": None}],
        )
        for document in documents:
            with self.subTest(document=document):
                self.api.response_factory = lambda body, rows=document: LocalAPI.envelope(rows)
                with self.assertRaisesRegex(translation.TranslationError, "编号|遗漏|空白|无效"):
                    translation.translate_segments(cues(), "en", self.options)

    def test_refuses_invalid_json_and_truncated_responses(self):
        documents = (
            {}, {"choices": []}, {"choices": ["wrong type"]},
            {"choices": [{"message": {"content": "```json\n{}\n```"}}]},
            LocalAPI.envelope([{"id": 1, "text": "one"}, {"id": 2, "text": "two"}], "length"),
        )
        for document in documents:
            with self.subTest(document=document):
                self.api.response_factory = lambda body, value=document: value
                with self.assertRaisesRegex(translation.TranslationError, "JSON|截断"):
                    translation.translate_segments(cues(), "en", self.options)

    def test_http_error_body_and_credential_never_surface(self):
        self.api.status = 401
        self.api.response_factory = lambda body: {"error": "FAKE_TEST_KEY private debug information"}
        with self.assertRaises(translation.TranslationError) as failure:
            translation.translate_segments(cues(), "en", self.options)
        self.assertIn("API Key", str(failure.exception))
        self.assertNotIn("FAKE_TEST_KEY", str(failure.exception))
        self.assertNotIn("private debug", str(failure.exception))
        self.assertIsNone(failure.exception.__cause__)

    def test_redirect_is_rejected_without_forwarding_authorization(self):
        self.api.status = 302
        self.api.redirect = self.api.base_url + "/elsewhere"
        with self.assertRaisesRegex(translation.TranslationError, "重定向"):
            translation.translate_segments(cues(), "en", self.options)
        self.assertEqual(len(self.api.requests), 1)

    def test_cancel_after_response_prevents_remaining_batches(self):
        event = threading.Event()

        def respond(body):
            event.set()
            return LocalAPI.valid_response(body)

        self.api.response_factory = respond
        with self.assertRaises(core.TranscriptionCancelled):
            translation.translate_segments(cues(35), "en", self.options, cancel_event=event)
        self.assertEqual(len(self.api.requests), 1)

    def test_long_cues_reduce_batch_size_without_losing_text(self):
        source = [core.SubtitleSegment(0, 1000, "a" * 5000), core.SubtitleSegment(1000, 2000, "b" * 3100), core.SubtitleSegment(2000, 3000, "c" * 3100)]
        result = translation.translate_segments(source, "en", self.options)
        self.assertEqual(len(result), 3)
        self.assertEqual([len(LocalAPI.entries(call["body"])) for call in self.api.requests], [1, 1, 1])


if __name__ == "__main__":
    unittest.main()
