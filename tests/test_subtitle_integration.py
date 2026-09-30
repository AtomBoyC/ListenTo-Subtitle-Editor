"""Independent file-level data-integrity checks; no model or paid service calls."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import subtitle_files as files
from subtitle_core import SubtitleSegment, TranscriptionCancelled
from translation_core import TranslationOptions


SRT = """42
00:00:05,000 --> 00:00:06,000
First line
Second line

42
00:00:01,000 --> 00:00:02,000
Third line

8
00:00:03,000 --> 00:00:04,000
Last line
"""
VTT = """WEBVTT

second
00:00:05.000 --> 00:00:06.000
First line
Second line

first
00:00:01.000 --> 00:00:02.000
Third line

last
00:00:03.000 --> 00:00:04.000
Last line
"""
ASS_HEADER = """[Script Info]
Title: Independent integration sample
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,36,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,1,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
ASS = ASS_HEADER + (
    r"Dialogue: 2,0:00:05.00,0:00:06.00,Default,Speaker,0010,0020,0030,,First line\NSecond line" + "\n"
    "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Third line\n"
    "Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,Last line\n"
)
SSA = r"""[Script Info]
Title: Independent integration sample
ScriptType: v4.00

[V4 Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, TertiaryColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, AlphaLevel, Encoding
Style: Default,Arial,36,16777215,255,0,0,0,0,1,2,1,2,10,10,10,0,1

[Events]
Format: Marked, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: Marked=0,0:00:05.00,0:00:06.00,Default,Speaker,0010,0020,0030,,First line\NSecond line
Dialogue: Marked=0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Third line
Dialogue: Marked=0,0:00:03.00,0:00:04.00,Default,,0,0,0,,Last line
"""


class FileIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "output"

    def source(self, text=SRT, extension="srt", encoding="utf-8"):
        source = self.root / ("existing." + extension)
        source.write_bytes(text.encode(encoding))
        return source

    def process(self, source, formats=("srt",), **kwargs):
        return files.process_subtitle_file(source, files.SubtitleFileOptions(
            output_dir=self.out, formats=formats, **kwargs))

    def fake_translate(self, segments, source_language, options, **kwargs):
        self.assertEqual(source_language, "en")
        self.assertFalse(options.bilingual)
        for segment in segments:
            self.assertNotRegex(segment.text, r"<[^>]+>|\{\\|\\[Nnh]")
        return [SubtitleSegment(segment.start_ms, segment.end_ms, "译文(" + segment.text + ")")
                for segment in segments]

    def test_same_format_keeps_original_timing_order_numbering_and_text(self):
        for fmt, document in (("srt", SRT), ("vtt", VTT), ("ass", ASS), ("ssa", SSA)):
            with self.subTest(fmt=fmt):
                source = self.source(document, fmt)
                result = self.process(source, (fmt,))
                self.assertEqual(result.output_paths[fmt].read_text(encoding="utf-8-sig"), document)
                self.assertEqual(source.read_text(encoding="utf-8"), document)
                self.assertEqual(result.segment_count, 3)

    def test_all_sixteen_format_paths_readable_and_keep_cue_order(self):
        import pysubs2
        fixtures = {"srt": SRT, "vtt": VTT, "ass": ASS, "ssa": SSA}
        for source_format, document in fixtures.items():
            for output_format in fixtures:
                with self.subTest(source=source_format, target=output_format):
                    result = self.process(self.source(document, source_format), (output_format,))
                    parsed = pysubs2.load(result.output_paths[output_format], encoding="utf-8", format_=output_format)
                    self.assertEqual(len(parsed.events), 3)
                    self.assertEqual([event.start for event in parsed.events], [5000, 1000, 3000])
                    self.assertEqual([event.end for event in parsed.events], [6000, 2000, 4000])
                    expected_texts = ["First line\nSecond line", "Third line", "Last line"]
                    if source_format == output_format == "vtt":
                        # pysubs2's permissive SRT-derived reader folds nonnumeric
                        # VTT identifiers into the preceding cue's payload.
                        parsed_document = files.read_subtitle_file(result.output_paths[output_format])
                        self.assertEqual([cue.text for cue in parsed_document.cues], expected_texts)
                    else:
                        self.assertEqual([event.plaintext for event in parsed.events], expected_texts)

    def test_srt_translation_keeps_inline_tags_multiline_indices_and_timings(self):
        document = SRT.replace("First line\nSecond line", '<i>First line</i>\n<font color="red">Second line</font>')
        with patch("translation_core.translate_segments", side_effect=self.fake_translate) as translator:
            result = self.process(self.source(document), translation=TranslationOptions(target_language="zh"), source_language="en")
        content = result.output_paths["srt"].read_text(encoding="utf-8")
        self.assertEqual(content.count("42\n"), 2)
        self.assertIn('00:00:05,000 --> 00:00:06,000\n<i>译文(First line)</i>\n<font color="red">译文(Second line)</font>', content)
        self.assertTrue(translator.called)

    def test_ass_translation_keeps_styles_tags_comments_drawings_and_karaoke(self):
        styled = ASS.replace(r"First line\NSecond line", r"{\an8\pos(100,200)\i1}First line{\i0}\NSecond line")
        drawing = r"Dialogue: 0,0:00:07.00,0:00:08.00,Default,,0,0,0,,{\p1}m 0 0 l 100 0 100 100{\p0}"
        karaoke = r"Dialogue: 0,0:00:09.00,0:00:10.00,Default,,0,0,0,,{\k20}Hello{\k30} world"
        comment = "Comment: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Editor only"
        styled += drawing + "\n" + karaoke + "\n" + comment + "\n"
        with patch("translation_core.translate_segments", side_effect=self.fake_translate):
            result = self.process(self.source(styled, "ass"), ("ass",), translation=TranslationOptions(target_language="zh"), source_language="en")
        content = result.output_paths["ass"].read_text(encoding="utf-8")
        self.assertIn(r"{\an8\pos(100,200)\i1}译文(First line){\i0}\N译文(Second line)", content)
        for protected in (drawing, karaoke, comment):
            self.assertIn(protected, content)
        self.assertIn("Style: Default,Arial,36", content)
        self.assertTrue(result.warnings)

    def test_vtt_translation_keeps_note_style_header_id_and_settings(self):
        document = VTT.replace("WEBVTT\n", "WEBVTT - sample\n\nNOTE Keep editor note\nThis is metadata\n\nSTYLE\n::cue { color: red; }\n")
        document = document.replace("00:00:05.000 --> 00:00:06.000", "00:00:05.000 --> 00:00:06.000 line:10% position:50%")
        with patch("translation_core.translate_segments", side_effect=self.fake_translate):
            result = self.process(self.source(document, "vtt"), ("vtt",), translation=TranslationOptions(target_language="zh"), source_language="en")
        content = result.output_paths["vtt"].read_text(encoding="utf-8")
        self.assertIn("WEBVTT - sample", content)
        self.assertIn("NOTE Keep editor note\nThis is metadata", content)
        self.assertIn("STYLE\n::cue { color: red; }", content)
        self.assertIn("second\n00:00:05.000 --> 00:00:06.000 line:10% position:50%", content)

    def test_strict_utf_bom_and_explicit_chinese_encoding(self):
        for source_encoding, option_encoding in (("utf-8-sig", "auto"), ("utf-16", "auto"), ("gb18030", "gb18030")):
            with self.subTest(encoding=source_encoding):
                document = SRT.replace("Third line", "中文台词")
                result = self.process(self.source(document, encoding=source_encoding), encoding=option_encoding)
                self.assertIn("中文台词", result.output_paths["srt"].read_text(encoding="utf-8-sig"))

    def test_invalid_utf8_fails_without_replacement_or_outputs(self):
        source = self.source()
        source.write_bytes(source.read_bytes().replace(b"First line", b"Invalid \xff text"))
        with self.assertRaises(ValueError):
            self.process(source)
        self.assertFalse(self.out.exists())

    def test_malformed_second_cue_fails_instead_of_silently_dropping(self):
        for malformed in (
            SRT.replace("00:00:01,000 --> 00:00:02,000", "bad timestamp --> 00:00:02,000"),
            SRT + "\n999\nInvalid truncated cue\n",
            SRT.replace("00:00:01,000", "00:99:01,000"),
        ):
            with self.subTest(malformed=malformed[-35:]):
                with self.assertRaises(ValueError):
                    self.process(self.source(malformed))
                self.assertFalse(self.out.exists())

    def test_ass_flat_export_skips_nonvisible_events_with_explicit_warnings(self):
        document = ASS + (
            "Comment: 0,0:00:07.00,0:00:08.00,Default,,0,0,0,,Editor only\n"
            + r"Dialogue: 0,0:00:09.00,0:00:10.00,Default,,0,0,0,,{\p1}m 0 0 l 50 50{\p0}" + "\n"
        )
        result = self.process(self.source(document, "ass"), ("srt", "vtt", "txt"))
        self.assertTrue(result.warnings)
        for path in result.output_paths.values():
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("Editor only", content)
            self.assertNotIn("m 0 0 l 50 50", content)
            self.assertIn("Third line", content)

    def test_translation_failure_leaves_source_and_output_unchanged(self):
        source = self.source()
        original = source.read_bytes()
        with patch("translation_core.translate_segments", side_effect=ValueError("inference failed")):
            with self.assertRaises(ValueError):
                self.process(source, ("srt", "ass", "vtt"), translation=TranslationOptions(target_language="zh"), source_language="en")
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(self.out.exists())

    def test_missing_cue_separator_cannot_silently_merge_entries(self):
        for fmt, document in (("srt", SRT), ("vtt", VTT)):
            with self.subTest(fmt=fmt):
                malformed = document.replace("\n\n", "\n") if fmt == "srt" else document.replace("Second line\n\nfirst", "Second line\nfirst")
                with self.assertRaises(ValueError):
                    self.process(self.source(malformed, fmt), (fmt,))
                self.assertFalse(self.out.exists())

    def test_ass_rounding_at_maximum_cannot_create_overflow(self):
        document = "1\n09:59:59,989 --> 09:59:59,990\nVery short ending\n"
        with self.assertRaises(ValueError):
            self.process(self.source(document), ("ass",))
        self.assertFalse(self.out.exists())

    def test_zero_duration_ass_editor_comments_are_preserved(self):
        comment = "Comment: 0,0:00:00.00,0:00:00.00,Default,,0,0,0,code once,Editor template"
        document = ASS + comment + "\n"
        result = self.process(self.source(document, "ass"), ("ass", "ssa"))
        self.assertIn(comment, result.output_paths["ass"].read_text(encoding="utf-8"))
        self.assertIn("Comment: Marked=0,0:00:00.00,0:00:00.00", result.output_paths["ssa"].read_text(encoding="utf-8"))

    def test_ass_custom_field_order_and_middle_text_with_commas(self):
        document = ASS_HEADER.replace(
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            "Format: Name, Text, End, Style, Start, MarginL, MarginR, MarginV, Effect, Layer",
        ) + "Dialogue: Speaker,First,line,0:00:06.00,Default,0:00:05.00,10,20,30,,2\n"
        result = self.process(self.source(document, "ass"), ("ass", "ssa", "srt"))
        self.assertEqual(result.output_paths["ass"].read_text(encoding="utf-8"), document)
        for fmt in ("ssa", "srt"):
            parsed = files.read_subtitle_file(result.output_paths[fmt])
            self.assertEqual([(cue.start_ms, cue.end_ms, cue.text) for cue in parsed.cues], [(5000, 6000, "First,line")])
        self.assertTrue(result.warnings)

    def test_vtt_timestamp_voice_and_class_controls_never_sent_to_translator(self):
        controls = "<v Speaker><00:00:05.400><c.green>First line</c></v>"
        document = VTT.replace("First line", controls)
        with patch("translation_core.translate_segments", side_effect=self.fake_translate):
            result = self.process(self.source(document, "vtt"), ("vtt", "srt"), translation=TranslationOptions(target_language="zh"), source_language="en")
        vtt = result.output_paths["vtt"].read_text(encoding="utf-8")
        self.assertIn(controls.replace("First line", "译文(First line)"), vtt)
        srt = result.output_paths["srt"].read_text(encoding="utf-8")
        self.assertNotIn("<00:00:05.400>", srt)
        self.assertNotIn("<v Speaker>", srt)
        self.assertIn("译文(First line)", srt)

    def test_existing_files_and_source_not_overwritten(self):
        source = self.source()
        initial = self.process(source, ("srt", "ass", "vtt"))
        before = {path: path.read_bytes() for path in initial.output_paths.values()}
        second = self.process(source, ("srt", "ass", "vtt"))
        self.assertTrue(set(before).isdisjoint(second.output_paths.values()))
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertEqual(source.read_text(encoding="utf-8"), SRT)

    def test_cancelled_before_processing_saves_nothing(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(TranscriptionCancelled):
            files.process_subtitle_file(self.source(), files.SubtitleFileOptions(output_dir=self.out), cancel_event=event)
        self.assertFalse(self.out.exists())

    def test_conversion_does_not_import_inference_modules(self):
        source = self.source()
        script = (
            "import sys; from pathlib import Path; "
            "from subtitle_files import process_subtitle_file, SubtitleFileOptions; "
            "process_subtitle_file(Path(sys.argv[1]), SubtitleFileOptions(output_dir=Path(sys.argv[2]), formats=('ass','srt'))); "
            "assert not {'faster_whisper','ctranslate2','offline_translate','sentencepiece'} & set(sys.modules)"
        )
        result = subprocess.run([sys.executable, "-c", script, str(source), str(self.out)], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
