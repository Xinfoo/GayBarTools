"""A recognised player can load its source or metadata only after Play.

The account-free local fixture uses a real nested player and native playback.
The quiz is already submitted; these tests never solve or submit questions.
"""

import importlib.util
import unittest
from unittest.mock import Mock, patch

import test_browser_integration as integration


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright 未安装")
class PreloadNoneVideoIntegrationTests(unittest.TestCase):
    setUpClass = integration.BrowserIntegrationTests.__dict__["setUpClass"]
    tearDownClass = integration.BrowserIntegrationTests.__dict__["tearDownClass"]
    setUp = integration.BrowserIntegrationTests.setUp
    launch = integration.BrowserIntegrationTests.launch

    def metrics(self):
        return self.browser.page.evaluate("JSON.parse(localStorage.getItem('preload-none-video-metrics'))")

    def player(self):
        frames = [frame for frame in self.browser.page.frames if frame.locator("video").count()]
        self.assertEqual(len(frames), 1)
        self.assertIsNotNone(frames[0].parent_frame, "本地用例必须覆盖真实内嵌播放器层级")
        return frames[0].locator("video#video_html5_api.vjs-tech")

    def complete_preload_none_video(self, delay_ms=0, lazy_source=False, mixed_loading_document=False):
        from gaybar_tools.workflow import Workflow

        self.launch(f"preload_none_video.html?chapterId=local-preload&courseId=local&delay={delay_ms}"
                    f"&lazy={int(lazy_source)}&mixed={int(mixed_loading_document)}")
        original_url = self.browser.current_url
        self.assertEqual(self.browser.prepare_start(), "quiz")
        self.assertTrue(self.browser.quiz_completed())
        solver = Mock()
        solver.solve.side_effect = AssertionError("已提交测验后的导航回归不应调用模型或重新作答")
        original_watch = self.browser._watch_video_targets
        entered_watch = []

        def watch_after_navigation(*args, **kwargs):
            video = self.player()
            entered_watch.append(True)
            self.assertEqual(self.browser.current_url, original_url)
            self.assertEqual(video.get_attribute("preload"), "none")
            self.assertEqual(bool(video.get_attribute("src")), not lazy_source)
            self.assertEqual(video.evaluate("video => video.readyState"), 0, "Next必须在加载媒体元数据之前返回")
            self.assertTrue(video.evaluate("video => video.paused"))
            self.assertEqual(self.metrics()["playClicks"], 0, "导航就绪检测不能提前点击播放器")
            self.assertTrue(video.locator("xpath=..").locator(".vjs-big-play-button").is_visible())
            if mixed_loading_document:
                self.assertEqual(video.locator("xpath=..").locator("xpath=..").locator("#reader").inner_text(),
                                 "正在加载...")
            return original_watch(*args, **kwargs)

        with patch.object(self.browser, "_watch_video_targets", side_effect=watch_after_navigation):
            completed = Workflow(self.browser, solver, self.settings, self.stop, self.logs.append).run()
        self.assertEqual(entered_watch, [True])
        solver.solve.assert_not_called()
        self.assertEqual(completed, 2)
        self.assertEqual(self.browser.current_url, f"{self.base_url}/complete.html")
        metrics = self.metrics()
        self.assertEqual(metrics["playClicks"], 1)
        self.assertTrue(metrics["playTrusted"])
        self.assertEqual(metrics["nextTrusted"], [True, True])
        self.assertTrue(metrics["ended"])
        self.assertGreater(metrics["duration"], 2.5)
        self.assertGreaterEqual(metrics["elapsed"] + .05, metrics["duration"])
        self.assertAlmostEqual(metrics["currentTime"], metrics["duration"], places=2)
        self.assertEqual(metrics["seekEvents"], 0)
        self.assertEqual(metrics["playbackRate"], 1)
        self.assertEqual(metrics["sourceAddedOnTrustedPlay"], lazy_source)
        if mixed_loading_document:
            self.assertTrue(metrics["documentLoadedAfterVideoEnded"])
            self.assertTrue(metrics["documentWheelAfterVideoEnded"])
            self.assertGreater(metrics["documentWheels"], 0)
            self.assertTrue(all(metrics["documentWheelTrusted"]))
            self.assertTrue(metrics["documentAtBottom"])
            self.assertGreater(metrics["documentScrollHeight"], metrics["documentClientHeight"] * 2)
            self.assertGreaterEqual(metrics["documentScrollTop"] + metrics["documentClientHeight"],
                                    metrics["documentScrollHeight"] - 3)
        if delay_ms:
            self.assertGreaterEqual(metrics["playStartDelay"], delay_ms / 1000 - .05,
                                    "视频开始必须确实延后，覆盖点击后仍暂停超过0.5秒的状态")

    def test_submitted_quiz_navigates_before_metadata_then_plays_video_naturally(self):
        self.complete_preload_none_video()

    def test_delayed_playback_after_single_trusted_click_waits_and_finishes_naturally(self):
        self.complete_preload_none_video(delay_ms=1200)

    def test_source_added_only_on_trusted_play_click_continues_workflow_naturally(self):
        self.complete_preload_none_video(lazy_source=True)

    def test_loading_document_below_video_does_not_block_playback_before_reading(self):
        with patch.object(self.browser, "NEXT_PAGE_TIMEOUT", 4):
            self.complete_preload_none_video(mixed_loading_document=True)

    def test_source_missing_player_is_clicked_before_reporting_playback_timeout(self):
        from gaybar_tools.browser import BrowserError

        self.launch("preload_none_video.html?chapterId=local-preload&courseId=local&empty=1")
        self.assertEqual(self.browser.prepare_start(), "quiz")
        self.assertTrue(self.browser.quiz_completed())
        self.assertTrue(self.browser.next_chapter())
        video = self.player()
        self.assertFalse(video.get_attribute("src"))
        self.assertEqual(video.evaluate("video => video.readyState"), 0)
        self.assertTrue(self.browser._view_signature("video")[1])
        self.assertEqual(self.metrics()["playClicks"], 0)
        self.settings.video_timeout = 2
        with self.assertRaisesRegex(BrowserError, "视频等待超时"):
            self.browser.watch_video()
        self.assertEqual(self.metrics()["playClicks"], 1)
        self.assertTrue(self.metrics()["playTrusted"])
        self.assertFalse(self.metrics()["ended"])
        self.assertEqual(self.metrics()["nextTrusted"], [True])


if __name__ == "__main__":
    unittest.main()
