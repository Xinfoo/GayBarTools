"""Ignore optional course resources without borrowing a neighbour's task icon."""

import functools
import importlib.util
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse
from unittest.mock import Mock, patch

import test_browser_integration as integration


PAGE = r"""<!doctype html><meta charset="utf-8"><title>Optional resources</title>
<style>
*{box-sizing:border-box}body{margin:0;font:17px sans-serif;background:white}
main{width:980px;margin:24px}.ans-attach-ct{position:relative;margin:25px 0;padding:42px 12px 12px;border:1px solid #ccc}
.ans-job-icon{display:block;position:absolute;top:8px;left:8px;width:140px;height:24px;color:orange}
.ans-job-icon[aria-label='任务点已完成']{color:green}iframe{display:block;width:100%;height:365px;border:0}
.document-viewer{height:320px;overflow:auto;padding:12px;background:#eee}.paper{min-height:1250px;background:white;padding:24px}
.video-js{position:relative;width:640px;height:360px;background:black}.vjs-tech{width:640px;height:360px}
.vjs-big-play-button{position:absolute;top:145px;left:255px;padding:12px 22px;font:inherit}
nav{display:flex;justify-content:flex-end;padding:20px}button{font:inherit;padding:12px 22px}
.catalog{position:fixed;top:10px;right:10px;width:190px;height:100px;background:#eee}
[hidden]{display:none!important}
</style><body><div id="app"></div><script>
const params=new URLSearchParams(location.search),view=params.get('view')||'',id=params.get('id')||'';
const store='non-task-resource-metrics',app=document.getElementById('app');
const local=(view,id)=>'/non_task.html?view='+view+'&id='+id;
if(view==='reader'){
  app.innerHTML='<div class="document-viewer" data-document-viewer tabindex="0"><article class="paper"><h2>阅读资源</h2><p>正文引用“任务点未完成”及“任务点已完成”，这些文字不能用作所属区域的任务标记。</p><p style="margin-top:1000px">文档结束</p></article></div>';
  const reader=app.querySelector('.document-viewer');
  reader.addEventListener('wheel',event=>top.resourceEvent(id,'wheel',{trusted:event.isTrusted}),{passive:true});
  reader.addEventListener('scroll',()=>top.resourceEvent(id,'scroll',{top:reader.scrollTop,client:reader.clientHeight,height:reader.scrollHeight,bottom:reader.scrollTop+reader.clientHeight>=reader.scrollHeight-3}));
  top.resourceReady(id);
}else if(view==='pdf'){
  app.innerHTML='<div id="docContainer"><iframe id="panView"></iframe></div>';
  app.querySelector('iframe').src=local('reader',id);
}else if(view==='video'){
  app.innerHTML='<div class="video-js"><video class="vjs-tech" controls preload="none" src="/lesson.webm"></video><button class="vjs-big-play-button" aria-label="播放视频">播放</button></div>';
  const video=app.querySelector('video'),button=app.querySelector('button');let started=0;
  button.addEventListener('click',event=>{top.resourceEvent(id,'play-click',{trusted:event.isTrusted});video.play().catch(()=>{});});
  video.addEventListener('play',()=>{button.hidden=true;started ||= performance.now();top.resourceEvent(id,'play');});
  video.addEventListener('seeking',()=>top.resourceEvent(id,'seeking'));
  video.addEventListener('ended',()=>top.resourceEvent(id,'ended',{time:video.currentTime,duration:video.duration,elapsed:(performance.now()-started)/1000,rate:video.playbackRate}));
  if(window.frameElement.dataset.localQuiz==='1'){
    app.querySelector('.video-js').insertAdjacentHTML('beforeend','<section class="TiMu" data-question-id="optional-inline-question" style="position:absolute;top:20px;left:20px;width:500px;background:white;padding:15px"><h3 class="Zy_TItle">【单选题】视频内可见插题</h3><div class="options"><label><input type="radio" name="inline" value="A">A. 本地选项一</label><label><input type="radio" name="inline" value="B">B. 本地选项二</label></div></section>');
  }
  top.resourceReady(id);
}else{
  const layout=params.get('layout')||'mixed',cards=new Map();
  const metrics={order:[],tasks:{},events:[],nativeDone:[],violations:[],nextClicks:0,nextTrusted:[]};
  const persist=()=>localStorage.setItem(store,JSON.stringify(metrics));
  app.innerHTML='<main><h1>章节内容</h1><div class="ans-attach-ct" id="outer"><span class="ans-job-icon" aria-label="任务点未完成">任务点</span><div id="cards"></div></div><nav><button id="next-chapter" disabled>下一节</button></nav></main><aside class="catalog"><span class="ans-job-icon" aria-label="任务点未完成">目录任务点</span></aside>';
  function updateNext(){document.getElementById('next-chapter').disabled=metrics.order.some(key=>metrics.tasks[key].required&&!metrics.tasks[key].done);}
  function prior(id,event){
    const unfinished=metrics.order.slice(0,metrics.order.indexOf(id)).filter(key=>metrics.tasks[key].required&&!metrics.tasks[key].done);
    if(unfinished.length)metrics.violations.push({id,event,unfinished});
  }
  function done(id){
    const task=metrics.tasks[id];if(task.done)return;
    prior(id,'complete');task.done=true;metrics.nativeDone.push(id);
    const icon=cards.get(id).querySelector(':scope > .ans-job-icon:not([hidden])');
    if(icon){icon.setAttribute('aria-label','任务点已完成');cards.get(id).classList.add('ans-job-finished');}
    if(layout==='late-marker'&&id==='required-video'){
      const previous=cards.get('late-document'),marker=document.createElement('span');
      marker.className='ans-job-icon';marker.setAttribute('aria-label','任务点未完成');marker.textContent='任务点';previous.prepend(marker);
      metrics.tasks['late-document'].required=true;metrics.events.push({id:'late-document',event:'became-required'});
    }
    updateNext();
  }
  window.resourceReady=id=>{metrics.tasks[id].ready=true;persist();};
  window.resourceEvent=(id,event,detail={})=>{
    const task=metrics.tasks[id];metrics.events.push({id,event,...detail});
    if(event==='wheel'){prior(id,event);task.wheels++;task.wheelTrusted=(task.wheelTrusted??true)&&detail.trusted;}
    else if(event==='scroll'){task.scrolls++;Object.assign(task,detail);if(detail.bottom&&task.wheels)done(id);}
    else if(event==='play-click'){prior(id,event);task.playClicks++;}
    else if(event==='play')task.playEvents++;
    else if(event==='seeking')task.seekEvents++;
    else if(event==='ended'){Object.assign(task,detail);task.ended=true;if(task.playClicks&&task.seekEvents===0&&detail.rate===1)done(id);}
    persist();
  };
  window.resourcesReady=()=>metrics.order.every(key=>metrics.tasks[key].ready);
  function add(kind,id,{required=false,hidden=false,nested=false,empty=false,complete=false,overlay=false}={}){
    const card=document.createElement('div');card.className='ans-attach-ct'+(complete?' ans-job-finished':'');card.dataset.localResourceId=id;
    if(required||hidden)card.innerHTML='<span class="ans-job-icon" aria-label="任务点'+(complete?'已':'未')+'完成"'+(hidden?' hidden':'')+'>任务点</span>';
    const frame=document.createElement('iframe');frame.className='ans-attach-online'+(kind==='video'?' ans-insertvideo-online':' insertdoc-online-pdf');
    frame.dataset.localResourceId=id;
    if(overlay)frame.dataset.localQuiz='1';
    metrics.order.push(id);metrics.tasks[id]={kind,required,ready:empty,done:false,wheels:0,scrolls:0,playClicks:0,playEvents:0,seekEvents:0,ended:false};
    card.appendChild(frame);cards.set(id,card);document.getElementById('cards').appendChild(card);
    if(empty){
      if(kind==='document'){const shell=document.createElement('div');shell.id='docContainer';card.appendChild(shell);shell.appendChild(frame);}
      frame.src='about:blank';
    }else frame.src=local(kind==='video'?'video':nested?'pdf':'reader',id);
  }
  if(layout==='mixed'){
    add('document','optional-document');add('video','required-video',{required:true});
    add('video','hidden-marker-video',{required:true,hidden:true,complete:true});
    add('document','required-document',{required:true});add('video','optional-video');
    add('document','nested-optional-document',{nested:true});
  }else if(layout==='all-optional'){
    add('document','optional-document');add('video','optional-video');
    add('document','nested-optional-document',{nested:true});
    add('document','empty-document',{empty:true});add('video','empty-video',{empty:true});
  }else if(layout==='document-pair'){
    add('document','optional-document',{nested:true});add('document','required-document',{required:true,nested:true});
  }else if(layout==='video-pair'){
    add('video','optional-video');add('video','required-video',{required:true});
  }else if(layout==='late-marker'){
    add('document','late-document');add('video','required-video',{required:true});
  }else if(layout.startsWith('optional-inline-quiz')){
    add('video','optional-video',{overlay:true});
    if(layout.endsWith('chapter-quiz')){
      const assessment=document.createElement('div');assessment.className='ans-attach-ct';
      assessment.innerHTML='<iframe id="chapter-quiz" class="ans-attach-online" src="/quiz.html"></iframe>';
      document.getElementById('cards').appendChild(assessment);
    }
  }
  document.getElementById('next-chapter').addEventListener('click',event=>{
    metrics.nextClicks++;metrics.nextTrusted.push(event.isTrusted);persist();location.href='/complete.html';
  });
  updateNext();persist();
}
</script></body>"""


