"""Dynamic assessment recognition with a retained player, without tab names."""

import importlib.util
import unittest
from unittest.mock import patch

import test_browser_integration as integration
from gaybar_tools.models import Answer


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright 未安装")
class RetainedVideoQuizIntegrationTests(unittest.TestCase):
    setUpClass = integration.BrowserIntegrationTests.__dict__["setUpClass"]
    tearDownClass = integration.BrowserIntegrationTests.__dict__["tearDownClass"]
    setUp = integration.BrowserIntegrationTests.setUp
    launch = integration.BrowserIntegrationTests.launch

    def metrics(self):
        return self.browser.page.evaluate("JSON.parse(localStorage.getItem('retained-video-quiz-metrics'))")

    def launch_fixture(self, query=""):
        self.launch(f"retained_video_quiz.html?chapterId=retained-video&courseId=local&{query}")
        self.assertEqual(self.browser.prepare_start(), "video")

    def complete_retained_player_transition(self, style, *, covered=False):
        self.launch_fixture(f"style={style}&cover={int(covered)}")
        video = self.browser.page.locator("video")
        self.browser.watch_video()
        self.assertTrue(video.evaluate("video => video.ended"))
        original_url = self.browser.current_url
        with patch.object(self.browser, "NEXT_PAGE_TIMEOUT", 3):
            self.assertTrue(self.browser.next_chapter(), "已结束播放器保留时仍须识别新出现的独立测验")
        self.assertEqual(self.browser.current_url, original_url)
        self.assertTrue(video.is_visible(), "用例必须保留可见的已结束播放器来复现真实页面布局")
        if covered:
            self.assertIsNone(self.browser._find_video_quiz(video), "独立章节表单覆盖播放器时不能误当播放器内弹题")
        self.assertEqual(self.browser.chapter_kind(), "quiz")
        questions = self.browser.get_questions()
        self.assertEqual(len(questions), 1)
        self.assertIn("完成视频后出现章节题目", questions[0].text)
        self.assertEqual([option.label for option in questions[0].options], ["A", "B"])
        self.assertIn("根据可见题干和选项", questions[0].options[1].text)
        self.browser.apply_answers([Answer(questions[0].id, ("B",))])
        self.assertTrue(self.browser.submit_quiz())
        self.assertFalse(self.browser.next_chapter())
        self.assertEqual(self.browser.current_url, f"{self.base_url}/complete.html")
        metrics = self.metrics()
        self.assertTrue(metrics["ended"])
        self.assertAlmostEqual(metrics["currentTime"], metrics["duration"], places=2)
        self.assertEqual(metrics["seekEvents"], 0)
        self.assertEqual(metrics["playbackRate"], 1)
        self.assertEqual(metrics["answer"], "B")
        self.assertTrue(all(metrics["inputTrusted"]))
        self.assertEqual(metrics["submitTrusted"], [True])
        self.assertEqual(metrics["nextTrusted"], [True, True])

    def test_chaoxing_question_structure_overrides_retained_ended_video(self):
        self.complete_retained_player_transition("chaoxing")

    def test_unknown_question_wrapper_and_standard_labels_need_no_fixed_tab_or_class(self):
        self.complete_retained_player_transition("labels")

    def test_ended_player_covered_by_independent_quiz_continues_submission_and_navigation(self):
        self.complete_retained_player_transition("chaoxing", covered=True)

    def test_unfinished_player_covered_by_independent_quiz_is_not_a_video_prompt(self):
        self.launch("retained_video_quiz.html?chapterId=retained-video&courseId=local&start=quiz&cover=1")
        self.browser.page.wait_for_function("document.querySelector('video').readyState > 0")
        video = self.browser.page.locator("video")
        self.assertTrue(video.is_visible(), "播放器布局和样式仍可见，仅被独立表单盖住")
        self.assertFalse(video.evaluate("video => video.ended"))
        self.assertEqual(self.browser._video_locators(), [])
        self.assertIsNone(self.browser._find_video_quiz(video))
        self.assertEqual(self.browser.prepare_start(), "quiz")
        self.assertEqual(len(self.browser.get_questions()), 1)

    def test_negative_offcanvas_player_is_excluded_but_scrollable_below_fold_player_remains(self):
        self.launch("retained_video_quiz.html?chapterId=retained-video&courseId=local&start=quiz")
        self.browser.page.wait_for_function("document.querySelector('video').readyState > 0")
        video = self.browser.page.locator("video")
        player = self.browser.page.locator("#retained-player")
        player.evaluate("player => player.style.cssText='position:absolute;left:-10000px;top:0'")
        self.assertTrue(video.is_visible(), "负坐标离屏播放器仍具有非零布局尺寸")
        self.assertFalse(video.evaluate("video => video.ended"))
        self.assertEqual(self.browser._video_locators(), [])
        self.assertEqual(self.browser.chapter_kind(), "quiz")
        player.evaluate("player => player.style.cssText='position:absolute;left:0;top:1800px'")
        self.assertGreater(video.bounding_box()["y"], self.browser.page.viewport_size["height"])
        self.assertTrue(video.is_visible())
        self.assertEqual(len(self.browser._video_locators()), 1, "可正常滚动到的下方视频不能当作已移出的旧播放器")
        self.assertEqual(self.browser.chapter_kind(), "video")

    def test_tab_caption_change_without_questions_does_not_prove_navigation(self):
        from gaybar_tools.browser import BrowserError

        self.launch_fixture("mode=tab-only")
        self.browser.watch_video()
        previous = self.browser.chapter_key()
        with patch.object(self.browser, "NEXT_PAGE_TIMEOUT", 2):
            with self.assertRaisesRegex(BrowserError, "章节没有变化"):
                self.browser.next_chapter()
        self.assertEqual(self.browser.page.locator("#activity-caption").inner_text(), "2 章节测验")
        self.assertEqual(self.browser.chapter_key(), previous)
        self.assertEqual(self.browser.chapter_kind(), "video")

    def test_unfinished_player_with_sibling_decoy_question_stays_video(self):
        self.launch_fixture("start=quiz")
        self.assertFalse(self.browser.page.locator("video").evaluate("video => video.ended"))
        self.assertTrue(self.browser.page.locator(".TiMu").is_visible())
        self.assertEqual(self.browser.chapter_kind(), "video", "播放活动中的兄弟题块不能抢走视频状态")
        self.assertEqual(self.metrics()["inputTrusted"], [])

    def test_clipped_unfinished_player_does_not_hide_visible_standard_quiz(self):
        self.launch("retained_video_quiz.html?chapterId=retained-video&courseId=local&start=quiz&clip=1&style=labels")
        video = self.browser.page.locator("video")
        self.assertFalse(video.evaluate("video => video.ended"))
        self.assertEqual(video.bounding_box()["height"], 360, "播放器自身仍有尺寸，只有父容器裁剪")
        self.assertEqual(self.browser.prepare_start(), "quiz")
        self.assertEqual(len(self.browser.get_questions()), 1)
        self.assertEqual(self.metrics()["inputTrusted"], [])

    def test_internal_scrollpane_preserves_below_fold_questions_and_real_selection(self):
        self.launch("complete.html")
        self.browser.page.set_content("""<!doctype html><html><head><style>
            html,body{margin:0;height:100vh;overflow:hidden;font:18px sans-serif}
            #quiz-scrollpane{margin:40px;height:360px;overflow:auto;border:1px solid #aaa}
            .TiMu{padding:20px;min-height:1100px}label{display:block;padding:14px}
            </style></head><body><div id="quiz-scrollpane">
            <section class="TiMu" id="scroll-question-one"><h3 class="Zy_TItle">1【单选题】第一道题应选择哪一项？</h3>
              <label><input type="radio" name="first" value="A">A. 第一项</label>
              <label><input type="radio" name="first" value="B">B. 第二项</label></section>
            <section class="TiMu" id="scroll-question-two"><h3 class="Zy_TItle">2【单选题】位于内部滚动区域下方的题目应怎样处理？</h3>
              <label><input type="radio" name="second" value="A">A. 因为当前屏幕看不到就漏掉</label>
              <label><input type="radio" name="second" value="B">B. 滚动到题目再正常选择</label></section>
            </div><script>window.scrollQuizClicks=[];
            document.addEventListener('click',event=>{if(event.target.matches('input'))
                window.scrollQuizClicks.push({name:event.target.name,value:event.target.value,trusted:event.isTrusted});});
            </script></body></html>""")
        second = self.browser.page.locator("#scroll-question-two")
        self.assertGreater(second.bounding_box()["y"], self.browser.page.viewport_size["height"])
        self.assertEqual(self.browser.prepare_start(), "quiz")
        questions = self.browser.get_questions()
        self.assertEqual([question.id for question in questions], ["scroll-question-one", "scroll-question-two"])
        self.browser.apply_answers([Answer(question.id, ("B",)) for question in questions])
        self.assertTrue(self.browser.page.locator('input[name="second"][value="B"]').is_checked())
        self.assertGreater(self.browser.page.locator("#quiz-scrollpane").evaluate("pane => pane.scrollTop"), 800)
        self.assertEqual(self.browser.page.evaluate("window.scrollY"), 0)
        self.assertEqual(self.browser.page.evaluate("window.scrollQuizClicks"), [
            {"name": "first", "value": "B", "trusted": True},
            {"name": "second", "value": "B", "trusted": True},
        ])
        self.assertEqual(len(self.browser.get_questions()), 2)

    def test_unknown_wrapper_with_hidden_native_radios_uses_visible_labels(self):
        self.launch("complete.html")
        self.browser.page.set_content("""<!doctype html><html><head><style>
            body{font:18px sans-serif;padding:30px}label{display:block;padding:20px;width:650px;cursor:pointer}
            input[type=radio]{display:none}
            </style></head><body><article class="custom-assessment-panel">
            <h3 class="custom-assessment-title">[单选题] 原生单选框被样式隐藏时，应怎样选择？</h3>
            <label><input type="radio" name="hidden-choice" value="A"><span>A. 点击不可见控件的坐标</span></label>
            <label><input type="radio" name="hidden-choice" value="B"><span>B. 点击可见文字行并核对原生选中状态</span></label>
            </article><script>window.hiddenInputClicks=[];
            document.addEventListener('click',event=>{if(event.target.matches('input'))
                window.hiddenInputClicks.push({value:event.target.value,trusted:event.isTrusted});});
            </script></body></html>""")
        self.assertFalse(self.browser.page.locator("input").first.is_visible())
        self.assertEqual(self.browser.prepare_start(), "quiz")
        questions = self.browser.get_questions()
        self.assertEqual(len(questions), 1)
        self.assertIn("原生单选框被样式隐藏", questions[0].text)
        self.assertEqual([option.label for option in questions[0].options], ["A", "B"])
        self.assertIn("点击可见文字行", questions[0].options[1].text)
        self.browser.apply_answers([Answer(questions[0].id, ("B",))])
        self.assertTrue(self.browser.page.locator('input[value="B"]').is_checked())
        self.assertFalse(self.browser.page.locator('input[value="A"]').is_checked())
        self.assertEqual(self.browser.page.evaluate("window.hiddenInputClicks"), [{"value": "B", "trusted": True}])


if __name__ == "__main__":
    unittest.main()
