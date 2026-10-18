"""Recognize Chaoxing's read-only answer views without resubmitting a quiz."""

import functools
import importlib.util
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import test_browser_integration as integration
from gaybar_tools.models import Answer


STYLE = """<style>
body{margin:20px;font:17px sans-serif}main{max-width:1000px}.TiMu{margin:25px 0}
.Zy_TItle{font-size:18px}label{display:block;padding:10px}button{font:inherit;padding:12px}
.ans-attach-ct{position:relative;padding:45px 15px 15px;margin:20px 0;border:1px solid #bbb}
.ans-job-icon{position:absolute;top:10px;left:12px;color:orange}
.myAnswerBx{padding:15px;background:#f5f6f9;display:flex;justify-content:space-between}
.answerScore{display:flex;gap:24px}.marking_dui{color:green}.newAnswerScore{font-weight:bold}
.document-viewer{height:280px;overflow:auto;background:#eee;padding:12px}
.document-viewer article{min-height:1150px;background:white;padding:15px}
[hidden]{display:none!important}
</style>"""


def result_row(*, waiting=False):
    score = ('' if waiting else '<div class="answerScore">'
             '<div class="CorrectOrNot fl"><span class="marking_dui">✓</span></div>'
             '<div class="fr newAnswerScore"><span class="scoreNum">9.1</span>分</div></div>')
    return f"""<section class="TiMu newTiMu ans-cc singleQuesId" data-question-id="graded-one">
<h3 class="Zy_TItle">1 [{'简答题' if waiting else '单选题'}] 本地测验问题</h3>
<div class="newAnswerBx"><div class="myAnswerBx marBot16">
<div class="myAnswer"><span class="answerFont fl">我的答案：</span>
<div class="answerCon">{'本地合成文本答案。' if waiting else 'A'}</div></div>
{score}</div></div></section>"""


def pending_review_result():
    return ('<div id="RightCon" class="RightCon newTestCon"><div class="newTestTitle">'
            '<div class="fr testTit_status">待批阅</div></div>' + result_row(waiting=True) + '</div>')


def pending_quiz(*, quoted=False, hidden=False):
    quote = '题干引用“我的答案：”“9.1分”仅为阅读资料，当前仍须作答。' if quoted else "本地测验问题"
    old = f'<div hidden>{result_row()}</div>' if hidden else ""
    return f"""<form id="workForm"><section class="TiMu newTiMu ans-cc singleQuesId"
data-question-id="pending-one"><h3 class="Zy_TItle">1 [单选题] {quote}</h3>
<label><input type="radio" name="q" value="A">A 本地选项</label>
<label><input type="radio" name="q" value="B">B 另一选项</label></section>
<button type="button" id="submit-quiz">提交</button></form>{old}"""


def quiz_page(mode):
    initial = result_row() if mode == "graded" else pending_quiz(
        quoted=mode == "quoted", hidden=mode == "hidden")
    if mode == "unconfirmed-short":
        initial = result_row(waiting=True)
    result = pending_review_result() if mode == "waiting" else result_row()
    if mode == "zero":
        result = result.replace('class="scoreNum">9.1', 'class="scoreNum">0').replace(
            'class="marking_dui">✓', 'class="marking_cuo">✗')
    elif mode == "mixed-result":
        result += result_row(waiting=True).replace('graded-one', 'graded-two')
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">{STYLE}</head>
<body><main id="quiz-content">{initial}</main><script>
window.gradeMetrics={{submitClicks:0,submitTrusted:[]}};
const submit=document.getElementById('submit-quiz');
if(submit)submit.addEventListener('click',event=>{{
 gradeMetrics.submitClicks++;gradeMetrics.submitTrusted.push(event.isTrusted);
 document.getElementById('quiz-content').innerHTML={result!r};
}});
</script></body></html>"""


SCOPED = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">{STYLE}</head>
<body><main><section id="sibling" class="ans-attach-ct" data-chapter-quiz>
<span class="ans-job-icon">任务点已完成</span>{result_row()}</section>
<section id="current" class="ans-attach-ct" data-chapter-quiz>
<span class="ans-job-icon">任务点未完成</span>{pending_quiz()}</section></main>
</body></html>"""


SCOPED_REVIEW = SCOPED.replace(result_row(), pending_review_result(), 1).replace(
    pending_quiz(), result_row(waiting=True).replace('graded-one', 'unconfirmed-one'))


