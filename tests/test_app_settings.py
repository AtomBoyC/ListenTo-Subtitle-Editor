"""GUI regressions for accidental selection changes and frozen job settings."""
from dataclasses import FrozenInstanceError
from pathlib import Path
import sys
import tempfile
import tkinter as tk
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


if __name__ == "__main__":
    unittest.main()
