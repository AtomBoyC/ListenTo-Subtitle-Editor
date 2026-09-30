"""Standard-library tests; no inference dependencies or model download needed."""

import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch


CORE_PATH = Path(__file__).resolve().parents[1] / "subtitle_core.py"
SPEC = importlib.util.spec_from_file_location("subtitle_core", CORE_PATH)
core = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = core
SPEC.loader.exec_module(core)


def segment(start, end, text):
    return SimpleNamespace(start=start, end=end, text=text)


class CacheMissError(Exception):
    pass


class FormattingTests(unittest.TestCase):
    def test_sorted_positive_unicode_and_duration(self):
        cues = core.normalize_segments([
            segment(2, 2, "中文\n  字幕"),
            segment(-1, 0.2, "开始"),
            segment(3, 9, "结尾"),
            segment(4, 5, "越界"),
            segment(float("nan"), 2, "无效"),
            segment(1, 2, " \n\t"),
        ], duration=4)
        self.assertEqual([cue.text for cue in cues], ["开始", "中文 字幕", "结尾"])
        self.assertEqual((cues[0].start_ms, cues[1].end_ms, cues[2].end_ms), (0, 2001, 4000))
        self.assertTrue(all(cue.end_ms > cue.start_ms for cue in cues))

    def test_timestamps_carry_at_hour_boundary_and_vtt_escaping(self):
        cues = core.normalize_segments([segment(3599.9996, 3601.02, "你好 & <世界>")])
        self.assertIn("01:00:00,000 --> 01:00:01,020", core.render_subtitles(cues, "srt"))
        self.assertIn("01:00:00.000 --> 01:00:01.020", core.render_subtitles(cues, "vtt"))
        self.assertIn("你好 &amp; &lt;世界&gt;", core.render_subtitles(cues, "vtt"))
        self.assertEqual(core.render_subtitles(cues, "txt"), "你好 & <世界>\n")

    def test_empty_is_error(self):
        with self.assertRaises(core.NoSpeechError):
            core.render_subtitles([], "srt")


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "中文视频.mp4"
        self.source.write_bytes(b"fake media")
        self.out = self.root / "字幕结果"
        self.options = core.TranscriptionOptions(output_dir=self.out, formats=("srt", "vtt", "txt"))
        self.calls = []
        self.download_calls = []
        self.cache = self.root / "model-cache"
        self.cache.mkdir()
        for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
            (self.cache / name).write_bytes(b"fake model asset")

    def fake_download(self, name, **kwargs):
        self.download_calls.append(kwargs["local_files_only"])
        return str(self.cache)

    def fake_runtime(self, segments):
        calls = self.calls

        class Model:
            def __init__(self, name, **kwargs):
                calls.append(("model", name, kwargs))

            def transcribe(self, path, **kwargs):
                calls.append(("transcribe", path, kwargs))
                return iter(segments), SimpleNamespace(language="zh", duration=10)

        return patch.dict(sys.modules, {
            "faster_whisper": SimpleNamespace(WhisperModel=Model),
            "faster_whisper.utils": SimpleNamespace(download_model=self.fake_download),
            "huggingface_hub.errors": SimpleNamespace(LocalEntryNotFoundError=CacheMissError),
        })

    def test_export_and_matching_collision_suffix(self):
        self.out.mkdir()
        existing = self.out / "中文视频.zh.vtt"
        existing.write_text("keep this", encoding="utf-8")
        updates = []
        with self.fake_runtime([segment(0, 1, "你好"), segment(1, 2, "字幕测试")]):
            result = core.transcribe_media(self.source, self.options, progress=updates.append)
        self.assertEqual(result.segment_count, 2)
        self.assertEqual(result.language, "zh")
        self.assertEqual(result.text, "你好\n字幕测试\n")
        self.assertEqual({p.stem for p in result.output_paths.values()}, {"中文视频.zh.2"})
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep this")
        self.assertEqual(self.source.read_bytes(), b"fake media")
        self.assertEqual(result.output_paths["srt"].read_text(encoding="utf-8"),
                         "1\n00:00:00,000 --> 00:00:01,000\n你好\n\n2\n00:00:01,000 --> 00:00:02,000\n字幕测试\n")
        self.assertEqual(updates[-1].percent, 100)
        self.assertEqual(self.calls[0][2], {"device": "cpu", "compute_type": "int8", "local_files_only": True})
        self.assertIsNone(self.calls[1][2]["language"])
        self.assertTrue(self.calls[1][2]["vad_filter"])
        self.assertEqual(self.calls[0][1], str(self.cache))
        self.assertEqual(self.download_calls, [True])

    def test_cancel_between_segments_writes_nothing(self):
        event = threading.Event()

        def cancel(update):
            if update.stage == "transcribing" and update.percent:
                event.set()

        with self.fake_runtime([segment(0, 1, "第一段"), segment(1, 2, "第二段")]):
            with self.assertRaises(core.TranscriptionCancelled):
                core.transcribe_media(self.source, self.options, cancel, event)
        self.assertFalse(self.out.exists())

    def test_cancel_before_start_does_not_initialize_model(self):
        event = threading.Event()
        event.set()
        with self.fake_runtime([segment(0, 1, "第一段")]):
            with self.assertRaises(core.TranscriptionCancelled):
                core.transcribe_media(self.source, self.options, cancel_event=event)
        self.assertEqual(self.calls, [])

    def test_silence_does_not_write_fake_success(self):
        with self.fake_runtime([segment(0, 1, "   ")]):
            with self.assertRaises(core.NoSpeechError):
                core.transcribe_media(self.source, self.options)
        self.assertFalse(self.out.exists())

    def test_failure_to_reserve_second_file_cleans_up_first(self):
        original_open = Path.open

        def denied(path, *args, **kwargs):
            if path.suffix == ".vtt":
                raise PermissionError("write denied")
            return original_open(path, *args, **kwargs)

        with self.fake_runtime([segment(0, 1, "文本")]), patch.object(Path, "open", denied):
            with self.assertRaises(PermissionError):
                core.transcribe_media(self.source, self.options)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_cancel_during_export_cleans_up_all_reserved_files(self):
        event = threading.Event()
        original_open = Path.open

        def cancel_on_vtt(path, *args, **kwargs):
            handle = original_open(path, *args, **kwargs)
            if path.suffix == ".vtt":
                event.set()
            return handle

        with self.fake_runtime([segment(0, 1, "文本")]), patch.object(Path, "open", cancel_on_vtt):
            with self.assertRaises(core.TranscriptionCancelled):
                core.transcribe_media(self.source, self.options, cancel_event=event)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_invalid_format_does_not_load_model(self):
        with self.fake_runtime([]):
            with self.assertRaises(ValueError):
                core.transcribe_media(self.source, core.TranscriptionOptions(formats=("mp4",)))
        self.assertEqual(self.calls, [])

    def test_cache_miss_retries_online_once(self):
        calls = []

        def download(name, **kwargs):
            calls.append(kwargs["local_files_only"])
            if kwargs["local_files_only"]:
                raise CacheMissError()
            return str(self.cache)

        class Model:
            def __init__(self, name, **kwargs):
                pass

            def transcribe(self, path, **kwargs):
                return iter([segment(0, 1, "文本")]), SimpleNamespace(language="zh", duration=2)

        with patch.dict(sys.modules, {
            "faster_whisper": SimpleNamespace(WhisperModel=Model),
            "faster_whisper.utils": SimpleNamespace(download_model=download),
            "huggingface_hub.errors": SimpleNamespace(LocalEntryNotFoundError=CacheMissError),
        }):
            core.transcribe_media(self.source, self.options)
        self.assertEqual(calls, [True, False])

    def test_model_errors_do_not_trigger_download(self):
        calls = []

        class Model:
            def __init__(self, name, **kwargs):
                calls.append(kwargs["local_files_only"])
                raise RuntimeError("invalid model")

        with patch.dict(sys.modules, {
            "faster_whisper": SimpleNamespace(WhisperModel=Model),
            "faster_whisper.utils": SimpleNamespace(download_model=self.fake_download),
            "huggingface_hub.errors": SimpleNamespace(LocalEntryNotFoundError=CacheMissError),
        }):
            with self.assertRaisesRegex(RuntimeError, "invalid model"):
                core.transcribe_media(self.source, self.options)
        self.assertEqual(calls, [True])
        self.assertEqual(self.download_calls, [True])

    def test_partial_cache_is_repaired_before_model_load(self):
        (self.cache / "model.bin").unlink()
        calls = []

        def download(name, **kwargs):
            calls.append(kwargs["local_files_only"])
            if not kwargs["local_files_only"]:
                (self.cache / "model.bin").write_bytes(b"completed download")
            return str(self.cache)

        with self.fake_runtime([segment(0, 1, "已恢复")]), patch.dict(sys.modules, {
            "faster_whisper.utils": SimpleNamespace(download_model=download),
        }):
            result = core.transcribe_media(self.source, self.options)
        self.assertEqual(calls, [True, False])
        self.assertEqual(result.text, "已恢复\n")
        self.assertEqual(self.calls[0][1], str(self.cache))

    def test_missing_tokenizer_or_empty_weight_prevents_inference(self):
        (self.cache / "model.bin").write_bytes(b"")
        (self.cache / "tokenizer.json").unlink()
        with self.fake_runtime([segment(0, 1, "不应输出")]):
            with self.assertRaisesRegex(RuntimeError, "模型下载不完整"):
                core.transcribe_media(self.source, self.options)
        self.assertEqual(self.download_calls, [True, False])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
