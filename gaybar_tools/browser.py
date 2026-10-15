"""Synchronous, conservative Playwright adapter for visible chapter controls.

JavaScript inspects DOM and media state; it never changes media position, speed,
answers, or task flags. A session belongs to the worker thread that launched it.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import math
import os
from pathlib import Path
import random
import re
import shutil
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, urlparse

from .config import AppSettings
from .cookies import CookieStore, CookieStoreError
from .isolation import IsolationError, prepare_browser_executable
from .models import Answer, Option, Question
from .paths import AppPaths


class BrowserError(RuntimeError):
    """The page cannot be operated safely with the supported adapter."""


class StopRequested(BrowserError):
    """The user requested cancellation."""


_CHAPTER_URL = re.compile(r"/(?:mycourse/studentstudy|knowledge/cards|studentstudy)(?:[/?]|$)", re.I)
_QUESTION_BLOCKS = ("[data-question-id]", ".TiMu", ".questionLi", ".question-item",
                    ".question", "fieldset", ".examPaper_question", ".singleQuesId")
_QUESTION_TITLES = ".Zy_TItle, .question-title, .questionTitle, .stem, legend, .mark_name, .qtContent"
_OPTION_SELECTORS = (".Zy_ulTop > li", ".answerList > li", ".answer-list > li",
                     ".options > li", ".option", "[role=radio]", "[role=checkbox]", "label")
_QUIZ_TAB = re.compile(r"^(?:2\s*)?学习检测$")
_NEXT = re.compile(r"^下[一1]节(?:\s*[>›»])?$")
_SUBMIT = re.compile(r"^(?:提交|提交答案|提交作业|交卷)$")
_RESULT = re.compile(r"(?:提交成功|已提交|作业已完成|测验已完成|我的答案\s*[:：]|本次得分|本次成绩|最终成绩)")
_SUBMITTED_RESULT = re.compile(r"提交成功|已提交|作业已完成|测验已完成")


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


class BrowserSession:
    def __init__(self, settings: AppSettings, paths: AppPaths,
                 log: Callable[[str], None], stop_event: threading.Event) -> None:
        self.settings, self.paths = settings, paths
        self.log, self.stop_event = log, stop_event
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._question_controls: dict[str, dict[str, Any]] = {}
        self._questions: dict[str, Question] = {}
        self._owner_thread: int | None = None
        self._mouse = (0.0, 0.0)
        self._unexpected_dialog = ""
        self._submitting = False
        self._last_video_log = 0.0
        self._chapter_page_selected = False
        self._page_order: list[Any] = []
        self._attached_pages: set[Any] = set()
        self._session_tmp: tempfile.TemporaryDirectory[str] | None = None
        self._cookie_store = CookieStore(paths)
        self._cookie_state_ready = False
        self._last_cookie_save = 0.0
        self._cookie_save_failed = False
        self._cookie_saved_logged = False
        self._cookie_restore_failed = False

    @property
    def current_url(self) -> str:
        return self._page.url if self._page and not self._page.is_closed() else ""

    @property
    def page(self) -> Any:
        """Current page, exposed for local diagnostic and integration fixtures."""
        return self._page

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise StopRequested("已停止。")
        if self._owner_thread is not None and self._owner_thread != threading.get_ident():
            raise BrowserError("浏览器必须由启动它的同一个工作线程操作。")

    def _wait(self, seconds: float) -> None:
        # Pump Playwright events, including native dialogs, during every wait.
        until = time.monotonic() + max(0, seconds)
        while time.monotonic() < until:
            self._check_stop()
            self._checkpoint_cookies()
            remaining = min(0.15, until - time.monotonic())
            if remaining <= 0:
                break
            if self._page is not None and not self._page.is_closed():
                self._page.wait_for_timeout(remaining * 1000)
            else:
                self.stop_event.wait(remaining)
        self._check_stop()
        self._checkpoint_cookies()

    def _restore_login_state(self) -> None:
        try:
            cookies = self._cookie_store.load()
            if cookies is not None:
                # The snapshot represents the latest saved jar, including a
                # deliberate empty jar after logout. Do not revive stale
                # profile cookies that are absent from that snapshot.
                original = None
                try:
                    original = self._context.cookies()
                    self._context.clear_cookies()
                    if cookies:
                        self._context.add_cookies(cookies)
                    self.log("已恢复项目内保存的浏览器登录状态；过期登录仍需手动更新。")
                except Exception:
                    # A browser/API mismatch must not destroy either the
                    # native jar or the last valid on-disk snapshot.
                    self._cookie_restore_failed = True
                    if original is not None:
                        try:
                            self._context.clear_cookies()
                            if original:
                                self._context.add_cookies(original)
                        except Exception:
                            pass
                    self.log("无法恢复本地登录状态，原文件已保留；请手动登录后点击立即保存登录状态。")
        except CookieStoreError as exc:
            self.log(str(exc))
        except Exception:
            # Browser API errors can contain Cookie data in their arguments.
            self.log("无法恢复本地登录状态，请在浏览器中手动登录。")
        self._cookie_state_ready = True
        self._last_cookie_save = time.monotonic()

    def _checkpoint_cookies(self, *, force: bool = False, report: bool = False) -> None:
        if self._context is None or not self._cookie_state_ready:
            return
        # Anonymous/analytics cookies can change on a login page as well, so
        # only an explicit Save may replace a snapshot after a failed restore.
        if self._cookie_restore_failed and not report:
            return
        now = time.monotonic()
        if not force and now - self._last_cookie_save < 5:
            return
        self._last_cookie_save = now
        try:
            cookies = self._context.cookies()
            self._cookie_store.save(cookies)
            self._cookie_restore_failed = False
            self._cookie_save_failed = False
            if report or not self._cookie_saved_logged:
                self.log("浏览器登录状态已保存到项目目录。")
                self._cookie_saved_logged = True
        except Exception:
            message = "无法保存浏览器登录状态；请确认浏览器仍在运行，且项目目录可写。"
            if report:
                raise BrowserError(message) from None
            if not self._cookie_save_failed:
                self.log(message)
                self._cookie_save_failed = True

    def save_login_state(self) -> None:
        """Save the current jar now, from its owner thread, even after Stop."""
        if self._owner_thread is not None and self._owner_thread != threading.get_ident():
            raise BrowserError("浏览器必须由启动它的同一个工作线程操作。")
        if self._context is None or not self._cookie_state_ready:
            raise BrowserError("请先打开浏览器，并在登录后保存登录状态。")
        self._checkpoint_cookies(force=True, report=True)

    def _delay(self) -> None:
        low = max(0, float(self.settings.action_delay_min))
        self._wait(random.uniform(low, max(low, float(self.settings.action_delay_max))))

    def _browser_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for path in (self.paths.runtime, self.paths.profile, self.paths.logs,
                     self.paths.screenshots, self.paths.browsers, self.paths.cache, self.paths.tmp):
            Path(path).mkdir(parents=True, exist_ok=True)
        temporary = str(self.paths.tmp)
        if sys.platform.startswith("linux"):
            # Chromium creates Unix sockets below TMPDIR, whose addresses have
            # a 108-byte limit. A long checkout/profile path must not break it.
            if self._session_tmp is None:
                self._session_tmp = tempfile.TemporaryDirectory(prefix="gbt-", dir="/tmp")
            temporary = self._session_tmp.name
        env.update({"PLAYWRIGHT_BROWSERS_PATH": str(self.paths.browsers),
                    "TMPDIR": temporary, "TMP": temporary, "TEMP": temporary,
                    "XDG_CACHE_HOME": str(self.paths.cache),
                    "XDG_CONFIG_HOME": str(Path(self.paths.runtime) / "config"),
                    "XDG_DATA_HOME": str(Path(self.paths.runtime) / "data"),
                    "XDG_STATE_HOME": str(Path(self.paths.runtime) / "state"),
                    "BREAKPAD_DUMP_LOCATION": str(Path(temporary) / "crash")})
        return env

    @contextmanager
    def _driver_env(self, env: dict[str, str]) -> Iterator[None]:
        # The driver reads these at startup. Restore the process environment
        # immediately; Chromium receives the explicit environment as well.
        names = ("PLAYWRIGHT_BROWSERS_PATH", "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME",
                 "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "BREAKPAD_DUMP_LOCATION")
        previous = {name: os.environ.get(name) for name in names}
        try:
            for name in names:
                os.environ[name] = env[name]
            yield
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def _discover_executable(self) -> str | None:
        configured = self.settings.browser_path.strip()
        if configured:
            path = Path(configured).expanduser()
            if not path.is_file():
                raise BrowserError(f"浏览器路径不存在：{path}")
            return str(path.resolve())
        names = ["chromium", "chromium-browser", "microsoft-edge", "microsoft-edge-stable"]
        candidates: list[Path] = []
        if sys.platform == "win32":
            names += ["chrome", "msedge"]
            for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
                base = os.environ.get(name)
                if base:
                    candidates.extend(Path(base) / suffix for suffix in (
                        "Chromium/Application/chrome.exe", "Google/Chrome/Application/chrome.exe",
                        "Microsoft/Edge/Application/msedge.exe"))
        for name in names:
            executable = shutil.which(name)
            if executable:
                return executable
        for path in candidates:
            if path.is_file():
                return str(path)
        # Only the project-local Playwright store is considered as a fallback.
        for pattern in ("chromium-*/chrome-linux/chrome", "chromium-*/chrome-linux64/chrome",
                        "chromium-*/chrome-win/chrome.exe", "chromium-*/chrome-win64/chrome.exe",
                        "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium"):
            matches = sorted(Path(self.paths.browsers).glob(pattern), reverse=True)
            if matches:
                return str(matches[0])
        return None

    def launch(self, start_url: str = "https://i.chaoxing.com/", headless: bool | None = None) -> None:
        if self._context is not None:
            raise BrowserError("浏览器已启动。")
        self._owner_thread = threading.get_ident()
        self._check_stop()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError("缺少 Playwright，请在项目虚拟环境安装 requirements.txt 中的依赖。") from exc
        try:
            env = self._browser_env()
            executable = self._discover_executable()
            with self._driver_env(env):
                self._playwright = sync_playwright().start()
                if executable is None:
                    executable = self._playwright.chromium.executable_path
                    if not Path(executable).is_file():
                        raise BrowserError("未找到支持的浏览器。请在项目虚拟环境运行 "
                                           "python -m gaybar_tools --install-browser，"
                                           "将 Chromium 安装到项目运行目录，或在设置中指定浏览器。")
                try:
                    executable = prepare_browser_executable(executable, self.paths)
                except IsolationError as exc:
                    raise BrowserError(str(exc)) from exc
                options: dict[str, Any] = {
                    "headless": self.settings.headless if headless is None else headless,
                    "viewport": {"width": 1440, "height": 960}, "locale": "zh-CN", "env": env,
                    "downloads_path": str(Path(self.paths.tmp) / "downloads"), "accept_downloads": False,
                    "args": [f"--disk-cache-dir={self.paths.cache}", "--no-first-run",
                             "--no-default-browser-check", "--disable-breakpad",
                             "--disable-crash-reporter", "--password-store=basic", "--use-mock-keychain"],
                    "timeout": 30000}
                options["executable_path"] = executable
                self._context = self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.paths.profile), **options)
            self._context.set_default_timeout(5000)
            self._context.set_default_navigation_timeout(20000)
            self._restore_login_state()
            self._context.on("page", self._attach_page)
            pages = self._context.pages
            self._page = pages[0] if pages else self._context.new_page()
            for page in self._context.pages:
                self._attach_page(page)
            self._page.goto(start_url, wait_until="domcontentloaded")
            self.log("浏览器已启动；请手动登录并进入课程的任意章节。")
        except Exception as exc:
            self.close()
            if isinstance(exc, (BrowserError, StopRequested)):
                raise
            raise BrowserError("浏览器启动失败。请指定 Chromium / Edge（Windows 也支持 Chrome），"
                               "或运行项目浏览器安装命令将 Chromium 安装到项目运行目录。"
                               f" 原因：{exc}") from exc

    def _attach_page(self, page: Any) -> None:
        if page in self._attached_pages:
            return
        self._attached_pages.add(page)
        if page not in self._page_order:
            self._page_order.append(page)
        page.on("dialog", self._on_dialog)

    def _on_dialog(self, dialog: Any) -> None:
        message = _clean(dialog.message)
        if self._submitting and re.search(
                r"(?:确定|确认|是否|完成).*?(?:提交|交卷)|(?:提交|交卷).*?(?:确定|确认|是否)", message):
            dialog.accept()
        elif self._submitting and _SUBMITTED_RESULT.search(message):
            dialog.accept()
        else:
            self._unexpected_dialog = message or "出现了无法识别的浏览器弹窗"
            dialog.dismiss()

    def _frames(self) -> list[Any]:
        self._check_stop()
        if self._page is None or self._page.is_closed():
            raise BrowserError("浏览器页面已关闭，请重新打开浏览器。")
        return [frame for frame in self._page.frames if not frame.is_detached()]

    @staticmethod
    def _visible(locator: Any) -> bool:
        try:
            return bool(locator.is_visible())
        except Exception:
            return False

    def _first_visible(self, selector: str, *, root: Any = None) -> Any | None:
        for frame in [root] if root is not None else self._frames():
            try:
                candidates = frame.locator(selector)
                for index in range(min(candidates.count(), 100)):
                    locator = candidates.nth(index)
                    if self._visible(locator):
                        return locator
            except Exception:
                continue
        return None

    def _text_control(self, pattern: re.Pattern[str], *, root: Any = None) -> Any | None:
        for frame in [root] if root is not None else self._frames():
            for selector in ("button, a, [role=button], [role=tab], input[type=submit], input[type=button]",
                             "li, span, div"):
                try:
                    candidates = frame.locator(selector).filter(has_text=pattern)
                    for index in range(min(candidates.count(), 120)):
                        loc = candidates.nth(index)
                        text = _clean(loc.inner_text(timeout=600)) or _clean(loc.get_attribute("value") or "")
                        if pattern.fullmatch(text) and self._visible(loc):
                            if loc.evaluate("e => e.children.length > 3 && !['BUTTON','A'].includes(e.tagName)"):
                                continue
                            return loc
                except Exception:
                    continue
        return None

    def _guard(self) -> None:
        self._check_stop()
        if self._unexpected_dialog:
            message, self._unexpected_dialog = self._unexpected_dialog, ""
            raise BrowserError(f"网页弹窗需要人工处理：{message}")
        for frame in self._frames():
            if not self._frame_visible(frame):
                continue
            login = re.search(r"(?:passport2?\.|/login(?:[/?]|$)|/loginpage|/sso/login)", frame.url, re.I)
            if login and frame.parent_frame is None:
                raise BrowserError("登录状态已失效，请手动登录后再开始。")
            captcha = self._first_visible(
                "#captcha, #captchaBox, .captcha-box, .geetest_panel, .nc-container, "
                "input[name*=captcha i], input[id*=captcha i], iframe[src*=captcha i], "
                "[class*=verify-wrap], [id*=verify-wrap]", root=frame)
            if captcha is not None:
                raise BrowserError("检测到验证码或安全验证，请手动处理后再开始。")
            try:
                body = frame.locator("body").inner_text(timeout=1000)
            except Exception:
                continue
            if login and (_clean(body) or self._first_visible("input[type=password]", root=frame) is not None):
                raise BrowserError("登录状态已失效，请手动登录后再开始。")
            if re.search(r"访问过于频繁|请求过于频繁|账号异常|帐号异常|请完成安全验证|请先登录|登录已过期", body):
                raise BrowserError("网页要求人工处理登录、安全验证或访问异常。")

    @staticmethod
    def _frame_visible(frame: Any) -> bool:
        """Ignore dormant authentication/verification frames in page chrome."""
        try:
            while frame.parent_frame is not None:
                element = frame.frame_element()
                try:
                    if not element.is_visible():
                        return False
                finally:
                    element.dispose()
                frame = frame.parent_frame
            return True
        except Exception:
            # A frame that detached while being inspected is no longer a
            # visible login or verification screen for the user to handle.
            return False

    def _is_chapter_page(self, page: Any) -> bool:
        if page.is_closed():
            return False
        identified = content = False
        # A course may be embedded in a generic outer page. Inspect every
        # frame's URL and visible contents independently, so a navigating or
        # detached sibling cannot hide a usable chapter further down the tree.
        for frame in page.frames:
            try:
                if frame.is_detached() or not self._frame_visible(frame):
                    continue
                parts = urlparse(frame.url)
                if _CHAPTER_URL.search(parts.path):
                    return True
                query = {key.lower(): value for key, value in parse_qs(parts.query).items()}
                identified = identified or bool(query.get("chapterid") or query.get("knowledgeid"))
                if self._first_visible(
                        ".ans-attach-online, .TiMu, [data-question-id], .chapter-content", root=frame) is not None:
                    return True
                tabs = frame.get_by_text(_QUIZ_TAB, exact=True)
                if any(self._visible(tabs.nth(index)) for index in range(min(tabs.count(), 100))):
                    return True
                content = content or self._first_visible(
                    "video, #iframe, #content-frame, #iframeBox", root=frame) is not None
            except Exception:
                # Frame navigation is transient; continue inspecting siblings.
                continue
        return identified and content

    def _open_pages(self) -> list[Any]:
        if self._context is None:
            return []
        # The synchronous Playwright API dispatches page/frame/dialog events
        # only while an API call runs. Merely reading context.pages and URL
        # properties during idle polling can otherwise leave a new tab unseen.
        pump = self._page
        if pump is None:
            pump = next(iter(self._context.pages), None)
        if pump is not None:
            try:
                pump.wait_for_timeout(1)
            except Exception:
                if not pump.is_closed():
                    raise
        pages = [page for page in self._context.pages if not page.is_closed()]
        self._checkpoint_cookies()
        for page in pages:
            self._attach_page(page)
        if self._chapter_page_selected and self._page in self._page_order:
            # Keep the order even after tabs close: completing or closing a
            # newer course must not resume an older course automatically.
            current_index = self._page_order.index(self._page)
            pages = [page for page in pages if self._page_order.index(page) >= current_index]
        return pages

    def _select_page(self, page: Any) -> None:
        if page is not self._page:
            self._page, self._mouse = page, (0.0, 0.0)
            self._question_controls.clear()
            self._questions.clear()
            self.log("已跟随课程章节页面。")

    def poll_ready(self) -> bool:
        self._check_stop()
        if self._context is None:
            return False
        try:
            pages = self._open_pages()
            candidates = [page for page in pages if self._is_chapter_page(page)]
            if not candidates:
                return False
            self._select_page(candidates[-1])
            self._chapter_page_selected = True
            self._guard()
            return True
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError("读取浏览器页面失败，请稍候重试或重新打开浏览器。") from exc

    def prepare_start(self) -> str:
        """Inspect visible content on an explicit start, without a URL gate."""
        self._check_stop()
        if self._context is None:
            raise BrowserError("请先打开浏览器，并手动进入课程章节。")
        try:
            pages = self._open_pages()
            if not pages:
                raise BrowserError("当前课程页面已关闭，请打开新的课程章节后再开始。")
            # Explicit start must inspect the latest page even when an older
            # tab matches the automatic URL heuristics. Otherwise a valid
            # new reader/player could accidentally start the older course.
            self._select_page(pages[-1])
            self._guard()
            kind = self.chapter_kind()
            if kind != "unknown":
                self._chapter_page_selected = True
            return kind
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError("读取浏览器页面失败，请稍候重试或重新打开浏览器。") from exc

    def chapter_key(self) -> str:
        self._guard()
        for frame in self._frames():
            parts = urlparse(frame.url)
            query = {key.lower(): value for key, value in parse_qs(parts.query).items()}
            identifier = (query.get("chapterid") or query.get("knowledgeid") or [""])[0]
            if identifier:
                course = (query.get("courseid") or [""])[0]
                clazz = (query.get("clazzid") or query.get("classid") or [""])[0]
                return f"{course}:{clazz}:{identifier}"
        selected = self._first_visible(".catalog_points.cur, .catalog_points.active, .chapter.active, "
                                       "[data-chapter-id][aria-current=true], .posCatalog_active")
        if selected is not None:
            return selected.get_attribute("data-chapter-id") or _clean(selected.inner_text())
        title = self._first_visible(".chapter-title, #chapterTitle, .chapterTitle, h1")
        suffix = _clean(title.inner_text()) if title is not None else ""
        return f"{self.current_url.split('#')[0]}|{suffix}"

    def _video_locators(self) -> list[Any]:
        result = []
        for frame in self._frames():
            try:
                videos = frame.locator("video")
                for index in range(videos.count()):
                    video = videos.nth(index)
                    if self._visible(video):
                        result.append(video)
            except Exception:
                continue
        return result

    def chapter_kind(self) -> str:
        self._guard()
        if self._video_locators():
            return "video"
        if self._has_quiz() or self._result_visible():
            return "quiz"
        if self._document_targets():
            return "document"
        return "unknown"

    def _move_mouse(self, x: float, y: float) -> None:
        start_x, start_y = self._mouse
        distance = math.hypot(x - start_x, y - start_y)
        steps = max(3, min(18, int(distance / 55)))
        for step in range(1, steps + 1):
            self._check_stop()
            t = step / steps
            curve = math.sin(math.pi * t) * min(distance * 0.03, 12)
            self._page.mouse.move(start_x + (x - start_x) * t + curve,
                                  start_y + (y - start_y) * t - curve / 2)
            self._wait(random.uniform(0.008, 0.025))
        self._mouse = (x, y)

    def _click(self, locator: Any) -> None:
        self._guard()
        if not self._visible(locator):
            raise BrowserError("目标控件不可见，已停止以免误点。")
        if locator.get_attribute("aria-disabled") == "true" or locator.get_attribute("disabled") is not None:
            raise BrowserError("目标控件当前不可用。")
        locator.scroll_into_view_if_needed(timeout=3000)
        locator.click(trial=True, timeout=3000)
        box = locator.bounding_box(timeout=2000)
        if not box or box["width"] <= 0 or box["height"] <= 0:
            raise BrowserError("无法确定控件的屏幕位置。")
        x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
        y = box["y"] + box["height"] * random.uniform(0.35, 0.65)
        self._delay()
        self._move_mouse(x, y)
        self._page.mouse.click(x, y, delay=random.uniform(65, 140))
        self._delay()

    def watch_video(self) -> None:
        try:
            self._watch_video()
        except BrowserError:
            self._pause_video_on_stop()
            raise

    def _pause_video_on_stop(self) -> None:
        # Stop must not leave a launched lesson playing in the background. Use a
        # trusted pointer click, without clearing the caller's cancellation flag.
        try:
            if not self._page or self._page.is_closed():
                return
            for frame in self._page.frames:
                videos = frame.locator("video")
                for index in range(videos.count()):
                    video = videos.nth(index)
                    if not self._visible(video) or video.evaluate("v => v.paused || v.ended"):
                        continue
                    control = self._first_visible(
                        ".vjs-play-control, button[aria-label*=Pause], button[aria-label*=暂停]", root=frame)
                    target = control if control is not None else video
                    target.scroll_into_view_if_needed(timeout=1000)
                    target.click(timeout=1000)
        except Exception:
            self.log("停止后未能确认播放器暂停，请手动检查浏览器。")

    def _watch_video(self) -> None:
        self._guard()
        videos = self._video_locators()
        if not videos:
            raise BrowserError("未找到可见的视频播放器。")
        deadline = time.monotonic() + float(self.settings.video_timeout)
        for number, video in enumerate(videos, start=1):
            self.log(f"开始等待视频 {number}/{len(videos)} 自然播放结束。")
            last_position, progressed_at, resumed_at = -1.0, time.monotonic(), 0.0
            while True:
                self._guard()
                if time.monotonic() > deadline:
                    raise BrowserError("视频等待超时，请检查播放器或网络。")
                try:
                    state = video.evaluate("""v => ({ended:v.ended, paused:v.paused,
                        time:v.currentTime, duration:Number.isFinite(v.duration)?v.duration:0,
                        rate:v.playbackRate, error:v.error?v.error.message:null})""")
                except Exception as exc:
                    raise BrowserError("视频播放器已发生变化，请确认当前章节。") from exc
                if state["error"]:
                    raise BrowserError(f"播放器错误：{state['error']}")
                if abs(float(state["rate"]) - 1) > 0.01:
                    raise BrowserError("检测到视频播放速度不是 1 倍，请手动恢复后再开始。")
                if state["ended"]:
                    self.log("视频已自然播放结束。")
                    break
                position = float(state["time"])
                if position > last_position + 0.15:
                    progressed_at, last_position = time.monotonic(), position
                if state["paused"]:
                    if time.monotonic() - resumed_at < 5:
                        raise BrowserError("播放器仍暂停，可能有弹题或提示需要人工处理。")
                    play = None
                    for frame in self._frames():
                        candidates = frame.locator("video")
                        if candidates.count() and self._visible(candidates.first):
                            play = self._first_visible(
                                ".vjs-big-play-button, .vjs-play-control, button[aria-label*=Play], "
                                "button[aria-label*=播放], [role=button][title=播放]", root=frame)
                            if play is not None:
                                break
                    self._click(play if play is not None else video)
                    resumed_at = time.monotonic()
                    self._wait(0.5)
                elif time.monotonic() - progressed_at > 120:
                    raise BrowserError("视频超过 120 秒没有进展，请检查缓冲或提示。")
                if time.monotonic() - self._last_video_log > 30:
                    self.log(f"视频进度 {int(position)} / {int(state['duration'])} 秒。")
                    self._last_video_log = time.monotonic()
                self._wait(max(0.25, float(self.settings.poll_interval)))

    def _document_targets(self) -> list[Any]:
        # A reader's outer attachment/wrapper often never scrolls. Discover the
        # actual scroll roots before accepting a flat, single-page document.
        marker = (".ans-attach-online, #reader, #viewerContainer, #document-viewer, .document-content, "
                  ".document-viewer, .pdfViewer, .chapter-content, [data-document], "
                  ".ppt-container, #docContainer, #read")
        selector = "body, body div, body main, body article, body section, " + marker
        targets: list[tuple[int, Any]] = []
        document_frames: set[Any] = set()
        for frame in self._frames():
            try:
                if frame.is_detached() or not self._frame_visible(frame):
                    continue
                explicit = frame.locator(marker)
                has_marker = any(self._visible(explicit.nth(i))
                                 for i in range(min(explicit.count(), 30)))
                known_path = bool(re.search(r"read|doc|pdf|ppt|office|knowledge/cards",
                                            urlparse(frame.url).path, re.I))
                parent = frame.parent_frame
                if not (has_marker or known_path or parent in document_frames):
                    continue
                document_frames.add(frame)
                candidates = frame.locator(selector)
                indices = candidates.evaluate_all(r"""(elements, marker) => {
                    const navigation = 'aside, nav, [role=navigation], [role=tree], '
                        + '.sidebar, .catalog, .catalogue, .directory, .toc, .outline, '
                        + '.chapter-list, .reader-toolbar, .toolbar, .thumbnails';
                    const rows = elements.map((e, index) => {
                        const root = e === document.body || e === document.documentElement;
                        const t = root ? document.scrollingElement : e;
                        if (!t || e.closest(navigation)) return null;
                        const r = e.getBoundingClientRect(), s = getComputedStyle(e);
                        if (s.visibility === 'hidden' || s.display === 'none'
                            || (root ? innerWidth : r.width) < 180
                            || (root ? innerHeight : r.height) < 24) return null;
                        const client = root ? Math.min(innerHeight, t.clientHeight || innerHeight) : t.clientHeight;
                        const scrolls = t.scrollHeight > client + 8
                            && (root || /(auto|scroll)/.test(s.overflowY));
                        if (!scrolls && !(e.matches(marker) || root)) return null;
                        const media = !!e.querySelector('canvas, img, svg');
                        const text = (e.innerText || '').replace(/\s+/g, '').length;
                        const embedded = [...e.querySelectorAll('iframe, embed, object')].some(child => {
                            const r=child.getBoundingClientRect(), s=getComputedStyle(child);
                            return r.width>0 && r.height>0 && child.getClientRects().length>0
                                && s.display!=='none' && s.visibility!=='hidden';
                        });
                        // Frames/plugins do not expose their reading progress on
                        // the shell. Their own document must supply a real target.
                        if (embedded) return null;
                        if (!scrolls && text < 40 && !media
                            && !(text > 0 && (e.matches(marker) || e.querySelector(marker)))) return null;
                        return {e, t, index, scrolls};
                    }).filter(Boolean);
                    return rows.filter(row => !rows.some(other => other !== row
                        && ((row.t === other.t && row.index > other.index)
                            || (other.scrolls && other.e.contains(row.e) && !row.scrolls)
                            || (row.e.contains(other.e) && row.t !== other.t
                                && (other.scrolls || !row.scrolls)))))
                        .sort((a, b) => Number(b.scrolls) - Number(a.scrolls))
                        .map(row => row.index);
                }""", marker)
                depth, ancestor = 0, parent
                while ancestor is not None:
                    depth += 1
                    ancestor = ancestor.parent_frame
                targets.extend((depth, candidates.nth(index)) for index in indices)
            except Exception:
                continue
        # Inner frames contain the actual pages; outer shells should never be
        # mistaken for proof that an embedded document has been read.
        return [target for _depth, target in sorted(targets, key=lambda row: -row[0])]

    @staticmethod
    def _scroll_metrics(locator: Any) -> dict[str, float]:
        return locator.evaluate(r"""e => {
            const root=e===document.body||e===document.documentElement;
            const t=root?document.scrollingElement:e;
            return {top:t.scrollTop, height:t.scrollHeight,
                    client:root?Math.min(innerHeight,t.clientHeight||innerHeight):t.clientHeight};
        }""")

    @staticmethod
    def _document_local_rect(locator: Any) -> dict[str, float]:
        # All rectangles returned here are in this element's frame viewport.
        # Body bounding boxes become negative during window scrolling, so the
        # scrolling document itself is represented by its viewport instead.
        return locator.evaluate(r"""e => {
            const root=e===document.body||e===document.documentElement;
            const box=n => {
                const r=n.getBoundingClientRect();
                const sx=n.offsetWidth ? r.width/n.offsetWidth : 1;
                const sy=n.offsetHeight ? r.height/n.offsetHeight : 1;
                return {x:r.left+n.clientLeft*sx, y:r.top+n.clientTop*sy,
                        width:n.clientWidth*sx, height:n.clientHeight*sy};
            };
            const r=root ? {x:0,y:0,width:innerWidth,height:innerHeight} : box(e);
            let left=Math.max(0,r.x), top=Math.max(0,r.y);
            let right=Math.min(innerWidth,r.x+r.width), bottom=Math.min(innerHeight,r.y+r.height);
            if (!root) for(let n=e.parentElement;n;n=n.parentElement){
                const s=getComputedStyle(n), a=box(n);
                if(/(auto|scroll|hidden|clip)/.test(s.overflowX)){
                    left=Math.max(left,a.x);right=Math.min(right,a.x+a.width);
                }
                if(/(auto|scroll|hidden|clip)/.test(s.overflowY)){
                    top=Math.max(top,a.y);bottom=Math.min(bottom,a.y+a.height);
                }
            }
            return {x:left,y:top,width:Math.max(0,right-left),height:Math.max(0,bottom-top)};
        }""")

    @staticmethod
    def _intersect_rect(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
        x, y = max(a["x"], b["x"]), max(a["y"], b["y"])
        return {"x": x, "y": y,
                "width": max(0, min(a["x"] + a["width"], b["x"] + b["width"]) - x),
                "height": max(0, min(a["y"] + a["height"], b["y"] + b["height"]) - y)}

    @staticmethod
    def _map_document_rect(rect: dict[str, float], mapping: dict[str, float]) -> dict[str, float]:
        return {"x": mapping["x"] + rect["x"] * mapping["sx"],
                "y": mapping["y"] + rect["y"] * mapping["sy"],
                "width": rect["width"] * mapping["sx"],
                "height": rect["height"] * mapping["sy"]}

    def _document_frame_geometry(self, frame: Any) -> tuple[dict[str, float], dict[str, float]]:
        parent = frame.parent_frame
        if parent is None:
            viewport = frame.evaluate("() => ({x:0,y:0,width:innerWidth,height:innerHeight})")
            return {"x": 0, "y": 0, "sx": 1, "sy": 1}, viewport
        parent_map, clip = self._document_frame_geometry(parent)
        element = frame.frame_element()
        try:
            local = self._document_local_rect(element)
            clip = self._intersect_rect(clip, self._map_document_rect(local, parent_map))
            # Playwright's bounding_box is already in MAIN-frame coordinates,
            # unlike the frame-local DOM rectangles above. Keep that distinction
            # explicit when mapping through nested or scaled iframes.
            box = element.bounding_box()
            size = element.evaluate("e => ({w:e.offsetWidth,h:e.offsetHeight,x:e.clientLeft,y:e.clientTop})")
            if not box or not size["w"] or not size["h"]:
                raise BrowserError("文档所在框架不可见。")
            sx, sy = box["width"] / size["w"], box["height"] / size["h"]
            return {"x": box["x"] + size["x"] * sx,
                    "y": box["y"] + size["y"] * sy, "sx": sx, "sy": sy}, clip
        finally:
            element.dispose()

    def _document_pointer(self, target: Any, variant: int = 0) -> tuple[float, float, dict[str, float]]:
        handle = target.element_handle(timeout=1500)
        if handle is None:
            raise BrowserError("文档内容区域已消失。")
        try:
            frame = handle.owner_frame()
        finally:
            handle.dispose()
        if frame is None:
            raise BrowserError("无法确定文档所在框架。")
        mapping, clip = self._document_frame_geometry(frame)
        region = self._intersect_rect(clip, self._map_document_rect(self._document_local_rect(target), mapping))
        if region["width"] < 24 or region["height"] < 24:
            raise BrowserError("文档内容区域被裁剪或不可见，请调整窗口或关闭遮挡提示。")
        positions = [(0.55, 0.55), (0.35, 0.55), (0.7, 0.7), (0.5, 0.35),
                     (0.25, 0.75), (0.75, 0.35), (0.5, 0.8), (0.2, 0.3)]
        for index in range(len(positions)):
            fx, fy = positions[(index + variant) % len(positions)]
            x, y = region["x"] + region["width"] * fx, region["y"] + region["height"] * fy
            local = {"x": (x - mapping["x"]) / mapping["sx"],
                     "y": (y - mapping["y"]) / mapping["sy"]}
            hit = target.evaluate(r"""(e,p) => {
                const h=document.elementFromPoint(p.x,p.y);
                const t=(e===document.body||e===document.documentElement)?document.scrollingElement:e;
                return !!h && (h===t||t.contains(h))
                    && !h.closest('aside,nav,[role=navigation],[role=tree],.sidebar,.catalog,.catalogue,'
                        + '.directory,.toc,.outline,.chapter-list,.toolbar,.reader-toolbar,.thumbnails,'
                        + 'button,input,select,textarea,[role=button],iframe,embed,object');
            }""", local)
            if not hit:
                continue
            ancestor = frame
            while ancestor.parent_frame is not None:
                parent = ancestor.parent_frame
                parent_map, _ = self._document_frame_geometry(parent)
                element = ancestor.frame_element()
                try:
                    hit = element.evaluate("""(e,p) => {
                        const h=document.elementFromPoint(p.x,p.y); return h===e||e.contains(h);
                    }""", {"x": (x - parent_map["x"]) / parent_map["sx"],
                              "y": (y - parent_map["y"]) / parent_map["sy"]})
                finally:
                    element.dispose()
                if not hit:
                    break
                ancestor = parent
            if hit:
                return x, y, region
        raise BrowserError("无法在文档内容内找到可接收鼠标滚动的位置，请关闭遮挡提示或调整阅读区域。")

    def read_document(self) -> None:
        self._guard()
        self._page.bring_to_front()
        targets = self._document_targets()
        if not targets:
            raise BrowserError("未找到可确认阅读进度的文档内容；嵌入阅读器或插件可能需要人工查看。")
        self.log("正在逐步滚动文档并确认阅读区域已到底部。")
        processed: set[str] = set()
        for number, target in enumerate(targets, start=1):
            self._guard()
            try:
                identity = target.evaluate("""e => {
                    if(e===document.body||e===document.documentElement)e=document.scrollingElement;
                    const a=[]; for(let n=e;n&&n.nodeType===1;n=n.parentElement){
                      a.unshift(n.tagName+':'+Array.from(n.parentElement?.children||[]).indexOf(n));
                    } return location.href+'|'+a.join('/');
                }""")
                if identity in processed:
                    continue
                processed.add(identity)
                target.scroll_into_view_if_needed(timeout=3000)
                previous_top, stuck, bottom_reads = -1.0, 0, 0
                bottom_since, bottom_signature, last_log = 0.0, None, 0.0
                for _ in range(1200):
                    self._guard()
                    metrics = self._scroll_metrics(target)
                    if metrics["client"] < 1 or metrics["height"] < 1:
                        raise BrowserError("文档阅读区域尺寸无效，无法确认阅读进度。")
                    if metrics["top"] + metrics["client"] >= metrics["height"] - 3:
                        # Also confirm a visible, unobstructed content area for
                        # single-page readers. A flat shell is not completion.
                        self._document_pointer(target, stuck)
                        signature = (metrics["height"], metrics["client"])
                        if signature != bottom_signature:
                            bottom_signature, bottom_since, bottom_reads = signature, time.monotonic(), 0
                        bottom_reads += 1
                        settle = max(1.2, 2 * max(0.4, float(self.settings.poll_interval)))
                        if bottom_reads >= 3 and time.monotonic() - bottom_since >= settle:
                            break
                        self._wait(max(0.4, float(self.settings.poll_interval)))
                        continue
                    bottom_reads, bottom_since, bottom_signature = 0, 0.0, None
                    if abs(metrics["top"] - previous_top) < 1:
                        stuck += 1
                        if stuck >= 6:
                            raise BrowserError("文档滚动没有进展，已尝试重新定位内容区域；请检查遮挡提示或嵌套阅读器。")
                    else:
                        stuck = 0
                    previous_top = metrics["top"]
                    x, y, region = self._document_pointer(target, stuck)
                    if not last_log or stuck or time.monotonic() - last_log > 20:
                        self.log(f"文档 {number}/{len(targets)}：可见内容 {region['width']:.0f}×{region['height']:.0f}，"
                                 f"进度 {metrics['top']:.0f}+{metrics['client']:.0f}/{metrics['height']:.0f}，"
                                 f"鼠标位置 ({x:.0f}, {y:.0f})。")
                        last_log = time.monotonic()
                    self._move_mouse(x, y)
                    self._page.mouse.wheel(0, random.uniform(650, 1000))
                    # wheel dispatch returns before the browser finishes scrolling.
                    self._wait(random.uniform(0.18, 0.32))
                else:
                    raise BrowserError("文档页过长或一直加载新内容，已停止自动滚动。")
                metrics = self._scroll_metrics(target)
                if metrics["top"] + metrics["client"] < metrics["height"] - 3:
                    raise BrowserError("尚未确认文档已滚动到底部。")
                self.log(f"文档 {number}/{len(targets)} 已确认到底部："
                         f"{metrics['top']:.0f}+{metrics['client']:.0f}/{metrics['height']:.0f}。")
            except (BrowserError, StopRequested):
                raise
            except Exception as exc:
                raise BrowserError("文档结构变化，无法确认已经读到底部。") from exc
        self.log("已确认文档阅读区域到底部。")

    def _has_quiz(self) -> bool:
        return any(self._first_visible(selector) is not None for selector in _QUESTION_BLOCKS)

    def open_quiz(self) -> bool:
        self._guard()
        if self._has_quiz():
            return True
        tab = self._text_control(_QUIZ_TAB)
        if tab is None:
            return False
        self._click(tab)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            self._guard()
            if self._has_quiz() or self._result_visible():
                return True
            self._wait(0.4)
        raise BrowserError("点击学习检测后未出现可识别的测验，请检查页面。")

    def _option_candidates(self, block: Any) -> list[Any]:
        for selector in _OPTION_SELECTORS:
            candidates = block.locator(selector)
            result = [candidates.nth(index) for index in range(min(candidates.count(), 30))
                      if self._visible(candidates.nth(index))]
            if len(result) >= 2:
                return result
        inputs = block.locator("input[type=radio], input[type=checkbox]")
        return [inputs.nth(index) for index in range(min(inputs.count(), 30))
                if self._visible(inputs.nth(index))]

    def get_questions(self) -> list[Question]:
        self._guard()
        self._question_controls.clear()
        self._questions.clear()
        questions: list[Question] = []
        for frame_number, frame in enumerate(self._frames()):
            blocks: list[Any] = []
            for selector in _QUESTION_BLOCKS:
                locs = frame.locator(selector)
                valid = [locs.nth(index) for index in range(min(locs.count(), 200))
                         if self._visible(locs.nth(index))]
                if valid:
                    blocks = valid
                    break
            for index, block in enumerate(blocks):
                self._check_stop()
                raw_text = block.inner_text(timeout=2000)
                text = _clean(raw_text)
                if not text:
                    continue
                if re.search(r"\[(?:填空|简答|论述|连线|排序|阅读|计算)题\]|【(?:填空|简答|论述)题】", text):
                    raise BrowserError("检测到尚不支持的题型；只支持单选、多选和判断。")
                candidates = self._option_candidates(block)
                if not candidates:
                    if _RESULT.search(text):
                        continue
                    raise BrowserError("题目没有可可靠识别的选项控件，请人工查看诊断截图。")
                title = block.locator(_QUESTION_TITLES)
                stem = _clean(title.first.inner_text(timeout=1500)) if title.count() else _clean(raw_text.splitlines()[0])
                kind = "multiple" if "多选" in stem else "boolean" if "判断" in stem else "single"
                if block.locator("input[type=checkbox], [role=checkbox]").count():
                    kind = "multiple"
                options: list[Option] = []
                controls: dict[str, Any] = {}
                for option_index, control in enumerate(candidates):
                    raw = _clean(control.inner_text(timeout=1000))
                    if not raw:
                        raw = _clean(control.get_attribute("aria-label") or control.get_attribute("value") or "")
                    if not raw:
                        raise BrowserError("选项包含没有文字标签的控件，需要人工处理。")
                    match = re.match(r"^([A-Ha-h])(?:[.、．:：)）\s]|$)\s*(.*)", raw)
                    if match:
                        label, option_text = match.group(1).upper(), match.group(2).strip() or raw
                    else:
                        label, option_text = chr(65 + option_index), raw
                    if label in controls:
                        raise BrowserError("题目中出现重复的选项标识，无法安全作答。")
                    options.append(Option(label=label, text=option_text))
                    controls[label] = control
                if len(options) == 2 and {option.text for option in options} <= {"对", "错", "正确", "错误", "是", "否", "√", "×"}:
                    kind = "boolean"
                question_id = (block.get_attribute("data-question-id") or block.get_attribute("data-id")
                               or block.get_attribute("id") or f"f{frame_number}-q{index + 1}")
                if question_id in self._questions:
                    question_id = f"f{frame_number}-{question_id}"
                question = Question(id=question_id, text=stem or "[题干为图片，请参考截图]", kind=kind,
                                    options=tuple(options))
                questions.append(question)
                self._questions[question_id] = question
                self._question_controls[question_id] = controls
        if not questions and not self._result_visible():
            raise BrowserError("未找到受支持的测验题目。")
        return questions

    def quiz_screenshot(self) -> bytes:
        self._guard()
        return self._page.screenshot(type="png", full_page=True, timeout=10000)

    @staticmethod
    def _selected(locator: Any) -> bool | None:
        return locator.evaluate(r"""e => {
            const input=e.matches('input')?e:e.querySelector('input[type=radio],input[type=checkbox]');
            if(input) return input.checked;
            const aria=e.getAttribute('aria-checked'); if(aria!==null)return aria==='true';
            const items=[e,...e.querySelectorAll('span,i,a')];
            if(items.some(n=>/(^|\s)(check_answer|checkAnswer|checked|selected|active|cur|on)(\s|$)/.test(n.className||'')))return true;
            if(e.matches('[role=radio],[role=checkbox],li,.option,label'))return false;
            return null;
        }""")

    def apply_answers(self, answers: list[Answer]) -> None:
        self._guard()
        if not self._questions:
            raise BrowserError("请先读取题目，再填写答案。")
        mapped = {answer.question_id: answer for answer in answers}
        if len(mapped) != len(answers) or set(mapped) != set(self._questions):
            raise BrowserError("答案与当前题目不完整对应，已停止填写。")
        for question_id, question in self._questions.items():
            choices = tuple(str(choice).strip().upper() for choice in mapped[question_id].choices)
            if not choices or len(set(choices)) != len(choices):
                raise BrowserError(f"题目 {question_id} 的答案为空或重复。")
            if question.kind != "multiple" and len(choices) != 1:
                raise BrowserError(f"题目 {question_id} 只能选择一个答案。")
            if not set(choices) <= set(self._question_controls[question_id]):
                raise BrowserError(f"题目 {question_id} 包含无效选项。")
        for question_id, question in self._questions.items():
            desired = {str(choice).strip().upper() for choice in mapped[question_id].choices}
            for label, control in self._question_controls[question_id].items():
                self._guard()
                selected = self._selected(control)
                if label in desired and selected is not True:
                    self._click(control)
                    if self._selected(control) is not True:
                        raise BrowserError(f"无法确认题目 {question_id} 的选项 {label} 已选中。")
                elif label not in desired and selected and question.kind == "multiple":
                    self._click(control)
                    if self._selected(control) is not False:
                        raise BrowserError(f"无法取消题目 {question_id} 原有的选项 {label}。")
            for label, control in self._question_controls[question_id].items():
                if self._selected(control) != (label in desired):
                    raise BrowserError(f"题目 {question_id} 的最终选项状态与答案不一致。")
            self.log(f"已填写题目 {question_id}：{', '.join(sorted(desired))}")

    def _result_visible(self) -> bool:
        for frame in self._frames():
            try:
                # Inspect the assessment document itself. Course outlines in an
                # outer frame can list other chapters as "已提交".
                quiz_structure = frame.locator(
                    ".TiMu, [data-question-id], .Zy_TItle, .Py_answer, .mark_answer, "
                    "#submission-result, .submit-success, .answerResult, #workForm, .workForm")
                if not quiz_structure.count() and not re.search(
                        r"(?:quiz|/work/|homework|exam|doHomeWork)", urlparse(frame.url).path, re.I):
                    continue
                # A plain status node is useful only within the identified quiz.
                nodes = frame.locator(
                    ".submit-success, #submission-result, .answerResult, .Py_answer, "
                    ".mark_answer, [role=status], .result-message, .workStatus")
                for index in range(min(nodes.count(), 60)):
                    node = nodes.nth(index)
                    if self._visible(node) and _SUBMITTED_RESULT.search(_clean(node.inner_text(timeout=800))):
                        return True
                # Chaoxing's graded view displays "我的答案：" for each question.
                # Require a separate result/score indication in the same frame.
                answers = frame.get_by_text(re.compile(r"^我的答案\s*[:：]"))
                scores = frame.get_by_text(re.compile(r"^(?:本次成绩|本次得分|最终成绩|得分|正确答案)\s*[:：]?"))
                if any(self._visible(answers.nth(i)) for i in range(min(answers.count(), 30))):
                    if any(self._visible(scores.nth(i)) for i in range(min(scores.count(), 30))):
                        return True
            except Exception:
                continue
        return False

    def quiz_completed(self) -> bool:
        """Read a visible submitted/graded result in the current assessment."""
        self._guard()
        return self._result_visible()

    def submit_quiz(self) -> bool:
        self._guard()
        if self._result_visible():
            return True
        submit = self._text_control(_SUBMIT)
        if submit is None:
            raise BrowserError("未找到可识别的提交按钮。")
        self._submitting = True
        try:
            self._click(submit)
            deadline = time.monotonic() + 20
            confirmation_clicked = False
            while time.monotonic() < deadline:
                self._guard()
                if self._result_visible():
                    self.log("已确认测验提交结果。")
                    return True
                for frame in self._frames():
                    dialogs = frame.locator("[role=dialog], .layui-layer, .layui-layer-dialog, .ant-modal, .el-message-box, .confirm-dialog, .popDiv")
                    for index in range(min(dialogs.count(), 15)):
                        dialog = dialogs.nth(index)
                        if not self._visible(dialog):
                            continue
                        text = _clean(dialog.inner_text(timeout=1000))
                        if re.search(r"未作答|漏答|尚未完成|还有.*未答", text):
                            raise BrowserError("网页提示仍有题目未作答，已停止提交。")
                        if re.search(r"提交|交卷", text) and not confirmation_clicked:
                            confirm = self._text_control(re.compile(r"^(?:确定|确认|确认提交|确定提交|提交)$"), root=dialog)
                            if confirm is None:
                                raise BrowserError("提交确认框中没有可识别的确认按钮。")
                            self._click(confirm)
                            confirmation_clicked = True
                self._wait(0.3)
            raise BrowserError("提交后没有检测到明确的成功结果，请人工确认，程序不会重复提交。")
        finally:
            self._submitting = False

    def next_chapter(self) -> bool:
        self._guard()
        previous = self.chapter_key()
        previous_url = self.current_url
        button = self._text_control(_NEXT)
        if button is None:
            return False
        if button.get_attribute("aria-disabled") == "true" or button.get_attribute("disabled") is not None:
            return False
        if re.search(r"(?:^|\s)(?:disabled|disable)(?:\s|$)", button.get_attribute("class") or ""):
            return False
        self._click(button)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            self._guard()
            ready = self.poll_ready()
            if ready and self.chapter_key() != previous:
                self._question_controls.clear()
                self._questions.clear()
                self.log("已进入下一节。")
                return True
            if not ready and self.current_url != previous_url:
                # A recognised chapter must be present before returning True;
                # completion/overview pages end the workflow naturally.
                self._wait(0.8)
                if not self.poll_ready():
                    self.log("已离开章节页面，没有下一节。")
                    return False
            self._wait(0.4)
        raise BrowserError("点击下一节后章节没有变化，已停止以防循环。")

    def capture_diagnostic(self) -> Path | None:
        if self._page is None or self._page.is_closed():
            return None
        try:
            Path(self.paths.screenshots).mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            path = Path(self.paths.screenshots) / f"diagnostic-{stamp}.png"
            self._page.screenshot(path=str(path), full_page=True, timeout=5000)
            return path
        except Exception:
            return None

    def close(self) -> None:
        if self._owner_thread is not None and self._owner_thread != threading.get_ident():
            raise BrowserError("请在浏览器工作线程中关闭浏览器。")
        # Stop only cancels course actions. The last checkpoint must still run
        # before Chromium closes, while its session cookies remain readable.
        self._checkpoint_cookies(force=True)
        context, playwright = self._context, self._playwright
        temporary, self._session_tmp = self._session_tmp, None
        self._context = self._playwright = self._page = None
        self._cookie_state_ready = False
        self._last_cookie_save = 0.0
        self._cookie_save_failed = False
        self._cookie_saved_logged = False
        self._cookie_restore_failed = False
        self._chapter_page_selected = False
        self._page_order.clear()
        self._attached_pages.clear()
        self._question_controls.clear()
        self._questions.clear()
        try:
            try:
                if context is not None:
                    context.close()
            finally:
                if playwright is not None:
                    playwright.stop()
        finally:
            if temporary is not None:
                temporary.cleanup()


def discover_browser(browser_path: str, paths: AppPaths) -> str | None:
    """Locate a supported installed browser without starting or downloading it."""
    from dataclasses import replace
    settings = replace(AppSettings(), browser_path=browser_path)
    return BrowserSession(settings, paths, lambda _: None, threading.Event())._discover_executable()
