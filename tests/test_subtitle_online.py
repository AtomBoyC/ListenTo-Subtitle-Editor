"""Imported subtitle files through the complete loopback API path."""
from pathlib import Path
import tempfile
import unittest

from subtitle_files import SubtitleFileOptions, process_subtitle_file
from translation_core import TranslationError, TranslationOptions
from test_translation_core import LocalAPI


class ImportedSubtitleOnlineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.srt"
        self.document = "7\n00:00:01,000 --> 00:00:03,000\n<i>Hello.</i>\n<b>Please listen.</b>\n\n7\n00:00:04,000 --> 00:00:05,000\n<u>Thank you.</u>\n"
        self.source.write_text(self.document, encoding="utf-8")
        self.api = LocalAPI()
        self.addCleanup(self.api.close)

    def options(self):
        return SubtitleFileOptions(output_dir=self.root / "output", formats=("srt", "ass"), source_language="en",
            translation=TranslationOptions(backend="online", target_language="zh", online_base_url=self.api.base_url,
                online_model="local-test", bilingual=True))

    def test_online_preserves_cues_tags_and_bilingual_text(self):
        result = process_subtitle_file(self.source, self.options())
        output = result.output_paths["srt"].read_text(encoding="utf-8")
        self.assertIn("7\n00:00:01,000 --> 00:00:03,000", output)
        self.assertEqual(output.count("7\n00:"), 2)
        self.assertIn("<i>Hello.</i>\n<b>Please listen.</b>\n<i>译文 1</i>\n<b>译文 2</b>", output)
        self.assertEqual(self.source.read_text(encoding="utf-8"), self.document)
        sent = LocalAPI.entries(self.api.requests[0]["body"])
        self.assertEqual([row["text"] for row in sent], ["Hello.", "Please listen.", "Thank you."])
        self.assertEqual(result.segment_count, 2)

    def test_incomplete_api_translation_exports_nothing(self):
        self.api.response_factory = lambda body: self.api.envelope([{"id": 1, "text": "部分译文"}])
        with self.assertRaises(TranslationError):
            process_subtitle_file(self.source, self.options())
        self.assertFalse((self.root / "output").exists())
        self.assertEqual(self.source.read_text(encoding="utf-8"), self.document)


if __name__ == "__main__":
    unittest.main()
