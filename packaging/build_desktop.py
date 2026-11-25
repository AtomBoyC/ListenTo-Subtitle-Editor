"""Cross-platform PyInstaller Desktop Packaging Script for macOS (.app / .dmg / .zip) & Windows (.exe / .zip)."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING_DIR = ROOT / "packaging"
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"


def run(cmd: list[str]) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=str(ROOT))


def zip_directory(src_dir: Path, zip_path: Path, arc_prefix: str = "") -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(src_dir.rglob("*")):
            rel = item.relative_to(src_dir)
            arcname = f"{arc_prefix}/{rel}" if arc_prefix else str(rel)
            if item.is_symlink():
                # Preserve macOS framework symlinks inside .app bundles
                info = zipfile.ZipInfo(arcname)
                info.create_system = 3
                info.external_attr = 0o120755 << 16
                zf.writestr(info, os.readlink(item))
            elif item.is_file():
                zf.write(item, arcname)


def main() -> int:
    # 1. Generate icons if missing
    run([sys.executable, str(PACKAGING_DIR / "generate_icons.py")])

    for d in (DIST_DIR, BUILD_DIR):
        if d.exists():
            shutil.rmtree(d)

    sep = ";" if sys.platform == "win32" else ":"
    arch = platform.machine().lower()
    if arch in ("amd64", "x86_64"):
        arch_label = "x64"
    elif arch in ("arm64", "aarch64"):
        arch_label = "arm64"
    else:
        arch_label = arch

    common_args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        f"--add-data=static{sep}static",
        f"--add-data=samples{sep}samples",
        "--hidden-import=uvicorn.logging",
        "--hidden-import=uvicorn.loops",
        "--hidden-import=uvicorn.loops.auto",
        "--hidden-import=uvicorn.protocols",
        "--hidden-import=uvicorn.protocols.http",
        "--hidden-import=uvicorn.protocols.http.auto",
        "--hidden-import=uvicorn.protocols.websockets",
        "--hidden-import=uvicorn.protocols.websockets.auto",
        "--hidden-import=uvicorn.lifespan",
        "--hidden-import=uvicorn.lifespan.on",
        "--hidden-import=multipart",
        "--hidden-import=python_multipart",
        "--collect-submodules=pysubs2",
        "--collect-all=webview",
    ]

    if sys.platform == "darwin":
        app_name = "ListenTo Studio"
        cmd = [
            *common_args,
            "--name",
            app_name,
            "--icon",
            str(PACKAGING_DIR / "icon.icns"),
            "--osx-bundle-identifier",
            "com.atomboyc.listentostudio",
            str(ROOT / "desktop.py"),
        ]
        run(cmd)

        app_bundle = DIST_DIR / f"{app_name}.app"
        binary = app_bundle / "Contents" / "MacOS" / app_name

        # Ad-hoc sign so macOS Gatekeeper handles local/downloaded bundle cleanly
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app_bundle)], check=False)

        # Smoke test compiled .app binary directly!
        print("Running packaged macOS .app smoke test...", flush=True)
        marker = Path(tempfile.gettempdir()) / "listento_macos_smoke.txt"
        marker.unlink(missing_ok=True)
        run([str(binary), "--smoke-test", "--smoke-test-file", str(marker)])
        assert marker.is_file() and "DESKTOP_SMOKE_TEST_OK" in marker.read_text(encoding="utf-8")
        marker.unlink(missing_ok=True)

        # Create symlinked .zip (preserving Framework symlinks via ditto on macOS)
        zip_out = DIST_DIR / f"ListenTo-Studio-macOS-{arch_label}.zip"
        run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app_bundle), str(zip_out)])

        # Also create drag-to-Applications .dmg installer!
        dmg_staging = BUILD_DIR / "dmg_stage"
        dmg_staging.mkdir(parents=True, exist_ok=True)
        run(["ditto", str(app_bundle), str(dmg_staging / f"{app_name}.app")])
        os.symlink("/Applications", dmg_staging / "Applications")
        dmg_out = DIST_DIR / f"ListenTo-Studio-macOS-{arch_label}.dmg"
        run(
            [
                "hdiutil",
                "create",
                "-volname",
                "ListenTo Studio 2.5",
                "-srcfolder",
                str(dmg_staging),
                "-ov",
                "-format",
                "UDZO",
                str(dmg_out),
            ]
        )
        print(f"\nmacOS Desktop Artifacts Ready:\n  App: {app_bundle}\n  DMG: {dmg_out}\n  ZIP: {zip_out}")

    elif sys.platform == "win32":
        exe_name = "ListenTo-Studio"
        cmd = [
            *common_args,
            "--onefile",
            "--name",
            exe_name,
            "--icon",
            str(PACKAGING_DIR / "icon.ico"),
            str(ROOT / "desktop.py"),
        ]
        run(cmd)

        exe_path = DIST_DIR / f"{exe_name}.exe"
        print("Running packaged Windows .exe smoke test...", flush=True)
        marker = Path(tempfile.gettempdir()) / "listento_win_smoke.txt"
        marker.unlink(missing_ok=True)
        run([str(exe_path), "--smoke-test", "--smoke-test-file", str(marker)])
        assert marker.is_file() and "DESKTOP_SMOKE_TEST_OK" in marker.read_text(encoding="utf-8")
        marker.unlink(missing_ok=True)

        zip_out = DIST_DIR / f"ListenTo-Studio-Windows-{arch_label}.zip"
        with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(exe_path, f"{exe_name}.exe")
        print(f"\nWindows Desktop Artifacts Ready:\n  EXE: {exe_path}\n  ZIP: {zip_out}")

    else:
        exe_name = "listento-studio"
        cmd = [*common_args, "--onefile", "--name", exe_name, str(ROOT / "desktop.py")]
        run(cmd)
        exe_path = DIST_DIR / exe_name
        run([str(exe_path), "--smoke-test"])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
