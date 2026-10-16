"""Native navigation through the observed unfinished-task HTML warning."""

import importlib.util
import unittest
from unittest.mock import patch

import test_browser_integration as integration


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright 未安装")
class NextWarningIntegrationTests(unittest.TestCase):
    setUpClass = integration.BrowserIntegrationTests.__dict__["setUpClass"]
    tearDownClass = integration.BrowserIntegrationTests.__dict__["tearDownClass"]
    setUp = integration.BrowserIntegrationTests.setUp
    launch = integration.BrowserIntegrationTests.launch

    def metrics(self):
        return self.browser.page.evaluate("JSON.parse(localStorage.getItem('next-warning-metrics'))")

    def launch_warning(self, query="", *, expected_kind="document"):
        self.launch(f"next_warning.html?chapterId=local-warning&courseId=local&{query}")
        self.browser.page.wait_for_function("window.nextWarningFixtureReady && window.nextWarningFixtureReady()")
        if expected_kind is not None:
            self.assertEqual(self.browser.prepare_start(), expected_kind)

    def assert_untouched_other_controls(self, metrics):
        self.assertEqual(metrics["studyClicks"], 0)
        self.assertEqual(metrics["closeClicks"], 0)
        self.assertEqual(metrics["hiddenClicks"], 0, "隐藏的旧jobFinishTip模板不能接受操作")

    def assert_successful_warning_navigation(self, query="", *, preopened=False):
        self.launch_warning(query)
        original_url, original_key = self.browser.current_url, self.browser.chapter_key()
        with patch.object(self.browser, "NEXT_PAGE_TIMEOUT", 5):
            self.assertTrue(self.browser.next_chapter())
        self.assertEqual(self.browser.current_url, original_url)
        self.assertNotEqual(self.browser.chapter_key(), original_key)
        self.assertEqual(self.browser.chapter_kind(), "document")
        self.assertTrue(self.browser._view_signature("document")[1])
        self.assertEqual(self.browser.page.locator("#activity-copy h2").inner_text(), "已经进入下一学习内容")
        metrics = self.metrics()
        self.assertTrue(metrics["contentReady"], "必须等待新内容就绪，关闭提示本身不是跳转成功")
        self.assertEqual(metrics["shown"], 1)
        self.assertEqual(metrics["footerClicks"], 0 if preopened else 1)
        self.assertEqual(metrics["footerTrusted"], [] if preopened else [True])
        self.assertEqual(metrics["modalNextClicks"], 1)
        self.assertEqual(metrics["modalNextTrusted"], [True])
        self.assert_untouched_other_controls(metrics)

    def test_delayed_popdiv_warning_confirms_once_then_waits_for_same_url_content(self):
        self.assert_successful_warning_navigation("delay=350")

    def test_delayed_popdiv_warning_inside_iframe_confirms_and_loads_content(self):
        self.assert_successful_warning_navigation("frame=1&delay=350")

    def test_unknown_fixed_container_uses_message_and_local_controls(self):
        self.assert_successful_warning_navigation("unknown=1")

    def test_preopened_warning_confirms_without_clicking_covered_footer(self):
        self.assert_successful_warning_navigation("preopen=1", preopened=True)

    def test_sticky_warning_is_not_confirmed_repeatedly_or_treated_as_navigation(self):
        from gaybar_tools.browser import BrowserError

        self.launch_warning("mode=sticky")
        original_key = self.browser.chapter_key()
        with patch.object(self.browser, "NEXT_PAGE_TIMEOUT", 2):
            with self.assertRaises(BrowserError):
                self.browser.next_chapter()
        metrics = self.metrics()
        self.assertEqual(metrics["footerClicks"], 1)
        self.assertEqual(metrics["modalNextClicks"], 1)
        self.assertFalse(metrics["contentReady"])
        self.assertEqual(self.browser.chapter_key(), original_key)
        self.assert_untouched_other_controls(metrics)

    def test_hiding_warning_without_new_content_does_not_complete_navigation(self):
        from gaybar_tools.browser import BrowserError

        self.launch_warning("mode=hide-only")
        original_key = self.browser.chapter_key()
        with patch.object(self.browser, "NEXT_PAGE_TIMEOUT", 2):
            with self.assertRaises(BrowserError):
                self.browser.next_chapter()
        metrics = self.metrics()
        self.assertEqual(metrics["modalNextClicks"], 1)
        self.assertFalse(metrics["contentReady"])
        self.assertEqual(self.browser.chapter_key(), original_key)
        self.assertTrue(self.browser.page.locator(".warning-layer").is_hidden())
        self.assert_untouched_other_controls(metrics)

    def test_preopened_warning_hiding_over_same_video_is_not_a_new_page(self):
        from gaybar_tools.browser import BrowserError

        self.launch_warning("preopen=1&mode=hide-only&video=1", expected_kind=None)
        video = self.browser.page.locator("#underlying-video")
        initial_source = video.get_attribute("src")
        original_video = video.element_handle()
        self.addCleanup(original_video.dispose)
        with patch.object(self.browser, "NEXT_PAGE_TIMEOUT", 2):
            with self.assertRaises(BrowserError):
                self.browser.next_chapter()
        self.assertTrue(original_video.evaluate("video => video === document.querySelector('#underlying-video')"))
        self.assertEqual(video.get_attribute("src"), initial_source)
        self.assertEqual(video.evaluate("video => video.readyState"), 0)
        self.assertEqual(self.browser.chapter_kind(), "video")
        self.assertTrue(self.browser.page.locator(".warning-layer").is_hidden())
        metrics = self.metrics()
        self.assertEqual(metrics["footerClicks"], 0)
        self.assertEqual(metrics["modalNextClicks"], 1)
        self.assertEqual(metrics["playClicks"], 0)
        self.assertFalse(metrics["contentReady"])
        self.assert_untouched_other_controls(metrics)

    def assert_body_reader_warning_does_not_navigate(self, *, unknown=False):
        from gaybar_tools.browser import BrowserError

        self.launch_warning(f"preopen=1&mode=hide-only&body_reader=1&unknown={int(unknown)}")
        tasks = self.browser.content_tasks()
        self.assertEqual([task.kind for task in tasks], ["document"])
        self.assertEqual(tasks[0].target.evaluate("node => node.tagName"), "BODY", "弹窗必须位于唯一BODY文档target内部")
        original_url, original_key = self.browser.current_url, self.browser.chapter_key()
        original_frame = self.browser.page.main_frame
        original_content = self.browser.page.locator("#activity-copy").inner_html()
        with patch.object(self.browser, "NEXT_PAGE_TIMEOUT", 2):
            with self.assertRaises(BrowserError):
                self.browser.next_chapter()
        self.assertEqual(self.browser.current_url, original_url)
        self.assertIs(self.browser.page.main_frame, original_frame)
        self.assertEqual(self.browser.page.locator("#activity-copy").inner_html(), original_content)
        self.assertEqual(self.browser.chapter_key(), original_key, "提示正文与按钮不属于文档语义，隐藏提示不能更换pagekey")
        self.assertTrue(self.browser.page.locator(".warning-layer").is_hidden())
        metrics = self.metrics()
        self.assertEqual(metrics["footerClicks"], 0)
        self.assertEqual(metrics["modalNextClicks"], 1)
        self.assertFalse(metrics["contentReady"])
        self.assert_untouched_other_controls(metrics)

    def test_preopened_popdiv_inside_body_reader_hiding_is_not_new_content(self):
        self.assert_body_reader_warning_does_not_navigate()

    def test_preopened_unknown_fixed_warning_inside_body_reader_hiding_is_not_new_content(self):
        self.assert_body_reader_warning_does_not_navigate(unknown=True)

    def test_stop_when_warning_appears_does_not_click_any_modal_control(self):
        from gaybar_tools.browser import StopRequested

        self.launch_warning("delay=200")
        self.browser.page.expose_function("requestWarningStop", self.stop.set)
        self.browser.page.evaluate("() => { window.onNextWarningShown = () => window.requestWarningStop(); }")
        with self.assertRaises(StopRequested):
            self.browser.next_chapter()
        metrics = self.metrics()
        self.assertEqual(metrics["shown"], 1)
        self.assertEqual(metrics["footerClicks"], 1)
        self.assertEqual(metrics["modalNextClicks"], 0)
        self.assertFalse(metrics["contentReady"])
        self.assert_untouched_other_controls(metrics)

    def test_unrelated_dialog_and_hidden_old_warning_are_not_accepted(self):
        from gaybar_tools.browser import BrowserError

        self.launch_warning("unrelated=1")
        with patch.object(self.browser, "NEXT_PAGE_TIMEOUT", 2):
            with self.assertRaises(BrowserError):
                self.browser.next_chapter()
        metrics = self.metrics()
        self.assertEqual(metrics["shown"], 1)
        self.assertEqual(metrics["footerClicks"], 1)
        self.assertEqual(metrics["modalNextClicks"], 0)
        self.assertFalse(metrics["contentReady"])
        self.assert_untouched_other_controls(metrics)

    def test_disabled_modal_next_is_not_clicked_or_reported_as_course_end(self):
        from gaybar_tools.browser import BrowserError

        self.launch_warning("modal_disabled=1")
        with patch.object(self.browser, "NEXT_PAGE_TIMEOUT", 2):
            with self.assertRaises(BrowserError):
                self.browser.next_chapter()
        metrics = self.metrics()
        self.assertEqual(metrics["footerClicks"], 1)
        self.assertEqual(metrics["modalNextClicks"], 0)
        self.assertFalse(metrics["contentReady"])
        self.assert_untouched_other_controls(metrics)

    def test_disabled_footer_next_does_not_open_or_confirm_warning(self):
        self.launch_warning("footer_disabled=1")
        self.assertFalse(self.browser.next_chapter())
        metrics = self.metrics()
        self.assertEqual(metrics["footerClicks"], 0)
        self.assertEqual(metrics["shown"], 0)
        self.assertEqual(metrics["modalNextClicks"], 0)
        self.assert_untouched_other_controls(metrics)


if __name__ == "__main__":
    unittest.main()
