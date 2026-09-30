"""Subtitle translation, format conversion, and media transcription GUI."""
from __future__ import annotations

import os
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
import queue
import sys
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from runtime_config import model_cache

ROOT = Path(__file__).resolve().parent
CACHE = model_cache("subtitle")
TRANSLATION_CACHE = model_cache("translation")
INPUT_MODES = {"字幕文件 · 翻译与格式转换": "subtitle", "视频 / 音频 · 自动生成字幕": "media"}
ENCODINGS = {"自动识别（UTF-8 / BOM）": "auto", "UTF-8": "utf-8",
             "UTF-8（BOM）": "utf-8-sig", "GB18030 · 中文旧编码": "gb18030",
             "UTF-16 LE": "utf-16-le", "UTF-16 BE": "utf-16-be"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt"}
LANGUAGES = {"自动检测": "auto", "中文": "zh", "英语": "en", "日语": "ja",
             "韩语": "ko", "法语": "fr", "德语": "de", "西班牙语": "es"}
MODELS = {"轻量 · tiny": "tiny", "均衡 · base（推荐）": "base",
          "更准确 · small": "small", "高精度 · medium": "medium",
          "大模型 · large-v3": "large-v3", "快速大模型 · turbo": "turbo"}
TARGETS = {"不翻译 · 原语言": None, **{label: code for label, code in LANGUAGES.items() if code != "auto"}}
BACKENDS = {"本地离线翻译": "offline", "在线 AI 翻译": "online"}


@dataclass(frozen=True)
class _TaskSettings:
    source: Path
    destination: Path
    language: str
    model: str
    formats: tuple[str, ...]
    translation: object = field(repr=False)
    input_mode: str = "subtitle"
    encoding: str = "auto"


def _language_label(code):
    return next((label for label, value in LANGUAGES.items() if value == code), code)


class SubtitleApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("听见 · 字幕翻译与转换")
        self.geometry("920x900")
        self.minsize(820, 800)
        self.configure(bg="#f3f5f9")
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = False
        self.active_task: _TaskSettings | None = None
        self._translation_incomplete = False
        self.output_files: list[Path] = []
        self.controls = []
        self.input_mode = tk.StringVar(value="字幕文件 · 翻译与格式转换")
        self.active_input_mode = "subtitle"
        self._previous_input_mode = "subtitle"
        self._mode_formats = {"subtitle": {"srt"}, "media": {"srt", "txt"}}
        self.file = tk.StringVar()
        self.destination = tk.StringVar(value=str(ROOT / "字幕结果"))
        self.language = tk.StringVar(value="自动检测")
        self.model = tk.StringVar(value="均衡 · base（推荐）")
        self.encoding = tk.StringVar(value="自动识别（UTF-8 / BOM）")
        self.target = tk.StringVar(value="不翻译 · 原语言")
        self.backend = tk.StringVar(value="本地离线翻译")
        self.bilingual = tk.BooleanVar(value=False)
        self.api_base_url = "https://api.openai.com/v1"
        self.api_model = ""
        self.api_key = ""
        self.translation_hint = tk.StringVar()
        self.file_hint = tk.StringVar()
        self.settings_hint = tk.StringVar()
        self.output_hint = tk.StringVar()
        self.formats = {code: tk.BooleanVar(value=code == "srt") for code in ("srt", "ass", "ssa", "vtt", "txt")}
        self.format_controls = {}
        self.status = tk.StringVar(value="选择一个字幕文件，可直接转换格式，也可以翻译。")
        self._build()
        for control, _ in self.controls:
            if isinstance(control, ttk.Combobox):
                for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                    control.bind(sequence, self._block_combobox_wheel, add="+")
        self._refresh_input_mode()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(120, self._poll)

    def _build(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f5f9")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("TLabel", font=("Microsoft YaHei UI", 10), background="#ffffff", foreground="#17243b")
        style.configure("Muted.TLabel", foreground="#667085", font=("Microsoft YaHei UI", 9))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 6))
        style.configure("Accent.TButton", background="#2957d5", foreground="white", borderwidth=0)
        style.map("Accent.TButton", background=[("active", "#2047b4"), ("disabled", "#a5b5df")])
        style.configure("TEntry", padding=5, font=("Microsoft YaHei UI", 10))
        style.configure("TCombobox", padding=4, font=("Microsoft YaHei UI", 10))
        style.configure("TCheckbutton", background="white", font=("Microsoft YaHei UI", 10))
        style.configure("Horizontal.TProgressbar", background="#2957d5", troughcolor="#e9edf5", borderwidth=0)

        shell = ttk.Frame(self, padding=20)
        shell.pack(fill="both", expand=True)
        tk.Label(shell, text="听见", font=("Microsoft YaHei UI", 23, "bold"),
                 bg="#f3f5f9", fg="#17243b", anchor="w").pack(fill="x")
        tk.Label(shell, text="转换字幕格式、翻译字幕，或从视频和音频生成字幕。",
                 font=("Microsoft YaHei UI", 11), bg="#f3f5f9", fg="#667085", anchor="w").pack(fill="x", pady=(4, 12))
        card = ttk.Frame(shell, style="Card.TFrame", padding=14)
        card.pack(fill="x")
        card.columnconfigure(0, weight=1)

        mode_row = ttk.Frame(card, style="Card.TFrame")
        mode_row.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(mode_row, text="输入类型").pack(side="left", padx=(0, 12))
        self.mode_combo = ttk.Combobox(mode_row, textvariable=self.input_mode, values=list(INPUT_MODES),
                                       state="readonly", width=33)
        self.mode_combo.pack(side="left")
        self.mode_combo.bind("<<ComboboxSelected>>", self._refresh_input_mode)
        self.controls.append((self.mode_combo, "readonly"))
        self.file_label = ttk.Label(card, text="01  选择字幕文件", font=("Microsoft YaHei UI", 11, "bold"))
        self.file_label.grid(row=1, column=0, sticky="w", pady=(0, 5))
        entry = ttk.Entry(card, textvariable=self.file)
        entry.grid(row=2, column=0, sticky="ew", padx=(0, 10))
        browse = ttk.Button(card, text="选择文件", command=self._choose_file)
        browse.grid(row=2, column=1)
        self.controls.extend([(entry, "normal"), (browse, "normal")])
        ttk.Label(card, textvariable=self.file_hint, style="Muted.TLabel").grid(row=3, column=0, columnspan=2, sticky="w", pady=(5, 10))

        notebook = ttk.Notebook(card)
        notebook.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self.notebook = notebook
        settings = ttk.Frame(notebook, style="Card.TFrame", padding=10)
        notebook.add(settings, text="字幕文件设置")
        self.settings_panel = settings
        for column in range(3):
            settings.columnconfigure(column, weight=1, uniform="settings")
        self.language_label = ttk.Label(settings, text="原字幕语言")
        self.language_label.grid(row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Label(settings, text="识别模型").grid(row=0, column=1, sticky="w", pady=(0, 6))
        ttk.Label(settings, text="字幕文件编码").grid(row=0, column=2, sticky="w", pady=(0, 6))
        self.language_combo = ttk.Combobox(settings, textvariable=self.language, values=list(LANGUAGES), state="readonly", width=15)
        self.model_combo = ttk.Combobox(settings, textvariable=self.model, values=list(MODELS), state="readonly", width=20)
        self.encoding_combo = ttk.Combobox(settings, textvariable=self.encoding, values=list(ENCODINGS), state="readonly", width=20)
        for column, combo in enumerate((self.language_combo, self.model_combo, self.encoding_combo)):
            combo.grid(row=1, column=column, sticky="ew", padx=(0, 10 if column < 2 else 0))
            self.controls.append((combo, "readonly"))
        ttk.Label(settings, textvariable=self.settings_hint, style="Muted.TLabel", wraplength=790).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        translation = ttk.Frame(notebook, style="Card.TFrame", padding=10)
        notebook.add(translation, text="字幕翻译")
        translation.columnconfigure(0, weight=1)
        translation.columnconfigure(1, weight=1)
        ttk.Label(translation, text="目标字幕语言").grid(row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Label(translation, text="翻译方式").grid(row=0, column=1, sticky="w", pady=(0, 6))
        self.target_combo = ttk.Combobox(translation, textvariable=self.target, values=list(TARGETS), state="readonly")
        self.target_combo.grid(row=1, column=0, sticky="ew", padx=(0, 12))
        self.backend_combo = ttk.Combobox(translation, textvariable=self.backend, values=list(BACKENDS), state="readonly")
        self.backend_combo.grid(row=1, column=1, sticky="ew")
        self.target_combo.bind("<<ComboboxSelected>>", self._refresh_translation_controls)
        self.backend_combo.bind("<<ComboboxSelected>>", self._refresh_translation_controls)
        self.bilingual_check = ttk.Checkbutton(translation, text="双语字幕：原文在上，译文在下", variable=self.bilingual)
        self.bilingual_check.grid(row=2, column=0, sticky="w", pady=(9, 0))
        self.api_button = ttk.Button(translation, text="API 设置…", command=self._api_settings)
        self.api_button.grid(row=2, column=1, sticky="e", pady=(9, 0))
        self.controls.extend([(self.target_combo, "readonly"), (self.backend_combo, "readonly"),
                              (self.bilingual_check, "normal"), (self.api_button, "normal")])
        ttk.Label(translation, textvariable=self.translation_hint, style="Muted.TLabel", wraplength=790).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(card, text="02  保存字幕", font=("Microsoft YaHei UI", 11, "bold")).grid(row=5, column=0, sticky="w", pady=(0, 7))
        destination = ttk.Entry(card, textvariable=self.destination)
        destination.grid(row=6, column=0, sticky="ew", padx=(0, 10))
        choose_dest = ttk.Button(card, text="更改目录", command=self._choose_destination)
        choose_dest.grid(row=6, column=1)
        self.controls.extend([(destination, "normal"), (choose_dest, "normal")])
        formats = ttk.Frame(card, style="Card.TFrame")
        formats.grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
        for code, label in [("srt", "SRT · 通用"), ("ass", "ASS · 样式"), ("ssa", "SSA · 样式"),
                            ("vtt", "VTT · 网页"), ("txt", "TXT · 文本")]:
            check = ttk.Checkbutton(formats, text=label, variable=self.formats[code])
            check.pack(side="left", padx=(0, 16))
            self.controls.append((check, "normal"))
            self.format_controls[code] = check
        ttk.Label(card, textvariable=self.output_hint, style="Muted.TLabel", wraplength=790).grid(row=8, column=0, columnspan=2, sticky="w", pady=(7, 0))

        actions = ttk.Frame(shell)
        actions.pack(fill="x", pady=(12, 9))
        self.start_button = ttk.Button(actions, text="开始转换字幕", style="Accent.TButton", command=self._start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="取消", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=10)
        self.open_button = ttk.Button(actions, text="打开结果目录", command=self._open_output, state="disabled")
        self.open_button.pack(side="right")
        self.progress = ttk.Progressbar(shell, mode="indeterminate", maximum=100)
        self.progress.pack(fill="x", pady=(0, 9))
        tk.Label(shell, textvariable=self.status, bg="#f3f5f9", fg="#43516b", anchor="w",
                 font=("Microsoft YaHei UI", 10), wraplength=810).pack(fill="x", pady=(0, 9))
        log_frame = ttk.Frame(shell)
        tk.Label(shell, text="语音识别在本机完成 · 离线翻译首次下载模型 · 在线翻译使用你配置的 API",
                 bg="#f3f5f9", fg="#7b8495", font=("Microsoft YaHei UI", 9), anchor="w").pack(side="bottom", fill="x", pady=(10, 0))
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, height=7, wrap="word", font=("Microsoft YaHei UI", 10),
                           bg="#ffffff", fg="#344054", relief="flat", padx=13, pady=10, state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    @staticmethod
    def _block_combobox_wheel(event):
        # TCombobox's default MouseWheel class binding changes its value even
        # when the user only meant to scroll. Popup-list scrolling, clicking,
        # and keyboard selection use separate bindings and remain available.
        return "break"

    @staticmethod
    def _incomplete_translation_summary(message):
        return isinstance(message, str) and message.startswith("离线翻译结束：") and "未完成目标" in message

    def _choose_file(self):
        if self._subtitle_mode():
            title = "选择需要翻译或转换格式的字幕文件"
            filetypes = [("字幕文件", "*.srt *.ass *.ssa *.vtt"), ("SRT 字幕", "*.srt"),
                         ("ASS / SSA 字幕", "*.ass *.ssa"), ("WebVTT 字幕", "*.vtt"), ("所有文件", "*.*")]
        else:
            title = "选择需要生成字幕的视频或音频"
            filetypes = [("视频和音频", "*.mp4 *.mkv *.mov *.avi *.webm *.mp3 *.wav *.m4a *.flac *.ogg *.aac *.wma"),
                         ("所有文件", "*.*")]
        path = filedialog.askopenfilename(title=title, filetypes=filetypes, parent=self)
        if path:
            self.file.set(path)

    def _choose_destination(self):
        path = filedialog.askdirectory(title="字幕保存目录", parent=self)
        if path:
            self.destination.set(path)

    def _subtitle_mode(self):
        return INPUT_MODES[self.input_mode.get()] == "subtitle"

    def _refresh_input_mode(self, event=None):
        mode = INPUT_MODES[self.input_mode.get()]
        if mode != self._previous_input_mode:
            self._mode_formats[self._previous_input_mode] = {
                code for code, selected in self.formats.items() if selected.get()
            }
            for code, selected in self.formats.items():
                selected.set(code in self._mode_formats[mode])
            self._previous_input_mode = mode
        subtitle_mode = mode == "subtitle"
        self.file_label.configure(text="01  选择字幕文件" if subtitle_mode else "01  选择视频或音频")
        self.file_hint.set("支持 SRT、ASS、SSA、VTT 字幕文件。" if subtitle_mode else
                           "支持 MP4、MKV、MOV、MP3、WAV、M4A 等常见格式。")
        self.language_label.configure(text="原字幕语言" if subtitle_mode else "视频中的语言")
        self.notebook.tab(self.settings_panel, text="字幕文件设置" if subtitle_mode else "识别设置")
        self.settings_hint.set(
            "仅转换格式无需指定语言；翻译字幕时，请选择具体的原字幕语言。乱码时可更改文件编码。"
            if subtitle_mode else "中文视频建议直接选“中文”。较大的识别模型通常更准确，也需要更多时间和内存。"
        )
        self.output_hint.set(
            "同格式处理保留样式等信息；格式互转时的兼容提示会显示在日志中。"
            if subtitle_mode else "从视频或音频生成 SRT、VTT 字幕与 TXT 文本。"
        )
        self.model_combo.configure(state="readonly" if not subtitle_mode and not self.running else "disabled")
        self.encoding_combo.configure(state="readonly" if subtitle_mode and not self.running else "disabled")
        for code, control in self.format_controls.items():
            supported = subtitle_mode or code in ("srt", "vtt", "txt")
            control.configure(state="normal" if supported and not self.running else "disabled")
            if not supported:
                self.formats[code].set(False)
        self._refresh_translation_controls()

    def _refresh_translation_controls(self, event=None):
        active = TARGETS[self.target.get()] is not None and not self.running
        online = BACKENDS[self.backend.get()] == "online"
        self.backend_combo.configure(state="readonly" if active else "disabled")
        self.bilingual_check.configure(state="normal" if active else "disabled")
        self.api_button.configure(state="normal" if active and online else "disabled")
        if self._subtitle_mode():
            self.start_button.configure(text="开始翻译字幕" if TARGETS[self.target.get()] else "开始转换字幕")
        else:
            self.start_button.configure(text="生成并翻译字幕" if TARGETS[self.target.get()] else "开始生成字幕")
        if TARGETS[self.target.get()] is None:
            self.translation_hint.set("当前保留原语言。选择目标语言后，会翻译字幕并保留起止时间。")
        elif online:
            self.translation_hint.set(f"最终字幕目标：{self.target.get()}。在线模式仅发送字幕文本至你设置的 API；密钥只保存在本次运行的内存中。")
        else:
            hint = f"最终字幕目标：{self.target.get()}。首次下载模型后可离线使用。英语中转模型用于中间步骤，最终字幕仍为{self.target.get()}。"
            if self._subtitle_mode():
                hint += "请准确选择原字幕语言。"
            self.translation_hint.set(hint)

    def _api_settings(self):
        if self.running:
            return
        dialog = tk.Toplevel(self)
        dialog.title("在线翻译设置")
        dialog.geometry("610x390")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        panel = ttk.Frame(dialog, style="Card.TFrame", padding=22)
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(0, weight=1)
        variables = [("API 基础地址（包含 /v1）", tk.StringVar(value=self.api_base_url)),
                     ("模型名称（按服务商填写）", tk.StringVar(value=self.api_model)),
                     ("API 密钥", tk.StringVar(value=self.api_key))]
        for index, (label, variable) in enumerate(variables):
            ttk.Label(panel, text=label).grid(row=index * 2, column=0, sticky="w", pady=(0, 5))
            entry = ttk.Entry(panel, textvariable=variable, show="•" if index == 2 else "")
            entry.grid(row=index * 2 + 1, column=0, sticky="ew", pady=(0, 13))
        ttk.Label(panel, text="支持 OpenAI 兼容的 Chat Completions API。仅发送字幕文本；\n服务可能收费。设置只用于本次运行，不写入磁盘。", style="Muted.TLabel").grid(row=6, column=0, sticky="w")
        def save():
            self.api_base_url = variables[0][1].get().strip()
            self.api_model = variables[1][1].get().strip()
            self.api_key = variables[2][1].get().strip()
            dialog.destroy()
        ttk.Button(panel, text="保存本次设置", style="Accent.TButton", command=save).grid(row=7, column=0, sticky="e", pady=(15, 0))

    def _append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_running(self, running):
        self.running = running
        for control, normal in self.controls:
            control.configure(state="disabled" if running else normal)
        self.start_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")
        self.open_button.configure(state="normal" if self.output_files and not running else "disabled")
        if running:
            self.progress.configure(mode="indeterminate", value=0)
            self.progress.start(15)
        else:
            self.progress.stop()
        self._refresh_input_mode()

    def _start(self):
        if self.running:
            return
        source = Path(self.file.get().strip().strip('"'))
        if not self.file.get().strip() or not source.is_file():
            messagebox.showwarning("请选择文件", "先选择一个存在的字幕文件。" if self._subtitle_mode() else
                                   "先选择一个存在的视频或音频文件。", parent=self)
            return
        mode = INPUT_MODES[self.input_mode.get()]
        if mode == "subtitle" and source.suffix.lower() not in SUBTITLE_EXTENSIONS:
            messagebox.showwarning("字幕格式不支持", "请选择 SRT、ASS、SSA 或 VTT 字幕文件。视频和音频请切换输入类型。", parent=self)
            return
        if mode == "media" and source.suffix.lower() in SUBTITLE_EXTENSIONS:
            messagebox.showwarning("请切换输入类型", "这是字幕文件，请切换到“字幕文件 · 翻译与格式转换”。", parent=self)
            return
        formats = tuple(name for name, selected in self.formats.items()
                        if selected.get() and (mode == "subtitle" or name in ("srt", "vtt", "txt")))
        if not formats:
            messagebox.showwarning("选择导出格式", "请至少选择一种字幕格式。", parent=self)
            return
        if not self.destination.get().strip():
            messagebox.showwarning("选择保存位置", "请设置字幕保存目录。", parent=self)
            return
        destination = Path(self.destination.get().strip().strip('"'))
        # Capture all settings before updating UI state or invoking callbacks.
        # The worker receives this snapshot rather than reading Tk variables.
        language = LANGUAGES[self.language.get()]
        model = MODELS[self.model.get()]
        encoding = ENCODINGS[self.encoding.get()]
        translation_options = None
        target_language = TARGETS[self.target.get()]
        if target_language:
            if mode == "subtitle" and language == "auto":
                messagebox.showwarning("请选择原字幕语言", "翻译字幕文件时，请在“字幕文件设置”中选择具体的原字幕语言。\n仅转换格式时可以保留“自动检测”。", parent=self)
                self.notebook.select(self.settings_panel)
                return
            from translation_core import TranslationOptions, validate_options
            translation_options = TranslationOptions(backend=BACKENDS[self.backend.get()], target_language=target_language,
                bilingual=self.bilingual.get(), online_base_url=self.api_base_url, online_model=self.api_model,
                api_key=self.api_key, model_cache=TRANSLATION_CACHE)
            if mode != "subtitle" or language != target_language:
                try:
                    validate_options(translation_options)
                except ValueError as error:
                    messagebox.showwarning("翻译设置不完整", str(error), parent=self)
                    return
        task = _TaskSettings(source, destination, language, model, formats,
                             translation_options, mode, encoding)
        self.active_task = task
        self.output_files = []
        self.cancel_event.clear()
        self._translation_incomplete = False
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.active_input_mode = mode
        self.status.set("正在读取并处理字幕…" if mode == "subtitle" else "正在准备识别模型。首次下载可能需要几分钟…")
        self._append(f"文件：{source.name}")
        self._append("操作：字幕翻译与格式转换" if mode == "subtitle" else "操作：视频 / 音频自动识别")
        if task.translation is not None:
            source_label = _language_label(task.language)
            target_label = _language_label(task.translation.target_language)
            backend_label = next(label for label, code in BACKENDS.items() if code == task.translation.backend)
            subtitle_style = "双语字幕" if task.translation.bilingual else "译文字幕"
            self._append(f"翻译设置：{source_label}（{task.language}） → {target_label}（{task.translation.target_language}） · {backend_label} · {subtitle_style}")
            if task.translation.backend == "offline" and task.language != task.translation.target_language:
                self._append(f"最终目标：{target_label}。如需下载英语中转模型，它只用于翻译中间步骤。")
        else:
            self._append(f"字幕设置：保持原语言 · 原语言：{_language_label(task.language)}（{task.language}）")
        self._append("导出格式：" + "、".join(format_name.upper() for format_name in task.formats))
        self._set_running(True)
        args = (task.source, task.destination, task.language, task.model, task.formats,
                task.translation, task.input_mode, task.encoding)
        threading.Thread(target=self._worker, args=args, daemon=True).start()

    def _worker(self, source, destination, language, model, formats, translation_options=None,
                input_mode="media", encoding="auto"):
        try:
            callback = lambda event: self.events.put(("progress", event))
            if input_mode == "subtitle":
                from subtitle_files import SubtitleFileOptions, process_subtitle_file
                options = SubtitleFileOptions(output_dir=destination, formats=formats, source_language=language,
                                               encoding=encoding, translation=translation_options)
                result = process_subtitle_file(source, options, progress=callback, cancel_event=self.cancel_event)
            else:
                from subtitle_core import TranscriptionOptions, transcribe_media
                options = TranscriptionOptions(model=model, language=language, device="cpu", compute_type="int8",
                                               output_dir=destination, formats=formats, model_cache=CACHE, translation=translation_options)
                result = transcribe_media(source, options, progress=callback, cancel_event=self.cancel_event)
            self.events.put(("done", result))
        except Exception as exc:
            self.events.put(("error", exc))

    def _poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    self._handle_progress(payload)
                elif kind == "done":
                    self.output_files = list(payload.output_paths.values())
                    warnings = tuple(getattr(payload, "warnings", ()) or ())
                    incomplete = self._translation_incomplete or any(self._incomplete_translation_summary(warning) for warning in warnings)
                    completion_note = "（部分片段未翻译，已保留原文）" if incomplete else ""
                    self._set_running(False)
                    self.progress.configure(mode="determinate", value=100)
                    output_language = payload.output_language or payload.language
                    if self.active_input_mode == "subtitle":
                        language_label = "原语言" if output_language in ("auto", "unknown", "und") else output_language
                        self.status.set(f"字幕处理完成{completion_note} · {payload.segment_count} 条字幕 · 字幕：{language_label} · {len(self.output_files)} 个文件")
                    else:
                        self.status.set(f"生成完成{completion_note} · 识别语言：{payload.language} · 字幕：{output_language} · {len(self.output_files)} 个文件")
                    for warning in warnings:
                        self._append(f"提示：{warning}")
                    for path in self.output_files:
                        self._append(f"已保存：{path}")
                elif kind == "error":
                    self._set_running(False)
                    from subtitle_core import TranscriptionCancelled
                    if isinstance(payload, TranscriptionCancelled):
                        self.status.set("已取消。可以重新选择文件并开始。")
                    else:
                        self.status.set("字幕处理失败，详情见下方。" if self.active_input_mode == "subtitle" else "生成失败，详情见下方。")
                        if self.active_task is not None and self.active_task.translation is not None:
                            self._append(f"本次最终字幕目标：{_language_label(self.active_task.translation.target_language)}；目标语言选择已保留。")
                        self._append(self._friendly_error(payload))
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _handle_progress(self, event):
        message = event.message
        if event.stage == "translation_warning" and self._incomplete_translation_summary(message):
            # Media results may omit warnings; retain the explicit backend
            # summary so completion still reports preserved source fragments.
            self._translation_incomplete = True
        if message:
            display_message = message
            if (self.active_task is not None and self.active_task.translation is not None
                    and event.stage.startswith("translat")):
                display_message = f"最终字幕：{_language_label(self.active_task.translation.target_language)} · {message}"
            self.status.set(display_message)
            self._append(message)
        if event.percent is None:
            self.progress.configure(mode="indeterminate")
            self.progress.start(15)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=max(0, min(99, event.percent)))

    @staticmethod
    def _friendly_error(exc):
        detail = str(exc)
        if isinstance(exc, (ImportError, ModuleNotFoundError)):
            return "缺少运行依赖。请通过 Start.cmd 启动，让启动器完成安装。\n" + detail
        lowered = detail.lower()
        if any(word in lowered for word in ("huggingface", "connection", "timeout", "certificate", "network", "offline", "localentrynotfound")):
            return "模型尚未下载完整或下载连接失败。请检查网络后重试；已下载的模型可以离线使用。\n" + detail
        return detail or type(exc).__name__

    def _cancel(self):
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self.status.set("正在取消，请等待当前处理步骤结束…")

    def _open_output(self):
        folder = self.output_files[0].parent if self.output_files else Path(self.destination.get())
        if folder.is_dir():
            os.startfile(str(folder))

    def _close(self):
        if self.running and not messagebox.askyesno("退出生成器", "任务尚未结束。确定退出并停止当前任务吗？", parent=self):
            return
        self.cancel_event.set()
        self.destroy()

    def _self_test(self):
        """Exercise mode-dependent controls without starting a processing job."""
        from unittest.mock import patch

        assert self._subtitle_mode()
        assert {code for code, value in self.formats.items() if value.get()} == {"srt"}
        assert TARGETS[self.target.get()] is None
        assert self.model_combo.instate(["disabled"])
        assert not self.encoding_combo.instate(["disabled"])
        assert str(self.start_button.cget("text")) == "开始转换字幕"

        self.formats["ass"].set(True)
        self.input_mode.set("视频 / 音频 · 自动生成字幕")
        self._refresh_input_mode()
        assert not self.model_combo.instate(["disabled"])
        assert self.encoding_combo.instate(["disabled"])
        assert {code for code, value in self.formats.items() if value.get()} == {"srt", "txt"}
        assert all(self.format_controls[code].instate(["disabled"]) for code in ("ass", "ssa"))
        self._set_running(True)
        assert all(control.instate(["disabled"]) for control, _ in self.controls)
        self._set_running(False)
        assert not self.model_combo.instate(["disabled"])
        assert self.encoding_combo.instate(["disabled"])

        self.input_mode.set("字幕文件 · 翻译与格式转换")
        self._refresh_input_mode()
        assert self.formats["ass"].get()
        assert self.model_combo.instate(["disabled"])
        assert not self.encoding_combo.instate(["disabled"])
        self.target.set("中文")
        self._refresh_translation_controls()
        assert not self.backend_combo.instate(["disabled"])
        assert not self.bilingual_check.instate(["disabled"])
        assert self.api_button.instate(["disabled"])
        assert str(self.start_button.cget("text")) == "开始翻译字幕"
        self.backend.set("在线 AI 翻译")
        self._refresh_translation_controls()
        assert not self.api_button.instate(["disabled"])
        self._set_running(True)
        assert self.api_button.instate(["disabled"])
        self._set_running(False)
        assert not self.api_button.instate(["disabled"])

        warnings = []
        self.file.set("self-test.srt")
        with patch.object(Path, "is_file", return_value=True), patch.object(
                messagebox, "showwarning", side_effect=lambda title, *args, **kwargs: warnings.append(title)):
            self._start()
        assert warnings == ["请选择原字幕语言"]
        assert not self.running
        self.language.set("中文")
        with patch.object(Path, "is_file", return_value=True), patch(
                "translation_core.validate_options", side_effect=AssertionError("same-language API validation")) as validation, patch.object(
                threading, "Thread") as worker:
            self._start()
        validation.assert_not_called()
        worker.return_value.start.assert_called_once()
        assert self.running
        self._set_running(False)
        self.file.set("")
        self.language.set("自动检测")
        self.target.set("不翻译 · 原语言")
        self.backend.set("本地离线翻译")
        self.formats["ass"].set(False)
        self._refresh_translation_controls()
        self.update_idletasks()
        print("GUI_MODE_STATES_OK")


if __name__ == "__main__":
    application = SubtitleApp()
    if "--self-test" in sys.argv:
        application.withdraw()
        try:
            application._self_test()
        finally:
            application.destroy()
        print("GUI_STARTUP_OK")
    else:
        application.mainloop()
