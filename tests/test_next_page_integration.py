"""Real input coverage of next-page navigation without a fixed quiz tab."""

import importlib.util
import time
import unittest

import test_browser_integration as integration


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright 未安装")
class NextPageIntegrationTests(unittest.TestCase):
    setUpClass = integration.BrowserIntegrationTests.__dict__["setUpClass"]
    tearDownClass = integration.BrowserIntegrationTests.__dict__["tearDownClass"]
    setUp = integration.BrowserIntegrationTests.setUp
    launch = integration.BrowserIntegrationTests.launch

    def wait_ready(self):
        # Explicit Start intentionally accepts actual content even when the
        # outer page lacks the site's old fixed tab / URL readiness markers.
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.browser.prepare_start() in {"video", "document", "quiz"}:
                return
            self.browser.page.wait_for_timeout(50)
        self.fail("本地连续学习页面未就绪")

    def metrics(self):
        return self.browser.page.evaluate("JSON.parse(localStorage.getItem('next-page-metrics') || '{}')")

    def run_sequence(self, start="document"):
        from gaybar_tools.worker import DemoSolver
        from gaybar_tools.workflow import Workflow

        self.launch(f"next_page.html?chapterId=same-chapter&courseId=local&start={start}")
        self.wait_ready()
        calls = []

        class ObservedSolver(DemoSolver):
            def solve(self, questions, screenshot=None):
                calls.append(questions)
                return super().solve(questions, screenshot)

        solver = ObservedSolver(self.browser, self.base_url)
        completed = Workflow(self.browser, solver, self.settings, self.stop, self.logs.append).run()
        self.assertEqual(self.browser.page.url, f"{self.base_url}/complete.html")
        metrics = self.metrics()
        self.assertTrue(all(metrics["nextTrusted"]))
        self.assertEqual(metrics["hiddenClicks"], 0)
        return completed, calls, metrics

    def test_two_documents_then_quiz_without_fixed_quiz_entry(self):
        completed, calls, metrics = self.run_sequence()
        self.assertEqual(completed, 3)
        self.assertEqual(len(calls), 1, "连续内容页不应调用模型或寻找不存在的检测入口")
        self.assertEqual([question.id for question in calls[0]], ["sequence-question"])
        self.assertEqual(metrics["nextClicks"], 3)
        for document in metrics["documents"].values():
            self.assertTrue(document["atBottom"])
            self.assertGreater(document["scrollEvents"], 0)
        self.assertEqual(metrics["answer"], "B")
        self.assertTrue(metrics["inputTrusted"])
        self.assertEqual(metrics["submitClicks"], 1)
        self.assertEqual(metrics["submitTrusted"], [True])

    def test_starting_on_quiz_answers_and_navigates_directly(self):
        completed, calls, metrics = self.run_sequence("quiz")
        self.assertEqual(completed, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(metrics["documents"], {})
        self.assertTrue(metrics["submitted"])
        self.assertEqual(metrics["nextClicks"], 1)

    def test_starting_on_submitted_quiz_skips_model_and_duplicate_submission(self):
        completed, calls, metrics = self.run_sequence("submitted")
        self.assertEqual(completed, 1)
        self.assertEqual(calls, [])
        self.assertTrue(metrics["alreadySubmitted"])
        self.assertEqual(metrics["submitClicks"], 0)
        self.assertEqual(metrics["nextClicks"], 1)

    def test_same_address_dom_replacement_waits_for_new_visible_content(self):
        self.launch("next_page.html?chapterId=same-chapter&courseId=local")
        self.wait_ready()
        self.assertEqual(self.browser.chapter_kind(), "document", "透明预加载视频和题目不可覆盖当前文档")
        self.assertEqual(self.browser.page.get_by_role("button", name="2 学习检测").count(), 0)
        initial_url = self.browser.page.url
        first_key = self.browser.chapter_key()
        self.browser.read_document()
        self.assertTrue(self.browser.next_chapter())
        self.assertEqual(self.browser.page.url, initial_url)
        self.assertEqual(self.browser.page.locator("#document-title").inner_text(), "可疑链接辨别")
        self.assertNotEqual(self.browser.chapter_key(), first_key)
        self.assertEqual(self.browser.chapter_kind(), "document")
        second_key = self.browser.chapter_key()
        self.browser.read_document()
        self.assertTrue(self.browser.next_chapter())
        self.assertEqual(self.browser.page.url, initial_url)
        self.assertNotEqual(self.browser.chapter_key(), second_key)
        self.assertEqual(self.browser.chapter_kind(), "quiz", "隐藏的旧文档和视频不得误判为当前页面")
        self.assertEqual([question.id for question in self.browser.get_questions()], ["sequence-question"])
        self.assertFalse(self.browser.quiz_completed(), "透明隐藏的旧提交结果不得视为本次已提交")


if __name__ == "__main__":
    unittest.main()
