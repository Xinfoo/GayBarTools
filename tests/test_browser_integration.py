"""Exercise the actual browser against the bundled, account-free course.

Run with ``python -m unittest discover -s tests -p test_browser_integration.py -v``. Only the
browser executable and Playwright are optional; page interactions are real.
"""

import functools
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from gaybar_tools.config import AppSettings
from gaybar_tools.models import Answer
from gaybar_tools.paths import AppPaths


PROJECT = Path(__file__).resolve().parents[1]
DEMO = PROJECT / "gaybar_tools" / "demo"


def browser_executable():
    configured = os.environ.get("GAYBARTOOLS_BROWSER_PATH", "")
    if configured and Path(configured).is_file():
        return configured
    for name in ("chromium", "chromium-browser", "google-chrome", "msedge", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    for name in ("chrome", "chrome.exe", "headless_shell", "headless_shell.exe"):
        found = next((PROJECT / ".runtime" / "browsers").rglob(name), None)
        if found and found.is_file():
            return str(found)
    return ""


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright 未安装，跳过浏览器集成测试")
class BrowserIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gaybar_tools.browser import BrowserSession, StopRequested

        cls.BrowserSession, cls.StopRequested = BrowserSession, StopRequested
        cls.executable = browser_executable()
        if not cls.executable:
            raise unittest.SkipTest("未找到浏览器；运行项目的浏览器安装命令或设置 GAYBARTOOLS_BROWSER_PATH")
        handler = functools.partial(QuietHandler, directory=str(DEMO))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=3)

    def setUp(self):
        temporary_root = PROJECT / ".runtime" / "tmp" if os.name == "nt" else Path("/tmp")
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="gaybar-browser-test-", dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.paths = AppPaths.for_root(Path(self.temporary.name)).prepare()
        self.settings = AppSettings(
            browser_path=self.executable, headless=True, vision=False, ocr=False,
            action_delay_min=0, action_delay_max=0, poll_interval=0.05,
            video_timeout=20, max_chapters=5,
        )
        self.stop = threading.Event()
        self.logs = []
        self.browser = self.BrowserSession(self.settings, self.paths, self.logs.append, self.stop)
        self.addCleanup(self.browser.close)

    def launch(self, path="chapter.html?chapterId=1&courseId=demo"):
        self.browser.launch(f"{self.base_url}/{path}", headless=True)

    def wait_ready(self):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.browser.poll_ready():
                return
            self.browser.page.wait_for_timeout(50)
        self.fail("本地演示章节未就绪")

    def metrics(self):
        return self.browser.page.evaluate("JSON.parse(localStorage.getItem('gaybar-demo-metrics') || '{}')")

    def answer_and_submit(self):
        self.assertTrue(self.browser.open_quiz())
        questions = self.browser.get_questions()
        self.assertEqual(len(questions), 3)
        self.assertEqual({question.kind for question in questions}, {"single", "multiple", "boolean"})
        choices = {"single": ("B",), "multiple": ("A", "B"), "boolean": ("A",)}
        answers = [Answer(question.id, choices[question.kind], 1.0, "本地集成测试") for question in questions]
        self.browser.apply_answers(answers)
        self.assertTrue(self.browser.submit_quiz(), "需要看到明确的成功反馈后才能进入下一节")

    def assert_quiz_events(self, metrics):
        self.assertTrue(metrics["submitted"])
        self.assertTrue(metrics["correct"])
        self.assertGreaterEqual(metrics["inputClicks"], 4)
        self.assertEqual(metrics["submitClicks"], 1)
        self.assertEqual(metrics["confirmClicks"], 1)
        self.assertTrue(metrics["lastInputTrusted"])
        self.assertTrue(metrics["submitTrusted"])
        self.assertTrue(metrics["confirmTrusted"])

    def test_two_chapters_use_native_video_inner_scroll_and_confirmed_submission(self):
        self.launch("home.html")
        self.assertFalse(self.browser.poll_ready(), "主页不应被识别为课程章节")
        self.browser.page.get_by_test_id("open-course").click()
        self.wait_ready()
        first_key = self.browser.chapter_key()
        self.assertEqual(self.browser.chapter_kind(), "video")
        watch_started = time.monotonic()
        self.browser.watch_video()
        watch_elapsed = time.monotonic() - watch_started
        video = self.metrics()["1"]["video"]
        self.assertTrue(video["ended"])
        self.assertGreater(video["duration"], 2.5)
        self.assertGreaterEqual(watch_elapsed, video["duration"], "不能跳过真实播放时间")
        # Chromium dispatches play after advancing its media clock by up to a
        # frame. The full watch wall clock above has no timing tolerance.
        self.assertGreaterEqual(video["elapsed"] + 0.05, video["duration"])
        self.assertAlmostEqual(video["currentTime"], video["duration"], places=2)
        self.assertEqual(video["seekEvents"], 0)
        self.assertEqual(video["playbackRate"], 1)

        self.answer_and_submit()
        self.assert_quiz_events(self.metrics()["1"]["quiz"])
        self.assertTrue(self.browser.next_chapter())
        self.wait_ready()
        self.assertNotEqual(self.browser.chapter_key(), first_key)
        self.assertEqual(self.browser.chapter_kind(), "document")
        self.browser.read_document()
        document = self.metrics()["2"]["document"]
        self.assertTrue(document["atBottom"])
        self.assertGreater(document["scrollEvents"], 0)
        self.assertGreater(document["scrollHeight"], document["clientHeight"] * 2)
        self.assertGreaterEqual(document["scrollTop"] + document["clientHeight"], document["scrollHeight"] - 3)

        self.answer_and_submit()
        self.assert_quiz_events(self.metrics()["2"]["quiz"])
        self.assertFalse(self.browser.next_chapter())
        self.assertFalse(self.browser.poll_ready())
        self.browser.page.get_by_test_id("course-complete").wait_for(state="visible")
        for chapter in ("1", "2"):
            navigation = self.metrics()[chapter]["navigation"]
            self.assertTrue(navigation["quizTabTrusted"])
            self.assertTrue(navigation["nextTrusted"])

    def test_workflow_completes_both_chapters(self):
        from gaybar_tools.worker import DemoSolver
        from gaybar_tools.workflow import Workflow

        self.launch()
        self.wait_ready()
        solver = DemoSolver(self.browser, self.base_url)
        completed = Workflow(self.browser, solver, self.settings, self.stop, self.logs.append).run()
        self.assertEqual(completed, 2)
        self.assertEqual(self.browser.page.url, f"{self.base_url}/complete.html")
        self.assert_quiz_events(self.metrics()["1"]["quiz"])
        self.assert_quiz_events(self.metrics()["2"]["quiz"])

    def test_stop_during_video_preserves_page_without_answering(self):
        self.launch()
        self.wait_ready()
        current = self.browser.page.url
        timer = threading.Timer(0.6, self.stop.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        with self.assertRaises(self.StopRequested):
            self.browser.watch_video()
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(self.browser.page.url, current)
        metrics = self.metrics()
        self.assertNotIn("quiz", metrics.get("1", {}))
        self.assertNotIn("navigation", metrics.get("1", {}))
        videos = [frame.locator("video") for frame in self.browser.page.frames if frame.locator("video").count()]
        self.assertEqual(len(videos), 1)
        self.assertTrue(videos[0].evaluate("video => video.paused"), "停止任务应暂停正在播放的视频")

    def test_completed_newer_course_tab_does_not_resume_an_older_course(self):
        self.launch()
        self.wait_ready()
        older_course = self.browser.page
        newer_course = older_course.context.new_page()
        newer_course.goto(f"{self.base_url}/chapter.html?chapterId=2&courseId=demo")
        self.wait_ready()
        self.assertIs(self.browser.page, newer_course)
        newer_course.goto(f"{self.base_url}/complete.html")
        self.assertFalse(self.browser.poll_ready(), "课程结束后不得退回更早的其他课程标签")
        self.assertIs(self.browser.page, newer_course)
        self.assertIn("chapterId=1", older_course.url)

        newest_course = older_course.context.new_page()
        newest_course.goto(f"{self.base_url}/chapter.html?chapterId=1&courseId=demo")
        self.wait_ready()
        self.assertIs(self.browser.page, newest_course, "仍应跟随用户新打开的课程标签")


if __name__ == "__main__":
    unittest.main()
