"""Own all synchronous Playwright operations on one background thread."""

import queue
import threading
from dataclasses import replace
from datetime import datetime
from urllib.parse import urlsplit

from .config import AppSettings
from .models import Answer
from .paths import AppPaths
from .workflow import Workflow, WorkflowStopped


class DemoSolver:
    """Deterministic answers exclusively for the bundled local demonstration."""

    def __init__(self, browser, demo_url: str):
        self.browser, self.origin = browser, urlsplit(demo_url).netloc

    def solve(self, questions, screenshot=None):
        current = urlsplit(self.browser.current_url)
        if current.hostname != "127.0.0.1" or current.netloc != self.origin:
            raise ValueError("演示答案仅适用于本次启动的本地演示页面。")
        expected = {"single": ("B",), "multiple": ("A", "B"),
                    "boolean": ("A",), "judge": ("A",)}
        return [Answer(q.id, expected[q.kind], 1.0, "本地演示固定答案") for q in questions]


class Worker(threading.Thread):
    def __init__(self, paths: AppPaths, events: queue.Queue, demo_url: str | None = None):
        super().__init__(name="browser-worker", daemon=True)
        self.paths, self.events, self.demo_url = paths, events, demo_url
        self.commands: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.shutdown_event = threading.Event()
        self.browser = None
        self._last_ready = None
        self._last_url = ""
        self._last_detection_error = ""
        self.log_file = paths.logs / (datetime.now().strftime("session-%Y%m%d-%H%M%S") + ".log")

    def emit(self, kind, value):
        self.events.put((kind, value))

    def log(self, message):
        line = datetime.now().strftime("%H:%M:%S") + "  " + str(message)
        self.emit("log", line)
        try:
            with self.log_file.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    def command(self, name, settings: AppSettings | None = None):
        if name == "shutdown":
            self.shutdown_event.set()
            self.stop_event.set()
        else:
            if self.shutdown_event.is_set():
                return
            # Clear a previous operation's stop before enqueueing. A stop that
            # arrives after this point must survive until the command starts.
            self.stop_event.clear()
        self.commands.put((name, settings))

    def stop(self):
        self.stop_event.set()

    def update_ready(self):
        try:
            ready = bool(self.browser and self.browser.poll_ready())
            self._last_detection_error = ""
        except Exception as exc:
            ready = False
            message = str(exc)
            if self.browser:
                key = self.browser.settings.api_key
                if key:
                    message = message.replace(key, "[已隐藏]")
            if message != self._last_detection_error and not self.stop_event.is_set():
                self.log("页面检测提示：" + message)
            self._last_detection_error = message
        url = self.browser.current_url if self.browser else ""
        if (ready, url) != (self._last_ready, self._last_url):
            self._last_ready, self._last_url = ready, url
            self.emit("ready", ready)
            self.emit("url", url)

    def run(self):
        from .browser import BrowserSession, StopRequested
        from .llm import QuizSolver, SolverCancelled

        try:
            while True:
                try:
                    name, settings = self.commands.get(timeout=0.6)
                except queue.Empty:
                    try:
                        self.update_ready()
                    except Exception:
                        if self._last_ready:
                            self._last_ready = False
                            self.emit("ready", False)
                    continue
                if name == "shutdown" or self.shutdown_event.is_set():
                    break
                self.emit("busy", True)
                try:
                    if self.stop_event.is_set():
                        raise WorkflowStopped("已停止。")
                    if name == "launch":
                        if self.browser:
                            self.browser.close()
                        browser_paths = replace(self.paths, profile=self.paths.runtime / "demo-browser-profile") if self.demo_url else self.paths
                        self.browser = BrowserSession(settings, browser_paths, self.log, self.stop_event)
                        self.browser.launch(self.demo_url or "https://i.chaoxing.com/")
                        self.emit("state", "请登录并进入任意课程章节" if not self.demo_url else "本地演示已就绪")
                        self.log("浏览器已打开，使用项目内独立会话。")
                    elif name == "test":
                        self.emit("state", "正在测试 API")
                        self.log(QuizSolver(settings, self.log, self.stop_event).test_connection())
                        self.emit("state", "API 连接成功")
                    elif name == "start":
                        if not self.browser:
                            raise ValueError("请先打开浏览器。")
                        self.browser.settings = settings
                        solver = DemoSolver(self.browser, self.demo_url) if self.demo_url else QuizSolver(settings, self.log, self.stop_event)
                        Workflow(self.browser, solver, settings, self.stop_event, self.log,
                                 lambda state: self.emit("state", state)).run()
                except (WorkflowStopped, StopRequested, SolverCancelled):
                    self.log("任务已停止，浏览器保留在当前页面。")
                    self.emit("state", "已停止")
                except Exception as exc:
                    message = str(exc)
                    if settings and settings.api_key:
                        message = message.replace(settings.api_key, "[已隐藏]")
                    self.log("处理停止：" + message)
                    self.emit("state", "需要处理：" + message)
                    if self.browser and name != "test" and not self.stop_event.is_set():
                        try:
                            path = self.browser.capture_diagnostic()
                            if path:
                                self.log(f"诊断截图：{path}")
                        except Exception:
                            pass
                finally:
                    if not self.shutdown_event.is_set():
                        self.stop_event.clear()
                    self.emit("busy", False)
                    try:
                        self.update_ready()
                    except Exception:
                        self.emit("ready", False)
        finally:
            if self.browser:
                try:
                    self.browser.close()
                except Exception:
                    self.log("独立浏览器关闭时出现异常，请检查其窗口是否已退出。")

