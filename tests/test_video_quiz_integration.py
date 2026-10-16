"""Account-free Chromium coverage of questions inserted into native playback.

The local HTTP provider below returns synthetic answers. No course account or
external model is used, and media playback/selection/submission use real events.
"""

import functools
import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import test_browser_integration as integration

from gaybar_tools.config import AppSettings
from gaybar_tools.models import Answer
from gaybar_tools.paths import AppPaths


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright 未安装")
class VideoQuizIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gaybar_tools.browser import BrowserError, BrowserSession, StopRequested

        cls.BrowserError, cls.BrowserSession, cls.StopRequested = BrowserError, BrowserSession, StopRequested
        cls.executable = integration.browser_executable()
        if not cls.executable:
            raise unittest.SkipTest("未找到 Chromium")
        handler = functools.partial(integration.QuietHandler, directory=str(integration.DEMO))
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
        temporary_root = integration.PROJECT / ".runtime" / "tmp" if os.name == "nt" else Path("/tmp")
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="gaybar-video-quiz-test-", dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.paths = AppPaths.for_root(Path(self.temporary.name)).prepare()
        self.settings = AppSettings(
            browser_path=self.executable, headless=True, vision=True, ocr=False,
            action_delay_min=0, action_delay_max=0, poll_interval=.05,
            video_timeout=20, max_chapters=2,
        )
        self.stop, self.logs = threading.Event(), []
        self.browser = self.BrowserSession(self.settings, self.paths, self.logs.append, self.stop)
        self.addCleanup(self.browser.close)

    completion_api = integration.BrowserIntegrationTests.completion_api

    def launch(self, mode="sequence"):
        self.browser.launch(
            f"{self.base_url}/video_quiz.html?chapterId=video-inserts&courseId=local&mode={mode}",
            headless=True,
        )

    def metrics(self):
        return self.browser.page.evaluate("JSON.parse(localStorage.getItem('video-quiz-metrics') || '{}')")

    def assert_untouched_decoys(self, metrics):
        self.assertEqual(metrics["decoys"], {"outside": 0, "sibling": 0})

    def assert_real_playback(self, metrics):
        self.assertTrue(metrics["ended"], "弹题之后仍须等待媒体自然结束")
        self.assertGreater(metrics["duration"], 2.5)
        self.assertAlmostEqual(metrics["currentTime"], metrics["duration"], places=2)
        self.assertGreaterEqual(metrics["elapsed"] + .05, metrics["duration"])
        self.assertEqual(metrics["seekEvents"], 0)
        self.assertEqual(metrics["playbackRate"], 1)
        self.assertEqual(metrics["rateChanges"], 0)

    def assert_no_popup_actions(self, metrics):
        self.assertEqual(len(metrics["questions"]), 1)
        question = metrics["questions"][0]
        self.assertEqual(question["inputClicks"], 0)
        self.assertEqual(question["submitClicks"], 0)
        self.assertEqual(question["continueClicks"], 0)
        self.assertFalse(question["resolved"])
        self.assertTrue(self.browser.page.locator("video").evaluate("v => v.paused"))
        self.assert_untouched_decoys(metrics)

    def test_two_insertions_retry_format_then_continue_to_learning_check(self):
        from gaybar_tools.llm import QuizSolver
        from gaybar_tools.workflow import Workflow

        batches = []

        def answer_locally(payload, attempt):
            questions = json.loads(payload["messages"][1]["content"][0]["text"].split("\n", 1)[1])
            batches.append(questions)
            if attempt == 1:
                return "deliberately invalid JSON for retry coverage"
            choices = {"single": ["B"], "multiple": ["A", "B"], "boolean": ["A"]}
            answers = []
            for question in questions:
                selected = choices[question["kind"]]
                if "视频中的第二道题" in question["text"]:
                    selected = ["B"]
                answers.append({"question_id": question["question_id"], "choices": selected})
            return json.dumps({"answers": answers}, ensure_ascii=False)

        requests = self.completion_api(answer_locally)
        self.launch()
        solver = QuizSolver(self.settings, self.logs.append, self.stop)
        with patch.dict(os.environ, {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}):
            completed = Workflow(self.browser, solver, self.settings, self.stop, self.logs.append).run()

        self.assertEqual(completed, 2)
        self.assertEqual(len(requests), 4, "首次格式失败、重试成功、第二道视频题和最后学习检测")
        self.assertEqual(requests[0]["messages"][:2], requests[1]["messages"][:2])
        for batch in batches[:3]:
            self.assertEqual(len(batch), 1, "不能混入外层、隐藏或兄弟框架学习检测")
            self.assertEqual(batch[0]["question_id"], "reused-video-question")
        self.assertNotEqual(batches[1][0]["text"], batches[2][0]["text"], "相同ID的后续弹题也须重新解答")
        self.assertTrue(any(part.get("type") == "image_url" for part in requests[0]["messages"][1]["content"]))
        metrics = self.metrics()
        self.assert_real_playback(metrics)
        self.assert_untouched_decoys(metrics)
        self.assertTrue(metrics["nextTrusted"])
        self.assertEqual(len(metrics["questions"]), 2)
        first, second = metrics["questions"]
        self.assertEqual((first["inputClicks"], first["submitClicks"], first["continueClicks"]), (1, 1, 1))
        self.assertEqual((second["inputClicks"], second["submitClicks"], second["continueClicks"]), (1, 0, 0))
        for question in metrics["questions"]:
            self.assertTrue(question["resolved"])
            self.assertTrue(question["trusted"])
            self.assertTrue(all(question["trusted"]), "所有作答和提交均应通过真实浏览器输入")
        chapter_metrics = self.browser.page.evaluate("JSON.parse(localStorage.getItem('gaybar-demo-metrics'))")
        self.assertTrue(chapter_metrics["video-inserts"]["quiz"]["submitted"])
        self.assertTrue(chapter_metrics["video-inserts"]["quiz"]["correct"])
        self.assertTrue(chapter_metrics["video-inserts"]["navigation"]["quizNextTrusted"])
        self.assertEqual(self.browser.page.url, f"{self.base_url}/complete.html")

    def test_already_selected_answer_still_submits_and_continues(self):
        self.launch("preselected")
        calls = []

        def answer(questions, screenshot):
            calls.append(questions)
            self.assertTrue(screenshot)
            return [Answer(question.id, ("A",)) for question in questions]

        self.browser.watch_video(on_quiz=answer)
        self.assertEqual(len(calls), 1)
        metrics = self.metrics()
        self.assert_real_playback(metrics)
        question = metrics["questions"][0]
        self.assertEqual((question["inputClicks"], question["submitClicks"], question["continueClicks"]), (0, 1, 1))
        self.assertTrue(all(question["trusted"]))
        self.assert_untouched_decoys(metrics)

    def test_nested_frame_plain_overlay_filters_outside_option_and_stem_error_words(self):
        self.launch("preselected")
        source = f"{self.base_url}/video_quiz.html?chapterId=video-inserts&courseId=local&mode=preselected"
        self.browser.page.set_content(
            '<!doctype html><html><body><h1>外层课程框架</h1>'
            f'<iframe title="内嵌视频活动" src="{source}" style="width:1100px;height:850px;border:0"></iframe>'
            '</body></html>'
        )
        inner = self.browser.page.frame_locator('iframe[title="内嵌视频活动"]')
        inner.locator("video").wait_for(state="visible")
        inner.locator("video").evaluate("""video => {
            const popup=video.parentElement.querySelector('.ans-videoquiz');
            popup.className='plain-overlay';popup.removeAttribute('data-video-quiz');
            const original=window.showQuestion;
            window.showQuestion=function(){
                original();
                popup.querySelector('.Zy_TItle').textContent+='题干中的“回答错误”字样不是提交后的结果反馈。';
            };
            const outside=document.createElement('label');
            outside.style.cssText='position:fixed;left:20px;top:650px;color:black';
            outside.innerHTML='<input type="radio" name="inserted-question" value="C">C. 视频之外的邻近控件';
            popup.querySelector('.TiMu').appendChild(outside);
        }""")
        calls = []

        def answer(questions, screenshot):
            calls.append(questions)
            self.assertEqual(len(questions), 1)
            self.assertIn("回答错误", questions[0].text)
            self.assertEqual([option.label for option in questions[0].options], ["A", "B"])
            self.assertTrue(screenshot.startswith(b"\x89PNG\r\n\x1a\n"))
            width, height = struct.unpack(">II", screenshot[16:24])
            self.assertEqual((width, height), (640, 360), "应按内嵌视频的页面坐标裁剪截图")
            return [Answer(questions[0].id, ("A",))]

        self.browser.watch_video(on_quiz=answer)
        self.assertEqual(len(calls), 1)
        metrics = self.metrics()
        self.assert_real_playback(metrics)
        self.assert_untouched_decoys(metrics)
        question = metrics["questions"][0]
        self.assertEqual((question["inputClicks"], question["submitClicks"], question["continueClicks"]), (0, 1, 1))
        self.assertTrue(all(question["trusted"]))

    def test_rejected_answer_stops_in_bounded_time_without_repeat_submissions(self):
        self.launch("wrong")
        calls = []

        def wrong_answer(questions, _screenshot):
            calls.append(questions)
            return [Answer(question.id, ("B",)) for question in questions]

        started = time.monotonic()
        with patch.object(self.BrowserSession, "VIDEO_QUIZ_WAIT_TIMEOUT", 1.0):
            with self.assertRaises(self.BrowserError):
                self.browser.watch_video(on_quiz=wrong_answer)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(len(calls), 1, "错误反馈不应导致反复请求模型")
        metrics = self.metrics()
        self.assertEqual(len(metrics["questions"]), 1)
        question = metrics["questions"][0]
        self.assertEqual((question["inputClicks"], question["submitClicks"], question["continueClicks"]), (1, 1, 0))
        self.assertFalse(question["resolved"])
        self.assertFalse(metrics["ended"])
        self.assertTrue(self.browser.page.locator("video").evaluate("v => v.paused"))
        self.assert_untouched_decoys(metrics)

    def test_stop_during_model_callback_does_not_click_and_leaves_media_paused(self):
        self.launch("stop")

        def stopped_answer(questions, _screenshot):
            self.stop.set()
            return [Answer(question.id, ("A",)) for question in questions]

        with self.assertRaises(self.StopRequested):
            self.browser.watch_video(on_quiz=stopped_answer)
        self.assert_no_popup_actions(self.metrics())

    def test_question_changed_during_callback_rejects_stale_answer(self):
        self.launch("mutate")

        def stale_answer(questions, _screenshot):
            self.browser.page.evaluate("mutateActiveQuestion()")
            return [Answer(question.id, ("A",)) for question in questions]

        with self.assertRaises(self.BrowserError):
            self.browser.watch_video(on_quiz=stale_answer)
        self.assert_no_popup_actions(self.metrics())


if __name__ == "__main__":
    unittest.main()