class ResourceHandler(integration.QuietHandler):
    def do_GET(self):
        if urlparse(self.path).path != "/non_task.html":
            return super().do_GET()
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright 未安装")
class NonTaskResourcesIntegrationTests(unittest.TestCase):
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
        handler = functools.partial(ResourceHandler, directory=str(integration.DEMO))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    def metrics(self):
        return self.browser.page.evaluate("JSON.parse(localStorage.getItem('non-task-resource-metrics'))")

    def launch_resources(self, layout):
        self.launch(f"non_task.html?chapterId=local-resources&courseId=local&layout={layout}")
        self.browser.page.wait_for_function("window.resourcesReady && window.resourcesReady()")
        self.assertIn(self.browser.prepare_start(), {"video", "document"})

    def assert_optional_untouched(self, metrics):
        for resource_id, resource in metrics["tasks"].items():
            if resource["required"]:
                continue
            with self.subTest(resource_id=resource_id):
                self.assertEqual(resource["wheels"], 0)
                self.assertEqual(resource["scrolls"], 0)
                self.assertEqual(resource["playClicks"], 0)
                self.assertEqual(resource["playEvents"], 0)
        self.assertEqual(metrics["violations"], [])

    def run_workflow(self, layout, expected_required, expected_executed):
        from gaybar_tools.workflow import Workflow

        self.launch_resources(layout)
        tasks = self.browser.content_tasks()
        self.assertEqual([task.required for task in tasks], expected_required)
        self.assertEqual(len({task.identity for task in tasks}), len(tasks))
        original_key = self.browser.chapter_key()
        original_process = self.browser.process_content
        counts = []
        self.process_waits = []

        def observe(**kwargs):
            with patch.object(self.browser, "_wait", wraps=self.browser._wait) as waits:
                count = original_process(**kwargs)
            self.process_waits.extend(call.args[0] for call in waits.call_args_list)
            self.assertEqual(self.browser.chapter_key(), original_key)
            self.assertEqual(len(self.browser.content_tasks()), len(tasks))
            counts.append(count)
            return count

        solver = Mock()
        solver.solve.side_effect = AssertionError("资源跳过回归不得调用答题模型")
        with patch.object(self.browser, "process_content", side_effect=observe):
            completed = Workflow(self.browser, solver, self.settings, self.stop, self.logs.append).run()
        solver.solve.assert_not_called()
        self.assertEqual(completed, 1)
        self.assertEqual(counts, [len(expected_executed)])
        self.assertEqual(self.browser.current_url, f"{self.base_url}/complete.html")
        metrics = self.metrics()
        self.assert_optional_untouched(metrics)
        self.assertEqual(metrics["nativeDone"], expected_executed)
        self.assertEqual(metrics["nextClicks"], 1)
        self.assertEqual(metrics["nextTrusted"], [True])
        return metrics

    def test_mixed_resources_skip_missing_icons_and_keep_marked_resources_in_order(self):
        metrics = self.run_workflow(
            "mixed", [False, True, True, True, False, False],
            ["required-video", "hidden-marker-video", "required-document"],
        )
        video, document = metrics["tasks"]["required-video"], metrics["tasks"]["required-document"]
        self.assertEqual(video["playClicks"], 1)
        self.assertTrue(video["ended"])
        self.assertEqual(video["seekEvents"], 0)
        self.assertEqual(video["rate"], 1)
        self.assertGreaterEqual(video["elapsed"] + .05, video["duration"])
        self.assertGreater(document["wheels"], 0)
        self.assertTrue(document["wheelTrusted"])
        self.assertTrue(document["bottom"])
        self.assertEqual(metrics["tasks"]["hidden-marker-video"]["playClicks"], 1)
        self.assertTrue(all(event["trusted"] for event in metrics["events"] if event["event"] == "play-click"))

    def test_all_optional_including_unmounted_attachments_navigate_once_without_loading_wait(self):
        self.run_workflow("all-optional", [False] * 5, [])
        self.assertLess(sum(self.process_waits), self.browser.NEXT_PAGE_TIMEOUT / 2)

    def test_own_marker_added_after_another_task_completes_reopens_skipped_reader(self):
        metrics = self.run_workflow(
            "late-marker", [False, True], ["required-video", "late-document"],
        )
        events = metrics["events"]
        marked_at = next(index for index, event in enumerate(events) if event["event"] == "became-required")
        wheel_at = next(index for index, event in enumerate(events) if event["event"] == "wheel")
        self.assertLess(marked_at, wheel_at)
        self.assertTrue(metrics["tasks"]["late-document"]["bottom"])

    def test_optional_video_inline_question_is_ignored_without_masking_separate_chapter_quiz(self):
        self.run_workflow("optional-inline-quiz", [False], [])
        self.browser.page.goto(
            f"{self.base_url}/non_task.html?chapterId=quiz&courseId=local&layout=optional-inline-quiz-chapter-quiz",
            wait_until="domcontentloaded",
        )
        self.browser.page.wait_for_function("window.resourcesReady && window.resourcesReady()")
        assessment = self.browser.page.frame_locator("#chapter-quiz")
        assessment.locator("#question-1").wait_for(state="visible")

        self.assertEqual(self.browser.prepare_start(), "quiz")
        questions = self.browser.get_questions()

        self.assertEqual(len(questions), 3)
        self.assertNotIn("optional-inline-question", {question.id for question in questions})
        self.assert_optional_untouched(self.metrics())

    def test_direct_read_document_skips_unmarked_nested_reader(self):
        self.launch_resources("document-pair")

        self.browser.read_document()

        metrics = self.metrics()
        self.assert_optional_untouched(metrics)
        self.assertEqual(metrics["nativeDone"], ["required-document"])
        self.assertGreater(metrics["tasks"]["required-document"]["wheels"], 0)
        self.assertEqual(metrics["nextClicks"], 0)

    def test_direct_watch_video_skips_unmarked_player(self):
        self.launch_resources("video-pair")

        self.browser.watch_video()

        metrics = self.metrics()
        self.assert_optional_untouched(metrics)
        self.assertEqual(metrics["nativeDone"], ["required-video"])
        self.assertEqual(metrics["tasks"]["required-video"]["playClicks"], 1)
        self.assertEqual(metrics["nextClicks"], 0)
