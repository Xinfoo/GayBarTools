"""Cancellation, atomic validation, and timers independent of a browser binary."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gaybar_tools.browser import BrowserError, BrowserSession, StopRequested
from gaybar_tools.config import AppSettings
from gaybar_tools.models import Answer, Option, Question
from gaybar_tools.paths import AppPaths


def question(identifier="video-question"):
    return Question(identifier, "判断题：是否正确？", "boolean", (Option("A", "对"), Option("B", "错")))


def media_state(*, paused=False, ended=False):
    return {"paused": paused, "ended": ended, "time": 0 if paused else 3,
            "duration": 3, "rate": 1, "error": None}


class VideoQuizGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gaybar-video-guards-")
        self.addCleanup(temporary.cleanup)
        self.stop = threading.Event()
        self.browser = BrowserSession(
            AppSettings(vision=False, ocr=False, video_timeout=20, poll_interval=.05),
            AppPaths.for_root(Path(temporary.name)), lambda _message: None, self.stop,
        )
        self.browser._guard = Mock(side_effect=self.browser._check_stop)
        self.browser._video_visible = Mock(return_value=True)

    def test_callback_failure_preserves_error_restores_cache_and_pauses(self):
        old_questions, old_controls = {"old": question("old")}, {"old": {"A": Mock()}}
        self.browser._questions, self.browser._question_controls = old_questions, old_controls
        root, video = Mock(), Mock()
        root.inner_text.return_value = "判断题：是否正确？\nA 对\nB 错\n提交"

        def extract(*_args, **_kwargs):
            self.browser._questions = {"new": question("new")}
            self.browser._question_controls = {"new": {"A": Mock()}}
            return [question("new")]

        error = RuntimeError("synthetic provider failure")
        callback = Mock(side_effect=error)
        self.browser._extract_questions = Mock(side_effect=extract)
        self.browser._watch_video = Mock(side_effect=lambda cb: self.browser._handle_video_quiz(root, video, cb))
        self.browser._pause_video_on_stop = Mock()
        with self.assertRaises(RuntimeError) as raised:
            self.browser.watch_video(on_quiz=callback)
        self.assertIs(raised.exception, error)
        self.assertIs(self.browser._questions, old_questions)
        self.assertIs(self.browser._question_controls, old_controls)
        self.browser._pause_video_on_stop.assert_called_once()

    def test_stop_after_callback_prevents_answer_validation_and_clicks(self):
        root, video = Mock(), Mock()
        root.inner_text.return_value = "判断题\nA 对\nB 错"
        self.browser._extract_questions = Mock(return_value=[question()])
        self.browser._apply_video_answers = Mock()

        def cancelled(questions, _screenshot):
            self.stop.set()
            return [Answer(questions[0].id, ("A",))]

        with self.assertRaises(StopRequested):
            self.browser._handle_video_quiz(root, video, cancelled)
        self.browser._apply_video_answers.assert_not_called()
        self.assertEqual(self.browser._extract_questions.call_count, 1)

    def test_invalid_later_answer_prevents_all_earlier_clicks(self):
        self.browser._questions = {"first": question("first"), "second": question("second")}
        self.browser._question_controls = {
            identifier: {"A": Mock(), "B": Mock()} for identifier in self.browser._questions
        }
        self.browser._selected, self.browser._click = Mock(), Mock()
        with self.assertRaisesRegex(BrowserError, "无效选项"):
            self.browser._apply_video_answers(
                [Answer("first", ("A",)), Answer("second", ("Z",))], Mock(), Mock(),
            )
        self.browser._selected.assert_not_called()
        self.browser._click.assert_not_called()

    def test_transparent_preloaded_overlay_is_not_extracted(self):
        root = Mock()
        root.is_visible.return_value = True  # Playwright includes opacity:0.
        root.evaluate.return_value = False
        self.browser._video_visible = BrowserSession._video_visible.__get__(self.browser)
        self.assertEqual(self.browser._video_question_blocks(root, Mock()), [])
        root.locator.assert_not_called()

    def test_transparent_embedding_iframe_is_not_extracted(self):
        root, handle, child, parent, iframe = Mock(), Mock(), Mock(), Mock(), Mock()
        root.is_visible.return_value = root.evaluate.return_value = True
        root.element_handle.return_value = handle
        handle.owner_frame.return_value = child
        child.parent_frame, parent.parent_frame = parent, None
        child.frame_element.return_value = iframe
        iframe.is_visible.return_value, iframe.evaluate.return_value = True, False
        self.browser._video_visible = BrowserSession._video_visible.__get__(self.browser)
        self.assertEqual(self.browser._video_question_blocks(root, Mock()), [])
        root.locator.assert_not_called()
        iframe.dispose.assert_called_once()

    def test_reused_dom_switching_question_on_selection_is_never_submitted(self):
        root, video, control_a, control_b = Mock(), Mock(), Mock(), Mock()
        root.inner_text.return_value = "判断题：是否正确？\nA 对\nB 错\n提交"
        original = question()
        changed = Question(original.id, "判断题：另一道题", original.kind, original.options)
        calls = [0]

        def extract(*_args, **_kwargs):
            current = original if calls[0] < 2 else changed
            calls[0] += 1
            self.browser._questions = {current.id: current}
            self.browser._question_controls = {current.id: {"A": control_a, "B": control_b}}
            return [current]

        self.browser._extract_questions = Mock(side_effect=extract)
        self.browser._selected = Mock(return_value=False)
        self.browser._click, self.browser._text_control = Mock(), Mock()
        with self.assertRaisesRegex(BrowserError, "选择答案后视频弹题发生变化"):
            self.browser._handle_video_quiz(root, video, lambda qs, _image: [Answer(qs[0].id, ("A",))])
        self.browser._click.assert_called_once_with(control_a)
        self.browser._text_control.assert_not_called()

    def test_model_processing_time_does_not_exhaust_video_deadline(self):
        clock = [100.0]
        video, prompt = Mock(), Mock()
        video.evaluate.side_effect = [media_state(paused=True), media_state(ended=True)]
        self.browser._video_locators = Mock(return_value=[video])
        self.browser._find_video_quiz = Mock(side_effect=[prompt, None])

        def slow_model(*_args):
            clock[0] += 300.0

        self.browser._handle_video_quiz = Mock(side_effect=slow_model)
        with patch("gaybar_tools.browser.time.monotonic", side_effect=lambda: clock[0]):
            self.browser._watch_video(on_quiz=Mock())
        self.browser._handle_video_quiz.assert_called_once()
        self.assertGreater(clock[0], 100 + self.browser.settings.video_timeout)

    def test_initial_pause_waits_for_late_prompt_before_clicking_play(self):
        clock, started, click_times = [100.0], [False], []
        video, frame, play = Mock(), Mock(), Mock()
        video.evaluate.side_effect = lambda _script: media_state(paused=not started[0], ended=started[0])
        self.browser._video_locators = Mock(return_value=[video])
        self.browser._find_video_quiz = Mock(return_value=None)
        self.browser._video_frame = Mock(return_value=frame)
        frame.locator.return_value.count.return_value = 1
        self.browser._visible = Mock(return_value=True)
        self.browser._first_visible = Mock(return_value=play)
        self.browser._wait = Mock(side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds))

        def click(_target):
            click_times.append(clock[0])
            started[0] = True

        self.browser._click = Mock(side_effect=click)
        with patch("gaybar_tools.browser.time.monotonic", side_effect=lambda: clock[0]):
            self.browser._watch_video()
        self.assertEqual(len(click_times), 1)
        self.assertGreaterEqual(click_times[0] - 100, self.browser.PAUSED_QUIZ_GRACE)
        self.browser._click.assert_called_once_with(play)

    def test_question_at_media_end_still_submits_and_continues(self):
        root, video, submit, continuation = Mock(), Mock(), Mock(), Mock()
        stage = [0]
        self.browser._extract_questions = Mock(return_value=[question()])
        self.browser._apply_video_answers = Mock()
        self.browser._video_question_blocks = Mock(side_effect=lambda *_args: [Mock()] if stage[0] == 0 else [])
        self.browser._visible = Mock(return_value=True)
        self.browser._wait = Mock()
        root.inner_text.side_effect = lambda **_kwargs: "判断题 A 对 B 错" if stage[0] == 0 else "回答正确\n继续学习"
        video.evaluate.return_value = {"paused": True, "ended": True}

        def control(pattern, **_kwargs):
            if stage[0] == 0 and pattern.fullmatch("提交"):
                return submit
            if stage[0] == 1 and pattern.fullmatch("继续学习"):
                return continuation
            return None

        def click(target):
            self.assertIs(target, submit if stage[0] == 0 else continuation)
            stage[0] += 1

        self.browser._text_control = Mock(side_effect=control)
        self.browser._click = Mock(side_effect=click)
        self.browser._handle_video_quiz(root, video, lambda qs, _image: [Answer(qs[0].id, ("A",))])
        self.assertEqual(stage[0], 2)
        self.assertEqual([call.args[0] for call in self.browser._click.call_args_list], [submit, continuation])


if __name__ == "__main__":
    unittest.main()
