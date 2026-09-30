# 听见 · 字幕翻译与转换

一个面向 Windows 的本地字幕工具，提供中文桌面界面。输入已有的 **SRT、ASS、SSA、VTT** 字幕，转换格式，或先翻译再导出；也可以从视频和音频生成字幕。

支持本地离线翻译、在线 AI 翻译和原文在上、译文在下的双语输出。使用现成的 Whisper 与 Argos 模型，无需自行训练模型。

## 能做什么

| 操作 | 输入 | 输出 |
| --- | --- | --- |
| 字幕格式转换 | SRT / ASS / SSA / VTT | SRT / ASS / SSA / VTT / TXT |
| 字幕翻译、双语字幕 | SRT / ASS / SSA / VTT | SRT / ASS / SSA / VTT / TXT |
| 语音识别，可接着翻译 | 视频或音频 | SRT / VTT / TXT |

- 可一次选择多个导出格式，例如英文 ASS → 中文 SRT + VTT。
- 同格式处理保留字幕结构、时间轴、编号以及可保护的格式标签；ASS/SSA 保留原样式和事件字段，VTT 保留标识与设置。
- 源文件保留，结果另存；重名自动增加序号。翻译失败或取消时，不保存半套输出文件。
- **只转换格式不加载 Whisper，也不下载翻译模型。**首次启动仍需安装 Python 依赖。
- TXT 是不带时间轴的纯文本导出，不能作为输入还原字幕。

媒体模式需要 ASS/SSA 输出时，可先生成 SRT，再切换到字幕文件模式转换。

## 快速开始

### 1. 准备环境

