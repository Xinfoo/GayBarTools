import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from gaybar_tools.config import AppSettings
from gaybar_tools.models import Answer, Option, Question
from gaybar_tools.paths import AppPaths
from gaybar_tools.worker import DemoSolver
from gaybar_tools.workflow import Workflow, WorkflowError, WorkflowStopped


class SettingsTests(unittest.TestCase):
    def test_key_not_saved_unless_explicitly_enabled(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            settings = AppSettings(api_key="test-secret")
            settings.save(path)
            self.assertNotIn("test-secret", path.read_text())
            settings.remember_key = True
            settings.save(path)
            self.assertIn("test-secret", path.read_text())
            settings.remember_key = False
            settings.save(path)
            self.assertNotIn("test-secret", path.read_text())

    def test_default_request_timeout_is_ten_times_the_legacy_value(self):
        self.assertEqual(AppSettings().request_timeout, 450.0)
        AppSettings(request_timeout=450.0).validate()

    def test_bad_settings_fail_before_browser_launch(self):
        for values in ({"base_url": 3}, {"request_timeout": float("nan")},
                       {"headless": "false"},
                       {"max_chapters": 2.5}, {"base_url": "https://u:p@example.test/v1"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                AppSettings(**values).validate()

    def test_legacy_confidence_setting_is_ignored_and_removed_on_save(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(json.dumps({"model": "existing-model", "confidence_threshold": 1.1}))
            settings = AppSettings.load(path)
            self.assertEqual(settings.model, "existing-model")
            self.assertFalse(hasattr(settings, "confidence_threshold"))
            settings.save(path)
            self.assertNotIn("confidence_threshold", json.loads(path.read_text()))

    def test_retired_recognition_settings_are_ignored_and_removed_on_save(self):
        retired = {"vision": False, "ocr": True, "ocr_languages": "chi_sim+eng"}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(json.dumps({"model": "image-model", **retired}))
            settings = AppSettings.load(path)
            self.assertEqual(settings.model, "image-model")
            settings.save(path)
            saved = json.loads(path.read_text())
            for name in retired:
                self.assertFalse(hasattr(settings, name))
                self.assertNotIn(name, saved)

    def test_paths_and_child_cache_stay_under_project(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = AppPaths.for_root(Path(folder)).prepare()
            environment = paths.environment()
            for key in ("TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
                        "XDG_DATA_HOME", "XDG_STATE_HOME", "PLAYWRIGHT_BROWSERS_PATH"):
                self.assertTrue(Path(environment[key]).is_relative_to(paths.root), key)

    def test_runtime_symlink_cannot_redirect_application_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "project"
            outside = Path(folder) / "outside"
            root.mkdir()
            outside.mkdir()
            try:
                (root / ".runtime").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("当前系统不支持创建测试软链接")
            with self.assertRaises(ValueError):
                AppPaths.for_root(root).prepare()
            self.assertEqual(list(outside.iterdir()), [])


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        self.browser = Mock()
        self.browser.poll_ready.return_value = True
        self.browser.chapter_key.return_value = "chapter-1"
        self.browser.prepare_start.return_value = "quiz"
        self.browser.chapter_kind.return_value = "video"
        self.browser.open_quiz.return_value = True
        self.browser.quiz_completed.return_value = False
        self.browser.get_questions.return_value = [Question("q", "题目", "single", (Option("A", "甲"), Option("B", "乙")))]
        self.browser.submit_quiz.return_value = True
        self.browser.next_chapter.return_value = False
        self.solver = Mock()
        self.solver.solve.return_value = [Answer("q", ("A",))]
        self.settings = AppSettings(model="test")

    def workflow(self):
        return Workflow(self.browser, self.solver, self.settings, self.stop, lambda value: None)

    def test_no_navigation_when_submission_is_uncertain(self):
        self.browser.submit_quiz.return_value = False
        with self.assertRaises(WorkflowError):
            self.workflow().run()
        self.browser.next_chapter.assert_not_called()

    def test_solver_failure_does_not_click_or_submit(self):
        self.solver.solve.side_effect = ValueError("模型请求失败")
        with self.assertRaises(ValueError):
            self.workflow().run()
        self.browser.apply_answers.assert_not_called()
        self.browser.submit_quiz.assert_not_called()

    def test_chapter_quiz_always_supplies_its_screenshot_to_the_model(self):
        screenshot = b"\x89PNG\r\n\x1a\nchapter-question-image"
        self.browser.quiz_screenshot.return_value = screenshot
        self.workflow()._solve_chapter_quiz()
        self.browser.quiz_screenshot.assert_called_once_with()
        self.solver.solve.assert_called_once_with(self.browser.get_questions.return_value, screenshot)

    def test_stop_during_model_request_prevents_clicks(self):
        def solve(*args):
            self.stop.set()
            return [Answer("q", ("A",))]
        self.solver.solve.side_effect = solve
        with self.assertRaises(WorkflowStopped):
            self.workflow().run()
        self.browser.apply_answers.assert_not_called()

    def test_unknown_type_is_never_advanced(self):
        self.browser.prepare_start.return_value = "unknown"
        with self.assertRaises(WorkflowError):
            self.workflow().run()
        self.browser.open_quiz.assert_not_called()

    def test_same_chapter_loop_is_detected(self):
        self.browser.prepare_start.return_value = "video"
        self.browser.next_chapter.return_value = True
        with self.assertRaises(WorkflowError):
            self.workflow().run()
        self.browser.process_content.assert_called_once()

    def test_consecutive_content_pages_do_not_require_a_quiz(self):
        self.browser.chapter_key.side_effect = ["chapter-1", "chapter-2"]
        self.browser.prepare_start.return_value = "video"
        self.browser.chapter_kind.return_value = "document"
        self.browser.next_chapter.side_effect = [True, False]
        self.assertEqual(self.workflow().run(), 2)
        self.assertEqual(self.browser.process_content.call_count, 2)
        self.browser.watch_video.assert_not_called()
        self.browser.read_document.assert_not_called()
        self.browser.open_quiz.assert_not_called()
        self.browser.quiz_completed.assert_not_called()
        self.browser.get_questions.assert_not_called()
        self.browser.submit_quiz.assert_not_called()
        self.solver.solve.assert_not_called()

    def test_explicit_start_uses_content_even_without_background_readiness(self):
        self.browser.prepare_start.return_value = "video"
        self.browser.poll_ready.return_value = False
        self.assertEqual(self.workflow().run(), 1)
        self.browser.prepare_start.assert_called_once()
        self.browser.process_content.assert_called_once()
        self.browser.poll_ready.assert_not_called()

    def test_video_watcher_receives_the_shared_model_callback(self):
        self.browser.prepare_start.return_value = "video"
        self.workflow().run()
        callback = self.browser.process_content.call_args.kwargs["on_quiz"]
        questions = self.browser.get_questions.return_value
        screenshot = b"synthetic-video-quiz-image"
        self.solver.solve.reset_mock()
        answers = callback(questions, screenshot)
        self.solver.solve.assert_called_once_with(questions, screenshot)
        self.assertIs(answers, self.solver.solve.return_value)

    def test_content_then_optional_quiz_are_separate_learning_pages(self):
        self.browser.prepare_start.return_value = "video"
        self.browser.chapter_kind.side_effect = ["quiz", "document", "quiz"]
        self.browser.chapter_key.side_effect = ["chapter1-video", "chapter1-quiz", "chapter2-document", "chapter2-quiz"]
        self.browser.next_chapter.side_effect = [True, True, True, False]
        self.assertEqual(self.workflow().run(), 4)
        self.assertEqual(self.browser.process_content.call_count, 2)
        self.assertEqual(self.solver.solve.call_count, 2)
        self.assertEqual(self.browser.submit_quiz.call_count, 2)
        self.assertEqual(self.browser.next_chapter.call_count, 4)
        self.browser.open_quiz.assert_not_called()

    def test_learning_page_limit_bounds_processing(self):
        self.settings.max_chapters = 2
        self.browser.prepare_start.return_value = "video"
        self.browser.chapter_kind.return_value = "quiz"
        self.browser.chapter_key.side_effect = ["chapter1-video", "chapter1-quiz"]
        self.browser.next_chapter.return_value = True
        self.assertEqual(self.workflow().run(), 2)
        self.browser.process_content.assert_called_once()
        self.browser.submit_quiz.assert_called_once()
        self.browser.chapter_kind.assert_called_once()

    def test_unknown_page_after_next_is_not_assumed_to_be_a_quiz(self):
        self.browser.prepare_start.return_value = "video"
        self.browser.chapter_kind.return_value = "unknown"
        self.browser.next_chapter.return_value = True
        with self.assertRaises(WorkflowError):
            self.workflow().run()
        self.browser.next_chapter.assert_called_once()
        self.browser.get_questions.assert_not_called()
        self.solver.solve.assert_not_called()

    def test_content_task_failure_never_advances_to_the_next_page(self):
        self.browser.prepare_start.return_value = "video"
        self.browser.process_content.side_effect = RuntimeError("第二个任务点处理失败")
        with self.assertRaisesRegex(RuntimeError, "第二个任务点"):
            self.workflow().run()
        self.browser.next_chapter.assert_not_called()
        self.solver.solve.assert_not_called()

    def test_stop_after_a_content_task_does_not_advance(self):
        self.browser.prepare_start.return_value = "document"
        self.browser.process_content.side_effect = lambda **_kwargs: self.stop.set()
        with self.assertRaises(WorkflowStopped):
            self.workflow().run()
        self.browser.next_chapter.assert_not_called()

    def test_stop_during_video_question_request_prevents_returning_answers(self):
        def solve(*_args):
            self.stop.set()
            return [Answer("q", ("A",))]
        self.solver.solve.side_effect = solve
        with self.assertRaises(WorkflowStopped):
            self.workflow()._solve_video_quiz(self.browser.get_questions.return_value, None)

    def test_stopped_video_question_does_not_request_model(self):
        self.stop.set()
        with self.assertRaises(WorkflowStopped):
            self.workflow()._solve_video_quiz(self.browser.get_questions.return_value, None)
        self.solver.solve.assert_not_called()

    def test_demo_answers_rejected_on_remote_origin(self):
        self.browser.current_url = "https://mooc1.chaoxing.com/mycourse/studentstudy?chapterId=1"
        solver = DemoSolver(self.browser, "http://127.0.0.1:1234/chapter.html")
        with self.assertRaises(ValueError):
            solver.solve(self.browser.get_questions())

    def test_resume_submitted_quiz_does_not_request_or_submit_again(self):
        self.browser.prepare_start.return_value = "quiz"
        self.browser.quiz_completed.return_value = True
        self.assertEqual(self.workflow().run(), 1)
        self.solver.solve.assert_not_called()
        self.browser.apply_answers.assert_not_called()
        self.browser.submit_quiz.assert_not_called()
        self.browser.next_chapter.assert_called_once()


if __name__ == "__main__":
    unittest.main()
