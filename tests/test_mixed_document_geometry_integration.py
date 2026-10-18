"""Native reading in a lower PDF task with viewport-propagated body overflow."""

import importlib.util
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import test_browser_integration as fixtures


def mixed_page(mode):
    root_style = 'html,body{height:100%}body{overflow:auto}' if mode == 'root-body' else ''
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>
      {root_style}
    </style></head><body>{mixed_page_body(mode)}</body></html>"""


def mixed_page_body(mode):
    return """<style>
      body{margin:0;background:#eef1f4}header{height:92px;padding:18px;box-sizing:border-box}
      #course-frame{display:block;width:900px;height:2100px;margin-left:28px;border:0}
      aside{position:fixed;left:960px;top:110px;width:220px;height:500px;overflow:auto}
      aside div{height:1600px}
    </style><header>一个已完成视频和一个未完成文档</header>
      <iframe id='course-frame' name='course-frame' src='/knowledge/cards?mode=MODE'></iframe>
      <aside class='catalog'><div>课程目录</div></aside>
      <script>
        window.readerWheels=[];window.videoPlays=0;window.nextClicks=0;window.catalogWheels=0;
        window.violations=[];window.readerBottom=false;window.readerMetrics=null;
        window.saveGeometry=()=>localStorage.setItem('mixed-document-geometry',JSON.stringify({
          wheels:window.readerWheels,plays:window.videoPlays,next:window.nextClicks,
          nextTrusted:window.nextTrusted,catalogWheels:window.catalogWheels,
          catalogTop:document.querySelector('aside').scrollTop,
          violations:window.violations,bottom:window.readerBottom,reader:window.readerMetrics
        }));
        document.querySelector('aside').addEventListener('wheel',()=>{
          ++window.catalogWheels;window.saveGeometry();
        },{passive:true});
      </script>
    """.replace('MODE', mode)


def cards_page(mode):
    return """<!doctype html><html><head><meta charset='utf-8'><style>
      body{margin:0;background:white}.ans-attach-ct{margin:20px;width:840px}
      .ans-job-icon{display:block;width:130px;height:26px}
      video{display:block;width:840px;height:900px;background:#111}
      .insertdoc-online-pdf{display:block;width:840px;height:620px;border:0}
      nav{margin:24px}
    </style></head><body>
      <div class='ans-attach-ct ans-job-finished videoContainer'>
        <div class='ans-job-icon ans-job-video' aria-label='任务点已完成'>任务点已完成</div>
        <video controls preload='none'></video>
      </div>
      <div class='ans-attach-ct'>
        <div class='ans-job-icon ans-job-icon-doc' aria-label='任务点未完成'>任务点</div>
        <iframe class='ans-attach-online insertdoc-online-pdf' name='pdf-shell'
          src='/ananas/modules/pdf/index.html?mode=MODE'></iframe>
      </div>
      <nav><button id='next-chapter'>下一节</button></nav>
      <script>
        document.querySelector('video').addEventListener('play',()=>{++top.videoPlays;top.saveGeometry();});
        document.querySelector('#next-chapter').addEventListener('click',event=>{
          if(!top.readerBottom){top.violations.push('next-before-document-bottom');top.saveGeometry();return;}
          ++top.nextClicks;top.nextTrusted=event.isTrusted;top.saveGeometry();top.location.href='/complete.html';
        });
      </script>
    </body></html>""".replace('MODE', mode)


def pdf_shell_page(mode):
    # BODY's overflow is propagated to the viewport when HTML has visible
    # overflow. Its zero-height own box does not clip the positioned reader.
    style = 'html,body{height:100%}' if mode == 'root-body' else ''
    if mode == 'contained-body':
        style = 'body{position:relative;height:12px;contain:paint}#docContainer{inset:auto;top:0;left:0;width:100%;height:620px}'
    return """<!doctype html><html><head><meta charset='utf-8'><style>
      html{margin:0}body{margin:0;overflow:hidden}
      STYLE
      #docContainer{position:absolute;inset:0}
      #panView{display:block;width:100%;height:100%;border:0}
    </style></head><body><div id='docContainer'>
      <iframe id='panView' name='pdf-reader' src='/screen/v2/file_local'></iframe>
    </div></body></html>""".replace('STYLE', style)


def pdf_reader_page(_mode):
    return """<!doctype html><html><head><meta charset='utf-8'><style>
      body{margin:0;background:#beeaf5}section{height:520px;padding:24px;box-sizing:border-box}
    </style></head><body>
      <section>静态资料第一面</section><section>静态资料第二面</section>
      <section>静态资料第三面</section><section>静态资料最后一面</section>
      <script>
        addEventListener('wheel',event=>{
          top.readerWheels.push({trusted:event.isTrusted,x:event.clientX,y:event.clientY});
          top.saveGeometry();
        },{capture:true,passive:true});
        addEventListener('scroll',()=>{
          const r=document.scrollingElement;
          top.readerMetrics={top:r.scrollTop,height:r.scrollHeight,client:r.clientHeight};
          top.readerBottom=r.scrollTop+r.clientHeight>=r.scrollHeight-3;top.saveGeometry();
        },{passive:true});
      </script>
    </body></html>"""


class MixedDocumentGeometryHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        pages = {
            '/chapter.html': mixed_page,
            '/knowledge/cards': cards_page,
            '/ananas/modules/pdf/index.html': pdf_shell_page,
            '/screen/v2/file_local': pdf_reader_page,
            '/complete.html': lambda _mode: '<!doctype html><h1 data-testid="course-complete">课程已完成</h1>',
        }
        parsed = urlsplit(self.path)
        renderer = pages.get(parsed.path)
        if renderer is None:
            self.send_error(404)
            return
        mode = parse_qs(parsed.query).get('mode', ['zero-shell'])[0]
        body = renderer(mode).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright 未安装')
class MixedDocumentGeometryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gaybar_tools.browser import BrowserSession, StopRequested

        cls.BrowserSession, cls.StopRequested = BrowserSession, StopRequested
        cls.executable = fixtures.browser_executable()
        if not cls.executable:
            raise unittest.SkipTest('未找到 Chromium')
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), MixedDocumentGeometryHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=3)

    setUp = fixtures.BrowserIntegrationTests.setUp

    def launch_tasks(self, mode, *, clipped=False):
        self.browser.launch(f'{self.base_url}/chapter.html?mode={mode}', headless=True)
        self.browser.page.frame_locator('#course-frame').frame_locator(
            '.insertdoc-online-pdf').frame_locator('#panView').locator('section').first.wait_for()
        reader = self.browser.page.frame(name='pdf-reader')
        self.assertIsNotNone(reader)
        reader.locator('section').first.wait_for()
        tasks = self.browser.content_tasks()
        self.assertEqual([(task.kind, task.completed) for task in tasks],
                         [('video', True), ('document', False)])
        shell = self.browser.page.frame(name='pdf-shell')
        self.assertGreater(shell.locator('#panView').evaluate('e => e.clientHeight'), 500)
        if not clipped:
            self.assertTrue(shell.locator('#panView').evaluate('''e => {
              const r=e.getBoundingClientRect();
              return document.elementFromPoint(r.left+r.width/2,r.top+r.height/2)===e;
            }'''))
        return reader, shell

    def assert_unfinished_document_read(self, reader):
        started = []
        count = self.browser.process_content(on_task=lambda kind, ordinal, total: started.append(kind))
        self.assertEqual(count, 1)
        self.assertEqual(started, ['document'])
        metrics = reader.evaluate('''() => ({
          top:document.scrollingElement.scrollTop,height:document.scrollingElement.scrollHeight,
          client:document.scrollingElement.clientHeight
        })''')
        self.assertGreater(metrics['top'], 0)
        self.assertGreaterEqual(metrics['top'] + metrics['client'], metrics['height'] - 3)
        surrounding = self.browser.page.evaluate('''() => ({
          wheels:window.readerWheels,plays:window.videoPlays,next:window.nextClicks,
          catalogWheels:window.catalogWheels,catalogTop:document.querySelector('aside').scrollTop
        })''')
        self.assertTrue(surrounding['wheels'])
        self.assertTrue(all(event['trusted'] for event in surrounding['wheels']))
        self.assertEqual(surrounding['plays'], 0)
        self.assertEqual(surrounding['next'], 0)
        self.assertEqual(surrounding['catalogWheels'], 0)
        self.assertEqual(surrounding['catalogTop'], 0)

    def test_completed_video_does_not_prevent_reading_lower_pdf_with_zero_height_body(self):
        reader, shell = self.launch_tasks('zero-shell')
        self.assertEqual(shell.locator('body').evaluate('e => e.clientHeight'), 0)
        self.assert_unfinished_document_read(reader)

    def test_completed_video_above_pdf_does_not_clip_to_scrolling_outer_body_box(self):
        reader, _shell = self.launch_tasks('root-body')
        before = self.browser.page.evaluate('''() => ({
          scrolling:document.scrollingElement.tagName,
          bodyHeight:document.body.clientHeight,viewportHeight:innerHeight,
          bodyOverflow:getComputedStyle(document.body).overflow,
          htmlOverflow:getComputedStyle(document.documentElement).overflow
        })''')
        self.assertEqual(before['scrolling'], 'HTML')
        self.assertEqual(before['bodyHeight'], before['viewportHeight'])
        self.assertEqual(before['bodyOverflow'], 'auto')
        self.assertEqual(before['htmlOverflow'], 'visible')
        solver = Mock()
        solver.solve.side_effect = AssertionError('混排文档几何回归不调用模型或作答')
        counts = []
        original = self.browser.process_content

        def observe(**kwargs):
            result = original(**kwargs)
            counts.append(result)
            return result

        from gaybar_tools.workflow import Workflow
        with patch.object(self.browser, 'process_content', side_effect=observe):
            pages = Workflow(self.browser, solver, self.settings, self.stop, self.logs.append).run()
        self.assertEqual(pages, 1)
        self.assertEqual(counts, [1])
        solver.solve.assert_not_called()
        self.assertEqual(self.browser.current_url, f'{self.base_url}/complete.html')
        state = self.browser.page.evaluate("JSON.parse(localStorage.getItem('mixed-document-geometry'))")
        self.assertTrue(state['wheels'])
        self.assertTrue(all(event['trusted'] for event in state['wheels']))
        self.assertTrue(state['bottom'])
        self.assertGreater(state['reader']['top'], 0)
        self.assertGreaterEqual(state['reader']['top'] + state['reader']['client'], state['reader']['height'] - 3)
        self.assertEqual(state['plays'], 0)
        self.assertEqual(state['next'], 1)
        self.assertTrue(state['nextTrusted'])
        self.assertEqual(state['catalogWheels'], 0)
        self.assertEqual(state['catalogTop'], 0)
        self.assertEqual(state['violations'], [])

    def test_paint_containment_preserves_real_body_clip_and_sends_no_wheel(self):
        from gaybar_tools.browser import BrowserError

        _reader, shell = self.launch_tasks('contained-body', clipped=True)
        self.assertEqual(shell.locator('body').evaluate('e => e.clientHeight'), 12)
        self.assertEqual(shell.locator('body').evaluate('e => getComputedStyle(e).contain'), 'paint')
        with self.assertRaisesRegex(BrowserError, '被裁剪或不可见'):
            self.browser.process_content()
        self.assertEqual(self.browser.page.evaluate('window.readerWheels'), [])
        self.assertEqual(self.browser.page.evaluate('window.nextClicks'), 0)


if __name__ == '__main__':
    unittest.main()
