"""Focused checks for assessment results without launching a browser."""
from pathlib import Path
import re
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from gaybar_tools.browser import BrowserSession
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


if __name__ == "__main__":
    unittest.main()
