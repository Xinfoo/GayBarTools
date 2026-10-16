"""Observed course markup: aria-hidden lists with visually usable choices.

All identifiers/text below are synthetic. No account, API key, remote course or
actual assessment content is used by this local Chromium regression.
"""

import importlib.util
import unittest

import test_browser_integration as integration
from gaybar_tools.models import Answer


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright 未安装")
class AriaHiddenOptionsIntegrationTests(unittest.TestCase):
    setUpClass = integration.BrowserIntegrationTests.__dict__["setUpClass"]
    tearDownClass = integration.BrowserIntegrationTests.__dict__["tearDownClass"]
    setUp = integration.BrowserIntegrationTests.setUp
    launch = integration.BrowserIntegrationTests.launch

    def metrics(self):
        return self.browser.page.evaluate("JSON.parse(localStorage.getItem('aria-hidden-options-metrics'))")

    def assert_complete_questions(self, questions):
        self.assertEqual([question.kind for question in questions], ["single", "single", "single", "boolean", "multiple"])
        self.assertEqual([len(question.options) for question in questions], [3, 4, 4, 2, 4])
        self.assertEqual([option.text for option in questions[0].options], ["第一题选项一", "第一题选项二", "第一题选项三"])
        self.assertEqual([option.label for option in questions[4].options], ["A", "B", "C", "D"])
        self.assertEqual([option.text for option in questions[3].options], ["对", "错"])

    def test_next_page_reads_five_visually_available_questions_then_submits_and_continues(self):
        from gaybar_tools.worker import DemoSolver
        from gaybar_tools.workflow import Workflow

        self.launch("aria_hidden_options.html?chapterId=local-aria-course&courseId=local")
        original_url = self.browser.current_url
        self.assertEqual(self.browser.prepare_start(), "document")
        self.assertEqual(self.browser.page.locator("input").count(), 0)
        calls = []
        case = self

        class ObservedSolver(DemoSolver):
            def solve(self, questions, screenshot=None):
                calls.append(questions)
                case.assertEqual(self.browser.current_url, original_url, "下一节只更换DOM，外层地址未变化")
                case.assert_complete_questions(questions)
                signature, ready = self.browser._view_signature("quiz")
                case.assertTrue(signature)
                case.assertTrue(ready, "aria-hidden不能使视觉可用的单选/多选题一直处于未就绪")
                return super().solve(questions, screenshot)

        solver = ObservedSolver(self.browser, self.base_url)
        completed = Workflow(self.browser, solver, self.settings, self.stop, self.logs.append).run()
        self.assertEqual(completed, 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.browser.current_url, f"{self.base_url}/complete.html")
        metrics = self.metrics()
        self.assertTrue(metrics["documentAtBottom"])
        self.assertEqual(metrics["answers"], {
            "local-single-one": ["B"], "local-single-two": ["B"], "local-single-three": ["B"],
            "local-boolean": ["A"], "local-multiple": ["A", "B"],
        })
        self.assertEqual(metrics["inputClicks"], 6)
        self.assertTrue(all(metrics["inputTrusted"]))
        self.assertEqual(metrics["submitClicks"], 1)
        self.assertEqual(metrics["submitTrusted"], [True])
        self.assertEqual(metrics["nextTrusted"], [True, True])
        self.assertEqual(metrics["oldClicks"], 0)
        self.assertTrue(metrics["submitted"])

    def test_aria_hidden_visual_choices_remain_available_while_css_hidden_old_data_is_filtered(self):
        self.launch("aria_hidden_options.html?chapterId=local-aria-course&courseId=local&start=quiz")
        self.assertEqual(self.browser.prepare_start(), "quiz")
        self.assertEqual(self.browser.page.locator("input").count(), 0)
        self.assertEqual(self.browser.page.locator("#local-multiple .num_option_dx").count(), 4)
        self.assertEqual(self.browser.page.locator("#local-multiple .num_option").count(), 0)
        lists = self.browser.page.locator("#quiz-form > .TiMu:not([id^=old-]) > ul")
        self.assertEqual([lists.nth(index).get_attribute("aria-hidden") for index in range(lists.count())],
                         ["true", "true", "true", None, "true"])
        self.assertTrue(all(self.browser._visible(lists.nth(index)) for index in range(lists.count())))
        self.assertFalse(self.browser._visible(self.browser.page.locator("#old-display-none")))
        self.assertFalse(self.browser._visible(self.browser.page.locator("#old-opacity-zero")))
        self.assertEqual(len(self.browser._question_blocks(self.browser.page.main_frame)), 5)
        questions = self.browser.get_questions()
        self.assert_complete_questions(questions)
        self.assertTrue(self.browser._view_signature("quiz")[1])
        self.assertEqual(self.metrics()["inputClicks"], 0)
        self.assertEqual(self.metrics()["oldClicks"], 0)

        def answers_with_multiple(choices):
            return [Answer(question.id, choices if question.kind == "multiple" else
                           (("A",) if question.kind == "boolean" else ("B",))) for question in questions]

        multiple = self.browser.page.locator("#local-multiple")
        self.browser.apply_answers(answers_with_multiple(("A", "B")))
        self.assertEqual(multiple.locator(".num_option_dx.check_answer_dx").all_text_contents(), ["A", "B"])
        self.assertEqual(multiple.locator(".check_answer").count(), 0, "多选使用真实check_answer_dx状态类")
        self.browser.apply_answers(answers_with_multiple(("B",)))
        self.assertEqual(multiple.locator(".num_option_dx.check_answer_dx").all_text_contents(), ["B"])
        self.browser.apply_answers(answers_with_multiple(("A", "B")))
        self.assertEqual(multiple.locator(".num_option_dx.check_answer_dx").all_text_contents(), ["A", "B"])
        self.assertEqual(self.metrics()["answers"]["local-multiple"], ["A", "B"])
        self.assertEqual(self.metrics()["inputClicks"], 8, "初始六次选择，加上取消A与重新选择A")
        self.assertTrue(all(self.metrics()["inputTrusted"]))
        self.assertEqual(self.metrics()["submitClicks"], 0)


if __name__ == "__main__":
    unittest.main()
