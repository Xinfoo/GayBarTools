"""Synchronous, conservative Playwright adapter for visible chapter controls.

JavaScript inspects DOM/media state and filters pointer exits during video work;
it never changes media position, speed, answers, or task flags. A session belongs
to the worker thread that launched it.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import secrets
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
from .models import Answer, Option, Question, VideoQuizSolver
from .paths import AppPaths


class BrowserError(RuntimeError):
    """The page cannot be operated safely with the supported adapter."""


class StopRequested(BrowserError):
    """The user requested cancellation."""


@dataclass(frozen=True)
class ContentTask:
    """One mounted reading region or player on the current learning page."""

    kind: str
    target: Any
    identity: str
    order: tuple[Any, ...]
    completed: bool = False
    placeholder: bool = False
    region: str = ""
    required: bool = True


_CHAPTER_URL = re.compile(r"/(?:mycourse/studentstudy|knowledge/cards|studentstudy)(?:[/?]|$)", re.I)
_QUESTION_BLOCKS = ("[data-question-id]", ".TiMu", ".questionLi", ".question-item",
                    ".question", "fieldset", ".examPaper_question", ".singleQuesId")
_QUESTION_TITLES = ".Zy_TItle, .question-title, .questionTitle, .stem, legend, .mark_name, .qtContent"
_OPTION_SELECTORS = (".Zy_ulTop > li", ".answerList > li", ".answer-list > li",
                     ".options > li", ".option", "[role=radio]", "[role=checkbox]", "label")
_NEXT = re.compile(r"^下[一1]节(?:\s*[>›»])?$")
_NEXT_TASK_WARNING = re.compile(r"当前章节.{0,16}任务点.{0,8}未完成[，,。\s]*是否.{0,8}完成")
_NEXT_WARNING_ROOTS = ("dialog, [role=dialog], .popDiv, .AlertCon02, .layui-layer, "
                       ".ant-modal, .el-message-box, .confirm-dialog")
_SUBMIT = re.compile(r"^(?:提交|提交答案|提交作业|交卷)$")
_RESULT = re.compile(r"(?:提交成功|已提交|作业已完成|测验已完成|我的答案\s*[:：]|本次得分|本次成绩|最终成绩)")
_SUBMITTED_RESULT = re.compile(r"提交成功|已提交|作业已完成|测验已完成")
# Compatibility candidates, not selectors confirmed from the reference video.
# Geometry and the owning player frame are checked before these are accepted.
_VIDEO_QUIZ_ROOTS = ("[data-video-quiz], .ans-videoquiz, .ans-videoquiz-container, #videoquiz, "
                     ".video-quiz, .videoquiz, .vjs-modal-dialog, [role=dialog]")
_VIDEO_QUESTION_BLOCKS = _QUESTION_BLOCKS + (".tk_topic", ".tkTopic", ".videoquiz-question")
_VIDEO_TITLES = _QUESTION_TITLES + ", .tk_title, .tk-title, .tkTopic_title, .topic-title"
_VIDEO_OPTIONS = (".tkitem, .tkItem, .tk-item, .tk-option, .videoquiz-options > li",) + _OPTION_SELECTORS
_VIDEO_CONTINUE = re.compile(r"^(?:继续学习|继续播放|返回视频)$")
_VIDEO_WRONG = re.compile(r"(?:回答|答案|作答)(?:不正确|错误)|答错(?:了)?|很遗憾.*(?:错误|答错)|(?:真|很)遗憾[，,\s]*再接再厉")
_VIDEO_RIGHT = re.compile(r"恭喜.*(?:答对|正确)|(?:回答|答案|作答)正确|答对了")

# Registered before page scripts, but inert outside the current video scope.
# Keep native event APIs, focus/visibility state and media methods untouched.
_POINTER_EXIT_BOOTSTRAP = """key => {
    const slot = Symbol.for(key);
    if (Object.prototype.hasOwnProperty.call(window, slot)) return;
    let active = false;
    const boundary = event => {
        if (active && event.isTrusted && event.relatedTarget === null
            && (event.type.startsWith('mouse') || event.pointerType === 'mouse'))
            event.stopImmediatePropagation();
    };
    for (const type of ['mouseout', 'mouseleave', 'pointerout', 'pointerleave'])
        window.addEventListener(type, boundary, {capture: true});
    Object.defineProperty(window, slot, {configurable: true,
        value: {setActive: value => {active = value === true;}}});
}"""

# Compact, single round-trip probes used while a transition is being polled.
# They keep question/answer structure out of the hot path: full text extraction
# still happens once when the page is actually answered.
_QUESTION_SELECTORS_JS = ("'[data-question-id], .TiMu, .questionLi, .question-item, "
                          ".question, fieldset, .examPaper_question, .singleQuesId'")
_TITLE_SELECTORS_JS = ("'.Zy_TItle, .question-title, .questionTitle, .stem, legend, "
                       ".mark_name, .qtContent'")
_OPTION_SELECTORS_JS = json.dumps(_OPTION_SELECTORS)
_TEXT_SELECTORS_JS = ("'textarea, input:not([type]), input[type=text], "
                      "[contenteditable=true], [contenteditable=plaintext-only], .blankItemInp, iframe'")
_NAV_SELECTOR_JS = ("'nav, aside, [role=navigation], .catalog, .course-outline, "
                    ".chapter-list, .sidebar, .directory, .toc, .outline'")
_PLAYER_SELECTOR_JS = ("'.video-js, .video-stage, .ans-video, .ans-video-player, "
                       ".video-container, .video-player, .player-container, [data-video-player], "
                       "[data-video-quiz], .ans-videoquiz, #videoquiz, .video-quiz, .videoquiz'")

_QUIZ_PROBE_SCRIPT = r"""(root) => {
    root = root || document;
    if (/\/modules\/video\//i.test(location.pathname)) return {rows: [], ready: false};
    const questionSelector = %s;
    const titleSelector = %s;
    const optionSelectors = %s;
    const textSelector = %s;
    const navSelector = %s;
    const playerSelector = %s;
    const visible = e => {
        const r = e.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return false;
        for (let n = e; n; n = n.parentElement) {
            const s = getComputedStyle(n);
            if (n.hidden || n.inert || s.display === 'none' || s.visibility === 'hidden'
                || s.visibility === 'collapse' || Number(s.opacity) === 0
                || s.contentVisibility === 'hidden') return false;
        }
        return true;
    };
    const clean = value => (value || '').replace(/\s+/g, ' ').trim();
    const nativeChoice = 'input[type=radio], input[type=checkbox]';
    const choiceControls = block => {
        // Match the full extractor's selector priority. A union double-counts
        // labels nested in custom option rows and misses plain input wrappers.
        for (const selector of optionSelectors) {
            const controls = [...block.querySelectorAll(selector)].slice(0, 30).filter(visible);
            if (controls.length >= 2) return controls;
        }
        const wrappers = [...block.querySelectorAll('label, li, div, p')].filter(e =>
            e.querySelectorAll(nativeChoice).length === 1 && clean(e.innerText));
        const controls = wrappers.filter(e => !wrappers.some(other =>
            other !== e && e.contains(other))).slice(0, 30).filter(visible);
        return controls.length >= 2 ? controls
            : [...block.querySelectorAll(nativeChoice)].slice(0, 30).filter(visible);
    };
    const excluded = '[role=toolbar], .edui-toolbar, .edui-dialog-container, '
        + '.edui-popup, .tox-toolbar, .tox-dialog, .cke_top, .cke_bottom';
    const textControls = (scope, depth = 0) => {
        let count = 0, uncertain = false;
        for (const control of [...scope.querySelectorAll(textSelector)].slice(0, 100)) {
            if (!visible(control) || control.closest(excluded)) continue;
            if (control.matches('.blankItemInp')) {
                // The preview activates this blank's initially hidden editor.
                // Keep the stable blank as one field, not its backing textarea.
                if (control.querySelector('.InpDIV') && control.querySelector('.textDIV')) count++;
                continue;
            }
            if (control.closest('.blankItemInp')) continue;
            if (control.tagName === 'IFRAME') {
                if (depth >= 4) continue;
                try {
                    const doc = control.contentDocument;
                    if (!doc) { uncertain = true; continue; }
                    const nested = textControls(doc, depth + 1);
                    count += nested.count;
                    uncertain = uncertain || nested.uncertain;
                } catch (_) { uncertain = true; }
                continue;
            }
            if (control.matches(':disabled') || control.readOnly
                || control.getAttribute('aria-disabled') === 'true'
                || control.getAttribute('aria-readonly') === 'true'
                || control.parentElement?.closest(
                    '[contenteditable=true], [contenteditable=plaintext-only]')) continue;
            count++;
        }
        return {count, uncertain};
    };
    const relativePath = element => {
        const base = root.nodeType === 9 ? root.documentElement : root;
        const path = [];
        for (let e = element; e !== base; e = e.parentElement) {
            if (!e || !e.parentElement) return null;
            path.unshift(':nth-child(' + ([...e.parentElement.children].indexOf(e) + 1) + ')');
        }
        return (root.nodeType === 9 ? 'html' : ':scope')
            + (path.length ? ' > ' + path.join(' > ') : '');
    };
    let found = [];
    for (const selector of questionSelector.split(',').map(s => s.trim())) {
        const candidates = [...root.querySelectorAll(selector)].slice(0, 300).filter(e =>
            visible(e) && !e.closest(navSelector) && !e.closest(playerSelector)
            && !e.querySelector(playerSelector));
        if (candidates.length) { found = candidates; break; }
    }
    if (!found.length && root.matches && root.matches(questionSelector)
        && visible(root) && !root.closest(navSelector) && !root.closest(playerSelector))
        found = [root];
    if (!found.length) {
        // A large reader/video page has no question marker. Gate the broad
        // container scan on the normalized scope text; this keeps navigation
        // polls away from walking every div in a document.
        if (!/(?:单选|多选|判断|填空|简答)题/.test(root.body ? root.body.innerText : (root.innerText || '')))
            return {rows: [], ready: false};
        // Unknown wrappers (plain article/panel with standard labels) are
        // recognised by question text and grouped controls, matching the full
        // extractor's fallback without one Playwright round trip per element.
        const nativeControls = nativeChoice + ', [role=radio], [role=checkbox]';
        const containers = [...root.querySelectorAll('div, section, article, fieldset, li')]
            .slice(0, 600).filter(e => visible(e) && !e.closest(navSelector)
                && !e.closest(playerSelector) && !e.querySelector(playerSelector));
        const matches = containers.map(e => {
            const body = (e.innerText || '').trim();
            if (e.querySelectorAll(nativeControls).length < 2
                && !(/(?:填空|简答)题/.test(body) && e.querySelector(textSelector))) return null;
            const title = e.querySelector(titleSelector + ', h2, h3, h4');
            if (!/(?:单选|多选|判断|填空|简答)题/.test(body)
                && !(title && title.innerText.trim())) return null;
            return e;
        }).filter(Boolean);
        found = matches.filter(e => !matches.some(other => other !== e && e.contains(other)));
    }
    if (!found.length) return {rows: [], ready: false};
    const blocks = found.filter(e => !found.some(other => other !== e && e.contains(other)));
    const pendingText = [];
    const rows = blocks.map((block, index) => {
        const title = block.querySelector(titleSelector);
        const bodyText = block.innerText || '';
        const stem = clean(title ? title.innerText : (bodyText.split('\n')[0] || bodyText));
        const id = block.getAttribute('data-question-id') || block.getAttribute('data-id')
            || block.getAttribute('id') || '';
        const textKind = stem.match(/(?:\[|【)\s*(填空|简答)题\s*(?:\]|】)|^(?:\d+[.、．:：)）]?\s*)?(填空|简答)题(?:\s|[:：])/);
        const textQuestion = !!textKind;
        const shortAnswer = textQuestion && textKind[0].includes('简答');
        const choices = textQuestion ? [] : choiceControls(block);
        const fields = textQuestion ? textControls(block) : {count: 0, uncertain: false};
        if (fields.uncertain) pendingText.push({index, path: relativePath(block), short: shortAnswer});
        const labels = choices.map(e => clean(e.innerText || e.getAttribute('aria-label')
            || e.getAttribute('value') || '')).filter(Boolean);
        const count = textQuestion ? fields.count : choices.length;
        const ready = !!stem && (textQuestion
            ? !fields.uncertain && (shortAnswer ? count === 1 : count > 0)
            : count >= 2 && labels.length === count);
        return [id, stem.slice(0, 200), textQuestion ? 'text' : 'choice', count,
                labels.slice(0, 12), ready ? 1 : 0];
    });
    return {rows, ready: rows.length > 0 && rows.every(row => row[5] === 1), pendingText};
}""" % (_QUESTION_SELECTORS_JS, _TITLE_SELECTORS_JS, _OPTION_SELECTORS_JS,
        _TEXT_SELECTORS_JS, _NAV_SELECTOR_JS, _PLAYER_SELECTOR_JS)

_VIDEO_PROBE_SCRIPT = r"""(e) => ({
    source: e.getAttribute('src')
        || [...e.querySelectorAll('source')].map(n => n.getAttribute('src')).join('|')
        || e.currentSrc || '',
    poster: e.getAttribute('poster') || '',
    id: e.id || ''
})"""

_RICH_TRAILING_BREAK_SCRIPT = r"""(root) => {
    const tail = node => {
        const children = [...node.childNodes];
        for (let i = children.length - 1; i >= 0; i--) {
            const child = children[i];
            if (child.nodeType === 3) {
                if (child.data.trim()) return /[\r\n]$/.test(child.data);
                if (/[\r\n]/.test(child.data)) return true;
                continue;
            }
            if (child.nodeType !== 1) continue;
            if (child.tagName === 'BR') return true;
            if (child.matches('p, div, li, section, article, blockquote')
                && !child.innerText.trim()
                && !child.querySelector('img, canvas, svg, video')) return true;
            return tail(child);
        }
        return false;
    };
    return tail(root);
}"""

_CAPTCHA_PROBE_SCRIPT = r"""() => {
    const selector = '#captcha, #captchaBox, .captcha-box, .geetest_panel, .nc-container, '
        + 'input[name*=captcha i], input[id*=captcha i], iframe[src*=captcha i], '
        + '[class*=verify-wrap], [id*=verify-wrap]';
    const visible = e => {
        const r = e.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return false;
        for (let n = e; n; n = n.parentElement) {
            const s = getComputedStyle(n);
            if (n.hidden || n.inert || s.display === 'none' || s.visibility === 'hidden'
                || s.visibility === 'collapse' || Number(s.opacity) === 0) return false;
        }
        return true;
    };
    return [...document.querySelectorAll(selector)].some(visible);
}"""

_VISIBLE_ELEMENT_PROBE = r"""(elements) => {
    const nav = %s;
    const player = %s;
    const visible = e => {
        const r = e.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return false;
        for (let n = e; n; n = n.parentElement) {
            const s = getComputedStyle(n);
            if (n.hidden || n.inert || s.display === 'none' || s.visibility === 'hidden'
                || s.visibility === 'collapse' || Number(s.opacity) === 0
                || s.contentVisibility === 'hidden') return false;
        }
        return true;
    };
    const rows = [];
    for (let index = 0; index < elements.length && rows.length < 200; index++) {
        const e = elements[index];
        if (!visible(e) || e.closest(nav) || e.closest(player) || e.querySelector(player)) continue;
        rows.push(index);
    }
    return rows;
}""" % (_NAV_SELECTOR_JS, _PLAYER_SELECTOR_JS)

_QUESTION_CONTAINER_PROBE = r"""(elements) => {
    const marker = /(?:单选|多选|判断|填空|简答)题/;
    const choices = 'input[type=radio], input[type=checkbox], [role=radio], [role=checkbox]';
    const texts = 'textarea, input:not([type]), input[type=text], [contenteditable=true], iframe, .blankItemInp';
    const titles = '.Zy_TItle, .question-title, .questionTitle, .stem, legend, .mark_name, .qtContent, h2, h3, h4';
    const nav = %s;
    const player = %s;
    const visible = e => {
        const r = e.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return false;
        for (let n = e; n; n = n.parentElement) {
            const s = getComputedStyle(n);
            if (n.hidden || n.inert || s.display === 'none' || s.visibility === 'hidden'
                || s.visibility === 'collapse' || Number(s.opacity) === 0
                || s.contentVisibility === 'hidden') return false;
        }
        return true;
    };
    const matches = elements.map((e, index) => {
        if (!visible(e) || e.closest(nav) || e.closest(player) || e.querySelector(player)) return null;
        const text = (e.innerText || '').trim();
        if (e.querySelectorAll(choices).length < 2
            && !(/(?:填空|简答)题/.test(text) && e.querySelector(texts))) return null;
        const title = e.querySelector(titles);
        if (!marker.test(text) && !(title && title.innerText.trim())) return null;
        return {element: e, index};
    }).filter(Boolean);
    return matches.filter(item => !matches.some(other => other !== item
        && item.element.contains(other.element))).slice(0, 200).map(item => item.index);
}""" % (_NAV_SELECTOR_JS, _PLAYER_SELECTOR_JS)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


class BrowserSession:
    VIDEO_QUIZ_WAIT_TIMEOUT = 15.0
    MAX_VIDEO_QUIZ_CORRECTIONS = 5
    VIDEO_START_TIMEOUT = 15.0
    PAUSED_QUIZ_GRACE = 1.0
    VIDEO_QUIZ_POLL_INTERVAL = 0.2
    NEXT_PAGE_TIMEOUT = 20.0
    NEXT_PAGE_SETTLE = 0.5
    # A completed page is confirmed by two clean scans. The gap between them
    # only needs to let footer/lazy content mount; waiting a full
    # poll_interval here added several seconds to every page.
    CONTENT_SETTLE = 0.5
    # Document bottoms still need several observations so that lazy pages can
    # grow, but the interval is independent of poll_interval.
    DOCUMENT_BOTTOM_SETTLE = 1.5
    DOCUMENT_BOTTOM_WAIT = 0.5

    def __init__(self, settings: AppSettings, paths: AppPaths,
                 log: Callable[[str], None], stop_event: threading.Event) -> None:
        self.settings, self.paths = settings, paths
        self.log, self.stop_event = log, stop_event
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._question_controls: dict[str, dict[str, Any]] = {}
        self._questions: dict[str, Question] = {}
        self._quiz_view_rows: dict[Any, tuple[float, list[Any]]] = {}
        self._quiz_target: Any = None
        self._content_nodes: list[tuple[Any, float, Any, str]] = []
        self._content_sequence = 0
        self._task_regions: list[dict[str, Any]] = []
        # Navigation probes run many times per transition. They deliberately
        # avoid the full answer/reading extraction and share per-iteration
        # results instead of walking every frame and question again.
        self._navigation_mode = False
        self._nav_content_tasks: dict[bool, list[ContentTask]] = {}
        self._nav_frames: list[Any] | None = None
        self._nav_frame_visibility: dict[int, bool] = {}
        self._nav_frame_body_text: dict[int, str] = {}
        self._nav_guard_ok = False
        self._nav_question_blocks: dict[int, list[Any]] = {}
        self._nav_quiz_probe: dict[int, dict[str, Any]] = {}
        self._nav_video_probe: dict[int, dict[str, Any]] = {}
        self._nav_document_probe: dict[int, dict[str, Any]] = {}
        self._nav_result_probe: dict[int, bool] = {}
        self._nav_has_quiz: bool | None = None
        self._nav_result_visible: dict[Any, bool] = {}
        self._pointer_guard_key = secrets.token_hex(24)
        self._pointer_guards: list[tuple[Any, float, Any]] = []
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
        env = self.paths.environment()
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
        env.update({"TMPDIR": temporary, "TMP": temporary, "TEMP": temporary,
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
            self._context.add_init_script(self._pointer_guard_script())
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
        if self._navigation_mode and self._nav_frames is not None:
            return self._nav_frames
        if self._page is None or self._page.is_closed():
            raise BrowserError("浏览器页面已关闭，请重新打开浏览器。")
        frames = [frame for frame in self._page.frames if not frame.is_detached()]
        if self._navigation_mode:
            self._nav_frames = frames
        return frames

    @staticmethod
    def _visible(locator: Any) -> bool:
        try:
            if not locator.is_visible():
                return False
            # Playwright's visibility excludes display:none but considers an
            # opacity-zero preloaded views visible. aria-hidden only controls
            # accessibility exposure; real visible answer lists use it too.
            evaluate = getattr(locator, "evaluate", None)
            if evaluate is not None:
                return bool(evaluate("""e => {
                    for(let n=e;n;n=n.parentElement){
                        const s=getComputedStyle(n);
                        if(n.hidden || n.inert
                           || s.visibility==='hidden' || s.visibility==='collapse'
                           || s.display==='none' || Number(s.opacity)===0
                           || s.contentVisibility==='hidden')return false;
                    }
                    return true;
                }"""))
            return True
        except Exception:
            return False

    def _first_visible(self, selector: str, *, root: Any = None) -> Any | None:
        for frame in [root] if root is not None else self._frames():
            if root is None and not self._frame_visible_memo(frame):
                continue
            try:
                candidates = frame.locator(selector)
                for index in range(min(candidates.count(), 100)):
                    locator = candidates.nth(index)
                    if self._visible(locator):
                        return locator
            except Exception:
                continue
        return None

    def _text_control(self, pattern: re.Pattern[str], *, root: Any = None, video_quiz: bool = False) -> Any | None:
        for frame in [root] if root is not None else self._frames():
            if root is None and not self._frame_visible_memo(frame):
                continue
            for selector in ("button, a, [role=button], [role=tab], input[type=submit], input[type=button]",
                             "li, span, div"):
                try:
                    candidates = frame.locator(selector).filter(has_text=pattern)
                    for index in range(min(candidates.count(), 120)):
                        loc = candidates.nth(index)
                        text = _clean(loc.inner_text(timeout=600)) or _clean(loc.get_attribute("value") or "")
                        if pattern.fullmatch(text) and (self._video_visible(loc) if video_quiz else self._visible(loc)):
                            if loc.evaluate("e => e.children.length > 3 && !['BUTTON','A'].includes(e.tagName)"):
                                continue
                            return loc
                except Exception:
                    continue
        return None

    def _captcha_present(self, frame: Any) -> bool:
        try:
            return bool(frame.evaluate(_CAPTCHA_PROBE_SCRIPT))
        except Exception:
            try:
                return self._first_visible(
                    "#captcha, #captchaBox, .captcha-box, .geetest_panel, .nc-container, "
                    "input[name*=captcha i], input[id*=captcha i], iframe[src*=captcha i], "
                    "[class*=verify-wrap], [id*=verify-wrap]", root=frame) is not None
            except Exception:
                return False

    def _guard(self) -> None:
        self._check_stop()
        if self._unexpected_dialog:
            message, self._unexpected_dialog = self._unexpected_dialog, ""
            raise BrowserError(f"网页弹窗需要人工处理：{message}")
        if self._navigation_mode and self._nav_guard_ok:
            return
        for frame in self._frames():
            if not self._frame_visible_memo(frame):
                continue
            login = re.search(r"(?:passport2?\.|/login(?:[/?]|$)|/loginpage|/sso/login)", frame.url, re.I)
            if login and frame.parent_frame is None:
                raise BrowserError("登录状态已失效，请手动登录后再开始。")
            captcha = self._captcha_present(frame)
            if captcha:
                raise BrowserError("检测到验证码或安全验证，请手动处理后再开始。")
            body = self._navigation_frame_body_text(frame) if self._navigation_mode else ""
            if not body:
                try:
                    body = frame.locator("body").inner_text(timeout=1000)
                except Exception:
                    continue
            if login and (_clean(body) or self._first_visible("input[type=password]", root=frame) is not None):
                raise BrowserError("登录状态已失效，请手动登录后再开始。")
            if re.search(r"访问过于频繁|请求过于频繁|账号异常|帐号异常|请完成安全验证|请先登录|登录已过期", body):
                raise BrowserError("网页要求人工处理登录、安全验证或访问异常。")
        if self._navigation_mode:
            self._nav_guard_ok = True

    @staticmethod
    def _frame_visible(frame: Any) -> bool:
        """Ignore dormant authentication/verification frames in page chrome."""
        try:
            while getattr(frame, "parent_frame", None) is not None:
                element = frame.frame_element()
                try:
                    if not BrowserSession._visible(element):
                        return False
                finally:
                    element.dispose()
                frame = frame.parent_frame
            return True
        except Exception:
            # A frame that detached while being inspected is no longer a
            # visible login or verification screen for the user to handle.
            return False

    def _frame_visible_memo(self, frame: Any) -> bool:
        """Cache visibility for one navigation iteration.

        Walking every frame's ancestor chain is one of the most frequently
        repeated calls during a transition. Frames cannot meaningfully change
        visibility within a single 0.25 s poll; the cache is reset before the
        next poll and when navigation ends.
        """
        if self._navigation_mode:
            key = id(frame)
            cached = self._nav_frame_visibility.get(key)
            if cached is not None:
                return cached
        visible = self._frame_visible(frame)
        if self._navigation_mode:
            self._nav_frame_visibility[id(frame)] = visible
        return visible

    def _reset_navigation_caches(self) -> None:
        """Forget per-poll probes without touching persistent task identity."""
        self._nav_content_tasks.clear()
        self._nav_frames = None
        self._nav_frame_visibility.clear()
        self._nav_frame_body_text.clear()
        self._nav_guard_ok = False
        self._nav_question_blocks.clear()
        self._nav_quiz_probe.clear()
        self._nav_video_probe.clear()
        self._nav_document_probe.clear()
        self._nav_result_probe.clear()
        self._nav_has_quiz = None
        self._nav_result_visible.clear()

    def _navigation_frame_body_text(self, frame: Any) -> str:
        """Read a frame body once per poll, shared by guard and warning probes."""
        key = id(frame)
        text = self._nav_frame_body_text.get(key)
        if text is not None:
            return text
        try:
            text = frame.locator("body").inner_text(timeout=700)
        except Exception:
            text = ""
        self._nav_frame_body_text[key] = text
        return text

    def _quiz_probe(self, root: Any) -> dict[str, Any]:
        """Read question count/readiness in one round trip.

        This intentionally returns only the compact data needed for a change
        signature. The complete question and option extraction still runs in
        ``get_questions`` when the page is actually answered.
        """
        key = id(root)
        if self._navigation_mode:
            cached = self._nav_quiz_probe.get(key)
            if cached is not None:
                return cached
        try:
            raw = root.evaluate(_QUIZ_PROBE_SCRIPT)
        except Exception:
            raw = None
        if not isinstance(raw, dict):
            raw = {"rows": [], "ready": False}
        rows = raw.get("rows") if isinstance(raw.get("rows"), list) else []
        pending = raw.get("pendingText")
        if isinstance(pending, list) and pending:
            # DOM JS cannot read cross-origin editor frames. Resolve only those
            # exact question blocks through Playwright; ordinary polls remain
            # one compact evaluate, without a full per-question extraction.
            for item in pending:
                if not isinstance(item, dict):
                    continue
                index, path = item.get("index"), item.get("path")
                if (not isinstance(index, int) or index < 0 or index >= len(rows)
                        or not isinstance(path, str) or not path
                        or not isinstance(rows[index], list) or len(rows[index]) < 6):
                    continue
                self._check_stop()
                try:
                    block = root if path == ":scope" else root.locator(path)
                    fields = self._text_answer_controls(block)
                    count = len(fields)
                    rows[index][3] = count
                    rows[index][5] = int(bool(rows[index][1]) and (
                        count == 1 if item.get("short") else count > 0))
                except StopRequested:
                    raise
                except Exception:
                    rows[index][5] = 0
            raw["ready"] = bool(rows) and all(
                isinstance(row, list) and len(row) >= 6 and row[5] == 1 for row in rows)
        ready = bool(raw.get("ready")) and bool(rows)
        payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        result = {
            "rows": rows,
            "ready": ready,
            "signature": hashlib.sha256(payload.encode()).hexdigest()[:24],
        }
        if self._navigation_mode:
            self._nav_quiz_probe[key] = result
        return result

    def _document_probe(self, target: Any) -> dict[str, Any]:
        """Read document text length/hash and image presence in one call."""
        key = id(target)
        cached = self._nav_document_probe.get(key)
        if cached is not None:
            return cached
        try:
            raw = self._document_semantic(target, digest=True)
        except Exception:
            raw = None
        if not isinstance(raw, dict):
            raw = {"text": "", "length": 0, "hash": "", "media": 0}
        result = {
            "text": raw.get("text") if isinstance(raw.get("text"), str) else "",
            "length": raw.get("length") if isinstance(raw.get("length"), int) else 0,
            "hash": raw.get("hash") if isinstance(raw.get("hash"), str) else "",
            "media": raw.get("media") if isinstance(raw.get("media"), int) else 0,
        }
        result["signature"] = hashlib.sha256(
            json.dumps([result["length"], result["hash"], result["media"]],
                       ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()[:24]
        self._nav_document_probe[key] = result
        return result

    def _video_probe(self, video: Any) -> dict[str, Any]:
        key = id(video)
        cached = self._nav_video_probe.get(key)
        if cached is not None:
            return cached
        try:
            raw = video.evaluate(_VIDEO_PROBE_SCRIPT)
        except Exception:
            raw = None
        if not isinstance(raw, dict):
            raw = {"source": "", "poster": "", "id": ""}
        result = {
            "source": raw.get("source") if isinstance(raw.get("source"), str) else "",
            "poster": raw.get("poster") if isinstance(raw.get("poster"), str) else "",
            "id": raw.get("id") if isinstance(raw.get("id"), str) else "",
        }
        self._nav_video_probe[key] = result
        return result

    def _result_probe_cached(self, root: Any) -> bool:
        if not self._navigation_mode:
            return self._frame_result_visible(root)
        key = id(root)
        cached = self._nav_result_probe.get(key)
        if cached is not None:
            return cached
        result = self._frame_result_visible(root)
        self._nav_result_probe[key] = result
        return result

    def _warning_text_present(self) -> bool:
        """Cheap title-text gate before running the full prompt locator scan."""
        for frame in self._frames():
            if not self._frame_visible_memo(frame):
                continue
            text = self._navigation_frame_body_text(frame)
            if text and _NEXT_TASK_WARNING.search(re.sub(r"\s+", " ", text)):
                return True
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
                if frame.is_detached() or not self._frame_visible_memo(frame):
                    continue
                parts = urlparse(frame.url)
                if _CHAPTER_URL.search(parts.path):
                    return True
                query = {key.lower(): value for key, value in parse_qs(parts.query).items()}
                identified = identified or bool(query.get("chapterid") or query.get("knowledgeid"))
                if self._first_visible(
                        ".ans-attach-online, .TiMu, [data-question-id], .chapter-content", root=frame) is not None:
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
            self._clear_content_nodes()
            self._clear_task_regions()
            self._page, self._mouse = page, (0.0, 0.0)
            self._question_controls.clear()
            self._questions.clear()
            self._quiz_view_rows.clear()
            if self._navigation_mode:
                self._reset_navigation_caches()
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

    def _chapter_identifier(self) -> str:
        self._guard()
        for frame in self._frames():
            if not self._frame_visible_memo(frame):
                continue
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

    def chapter_key(self) -> str:
        """Stable activity identity, including quiz cards in the same chapter.

        A chapter URL alone is insufficient: 下一节 can replace only the inner
        activity document, or replace its DOM without changing any URL.
        Media clocks, scrolling and answer selection are absent from this key.
        """
        kind = self.chapter_kind()
        signature, _ready = self._view_signature(kind)
        return f"{self._chapter_identifier()}|{kind}|{signature}"

    def _view_signature(self, kind: str, *, include_occluded: bool = False) -> tuple[str, bool]:
        """Describe the actual content, excluding outer navigation and status.

        Only semantic content changes count as a new activity. Updating the
        outer URL/title while an old iframe is still on screen must not release
        the transition wait. Readiness follows the first pending task; later
        attachments can finish loading before their own turn.
        """
        if self._navigation_mode:
            return self._navigation_view_signature(kind, include_occluded=include_occluded)
        parts: list[Any] = []
        ready = True
        if kind in {"video", "document", "mixed"}:
            # The page identity describes all of its content even when its
            # first recognised kind is video. Use structural order for this
            # stable signature, rather than viewport/scroll coordinates.
            content = self.content_tasks(include_occluded=True) if include_occluded else self.content_tasks()
            tasks = sorted(content, key=lambda task: tuple(segment[2] for segment in task.order))
            pending_ready: dict[str, bool] = {}
            for task in tasks:
                if task.region:
                    parts.append(("task-region", task.kind, task.region))
                    if task.completed or not task.required:
                        continue
                    if task.placeholder:
                        if task.kind == "quiz" and self._unpublished_quiz(task.target):
                            continue
                        pending_ready[task.identity] = False
                        continue
                if task.kind == "quiz":
                    rows, quiz_ready = self._quiz_material(task.target)
                    if not task.region:
                        parts.append(("quiz", rows))
                    pending_ready[task.identity] = quiz_ready
                    continue
                if task.kind == "video":
                    state = task.target.evaluate("""e => ({
                        source:e.getAttribute('src') ||
                            [...e.querySelectorAll('source')].map(n=>n.getAttribute('src')).join('|') || e.currentSrc,
                        poster:e.getAttribute('poster') || '', id:e.id || ''
                    })""")
                    source = "" if state["source"].startswith("blob:") else state["source"]
                    if not task.region:
                        parts.append(("video", source, state["poster"], state["id"]))
                    # Some players attach their media only after a real play
                    # click. A mounted player is enough for navigation; the
                    # playback workflow handles loading after that click.
                    pending_ready[task.identity] = True
                else:
                    data = self._document_semantic(task.target)
                    if re.fullmatch(r"(?:正在)?加载(?:中)?[.。…\s]*|请稍[候等][.。…\s]*", data["text"]):
                        pending_ready[task.identity] = False
                        continue
                    if data["text"] or data["media"]:
                        if not task.region:
                            parts.append(("document", data["text"], data["media"]))
                    pending_ready[task.identity] = bool(data["text"] or data["media"])
            # Later readers may load only after the preceding video finishes
            # or after they scroll into view. They cannot hold navigation to
            # an already usable first task; execution handles tasks in order.
            ready = next((pending_ready[task.identity] for task in content
                          if task.identity in pending_ready), bool(tasks))
        elif kind == "quiz":
            for frame in self._frames():
                if not self._frame_visible_memo(frame):
                    continue
                origin = frame.evaluate("performance.timeOrigin")
                cached = self._quiz_view_rows.get(frame)
                submitted = self._frame_result_visible(frame)
                if submitted and cached and cached[0] == origin:
                    parts.append(("quiz", cached[1]))
                    continue
                rows, questions_ready = self._quiz_rows(frame)
                if rows:
                    ready = ready and (submitted or questions_ready)
                    self._quiz_view_rows[frame] = (origin, rows)
                    parts.append(("quiz", rows))
                elif submitted:
                    # Some submitted views replace their question DOM entirely.
                    # Retain the known questions' identity within this document
                    # so submitting does not turn it into a different activity.
                    parts.append(("quiz", cached[1]) if cached and cached[0] == origin
                                 else ("quiz-result",))
        if not parts:
            return "", False
        payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()[:24], ready

    def _navigation_view_signature(self, kind: str, *,
                                   include_occluded: bool = False) -> tuple[str, bool]:
        """Cheap change signature used only while polling a transition.

        Full question text, option labels and reader text are still extracted
        when the page is answered or read. Here one compact probe per task is
        enough to answer "did the material change and is it usable yet?".
        """
        parts: list[Any] = []
        ready = True
        if kind in {"video", "document", "mixed"}:
            content = self.content_tasks(include_occluded=include_occluded, _cached=True)
            tasks = sorted(content, key=lambda task: tuple(segment[2] for segment in task.order))
            pending_ready: dict[str, bool] = {}
            for task in tasks:
                try:
                    if task.region:
                        parts.append(("task-region", task.kind, task.region))
                        if task.completed or not task.required:
                            continue
                        if task.placeholder:
                            if task.kind == "quiz" and self._unpublished_quiz(task.target):
                                continue
                            pending_ready[task.identity] = False
                            continue
                    if task.kind == "quiz":
                        probe = self._quiz_probe(task.target)
                        if not task.region:
                            parts.append(("quiz", probe["signature"]))
                        pending_ready[task.identity] = bool(probe["ready"])
                        continue
                    if task.kind == "video":
                        state = self._video_probe(task.target)
                        source = "" if state["source"].startswith("blob:") else state["source"]
                        if not task.region:
                            parts.append(("video", source, state["poster"], state["id"]))
                        # A mounted player is enough for navigation. Media can be
                        # attached only after the real playback click.
                        pending_ready[task.identity] = True
                    else:
                        data = self._document_probe(task.target)
                        if re.fullmatch(r"(?:正在)?加载(?:中)?[.。…\s]*|请稍[候等][.。…\s]*", data["text"]):
                            pending_ready[task.identity] = False
                            continue
                        if data["text"] or data["media"]:
                            if not task.region:
                                parts.append(("document", data["signature"]))
                        pending_ready[task.identity] = bool(data["text"] or data["media"])
                except BrowserError:
                    raise
                except Exception:
                    # The target was replaced while the poll was reading it.
                    pending_ready[task.identity] = False
                    continue
            ready = next((pending_ready[task.identity] for task in content
                          if task.identity in pending_ready), bool(tasks))
        elif kind == "quiz":
            for frame in self._frames():
                try:
                    if not self._frame_visible_memo(frame):
                        continue
                    probe = self._quiz_probe(frame)
                    submitted = self._result_probe_cached(frame)
                    rows = probe["rows"]
                    if rows:
                        ready = ready and (submitted or probe["ready"])
                        parts.append(("quiz", probe["signature"]))
                    elif submitted:
                        parts.append(("quiz-result",))
                except BrowserError:
                    raise
                except Exception:
                    continue
        if not parts:
            return "", False
        payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()[:24], ready

    @staticmethod
    def _document_semantic(target: Any, *, digest: bool = False) -> dict[str, Any]:
        return target.evaluate("""(e, {warningPattern, dialogs, digest}) => {
                    const chrome='button, input, select, textarea, script, style, nav, .ans-job-icon, [role=status], '
                        + '[role=progressbar], .status, .progress, .toolbar, .reader-toolbar, ' + dialogs;
                    const warning=new RegExp(warningPattern), warnings=[];
                    const text=n=>(n.innerText || n.textContent || '').replace(/\\s+/g,' ').trim();
                    const actions='button,a,[role=button],input[type=button],input[type=submit],li,span,div';
                    // Match the same minimal floating prompt in custom skins.
                    // Its appearance/disappearance is chrome, not new reading.
                    for(const go of e.querySelectorAll(actions)){
                        if(text(go)!=='去学习')continue;
                        for(let p=go.parentElement;p && e.contains(p) && !p.matches('body,html');p=p.parentElement){
                            if(!warning.test(text(p)))continue;
                            if(![...p.querySelectorAll(actions)].some(n=>/^下[一1]节(?:\\s*[>›»])?$/.test(text(n))))continue;
                            let floating=false;
                            for(let n=p;n && !n.matches('body,html');n=n.parentElement){
                                if(['fixed','absolute'].includes(getComputedStyle(n).position)){floating=true;break;}
                            }
                            if(floating){warnings.push(p);break;}
                        }
                    }
                    const excluded=n=>n.closest(chrome) || warnings.some(root=>root.contains(n));
                    const visible=n=>{
                        for(let p=n;p;p=p.parentElement){
                            const s=getComputedStyle(p);
                            if(p.hidden || p.inert
                               || s.display==='none' || s.visibility==='hidden' || Number(s.opacity)===0)return false;
                        }
                        return true;
                    };
                    const texts=[],walker=document.createTreeWalker(e,NodeFilter.SHOW_TEXT);
                    for(let n=walker.nextNode();n;n=walker.nextNode()){
                        if(n.parentElement && !excluded(n.parentElement) && visible(n.parentElement))texts.push(n.textContent);
                    }
                    const content=texts.join(' ').replace(/\\s+/g,' ').trim();
                    const media=[...e.querySelectorAll('img, canvas, svg')].filter(n=>visible(n) && !excluded(n));
                    if(digest){
                        let hash=2166136261;
                        for(let i=0;i<content.length;i++){
                            hash^=content.charCodeAt(i);hash=Math.imul(hash,16777619);
                        }
                        return {text:content.slice(0,300), length:content.length,
                                hash:(hash>>>0).toString(16), media:media.length};
                    }
                    return {
                        text:content,
                        media:media.map(n=>[
                            n.tagName, n.getAttribute('src') || '', n.getAttribute('aria-label') || '',
                            n.getAttribute('width') || '', n.getAttribute('height') || ''])
                    };
                }""", {"warningPattern": _NEXT_TASK_WARNING.pattern, "dialogs": _NEXT_WARNING_ROOTS,
                       "digest": digest})

    def _content_document_tokens(self, kind: str, *, include_occluded: bool = False) -> tuple[Any, ...]:
        """Navigation evidence when two chapters reuse identical material.

        This is deliberately absent from chapter_key: returning to an already
        processed page must still be caught by Workflow's visited set.
        """
        tokens = []
        if kind in {"video", "document", "mixed"}:
            try:
                videos = self._video_locators(include_occluded=True) if include_occluded else self._video_locators()
                roots = videos + self._document_targets() + self._chapter_quiz_targets()
            except BrowserError:
                raise
            except Exception:
                roots = []
        elif kind == "quiz":
            roots = []
            for frame in self._frames():
                try:
                    if not self._frame_visible_memo(frame):
                        continue
                    if self._question_blocks(frame) or self._frame_result_visible(frame):
                        tokens.append((id(frame), frame.evaluate("performance.timeOrigin")))
                except BrowserError:
                    raise
                except Exception:
                    continue
        else:
            roots = []
        for root in roots:
            try:
                handle = root.element_handle(timeout=1000)
                if handle is None:
                    continue
                try:
                    frame = handle.owner_frame()
                    if frame is not None:
                        # Blob URLs are ephemeral and excluded from the stable
                        # visited key. Their replacement is useful navigation
                        # evidence for SPA players that keep the same document.
                        resource = root.evaluate("e => e.tagName==='VIDEO' ? (e.currentSrc || e.getAttribute('src') || '') : ''")
                        tokens.append((id(frame), frame.evaluate("performance.timeOrigin"), resource))
                finally:
                    handle.dispose()
            except BrowserError:
                raise
            except Exception:
                continue
        return tuple(sorted(set(tokens)))

    def _video_locators(self, *, include_occluded: bool = False) -> list[Any]:
        result = []
        for frame in self._frames():
            try:
                if not self._frame_visible_memo(frame):
                    continue
                videos = frame.locator("video")
                for index in range(videos.count()):
                    video = videos.nth(index)
                    visible = (self._video_visible(video, player=True, include_occluded=True)
                               if include_occluded else self._video_visible(video, player=True))
                    if visible:
                        result.append(video)
            except Exception:
                continue
        return result

    def chapter_kind(self) -> str:
        self._guard()
        tasks = self.content_tasks(_cached=self._navigation_mode)
        quizzes = [task for task in tasks if task.kind == "quiz"]
        if any(task.region and task.placeholder for task in quizzes):
            return "mixed"
        if (len(quizzes) > 1 or (any(task.region for task in quizzes)
                and any(task.kind in {"video", "document"} for task in tasks))):
            return "mixed"
        videos = [task.target for task in tasks if task.kind == "video"]
        if videos:
            # Completed/optional players must not mask a separate assessment.
            # Keep pending playback and its overlays in the video flow. The
            # task already carries the region state, so do not walk the
            # attachment card again for every player.
            unfinished = [task.target for task in tasks
                          if task.kind == "video" and task.required and not task.completed]

            def safe_ended(video: Any) -> bool:
                try:
                    return bool(video.evaluate("v => v.ended"))
                except BrowserError:
                    raise
                except Exception:
                    return False

            def safe_video_quiz(video: Any) -> bool:
                try:
                    return self._find_video_quiz(video) is not None
                except BrowserError:
                    raise
                except Exception:
                    return False

            finished = all(safe_ended(video) for video in unfinished)
            if not finished or any(safe_video_quiz(video) for video in unfinished):
                return "video"
        if quizzes or self._has_quiz() or self._result_visible():
            return "quiz"
        if videos:
            return "video"
        if any(task.kind == "document" for task in tasks):
            return "document"
        if not tasks and self._document_targets():
            return "document"
        if tasks:
            return tasks[0].kind
        return "unknown"

    def _clear_content_nodes(self) -> None:
        nodes, self._content_nodes = self._content_nodes, []
        for _frame, _origin, handle, _identity in nodes:
            try:
                handle.dispose()
            except Exception:
                pass

    def _clear_task_regions(self) -> None:
        regions, self._task_regions = self._task_regions, []
        for region in regions:
            try:
                region["node"].dispose()
            except Exception:
                pass

    def _task_region(self, target: Any) -> tuple[Any, Any] | None:
        """Find only this target's nearest attachment card, across frames."""
        current = target.element_handle(timeout=1500)
        while current is not None:
            try:
                frame = current.owner_frame()
                region = current.evaluate_handle("e => e.closest('.ans-attach-ct')")
                element = region.as_element()
                if element is not None:
                    return element, frame
                region.dispose()
                if frame is None or frame.parent_frame is None:
                    return None
                parent = frame.frame_element()
            finally:
                current.dispose()
            current = parent
        return None

    @staticmethod
    def _region_state(region: Any) -> dict[str, Any]:
        return region.evaluate(r"""card => {
            const visible=e=>{
                const r=e.getBoundingClientRect();if(r.width<=0||r.height<=0)return false;
                for(let n=e;n;n=n.parentElement){const s=getComputedStyle(n);
                    if(n.hidden||n.inert||s.display==='none'||s.visibility==='hidden'
                       ||s.contentVisibility==='hidden'||Number(s.opacity)===0)return false;}
                return true;
            };
            const ownMarkers=[...card.querySelectorAll('.ans-job-icon')].filter(e=>
                e.closest('.ans-attach-ct')===card);
            const markers=ownMarkers.filter(visible);
            const labels=markers.flatMap(e=>[e.getAttribute('aria-label'),e.getAttribute('title'),
                e.getAttribute('data-original-title'),e.innerText].map(s=>(s||'').replace(/\s+/g,' ').trim()));
            // Static PDF cards can paint an empty icon entirely through this
            // owner class. Require the card's own visible icon, and let an
            // explicit unfinished label override a stale completed class.
            const completed=labels.includes('任务点已完成')
                || (card.classList.contains('ans-job-finished')&&markers.length>0);
            const attachments=[...card.querySelectorAll('iframe,video,embed,object')]
                .filter(e=>e.closest('.ans-attach-ct')===card);
            const video=attachments.some(e=>e.tagName==='VIDEO'||/ans-insertvideo|modules\/video\//i.test(
                (e.className||'')+' '+(e.getAttribute('src')||'')));
            const quiz=attachments.some(e=>/\/(?:work|quiz|exam|homework)(?:\/|[.])|doHomeWork/i.test(e.getAttribute('src')||''));
            const keys=['objectid','objectId','jobid','jobId','fileid','fileId','resourceid','resourceId','aid'];
            const ids=[];
            for(const e of [card,...attachments]){
                for(const key of ['data-resource-id','data-objectid','data-object-id','data-fileid','data-file-id','objectid','jobid']){
                    const value=e.getAttribute(key);if(value)ids.push([key,value]);
                }
                try{const data=JSON.parse(e.getAttribute('data')||'{}');
                    for(const key of keys)if(data[key]!==undefined&&data[key]!==null)ids.push([key,String(data[key])]);
                }catch{}
            }
            const sources=attachments.map(e=>{
                const raw=e.getAttribute('src')||(['EMBED','OBJECT'].includes(e.tagName)?e.getAttribute('data'):'')||'';
                if(!raw||raw==='about:blank'||raw.startsWith('blob:'))return '';
                try{const url=new URL(raw,location.href);
                    if(/\/ananas\/modules\/(?:video|pdf|ppt|doc)\/index\.html/i.test(url.pathname))return '';
                    for(const key of [...url.searchParams.keys()])if(/^(?:_|v|t|ts|timestamp|enc|token|expires|signature|isPassed|progress|completed)$/i.test(key))url.searchParams.delete(key);
                    return url.origin+url.pathname+url.search;
                }catch{return '';}
            }).filter(Boolean);
            const path=[];for(let n=card;n&&n.parentElement;n=n.parentElement)path.unshift([...n.parentElement.children].indexOf(n));
            return {required:ownMarkers.length>0, completed:completed&&!labels.includes('任务点未完成'),
                kind:video?'video':quiz?'quiz':'document',ids,sources,path};
        }""")

    def _task_marker_state(self, target: Any) -> dict[str, Any] | None:
        self._check_stop()
        owner = self._task_region(target)
        if owner is None:
            return None
        region, _frame = owner
        try:
            return self._region_state(region)
        finally:
            region.dispose()

    def task_completed(self, target: Any) -> bool:
        state = self._task_marker_state(target)
        return bool(state and state["completed"])

    def task_required(self, target: Any) -> bool:
        state = self._task_marker_state(target)
        # Unknown layouts retain the existing adapter fallback. Only a known
        # attachment without its own job icon confirms an optional resource.
        return state is None or bool(state["required"])

    def _task_skip_reason(self, target: Any) -> str:
        state = self._task_marker_state(target)
        if state is not None:
            if not state["required"]:
                return "未标记为任务点"
            if state["completed"]:
                return "任务点已完成"
        return ""

    def _inactive_video_region(self, target: Any) -> bool:
        state = self._task_marker_state(target)
        return bool(state and (state["completed"] or not state["required"]) and state["kind"] == "video")

    def _task_region_info(self, target: Any, *, placeholder: bool = False) -> dict[str, Any] | None:
        try:
            owner = self._task_region(target)
        except BrowserError:
            raise
        except Exception:
            return None
        if owner is None:
            return None
        region, frame = owner
        retained = False
        try:
            state = self._region_state(region)
            origin = frame.evaluate("performance.timeOrigin")
            inner = None
            if not placeholder:
                if state["kind"] == "video":
                    inner = target.evaluate(r"""e => {
                        const card=e.closest('.ans-attach-ct'),scope=card||document;
                        const normalize=raw=>{
                            if(!raw||raw.startsWith('blob:'))return '';
                            try{const url=new URL(raw,location.href);
                                for(const key of [...url.searchParams.keys()])if(/^(?:_|v|t|ts|timestamp|enc|token|expires|signature|isPassed|progress|completed)$/i.test(key))url.searchParams.delete(key);
                                return url.origin+url.pathname+url.search;
                            }catch{return raw;}
                        };
                        return [...scope.querySelectorAll('video')]
                            .filter(v=>v.closest('.ans-attach-ct')===card)
                            .map(v=>[normalize(v.getAttribute('src')||v.currentSrc||''),v.getAttribute('poster')||'']);
                    }""")
                elif state["kind"] == "quiz":
                    inner, _ready = self._quiz_material(target)
                else:
                    scope = target.evaluate_handle("e => e.closest('.ans-attach-ct') || document.body")
                    try:
                        inner = self._document_semantic(scope)
                    finally:
                        scope.dispose()
            # Job/owner IDs alone cannot identify the actual material. Include
            # the scoped media/text as well, never a sibling task's resources.
            material = [state["ids"], state["sources"], inner]
            inner_known = bool(inner)
            if state["kind"] == "video":
                inner_known = bool(inner and any(source for source, _poster in inner))
            elif isinstance(inner, dict):
                inner_known = bool(inner["text"] or inner["media"]) and not bool(re.fullmatch(
                    r"(?:正在)?加载(?:中)?[.。…\s]*|请稍[候等][.。…\s]*", inner["text"]))
            fingerprint = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            outer = hashlib.sha256(json.dumps([state["ids"], state["sources"]], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            outer_known = bool(state["ids"] or state["sources"])
            entries = [entry for entry in self._task_regions if entry["frame"] is frame and entry["origin"] == origin]
            index = region.evaluate("(e,nodes)=>nodes.findIndex(node=>node===e)", [entry["node"] for entry in entries]) if entries else -1
            if index >= 0:
                entry = entries[index]
                outer_changed = outer_known and entry["outer_known"] and outer != entry["outer"]
                if outer_changed:
                    # Explicitly changing an attachment's resource is a new
                    # material even when both old and new remain unmounted.
                    entry["material"] = fingerprint
                    entry["descriptor"] = hashlib.sha256((entry["base"] + fingerprint).encode()).hexdigest()[:24]
                    entry["was_placeholder"] = placeholder
                    entry["inner_known"] = inner_known
                elif (entry["was_placeholder"] or not entry["inner_known"]) and not placeholder and inner_known:
                    # First mounting of the same completed attachment is not
                    # navigation. Adopt its real resource while keeping key.
                    entry["material"] = fingerprint
                    entry["was_placeholder"] = False
                elif fingerprint != entry["material"] and not placeholder and inner_known:
                    entry["material"] = fingerprint
                    entry["descriptor"] = hashlib.sha256((entry["base"] + fingerprint).encode()).hexdigest()[:24]
                if inner_known:
                    entry["inner_known"] = True
                if outer_known:
                    entry["outer"], entry["outer_known"] = outer, True
                descriptor = entry["descriptor"]
            else:
                lineage = [state["path"]]
                ancestor = frame
                while ancestor.parent_frame is not None:
                    element = ancestor.frame_element()
                    try:
                        lineage.insert(0, element.evaluate("""e=>{const p=[];for(let n=e;n&&n.parentElement;n=n.parentElement)p.unshift([...n.parentElement.children].indexOf(n));return p;}"""))
                    finally:
                        element.dispose()
                    ancestor = ancestor.parent_frame
                base = json.dumps([state["kind"], lineage], separators=(",", ":"))
                descriptor = hashlib.sha256((base + fingerprint).encode()).hexdigest()[:24]
                self._task_regions.append({"frame": frame, "origin": origin, "node": region,
                    "base": base, "material": fingerprint, "descriptor": descriptor,
                    "was_placeholder": placeholder, "outer": outer, "outer_known": outer_known,
                    "inner_known": inner_known})
                retained = True
            return {"completed": state["completed"], "required": state["required"],
                    "kind": state["kind"], "descriptor": descriptor}
        except BrowserError:
            raise
        except Exception:
            # The attachment card or its frame disappeared while it was being
            # inspected. Treat it as unowned for this scan; the next discovery
            # pass reads the current DOM.
            return None
        finally:
            if not retained:
                region.dispose()

    def _content_identity(self, target: Any, kind: str) -> str:
        handle = target.element_handle(timeout=2000)
        if handle is None:
            raise BrowserError("学习内容已经变化，无法确认当前任务。")
        retained = False
        try:
            frame = handle.owner_frame()
            if frame is None:
                raise BrowserError("无法确定学习内容所在页面。")
            origin = frame.evaluate("performance.timeOrigin")
            candidates = [(old, identity) for owner, stamp, old, identity in self._content_nodes
                          if owner is frame and stamp == origin and identity.startswith(kind + ":")]
            if candidates:
                try:
                    index = handle.evaluate("(e,nodes) => nodes.findIndex(node => node===e)",
                                            [old for old, _identity in candidates])
                except Exception:
                    # Retained handles from a replaced document cannot be
                    # compared; issue a fresh identity for the current node.
                    index = -1
                if index >= 0:
                    return candidates[index][1]
            self._content_sequence += 1
            identity = f"{kind}:{self._content_sequence}"
            self._content_nodes.append((frame, origin, handle, identity))
            retained = True
            return identity
        finally:
            if not retained:
                handle.dispose()

    def _content_order(self, target: Any) -> tuple[Any, ...]:
        """Order frame subtrees by their embedding card, then their contents."""
        describe = """e => {
            const r=e.getBoundingClientRect(), path=[];
            let x=r.left+window.scrollX, y=r.top+window.scrollY;
            for(let n=e;n&&n.parentElement;n=n.parentElement){
                path.unshift([...n.parentElement.children].indexOf(n));
                if(n.parentElement!==document.scrollingElement){
                    x+=n.parentElement.scrollLeft;y+=n.parentElement.scrollTop;
                }
            }
            return [y,x,path];
        }"""
        handle = target.element_handle(timeout=1500)
        if handle is None:
            raise BrowserError("学习内容已经变化。")
        try:
            frame = handle.owner_frame()
            local = target.evaluate(describe)
            order = [(local[0], local[1], tuple(local[2]))]
            while frame is not None and frame.parent_frame is not None:
                element = frame.frame_element()
                try:
                    local = element.evaluate(describe)
                    order.insert(0, (local[0], local[1], tuple(local[2])))
                finally:
                    element.dispose()
                frame = frame.parent_frame
            return tuple(order)
        finally:
            handle.dispose()

    def _assessment_target(self, block: Any) -> Any:
        owner = block.locator("xpath=ancestor-or-self::*[contains(concat(' ',normalize-space(@class),' '),"
                              "' ans-attach-ct ') or @data-chapter-quiz][1]")
        if owner.count():
            return owner.first
        return self._video_frame(block).locator("body")

    def _chapter_quiz_targets(self) -> list[Any]:
        if self._navigation_mode:
            return self._chapter_quiz_targets_fast()
        targets: dict[str, Any] = {}
        for frame in self._frames():
            if not self._frame_visible_memo(frame):
                continue
            blocks = self._question_blocks(frame)
            for block in blocks:
                target = self._assessment_target(block)
                targets[self._content_identity(target, "quiz")] = target
            if self._frame_result_visible(frame):
                results = frame.locator(".submit-success, #submission-result, .answerResult, .workStatus, .Py_answer, .mark_answer")
                found = False
                for index in range(min(results.count(), 60)):
                    result = results.nth(index)
                    if not self._visible(result):
                        continue
                    target = self._assessment_target(result)
                    targets[self._content_identity(target, "quiz")] = target
                    found = True
                if not blocks and not found:
                    target = frame.locator("body")
                    targets[self._content_identity(target, "quiz")] = target
        return list(targets.values())

    def _chapter_quiz_targets_fast(self) -> list[Any]:
        """Navigation-only discovery: one body target for a standalone quiz.

        An assessment frame normally contains many questions under one body.
        Looking up and fingerprinting the same owner once per question made a
        10-question page several times slower than the page itself.
        """
        targets: dict[str, Any] = {}
        for frame in self._frames():
            try:
                if not self._frame_visible_memo(frame):
                    continue
                probe = self._quiz_probe(frame)
                submitted = self._result_probe_cached(frame)
                if not probe["rows"] and not submitted:
                    continue
                if probe["rows"] and self._root_has_task_cards(frame):
                    blocks = self._question_blocks(frame)
                    if blocks:
                        for block in blocks:
                            target = self._assessment_target(block)
                            targets[self._content_identity(target, "quiz")] = target
                        continue
                target = frame.locator("body")
                targets[self._content_identity(target, "quiz")] = target
            except BrowserError:
                raise
            except Exception:
                # The frame can detach between the fast click and this poll.
                continue
        return list(targets.values())

    def _quiz_roots_for(self, target: Any) -> list[Any]:
        roots = [target]
        handle = target.element_handle(timeout=1500)
        if handle is None:
            # Locators re-resolve the same assessment after a DOM replacement.
            # An empty transition must not widen the scope to sibling quizzes.
            return []
        try:
            owner = handle.owner_frame()
            if owner is None:
                return []
            for frame in self._frames():
                try:
                    ancestor = frame
                    while ancestor.parent_frame is not None and ancestor.parent_frame is not owner:
                        ancestor = ancestor.parent_frame
                    if ancestor.parent_frame is owner:
                        element = ancestor.frame_element()
                        try:
                            if handle.evaluate("(root,child) => root===child || root.contains(child)", element):
                                roots.append(frame)
                        finally:
                            element.dispose()
                except BrowserError:
                    raise
                except Exception:
                    # A detaching sibling cannot invalidate the current quiz.
                    continue
        finally:
            handle.dispose()
        return roots

    def _quiz_roots(self) -> list[Any]:
        return self._quiz_roots_for(self._quiz_target) if self._quiz_target is not None else self._frames()

    @staticmethod
    def _quiz_root_visible(root: Any) -> bool:
        if getattr(root, "element_handle", None) is not None:
            return BrowserSession._visible(root)
        return BrowserSession._frame_visible(root)

    @contextmanager
    def quiz_scope(self, target: Any) -> Iterator[None]:
        previous = self._quiz_target
        old_questions, old_controls = self._questions, self._question_controls
        self._quiz_target = target
        try:
            yield
        finally:
            self._quiz_target = previous
            self._questions, self._question_controls = old_questions, old_controls

    def _quiz_rows(self, root: Any) -> tuple[list[Any], bool]:
        """Build the compact assessment rows in one browser round trip.

        These rows identify and preflight a quiz for change detection. The
        real question objects, controls and option mapping are still extracted
        by ``_extract_questions`` before answering.
        """
        probe = self._quiz_probe(root)
        rows: list[Any] = []
        for item in probe["rows"]:
            if not isinstance(item, (list, tuple)) or len(item) < 6:
                continue
            identity, stem, kind, count, labels, _row_ready = item[:6]
            identity = identity if isinstance(identity, str) else ""
            stem = stem if isinstance(stem, str) else ""
            if not stem:
                continue
            if kind == "text":
                rows.append((identity, stem, "text", count if isinstance(count, int) else 0))
            else:
                options = [label for label in labels if isinstance(label, str)] if isinstance(labels, list) else []
                rows.append((identity, stem, options))
        return rows, bool(rows) and bool(probe["ready"])

    def _quiz_material(self, target: Any) -> tuple[list[Any], bool]:
        frame = self._video_frame(target)
        origin = frame.evaluate("performance.timeOrigin")
        path = target.evaluate("""e => {
            const path=[];
            for(let node=e;node&&node.parentElement;node=node.parentElement)
                path.unshift([...node.parentElement.children].indexOf(node));
            return path;
        }""")
        # ContentTask identities are reset between runs. Retain assessment
        # rows across those resets and after submission replaces its fields.
        key = (frame, "quiz-root", tuple(path))
        cached = self._quiz_view_rows.get(key)
        submitted = self._frame_result_visible(target)
        if submitted and cached and cached[0] == origin:
            return cached[1], True
        rows, ready = self._quiz_rows(target)
        if rows:
            self._quiz_view_rows[key] = (origin, rows)
        elif submitted:
            rows = cached[1] if cached and cached[0] == origin else ["quiz-result"]
        return rows, submitted or ready

    def _unpublished_quiz(self, target: Any) -> bool:
        try:
            owner = self._task_region(target)
        except BrowserError:
            raise
        except Exception:
            owner = None
        if owner is not None:
            region, _frame = owner
            try:
                if re.search(r"教师.{0,8}(?:未创建|未完成|尚未发布)|测验尚未发布",
                             region.evaluate("e => e.innerText || ''")):
                    return True
            except BrowserError:
                raise
            except Exception:
                pass
            finally:
                region.dispose()
        try:
            roots = self._quiz_roots_for(target)
        except BrowserError:
            raise
        except Exception:
            roots = []
        for root in roots:
            try:
                body = root if getattr(root, "element_handle", None) is not None else root.locator("body")
                if re.search(r"教师.{0,8}(?:未创建|未完成|尚未发布)|测验尚未发布", body.inner_text(timeout=1500)):
                    return True
            except BrowserError:
                raise
            except Exception:
                continue
        return False

    def content_tasks(self, *, include_occluded: bool = False,
                      _cached: bool = False) -> list[ContentTask]:
        """Discover mounted media, readers and assessments in page order.

        ``_cached`` is private to the navigation poll: it reuses one discovery
        pass for ``chapter_kind`` and ``_view_signature`` instead of walking
        every frame, question and reader again for each call.
        """
        if _cached and self._navigation_mode:
            cached = self._nav_content_tasks.get(include_occluded)
            if cached is not None:
                return cached
        result = self._content_tasks_uncached(include_occluded=include_occluded)
        if self._navigation_mode:
            self._nav_content_tasks[include_occluded] = result
        return result

    def _content_tasks_uncached(self, *, include_occluded: bool = False) -> list[ContentTask]:
        self._guard()
        tasks = []
        videos = self._video_locators(include_occluded=True) if include_occluded else self._video_locators()
        documents = self._document_targets()
        for kind, targets in (("video", videos), ("document", documents),
                              ("quiz", self._chapter_quiz_targets())):
            for target in targets:
                try:
                    identity = self._content_identity(target, kind)
                    region = self._task_region_info(target)
                    if kind == "quiz" and not region and (videos or documents):
                        # Unowned questions near a generic player can be unrelated
                        # examples/decoys. Only identified attachment assessments
                        # join that page's ordered work; legacy standalone quiz
                        # recognition still handles the next assessment view.
                        continue
                    submitted = kind == "quiz" and self._result_probe_cached(target)
                    tasks.append(ContentTask(kind, target, identity, self._content_order(target),
                        completed=submitted or bool(region and region["completed"]), region=region["descriptor"] if region else "",
                        required=region["required"] if region else True))
                except BrowserError:
                    raise
                except Exception:
                    # A target can disappear during a navigation click or a
                    # same-document replacement. Drop it; the next scan sees
                    # the current DOM instead of stopping the workflow.
                    continue
        known_regions = {task.region for task in tasks if task.region}
        for frame in self._frames():
            try:
                if not self._frame_visible_memo(frame):
                    continue
                anchors = frame.locator(".ans-attach-ct iframe, .ans-attach-ct embed, .ans-attach-ct object")
                for index in range(min(anchors.count(), 200)):
                    anchor = anchors.nth(index)
                    if not self._visible(anchor) or not self._video_visible(anchor):
                        continue
                    region = self._task_region_info(anchor, placeholder=True)
                    if not region or region["descriptor"] in known_regions:
                        continue
                    kind = region["kind"]
                    tasks.append(ContentTask(kind, anchor, self._content_identity(anchor, kind),
                        self._content_order(anchor), completed=region["completed"], placeholder=True,
                        region=region["descriptor"], required=region["required"]))
                    known_regions.add(region["descriptor"])
            except BrowserError:
                raise
            except Exception:
                continue
        # A real DOM node can be reached through more than one reader selector.
        unique = {task.identity: task for task in tasks}
        return sorted(unique.values(), key=lambda task: task.order)

    def process_content(self, *,
                        on_quiz: VideoQuizSolver | None = None,
                        on_chapter_quiz: Callable[[], None] | None = None,
                        on_task: Callable[[str, int, int], None] | None = None) -> int:
        """Finish this page's content from top to bottom before navigation."""
        self._clear_content_nodes()
        completed: set[str] = set()
        non_tasks: set[str] = set()
        executed = 0
        skipped = 0
        stable_end = 0
        unresolved_since = 0.0
        try:
            for _iteration in range(1200):
                self._guard()
                tasks = self.content_tasks()
                # A lazy attachment may gain a job icon while another task is
                # running. Revisit only resources skipped for having no icon,
                # never replay work already executed or marked complete.
                reopened = {task.identity for task in tasks
                            if task.identity in non_tasks and task.required and not task.completed}
                completed.difference_update(reopened)
                non_tasks.difference_update(reopened)
                skipped -= len(reopened)
                pending = [task for task in tasks if task.identity not in completed]
                if pending:
                    stable_end = 0
                    task = pending[0]
                    skip_reason = self._task_skip_reason(task.target)
                    if task.completed and not skip_reason:
                        skip_reason = "测验已提交"
                    if skip_reason:
                        completed.add(task.identity)
                        if skip_reason == "未标记为任务点":
                            non_tasks.add(task.identity)
                        skipped += 1
                        self.log(f"本区域{skip_reason}，跳过视频播放或静态阅读。")
                        continue
                    if self._unresolved_content(tasks, before=pending[0].order):
                        if not unresolved_since:
                            unresolved_since = time.monotonic()
                        if time.monotonic() - unresolved_since >= self.NEXT_PAGE_TIMEOUT:
                            raise BrowserError("前面的学习附件尚未加载完成，请检查阅读器；不会先处理后面的任务。")
                        self._wait(max(0.4, float(self.settings.poll_interval)))
                        continue
                    if task.placeholder:
                        if task.kind == "quiz" and self._unpublished_quiz(task.target):
                            completed.add(task.identity)
                            skipped += 1
                            self.log("教师尚未创建或发布该测验，跳过此任务区域。")
                            continue
                        if not unresolved_since:
                            unresolved_since = time.monotonic()
                        if time.monotonic() - unresolved_since >= self.NEXT_PAGE_TIMEOUT:
                            raise BrowserError("当前学习附件尚未加载完成，请检查阅读器；不会先处理后面的任务。")
                        self._wait(max(0.4, float(self.settings.poll_interval)))
                        continue
                    unresolved_since = 0.0
                    number = executed + 1
                    if on_task is not None:
                        on_task(task.kind, number, executed + sum(item.required and not item.completed for item in pending))
                    skip_reason = self._task_skip_reason(task.target)
                    if skip_reason:
                        completed.add(task.identity)
                        if skip_reason == "未标记为任务点":
                            non_tasks.add(task.identity)
                        skipped += 1
                        self.log(f"本区域{skip_reason}，跳过视频播放或静态阅读。")
                        continue
                    action = {"video": "视频", "document": "静态阅读", "quiz": "章节测验"}[task.kind]
                    self.log(f"按页面顺序处理第 {number} 个任务：{action}。")
                    if task.kind == "video":
                        try:
                            self._watch_video_targets([task.target], on_quiz)
                        except Exception:
                            self._pause_video_on_stop()
                            raise
                    elif task.kind == "quiz":
                        if on_chapter_quiz is None:
                            raise BrowserError("当前页面包含章节测验，需要通过完整学习流程处理。")
                        with self.quiz_scope(task.target):
                            on_chapter_quiz()
                    else:
                        self._read_document_targets([task.target])
                    completed.add(task.identity)
                    executed += 1
                    continue
                if not completed:
                    raise BrowserError("当前页面没有可处理的视频或静态阅读任务。")
                if self._unresolved_content(tasks):
                    if not unresolved_since:
                        unresolved_since = time.monotonic()
                    if time.monotonic() - unresolved_since >= self.NEXT_PAGE_TIMEOUT:
                        raise BrowserError("仍有学习附件未能读取，请展开任务或检查文档阅读器加载状态；不会跳到下一节。")
                    stable_end = 0
                else:
                    unresolved_since = 0.0
                # Reveal the page tail normally, allowing below-fold/lazy
                # attachments to mount before declaring the page complete.
                footer = self._text_control(_NEXT)
                if footer is not None:
                    footer.scroll_into_view_if_needed(timeout=2000)
                else:
                    self._page.locator("body").press("Control+End", timeout=2000)
                self._wait(self.CONTENT_SETTLE)
                if not unresolved_since:
                    stable_end += 1
                if stable_end >= 2:
                    final_tasks = self.content_tasks()
                    if (not [task for task in final_tasks if task.identity not in completed
                             or (task.identity in non_tasks and task.required and not task.completed)]
                            and not self._unresolved_content(final_tasks)):
                        self.log(f"本页学习任务已处理完毕：执行 {executed} 个，跳过 {skipped} 个已完成或非任务区域。")
                        return executed
                    stable_end = 0
            raise BrowserError("页面持续出现新的学习任务，已停止以防无限循环。")
        finally:
            self._clear_content_nodes()

    def _unresolved_content(self, tasks: list[ContentTask], *,
                            before: tuple[Any, ...] | None = None) -> bool:
        """Do not silently skip an explicitly mounted unreadable attachment."""
        covered: set[Any] = set()
        for task in tasks:
            if task.placeholder:
                continue
            handle = task.target.element_handle(timeout=1000)
            if handle is None:
                continue
            try:
                frame = handle.owner_frame()
                while frame is not None:
                    covered.add(frame)
                    frame = frame.parent_frame
            finally:
                handle.dispose()
        for frame in self._frames():
            if not self._frame_visible_memo(frame) or re.search(r"/(?:work|quiz|exam|homework)(?:/|[.])|doHomeWork", urlparse(frame.url).path, re.I):
                continue
            nodes = frame.locator("iframe, embed, object")
            for index in range(min(nodes.count(), 100)):
                node = nodes.nth(index)
                explicit = node.evaluate("""e => {
                    if(!e.matches('embed,object,.ans-attach-online') &&
                       !e.closest('.ans-attach-online,[data-document],#reader,#docContainer,.document-viewer'))return false;
                    const r=e.getBoundingClientRect();
                    if(r.width<=0 || r.height<=0 || e.clientWidth<=0 || e.clientHeight<=0)return false;
                    for(let n=e;n;n=n.parentElement){
                        const s=getComputedStyle(n);
                        if(n.hidden || n.inert || s.display==='none' || s.visibility==='hidden'
                           || s.visibility==='collapse' || s.contentVisibility==='hidden'
                           || Number(s.opacity)===0)return false;
                    }
                    return true;
                }""")
                if not explicit or not self._video_visible(node):
                    continue
                if self._task_skip_reason(node):
                    continue
                source = node.get_attribute("src") or node.get_attribute("data") or ""
                if re.search(r"/(?:work|quiz|exam|homework)(?:/|[.])|doHomeWork", urlparse(source).path, re.I):
                    continue
                if node.evaluate("e => e.matches('embed,object')"):
                    if before is None or self._content_order(node) < before:
                        return True
                    continue
                handle = node.element_handle(timeout=1000)
                if handle is None:
                    return True
                try:
                    child = handle.content_frame()
                    if child not in covered and (before is None or self._content_order(node) < before):
                        return True
                finally:
                    handle.dispose()
        return False

    def _player_control(self, video: Any, selector: str) -> Any | None:
        # Controls belong to this video, not the first sibling player in a frame.
        owner = video.locator("xpath=ancestor::*[contains(concat(' ',normalize-space(@class),' '),' video-js ') "
                              "or contains(concat(' ',normalize-space(@class),' '),' video-stage ') "
                              "or contains(concat(' ',normalize-space(@class),' '),' ans-video ') "
                              "or contains(concat(' ',normalize-space(@class),' '),' ans-video-player ') "
                              "or contains(concat(' ',normalize-space(@class),' '),' video-player ') "
                              "or contains(concat(' ',normalize-space(@class),' '),' video-container ') "
                              "or contains(concat(' ',normalize-space(@class),' '),' player-container ') "
                              "or @data-video-player][1]")
        if owner.count() and owner.first.locator("video").count() == 1:
            control = self._first_visible(selector, root=owner.first)
            if control is not None:
                return control
        frame = self._video_frame(video)
        if frame.locator("video").count() == 1:
            return self._first_visible(selector, root=frame)
        return None

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

    def _click(self, locator: Any, *, humanized: bool = True) -> None:
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
        if humanized:
            self._delay()
        self._move_mouse(x, y)
        self._page.mouse.click(x, y, delay=random.uniform(65, 140) if humanized else 0)
        if humanized:
            self._delay()

    def watch_video(self, on_quiz: VideoQuizSolver | None = None) -> None:
        try:
            self._watch_video(on_quiz)
        except Exception:
            self._pause_video_on_stop()
            raise

    @staticmethod
    def _overlaps_video(box: dict | None, video_box: dict | None) -> bool:
        if not box or not video_box:
            return False
        area = min(box["width"] * box["height"], video_box["width"] * video_box["height"])
        width = min(box["x"] + box["width"], video_box["x"] + video_box["width"]) - max(box["x"], video_box["x"])
        height = min(box["y"] + box["height"], video_box["y"] + video_box["height"]) - max(box["y"], video_box["y"])
        return area > 0 and max(0, width) * max(0, height) >= area * 0.2

    def _video_frame(self, video: Any) -> Any:
        handle = video.element_handle(timeout=2000)
        if handle is None:
            raise BrowserError("视频播放器已发生变化，请确认当前章节。")
        try:
            frame = handle.owner_frame()
        finally:
            handle.dispose()
        if frame is None:
            raise BrowserError("无法确定视频所在页面。")
        return frame

    def _pointer_guard_script(self) -> str:
        return f"({_POINTER_EXIT_BOOTSTRAP})({json.dumps(self._pointer_guard_key)});"

    @staticmethod
    def _release_pointer_guard(handle: Any) -> None:
        try:
            handle.evaluate("guard => guard.setActive(false)")
        except Exception:
            pass  # Navigation/closing already destroyed this document.
        finally:
            try:
                handle.dispose()
            except Exception:
                pass

    def _clear_pointer_guards(self) -> None:
        guards, self._pointer_guards = self._pointer_guards, []
        for _, _, handle in guards:
            self._release_pointer_guard(handle)

    def _pointer_guard(self, frame: Any) -> Any:
        origin = frame.evaluate("performance.timeOrigin")
        kept = []
        current = None
        for owner, stamp, handle in self._pointer_guards:
            if owner.is_detached() or (owner is frame and stamp != origin):
                self._release_pointer_guard(handle)
            else:
                kept.append((owner, stamp, handle))
                if owner is frame:
                    current = handle
        self._pointer_guards = kept
        if current is not None:
            return current
        # The handoff uses a random, non-enumerable symbol, removed as soon as
        # Python owns the handle. No fixed automation global or control events.
        current = frame.evaluate_handle("""key => {
            const slot = Symbol.for(key), guard = window[slot];
            delete window[slot];
            return guard;
        }""", self._pointer_guard_key)
        if current.evaluate("guard => guard === undefined"):
            current.dispose()
            # Existing about:blank/restored documents may predate add_init_script.
            frame.evaluate(self._pointer_guard_script())
            current = frame.evaluate_handle("""key => {
                const slot = Symbol.for(key), guard = window[slot];
                delete window[slot];
                return guard;
            }""", self._pointer_guard_key)
        self._pointer_guards.append((frame, origin, current))
        return current

    @contextmanager
    def _video_pointer_scope(self, video: Any) -> Iterator[None]:
        active = []
        try:
            if self._context is not None:
                try:
                    frame = self._video_frame(video)
                    while frame is not None:
                        guard = self._pointer_guard(frame)
                        guard.evaluate("guard => guard.setActive(true)")
                        active.append(guard)
                        frame = frame.parent_frame
                except Exception:
                    # A compatibility aid must not introduce a new blocker.
                    self.log("未能启用部分页面的鼠标移出兼容，将沿用正常播放恢复流程。")
            yield
        finally:
            for guard in reversed(active):
                try:
                    guard.evaluate("guard => guard.setActive(false)")
                except Exception:
                    pass  # A replaced frame no longer has an active listener.

    def _video_visible(self, locator: Any, *, player: bool = False, include_occluded: bool = False) -> bool:
        """Exclude retired player regions without hiding below-fold quiz rows."""
        self._check_stop()
        visible_ancestors = """e => {
            const rect=e.getBoundingClientRect();
            let left=rect.left,right=rect.right,top=rect.top,bottom=rect.bottom;
            let scrollX=false,scrollY=false;
            for(let node=e;node;node=node.parentElement){
                const style=getComputedStyle(node);
                if(style.display==='none' || style.visibility==='hidden' || style.visibility==='collapse'
                   || Number(style.opacity)===0 || style.contentVisibility==='hidden')return false;
                if(node!==e){
                    const r=node.getBoundingClientRect();
                    if(/^(hidden|clip)$/.test(style.overflowX)){
                        if(node.clientWidth===0)return false;
                        if(!scrollX){left=Math.max(left,r.left+node.clientLeft);
                            right=Math.min(right,r.left+node.clientLeft+node.clientWidth);}
                    }
                    if(/^(hidden|clip)$/.test(style.overflowY)){
                        if(node.clientHeight===0)return false;
                        if(!scrollY){top=Math.max(top,r.top+node.clientTop);
                            bottom=Math.min(bottom,r.top+node.clientTop+node.clientHeight);}
                    }
                    // A scrollable reader/player can reveal its own contents.
                    scrollX ||= /^(auto|scroll)$/.test(style.overflowX) && node.scrollWidth>node.clientWidth;
                    scrollY ||= /^(auto|scroll)$/.test(style.overflowY) && node.scrollHeight>node.clientHeight;
                }
            }
            return right>left && bottom>top;
        }"""
        exposed_player = """(e, includeOccluded) => {
            const r=e.getBoundingClientRect();
            const left=Math.max(0,r.left),right=Math.min(innerWidth,r.right);
            const top=Math.max(0,r.top),bottom=Math.min(innerHeight,r.bottom);
            // Below-fold players can be brought into view by normal scrolling.
            if(right<=left || bottom<=top){
                let revealX=window.scrollX,revealY=window.scrollY;
                for(let n=e.parentElement;n;n=n.parentElement){
                    if(n!==document.scrollingElement){revealX+=n.scrollLeft;revealY+=n.scrollTop;}
                }
                // Negative off-canvas views cannot be reached by scrolling
                // from the document's origin. Already scrolled content can.
                return r.right+revealX>0 && r.bottom+revealY>0;
            }
            if(includeOccluded)return true;
            let owner=e.closest('.video-js, .video-stage, .ans-video, .ans-video-player, '
                + '.video-container, .video-player, .player-container, [data-video-player]');
            if(!owner && e.tagName==='VIDEO' && e.parentElement
               && !e.parentElement.matches('body,html')){
                const p=e.parentElement.getBoundingClientRect();
                if(p.width*p.height<=r.width*r.height*2.5)owner=e.parentElement;
            }
            // Player controls and its own question mask remain part of video
            // playback. A separate assessment covering every sample does not.
            for(const x of [.2,.5,.8])for(const y of [.2,.5,.8]){
                const hit=document.elementFromPoint(left+(right-left)*x,top+(bottom-top)*y);
                if(hit && (hit===e || e.contains(hit) || (owner && owner.contains(hit))))return true;
            }
            return false;
        }"""
        try:
            if not self._visible(locator) or not locator.evaluate(visible_ancestors):
                return False
            if player:
                exposed = locator.evaluate(exposed_player, True) if include_occluded else locator.evaluate(exposed_player)
                if not exposed:
                    return False
            handle = locator.element_handle(timeout=1000)
            if handle is None:
                return False
            try:
                frame = handle.owner_frame()
            finally:
                handle.dispose()
            # Computed styles do not cross iframe document boundaries. Inspect
            # the embedding elements and their ancestors separately as well.
            while frame is not None and frame.parent_frame is not None:
                element = frame.frame_element()
                try:
                    if not self._visible(element) or not element.evaluate(visible_ancestors):
                        return False
                    if player:
                        exposed = element.evaluate(exposed_player, True) if include_occluded else element.evaluate(exposed_player)
                        if not exposed:
                            return False
                finally:
                    element.dispose()
                frame = frame.parent_frame
            return frame is not None
        except (BrowserError, StopRequested):
            raise
        except Exception:
            return False

    def _video_question_blocks(self, root: Any, video: Any) -> list[Any]:
        """Read only visible question controls overlapping this player."""
        if not self._video_visible(root):
            return []
        video_box = video.bounding_box(timeout=1000)
        for selector in _VIDEO_QUESTION_BLOCKS:
            locators = root.locator(selector)
            blocks = [locators.nth(index) for index in range(min(locators.count(), 100))
                      if self._video_visible(locators.nth(index))
                      and self._overlaps_video(locators.nth(index).bounding_box(timeout=1000), video_box)]
            if blocks:
                return blocks
        # Some players render a plain overlay with a title and radio labels,
        # without any of the assessment question classes above.
        if (self._overlaps_video(root.bounding_box(timeout=1000), video_box)
                and len(self._option_candidates(root, video_quiz=True, video=video)) >= 2):
            text = _clean(root.inner_text(timeout=1500))
            if re.search(r"(?:单选|多选|判断)题", text) or root.locator(_VIDEO_TITLES).count():
                return [root]
        return []

    def _find_video_quiz(self, video: Any) -> Any | None:
        """Find a prompt belonging to this player, never a sibling assessment."""
        frame = self._video_frame(video)
        if not self._frame_visible_memo(frame):
            return None
        video_box = video.bounding_box(timeout=1000)
        candidates: list[Any] = []
        explicit = frame.locator(_VIDEO_QUIZ_ROOTS)
        candidates.extend(explicit.nth(index) for index in range(min(explicit.count(), 60)))

        # A DOM-independent fallback for the full-player question mask shown
        # in Example3. It must overlap the actual video, contain question-like
        # controls, and cannot be an outer element containing that video.
        overlays = frame.locator("div, section, article, form, dialog")
        handle = video.element_handle(timeout=2000)
        if handle is not None:
            try:
                indices = overlays.evaluate_all(r"""(elements, video) => {
                    const v=video.getBoundingClientRect(), va=v.width*v.height;
                    const owner=video.closest('.video-js, .video-stage, .ans-video, .ans-video-player, '
                        + '.video-container, .video-player, .player-container, [data-video-player]')
                        || (video.parentElement && !video.parentElement.matches('body,html')
                            ? video.parentElement : null);
                    return elements.map((e,index)=>{
                        if(e.contains(video) || !owner || !owner.contains(e))return null;
                        const r=e.getBoundingClientRect(), s=getComputedStyle(e), area=r.width*r.height;
                        if(area<=0 || area>va*2.5 || s.display==='none' || s.visibility==='hidden'
                           || Number(s.opacity)===0)return null;
                        const overlap=Math.max(0,Math.min(r.right,v.right)-Math.max(r.left,v.left))
                            *Math.max(0,Math.min(r.bottom,v.bottom)-Math.max(r.top,v.top));
                        if(overlap<Math.min(area,va)*.2)return null;
                        if(e.querySelectorAll('input[type=radio],input[type=checkbox],[role=radio],[role=checkbox]').length<2
                           && e.querySelectorAll('label,.option,.tkitem,.tkItem').length<2)return null;
                        if(!/(单选|多选|判断)题/.test(e.innerText||'')
                           && !e.querySelector('[data-question-id],.TiMu,.questionLi,.question-item,.question,fieldset,.tk_topic,.tkTopic'))return null;
                        return {index,area};
                    }).filter(Boolean).sort((a,b)=>b.area-a.area).slice(0,30).map(x=>x.index);
                }""", handle)
                candidates.extend(overlays.nth(index) for index in indices)
            finally:
                handle.dispose()

        # Prefer the nearest player wrapper, not arbitrary ancestors such as
        # the chapter body. Its contained overlay iframe is also in scope.
        wrapper = video.locator("xpath=..")
        for depth in range(4):
            if not wrapper.count():
                break
            tag = wrapper.evaluate("e => e.tagName")
            box = wrapper.bounding_box(timeout=1000)
            if tag in {"BODY", "HTML"}:
                break
            player_wrapper = depth == 0 or wrapper.evaluate("""e => e.matches(
                '.video-js, .video-stage, .ans-video, .ans-video-player, .video-container, '
                + '.video-player, .player-container, [data-video-player], #video, #video-player, #videoPlayer')""")
            if (player_wrapper and box and video_box and self._overlaps_video(box, video_box)
                    and box["width"] * box["height"] <= video_box["width"] * video_box["height"] * 2.5):
                candidates.append(wrapper)
            wrapper = wrapper.locator("xpath=..")

        for root in candidates:
            self._check_stop()
            try:
                if not self._video_visible(root) or not self._overlaps_video(root.bounding_box(timeout=1000), video_box):
                    continue
                if self._video_question_blocks(root, video):
                    return root
                # Only descend through iframes physically inside an accepted
                # player/overlay root. Sibling course and quiz frames stay out.
                for child in frame.child_frames:
                    element = child.frame_element()
                    try:
                        contained = root.evaluate("(root, child) => root.contains(child)", element)
                    finally:
                        element.dispose()
                    if contained and self._frame_visible_memo(child):
                        body = child.locator("body")
                        if self._video_question_blocks(body, video):
                            return body
            except (BrowserError, StopRequested):
                raise
            except Exception:
                continue
        return None

    @staticmethod
    def _question_fingerprint(questions: list[Question]) -> tuple:
        return tuple((q.id, q.text, q.kind, tuple((o.label, o.text) for o in q.options)) for q in questions)

    def _video_quiz_screenshot(self, root: Any, video: Any) -> bytes:
        # The overlay and video share viewport coordinates even across frames.
        # Keep unrelated chapter navigation and other assessments out of vision.
        box = video.bounding_box(timeout=2000)
        viewport = self._page.viewport_size
        if box and viewport:
            x, y = max(0, box["x"]), max(0, box["y"])
            width = min(box["x"] + box["width"], viewport["width"]) - x
            height = min(box["y"] + box["height"], viewport["height"]) - y
            if width > 0 and height > 0:
                return self._page.screenshot(type="png", clip={"x": x, "y": y, "width": width, "height": height}, timeout=10000)
        return root.screenshot(type="png", timeout=10000)

    def _apply_video_answers(self, answers: list[Answer], root: Any, video: Any) -> None:
        self._validate_answers(answers)
        fingerprint = self._question_fingerprint(list(self._questions.values()))
        mapped = {answer.question_id: answer for answer in answers}
        operations: list[tuple[Any, bool]] = []
        for question_id, question in self._questions.items():
            desired = {str(choice).strip().upper() for choice in mapped[question_id].choices}
            for label, control in self._question_controls[question_id].items():
                selected = self._selected(control)
                if question.kind == "multiple" and label not in desired and selected:
                    operations.append((control, False))
            for label in desired:
                control = self._question_controls[question_id][label]
                if self._selected(control) is not True:
                    operations.append((control, True))
        for index, (control, desired) in enumerate(operations):
            self._guard()
            self._click(control)
            self._check_stop()
            current = self._extract_questions([root], video=video)
            self._check_stop()
            if current and self._question_fingerprint(current) != fingerprint:
                raise BrowserError("选择答案后视频弹题发生变化，已停止以免提交未经解答的题目。")
            if not current:
                if index + 1 != len(operations):
                    raise BrowserError("视频弹题在答案尚未填完时发生变化，已停止。")
                return  # Selecting the final answer may close the whole prompt.
            if self._selected(control) is not desired:
                raise BrowserError("无法确认视频弹题选项已正确选中。")

    @staticmethod
    def _watch_video_feedback(root: Any, lines: list[str]) -> Any:
        """Observe feedback rendering without changing the page or its answers."""
        return root.evaluate_handle(r"""(root, lines) => {
            const clean=s=>(s||'').replace(/\s+/g,' ').trim();
            const matches=n=>n&&lines.some(line=>clean(n.textContent).includes(line));
            const labels=[...root.querySelectorAll('*')].filter(n=>matches(n)
                && ![...n.children].some(matches));
            const visible=n=>{
                if(!n.isConnected||!n.getClientRects().length)return false;
                for(let e=n;e;e=e.parentElement){const s=getComputedStyle(e);
                    if(e.hidden||s.display==='none'||s.visibility==='hidden'||Number(s.opacity)===0)return false;}
                return true;
            };
            const visibility=new Map(labels.map(n=>[n,visible(n)]));
            const state={changed:false};
            const inspect=records=>{
                for(const m of records){
                    if(m.type==='childList' && [...m.addedNodes,...m.removedNodes].some(matches))state.changed=true;
                    if(m.type==='characterData' && (matches(m.target)
                        || lines.some(line=>clean(m.oldValue).includes(line))))state.changed=true;
                    if(m.type==='attributes')for(const n of labels){
                        if(m.target!==n&&(n.parentElement===root||m.target!==n.parentElement))continue;
                        const current=visible(n);
                        if(current!==visibility.get(n))state.changed=true;
                        visibility.set(n,current);
                    }
                }
            };
            state.observer=new MutationObserver(inspect);
            state.observer.observe(root,{childList:true,subtree:true,characterData:true,
                characterDataOldValue:true,attributes:true,attributeFilter:['hidden','style','class']});
            state.read=()=>{inspect(state.observer.takeRecords());return state.changed;};
            return state;
        }""", lines)

    def _handle_video_quiz(self, root: Any, video: Any,
                           on_quiz: VideoQuizSolver | None) -> None:
        self._guard()
        if on_quiz is None:
            raise BrowserError("视频出现答题界面，请启动带模型解题的自动流程。")
        old_questions, old_controls = self._questions, self._question_controls
        try:
            questions = self._extract_questions([root], video=video)
            if not questions:
                raise BrowserError("视频弹题没有可识别的选择题，请查看诊断截图。")
            original_lines = {_clean(line) for line in root.inner_text(timeout=1500).splitlines()}
            # Restarting on an already rejected answer must also allow recovery.
            # A phrase quoted in the question itself is not result feedback.
            option_texts = {_clean(option.text) for q in questions for option in q.options}
            existing_wrong = [line for line in original_lines if _VIDEO_WRONG.search(line)
                              and not any(line in _clean(q.text) or _clean(q.text) in line for q in questions)
                              and re.sub(r"^[A-Ha-h](?:[.、．:：)）\s]|$)\s*", "", line) not in option_texts]
            original_lines.difference_update(existing_wrong)
            corrections, correction_feedback = 0, None

            def correction_note(previous: list[Answer], feedback: str, round_number: int) -> str:
                return json.dumps({"correction_round": round_number,
                    "previous_answers": [{"question_id": a.question_id, "choices": list(a.choices)} for a in previous],
                    "feedback": feedback[:6000]}, ensure_ascii=False)

            if existing_wrong:
                previous = [Answer(q.id, tuple(label for label, control in self._question_controls[q.id].items()
                                              if self._selected(control))) for q in questions]
                corrections = 1
                correction_feedback = correction_note(previous, "\n".join(existing_wrong), corrections)

            while True:
                self._guard()
                fingerprint = self._question_fingerprint(questions)
                screenshot = self._video_quiz_screenshot(root, video)
                self.log(f"正在纠正视频插题（{corrections}/{self.MAX_VIDEO_QUIZ_CORRECTIONS}）。"
                         if correction_feedback else f"检测到视频中途弹题，正在解答 {len(questions)} 道题。")
                answers = (on_quiz(questions, screenshot, video_quiz_feedback=correction_feedback)
                           if correction_feedback else on_quiz(questions, screenshot))
                self._guard()
                current = self._extract_questions([root], video=video)
                self._check_stop()
                if self._question_fingerprint(current) != fingerprint:
                    raise BrowserError("模型回答期间视频弹题发生变化，已停止以免填写旧答案。")
                self._apply_video_answers(answers, root, video)
                deadline = time.monotonic() + self.VIDEO_QUIZ_WAIT_TIMEOUT
                submitted = continued = False
                feedback_watch = None
                try:
                    while True:
                        self._guard()
                        active = bool(self._video_question_blocks(root, video))
                        try:
                            state = video.evaluate("v => ({paused:v.paused, ended:v.ended})")
                        except BrowserError:
                            raise
                        except Exception:
                            raise BrowserError("视频播放器已发生变化，请检查当前页面。") from None
                        try:
                            text = root.inner_text(timeout=1000) if self._video_visible(root) else ""
                        except BrowserError:
                            raise
                        except Exception:
                            text = ""
                        feedback_lines = [_clean(line) for line in text.splitlines() if _clean(line) not in original_lines]
                        feedback = "\n".join(feedback_lines)
                        expired = time.monotonic() >= deadline
                        wrong = submitted and bool(_VIDEO_WRONG.search(feedback))
                        if wrong:
                            # Old red text may remain while a new submission is
                            # pending. Only a fresh rendering (or the full result
                            # wait) counts as another rejected submission.
                            fresh = True
                            if feedback_watch is not None:
                                try:
                                    fresh = bool(feedback_watch.evaluate("s => s.read()"))
                                except Exception:
                                    fresh = True
                            if not fresh and not expired:
                                self._wait(self.VIDEO_QUIZ_POLL_INTERVAL)
                                continue
                            if corrections >= self.MAX_VIDEO_QUIZ_CORRECTIONS:
                                raise BrowserError(f"视频插题已纠错 {corrections} 次仍未通过，已保留当前页面。")
                            corrections += 1
                            correction_feedback = correction_note(answers, feedback, corrections)
                            questions = self._extract_questions([root], video=video)
                            if not questions:
                                raise BrowserError("答错后未能重新读取视频插题，请查看当前页面。")
                            break
                        if expired:
                            raise BrowserError("视频弹题未能通过或未恢复播放，请检查当前题目。")
                        continuation = self._text_control(_VIDEO_CONTINUE, root=root, video_quiz=True) if self._video_visible(root) else None
                        submit = self._text_control(_SUBMIT, root=root, video_quiz=True) if not submitted and active else None
                        if (continuation is not None and not continued
                                and (submitted or _VIDEO_RIGHT.search(feedback) or submit is None)):
                            self._click(continuation)
                            continued = True
                        elif continuation is None and not active and (not state["paused"] or state["ended"]):
                            self.log("视频弹题已处理，播放器已恢复。")
                            return
                        elif submit is not None:
                            current = self._extract_questions([root], video=video)
                            self._check_stop()
                            if current and self._question_fingerprint(current) != fingerprint:
                                raise BrowserError("提交前视频弹题发生变化，已停止以免提交未经解答的题目。")
                            if not current:
                                self._wait(self.VIDEO_QUIZ_POLL_INTERVAL)
                                continue
                            old_wrong = [line for line in feedback_lines if _VIDEO_WRONG.search(line)]
                            if old_wrong:
                                feedback_watch = self._watch_video_feedback(root, old_wrong)
                            self._click(submit)
                            submitted = True
                        self._wait(self.VIDEO_QUIZ_POLL_INTERVAL)
                finally:
                    if feedback_watch is not None:
                        try:
                            feedback_watch.evaluate("s => s.observer.disconnect()")
                        except Exception:
                            pass  # A closed/replaced frame already removed it.
                        finally:
                            feedback_watch.dispose()
        finally:
            self._questions, self._question_controls = old_questions, old_controls

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
                    control = self._player_control(
                        video, ".vjs-play-control, button[aria-label*=Pause], button[aria-label*=暂停]")
                    target = control if control is not None else video
                    target.scroll_into_view_if_needed(timeout=1000)
                    target.click(timeout=1000)
        except Exception:
            self.log("停止后未能确认播放器暂停，请手动检查浏览器。")

    def _watch_video(self, on_quiz: VideoQuizSolver | None = None) -> None:
        self._watch_video_targets(self._video_locators(), on_quiz)

    def _watch_video_targets(self, videos: list[Any],
                             on_quiz: VideoQuizSolver | None = None) -> None:
        self._guard()
        if not videos:
            raise BrowserError("未找到可见的视频播放器。")
        deadline = time.monotonic() + float(self.settings.video_timeout)
        for number, video in enumerate(videos, start=1):
            if not self.task_required(video):
                self.log(f"视频 {number}/{len(videos)} 未标记为任务点，跳过播放。")
                continue
            with self._video_pointer_scope(video):
                self.log(f"开始等待视频 {number}/{len(videos)} 自然播放结束。")
                last_position, progressed_at, play_requested_at = -1.0, time.monotonic(), 0.0
                paused_since = 0.0
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
                    if state["paused"] or state["ended"]:
                        prompt = self._find_video_quiz(video)
                        if prompt is not None:
                            started = time.monotonic()
                            self._handle_video_quiz(prompt, video, on_quiz)
                            self._check_stop()
                            # Model work is not buffering time or playback time.
                            deadline += time.monotonic() - started
                            progressed_at, play_requested_at, paused_since = time.monotonic(), 0.0, 0.0
                            continue
                    if state["ended"]:
                        self.log("视频已自然播放结束。")
                        break
                    position = float(state["time"])
                    if position > last_position + 0.15:
                        progressed_at, last_position = time.monotonic(), position
                    if state["paused"]:
                        if play_requested_at:
                            if time.monotonic() - play_requested_at < self.VIDEO_START_TIMEOUT:
                                # A preload-none player may still be loading after
                                # the trusted click. Keep polling for prompts/stop
                                # without toggling the play control a second time.
                                self._wait(self.VIDEO_QUIZ_POLL_INTERVAL)
                                continue
                            raise BrowserError("点击播放后视频仍未开始，请检查播放器加载状态或页面提示。")
                        if not paused_since:
                            paused_since = time.monotonic()
                        if time.monotonic() - paused_since < self.PAUSED_QUIZ_GRACE:
                            self._wait(self.VIDEO_QUIZ_POLL_INTERVAL)
                            continue
                        play = self._player_control(video,
                            ".vjs-big-play-button, .vjs-play-control, button[aria-label*=Play], "
                            "button[aria-label*=播放], [role=button][title=播放]")
                        self._click(play if play is not None else video)
                        play_requested_at = time.monotonic()
                        self._wait(0.5)
                    elif time.monotonic() - progressed_at > 120:
                        raise BrowserError("视频超过 120 秒没有进展，请检查缓冲或提示。")
                    else:
                        paused_since, play_requested_at = 0.0, 0.0
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
                if frame.is_detached() or not self._frame_visible_memo(frame):
                    continue
                if re.search(r"/(?:work|quiz|exam|homework)(?:/|[.])|doHomeWork", urlparse(frame.url).path, re.I):
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
                    const visible=e=>{
                        const r=e.getBoundingClientRect();
                        if(r.width<=0 || r.height<=0)return false;
                        for(let n=e;n;n=n.parentElement){
                            const s=getComputedStyle(n);
                            if(n.hidden || n.inert || s.display==='none' || s.visibility==='hidden'
                               || s.visibility==='collapse' || Number(s.opacity)===0
                               || s.contentVisibility==='hidden')return false;
                        }
                        return true;
                    };
                    const rows = elements.map((e, index) => {
                        const root = e === document.body || e === document.documentElement;
                        const t = root ? document.scrollingElement : e;
                        if (!t || e.closest(navigation)) return null;
                        const r = e.getBoundingClientRect(), s = getComputedStyle(e);
                        if (s.visibility === 'hidden' || s.display === 'none'
                            || (root ? innerWidth : r.width) < 180
                            || (root ? innerHeight : r.height) < 24) return null;
                        for(let n=e;n;n=n.parentElement){
                            const style=getComputedStyle(n);
                            if(n.hidden || n.inert
                               || style.display==='none' || style.visibility==='hidden'
                               || style.visibility==='collapse' || Number(style.opacity)===0
                               || style.contentVisibility==='hidden')return null;
                        }
                        const client = root ? Math.min(innerHeight, t.clientHeight || innerHeight) : t.clientHeight;
                        const scrolls = t.scrollHeight > client + 8
                            && (root || /(auto|scroll)/.test(s.overflowY));
                        if (!scrolls && !(e.matches(marker) || root)) return null;
                        const media = !!e.querySelector('canvas, img, svg');
                        const text = (e.innerText || '').replace(/\s+/g, '').length;
                        const embedded = [...e.querySelectorAll('iframe, embed, object, video')].some(visible);
                        // Frames/plugins do not expose their reading progress on
                        // the shell. Their own document must supply a real target.
                        if (embedded) return null;
                        const player='.video-js, .video-stage, .ans-video, .ans-video-player, '
                            + '.video-container, .video-player, .player-container, [data-video-player]';
                        const question='[data-question-id], .TiMu, .questionLi, .question-item, '
                            + '.question, fieldset, .examPaper_question';
                        if ((e.matches(question) && visible(e))
                            || [...e.querySelectorAll(question)].some(visible))return null;
                        if(e.closest(player) || [...e.querySelectorAll(player)].some(visible))return null;
                        if (!scrolls && text < 40 && !media
                            && !(text > 0 && (e.matches(marker) || e.querySelector(marker)))) return null;
                        return {e, t, index, scrolls,
                            explicit:e.matches(marker) && (scrolls || r.height<=innerHeight+8)};
                    }).filter(Boolean);
                    return rows.filter(row => !rows.some(other => other !== row
                        && ((row.t === other.t && row.index > other.index)
                            || (other.scrolls && other.e.contains(row.e) && !row.scrolls && !row.explicit)
                            || (row.e.contains(other.e) && row.t !== other.t
                                && (other.scrolls || other.explicit || !row.scrolls)))))
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
            if(!root && t.scrollHeight<=t.clientHeight+8){
                // A flat content region can be taller than its viewport. Its
                // progress belongs to the nearest scrolling ancestor/window,
                // rather than the region's always-zero scrollTop.
                for(let parent=e.parentElement;parent;parent=parent.parentElement){
                    const windowRoot=parent===document.scrollingElement;
                    const style=getComputedStyle(parent);
                    const client=windowRoot?Math.min(innerHeight,parent.clientHeight||innerHeight):parent.clientHeight;
                    if(parent.scrollHeight<=client+8 || (!windowRoot && !/(auto|scroll)/.test(style.overflowY)))continue;
                    const r=e.getBoundingClientRect(),p=parent.getBoundingClientRect();
                    const start=r.top-(windowRoot?0:p.top+parent.clientTop)+parent.scrollTop;
                    return {top:Math.max(0,parent.scrollTop-start),height:r.height,client};
                }
            }
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
            const htmlStyle=getComputedStyle(document.documentElement);
            const bodyStyle=document.body?getComputedStyle(document.body):null;
            // With visible HTML overflow, BODY's overflow belongs to the
            // viewport. Its own box can be empty or move above the viewport
            // during window scrolling without clipping visible descendants.
            // Containment disables this propagation; independent BODY scroll
            // containers must still clip like other ordinary ancestors.
            const bodyOverflowToViewport=bodyStyle
                && htmlStyle.overflowX==='visible'&&htmlStyle.overflowY==='visible'
                && htmlStyle.contain==='none'&&bodyStyle.contain==='none';
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
                // The viewport intersection above already accounts for root
                // overflow. Do not clip it a second time using moving boxes.
                if(n===document.documentElement||n===document.scrollingElement
                    ||(n===document.body&&bodyOverflowToViewport))continue;
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
        self._clear_content_nodes()
        try:
            self._read_document_targets(self._document_targets())
        finally:
            self._clear_content_nodes()

    def _read_document_targets(self, targets: list[Any]) -> None:
        self._guard()
        self._page.bring_to_front()
        if not targets:
            raise BrowserError("未找到可确认阅读进度的文档内容；嵌入阅读器或插件可能需要人工查看。")
        self.log("正在逐步滚动文档并确认阅读区域已到底部。")
        processed: set[str] = set()
        for number, target in enumerate(targets, start=1):
            self._guard()
            try:
                skip_reason = self._task_skip_reason(target)
                if skip_reason:
                    self.log(f"文档 {number}/{len(targets)} {skip_reason}，跳过静态阅读。")
                    continue
                identity = self._content_identity(target, "document")
                if identity in processed:
                    continue
                processed.add(identity)
                target.scroll_into_view_if_needed(timeout=3000)
                previous_top, stuck, bottom_reads = -1.0, 0, 0
                bottom_since, bottom_signature, last_log = 0.0, None, 0.0
                skip_reason = ""
                for _ in range(1200):
                    self._guard()
                    skip_reason = self._task_skip_reason(target)
                    if skip_reason:
                        break
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
                        if (bottom_reads >= 3
                                and time.monotonic() - bottom_since >= self.DOCUMENT_BOTTOM_SETTLE):
                            break
                        self._wait(self.DOCUMENT_BOTTOM_WAIT)
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
                    skip_reason = self._task_skip_reason(target)
                    if skip_reason:
                        break
                    self._page.mouse.wheel(0, random.uniform(650, 1000))
                    # wheel dispatch returns before the browser finishes scrolling.
                    self._wait(random.uniform(0.18, 0.32))
                else:
                    raise BrowserError("文档页过长或一直加载新内容，已停止自动滚动。")
                if skip_reason:
                    self.log(f"文档 {number}/{len(targets)} {skip_reason}，结束静态阅读。")
                    continue
                metrics = self._scroll_metrics(target)
                if metrics["top"] + metrics["client"] < metrics["height"] - 3:
                    raise BrowserError("尚未确认文档已滚动到底部。")
                self.log(f"文档 {number}/{len(targets)} 已确认到底部："
                         f"{metrics['top']:.0f}+{metrics['client']:.0f}/{metrics['height']:.0f}。")
            except (BrowserError, StopRequested):
                raise
            except Exception as exc:
                raise BrowserError("文档结构变化，无法确认已经读到底部。") from exc
        self.log("文档阅读区域已处理完毕。")

    def _root_has_task_cards(self, root: Any) -> bool:
        """Whether this question scope also contains attachment cards.

        Standalone assessment documents (the common ``doHomeWorkNew`` case)
        have no ``.ans-attach-ct`` inside their frame. In that case the
        per-question task-marker walk and video-frame climb are unnecessary.
        """
        try:
            return root.locator(".ans-attach-ct").count() > 0
        except Exception:
            return True

    def _frame_is_video_module(self, root: Any) -> bool:
        if not hasattr(root, "parent_frame"):
            return False
        try:
            return bool(re.search(r"/modules/video/", urlparse(root.url).path, re.I))
        except Exception:
            return False

    def _question_blocks(self, root: Any) -> list[Any]:
        """Find question bodies from content, never from activity tab captions.

        Known layouts also expose incomplete questions during loading, allowing
        the navigation wait to wait for their choices. Other layouts need a
        question stem and grouped choice controls in the same small container.
        """
        if self._navigation_mode:
            key = id(root)
            cached = self._nav_question_blocks.get(key)
            if cached is not None:
                return cached
        result = self._question_blocks_uncached(root)
        if self._navigation_mode:
            self._nav_question_blocks[id(root)] = result
        return result

    def _question_blocks_uncached(self, root: Any) -> list[Any]:
        if self._frame_is_video_module(root):
            return []
        has_cards = self._root_has_task_cards(root)
        for selector in _QUESTION_BLOCKS:
            locators = root.locator(selector)
            indices = locators.evaluate_all(_VISIBLE_ELEMENT_PROBE)
            blocks = [locators.nth(index) for index in indices]
            if blocks:
                if has_cards:
                    blocks = [block for block in blocks if self._chapter_question(block)]
                if blocks:
                    return blocks
        containers = root.locator("div, section, article, fieldset, li")
        indices = containers.evaluate_all(_QUESTION_CONTAINER_PROBE)
        blocks = [containers.nth(index) for index in indices]
        if has_cards:
            blocks = [block for block in blocks if self._chapter_question(block)]
        return blocks

    def _chapter_question(self, block: Any) -> bool:
        state = self._task_marker_state(block)
        if state and state["kind"] == "video":
            return False
        if block.evaluate("""e => !!e.closest('.video-js, .video-stage, .ans-video, .ans-video-player, '
            + '.video-player, [data-video-quiz], .ans-videoquiz, #videoquiz, .video-quiz, .videoquiz')"""):
            return False
        frame = self._video_frame(block)
        while frame is not None:
            if re.search(r"/modules/video/", urlparse(frame.url).path, re.I):
                return False
            frame = frame.parent_frame
        return True

    def _has_quiz(self) -> bool:
        if self._navigation_mode and self._nav_has_quiz is not None:
            return self._nav_has_quiz
        if self._navigation_mode:
            result = any(self._quiz_probe(frame)["rows"] for frame in self._frames()
                         if self._frame_visible_memo(frame))
        else:
            result = False
            for frame in self._frames():
                try:
                    if self._frame_visible_memo(frame) and self._question_blocks(frame):
                        result = True
                        break
                except BrowserError:
                    raise
                except Exception:
                    continue
        if self._navigation_mode:
            self._nav_has_quiz = result
        return result

    def _option_candidates(self, block: Any, *, video_quiz: bool = False, video: Any = None) -> list[Any]:
        video_box = video.bounding_box(timeout=1000) if video is not None else None
        is_visible = self._video_visible if video_quiz else self._visible
        for selector in _VIDEO_OPTIONS if video_quiz else _OPTION_SELECTORS:
            candidates = block.locator(selector)
            result = [candidates.nth(index) for index in range(min(candidates.count(), 30))
                      if is_visible(candidates.nth(index))
                      and (video is None or self._overlaps_video(candidates.nth(index).bounding_box(timeout=1000), video_box))]
            if len(result) >= 2:
                return result
        # Modern views can hide native inputs inside clickable custom rows.
        # Keep the complete row text and the real input used to verify state.
        wrappers = block.locator("label, li, div, p")
        indices = wrappers.evaluate_all("""elements => {
            const selector='input[type=radio], input[type=checkbox]';
            const rows=elements.map((e,index)=>({element:e,index})).filter(({element:e})=>
                e.querySelectorAll(selector).length===1 && (e.innerText||'').trim());
            return rows.filter(item=>!rows.some(other=>other!==item
                && item.element.contains(other.element))).slice(0,30).map(item=>item.index);
        }""")
        result = [wrappers.nth(index) for index in indices
                  if is_visible(wrappers.nth(index))
                  and (video is None or self._overlaps_video(wrappers.nth(index).bounding_box(timeout=1000), video_box))]
        if len(result) >= 2:
            return result
        inputs = block.locator("input[type=radio], input[type=checkbox]")
        return [inputs.nth(index) for index in range(min(inputs.count(), 30))
                if is_visible(inputs.nth(index))
                and (video is None or self._overlaps_video(inputs.nth(index).bounding_box(timeout=1000), video_box))]

    def get_questions(self) -> list[Question]:
        return self._extract_questions(self._quiz_roots())

    def _text_answer_controls(self, block: Any, *, _inside_blank: bool = False) -> list[Any]:
        """Read the editable fields, including UEditor's visible iframe body.

        Editor toolbars and hidden serialization textareas are not answer
        fields. Returning actual locators keeps typing on the browser's native
        input path instead of invoking an editor API or changing DOM values.
        """
        selector = (".blankItemInp, input:not([type]), input[type=text], textarea, "
                    "[contenteditable=true], [contenteditable=plaintext-only], iframe")
        excluded = ("[role=toolbar], .edui-toolbar, .edui-dialog-container, "
                    ".edui-popup, .tox-toolbar, .tox-dialog, .cke_top, .cke_bottom")

        def collect(root: Any, depth: int = 0) -> list[Any]:
            fields: list[Any] = []
            candidates = root.locator(selector)
            for index in range(min(candidates.count(), 100)):
                self._check_stop()
                control = candidates.nth(index)
                if not self._visible(control) or control.evaluate(
                        "(e, excluded) => !!e.closest(excluded)", excluded):
                    continue
                if control.evaluate("e => e.matches('.blankItemInp')"):
                    # Multi-blank UEditor questions initially expose a display
                    # div. Clicking it activates that blank's hidden editor
                    # and can hide all other editors. Cache the stable wrapper
                    # so extraction needs no clicks and later blanks survive.
                    if control.locator(".InpDIV").count() and control.locator(".textDIV").count():
                        fields.append(control)
                    continue
                if not _inside_blank and control.evaluate("e => !!e.closest('.blankItemInp')"):
                    continue
                if control.evaluate("e => e.tagName === 'IFRAME'"):
                    if depth >= 4:
                        continue
                    handle = control.element_handle(timeout=1500)
                    if handle is None:
                        continue
                    try:
                        frame = handle.content_frame()
                    finally:
                        handle.dispose()
                    if frame is not None:
                        fields.extend(collect(frame, depth + 1))
                    continue
                if not control.is_editable(timeout=1500):
                    continue
                # A nested editable is part of its outer editor, not another
                # blank. Native inputs embedded in that editor follow suit.
                if control.evaluate("""e => !!e.parentElement?.closest(
                        '[contenteditable=true], [contenteditable=plaintext-only]')"""):
                    continue
                fields.append(control)
            return fields

        return collect(block)

    def _resolve_text_answer_control(self, control: Any) -> Any:
        if not control.evaluate("e => e.matches('.blankItemInp')"):
            return control
        display = control.locator(".InpDIV").first
        if self._visible(display):
            self._click(display)
        deadline = time.monotonic() + 5
        while True:
            self._guard()
            fields = self._text_answer_controls(control.locator(".textDIV"), _inside_blank=True)
            if len(fields) == 1:
                return fields[0]
            if time.monotonic() >= deadline:
                raise BrowserError("点击填空区域后未找到可编辑的文本框。")
            self._wait(.1)

    def _trim_rich_text_tail(self, control: Any, actual: dict[str, Any]) -> dict[str, Any]:
        """Remove a structural trailing newline produced by rich editors.

        UEditor and plain contenteditable can leave an empty paragraph or a
        trailing <br> after ``insert_text``. Only delete when the DOM tail is
        actually an empty block/break, so a real trailing character is never
        removed. Native input/textarea values are left untouched.
        """
        for _ in range(3):
            text = actual.get("text") if isinstance(actual, dict) else None
            if (not isinstance(actual, dict) or not actual.get("rich")
                    or not isinstance(text, str) or not re.search(r"[\r\n]$", text)):
                break
            try:
                if not control.evaluate(_RICH_TRAILING_BREAK_SCRIPT):
                    break
                self._page.keyboard.press("ControlOrMeta+End", timeout=3000)
                self._page.keyboard.press("Backspace", timeout=3000)
                actual = control.evaluate(
                    "e => ({text: 'value' in e ? e.value : e.innerText, rich: !('value' in e)})")
            except Exception:
                break
        return actual

    @staticmethod
    def _answer_text(value: str, *, rich: bool = False) -> str:
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        if rich:
            # Chromium turns inserted newlines into editor paragraphs. Their
            # rendered innerText can contain two breaks for one paragraph
            # boundary, so compare the text without paragraph spacing.
            value = re.sub(r"\n{2,}", "\n", value).rstrip("\n")
        return value

    def _extract_questions(self, roots: list[Any], *, video: Any = None) -> list[Question]:
        self._guard()
        self._question_controls = {}
        self._questions = {}
        questions: list[Question] = []
        for frame_number, frame in enumerate(roots):
            if video is None and not self._quiz_root_visible(frame):
                continue
            blocks: list[Any] = []
            if video is not None:
                blocks = self._video_question_blocks(frame, video)
            else:
                blocks = self._question_blocks(frame)
            for index, block in enumerate(blocks):
                self._check_stop()
                raw_text = block.inner_text(timeout=2000)
                text = _clean(raw_text)
                if not text:
                    continue
                title = block.locator(_VIDEO_TITLES if video is not None else _QUESTION_TITLES)
                stem = _clean(title.first.inner_text(timeout=1500)) if title.count() else (
                    text if video is not None else _clean(raw_text.splitlines()[0]))
                text_kind = re.search(r"(?:\[|【)\s*(填空|简答)题\s*(?:\]|】)|"
                                      r"^(?:\d+[.、．:：)）]?\s*)?(填空|简答)题(?:\s|[:：])", stem)
                if text_kind and video is not None:
                    raise BrowserError("视频插题目前仅支持单选、多选和判断。")
                if text_kind:
                    kind = "fill" if "填空" in text_kind.group() else "short"
                    fields = self._text_answer_controls(block)
                    if not fields or (kind == "short" and len(fields) != 1):
                        raise BrowserError("文本题的作答区域尚未完整加载或无法识别。")
                    question_id = (block.get_attribute("data-question-id") or block.get_attribute("data-id")
                                   or block.get_attribute("id") or f"f{frame_number}-q{index + 1}")
                    if question_id in self._questions:
                        question_id = f"f{frame_number}-{question_id}"
                    question = Question(id=question_id, text=stem, kind=kind, options=(),
                                        blank_count=len(fields) if kind == "fill" else 0)
                    questions.append(question)
                    self._questions[question_id] = question
                    self._question_controls[question_id] = {
                        str(field_index + 1): field for field_index, field in enumerate(fields)
                    }
                    continue
                if re.search(r"\[(?:填空|简答|论述|连线|排序|阅读|计算)题\]|【(?:填空|简答|论述)题】", text):
                    if video is not None:
                        raise BrowserError("视频插题目前仅支持单选、多选和判断。")
                    raise BrowserError("检测到尚不支持的题型；章节测验支持单选、多选、判断、填空和简答。")
                candidates = self._option_candidates(block, video_quiz=video is not None, video=video)
                if not candidates:
                    if _RESULT.search(text):
                        continue
                    raise BrowserError("题目没有可可靠识别的选项控件，请人工查看诊断截图。")
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
        if not questions and video is None and not self._result_visible():
            raise BrowserError("未找到受支持的测验题目。")
        return questions

    def quiz_screenshot(self) -> bytes:
        self._guard()
        if self._quiz_target is not None:
            return self._quiz_target.screenshot(type="png", timeout=10000)
        return self._page.screenshot(type="png", full_page=True, timeout=10000)

    @staticmethod
    def _selected(locator: Any) -> bool | None:
        return locator.evaluate(r"""e => {
            const input=e.matches('input')?e:e.querySelector('input[type=radio],input[type=checkbox]');
            if(input) return input.checked;
            const aria=e.getAttribute('aria-checked'); if(aria!==null)return aria==='true';
            const items=[e,...e.querySelectorAll('span,i,a')];
            if(items.some(n=>/(^|\s)(check_answer(?:_dx)?|checkAnswer|checked|selected|active|cur|on)(\s|$)/.test(n.className||'')))return true;
            if(e.matches('[role=radio],[role=checkbox],li,.option,label'))return false;
            return null;
        }""")

    def _validate_answers(self, answers: list[Answer]) -> None:
        self._guard()
        if not self._questions:
            raise BrowserError("请先读取题目，再填写答案。")
        mapped = {answer.question_id: answer for answer in answers}
        if len(mapped) != len(answers) or set(mapped) != set(self._questions):
            raise BrowserError("答案与当前题目不完整对应，已停止填写。")
        for question_id, question in self._questions.items():
            answer = mapped[question_id]
            if question.kind in {"fill", "short"}:
                expected = question.blank_count if question.kind == "fill" else 1
                if (answer.choices or expected < 1 or len(answer.texts) != expected
                        or len(self._question_controls[question_id]) != expected
                        or any(not isinstance(text, str) or not text.strip() for text in answer.texts)):
                    raise BrowserError(f"题目 {question_id} 的文本答案与作答区域不完整对应。")
                continue
            if answer.texts:
                raise BrowserError(f"题目 {question_id} 应使用选项答案。")
            choices = tuple(str(choice).strip().upper() for choice in answer.choices)
            if not choices or len(set(choices)) != len(choices):
                raise BrowserError(f"题目 {question_id} 的答案为空或重复。")
            if question.kind != "multiple" and len(choices) != 1:
                raise BrowserError(f"题目 {question_id} 只能选择一个答案。")
            if not set(choices) <= set(self._question_controls[question_id]):
                raise BrowserError(f"题目 {question_id} 包含无效选项。")

    def apply_answers(self, answers: list[Answer]) -> None:
        self._validate_answers(answers)
        mapped = {answer.question_id: answer for answer in answers}
        for question_id, question in self._questions.items():
            if question.kind in {"fill", "short"}:
                for control, text in zip(self._question_controls[question_id].values(), mapped[question_id].texts):
                    self._guard()
                    control = self._resolve_text_answer_control(control)
                    self._guard()
                    self._click(control)
                    self._guard()
                    control.press("ControlOrMeta+A", timeout=3000)
                    self._guard()
                    self._page.keyboard.insert_text(text)
                    self._guard()
                    actual = control.evaluate("e => ({text: 'value' in e ? e.value : e.innerText, rich: !('value' in e)})")
                    actual = self._trim_rich_text_tail(control, actual)
                    if self._answer_text(actual["text"], rich=actual["rich"]) != self._answer_text(text, rich=actual["rich"]):
                        raise BrowserError(f"无法确认题目 {question_id} 的文本答案已完整填写。")
                self.log(f"已填写题目 {question_id}：{len(mapped[question_id].texts)} 个文本区域")
                continue
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
        if self._navigation_mode:
            key = id(self._quiz_target)
            cached = self._nav_result_visible.get(key)
            if cached is not None:
                return cached
        try:
            roots = self._quiz_roots()
        except BrowserError:
            raise
        except Exception:
            # The next poll can re-resolve the current locator. Never use a
            # different assessment's grade when this scope is transitioning.
            return False
        if not roots:
            return False
        result = any(self._result_probe_cached(frame) for frame in roots)
        if self._navigation_mode:
            self._nav_result_visible[id(self._quiz_target)] = result
        return result

    def _frame_result_visible(self, frame: Any) -> bool:
        if not self._frame_visible_memo(frame):
            return False
        try:
            # Inspect the assessment document itself. Course outlines in an
            # outer frame can list other chapters as "已提交".
            quiz_structure = frame.locator(
                ".TiMu, [data-question-id], .Zy_TItle, .Py_answer, .mark_answer, "
                "#submission-result, .submit-success, .answerResult, #workForm, .workForm")
            url = getattr(frame, "url", "")
            scoped = getattr(frame, "element_handle", None) is not None
            if not quiz_structure.count() and not scoped and not re.search(
                    r"(?:quiz|/work/|homework|exam|doHomeWork)", urlparse(url).path, re.I):
                return False
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
            # doHomeWorkNew renders per-question grades as a bare number and
            # "分", without a score label. Match its read-only answer structure
            # so numbers quoted in stems/options cannot confirm submission.
            graded = frame.locator(".newAnswerBx .myAnswerBx")
            for index in range(min(graded.count(), 60)):
                result = graded.nth(index)
                if not self._visible(result):
                    continue
                labels = result.locator(".myAnswer .answerFont")
                if not any(self._visible(labels.nth(i)) and re.match(
                        r"^我的答案\s*[:：]", _clean(labels.nth(i).inner_text(timeout=800)))
                        for i in range(min(labels.count(), 10))):
                    continue
                controls = result.locator(
                    "input:not([type=hidden]), textarea, [contenteditable=true], [role=textbox]")
                if any(self._visible(controls.nth(i)) for i in range(min(controls.count(), 30))):
                    continue
                grades = result.locator(".answerScore .scoreNum")
                if any(self._visible(grades.nth(i)) and re.fullmatch(
                        r"\d+(?:\.\d+)?", _clean(grades.nth(i).inner_text(timeout=800)))
                        for i in range(min(grades.count(), 10))):
                    return True
                # A submitted text answer may await teacher grading. Read the
                # assessment's status or this answer's grade area, not stems.
                for review in (frame.locator(".newTestTitle .testTit_status"),
                               result.locator(".answerScore")):
                    if any(self._visible(review.nth(i)) and re.fullmatch(
                            r"(?:待|等待)(?:批阅|批改|评分)",
                            _clean(review.nth(i).inner_text(timeout=800)))
                            for i in range(min(review.count(), 10))):
                        return True
        except Exception:
            pass
        return False

    def quiz_completed(self) -> bool:
        """Read a visible submitted/graded result in the current assessment."""
        self._guard()
        return self._result_visible()

    def _quiz_dialog_roots(self) -> list[Any]:
        if self._quiz_target is None:
            return self._frames()
        roots = []
        handle = self._quiz_target.element_handle(timeout=1500)
        if handle is None:
            return []
        try:
            frame = handle.owner_frame()
        finally:
            handle.dispose()
        while frame is not None:
            roots.append(frame)
            frame = frame.parent_frame
        return roots

    def submit_quiz(self) -> bool:
        self._guard()
        if self._result_visible():
            return True
        deadline = time.monotonic() + 20
        submit = None
        while time.monotonic() < deadline:
            try:
                roots = self._quiz_roots()
            except BrowserError:
                raise
            except Exception:
                roots = []
            for root in roots:
                submit = self._text_control(_SUBMIT, root=root)
                if submit is not None:
                    break
            if submit is not None:
                break
            # The current frame may be replacing its document or already
            # showing the result. Retry only its own scope, without clicking.
            if self._result_visible():
                return True
            self._wait(0.3)
            self._guard()
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
                try:
                    dialog_roots = self._quiz_dialog_roots()
                except BrowserError:
                    raise
                except Exception:
                    self._wait(0.3)
                    continue
                for frame in dialog_roots:
                    try:
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
                    except BrowserError:
                        raise
                    except Exception:
                        continue
                self._wait(0.3)
            raise BrowserError("提交后没有检测到明确的成功结果，请人工确认，程序不会重复提交。")
        finally:
            self._submitting = False

    def _left_learning_page(self) -> bool:
        """A settled overview/completion page, rather than a blank transition."""
        try:
            return bool(self._page.evaluate("""() => {
                if(document.readyState!=='complete' || !document.body)return false;
                const text=(document.body.innerText || '').replace(/\\s+/g,' ').trim();
                if(!text || /^(?:正在)?加载(?:中)?[.。…\\s]*$|^请稍[候等][.。…\\s]*$/.test(text))return false;
                const hasFrame=[...document.querySelectorAll('iframe')].some(e=>{
                    const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                    return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
                });
                if(hasFrame)return false;
                return /课程已完成|课程学习完成|全部章节已完成|没有下一节|已完成全部|课程.{0,8}(?:结束|完成)/.test(text)
                    || !!document.querySelector('[data-testid=course-complete], .course-complete, .courseComplete, '
                        + '.course-list, .courseList, .learncourse-list, .course-overview');
            }"""))
        except Exception:
            return False

    def _next_warning(self) -> Any | None:
        """Find the next-section control inside the known unfinished-task prompt."""
        if self._navigation_mode and not self._warning_text_present():
            return None
        def control(container: Any) -> Any | None:
            if not self._visible(container):
                return None
            text = _clean(container.inner_text(timeout=1000))
            if not _NEXT_TASK_WARNING.search(text):
                return None
            if self._text_control(re.compile(r"^去学习$"), root=container) is None:
                return None
            return self._text_control(_NEXT, root=container)

        def find(frame: Any) -> Any | None:
            dialogs = frame.locator(_NEXT_WARNING_ROOTS)
            for index in range(min(dialogs.count(), 30)):
                target = control(dialogs.nth(index))
                if target is not None:
                    return target
            # Some skins have no dialog role or familiar class. Start at the
            # prompt and find its nearest floating container with both actions;
            # never fall back to searching the whole page's footer.
            prompts = frame.get_by_text(_NEXT_TASK_WARNING)
            for index in range(min(prompts.count(), 15)):
                container = prompts.nth(index)
                for _depth in range(8):
                    tag = container.evaluate("e => e.tagName")
                    if tag in {"HTML", "BODY"}:
                        break
                    floating = container.evaluate("""e => {
                        for(let n=e;n && !n.matches('body,html');n=n.parentElement){
                            if(['fixed','absolute'].includes(getComputedStyle(n).position))return true;
                        }
                        return false;
                    }""")
                    if floating:
                        target = control(container)
                        if target is not None:
                            return target
                    container = container.locator("xpath=..")
            return None

        for frame in self._frames():
            if not self._frame_visible_memo(frame):
                continue
            try:
                target = find(frame)
                if target is not None:
                    return target
            except StopRequested:
                raise
            except Exception:
                # Navigation may replace the frame while inspecting its layer.
                continue
        return None

    def next_chapter(self) -> bool:
        self._navigation_mode = True
        self._reset_navigation_caches()
        try:
            return self._next_chapter()
        finally:
            self._navigation_mode = False
            self._reset_navigation_caches()

    def _next_chapter(self) -> bool:
        self._guard()
        warning = self._next_warning()
        include_occluded = warning is not None
        previous_kind = self.chapter_kind()
        # A warning already open at entry can cover the old player. Snapshot
        # its real material as well, so merely closing the layer cannot look
        # like navigation to a newly visible video.
        if include_occluded and previous_kind != "quiz" and self._video_locators(include_occluded=True):
            previous_kind = "video"
        previous, _previous_ready = (self._view_signature(previous_kind, include_occluded=True)
                                     if include_occluded else self._view_signature(previous_kind))
        previous_chapter = self._chapter_identifier()
        previous_documents = (self._content_document_tokens(previous_kind, include_occluded=True)
                              if include_occluded else self._content_document_tokens(previous_kind))
        previous_url = self.current_url
        button = warning if warning is not None else self._text_control(_NEXT)
        if button is None:
            return False
        if button.get_attribute("aria-disabled") == "true" or button.get_attribute("disabled") is not None:
            if warning is not None:
                raise BrowserError("任务点提示框中的下一节按钮当前不可用。")
            return False
        if re.search(r"(?:^|\s)(?:disabled|disable)(?:\s|$)", button.get_attribute("class") or ""):
            if warning is not None:
                raise BrowserError("任务点提示框中的下一节按钮当前不可用。")
            return False
        if warning is not None:
            self.log("检测到未完成任务点提示，点击提示框内的下一节。")
        self._click(button, humanized=False)
        warning_confirmed = warning is not None
        deadline = time.monotonic() + self.NEXT_PAGE_TIMEOUT
        candidate, candidate_since = None, 0.0
        departed_since = 0.0
        kind, content_ready = previous_kind, _previous_ready
        while time.monotonic() < deadline:
            # One discovery pass per poll; chapter_kind, the signature and the
            # warning/guard checks share its cached frames and probes.
            self._reset_navigation_caches()
            self._guard()
            warning = self._next_warning()
            if warning is not None:
                candidate, candidate_since, departed_since = None, 0.0, 0.0
                if not warning_confirmed:
                    try:
                        if re.search(r"(?:^|\s)(?:disabled|disable)(?:\s|$)", warning.get_attribute("class") or ""):
                            raise BrowserError("任务点提示框中的下一节按钮当前不可用。")
                        self.log("检测到未完成任务点提示，点击提示框内的下一节。")
                        self._click(warning, humanized=False)
                    except BrowserError:
                        raise
                    except Exception:
                        # Frame replacement can take longer than a handful of
                        # polls. Retry within the existing transition deadline.
                        self._wait(0.1)
                        continue
                    warning_confirmed = True
                    deadline = time.monotonic() + self.NEXT_PAGE_TIMEOUT
                self._wait(0.25)
                continue
            try:
                ready = self.poll_ready()
                kind = self.chapter_kind()
                signature, content_ready = (self._view_signature(kind, include_occluded=True)
                                           if include_occluded else self._view_signature(kind))
                # A valid reader may have neither Chaoxing URL markers nor a
                # quiz tab. Explicit content recognition is sufficient after
                # the click.
                chapter = self._chapter_identifier()
            except (BrowserError, StopRequested):
                raise
            except Exception:
                # The fast click can replace the old frame while a probe is
                # still resolving a locator. Treat that as a transient poll
                # failure and retry on the new document.
                candidate, candidate_since, departed_since = None, 0.0, 0.0
                self._wait(0.1)
                continue
            changed = kind != previous_kind or signature != previous
            if include_occluded and kind in {"video", "document", "mixed"} and previous_kind in {"video", "document", "mixed"}:
                changed = signature != previous
            if (content_ready and not changed and chapter != previous_chapter
                    and previous_documents):
                documents = (self._content_document_tokens(kind, include_occluded=True)
                             if include_occluded else self._content_document_tokens(kind))
                changed = documents != previous_documents
            if content_ready and signature and changed:
                current = (kind, signature)
                if current != candidate:
                    candidate, candidate_since = current, time.monotonic()
                elif time.monotonic() - candidate_since >= self.NEXT_PAGE_SETTLE:
                    self._question_controls.clear()
                    self._questions.clear()
                    # Handles from the old document are no longer safe to
                    # compare or evaluate once the transition is accepted.
                    self._clear_content_nodes()
                    self._clear_task_regions()
                    self._quiz_view_rows.clear()
                    self._chapter_page_selected = True
                    self.log("已进入下一学习页面。")
                    return True
            else:
                candidate, candidate_since = None, 0.0
            if (not ready and not content_ready and self.current_url != previous_url
                    and self._left_learning_page()):
                # Completion/overview pages end the workflow. An empty frame
                # inside the chapter is a loading state and keeps waiting.
                if not departed_since:
                    departed_since = time.monotonic()
                if time.monotonic() - departed_since >= 1.0:
                    self.log("已离开章节页面，没有下一节。")
                    return False
            else:
                departed_since = 0.0
            self._wait(0.25)
        if kind == "quiz" and not content_ready:
            raise BrowserError("已检测到答题内容，但未能完整读取题干和选项，请检查页面是否加载完成。")
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
        self._clear_content_nodes()
        self._clear_task_regions()
        self._clear_pointer_guards()
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
        self._quiz_view_rows.clear()
        self._quiz_target = None
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
