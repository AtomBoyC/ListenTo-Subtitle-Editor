"""Focused formatting, encoding, and transaction checks, without model traffic."""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import subtitle_files as files
from subtitle_core import SubtitleSegment, TranscriptionCancelled
from translation_core import TranslationOptions


SRT = "19\n00:00:02,123 --> 00:00:03,456\n<b>Hello</b> <i>world</i>!\nSecond line.\n\n3\n00:00:00,987 --> 00:00:01,999\nA & B <filename>\n"
ASS_HEADER = "[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Fancy,Arial,32,&H00FFFFFF,&H00000000,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"


class SubtitleFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "result"

    def source(self, text, suffix="srt", encoding="utf-8"):
        path = self.root / f"source.{suffix}"
        path.write_bytes(text.encode(encoding))
        return path

    def process(self, source, formats=("srt",), **kwargs):
        return files.process_subtitle_file(source, files.SubtitleFileOptions(output_dir=self.output, formats=formats, **kwargs))

    def test_html_basic_styles_convert_to_ass_without_losing_literal_angles_or_ampersand(self):
        result = self.process(self.source(SRT), ("ass",))
        document = files.read_subtitle_file(result.output_paths["ass"])
        self.assertEqual([(cue.start_ms, cue.end_ms) for cue in document.cues], [(2120, 3460), (990, 2000)])
        self.assertIn(r"{\b1}Hello{\b0}", document.cues[0].text)
        self.assertIn(r"{\i1}world{\i0}", document.cues[0].text)
        self.assertIn(r"\NSecond line.", document.cues[0].text)
        self.assertEqual(document.cues[1].text, "A & B <filename>")

    def test_ass_basic_styles_convert_to_html_and_plain_literal_angle_text_is_escaped(self):
        source = self.source(ASS_HEADER + r"Dialogue: 0,0:00:01.00,0:00:02.00,Fancy,,0,0,0,,{\b1}Bold{\b0} <filename> &amp;" + "\n", "ass")
        result = self.process(source, ("srt", "vtt", "txt"))
        for format_name in ("srt", "vtt"):
            contents = result.output_paths[format_name].read_text(encoding="utf-8")
            self.assertIn("<b>Bold</b>", contents)
            self.assertIn("&lt;filename&gt; &amp;amp;", contents)
        self.assertEqual(result.output_paths["txt"].read_text(encoding="utf-8"), "Bold <filename> &amp;\n")

    def test_custom_ass_field_order_and_comma_in_text_are_respected(self):
        original = "[Script Info]\nScriptType: v4.00+\n[Events]\nFormat: End, Text, Start, Style, Name\nDialogue: 0:00:02.00,Hello, world,0:00:01.00,Fancy,Alice\n"
        source = self.source(original, "ass")

        def fake(segments, source_language, options, **kwargs):
            self.assertEqual([cue.text for cue in segments], ["Hello, world"])
            return [replace(cue, text="你好，世界") for cue in segments]

        with patch("translation_core.translate_segments", side_effect=fake):
            result = self.process(source, ("ass", "srt"), source_language="en", translation=TranslationOptions())
        self.assertEqual(result.output_paths["ass"].read_text(encoding="utf-8"), original.replace("Hello, world", "你好，世界"))
        self.assertIn("00:00:01,000 --> 00:00:02,000", result.output_paths["srt"].read_text(encoding="utf-8"))

    def test_model_cannot_inject_new_tags_controls_or_blank_text(self):
        source = self.source(SRT)
        for inserted in ("<i>Injected</i>", r"{\an8}Injected", r"injected\Nline", "unclosed <tag", "", "new\nline"):
            with self.subTest(inserted=inserted):
                with patch("translation_core.translate_segments", side_effect=lambda segments, *args, **kwargs: [replace(cue, text=inserted) for cue in segments]):
                    with self.assertRaises(files.SubtitleFileError):
                        self.process(source, ("srt", "ass"), source_language="en", translation=TranslationOptions())
                self.assertFalse(self.output.exists())

    def test_original_literal_math_delimiters_are_protected_before_translation(self):
        source = self.source("1\n00:00:01,000 --> 00:00:02,000\nValue 4 < 5, x > 1 and {literal}.\n")
        seen = []

        def fake(segments, *args, **kwargs):
            seen.extend(cue.text for cue in segments)
            return [replace(cue, text="译文") for cue in segments]

        with patch("translation_core.translate_segments", side_effect=fake):
            result = self.process(source, source_language="en", translation=TranslationOptions())
        self.assertTrue(all(not any(character in text for character in "{}<>") for text in seen))
        text = result.output_paths["srt"].read_text(encoding="utf-8")
        self.assertIn("<", text)
        self.assertIn(">", text)
        self.assertIn("{literal}", text)

    def test_bilingual_rebuilds_ass_controls_and_vtt_inline_time_without_sending_them(self):
        original = "WEBVTT\n\nmy-id\n00:01.000 --> 00:03.000 position:50%\n<v Alice><c.green>Hello<00:00:02.000> world</c></v>\n"
        seen = []

        def fake(segments, language, options, **kwargs):
            self.assertFalse(options.bilingual)
            seen.extend(cue.text for cue in segments)
            return [replace(cue, text="你好" if cue.text == "Hello" else "世界") for cue in segments]

        with patch("translation_core.translate_segments", side_effect=fake):
            result = self.process(self.source(original, "vtt"), ("vtt",), source_language="en", translation=TranslationOptions(bilingual=True))
        contents = result.output_paths["vtt"].read_text(encoding="utf-8")
        self.assertEqual(seen, ["Hello", "world"])
        self.assertIn('<v Alice><c.green>Hello<00:00:02.000> world</c></v>\n<v Alice><c.green>你好<00:00:02.000> 世界</c></v>', contents)
        self.assertIn("my-id\n00:01.000 --> 00:03.000 position:50%", contents)

    def test_explicit_utf16_without_bom_and_utf8_bom_are_read_strictly(self):
        for encoding in ("utf-16-le", "utf-16-be", "utf-8-sig"):
            with self.subTest(encoding=encoding):
                source = self.source(SRT, encoding=encoding)
                result = self.process(source, encoding=encoding)
                self.assertEqual(result.output_paths["srt"].read_text(encoding="utf-8"), SRT)
        with self.assertRaises(files.SubtitleFileError):
            self.process(self.source(SRT, encoding="utf-16-le"))

    def test_comment_and_pure_drawing_do_not_become_visible_but_mixed_text_does(self):
        original = ASS_HEADER + "Comment: 0,0:00:00.00,0:00:00.00,Fancy,,0,0,0,,template code\n" + r"Dialogue: 0,0:00:01.00,0:00:02.00,Fancy,,0,0,0,,{\p1}m 0 0 l 20 20" + "\n" + r"Dialogue: 0,0:00:02.00,0:00:03.00,Fancy,,0,0,0,,Visible {\p1}m 0 0 l 10 10{\p0} text" + "\n"
        result = self.process(self.source(original, "ass"), ("srt", "ass"))
        plain = result.output_paths["srt"].read_text(encoding="utf-8")
        self.assertNotIn("template code", plain)
        self.assertNotIn("m 0 0", plain)
        self.assertIn("Visible  text", plain)
        self.assertEqual(result.output_paths["ass"].read_text(encoding="utf-8"), original)
        self.assertTrue(any("已跳过" in warning for warning in result.warnings))

    def test_unsupported_target_text_fails_entire_multiple_format_export(self):
        source = self.source("1\n00:00:00,001 --> 00:00:01,000\nLiteral {curly} braces\n")
        with self.assertRaises(files.SubtitleFileError):
            self.process(source, ("srt", "ass"))
        self.assertFalse(self.output.exists())

    def test_cancel_during_translation_or_export_leaves_no_partial_set(self):
        source = self.source(SRT)
        event = threading.Event()

        def fake(segments, *args, **kwargs):
            event.set()
            return [replace(cue, text="译文") for cue in segments]

        options = files.SubtitleFileOptions(self.output, ("srt", "vtt"), "en", translation=TranslationOptions())
        with patch("translation_core.translate_segments", side_effect=fake):
            with self.assertRaises(TranscriptionCancelled):
                files.process_subtitle_file(source, options, cancel_event=event)
        self.assertFalse(self.output.exists())
        event.clear()
        original_open = Path.open

        def open_then_cancel(path, *args, **kwargs):
            handle = original_open(path, *args, **kwargs)
            if args and args[0] == "x" and path.suffix == ".vtt":
                event.set()
            return handle

        with patch.object(Path, "open", open_then_cancel):
            with self.assertRaises(TranscriptionCancelled):
                files.process_subtitle_file(source, replace(options, translation=None), cancel_event=event)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(source.read_text(encoding="utf-8"), SRT)

    def test_language_filename_injection_is_rejected_before_outputs(self):
        with self.assertRaises(files.SubtitleFileError):
            self.process(self.source(SRT), source_language="../../escape")
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
