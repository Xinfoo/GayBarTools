"""Real-browser readiness regressions using only the bundled local course."""

import importlib.util
import time
import unittest

import test_browser_integration as fixtures

from gaybar_tools.browser import BrowserError


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright 未安装，跳过浏览器集成测试")
class BrowserReadinessIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.BrowserIntegrationTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixtures.BrowserIntegrationTests.tearDownClass.__func__(cls)

    def setUp(self):
        fixtures.BrowserIntegrationTests.setUp(self)

    def test_idle_poll_follows_a_popup_opened_after_the_last_browser_call(self):
        fixtures.BrowserIntegrationTests.launch(self, "home.html")
        home = self.browser.page
        self.assertFalse(self.browser.poll_ready())
        home.evaluate("""() => {
            setTimeout(() => window.open('/chapter.html?chapterId=1&courseId=demo'), 150);
        }""")
        # No Playwright calls between polls: this is the idle desktop worker's
        # behavior while the user opens a course tab in the browser window.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            time.sleep(0.05)
            if self.browser.poll_ready():
                break
        else:
            self.fail("空闲轮询未发现新打开的课程标签")
        self.assertIsNot(self.browser.page, home)
        self.assertEqual(self.browser.chapter_kind(), "video")

    def test_embedded_chapter_without_url_identifiers_can_start(self):
        fixtures.BrowserIntegrationTests.launch(self, "home.html")
        self.browser.page.set_content("""
            <iframe srcdoc='<div class="chapter-content">本地静态章节内容</div>'></iframe>
        """)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.browser.poll_ready():
                break
            time.sleep(0.05)
        else:
            self.fail("未识别不带章节参数的嵌套章节")
        self.assertEqual(self.browser.prepare_start(), "document")

    def test_hidden_and_empty_login_iframes_do_not_block_but_visible_login_does(self):
        fixtures.BrowserIntegrationTests.launch(self, "home.html")
        page = self.browser.page
        page.route("**/login*", lambda route: route.fulfill(
            content_type="text/html", body="" if "empty=1" in route.request.url else "请先登录"))
        page.set_content("""
            <div class="chapter-content">本地静态章节内容</div>
            <iframe id="hidden-login" hidden src="/login"></iframe>
            <iframe src="/login?empty=1"></iframe>
        """)
        page.frame_locator("#hidden-login").locator("body").wait_for(state="attached")
        self.assertEqual(self.browser.prepare_start(), "document")
        page.locator("#hidden-login").evaluate("element => element.hidden = false")
        with self.assertRaisesRegex(BrowserError, "登录"):
            self.browser.prepare_start()


if __name__ == "__main__":
    unittest.main()