需要 **Windows、64 位 Python 3.9 或更新版本，并包含 Tcl/Tk**。本项目已在 Python 3.12 环境验证。可从 [Python 官网](https://www.python.org/downloads/windows/) 安装，保留安装器中的 Tcl/Tk 组件。

首次安装依赖需要联网。使用新的识别模型或离线翻译方向时，也需要首次下载模型；对应模型完整缓存后，可以离线运行。

### 2. 下载并启动

可下载 [源码 ZIP](https://github.com/AtomBoyC/ListenTo-Subtitle-Editor/archive/refs/heads/master.zip)，解压后双击 **`Start.cmd`**。请保留整个项目文件夹。

也可以使用 Git：

```powershell
git clone https://github.com/AtomBoyC/ListenTo-Subtitle-Editor.git
cd ListenTo-Subtitle-Editor
.\Start.cmd
```

启动器会创建独立的 `.venv` 环境、检查依赖，然后打开界面。无需手动安装 Whisper 模型。

### 3. 选择操作

**只转换格式**

1. 输入类型选择“字幕文件 · 翻译与格式转换”，打开字幕文件。
2. 目标语言保持“不翻译 · 原语言”。
3. 选择输出格式和保存目录，点击“开始转换字幕”。

**翻译字幕**

1. 打开字幕文件，在“字幕文件设置”中选择具体的原字幕语言。
2. 在“字幕翻译”中选择目标语言、离线或在线方式。
3. 需要双语时，勾选“原文在上，译文在下”。
4. 选择输出格式和保存目录，开始翻译。

**从视频或音频生成字幕**

切换到“视频 / 音频 · 自动生成字幕”，选择媒体文件、语音语言和识别模型。语音语言可自动检测；已有字幕的翻译需要明确选择原语言。默认识别模型为 `base`，默认使用 CPU + int8。

GUI 默认把结果保存在项目目录下的 `字幕结果/`，也可以自行更改。

## 翻译方式

| 方式 | 设置与运行 | 数据发送 |
| --- | --- | --- |
| 本地离线翻译 | 使用 Argos 模型，由 CTranslate2 在本机运行；首次使用新方向需下载模型 | 文字留在本机 |
| 在线 AI 翻译 | 配置兼容 OpenAI Chat Completions 的服务地址、模型名和 API 密钥 | 仅发送待翻译文字片段 |

界面和离线翻译支持 **中文、英语、日语、韩语、法语、德语、西班牙语**。部分离线组合通过英语中转，并需缓存两个方向的模型。人名、数字、术语和中转译文需要人工校对。

例如日语译中文使用 **日语 → 英语 → 中文**，首次需要日译英、英译中两个模型。下载提示中的“日语 → 英语”表示其中一步，最终输出仍是中文。开始时日志会记录所选最终目标和完整路线，界面的语言选项不会因鼠标滚轮而切换。离线加载兼容使用 TXT 词表的旧版日语模型。

在线模式点击“API 设置…”，按服务商提供的信息填写基础地址（通常包含 `/v1`）、模型名称和密钥。服务需要按提示返回完整的 JSON 译文，可能按使用量收费。远程地址必须使用 HTTPS；本机 `localhost` 或回环地址允许 HTTP。

界面中的 API 设置只保存在本次运行的内存中，不写入配置文件。字幕原文件和音视频不上传。命令行从 `SUBTITLE_API_KEY` 环境变量读取密钥。

## 格式、编码与兼容限制

- **时间轴与顺序**：翻译不重新识别语音，也不合并普通文字条目；保留原顺序和起止时间。重叠或倒序字幕不会被自动排序。同格式保留原编号，跨格式按原条目顺序生成编号。
- **样式**：跨格式保留可转换的基础粗体、斜体和下划线。字体、定位、动画和格式专用信息无法保证完整迁移；兼容提示会显示在日志中。ASS 与 SSA 互转尽量保留可转换样式与事件字段，非标准字段可能使用默认样式。
- **特殊事件**：ASS/SSA 的注释、绘图和卡拉 OK 行保留原文，不送入翻译模型。转换到 SRT/VTT/TXT 时跳过注释和纯绘图，混合绘图只导出可见文字；卡拉 OK 会失去逐字时间和效果。跳过的事件会明确提示。
- **时间精度**：ASS/SSA 为 10 毫秒粒度，跨格式导出会舍入，过短文字条目至少保留 10 毫秒。本工具跨格式导出 ASS/SSA 支持至 `9:59:59.99`，SRT 支持至 `99:59:59.999`；超出时请保留 VTT。
- **编码**：自动模式接受 UTF-8（有无 BOM）和带 BOM 的 UTF-16。旧中文字幕可明确选择 GB18030，无 BOM 的 UTF-16 可选择 LE/BE。解码失败会停止，不会悄悄替换乱码。所有输出使用 UTF-8；换行会规范化。
- **取消**：在当前推理片段、翻译条目或网络请求结束后生效。

## 命令行

以下 PowerShell 示例使用启动器默认创建的 `.venv`。如果配置了其他 Python 环境，请替换解释器路径。命令行未指定 `--output-dir` 时，结果默认保存在输入文件所在目录。

### 仅转换格式

```powershell
.\.venv\Scripts\python.exe .\subtitle_files.py ".\示例\英文样式字幕.ass" --format srt vtt --output-dir ".\字幕结果"
```

### 离线英译中，导出双语 SRT 和 ASS

```powershell
.\.venv\Scripts\python.exe .\subtitle_files.py ".\示例\英文字幕.srt" --source-language en --target-language zh --translation-mode offline --bilingual --format srt ass --output-dir ".\字幕结果"
```

### 在线翻译

先在当前进程配置 `SUBTITLE_API_KEY` 环境变量，再运行以下命令；把地址和模型名占位符替换为服务商的实际设置。

```powershell
.\.venv\Scripts\python.exe .\subtitle_files.py "input.srt" --source-language en --target-language zh --translation-mode online --api-base-url "https://YOUR_API_HOST/v1" --api-model "YOUR_MODEL" --format srt --output-dir ".\字幕结果"
```

### 离线日译中，保留 ASS 样式并导出 SRT

```powershell
.\.venv\Scripts\python.exe .\subtitle_files.py ".\示例\日文样式字幕.ass" --source-language ja --target-language zh --translation-mode offline --format ass srt --output-dir ".\字幕结果"
```

### 从视频生成字幕

```powershell
.\.venv\Scripts\python.exe .\subtitle_core.py "video.mp4" --language zh --model base --format srt vtt txt --output-dir ".\字幕结果"
```

查看全部参数：

```powershell
.\.venv\Scripts\python.exe .\subtitle_files.py --help
.\.venv\Scripts\python.exe .\subtitle_core.py --help
```

## 文件与模型缓存

默认目录如下，不随源码上传模型或运行环境：

```text
ListenTo-Subtitle-Editor/
├── Start.cmd                # Windows 启动入口
├── app.py                   # 中文桌面界面
├── subtitle_files.py        # 已有字幕的翻译与转换
├── subtitle_core.py         # 视频/音频识别
├── translation_core.py      # 翻译接口与在线请求
├── offline_translate.py     # 本地翻译模型下载与推理
├── ass_format_convert.py    # ASS/SSA 样式转换
├── bootstrap.py             # 独立环境安装与启动
├── runtime_config.py        # 本机路径配置
├── requirements.txt
├── 示例/
├── tests/
├── .venv/                   # 首次启动创建，Git 忽略
├── .cache/
│   ├── models/              # Whisper 模型
│   └── translation/         # 离线翻译模型
└── 字幕结果/                # GUI 默认输出目录
```

可通过以下环境变量复用已有缓存或 Python 环境：

| 环境变量 | 用途 |
| --- | --- |
| `SUBTITLE_PYTHON_ENV` | Python 虚拟环境目录 |
| `SUBTITLE_MODEL_CACHE` | Whisper 模型缓存目录 |
| `SUBTITLE_TRANSLATION_CACHE` | 离线翻译模型缓存目录 |

也可以在项目目录自行创建 `local_paths.json`，配置 `python_environment`、`whisper_models`、`translation_models` 三个路径。环境变量优先；该文件被 Git 忽略，只保存路径，不用于保存 API 密钥。

输出文件名包含语言，例如 `课程.zh.srt` 或双语的 `课程.en-zh.srt`。未指定语言的纯转换使用 `课程.converted.srt`；重复处理会生成 `.2` 等序号。

## 示例与验证

- [英文 SRT 示例](示例/英文字幕.srt)
- [带样式的 ASS 示例](示例/英文样式字幕.ass)
- [日语 ASS 示例](示例/日文样式字幕.ass)
- [实际离线日译中的 SRT](示例/翻译结果/日文样式字幕.zh.srt)
- [实际离线翻译生成的双语 SRT](示例/翻译结果/英文字幕.en-zh.2.srt)

项目的自动化测试覆盖字幕解析、16 种格式转换路径、标签与时间轴保护、严格编码、文件防覆盖、取消、离线新旧模型布局、语言选择保护以及在线接口校验。在线测试使用本机模拟服务，不调用收费 API；另外已验证实际离线英译中、日译中和 GUI 启动。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\Start.cmd --self-test
```

## 使用的项目

- [OpenAI Whisper](https://github.com/openai/whisper)：语音识别模型。
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)：Whisper 的 CTranslate2 实现。
- [Argos Translate](https://github.com/argosopentech/argos-translate) 与 [官方模型索引](https://github.com/argosopentech/argospm-index)：离线翻译模型。
- [pysubs2](https://github.com/tkarabela/pysubs2)：ASS/SSA 格式与样式转换。

模型与依赖的使用条件以各上游项目为准。
