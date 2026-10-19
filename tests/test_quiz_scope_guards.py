"""Assessment-scope recovery without browsers, accounts, or real waits."""
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from gaybar_tools.browser import BrowserError, BrowserSession, StopRequested
from gaybar_tools.config import AppSettings
from gaybar_tools.paths import AppPaths


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


class DetachingFrame:
    @property
    def parent_frame(self):
        raise RuntimeError("frame is detaching")


class QuizScopeGuardTests(unittest.TestCase):
    def setUp(self):
        self.session = BrowserSession(
            AppSettings(), AppPaths.for_root(Path("/tmp/unused-quiz-scope")),
            lambda _message: None, threading.Event())
        self.target, self.sibling, self.button = object(), object(), object()
        self.session._quiz_target = self.target
        self.clock = Clock()
        self.guard = patch.object(self.session, "_guard").start()
        self.addCleanup(patch.stopall)

    def test_scope_failure_never_reads_sibling_results(self):
        with patch.object(self.session, "_quiz_roots", side_effect=RuntimeError("transition")), \
                patch.object(self.session, "_frames", return_value=[self.sibling]) as frames, \
                patch.object(self.session, "_result_probe_cached", return_value=True) as result:
            self.assertFalse(self.session.quiz_completed())
        frames.assert_not_called()
        result.assert_not_called()

    def test_transient_scope_failure_is_not_cached_as_incomplete(self):
        self.session._navigation_mode = True
        with patch.object(self.session, "_quiz_roots", side_effect=[RuntimeError("transition"), [self.target]]), \
                patch.object(self.session, "_result_probe_cached", return_value=True):
            self.assertFalse(self.session._result_visible())
            self.assertTrue(self.session._result_visible())

    def test_submit_retries_own_scope_without_using_sibling_button(self):
        with patch("gaybar_tools.browser.time.monotonic", self.clock.monotonic), \
                patch.object(self.session, "_wait", side_effect=self.clock.wait), \
                patch.object(self.session, "_result_visible", side_effect=[False, False, True]), \
                patch.object(self.session, "_quiz_roots", side_effect=[RuntimeError("transition"), [self.target]]), \
                patch.object(self.session, "_frames", return_value=[self.sibling]) as frames, \
                patch.object(self.session, "_text_control", return_value=self.button) as find, \
                patch.object(self.session, "_click") as click:
            self.assertTrue(self.session.submit_quiz())
        frames.assert_not_called()
        self.assertEqual([call.kwargs["root"] for call in find.call_args_list], [self.target])
        click.assert_called_once_with(self.button)
        self.assertAlmostEqual(self.clock.now, 0.3)

    def test_normal_submit_adds_no_wait_or_duplicate_preflight_guard(self):
        with patch("gaybar_tools.browser.time.monotonic", self.clock.monotonic), \
                patch.object(self.session, "_wait") as wait, \
                patch.object(self.session, "_result_visible", side_effect=[False, True]), \
                patch.object(self.session, "_quiz_roots", return_value=[self.target]), \
                patch.object(self.session, "_text_control", return_value=self.button), \
                patch.object(self.session, "_click") as click:
            self.assertTrue(self.session.submit_quiz())
        click.assert_called_once_with(self.button)
        wait.assert_not_called()
        self.assertEqual(self.guard.call_count, 2)  # Entry and post-click check.

    def test_unavailable_scope_wait_is_bounded_and_never_submits_sibling(self):
        with patch("gaybar_tools.browser.time.monotonic", self.clock.monotonic), \
                patch.object(self.session, "_wait", side_effect=self.clock.wait), \
                patch.object(self.session, "_quiz_roots", side_effect=RuntimeError("transition")), \
                patch.object(self.session, "_frames", return_value=[self.sibling]) as frames, \
                patch.object(self.session, "_text_control", return_value=self.button) as find, \
                patch.object(self.session, "_click") as click:
            with self.assertRaisesRegex(BrowserError, "提交按钮"):
                self.session.submit_quiz()
        frames.assert_not_called()
        find.assert_not_called()
        click.assert_not_called()
        self.assertGreaterEqual(self.clock.now, 20)
        self.assertLess(self.clock.now, 20.31)

    def test_result_scope_recovery_after_click_does_not_resubmit(self):
        with patch("gaybar_tools.browser.time.monotonic", self.clock.monotonic), \
                patch.object(self.session, "_wait", side_effect=self.clock.wait), \
                patch.object(self.session, "_quiz_roots", side_effect=[
                    [self.target], [self.target], RuntimeError("transition"), [self.target]]), \
                patch.object(self.session, "_quiz_dialog_roots", return_value=[]), \
                patch.object(self.session, "_text_control", return_value=self.button) as find, \
                patch.object(self.session, "_click") as click, \
                patch.object(self.session, "_result_probe_cached", side_effect=lambda _root: bool(click.call_count)):
            self.assertTrue(self.session.submit_quiz())
        click.assert_called_once_with(self.button)
        find.assert_called_once()
        self.assertFalse(self.session._submitting)

    def test_late_scope_recovery_keeps_full_post_submit_confirmation_window(self):
        clicked_at = None

        def find_submit(_pattern, *, root):
            self.assertIs(root, self.target)
            return self.button if self.clock.now >= 19 else None

        def click_submit(_button):
            nonlocal clicked_at
            clicked_at = self.clock.now

        def result_visible():
            return clicked_at is not None and self.clock.now >= clicked_at + 2

        with patch("gaybar_tools.browser.time.monotonic", self.clock.monotonic), \
                patch.object(self.session, "_wait", side_effect=self.clock.wait), \
                patch.object(self.session, "_quiz_roots", return_value=[self.target]), \
                patch.object(self.session, "_quiz_dialog_roots", return_value=[]), \
                patch.object(self.session, "_text_control", side_effect=find_submit), \
                patch.object(self.session, "_click", side_effect=click_submit) as click, \
                patch.object(self.session, "_result_visible", side_effect=result_visible):
            self.assertTrue(self.session.submit_quiz())
        click.assert_called_once_with(self.button)
        self.assertGreater(self.clock.now, 20)

    def test_retry_preserves_cancellation(self):
        with patch("gaybar_tools.browser.time.monotonic", self.clock.monotonic), \
                patch.object(self.session, "_quiz_roots", side_effect=RuntimeError("transition")), \
                patch.object(self.session, "_wait", side_effect=StopRequested("已停止")), \
                patch.object(self.session, "_click") as click:
            with self.assertRaises(StopRequested):
                self.session.submit_quiz()
        click.assert_not_called()

    def test_detaching_sibling_does_not_hide_valid_scoped_descendant(self):
        owner = SimpleNamespace(parent_frame=None)
        included = SimpleNamespace(in_scope=True, dispose=Mock())
        excluded = SimpleNamespace(in_scope=False, dispose=Mock())
        child = SimpleNamespace(parent_frame=owner, frame_element=lambda: included)
        sibling = SimpleNamespace(parent_frame=owner, frame_element=lambda: excluded)
        handle = SimpleNamespace(owner_frame=lambda: owner, dispose=Mock(),
                                 evaluate=lambda _script, element: element.in_scope)
        target = SimpleNamespace(element_handle=lambda **_kwargs: handle)
        with patch.object(self.session, "_frames", return_value=[owner, DetachingFrame(), sibling, child]):
            self.assertEqual(self.session._quiz_roots_for(target), [target, child])
        handle.dispose.assert_called_once()
        included.dispose.assert_called_once()
        excluded.dispose.assert_called_once()

    def test_missing_scoped_target_is_re_resolved_without_widening(self):
        owner = SimpleNamespace(parent_frame=None)
        handle = SimpleNamespace(owner_frame=lambda: owner, dispose=Mock())
        target = SimpleNamespace(element_handle=Mock(side_effect=[None, handle]))
        with patch.object(self.session, "_frames", return_value=[owner]):
            self.assertEqual(self.session._quiz_roots_for(target), [])
            self.assertEqual(self.session._quiz_roots_for(target), [target])
        self.assertEqual(target.element_handle.call_count, 2)

    def test_missing_target_does_not_search_sibling_confirmation_dialogs(self):
        self.session._quiz_target = SimpleNamespace(element_handle=lambda **_kwargs: None)
        with patch.object(self.session, "_frames", return_value=[self.sibling]) as frames:
            self.assertEqual(self.session._quiz_dialog_roots(), [])
        frames.assert_not_called()

    def test_browser_errors_are_not_swallowed_as_scope_transitions(self):
        with patch.object(self.session, "_quiz_roots", side_effect=BrowserError("页面已关闭")):
            with self.assertRaisesRegex(BrowserError, "页面已关闭"):
                self.session._result_visible()


if __name__ == "__main__":
    unittest.main()
