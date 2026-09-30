from pathlib import Path
import unittest

import pysubs2

from ass_format_convert import AssFormatConversionError, convert_ass_format

ROOT = Path(__file__).resolve().parents[1]


class AssFormatConversionTests(unittest.TestCase):
    def sample(self):
        content = (ROOT / "示例" / "英文样式字幕.ass").read_text(encoding="utf-8")
        return content.replace("Comment: 0,0:00:00.00,0:00:01.00", "Comment: 0,0:00:00.00,0:00:00.00") + "\n[Aegisub Extradata]\nData: 0,example,kept\n"

    def test_styles_actor_timings_comment_and_unknown_sections_roundtrip(self):
        original = self.sample()
        converted = convert_ass_format(original, "ass", "ssa")
        returned = convert_ass_format(converted, "ssa", "ass")
        for fmt, text in (("ssa", converted), ("ass", returned)):
            subs = pysubs2.SSAFile.from_string(text, format_=fmt)
            self.assertEqual(subs.styles["Default"].fontname, "Microsoft YaHei")
            self.assertEqual(subs.styles["Default"].fontsize, 48)
            self.assertEqual([(e.start, e.end) for e in subs], [(0, 0), (1000, 3500), (4000, 7000)])
            self.assertEqual(subs.events[-1].name, "Teacher")
            self.assertTrue(subs.events[0].is_comment)
            self.assertIn("[Aegisub Extradata]\nData: 0,example,kept", text)

    def test_reordered_style_columns_keep_values(self):
        content = self.sample()
        rows = content.splitlines()
        index = next(i for i, row in enumerate(rows) if row.startswith("Format: Name, Fontname"))
        names = rows[index].split(": ", 1)[1].split(", ")
        values = rows[index + 1].split(": ", 1)[1].split(",")
        names[0], names[1] = names[1], names[0]
        values[0], values[1] = values[1], values[0]
        rows[index] = "Format: " + ", ".join(names)
        rows[index + 1] = "Style: " + ",".join(values)
        subs = pysubs2.SSAFile.from_string(convert_ass_format("\n".join(rows), "ass", "ssa"), format_="ssa")
        self.assertEqual(subs.styles["Default"].fontname, "Microsoft YaHei")

    def test_noncanonical_events_rejected_instead_of_misreading_fields(self):
        content = self.sample().replace("Format: Layer, Start, End, Style", "Format: Start, Layer, End, Style")
        with self.assertRaises(AssFormatConversionError):
            convert_ass_format(content, "ass", "ssa")


if __name__ == "__main__":
    unittest.main()