MIXED = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">{STYLE}</head>
<body><main><h1>已提交测验与待阅读文档混排</h1>
<section id="graded-quiz" class="ans-attach-ct" data-chapter-quiz>
<span class="ans-job-icon" aria-label="任务点未完成">任务点</span>{result_row()}
<a href="#" id="edit-answers">修改答案</a></section>
<section id="pending-document" class="ans-attach-ct">
<span class="ans-job-icon" aria-label="任务点未完成">任务点</span>
<div class="document-viewer" data-document-viewer tabindex="0"><article>
<h2>本地后续阅读任务</h2><p style="padding-top:950px">阅读结束</p></article></div></section>
<button id="next-chapter" disabled>下一节</button></main><script>
history.replaceState(null,'','/knowledge/cards?chapterId=local-grade&courseId=local');
window.gradeMetrics={{editClicks:0,wheels:0,wheelTrusted:[],bottom:false,nextClicks:0,nextTrusted:[]}};
function persist(){{localStorage.setItem('grade-metrics',JSON.stringify(gradeMetrics));}}
document.getElementById('edit-answers').addEventListener('click',event=>{{event.preventDefault();gradeMetrics.editClicks++;persist();}});
const viewer=document.querySelector('.document-viewer'),next=document.getElementById('next-chapter');
viewer.addEventListener('wheel',event=>{{gradeMetrics.wheels++;gradeMetrics.wheelTrusted.push(event.isTrusted);persist();}},{{passive:true}});
viewer.addEventListener('scroll',()=>{{
 gradeMetrics.bottom=viewer.scrollTop+viewer.clientHeight>=viewer.scrollHeight-3;
 next.disabled=!(gradeMetrics.bottom&&gradeMetrics.wheels);persist();
}});
next.addEventListener('click',event=>{{gradeMetrics.nextClicks++;gradeMetrics.nextTrusted.push(event.isTrusted);persist();location.href='/complete.html';}});
persist();
</script></body></html>"""


class GradedFixtureHandler(integration.QuietHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/work/index":
            page = quiz_page(parse_qs(parsed.query).get("mode", ["score"])[0])
        else:
            page = {"/scoped.html": SCOPED, "/scoped-review.html": SCOPED_REVIEW,
                    "/mixed.html": MIXED}.get(parsed.path)
        if page is None:
            return super().do_GET()
        data = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright 未安装")
class GradedResultsIntegrationTests(unittest.TestCase):
    tearDownClass = integration.BrowserIntegrationTests.__dict__["tearDownClass"]
    setUp = integration.BrowserIntegrationTests.setUp
    launch = integration.BrowserIntegrationTests.launch

    @classmethod
    def setUpClass(cls):
        from gaybar_tools.browser import BrowserSession, StopRequested

        cls.BrowserSession, cls.StopRequested = BrowserSession, StopRequested
        cls.executable = integration.browser_executable()
        if not cls.executable:
            raise unittest.SkipTest("未找到浏览器")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(
            GradedFixtureHandler, directory=str(integration.DEMO)))
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    def submit_once(self, mode):
        if self.browser._context is None:
            self.launch(f"work/index?mode={mode}")
        else:
            self.browser.page.goto(f"{self.base_url}/work/index?mode={mode}")
        self.assertFalse(self.browser.quiz_completed())
        questions = self.browser.get_questions()
        self.assertEqual(len(questions), 1)
        self.browser.apply_answers([Answer(questions[0].id, ("A",))])
        self.assertTrue(self.browser.submit_quiz())
        self.assertTrue(self.browser.quiz_completed())
        self.assertTrue(self.browser.submit_quiz(), "重复调用也不能重新提交已完成测验")
        metrics = self.browser.page.evaluate("gradeMetrics")
        self.assertEqual(metrics, {"submitClicks": 1, "submitTrusted": [True]})

    def test_submit_accepts_real_structure_zero_score_and_unscored_short_once(self):
        for mode in ("score", "zero", "mixed-result"):
            with self.subTest(mode=mode):
                self.submit_once(mode)

    def test_submitted_short_answer_waiting_for_grading_needs_no_numeric_score(self):
        self.submit_once("waiting")

    def test_graded_sibling_does_not_complete_pending_quiz_in_same_document(self):
        self.launch("scoped.html")
        with self.browser.quiz_scope(self.browser.page.locator("#sibling")):
            self.assertTrue(self.browser.quiz_completed())
        with self.browser.quiz_scope(self.browser.page.locator("#current")):
            self.assertFalse(self.browser.quiz_completed())
            questions = self.browser.get_questions()
            self.assertEqual([q.id for q in questions], ["pending-one"])

    def test_hidden_old_grade_cannot_complete_visible_editable_quiz(self):
        self.launch("work/index?mode=hidden")
        self.assertFalse(self.browser.quiz_completed())
        self.assertEqual(len(self.browser.get_questions()), 1)

    def test_answer_and_score_words_in_pending_stem_are_not_submission_evidence(self):
        self.launch("work/index?mode=quoted")
        self.assertFalse(self.browser.quiz_completed())
        self.assertEqual(len(self.browser.get_questions()), 1)

    def test_readonly_answer_without_score_or_review_status_is_not_confirmed(self):
        self.launch("work/index?mode=unconfirmed-short")
        self.assertFalse(self.browser.quiz_completed())

    def test_sibling_review_header_cannot_complete_unconfirmed_readonly_answer(self):
        self.launch("scoped-review.html")
        with self.browser.quiz_scope(self.browser.page.locator("#sibling")):
            self.assertTrue(self.browser.quiz_completed())
        with self.browser.quiz_scope(self.browser.page.locator("#current")):
            self.assertFalse(self.browser.quiz_completed())

    def test_already_graded_quiz_is_skipped_then_document_and_next_complete(self):
        from gaybar_tools.workflow import Workflow

        self.launch("mixed.html")
        self.assertEqual(self.browser.prepare_start(), "mixed")
        tasks = self.browser.content_tasks()
        self.assertEqual([task.kind for task in tasks], ["quiz", "document"])
        self.assertEqual([task.completed for task in tasks], [True, False])
        solver = Mock(solve=Mock(side_effect=AssertionError("已提交测验不能再次调用模型")))
        self.assertEqual(Workflow(self.browser, solver, self.settings, self.stop, self.logs.append).run(), 1)
        solver.solve.assert_not_called()
        self.assertEqual(self.browser.current_url, f"{self.base_url}/complete.html")
        metrics = self.browser.page.evaluate("JSON.parse(localStorage.getItem('grade-metrics'))")
        self.assertEqual(metrics["editClicks"], 0)
        self.assertGreater(metrics["wheels"], 0)
        self.assertTrue(all(metrics["wheelTrusted"]))
        self.assertTrue(metrics["bottom"])
        self.assertEqual(metrics["nextClicks"], 1)
        self.assertEqual(metrics["nextTrusted"], [True])


if __name__ == "__main__":
    unittest.main()
