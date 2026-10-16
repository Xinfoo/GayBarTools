"""Exercise the actual browser against the bundled, account-free course.

Run with ``python -m unittest discover -s tests -p test_browser_integration.py -v``. Only the
browser executable and Playwright are optional; page interactions are real.
"""

import functools
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

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
        answers = [Answer(question.id, choices[question.kind], "本地集成测试") for question in questions]
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

    def completion_api(self, content_for_request):
        """Use a real local endpoint without an account or paid model."""
        requests = []

        class SyntheticAPI(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(payload)
                content = content_for_request(payload, len(requests))
                body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        api = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticAPI)
        api_thread = threading.Thread(target=api.serve_forever, daemon=True)
        api_thread.start()

        def close_api():
            api.shutdown()
            api.server_close()
            api_thread.join(timeout=3)

        self.addCleanup(close_api)
        self.settings.base_url = f"http://127.0.0.1:{api.server_port}/v1"
        self.settings.model = "local-synthetic-model"
        return requests

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

    def test_model_confidence_does_not_interrupt_automatic_submission_or_navigation(self):
        from gaybar_tools.llm import QuizSolver
        from gaybar_tools.workflow import Workflow

        requests = []

        class SyntheticAPI(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(payload)
                text = payload["messages"][1]["content"][0]["text"]
                questions = json.loads(text.split("\n", 1)[1])
                choices = {"single": ["B"], "multiple": ["A", "B"], "boolean": ["A"]}
                answers = []
                for index, question in enumerate(questions):
                    answer = {
                        "question_id": question["question_id"],
                        "choices": choices[question["kind"]],
                        "reason": "本地合成 API 回答",
                    }
                    if len(requests) == 1:
                        answer["confidence"] = 0
                    elif index:
                        answer["confidence"] = "unused" if index == 1 else {"unused": True}
                    answers.append(answer)
                content = json.dumps({"answers": answers}, ensure_ascii=False)
                body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        api = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticAPI)
        api_thread = threading.Thread(target=api.serve_forever, daemon=True)
        api_thread.start()
        try:
            self.settings.base_url = f"http://127.0.0.1:{api.server_port}/v1"
            self.settings.model = "local-synthetic-model"
            self.settings.vision = True
            self.assertTrue(self.settings.auto_submit)
            self.launch()
            self.wait_ready()
            solver = QuizSolver(self.settings, self.logs.append, self.stop)
            with patch.dict(os.environ, {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}):
                completed = Workflow(self.browser, solver, self.settings, self.stop, self.logs.append).run()

            self.assertEqual(completed, 2)
            self.assertEqual(len(requests), 2)
            self.assertEqual(self.browser.page.url, f"{self.base_url}/complete.html")
            for chapter in ("1", "2"):
                self.assert_quiz_events(self.metrics()[chapter]["quiz"])
                self.assertTrue(self.metrics()[chapter]["navigation"]["nextTrusted"])
        finally:
            api.shutdown()
            api.server_close()
            api_thread.join(timeout=3)

    def test_format_retries_resume_submission_and_navigation_after_valid_answers(self):
        from gaybar_tools.llm import QuizSolver
        from gaybar_tools.workflow import Workflow

        def content_for_request(payload, attempt):
            questions = json.loads(payload["messages"][1]["content"][0]["text"].split("\n", 1)[1])
            choices = {"single": ["B"], "multiple": ["A", "B"], "boolean": ["A"]}
            answers = [
                {"question_id": question["question_id"], "choices": choices[question["kind"]]}
                for question in questions
            ]
            if attempt == 1:
                return "This is not JSON."
            if attempt == 2:
                answers = []
            elif attempt == 3:
                answers[0]["choices"] = ["Z"]
            elif attempt == 5:
                answers.pop()
            return json.dumps({"answers": answers}, ensure_ascii=False)

        requests = self.completion_api(content_for_request)
        observations = []
        browser_test = self

        class ObservedSolver(QuizSolver):
            def _completion(self, messages):
                content = super()._completion(messages)
                observations.append(browser_test.metrics())
                return content

        self.settings.vision = True
        self.launch()
        self.wait_ready()
        solver = ObservedSolver(self.settings, self.logs.append, self.stop)
        with patch.dict(os.environ, {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}):
            completed = Workflow(self.browser, solver, self.settings, self.stop, self.logs.append).run()

        self.assertEqual(completed, 2)
        self.assertEqual(len(requests), 6, "首章可在第三次重试成功；下一章应重新计算重试次数")
        for start, end in ((0, 4), (4, 6)):
            original = requests[start]["messages"][:2]
            for request_payload in requests[start:end]:
                self.assertEqual(request_payload["messages"][:2], original, "重试必须保留同一批原题和截图")
        for index, observation in enumerate(observations):
            chapter = "1" if index < 4 else "2"
            quiz = observation.get(chapter, {}).get("quiz", {})
            for event in ("inputClicks", "submitClicks", "confirmClicks"):
                self.assertEqual(quiz.get(event, 0), 0, "格式通过前不能填写或提交当前章")
        self.assertEqual(self.browser.page.url, f"{self.base_url}/complete.html")
        for chapter in ("1", "2"):
            self.assert_quiz_events(self.metrics()[chapter]["quiz"])
            self.assertTrue(self.metrics()[chapter]["navigation"]["nextTrusted"])

    def test_exhausted_format_retries_leave_quiz_without_clicking_answers(self):
        from gaybar_tools.llm import QuizSolver, SolverError
        from gaybar_tools.workflow import Workflow

        requests = self.completion_api(lambda _payload, _attempt: '{"answers": []}')
        self.settings.vision = True
        self.launch("quiz.html?chapterId=1&courseId=demo")
        self.wait_ready()
        current_url = self.browser.page.url
        solver = QuizSolver(self.settings, self.logs.append, self.stop)
        with patch.dict(os.environ, {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}):
            with self.assertRaises(SolverError) as failure:
                Workflow(self.browser, solver, self.settings, self.stop, self.logs.append).run()

        self.assertEqual(len(requests), 4, f"首次失败后最多再重试三次：{failure.exception}")
        self.assertEqual(self.browser.page.url, current_url)
        self.assertEqual(self.browser.page.locator("input:checked").count(), 0)
        quiz = self.metrics().get("1", {}).get("quiz", {})
        for event in ("inputClicks", "submitClicks", "confirmClicks"):
            self.assertEqual(quiz.get(event, 0), 0)
        self.assertFalse(quiz.get("submitted", False))

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
