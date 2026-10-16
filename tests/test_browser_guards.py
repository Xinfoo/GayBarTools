"""Focused checks for assessment results without launching a browser."""
from pathlib import Path
import re
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from gaybar_tools.browser import BrowserError, BrowserSession
from gaybar_tools.config import AppSettings
from gaybar_tools.paths import AppPaths


class Node:
    def __init__(self, text, visible=True):
        self.text, self.visible = text, visible

    def inner_text(self, **_kwargs):
        return self.text

    def is_visible(self):
        return self.visible


class Nodes:
    def __init__(self, nodes=()):
        self.nodes = list(nodes)

    def count(self):
        return len(self.nodes)

    def nth(self, index):
        return self.nodes[index]


class Frame:
    def __init__(self, texts, *, quiz=True, visible=True):
        self.url = "https://example.test/quiz.html" if quiz else "https://example.test/course.html"
        self.nodes = [Node(text, visible) for text in texts]
        self.quiz = quiz

    def locator(self, selector):
        if ".TiMu" in selector:
            return Nodes([Node("测验")] if self.quiz else [])
        return Nodes(self.nodes)

    def get_by_text(self, pattern):
        return Nodes(node for node in self.nodes if re.search(pattern, node.text))


class BrowserResultGuardTests(unittest.TestCase):
    def setUp(self):
        self.session = BrowserSession(AppSettings(), AppPaths.for_root(Path("/tmp/unused-browser-guard")),
                                      lambda _message: None, threading.Event())

    def completed(self, frame):
        with patch.object(self.session, "_frames", return_value=[frame]), patch.object(self.session, "_guard"):
            return self.session.quiz_completed()

    def test_mine_only_is_not_a_submitted_result(self):
        self.assertFalse(self.completed(Frame(["我的答案：B"])))

    def test_mine_and_score_are_a_graded_result(self):
        self.assertTrue(self.completed(Frame(["我的答案：B", "本次成绩：100 分"])))

    def test_explicit_submission_success_is_complete(self):
        self.assertTrue(self.completed(Frame(["提交成功 · 已完成本节学习检测。"])))

    def test_hidden_results_and_outer_course_sidebar_do_not_complete(self):
        self.assertFalse(self.completed(Frame(["提交成功"], visible=False)))
        self.assertFalse(self.completed(Frame(["上一章节已提交"], quiz=False)))

    def test_completed_newer_course_does_not_fall_back_to_an_older_course(self):
        older, newer = Mock(), Mock()
        older.is_closed.return_value = newer.is_closed.return_value = False
        self.session._context = SimpleNamespace(pages=[older, newer])
        self.session._page = older
        ready_pages = [older, newer]
        with patch.object(self.session, "_guard"), patch.object(
                self.session, "_is_chapter_page", side_effect=lambda page: page in ready_pages):
            self.assertTrue(self.session.poll_ready())
            self.assertIs(self.session.page, newer)
            ready_pages.remove(newer)
            self.assertFalse(self.session.poll_ready())
            self.assertIs(self.session.page, newer)


class ReadyFrame:
    def __init__(self, url="https://example.test/course", *, selectors=(), quiz=False,
                 body="", parent=None, visible=True, broken=False):
        self.url, self.selectors, self.quiz, self.body = url, selectors, quiz, body
        self.parent_frame, self.visible, self.broken = parent, visible, broken

    def is_detached(self):
        if self.broken:
            raise RuntimeError("frame is navigating")
        return False

    def frame_element(self):
        return SimpleNamespace(is_visible=lambda: self.visible, dispose=lambda: None)

    def locator(self, selector):
        if selector == "body":
            return SimpleNamespace(inner_text=lambda **_kwargs: self.body)
        return Nodes(Node("内容") for item in self.selectors if item in selector.split(", "))

    def get_by_text(self, _pattern, **_kwargs):
        return Nodes([Node("2 学习检测")] if self.quiz else [])


class ReadyPage:
    def __init__(self, frames):
        self.frames, self.closed, self.pump = frames, False, lambda: None
        self.handlers = []

    @property
    def url(self):
        return self.frames[0].url

    def is_closed(self):
        return self.closed

    def wait_for_timeout(self, _milliseconds):
        self.pump()

    def on(self, event, handler):
        self.handlers.append((event, handler))


