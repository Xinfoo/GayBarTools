"""Transition guards without real clocks, browser accounts or network calls."""
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from gaybar_tools.browser import BrowserError, BrowserSession
from gaybar_tools.config import AppSettings
from gaybar_tools.paths import AppPaths


class NextPageGuardTests(unittest.TestCase):
    def setUp(self):
        self.session = BrowserSession(AppSettings(), AppPaths.for_root(Path('/tmp/unused-next-page-guard')),
                                      lambda _message: None, threading.Event())
        self.clock = 0.0
        self.page = SimpleNamespace(url='https://example.test/chapter?chapterId=1', is_closed=lambda: False)
        self.session._page = self.page
        self.button = Mock()
        self.button.get_attribute.return_value = None
        self.patches = [
            patch.object(self.session, '_guard'),
            patch.object(self.session, '_text_control', return_value=self.button),
            patch.object(self.session, '_next_warning', return_value=None),
            patch.object(self.session, '_click'),
            patch.object(self.session, 'poll_ready', return_value=True),
            patch.object(self.session, 'chapter_kind', return_value='document'),
            patch.object(self.session, '_chapter_identifier', return_value='chapter-1'),
            patch.object(self.session, '_content_document_tokens', return_value=((1, 100),)),
            patch.object(self.session, '_left_learning_page', return_value=False),
            patch.object(self.session, '_wait', side_effect=self.advance),
            patch('gaybar_tools.browser.time.monotonic', side_effect=lambda: self.clock),
            patch.object(self.session, 'NEXT_PAGE_TIMEOUT', 3),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def advance(self, seconds):
        self.clock += seconds

    def test_disabled_next_does_not_click(self):
        self.button.get_attribute.side_effect = lambda name: 'true' if name == 'aria-disabled' else None
        with patch.object(self.session, '_view_signature', return_value=('old', True)):
            self.assertFalse(self.session.next_chapter())
        self.session._click.assert_not_called()

    def test_unchanged_old_content_times_out_despite_outer_chapter_change(self):
        self.session._chapter_identifier.side_effect = lambda: 'chapter-1' if self.clock < 0.5 else 'chapter-2'
        with patch.object(self.session, '_view_signature', return_value=('old', True)):
            with self.assertRaisesRegex(BrowserError, '章节没有变化'):
                self.session.next_chapter()

    def test_empty_loading_and_transient_view_wait_for_stable_content(self):
        self.session.poll_ready.return_value = False
        self.session.chapter_kind.side_effect = lambda: 'document' if self.clock < 0.25 else (
            'unknown' if self.clock < 1.25 else 'quiz')
        def snapshot(kind):
            if kind == 'unknown':
                return '', False
            return ('old', True) if kind == 'document' else ('questions', True)
        with patch.object(self.session, '_view_signature', side_effect=snapshot):
            self.assertTrue(self.session.next_chapter())
        self.assertGreaterEqual(self.clock, 1.75)
        self.assertTrue(self.session._chapter_page_selected)

    def test_known_url_with_incomplete_quiz_options_keeps_waiting(self):
        def snapshot(_kind):
            if self.clock < 0.25:
                return 'old', True
            return 'new-questions', self.clock >= 1.25
        with patch.object(self.session, '_view_signature', side_effect=snapshot):
            self.assertTrue(self.session.next_chapter())
        self.assertGreaterEqual(self.clock, 1.75)

    def test_same_material_new_chapter_requires_new_content_document(self):
        self.session._chapter_identifier.side_effect = lambda: 'chapter-1' if self.clock < 0.25 else 'chapter-2'
        self.session._content_document_tokens.side_effect = lambda _kind: ((1, 100),) if self.clock < 1 else ((1, 200),)
        with patch.object(self.session, '_view_signature', return_value=('same-material', True)):
            self.assertTrue(self.session.next_chapter())
        self.assertGreaterEqual(self.clock, 1.5)

    def test_incomplete_quiz_reports_question_recognition_instead_of_unchanged_chapter(self):
        self.session.chapter_kind.side_effect = lambda: 'document' if self.clock == 0 else 'quiz'
        with patch.object(self.session, '_view_signature', side_effect=lambda kind:
                          ('old', True) if kind == 'document' else ('questions', False)):
            with self.assertRaisesRegex(BrowserError, '已检测到答题内容.*题干和选项'):
                self.session.next_chapter()

    def test_unknown_nonempty_destination_is_not_completion(self):
        self.session.poll_ready.return_value = False
        self.session.chapter_kind.side_effect = lambda: 'document' if self.clock == 0 else 'unknown'
        self.session._wait.side_effect = lambda seconds: (self.advance(seconds), setattr(self.page, 'url', 'https://example.test/loading'))
        with patch.object(self.session, '_view_signature', side_effect=lambda kind: ('old', True) if kind == 'document' else ('', False)):
            with self.assertRaisesRegex(BrowserError, '章节没有变化'):
                self.session.next_chapter()

    def test_detached_frame_during_poll_is_retried_instead_of_stopping(self):
        calls = {"count": 0}

        def chapter_kind():
            calls["count"] += 1
            if calls["count"] == 1:
                return "document"
            if calls["count"] == 2:
                raise RuntimeError("Locator.count: Frame was detached")
            return "quiz"

        self.session.chapter_kind.side_effect = chapter_kind
        with patch.object(self.session, "_view_signature",
                          side_effect=lambda kind: ("old", True) if kind == "document"
                          else ("questions", True)):
            self.assertTrue(self.session.next_chapter())
        self.assertGreaterEqual(calls["count"], 3)

    def test_next_button_uses_the_fast_click_path(self):
        with patch.object(self.session, '_view_signature', return_value=('old', True)):
            with self.assertRaisesRegex(BrowserError, '章节没有变化'):
                self.session.next_chapter()
        self.session._click.assert_called_once_with(self.button, humanized=False)

    def test_repeated_detachments_recover_within_transition_deadline(self):
        probes = []

        def snapshot(_kind):
            if not probes:
                probes.append("initial")
                return "old", True
            probes.append(self.clock)
            if self.clock < 1.2:
                raise RuntimeError("Frame was detached")
            return "new", True

        with patch.object(self.session, "_view_signature", side_effect=snapshot):
            self.assertTrue(self.session.next_chapter())
        self.assertGreater(len(probes), 8)
        self.assertGreaterEqual(self.clock, 1.7)
        self.assertLess(self.clock, self.session.NEXT_PAGE_TIMEOUT)

    def test_persistent_detachments_use_deadline_and_keep_single_next_click(self):
        probes = {"count": 0}

        def snapshot(_kind):
            probes["count"] += 1
            if probes["count"] == 1:
                return "old", True
            raise RuntimeError("Frame was detached")

        with patch.object(self.session, "_view_signature", side_effect=snapshot):
            with self.assertRaisesRegex(BrowserError, "章节没有变化"):
                self.session.next_chapter()
        self.assertGreaterEqual(self.clock, self.session.NEXT_PAGE_TIMEOUT)
        self.assertLess(self.clock, self.session.NEXT_PAGE_TIMEOUT + 0.11)
        self.session._click.assert_called_once_with(self.button, humanized=False)

    def test_warning_frame_retries_beyond_eight_failures_then_confirms_once(self):
        warning = Mock()
        warning_reads = {"count": 0}

        def warning_class(_name):
            warning_reads["count"] += 1
            if warning_reads["count"] <= 12:
                raise RuntimeError("Frame was detached")
            return ""

        warning.get_attribute.side_effect = warning_class
        self.session._next_warning.side_effect = lambda: (
            warning if 0.1 <= self.clock < 1.5 else None)
        with patch.object(self.session, "_view_signature", side_effect=lambda _kind:
                          ("old", True) if self.clock < 1.5 else ("new", True)):
            self.assertTrue(self.session.next_chapter())
        self.assertEqual(warning_reads["count"], 13)
        self.assertEqual(self.session._click.call_count, 2)
        self.session._click.assert_any_call(warning, humanized=False)

    def test_explicit_completion_destination_ends_normally(self):
        self.session.poll_ready.return_value = False
        self.session.chapter_kind.side_effect = lambda: 'document' if self.clock == 0 else 'unknown'
        self.session._left_learning_page.return_value = True
        self.session._wait.side_effect = lambda seconds: (self.advance(seconds), setattr(self.page, 'url', 'https://example.test/complete'))
        with patch.object(self.session, '_view_signature', side_effect=lambda kind: ('old', True) if kind == 'document' else ('', False)):
            self.assertFalse(self.session.next_chapter())
        self.assertGreaterEqual(self.clock, 1.25)


if __name__ == '__main__':
    unittest.main()
