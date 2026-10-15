"""Exercise queued cancellation without starting Tk, a browser, or an API."""

import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gaybar_tools.config import AppSettings
from gaybar_tools.paths import AppPaths
from gaybar_tools.worker import Worker


class WorkerCancellationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="gaybar-worker-test-", dir="/tmp")
        self.addCleanup(folder.cleanup)
        self.paths = AppPaths.for_root(Path(folder.name)).prepare()
        self.events = queue.Queue()
        self.worker = Worker(self.paths, self.events)
        self.settings = AppSettings(model="mock-model")

    def finish_after_pending_command(self):
        # A raw queue sentinel leaves shutdown_event unset, so the first queued
        # operation is really dequeued and its cancellation path is exercised.
        self.worker.commands.put(("shutdown", None))
        self.worker.run()

    def test_stop_after_enqueue_before_dequeue_does_not_launch(self):
        self.worker.command("launch", self.settings)
        self.worker.stop()
        with patch("gaybar_tools.browser.BrowserSession") as browser:
            self.finish_after_pending_command()
        browser.assert_not_called()
        self.assertIn(("state", "已停止"), list(self.events.queue))

    def test_stop_before_start_dequeue_does_not_enter_workflow(self):
        self.worker.browser = Mock()
        self.worker.command("start", self.settings)
        self.worker.stop()
        with patch("gaybar_tools.worker.Workflow") as workflow:
            self.finish_after_pending_command()
        workflow.assert_not_called()
        self.worker.browser.close.assert_called_once()

    def test_stop_before_test_dequeue_does_not_send_request(self):
        self.worker.command("test", self.settings)
        self.worker.stop()
        with patch("gaybar_tools.llm.QuizSolver") as solver:
            self.finish_after_pending_command()
        solver.assert_not_called()

    def test_shutdown_before_dequeue_discards_pending_launch(self):
        self.worker.browser = Mock()
        self.worker.command("launch", self.settings)
        self.worker.command("shutdown")
        with patch("gaybar_tools.browser.BrowserSession") as browser, patch("gaybar_tools.worker.Workflow") as workflow:
            self.worker.run()
        browser.assert_not_called()
        workflow.assert_not_called()
        self.worker.browser.close.assert_called_once()
        self.assertTrue(self.worker.stop_event.is_set())

    def test_new_commands_are_ignored_after_shutdown(self):
        self.worker.command("shutdown")
        queued = self.worker.commands.qsize()
        self.worker.command("launch", self.settings)
        self.worker.command("start", self.settings)
        self.assertEqual(self.worker.commands.qsize(), queued)
        self.assertTrue(self.worker.stop_event.is_set())

    def test_explicit_new_command_can_resume_after_previous_stop(self):
        self.worker.browser = Mock()
        self.worker.stop()
        self.worker.command("start", self.settings)
        self.assertFalse(self.worker.stop_event.is_set())
        with patch("gaybar_tools.worker.Workflow") as workflow:
            self.finish_after_pending_command()
        workflow.return_value.run.assert_called_once()

    def test_gui_demo_uses_separate_profile_and_shared_browser_store(self):
        self.worker.demo_url = "http://127.0.0.1:1234/chapter.html"
        self.worker.command("launch", self.settings)
        with patch("gaybar_tools.browser.BrowserSession") as browser:
            self.finish_after_pending_command()
        demo_paths = browser.call_args.args[1]
        self.assertNotEqual(demo_paths.profile, self.paths.profile)
        self.assertTrue(demo_paths.profile.is_relative_to(self.paths.runtime))
        self.assertEqual(demo_paths.browsers, self.paths.browsers)
        browser.return_value.launch.assert_called_once_with(self.worker.demo_url)


if __name__ == "__main__":
    unittest.main()
