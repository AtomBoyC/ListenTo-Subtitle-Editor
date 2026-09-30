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


if __name__ == "__main__":
    unittest.main()
