"""Create/reuse an isolated Python environment, then launch the subtitle GUI."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import venv

from runtime_config import environment_path as configured_environment_path


TOOL_DIR = Path(__file__).resolve().parent
REQUIREMENTS = TOOL_DIR / "requirements.txt"
EXPECTED_FASTER_WHISPER = "1.2.1"


def python_in(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def child_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["HF_HUB_DISABLE_XET"] = "1"
    env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    return env


def run(arguments: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        arguments,
        cwd=str(TOOL_DIR),
        env=child_environment(),
        check=False,
        capture_output=capture,
        text=capture,
        encoding="utf-8" if capture else None,
        errors="replace" if capture else None,
    )


def environment_path() -> Path:
    return configured_environment_path()


def main() -> int:
    if sys.version_info < (3, 9):
        raise RuntimeError("需要 Python 3.9 或更新版本。")
    if not REQUIREMENTS.is_file() or not (TOOL_DIR / "app.py").is_file():
        raise RuntimeError("工具文件不完整，请保留整个字幕生成器文件夹后再运行。")

    env_dir = environment_path()
    interpreter = python_in(env_dir)
    if not interpreter.is_file():
        print("首次启动：正在创建独立的 Python 环境……", flush=True)
        venv.EnvBuilder(with_pip=True).create(str(env_dir))

    print(f"运行环境：{env_dir}", flush=True)
    check = run(
        [
            str(interpreter),
            "-c",
            "import sys, tkinter; "
            "assert sys.version_info >= (3, 9), 'Python 3.9+ required'; "
            "assert sys.prefix != sys.base_prefix, 'Isolated environment required'",
        ],
        capture=True,
    )
    if check.returncode:
        raise RuntimeError(
            "独立运行环境不可用，或 Python 缺少 Tkinter。请使用包含 Tcl/Tk 的 Python。\n"
            + (check.stderr.strip() or check.stdout.strip())
        )

    dependency_check = run(
        [
            str(interpreter),
            "-c",
            "from importlib.metadata import version; import faster_whisper; "
            f"assert version('faster-whisper') == {EXPECTED_FASTER_WHISPER!r}; "
            "assert version('av') == '15.1.0'; "
            "assert version('sentencepiece') == '0.2.2'; "
            "assert version('pysubs2') == '1.8.0'",
        ],
        capture=True,
    )
    if dependency_check.returncode:
        print("正在安装字幕处理依赖；首次启动需要联网，可能需要几分钟……", flush=True)
        install = run(
            [
                str(interpreter),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--require-virtualenv",
                "-r",
                str(REQUIREMENTS),
            ]
        )
        if install.returncode:
            raise RuntimeError(
                "依赖安装失败。请检查网络连接及上方错误信息，然后重新运行 Start.cmd。"
            )

    print("正在打开字幕工具……纯格式转换无需下载模型。", flush=True)
    app = run([str(interpreter), str(TOOL_DIR / "app.py"), *sys.argv[1:]])
    if app.returncode:
        raise RuntimeError(f"字幕生成器退出，状态码：{app.returncode}。请查看上方错误信息。")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已停止启动。", flush=True)
        raise SystemExit(130)
    except Exception as exc:
        print(f"\n启动失败：{exc}", file=sys.stderr, flush=True)
        try:
            input("\n按 Enter 关闭此窗口……")
        except (EOFError, KeyboardInterrupt):
            pass
        raise SystemExit(1)
