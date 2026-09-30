"""GUI regressions for accidental selection changes and frozen job settings."""
from dataclasses import FrozenInstanceError
from pathlib import Path
import sys
import tempfile
import tkinter as tk
from types import SimpleNamespace
from tkinter import ttk
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from subtitle_core import ProgressUpdate


class AppSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "Japanese.srt"
        self.source.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")
        self.gui = app.SubtitleApp()
        self.gui.withdraw()
        self.addCleanup(self.gui.destroy)
        self.gui.update_idletasks()

    def configure_translation(self):
        self.gui.file.set(str(self.source))
        self.gui.destination.set(str(self.root / "result"))
        self.gui.language.set("日语")
        self.gui.target.set("中文")
        self.gui.backend.set("本地离线翻译")
        self.gui.bilingual.set(True)
        self.gui._refresh_translation_controls()

    def start_without_worker(self):
        with patch.object(app.threading, "Thread") as thread:
            self.gui._start()
        thread.return_value.start.assert_called_once()
        return thread.call_args.kwargs["args"]

    def test_native_readonly_mousewheel_can_change_value_but_app_combos_are_protected(self):
        # Reproduce Tk's class binding with a separate, unguarded control.
        native = ttk.Combobox(self.gui, values=("中文", "英语", "日语"), state="readonly")
        native.current(0)
        native.event_generate("<MouseWheel>", delta=-120)
        self.assertEqual(native.get(), "英语")
        native.destroy()

        for control, _ in self.gui.controls:
            if not isinstance(control, ttk.Combobox):
                continue
            with self.subTest(control=str(control)):
                control.configure(state="readonly")
                control.current(1)
                selected = control.get()
                for delta in (-120, 120, -240, 240):
                    control.event_generate("<MouseWheel>", delta=delta)
                    self.assertEqual(control.get(), selected)

    def test_explicit_selection_and_keyboard_class_bindings_are_unchanged(self):
        self.gui.target_combo.current(list(app.TARGETS).index("中文"))
        self.gui.target_combo.event_generate("<<ComboboxSelected>>")
        self.assertEqual(self.gui.target.get(), "中文")
        self.assertIn("中文", self.gui.translation_hint.get())
        self.gui.target_combo.current(list(app.TARGETS).index("英语"))
        self.gui.target_combo.event_generate("<<ComboboxSelected>>")
        self.assertEqual(self.gui.target.get(), "英语")
        self.assertIn("英语", self.gui.translation_hint.get())
        self.assertEqual(self.gui.target_combo.bind("<KeyPress>"), "")
        self.assertEqual(self.gui.target_combo.bind("<Key-Down>"), "")
        self.assertIn("combobox::Post", self.gui.bind_class("TCombobox", "<Key-Down>"))

    def test_start_freezes_parameters_disables_controls_and_records_final_target(self):
        self.configure_translation()
        args = self.start_without_worker()
        self.assertEqual(args[2], "ja")
        self.assertEqual(args[5].target_language, "zh")
        self.assertEqual(args[5].backend, "offline")
        self.assertTrue(args[5].bilingual)
        self.assertEqual(args[6], "subtitle")
        self.assertTrue(all(control.instate(["disabled"]) for control, _ in self.gui.controls))
        with self.assertRaises(FrozenInstanceError):
            self.gui.active_task.language = "en"
        for delta in (-120, 120):
            self.gui.target_combo.event_generate("<MouseWheel>", delta=delta)
            self.assertEqual(self.gui.target.get(), "中文")
        log = self.gui.log.get("1.0", "end")
        self.assertIn("日语（ja） → 中文（zh）", log)
        self.assertIn("本地离线翻译", log)
        self.assertIn("双语字幕", log)
        self.assertIn("最终目标：中文", log)
        self.assertIn("导出格式：SRT", log)

    def test_ui_callback_changes_cannot_change_worker_snapshot(self):
        self.configure_translation()
        original = self.gui._set_running

        def update_ui_and_mutate_variables(running):
            original(running)
            self.gui.language.set("英语")
            self.gui.target.set("英语")
            self.gui.encoding.set("GB18030 · 中文旧编码")

        with patch.object(self.gui, "_set_running", side_effect=update_ui_and_mutate_variables):
            args = self.start_without_worker()
        self.assertEqual(args[2], "ja")
        self.assertEqual(args[5].target_language, "zh")
        self.assertEqual(args[7], "auto")
        self.assertEqual(self.gui.active_task.language, "ja")
        self.assertEqual(self.gui.active_task.translation.target_language, "zh")

    def test_pivot_download_status_still_displays_final_chinese_target(self):
        self.configure_translation()
        self.start_without_worker()
        self.gui._handle_progress(ProgressUpdate("translating", "正在下载 日语 → 英语 中转模型……", 10))
        self.assertIn("最终字幕：中文", self.gui.status.get())
        self.assertIn("日语 → 英语", self.gui.status.get())
        self.assertIn("最终字幕目标：中文", self.gui.translation_hint.get())
        self.assertEqual(self.gui.active_task.translation.target_language, "zh")

    def test_processing_error_preserves_target_and_restores_editable_settings(self):
        self.configure_translation()
        self.start_without_worker()
        self.gui.events.put(("error", ValueError("模型格式需要重新准备")))
        self.gui._poll()
        self.assertFalse(self.gui.running)
        self.assertEqual(self.gui.target.get(), "中文")
        self.assertEqual(self.gui.language.get(), "日语")
        self.assertEqual(self.gui.backend.get(), "本地离线翻译")
        self.assertTrue(self.gui.bilingual.get())
        self.assertFalse(self.gui.target_combo.instate(["disabled"]))
        self.assertFalse(self.gui.backend_combo.instate(["disabled"]))
        self.assertIn("本次最终字幕目标：中文", self.gui.log.get("1.0", "end"))

    def test_active_task_cannot_open_api_settings_and_secrets_are_not_logged(self):
        self.configure_translation()
        self.gui.backend.set("在线 AI 翻译")
        self.gui.api_model = "example-model"
        self.gui.api_key = "FAKE_SECRET_KEY_FOR_GUI_TEST"
        self.start_without_worker()
        with patch.object(app.tk, "Toplevel", side_effect=AssertionError("settings must remain frozen")):
            self.gui._api_settings()
        self.assertNotIn(self.gui.api_key, self.gui.log.get("1.0", "end"))
        self.assertNotIn(self.gui.api_key, repr(self.gui.active_task))
        self.assertTrue(self.gui.api_button.instate(["disabled"]))

    def result(self, warnings=None, include_warnings=True):
        result = SimpleNamespace(output_paths={"srt": self.root / "result" / "Japanese.zh.srt"},
                                 language="ja", output_language="zh", segment_count=2)
        if include_warnings:
            result.warnings = warnings or ()
        return result

    def test_incomplete_offline_summary_is_visible_on_completion_and_files_remain_available(self):
        self.configure_translation()
        self.start_without_worker()
        summary = "离线翻译结束：1 个片段未完成目标中文翻译，已保留最初原文。"
        result = self.result(("第 2 条字幕片段翻译失败，保留原文。", summary))
        self.gui.events.put(("done", result))
        self.gui._poll()
        self.assertIn("部分片段未翻译", self.gui.status.get())
        self.assertIn("已保留原文", self.gui.status.get())
        self.assertEqual(self.gui.target.get(), "中文")
        self.assertEqual(self.gui.output_files, list(result.output_paths.values()))
        self.assertFalse(self.gui.open_button.instate(["disabled"]))
        log = self.gui.log.get("1.0", "end")
        self.assertIn(summary, log)
        self.assertIn("第 2 条字幕片段", log)
        self.assertIn("已保存：", log)

    def test_plain_style_warnings_do_not_mark_translation_as_incomplete(self):
        self.configure_translation()
        self.start_without_worker()
        warning = "跨格式转换可能丢失高级样式。"
        self.gui.events.put(("done", self.result((warning,))))
        self.gui._poll()
        self.assertIn("字幕处理完成", self.gui.status.get())
        self.assertNotIn("未翻译", self.gui.status.get())
        self.assertIn(warning, self.gui.log.get("1.0", "end"))

    def test_media_completion_without_warning_field_uses_progress_summary_and_next_job_resets_it(self):
        self.configure_translation()
        self.start_without_worker()
        self.gui.active_input_mode = "media"
        summary = "离线翻译结束：1 个片段未完成目标中文翻译，保留原文。"
        self.gui.events.put(("progress", ProgressUpdate("translation_warning", summary)))
        self.gui.events.put(("done", self.result(include_warnings=False)))
        self.gui._poll()
        self.assertIn("生成完成", self.gui.status.get())
        self.assertIn("部分片段未翻译", self.gui.status.get())
        self.assertIn(summary, self.gui.log.get("1.0", "end"))
        self.start_without_worker()
        self.gui.events.put(("done", self.result(include_warnings=False)))
        self.gui._poll()
        self.assertNotIn("未翻译", self.gui.status.get())
        self.assertFalse(self.gui._translation_incomplete)


if __name__ == "__main__":
    unittest.main()