class BrowserReadinessTests(unittest.TestCase):
    def setUp(self):
        self.session = BrowserSession(AppSettings(), AppPaths.for_root(Path("/tmp/unused-browser-guard")),
                                      lambda _message: None, threading.Event())

    def connect(self, *pages):
        self.session._context = SimpleNamespace(pages=list(pages))
        self.session._page = pages[0]

    def test_nested_frame_identifier_does_not_require_outer_chapter_query(self):
        outer = ReadyFrame()
        child = ReadyFrame("https://example.test/embed?knowledgeId=12", selectors=("video",), parent=outer)
        self.assertTrue(self.session._is_chapter_page(ReadyPage([outer, child])))

    def test_visible_chapter_controls_do_not_require_known_url(self):
        self.assertTrue(self.session._is_chapter_page(ReadyPage([ReadyFrame(selectors=(".TiMu",))])))
        self.assertTrue(self.session._is_chapter_page(ReadyPage([ReadyFrame(selectors=(".chapter-content",))])))

    def test_activity_tab_caption_alone_does_not_identify_a_learning_page(self):
        self.assertFalse(self.session._is_chapter_page(ReadyPage([ReadyFrame(quiz=True)])))

    def test_homepage_media_and_generic_iframe_are_not_chapter_controls(self):
        self.assertFalse(self.session._is_chapter_page(ReadyPage([ReadyFrame(selectors=("video", "#iframe"))])))

    def test_detaching_sibling_does_not_hide_loaded_chapter(self):
        self.assertTrue(self.session._is_chapter_page(
            ReadyPage([ReadyFrame(broken=True), ReadyFrame(selectors=(".chapter-content",))])))

    def test_poll_dispatches_popup_events_before_reading_page_cache(self):
        home = ReadyPage([ReadyFrame()])
        chapter = ReadyPage([ReadyFrame("https://example.test/mycourse/studentstudy")])
        self.connect(home)
        home.pump = lambda: self.session._context.pages.append(chapter)
        self.assertTrue(self.session.poll_ready())
        self.assertIs(self.session.page, chapter)

    def test_poll_does_not_swallow_login_or_safety_errors(self):
        page = ReadyPage([ReadyFrame("https://example.test/mycourse/studentstudy")])
        self.connect(page)
        with patch.object(self.session, "_guard", side_effect=BrowserError("请手动处理安全验证")):
            with self.assertRaisesRegex(BrowserError, "安全验证"):
                self.session.poll_ready()

    def test_prepare_start_uses_content_even_when_readiness_detection_fails(self):
        page = ReadyPage([ReadyFrame(selectors=("#reader",))])
        self.connect(page)
        with patch.object(self.session, "chapter_kind", return_value="document"):
            self.assertFalse(self.session.poll_ready())
            self.assertEqual(self.session.prepare_start(), "document")

    def test_prepare_start_inspects_newer_reader_instead_of_older_recognized_course(self):
        older = ReadyPage([ReadyFrame("https://example.test/mycourse/studentstudy")])
        newer = ReadyPage([ReadyFrame(selectors=("#reader",))])
        self.connect(older, newer)
        with patch.object(self.session, "chapter_kind", return_value="document"):
            self.assertTrue(self.session.poll_ready())
            self.assertIs(self.session.page, older)
            self.assertEqual(self.session.prepare_start(), "document")
            self.assertIs(self.session.page, newer)

    def test_prepare_start_reports_login_instead_of_an_unrecognized_chapter(self):
        self.connect(ReadyPage([ReadyFrame("https://passport2.chaoxing.com/login")]))
        with self.assertRaisesRegex(BrowserError, "登录"):
            self.session.prepare_start()

    def test_hidden_and_empty_login_frames_do_not_block_course(self):
        main = ReadyFrame("https://example.test/mycourse/studentstudy")
        hidden = ReadyFrame("https://passport2.chaoxing.com/login", parent=main, visible=False,
                            body="请先登录")
        empty = ReadyFrame("https://passport2.chaoxing.com/login", parent=main)
        self.connect(ReadyPage([main, hidden, empty]))
        self.assertTrue(self.session.poll_ready())

    def test_login_frame_in_hidden_ancestor_does_not_block_course(self):
        main = ReadyFrame("https://example.test/mycourse/studentstudy")
        hidden_parent = ReadyFrame(parent=main, visible=False)
        login = ReadyFrame("https://passport2.chaoxing.com/login", parent=hidden_parent,
                           body="请先登录")
        self.connect(ReadyPage([main, hidden_parent, login]))
        self.assertTrue(self.session.poll_ready())

    def test_visible_login_frame_still_requires_manual_login(self):
        main = ReadyFrame("https://example.test/mycourse/studentstudy")
        login = ReadyFrame("https://passport2.chaoxing.com/login", parent=main, body="账号登录")
        self.connect(ReadyPage([main, login]))
        with self.assertRaisesRegex(BrowserError, "登录"):
            self.session.poll_ready()

    def test_closed_newer_course_does_not_resume_older_course(self):
        older = ReadyPage([ReadyFrame("https://example.test/mycourse/studentstudy")])
        newer = ReadyPage([ReadyFrame("https://example.test/mycourse/studentstudy")])
        self.connect(older, newer)
        self.assertTrue(self.session.poll_ready())
        newer.closed = True
        self.assertFalse(self.session.poll_ready())
        with self.assertRaisesRegex(BrowserError, "已关闭"):
            self.session.prepare_start()
        newest = ReadyPage([ReadyFrame("https://example.test/mycourse/studentstudy")])
        self.session._context.pages.append(newest)
        self.assertTrue(self.session.poll_ready())
        self.assertIs(self.session.page, newest)

    def test_page_dialog_handler_is_attached_only_once(self):
        page = ReadyPage([ReadyFrame()])
        self.session._attach_page(page)
        self.session._attach_page(page)
        self.assertEqual(len(page.handlers), 1)


if __name__ == "__main__":
    unittest.main()
