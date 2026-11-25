"""Native Desktop Application Launcher for ListenTo Studio v2.0 (macOS .app & Windows .exe)."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

# In PyInstaller --windowed mode on Windows, sys.stdout / sys.stderr are None,
# which would otherwise break uvicorn's logging formatter isatty() check.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# Activate desktop runtime paths before importing server / config_store
os.environ.setdefault("LISTENTO_DESKTOP", "1")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(s.getsockname()[1])


def _wait_for_server(url: str, timeout: float = 18.0) -> bool:
    deadline = time.monotonic() + timeout
    health_url = f"{url}/api/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.08)
    return False


def _run_smoke_test(base_url: str, marker_file: str = "") -> int:
    """Verify packaged executable end-to-end: webview import, health, desktop runtime meta, sample parse, and export."""
    import webview  # noqa: F401 — verify native pywebview backend is bundled cleanly

    with urllib.request.urlopen(f"{base_url}/api/meta", timeout=5.0) as resp:
        meta = json.loads(resp.read().decode("utf-8"))
        assert meta.get("runtime") == "desktop", f"Expected desktop runtime, got {meta.get('runtime')}"
        assert len(meta.get("presets", [])) >= 5, "Provider presets missing"

    with urllib.request.urlopen(f"{base_url}/api/samples/anime_ja_ass", timeout=5.0) as resp:
        doc = json.loads(resp.read().decode("utf-8"))
        assert doc.get("source_language") == "ja", "Sample language mismatch"
        doc["cues"][1]["target_text"] = "第三音乐教室·放学后"

    export_req = urllib.request.Request(
        f"{base_url}/api/subtitles/export",
        data=json.dumps(
            {
                "document": doc,
                "formats": ["srt", "ass"],
                "track_mode": "bilingual_tgt_top",
                "ass_secondary_scale": 72,
                "fallback_to_source": True,
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(export_req, timeout=5.0) as resp:
        exported = json.loads(resp.read().decode("utf-8"))
        assert len(exported.get("files", [])) == 2, "Export file count mismatch"

    with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as resp:
        html = resp.read().decode("utf-8")
        assert "听见 Studio" in html, "Frontend index.html failed to render"

    if marker_file:
        Path(marker_file).write_text("DESKTOP_SMOKE_TEST_OK\n", encoding="utf-8")
    print("DESKTOP_SMOKE_TEST_OK", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ListenTo Studio Desktop Application")
    parser.add_argument("--browser", action="store_true", help="Open in system default browser instead of native window")
    parser.add_argument("--smoke-test", action="store_true", help="Run packaged binary self-test and exit")
    parser.add_argument("--smoke-test-file", default="", help="Optional marker file path for windowed smoke test")
    parser.add_argument("--port", type=int, default=0, help="Optional fixed localhost port")
    args = parser.parse_args()

    temp_ctx = None
    if args.smoke_test:
        temp_ctx = tempfile.TemporaryDirectory(prefix="listento-smoke-")
        os.environ["LISTENTO_DATA_DIR"] = str(Path(temp_ctx.name) / "data")
        os.environ["LISTENTO_EXPORTS_DIR"] = str(Path(temp_ctx.name) / "exports")

    import uvicorn
    from server import app

    port = args.port if args.port > 0 else _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
        log_config=None,
    )
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, name="listento-uvicorn", daemon=True)
    server_thread.start()

    if not _wait_for_server(base_url, timeout=20.0):
        if temp_ctx:
            temp_ctx.cleanup()
        return 1

    if args.smoke_test:
        try:
            return _run_smoke_test(base_url, args.smoke_test_file)
        finally:
            server.should_exit = True
            server_thread.join(timeout=3.0)
            if temp_ctx:
                temp_ctx.cleanup()

    if not args.browser:
        try:
            import webview  # type: ignore

            window = webview.create_window(
                title="听见 Studio 2.0 · AI 双语字幕精修工作台",
                url=base_url,
                width=1360,
                height=860,
                min_size=(980, 660),
                text_select=True,
            )

            def _on_closed() -> None:
                server.should_exit = True

            window.events.closed += _on_closed
            webview.start(debug=False)
            server.should_exit = True
            return 0
        except Exception:
            pass

    webbrowser.open(base_url)
    try:
        while server_thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        server.should_exit = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
