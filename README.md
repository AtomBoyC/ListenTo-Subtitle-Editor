# 听见 Studio 2.5 · AI 双语字幕精修与语音识别工作台

> 由原 Windows `Tkinter + Argos` 批处理字幕工具全面重构升级而来，融合了 **VideoLingo / llm-subtrans** 的静音感知动态分批与上下文反思精翻管线、**SenseVoice / Whisper** 音视频语音识别、**SubPlayer / Subtitle Edit** 的交互声波打轴条与时间轴体检，以及 **SmartSub** 的多集队列批量精翻与一键打包体验。
>
> 📦 **旧版归档说明**：原 v1.0 版本（Windows `Tkinter` 桌面版 + Argos 离线模型）已完整备份保存在 [`legacy-v1`](https://github.com/AtomBoyC/ListenTo-Subtitle-Editor/tree/legacy-v1) 分支。

---

## 📥 三种使用方式（桌面端直开 / 服务器 Docker / 本机源码）

| 场景 | 适用人群 | 获取与启动方式 |
| :--- | :--- | :--- |
| **💻 Windows / macOS 桌面应用** | 普通用户，不想安装 Python 或敲命令 | 前往 **[GitHub Releases](https://github.com/AtomBoyC/ListenTo-Subtitle-Editor/releases/latest)** 下载 `ListenTo-Studio.exe`（Windows）或 `.dmg`（macOS Apple Silicon / Intel），**双击直接打开即用** |
| **🐳 服务器 / NAS Docker Compose** | 云服务器、群晖、飞牛 OS、1Panel、团队共享 | 单文件一键部署：`docker compose up -d`（官方多架构镜像 `atomboyc/listento-subtitle-editor:latest`） |
| **🛠️ 源码脚本启动** | 开发者本地调试与二次开发 | macOS / Linux 执行 `./start.sh`，Windows 双击 `Start.cmd` |

📖 **完整图文级配置、Nginx 反代与排障说明见：[docs/部署与使用指南.md](docs/部署与使用指南.md)**

---

## 相比 v1.0 解决了哪些核心痛点？

| 维度 | 旧版 v1.0 (`Tkinter + Argos`) | 重构版 Studio v2.5 (`桌面原生窗口 + Web/Docker`) |
| --- | --- | --- |
| **翻译准确度** | Argos 小模型逐词硬翻、日译中需经英语二次中转，遇换行 `\n` 或 `<i>` 标签直接把单句切碎孤立翻译（如把 `subtitle editor` 译为「收看字幕编辑」） | **静音间隔与标点感知动态分批 + 跨批剧情滚动记忆 + 强制术语表 +「直译→审校反思→地道意译」管线**，内置 API 429/5xx 指数退避自动重试 |
| **交互与校对** | 盲盒批处理，无法预览字幕内容、无法修改单句错译、API 密钥重启即丢 | **千行渐进式高性能双语编辑器**：支持手动输入/打轴全流程 `Cmd/Ctrl+Z` 撤销重做、单句 AI 润色/缩字候选替换、字幕质量体检一键修复重叠与标点 |
| **声波打轴与监看** | 无任何画面或时间轴对照 | **16:9 实时字幕监看台 + Web Audio 交互波形打轴条**：本地拖入音视频零上传同步播放、真实音轨声波渲染、鼠标直接拖拽色块平移或修剪起止时间 |
| **批量与跨平台** | 仅支持安装了 Python+Tk 的 Windows 电脑 | 支持 **多集字幕队列批量精翻 + 一键打包导出 `.zip`**；提供 **Windows `.exe` 单文件版**、**macOS `.app/.dmg` 原生应用** 与 **`amd64 + arm64` Docker Compose 部署** |
| **界面与操作** | Tk 原生控件，按钮平铺 | **专业剪辑软件风界面**（参考 Descript / DaVinci Resolve / Linear）：菜单栏收纳低频功能、`⌘/Ctrl+K` 命令面板、字幕行右键菜单、底部全宽时间轴与状态栏，深浅双主题 |

---

## 快速开始

### 1. Windows & macOS 桌面客户端（零环境依赖）

前往 **[Releases 下载最新版](https://github.com/AtomBoyC/ListenTo-Subtitle-Editor/releases/latest)**：

- **Windows 10 / 11**：下载 `ListenTo-Studio.exe`，双击直接运行（导出字幕默认保存在 `Downloads\ListenTo-Subtitles`）。
- **macOS (Apple Silicon / Intel)**：下载对应架构的 `.dmg`，将 `ListenTo Studio.app` 拖入 `Applications` 运行。
  > *macOS 首次打开若提示「无法验证开发者」，可在「系统设置 → 隐私与安全性」点击「仍要打开」，或执行 `xattr -cr "/Applications/ListenTo Studio.app"`。*

### 2. 服务器 / NAS Docker Compose 部署（无需克隆源码）

```bash
mkdir -p listento-studio && cd listento-studio
curl -fsSL https://raw.githubusercontent.com/AtomBoyC/ListenTo-Subtitle-Editor/master/docker-compose.yml -o docker-compose.yml
docker compose up -d
```

或者使用单行 `docker run`：

```bash
docker run -d \
  --name listento-studio \
  -p 8765:8765 \
  -v $(pwd)/data:/data \
  -v $(pwd)/exports:/exports \
  --add-host=host.docker.internal:host-gateway \
  --restart unless-stopped \
  atomboyc/listento-subtitle-editor:latest
```

启动后浏览器访问 **`http://服务器IP:8765`**，API Key 与字幕项目进度会自动持久化保存在 `./data` 目录中。

### 3. 本机源码运行与桌面端自行打包

```bash
# 直接启动本地服务 (macOS / Linux)
chmod +x start.sh && ./start.sh

# 运行全量单元测试与功能集成测试
./.venv/bin/python -m unittest discover -s tests -v

# 本地打包桌面端安装包 (.app / .dmg / .exe)
./.venv/bin/pip install -r requirements-desktop.txt
./.venv/bin/python packaging/build_desktop.py
```

---

## 项目目录结构

```text
ListenTo-Subtitle-Editor/
├── server.py                      # FastAPI 核心服务、流式 SSE 翻译、ASR 识别与字幕体检接口
├── desktop.py                     # Windows / macOS 原生桌面窗口启动入口 (pywebview)
├── core/
│   ├── models.py                  # 字幕文档、术语表、ASR 配置与样式预设数据模型
│   ├── parser.py                  # 多编码解析、单条智能拆分、字幕体检修复与 4 款影院 ASS 导出
│   ├── translator.py              # 13 家平台预设 + /models 实时发现 + 滑动窗口反思精翻管线
│   ├── asr.py                     # 双引擎音视频转字幕（SenseVoice / Groq Whisper / 本地 Faster-Whisper）
│   └── config_store.py            # 跨平台配置与历史工程持久化（适配桌面端/Docker/本地开发）
├── static/                        # 现代双语字幕剪辑台前端 (HTML / CSS / JS)
├── samples/                       # 内置中英/中日/双语分轨体验示例字幕
├── packaging/                     # 桌面图标生成与 PyInstaller 跨平台自动打包脚本
├── docs/部署与使用指南.md          # 桌面端、服务器 Compose 与 Nginx 反代详细文档
├── Dockerfile                     # 多架构 (amd64/arm64) 生产容器构建文件
├── docker-compose.yml             # 服务器单文件一键部署编排配置
└── .github/workflows/             # Docker Hub 自动发布 + Windows/macOS 桌面安装包自动构建流水线
```
