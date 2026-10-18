"""Run with python -m gaybar_tools from the project directory."""

import argparse
import importlib.util
import shutil
import subprocess
import sys
import threading
from dataclasses import replace
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import AppSettings
from .paths import AppPaths


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def demo_server():
    directory = Path(__file__).resolve().parent / "demo"
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(directory)))
    threading.Thread(target=server.serve_forever, name="local-demo", daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/chapter.html?chapterId=1&courseId=demo"


def doctor(paths):
    healthy = True
    print(f"Python: {sys.version.split()[0]}")
    print(f"项目目录: {paths.root}")
    print(f"运行数据: {paths.runtime}")
    for module in ("tkinter", "playwright"):
        present = importlib.util.find_spec(module) is not None
        print(f"{module}: {'可用' if present else '未安装'}")
        healthy &= present
    if sys.platform.startswith("linux"):
        isolation = shutil.which("bwrap")
        print(f"Linux 写入隔离: {isolation or '缺少 bubblewrap (bwrap)'}")
        healthy &= bool(isolation)
    try:
        from playwright.sync_api import sync_playwright
        settings = AppSettings.load(paths.runtime / "settings.json")
        # Discovery should not launch the browser or contact a remote service.
        from .browser import discover_browser
        with sync_playwright() as playwright:
            executable = discover_browser(settings.browser_path, paths)
            if not executable and Path(playwright.chromium.executable_path).is_file():
                executable = playwright.chromium.executable_path
            print(f"浏览器: {executable or '未发现，请运行 --install-browser'}")
            healthy &= bool(executable)
    except ImportError:
        print("浏览器检查: 缺少 Playwright 或浏览器模块")
        healthy = False
    except Exception as exc:
        print(f"浏览器检查: {type(exc).__name__}")
        healthy = False
    print(f"可选 OCR: {shutil.which('tesseract') or '未安装（可使用视觉模型）'}")
    return 0 if healthy else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="GayBarTools · 学习通章节助手")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--doctor", action="store_true", help="检查依赖和隔离路径")
    group.add_argument("--install-browser", action="store_true", help="下载 Chromium 到项目 .runtime/browsers")
    group.add_argument("--demo", action="store_true", help="本地演示，无需登录或 API")
    group.add_argument("--demo-headless", action="store_true", help="运行本地演示完整流程后退出")
    args = parser.parse_args(argv)
    paths = AppPaths.for_root().prepare()
    paths.activate()
    if args.doctor:
        return doctor(paths)
    if args.install_browser:
        return subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"], env=paths.environment(), cwd=paths.root)
    if importlib.util.find_spec("playwright") is None:
        print("缺少 Playwright。请先运行 python3 scripts/bootstrap.py。", file=sys.stderr)
        return 2
    try:
        settings = AppSettings.load(paths.runtime / "settings.json")
    except (ValueError, TypeError, OSError) as exc:
        print(f"无法读取 .runtime/settings.json：{exc}", file=sys.stderr)
        return 2
    server = None
    url = None
    if args.demo or args.demo_headless:
        server, url = demo_server()
    try:
        if args.demo_headless:
            from .browser import BrowserSession
            from .worker import DemoSolver
            from .workflow import Workflow
            import tempfile
            # Never replace the user's saved login session during a demo.
            with tempfile.TemporaryDirectory(prefix="demo-", dir=paths.tmp) as folder:
                demo_paths = replace(AppPaths.for_root(Path(folder)), browsers=paths.browsers).prepare()
                settings = replace(settings, max_chapters=4)
                stop = threading.Event()
                browser = BrowserSession(settings, demo_paths, print, stop)
                try:
                    browser.launch(url, headless=True)
                    count = Workflow(browser, DemoSolver(browser, url), settings, stop, print, print).run()
                    if count != 4:
                        raise RuntimeError(f"演示应完成 4 个学习页面，实际完成 {count} 个。")
                    print("本地演示完成：视频 → 检测 → 文档 → 检测。")
                    return 0
                finally:
                    browser.close()
        import tkinter as tk
        from .ui import Application
        root = tk.Tk()
        Application(root, paths, settings, url)
        root.mainloop()
        return 0
    except Exception as exc:
        message = str(exc)
        if settings.api_key:
            message = message.replace(settings.api_key, "[已隐藏]")
        print(f"启动或运行失败：{message}", file=sys.stderr)
        return 1
    finally:
        if server:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
