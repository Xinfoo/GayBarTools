"""Account-free ordered chapter assessments mixed with native media/readers."""

import functools
import importlib.util
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit
from unittest.mock import Mock, patch

import test_browser_integration as integration
from gaybar_tools.models import Answer


MAIN = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<style>
*{box-sizing:border-box}body{margin:0;font:17px sans-serif}main{width:1020px;margin:20px}
.ans-attach-ct{position:relative;padding:42px 12px 12px;margin:24px 0;border:1px solid #bbb}
.ans-job-icon{position:absolute;top:8px;left:10px;width:160px;height:24px;color:orange}
.ans-job-finished>.ans-job-icon{color:green}iframe{width:100%;height:330px;border:0;display:block}
.video-js{position:relative;width:640px;height:360px;background:black}video{width:640px;height:360px}
.vjs-big-play-button{position:absolute;left:260px;top:145px;padding:15px;font:inherit}
nav{display:flex;justify-content:flex-end;margin:30px 0}button{font:inherit;padding:12px 24px}
[hidden]{display:none!important}
</style></head><body><main><h1>本地混排章节</h1><div id="tasks"></div>
<nav><button id="next-chapter" disabled>下一节</button></nav></main><script>
const params=new URLSearchParams(location.search),store='inline-quiz-metrics';
history.replaceState(null,'','/knowledge/cards?chapterId=local-inline&courseId=local');
const ids=params.get('layout')==='late-first'?['quiz-a','video','doc-b']:['doc-a','quiz-a','video','quiz-b','doc-b'];
const skip=params.get('completed'),unpublished=params.get('unpublished'),late=params.get('late');
const precompleted=id=>id===skip||(params.get('all-completed')==='1'&&id!==unpublished);
const metrics={expected:ids.filter(id=>!precompleted(id)&&id!==unpublished),completed:[],events:[],tasks:{},violations:[],nextClicks:0,nextTrusted:[],decoyClicks:0};
function persist(){localStorage.setItem(store,JSON.stringify(metrics));}
function check(id,event){const i=metrics.expected.indexOf(id),unfinished=metrics.expected.slice(0,i).filter(p=>!metrics.completed.includes(p));if(unfinished.length)metrics.violations.push({id,event,unfinished});}
window.inlineEvent=(id,event,detail={})=>{check(id,event);metrics.events.push({id,event,...detail});persist();};
window.inlineReady=(id)=>{metrics.tasks[id].ready=true;persist();};
window.inlineDone=id=>{if(metrics.completed.includes(id))return;check(id,'complete');metrics.completed.push(id);metrics.events.push({id,event:'complete'});const card=document.getElementById(id);card.classList.add('ans-job-finished');card.querySelector('.ans-job-icon').setAttribute('aria-label','任务点已完成');card.querySelector('.ans-job-icon').textContent='任务点已完成';document.getElementById('next-chapter').disabled=!metrics.expected.every(p=>metrics.completed.includes(p));persist();};
window.inlineUpdate=(id,detail)=>{Object.assign(metrics.tasks[id],detail);persist();};
window.inlineDecoyClicked=()=>{metrics.decoyClicks++;persist();};
for(const id of ids){
 const kind=id.startsWith('quiz')?'quiz':id==='video'?'video':'document';
 metrics.tasks[id]={kind,ready:false,wheels:0,playClicks:0,seekEvents:0,rate:1,answerClicks:0,submitClicks:0,confirmClicks:0};
 const card=document.createElement('section');card.id=id;card.className='ans-attach-ct'+(precompleted(id)?' ans-job-finished':'');
 card.innerHTML=`<span class="ans-job-icon" aria-label="${precompleted(id)?'任务点已完成':'任务点未完成'}">${precompleted(id)?'任务点已完成':'任务点'}</span><h2>${id}</h2>`;
 document.getElementById('tasks').appendChild(card);
 if(kind==='video'){
   card.innerHTML+='<div class="video-js"><video class="vjs-tech" controls preload="none" src="/lesson.webm"></video><button class="vjs-big-play-button" aria-label="播放视频">播放</button></div>';
   const video=card.querySelector('video'),play=card.querySelector('button');let started=0;
   play.addEventListener('click',event=>{metrics.tasks[id].playClicks++;inlineEvent(id,'play-click',{trusted:event.isTrusted});video.play();});
   video.addEventListener('play',()=>{play.hidden=true;if(!started)started=performance.now();});
   video.addEventListener('seeking',()=>{metrics.tasks[id].seekEvents++;persist();});
   video.addEventListener('ratechange',()=>{metrics.tasks[id].rate=video.playbackRate;persist();});
   video.addEventListener('ended',()=>{inlineUpdate(id,{ended:true,time:video.currentTime,duration:video.duration,elapsed:(performance.now()-started)/1000});inlineDone(id);});
   inlineReady(id);
 }else{
   const frame=document.createElement('iframe');frame.dataset.task=id;
   frame.src=(kind==='quiz'?'/work/index':'/document.html')+'?task='+id+'&completed='+precompleted(id)+'&unpublished='+(id===unpublished)+'&late='+(id===late)+'&reload='+(id===params.get('reload'))+'&confirmDelay='+(params.get('sibling-dialog')==='1'?500:0);
   card.appendChild(frame);
 }
}
if(params.get('sibling-dialog')==='1'){const frame=document.createElement('iframe');frame.id='sibling-confirmation';frame.src='/dialog.html';frame.style.cssText='position:fixed;right:10px;top:10px;width:250px;height:180px;border:1px solid #aaa';document.body.appendChild(frame);}
window.inlineFixtureMounted=true;
window.startDeferredInlineQuiz=()=>{const frame=[...document.querySelectorAll('iframe')].find(f=>f.dataset.task===late);frame.contentWindow.startDeferredInlineQuiz();};
document.getElementById('next-chapter').addEventListener('click',event=>{metrics.nextClicks++;metrics.nextTrusted.push(event.isTrusted);inlineEvent('next','next',{trusted:event.isTrusted});location.href='/complete.html';});
document.getElementById('next-chapter').disabled=!metrics.expected.every(id=>metrics.completed.includes(id));
persist();
</script></body></html>"""


DOCUMENT = r"""<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;font:17px sans-serif}.document-viewer{height:300px;overflow:auto;padding:12px;background:#eee}
article{min-height:1050px;padding:20px;background:white}
</style></head><body><div id="document-viewer" class="document-viewer" data-document-viewer tabindex="0">
<article><h2>本地阅读资料</h2><p>在当前阅读器内正常滚动，完成后再处理下一项任务。</p><p style="padding-top:900px">阅读结束</p></article>
</div><script>
const id=new URLSearchParams(location.search).get('task'),viewer=document.getElementById('document-viewer');
viewer.addEventListener('wheel',event=>{window.wheels=(window.wheels||0)+1;top.inlineUpdate(id,{wheels:window.wheels,wheelTrusted:event.isTrusted});top.inlineEvent(id,'wheel',{trusted:event.isTrusted});},{passive:true});
viewer.addEventListener('scroll',()=>{const bottom=viewer.scrollTop+viewer.clientHeight>=viewer.scrollHeight-3;top.inlineUpdate(id,{bottom});if(bottom&&window.wheels)top.inlineDone(id);});
top.inlineReady(id);
</script></body></html>"""


QUIZ = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>
body{margin:0;padding:10px;font:17px sans-serif}.TiMu{margin:10px 0}.Zy_TItle{font-size:18px}
label{display:block;padding:10px}.quiz-buttons{padding:12px;text-align:center}button{padding:10px 24px;font:inherit}
.confirm-dialog{position:fixed;inset:40px 100px;background:white;border:2px solid #888;padding:20px;text-align:center}
[hidden]{display:none!important}
</style></head><body><main id="quiz-host">加载中</main><script>
const params=new URLSearchParams(location.search),id=params.get('task');let answerClicks=0,submitClicks=0,confirmClicks=0;
function render(){
 document.getElementById('quiz-host').innerHTML=`<form id="workForm"><section class="TiMu" data-question-id="shared-question"><h3 class="Zy_TItle">1 [单选题] ${id} 应选择哪个选项？</h3><label><input type="radio" name="same-name" value="A">A 正确的本地选项</label><label><input type="radio" name="same-name" value="B">B 另一选项</label></section><div class="quiz-buttons"><button id="submit-quiz" type="button">提交</button></div></form><div id="submission-result" role="status"></div><section class="confirm-dialog" role="dialog" hidden><p>确认提交？</p><button id="confirm-submit" type="button">提交</button></section>`;
 const form=document.getElementById('workForm'),dialog=document.querySelector('.confirm-dialog');
 form.addEventListener('click',event=>{if(event.target.matches('input')){top.inlineUpdate(id,{answerClicks:++answerClicks,inputTrusted:event.isTrusted});top.inlineEvent(id,'answer-click',{trusted:event.isTrusted});}});
 document.getElementById('submit-quiz').addEventListener('click',event=>{top.inlineUpdate(id,{submitClicks:++submitClicks});top.inlineEvent(id,'submit',{trusted:event.isTrusted});if(form.querySelector('input:checked'))setTimeout(()=>dialog.hidden=false,Number(params.get('confirmDelay')||0));});
 document.getElementById('confirm-submit').addEventListener('click',event=>{top.inlineUpdate(id,{confirmClicks:++confirmClicks,answer:form.querySelector('input:checked').value});top.inlineEvent(id,'confirm',{trusted:event.isTrusted});if(params.get('reload')==='true'){location.href='/work/result?task='+id;return;}form.hidden=true;dialog.hidden=true;document.getElementById('submission-result').textContent='提交成功';top.inlineDone(id);});
 top.inlineReady(id);
}
if(params.get('completed')==='true'){document.getElementById('quiz-host').innerHTML='<div id="submission-result" role="status">已提交，本次测验已完成</div>';top.inlineReady(id);}
else if(params.get('unpublished')==='true'){document.getElementById('quiz-host').textContent='教师未创建完成该测验';top.inlineReady(id);}
else if(params.get('late')!=='true')render();
window.startDeferredInlineQuiz=()=>{top.inlineUpdate(id,{deferredStarted:true});setTimeout(render,900);};
window.inlineQuizMounted=true;
</script></body></html>"""


RESULT = r"""<!doctype html><html><head><meta charset="utf-8"></head><body>
<div id="submission-result" role="status">提交成功，本次测验已完成</div><script>
const id=new URLSearchParams(location.search).get('task');top.inlineUpdate(id,{resultReloaded:true});top.inlineReady(id);top.inlineDone(id);
</script></body></html>"""


DIALOG = r"""<!doctype html><html><head><meta charset="utf-8"></head><body>
<section class="confirm-dialog" role="dialog"><p>确认提交？</p><button id="decoy-submit">提交</button></section>
<script>document.getElementById('decoy-submit').addEventListener('click',()=>top.inlineDecoyClicked());</script>
</body></html>"""


class InlineFixtureHandler(integration.QuietHandler):
    def do_GET(self):
        pages = {"/inline.html": MAIN, "/document.html": DOCUMENT, "/work/index": QUIZ,
                 "/work/result": RESULT, "/dialog.html": DIALOG}
        page = pages.get(urlsplit(self.path).path)
        if page is None:
            return super().do_GET()
        data = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright 未安装")
class InlineQuizTasksIntegrationTests(unittest.TestCase):
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
            InlineFixtureHandler, directory=str(integration.DEMO)))
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    def metrics(self):
        return self.browser.page.evaluate("JSON.parse(localStorage.getItem('inline-quiz-metrics'))")

    def launch_inline(self, query=""):
        self.launch("inline.html?chapterId=local-inline&courseId=local&" + query)
        self.browser.page.wait_for_function("window.inlineFixtureMounted && [...document.querySelectorAll('iframe')].every(f=>f.contentDocument?.readyState==='complete')")

    def solver(self, expected_ids):
        observed = []

        def solve(questions, screenshot=None):
            self.assertIsNone(screenshot)
            self.assertEqual(len(questions), 1, "每次只能向模型发送当前任务的题目")
            self.assertEqual(questions[0].id, "shared-question", "相同题号应由任务作用域隔离")
            task_id = expected_ids[len(observed)]
            self.assertIn(task_id, questions[0].text)
            observed.append(task_id)
            return [Answer(questions[0].id, ("A",))]

        return Mock(solve=Mock(side_effect=solve)), observed

    def run_inline(self, query="", *, expected_quizzes=("quiz-a", "quiz-b")):
        from gaybar_tools.workflow import Workflow

        self.launch_inline(query)
        self.assertEqual(self.browser.prepare_start(), "mixed")
        initial_key = self.browser.chapter_key()
        solver, observed = self.solver(expected_quizzes)
        task_kinds = []
        counts = []
        original = self.browser.process_content

        def process(**kwargs):
            on_task = kwargs.get("on_task")

            def started(kind, ordinal, total):
                task_kinds.append(kind)
                self.assertEqual(ordinal, len(task_kinds))
                self.assertGreaterEqual(total, ordinal)
                if on_task:
                    on_task(kind, ordinal, total)

            kwargs["on_task"] = started
            count = original(**kwargs)
            counts.append(count)
            self.assertEqual(self.browser.chapter_key(), initial_key,
                             "填写答案、提交结果及任务标记变化不能变更同一混排页的身份")
            return count

        with patch.object(self.browser, "process_content", side_effect=process):
            completed = Workflow(self.browser, solver, self.settings, self.stop, self.logs.append).run()
        self.assertEqual(completed, 1)
        self.assertEqual(observed, list(expected_quizzes))
        metrics = self.metrics()
        self.assertEqual(metrics["completed"], metrics["expected"])
        self.assertEqual(metrics["violations"], [])
        self.assertEqual(counts, [len(metrics["expected"])])
        self.assertEqual(metrics["nextClicks"], 1)
        self.assertEqual(metrics["nextTrusted"], [True])
        self.assertEqual(self.browser.current_url, f"{self.base_url}/complete.html")
        self.assertTrue(all(event["trusted"] for event in metrics["events"] if "trusted" in event))
        for task_id in expected_quizzes:
            self.assertEqual(metrics["tasks"][task_id]["answerClicks"], 1)
            self.assertEqual(metrics["tasks"][task_id]["submitClicks"], 1)
            self.assertEqual(metrics["tasks"][task_id]["confirmClicks"], 1)
            self.assertEqual(metrics["tasks"][task_id]["answer"], "A")
        video = metrics["tasks"]["video"]
        self.assertEqual(video["playClicks"], 1)
        self.assertTrue(video["ended"])
        self.assertEqual(video["seekEvents"], 0)
        self.assertEqual(video["rate"], 1)
        self.assertAlmostEqual(video["time"], video["duration"], places=2)
        self.assertGreaterEqual(video["elapsed"] + .05, video["duration"])
        return metrics, task_kinds

    def test_document_quiz_video_quiz_document_are_scoped_and_ordered_before_one_next(self):
        metrics, kinds = self.run_inline()
        self.assertEqual(kinds, ["document", "quiz", "video", "quiz", "document"])
        self.assertGreater(metrics["tasks"]["doc-a"]["wheels"], 0)
        self.assertGreater(metrics["tasks"]["doc-b"]["wheels"], 0)

    def test_completed_quiz_result_cannot_skip_later_pending_quiz(self):
        metrics, kinds = self.run_inline("completed=quiz-a", expected_quizzes=("quiz-b",))
        self.assertEqual(kinds, ["document", "video", "quiz", "document"])
        self.assertEqual(metrics["tasks"]["quiz-a"]["answerClicks"], 0)
        self.assertEqual(metrics["tasks"]["quiz-a"]["submitClicks"], 0)

    def test_teacher_unpublished_quiz_is_skipped_without_model_or_submit(self):
        metrics, kinds = self.run_inline("unpublished=quiz-a", expected_quizzes=("quiz-b",))
        self.assertEqual(kinds, ["document", "video", "quiz", "document"])
        self.assertEqual(metrics["tasks"]["quiz-a"]["answerClicks"], 0)
        self.assertEqual(metrics["tasks"]["quiz-a"]["submitClicks"], 0)

    def test_manual_submission_stops_at_first_inline_quiz_before_later_media_or_next(self):
        from gaybar_tools.workflow import Workflow

        self.launch_inline()
        self.settings.auto_submit = False
        solver, observed = self.solver(("quiz-a",))
        completed = Workflow(self.browser, solver, self.settings, self.stop, self.logs.append).run()
        self.assertEqual(completed, 0)
        self.assertEqual(observed, ["quiz-a"])
        metrics = self.metrics()
        self.assertEqual(metrics["completed"], ["doc-a"])
        self.assertEqual(metrics["tasks"]["quiz-a"]["answerClicks"], 1)
        self.assertEqual(metrics["tasks"]["quiz-a"]["submitClicks"], 0)
        self.assertEqual(metrics["tasks"]["video"]["playClicks"], 0)
        self.assertEqual(metrics["tasks"]["quiz-b"]["answerClicks"], 0)
        self.assertEqual(metrics["tasks"]["doc-b"]["wheels"], 0)
        self.assertEqual(metrics["nextClicks"], 0)
        self.assertEqual(metrics["violations"], [])

    def test_loading_inline_quiz_before_video_waits_for_form_and_keeps_order(self):
        from gaybar_tools.workflow import Workflow

        self.launch_inline("layout=late-first&late=quiz-a")
        self.assertEqual(self.browser.chapter_kind(), "mixed")
        solver, observed = self.solver(("quiz-a",))
        original_wait = self.browser._wait
        waits = 0
        loading = True

        def wait(seconds):
            nonlocal waits, loading
            if loading:
                metrics = self.metrics()
                loading = not metrics["tasks"]["quiz-a"]["ready"]
                if loading:
                    waits += 1
                    if not metrics["tasks"]["quiz-a"].get("deferredStarted"):
                        self.browser.page.evaluate("window.startDeferredInlineQuiz()")
                    self.assertEqual(metrics["tasks"]["video"]["playClicks"], 0)
            original_wait(seconds)

        with patch.object(self.browser, "_wait", side_effect=wait):
            completed = Workflow(self.browser, solver, self.settings, self.stop, self.logs.append).run()
        self.assertGreaterEqual(waits, 1, "必须等待尚未加载的上方测验，不能直接播放后方视频")
        self.assertEqual(completed, 1)
        self.assertEqual(observed, ["quiz-a"])
        metrics = self.metrics()
        self.assertEqual(metrics["completed"], ["quiz-a", "video", "doc-b"])
        self.assertEqual(metrics["violations"], [])
        self.assertEqual(metrics["nextClicks"], 1)

    def test_quiz_frame_navigating_to_submission_result_does_not_repeat_or_skip_later_tasks(self):
        from gaybar_tools.workflow import Workflow

        self.launch_inline("reload=quiz-a")
        solver, observed = self.solver(("quiz-a", "quiz-b"))
        completed = Workflow(self.browser, solver, self.settings, self.stop, self.logs.append).run()
        self.assertEqual(completed, 1)
        self.assertEqual(observed, ["quiz-a", "quiz-b"])
        metrics = self.metrics()
        self.assertTrue(metrics["tasks"]["quiz-a"]["resultReloaded"])
        self.assertEqual(metrics["completed"], metrics["expected"])
        self.assertEqual(metrics["violations"], [])
        self.assertEqual(metrics["nextClicks"], 1)
        for task_id in ("quiz-a", "quiz-b"):
            self.assertEqual(metrics["tasks"][task_id]["answerClicks"], 1)
            self.assertEqual(metrics["tasks"][task_id]["submitClicks"], 1)
            self.assertEqual(metrics["tasks"][task_id]["confirmClicks"], 1)

    def test_only_unpublished_quiz_pending_is_ready_and_skips_to_next_without_model(self):
        from gaybar_tools.workflow import Workflow

        self.launch_inline("all-completed=1&unpublished=quiz-a")
        self.assertEqual(self.browser.prepare_start(), "mixed")
        signature, ready = self.browser._view_signature("mixed")
        self.assertTrue(signature)
        self.assertTrue(ready, "未发布测验可跳过，不能将已完成的整页误判为尚未就绪")
        pending = [task for task in self.browser.content_tasks() if task.required and not task.completed]
        self.assertEqual([(task.kind, task.placeholder) for task in pending], [("quiz", True)])
        solver = Mock()
        solver.solve.side_effect = AssertionError("未发布或已完成任务不得调用模型")
        completed = Workflow(self.browser, solver, self.settings, self.stop, self.logs.append).run()
        self.assertEqual(completed, 1)
        solver.solve.assert_not_called()
        metrics = self.metrics()
        self.assertEqual(metrics["completed"], [])
        self.assertEqual(metrics["nextClicks"], 1)
        self.assertEqual(metrics["nextTrusted"], [True])
        self.assertEqual(metrics["violations"], [])
        for task in metrics["tasks"].values():
            self.assertEqual(task["wheels"], 0)
            self.assertEqual(task["playClicks"], 0)
            self.assertEqual(task["answerClicks"], 0)
            self.assertEqual(task["submitClicks"], 0)

    def test_sibling_confirmation_is_ignored_while_current_quiz_confirmation_is_delayed(self):
        metrics, kinds = self.run_inline("sibling-dialog=1")
        self.assertEqual(kinds, ["document", "quiz", "video", "quiz", "document"])
        self.assertEqual(metrics["decoyClicks"], 0, "当前测验提交不能点击兄弟框架的确认按钮")


if __name__ == "__main__":
    unittest.main()
