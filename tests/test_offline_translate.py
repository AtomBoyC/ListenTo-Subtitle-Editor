"""Regressions for official legacy Argos model layouts and safe caching."""

import io
import json
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile


APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
import offline_translate as offline
from subtitle_core import TranscriptionCancelled


def package(path, source="ja", target="en", *, vocabulary="shared-txt", config=False):
    """Reproduce ja_en 1.1's real file layout without distributable weights."""
    model = path / "model"
    model.mkdir(parents=True)
    (path / "metadata.json").write_text(json.dumps({
        "package_version": "1.1", "argos_version": "1.1",
        "from_code": source, "to_code": target,
    }), encoding="utf-8")
    (path / "sentencepiece.model").write_bytes(b"fixture sentencepiece")
    (model / "model.bin").write_bytes(struct.pack("<I", 5) + b"fixture weights")
    if vocabulary == "shared-txt":
        (model / "shared_vocabulary.txt").write_text("<unk>\n<s>\n</s>\nhello\n", encoding="utf-8")
    elif vocabulary == "shared-json":
        (model / "shared_vocabulary.json").write_text('["<unk>", "<s>", "</s>", "hello"]', encoding="utf-8")
    elif vocabulary == "separate-txt":
        for side in ("source", "target"):
            (model / f"{side}_vocabulary.txt").write_text("<unk>\n<s>\n</s>\nhello\n", encoding="utf-8")
    if config:
        (model / "config.json").write_text('{"bos_token": "<s>", "eos_token": "</s>"}', encoding="utf-8")
    return path


def archive_package(source, destination):
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in source.rglob("*"):
            if file.is_file():
                archive.write(file, str(Path("ja_en") / file.relative_to(source)))


class LegacyArgosValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def test_official_japanese_layout_needs_no_config_json(self):
        directory = package(self.root / "ja_en")
        self.assertFalse((directory / "model" / "config.json").exists())
        self.assertEqual(offline._metadata(directory, "ja", "en")["package_version"], "1.1")

    def test_modern_json_shared_vocabulary_and_config_are_accepted(self):
        directory = package(self.root / "en_zh", "en", "zh", vocabulary="shared-json", config=True)
        self.assertEqual(offline._metadata(directory, "en", "zh")["to_code"], "zh")

    def test_separate_legacy_vocabularies_are_accepted(self):
        directory = package(self.root / "ja_en", vocabulary="separate-txt")
        self.assertEqual(offline._metadata(directory, "ja", "en")["from_code"], "ja")

    def test_missing_target_vocabulary_is_rejected(self):
        directory = package(self.root / "ja_en", vocabulary="separate-txt")
        (directory / "model" / "target_vocabulary.txt").unlink()
        with self.assertRaisesRegex(offline.OfflineTranslationError, "target"):
            offline._metadata(directory, "ja", "en")

    def test_empty_required_files_are_rejected(self):
        for relative in ("model/model.bin", "sentencepiece.model", "model/shared_vocabulary.txt"):
            with self.subTest(relative=relative):
                directory = package(self.root / relative.replace("/", "_"))
                (directory / relative).write_bytes(b"")
                with self.assertRaises(offline.OfflineTranslationError):
                    offline._metadata(directory, "ja", "en")

    def test_wrong_language_is_not_accepted_as_legacy_compatibility(self):
        directory = package(self.root / "ja_en", "en", "ja")
        with self.assertRaisesRegex(offline.OfflineTranslationError, "语言代码不符"):
            offline._metadata(directory, "ja", "en")

    def test_existing_invalid_config_is_rejected(self):
        for content in ("", "{broken", "[]"):
            with self.subTest(content=content):
                directory = package(self.root / (str(len(content)) + "-config"), config=True)
                (directory / "model" / "config.json").write_text(content, encoding="utf-8")
                with self.assertRaises(offline.OfflineTranslationError):
                    offline._metadata(directory, "ja", "en")

    def test_verified_legacy_zip_can_be_extracted(self):
        directory = package(self.root / "source")
        archive = self.root / "ja-en.argosmodel"
        archive_package(directory, archive)
        extracted = offline._extract(archive, self.root / "unpacked", "ja", "en", None)
        self.assertEqual(extracted, self.root / "unpacked" / "ja_en")
        self.assertTrue((extracted / "model" / "shared_vocabulary.txt").is_file())
        self.assertFalse((extracted / "model" / "config.json").exists())

    def test_zip_path_traversal_is_still_rejected(self):
        archive = self.root / "unsafe.argosmodel"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("ja_en/metadata.json", "{}")
            bundle.writestr("../escaped.txt", "forbidden")
        with self.assertRaisesRegex(offline.OfflineTranslationError, "不安全路径"):
            offline._extract(archive, self.root / "unpacked", "ja", "en", None)
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_install_preserves_existing_valid_cache(self):
        directory = package(self.root / "source")
        archive = self.root / "ja-en.argosmodel"
        archive_package(directory, archive)
        cache = self.root / "cache"
        existing = package(cache / "ja-en" / "1.1")
        weights = existing / "model" / "model.bin"
        weights.write_bytes(b"existing intact cached data")
        with patch.object(offline, "_download", side_effect=lambda url, path, *args: shutil.copyfile(archive, path)):
            installed, metadata = offline._install(("ja", "en"), [{"package_version": "1.1", "links": ["https://example.invalid/data"]}], cache, None, None)
        self.assertEqual(installed, existing)
        self.assertEqual(weights.read_bytes(), b"existing intact cached data")
        self.assertEqual(list(cache.glob(".download-*")), [])

    def test_cancelled_download_leaves_no_partial_cached_model(self):
        cancelled = threading.Event()

        class Response(io.BytesIO):
            headers = {"Content-Length": "4"}

            def read(self, size=-1):
                result = super().read(size)
                cancelled.set()
                return result

        cache = self.root / "cache"
        cache.mkdir()
        with patch.object(offline, "_https_open", return_value=Response(b"data")):
            with self.assertRaises(TranscriptionCancelled):
                offline._install(("ja", "en"), [{"package_version": "1.1", "links": ["https://example.invalid/data"]}], cache, None, cancelled)
        self.assertEqual(list(cache.iterdir()), [])

    def test_cached_legacy_pivot_announces_final_chinese_goal_without_network(self):
        cache = self.root / "cache"
        package(cache / "ja-en" / "1.1")
        package(cache / "en-zh" / "1.1", "en", "zh", vocabulary="shared-json", config=True)
        loaded = []

        class Tokenizer:
            def __init__(self, model_file):
                self.model_file = model_file

            def encode(self, text, out_type=str):
                return [text]

            def decode_pieces(self, tokens):
                return " ".join(tokens)

        class Translator:
            def __init__(self, model_path, **kwargs):
                self.source_is_japanese = "ja-en" in Path(model_path).parts
                loaded.append(model_path)

            def translate_batch(self, tokens, **kwargs):
                translated = "Hello" if self.source_is_japanese else "你好"
                return [SimpleNamespace(hypotheses=[[translated]])]

            def unload_model(self):
                pass

        updates = []
        with patch.dict(sys.modules, {"ctranslate2": SimpleNamespace(Translator=Translator),
                                    "sentencepiece": SimpleNamespace(SentencePieceProcessor=Tokenizer)}), patch.object(
                offline, "_https_open", side_effect=AssertionError("cached route must not use network")):
            translated = offline.translate_texts(["こんにちは"], "ja", "zh", cache, updates.append)
        self.assertEqual(translated, ["你好"])
        self.assertEqual(len(loaded), 2)
        self.assertIn("最终目标：中文", updates[0].message)
        self.assertIn("日语 → 英语 → 中文", updates[0].message)
        self.assertEqual(updates[-1].percent, 100)


class EmptyModelOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.cache = Path(self.temporary.name) / "cache"
        package(self.cache / "ja-en" / "1.1")
        package(self.cache / "en-zh" / "1.1", "en", "zh", vocabulary="shared-json", config=True)
        self.addCleanup(self.temporary.cleanup)
        self.calls = []
        self.updates = []

    def run_model(self, texts, respond, *, cancel_event=None):
        calls = self.calls

        class Tokenizer:
            def __init__(self, model_file):
                pass

            def encode(self, text, out_type=str):
                return [text]

            def decode_pieces(self, tokens):
                return " ".join(tokens).replace("▁", " ")

        class Translator:
            def __init__(self, model_path, **kwargs):
                self.leg = "ja-en" if "ja-en" in Path(model_path).parts else "en-zh"

            def translate_batch(self, tokens, **kwargs):
                calls.append((self.leg, tokens[0][0], kwargs))
                alternatives = respond(self.leg, tokens[0][0], kwargs["num_hypotheses"])
                return [SimpleNamespace(hypotheses=alternatives)]

            def unload_model(self):
                pass

        with patch.dict(sys.modules, {"ctranslate2": SimpleNamespace(Translator=Translator),
                                    "sentencepiece": SimpleNamespace(SentencePieceProcessor=Tokenizer)}), patch.object(
                offline, "_https_open", side_effect=AssertionError("cached test must not use network")):
            return offline.translate_texts(texts, "ja", "zh", self.cache, self.updates.append, cancel_event)

    def test_empty_top_candidate_recovers_from_bounded_ranked_alternative(self):
        def respond(leg, text, count):
            if leg == "en-zh":
                self.assertEqual(text, "Hello")
                return [["你好"]]
            return [["▁", "▁"]] if count == 1 else [["▁"], ["Hello"], ["Lower ranked"], ["Last"]]

        result = self.run_model(["こんにちは"], respond)
        self.assertEqual(result, ["你好"])
        self.assertEqual([call[2]["num_hypotheses"] for call in self.calls], [1, 4, 1])
        for _, _, arguments in self.calls:
            self.assertEqual(arguments["beam_size"], 4)
            self.assertEqual(arguments["length_penalty"], 0.2)
            self.assertNotIn("min_decoding_length", arguments)
        self.assertFalse(any(update.stage == "translation_warning" for update in self.updates))

    def test_unusable_candidates_keep_original_and_skip_remaining_pivot(self):
        result = self.run_model(["こんにちは"], lambda leg, text, count: [["▁"], ["..."], ["!"], [" "]] if count == 4 else [["▁"]])
        self.assertEqual(result, ["こんにちは"])
        self.assertEqual([call[0] for call in self.calls], ["ja-en", "ja-en"])
        warnings = [update for update in self.updates if update.stage == "translation_warning"]
        self.assertEqual(len(warnings), 2)
        self.assertTrue(warnings[0].message.startswith("片段 1：日语 → 英语"))
        self.assertIn("最终目标中文未完成", warnings[0].message)
        self.assertIn("共 1 个片段", warnings[1].message)
        self.assertEqual(warnings[1].percent, 100)

    def test_second_leg_failure_restores_initial_japanese_not_intermediate_english(self):
        result = self.run_model(["こんにちは"], lambda leg, text, count: [["Hello"]] if leg == "ja-en" else [["▁"]] * count)
        self.assertEqual(result, ["こんにちは"])
        self.assertNotEqual(result, ["Hello"])
        warnings = [update.message for update in self.updates if update.stage == "translation_warning"]
        self.assertTrue(warnings[0].startswith("片段 1：英语 → 中文"))
        self.assertIn("最初原文", warnings[0])

    def test_one_untranslated_fragment_does_not_drop_or_stop_other_fragments(self):
        def respond(leg, text, count):
            if leg == "en-zh":
                return [["我是学生"]]
            if text == "こんにちは":
                return [["▁"]] * count
            return [["I am a student"]]

        result = self.run_model(["こんにちは", "私は学生です"], respond)
        self.assertEqual(result, ["こんにちは", "我是学生"])
        self.assertFalse(any(leg == "en-zh" and text == "こんにちは" for leg, text, _ in self.calls))
        warnings = [update.message for update in self.updates if update.stage == "translation_warning"]
        self.assertEqual(len(warnings), 2)
        self.assertTrue(warnings[0].startswith("片段 1："))
        self.assertIn("共 1 个片段", warnings[-1])

    def test_punctuation_only_fragments_are_preserved_without_inference_or_warning(self):
        originals = ["...", "！？", "♪", " ", "🙂"]
        result = self.run_model(originals, lambda *args: self.fail("punctuation needs no model"))
        self.assertEqual(result, originals)
        self.assertEqual(self.calls, [])
        self.assertFalse(any(update.stage == "translation_warning" for update in self.updates))

    def test_punctuation_only_candidate_is_not_accepted_for_meaningful_input(self):
        def respond(leg, text, count):
            if leg == "en-zh":
                return [["你好"]]
            return [["."]] if count == 1 else [["!"], ["Hello"], ["▁"], [" "]]

        result = self.run_model(["こんにちは"], respond)
        self.assertEqual(result, ["你好"])
        self.assertEqual([call[2]["num_hypotheses"] for call in self.calls], [1, 4, 1])

    def test_cancellation_after_empty_result_prevents_retry_and_partial_output(self):
        cancelled = threading.Event()

        def respond(leg, text, count):
            cancelled.set()
            return [["▁"]]

        with self.assertRaises(TranscriptionCancelled):
            self.run_model(["こんにちは"], respond, cancel_event=cancelled)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(any(update.stage == "translation_warning" for update in self.updates))

    def test_cancellation_during_retry_does_not_return_a_fallback_file(self):
        cancelled = threading.Event()

        def respond(leg, text, count):
            if count == 4:
                cancelled.set()
                return [["Hello"]]
            return [["▁"]]

        with self.assertRaises(TranscriptionCancelled):
            self.run_model(["こんにちは"], respond, cancel_event=cancelled)
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(any(update.stage == "translation_warning" for update in self.updates))

    def test_model_added_ass_overrides_are_removed_before_next_pivot(self):
        def respond(leg, text, count):
            if leg == "ja-en":
                return [[r"{\fnArial\fs20\bord1\3c&HFFFFFF&}Hello"]]
            self.assertEqual(text, "Hello")
            return [["你好"]]

        original = ["こんにちは"]
        result = self.run_model(original, respond)
        self.assertEqual(result, ["你好"])
        self.assertEqual(original, ["こんにちは"])
        warnings = [update.message for update in self.updates if update.stage == "translation_warning"]
        self.assertEqual(warnings, ["片段 1：日语 → 英语 已移除模型误加的 ASS 样式标记，译文正文已保留。"])

    def test_last_leg_model_added_override_keeps_chinese_body_and_warns(self):
        result = self.run_model(["こんにちは"], lambda leg, text, count: [["Hello"]] if leg == "ja-en" else [[r"{\1c&HFFFFFF&\fnArial}你好{\r}"]])
        self.assertEqual(result, ["你好"])
        warnings = [update.message for update in self.updates if update.stage == "translation_warning"]
        self.assertEqual(warnings, ["片段 1：英语 → 中文 已移除模型误加的 ASS 样式标记，译文正文已保留。"])

    def test_style_only_top_candidate_retries_and_does_not_warn_for_discarded_style(self):
        def respond(leg, text, count):
            if leg == "en-zh":
                return [["你好"]]
            if count == 1:
                return [[r"{\fnArial\fs20}"]]
            return [[r"{\fnArial}"], ["Hello"], ["Other"], ["Last"]]

        self.assertEqual(self.run_model(["こんにちは"], respond), ["你好"])
        self.assertEqual([call[2]["num_hypotheses"] for call in self.calls], [1, 4, 1])
        self.assertFalse(any(update.stage == "translation_warning" for update in self.updates))

    def test_selected_retry_candidate_style_cleanup_warns_once_without_untranslated_summary(self):
        def respond(leg, text, count):
            if leg == "en-zh":
                self.assertEqual(text, "Hello")
                return [["你好"]]
            return [["▁"]] if count == 1 else [["▁"], [r"{\fs20}Hello"], ["Other"], ["Last"]]

        self.assertEqual(self.run_model(["こんにちは"], respond), ["你好"])
        warnings = [update.message for update in self.updates if update.stage == "translation_warning"]
        self.assertEqual(warnings, ["片段 1：日语 → 英语 已移除模型误加的 ASS 样式标记，译文正文已保留。"])

    def test_all_style_only_candidates_fall_back_instead_of_treating_tag_words_as_body(self):
        result = self.run_model(["こんにちは"], lambda leg, text, count: [[r"{\fnArial\fs20}"]] * count)
        self.assertEqual(result, ["こんにちは"])
        warnings = [update.message for update in self.updates if update.stage == "translation_warning"]
        self.assertEqual(len(warnings), 2)
        self.assertIn("最终目标中文未完成", warnings[0])
        self.assertIn("共 1 个片段", warnings[1])
        self.assertFalse(any("已移除" in warning for warning in warnings))

    def test_plain_braces_html_and_unclosed_override_are_not_silently_removed(self):
        tokenizer = SimpleNamespace(decode_pieces=lambda tokens: " ".join(tokens))
        for text in ("{ordinary text}Hello", "<i>Hello</i>", r"{\fnArial Hello", "{\\ not a command}Hello"):
            with self.subTest(text=text):
                result, cleaned = offline._decode_candidate(tokenizer, [text], "")
                self.assertEqual(result, text)
                self.assertFalse(cleaned)

    def test_kana_only_intermediate_result_retries_before_english_to_chinese(self):
        def respond(leg, text, count):
            if leg == "en-zh":
                self.assertEqual(text, "No")
                return [["不是"]]
            return [["いいえ"]] if count == 1 else [["いいえ"], ["No"], ["Other"], ["Last"]]

        self.assertEqual(self.run_model(["違います"], respond), ["不是"])
        self.assertEqual([call[2]["num_hypotheses"] for call in self.calls], [1, 4, 1])
        self.assertFalse(any(update.stage == "translation_warning" for update in self.updates))

    def test_kana_only_final_result_retries_to_a_valid_chinese_candidate(self):
        def respond(leg, text, count):
            if leg == "ja-en":
                return [["Hello"]]
            return [["まだ"]] if count == 1 else [["まだ"], ["你好"], ["Other"], ["Last"]]

        self.assertEqual(self.run_model(["こんにちは"], respond), ["你好"])
        self.assertEqual([call[2]["num_hypotheses"] for call in self.calls], [1, 1, 4])

    def test_kana_only_candidates_keep_initial_source_with_untranslated_warning(self):
        result = self.run_model(["元の文章"], lambda leg, text, count: [["カナ"]] * count)
        self.assertEqual(result, ["元の文章"])
        self.assertEqual([call[0] for call in self.calls], ["ja-en", "ja-en"])
        warnings = [update.message for update in self.updates if update.stage == "translation_warning"]
        self.assertIn("最终目标中文未完成", warnings[0])
        self.assertIn("共 1 个片段", warnings[-1])

    def test_numbers_and_punctuation_do_not_make_kana_a_target_language_result(self):
        for text in ("123まだ!", "カタカナ?42", "ｶﾅ...", "かな・42"):
            with self.subTest(text=text):
                self.assertFalse(offline._candidate_valid(text, "zh"))
                self.assertFalse(offline._candidate_valid(text, "en"))

    def test_middle_dot_and_shared_han_are_accepted(self):
        for text in ("阿莉丝・玛莉", "・中文", "中文", "ABC・42"):
            with self.subTest(text=text):
                self.assertTrue(offline._candidate_valid(text, "zh"))

    def test_mixed_proper_name_scripts_are_not_rejected_as_kana_only(self):
        for text in ("阿莉丝カナ", "Kanaカナ", "한カナ"):
            with self.subTest(text=text):
                self.assertTrue(offline._candidate_valid(text, "zh"))

    def test_kana_is_valid_for_a_japanese_target(self):
        self.assertTrue(offline._candidate_valid("まだ", "ja"))
        self.assertTrue(offline._candidate_valid("カタカナ", "ja"))

    def test_unicode_private_use_is_invalid_even_with_other_letters(self):
        for text, language in (("正文\ue000", "zh"), ("Hello\U000f0000", "en"), ("こんにちは\U00100000", "ja")):
            with self.subTest(language=language):
                self.assertFalse(offline._candidate_valid(text, language))

    def test_private_use_output_recovers_without_leaking_discarded_style_warning(self):
        def respond(leg, text, count):
            if leg == "en-zh":
                return [["你好"]]
            return [[r"{\fnArial}" + "\ue000broken"]] if count == 1 else [["\ue000broken"], ["Hello"], ["Other"], ["Last"]]

        self.assertEqual(self.run_model(["こんにちは"], respond), ["你好"])
        self.assertFalse(any(update.stage == "translation_warning" for update in self.updates))

    def test_private_use_final_candidates_fall_back_to_original_not_english(self):
        result = self.run_model(["こんにちは"], lambda leg, text, count: [["Hello"]] if leg == "ja-en" else [["\ue000噪声"]] * count)
        self.assertEqual(result, ["こんにちは"])
        warnings = [update.message for update in self.updates if update.stage == "translation_warning"]
        self.assertTrue(warnings[0].startswith("片段 1：英语 → 中文"))
        self.assertIn("最终目标中文未完成", warnings[0])


if __name__ == "__main__":
    unittest.main()
