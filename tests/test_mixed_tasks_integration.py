"""Native wheel/media ordering for every task mounted on one course page."""

import importlib.util
import unittest
from unittest.mock import Mock, patch

import test_browser_integration as integration


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright 未安装")
class MixedTasksIntegrationTests(unittest.TestCase):
    setUpClass = integration.BrowserIntegrationTests.__dict__["setUpClass"]
    tearDownClass = integration.BrowserIntegrationTests.__dict__["tearDownClass"]
    setUp = integration.BrowserIntegrationTests.setUp
    launch = integration.BrowserIntegrationTests.launch

    def metrics(self):
        return self.browser.page.evaluate("JSON.parse(localStorage.getItem('mixed-tasks-metrics'))")

    def launch_tasks(self, layout="doc-video-doc-video", extra="", *, wait_ready=True):
        self.launch(f"mixed_tasks.html?layout={layout}&{extra}")
        ready = "window.mixedFixtureReady && window.mixedFixtureReady()" if wait_ready else "window.mixedFixtureMounted"
        self.browser.page.wait_for_function(ready)

    def run_tasks(self, layout, expected, *, already_launched=False):
        from gaybar_tools.workflow import Workflow

        if not already_launched:
            self.launch_tasks(layout, wait_ready=layout != "loading-doc-video")
        initial = self.browser.content_tasks()
        initial_kinds = [kind for kind in expected if layout not in {"dynamic", "loading-doc-video"} or kind == "video"]
        self.assertEqual([task.kind for task in initial], initial_kinds, "隐藏残留、目录和播放器壳不能成为内容任务")
        self.assertEqual(len({task.identity for task in initial}), len(initial))
        solver = Mock()
        solver.solve.side_effect = AssertionError("混排内容回归不包含答题或模型请求")
        callbacks, returns = [], []
        original = self.browser.process_content

        def observe(*, on_quiz=None, on_task=None):
            initial_key = self.browser.chapter_key()
            def task_started(kind, ordinal, total_known):
                callbacks.append((kind, ordinal, total_known))
                if on_task:
                    on_task(kind, ordinal, total_known)
            if layout == "loading-doc-video":
                original_unresolved = self.browser._unresolved_content
                unresolved_before_waits = 0

                def observe_unresolved(tasks, *, before=None):
                    nonlocal unresolved_before_waits
                    unresolved = original_unresolved(tasks, before=before)
                    if before is not None and unresolved:
                        unresolved_before_waits += 1
                    return unresolved

                with patch.object(self.browser, "_unresolved_content", side_effect=observe_unresolved):
                    # Preparation/key scans must not consume the delayed
                    # reader's loading interval before processing starts.
                    self.browser.page.evaluate("() => { window.startMixedDeferredReaders(); }")
                    count = original(on_quiz=on_quiz, on_task=task_started)
                self.assertGreaterEqual(unresolved_before_waits, 1,
                                        "必须真实进入等待上方附件的分支，不能只碰巧在处理前加载完")
            else:
                count = original(on_quiz=on_quiz, on_task=task_started)
            if layout not in {"dynamic", "loading-doc-video"}:
                self.assertEqual(self.browser.chapter_key(), initial_key,
                                 "完成滚动、自然播放及隐藏播放按钮不能改变静态页面的任务身份")
            returns.append(count)
            return count

        with patch.object(self.browser, "process_content", side_effect=observe):
            completed = Workflow(self.browser, solver, self.settings, self.stop, self.logs.append).run()
        solver.solve.assert_not_called()
        self.assertEqual(completed, 1, "一个页面上的多个内容任务完成后仅计一个学习页面")
        self.assertEqual(returns, [len(expected)])
        self.assertEqual([item[0] for item in callbacks], expected)
        self.assertEqual([item[1] for item in callbacks], list(range(1, len(expected)+1)))
        self.assertTrue(all(total >= ordinal for _kind, ordinal, total in callbacks))
        self.assertEqual(self.browser.current_url, f"{self.base_url}/complete.html")
        metrics = self.metrics()
        self.assertEqual(metrics["violations"], [], "当前任务的真实交互必须发生在前序任务完成之后")
        self.assertEqual(metrics["completed"], metrics["expected"])
        self.assertEqual(metrics["nextClicks"], 1)
        self.assertEqual(metrics["nextTrusted"], [True])
        self.assertEqual(metrics["events"][-1], {"id":"next", "kind":"navigation", "event":"click"})
        self.assertEqual(metrics["directoryWheels"], 0)
        for task_id in metrics["expected"]:
            task = metrics["tasks"][task_id]
            if task["kind"] == "document":
                self.assertGreater(task["wheels"], 0)
                self.assertTrue(task["wheelTrusted"])
                self.assertTrue(task["bottom"])
                self.assertGreater(task["scrolls"], 0)
            else:
                self.assertEqual(task["playClicks"], 1)
                self.assertTrue(task["ended"])
                self.assertEqual(task["seekEvents"], 0)
                self.assertEqual(task["playbackRate"], 1)
                self.assertAlmostEqual(task["currentTime"], task["duration"], places=2)
                self.assertGreaterEqual(task["elapsed"]+.05, task["duration"])
        self.assertEqual(metrics["tasks"]["old-hidden-reader"]["wheels"], 0)
        self.assertEqual(metrics["tasks"]["old-hidden-video"]["playClicks"], 0)
        self.assertTrue(all(event["trusted"] for event in metrics["events"] if event["event"] == "play-click"))
        return metrics

    def test_document_video_document_video_follow_top_to_bottom_before_one_next(self):
        self.run_tasks("doc-video-doc-video", ["document", "video", "document", "video"])

    def test_same_url_identical_readers_at_different_frame_depths_are_all_processed(self):
        self.launch_tasks("documents")
        frames = [frame for frame in self.browser.page.frames if frame.locator("#same-reader").count()
                  and self.browser._frame_visible(frame)]
        self.assertEqual(len(frames), 3)
        self.assertEqual(len({frame.url for frame in frames}), 1)
        self.assertEqual(len({frame.locator("#same-reader").inner_html() for frame in frames}), 1)
        self.assertIs(frames[0].parent_frame, self.browser.page.main_frame)
        self.assertIsNot(frames[1].parent_frame, self.browser.page.main_frame)
        # Reading DOM/URL above does not scroll or play anything; keep that
        # same mount so all three readers' native events remain observable.
        self.run_tasks("documents", ["document", "document", "document"], already_launched=True)

    def test_video_before_document_keeps_page_order(self):
        self.run_tasks("video-doc", ["video", "document"])

    def test_document_appended_after_video_is_processed_before_next(self):
        metrics = self.run_tasks("dynamic", ["video", "document"])
        self.assertTrue(any(event["event"] == "appended" for event in metrics["events"]))

    def test_loading_document_above_video_finishes_before_video_starts(self):
        self.run_tasks("loading-doc-video", ["document", "video"])

    def test_unknown_plugin_appearing_during_final_wait_prevents_next(self):
        from gaybar_tools.browser import BrowserError
        from gaybar_tools.workflow import Workflow

        self.launch_tasks("tail-plugin")
        original = self.browser._unresolved_content
        quiet_tail_checks = 0
        solver = Mock()

        def observe_unresolved(tasks, *, before=None):
            nonlocal quiet_tail_checks
            unresolved = original(tasks, before=before)
            if before is None and not unresolved and self.metrics()["completed"] == ["video-1"]:
                quiet_tail_checks += 1
                if quiet_tail_checks == 2:
                    # Place a real unknown attachment precisely in the final
                    # wait window, without guessing media-poll timer phases or
                    # replacing any wheel/playback/completion implementation.
                    self.browser.page.evaluate("() => { setTimeout(() => window.appendMixedPlugin(), 0); }")
            return unresolved

        with patch.object(self.browser, "NEXT_PAGE_TIMEOUT", 1), patch.object(
                self.browser, "_unresolved_content", side_effect=observe_unresolved), patch.object(
                self.browser, "next_chapter", wraps=self.browser.next_chapter) as next_page:
            with self.assertRaisesRegex(BrowserError, "学习附件.*未能读取"):
                Workflow(self.browser, solver, self.settings, self.stop, self.logs.append).run()
        next_page.assert_not_called()
        solver.solve.assert_not_called()
        self.assertEqual(quiet_tail_checks, 2)
        metrics = self.metrics()
        self.assertTrue(metrics["pluginPresent"])
        self.assertTrue(self.browser.page.locator("#late-unsupported-plugin").is_visible())
        self.assertEqual(metrics["completed"], ["video-1"])
        self.assertEqual(metrics["tasks"]["video-1"]["playClicks"], 1)
        self.assertTrue(metrics["tasks"]["video-1"]["ended"])
        self.assertEqual(metrics["tasks"]["video-1"]["seekEvents"], 0)
        self.assertEqual(metrics["tasks"]["video-1"]["playbackRate"], 1)
        self.assertEqual(metrics["nextClicks"], 0)

    def test_failed_first_video_does_not_read_later_document_or_click_next(self):
        from gaybar_tools.browser import BrowserError
        from gaybar_tools.workflow import Workflow

        self.launch_tasks("video-doc", "fail=video-1")
        solver = Mock()
        with self.assertRaises(BrowserError):
            Workflow(self.browser, solver, self.settings, self.stop, self.logs.append).run()
        solver.solve.assert_not_called()
        metrics = self.metrics()
        self.assertEqual(metrics["nextClicks"], 0)
        self.assertEqual(metrics["completed"], [])
        self.assertEqual(metrics["tasks"]["document-2"]["wheels"], 0)

    def test_stop_during_first_video_does_not_start_later_document_or_next(self):
        from gaybar_tools.browser import StopRequested
        from gaybar_tools.workflow import Workflow, WorkflowStopped

        self.launch_tasks("video-doc")
        self.browser.page.expose_function("requestMixedStop", self.stop.set)
        self.browser.page.evaluate("() => { window.onMixedPlay = () => window.requestMixedStop(); }")
        solver = Mock()
        with self.assertRaises((StopRequested, WorkflowStopped)):
            Workflow(self.browser, solver, self.settings, self.stop, self.logs.append).run()
        solver.solve.assert_not_called()
        metrics = self.metrics()
        self.assertTrue(any(event["event"] == "play" for event in metrics["events"]))
        self.assertEqual(metrics["nextClicks"], 0)
        self.assertEqual(metrics["completed"], [])
        self.assertEqual(metrics["tasks"]["document-2"]["wheels"], 0)
        self.assertTrue(self.browser.page.locator("video#video-1").evaluate("video => video.paused"))

    def test_directory_poster_shell_and_hidden_residuals_are_not_tasks(self):
        self.launch_tasks("none")
        self.assertEqual(self.browser.content_tasks(), [])
        self.assertEqual(self.metrics()["nextClicks"], 0)


if __name__ == "__main__":
    unittest.main()
