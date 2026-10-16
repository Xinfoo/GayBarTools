"""Real wheel regressions for nested document readers, using local pages only."""

import importlib.util
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import test_browser_integration as fixtures

from gaybar_tools.browser import BrowserError


WHEEL_RECORDER = """
window.wheelEvents = [];
addEventListener('wheel', event => {
  window.wheelEvents.push({trusted: event.isTrusted, x: event.clientX, y: event.clientY});
}, {capture: true, passive: true});
"""


def course_page(mode):
    overlay = "<div id='blocking-overlay'>请先关闭此提示</div>" if mode == "blocked" else ""
    hidden_reader = ("<iframe hidden name='hidden-document' "
                     "src='/document-reader.html?mode=window'></iframe>") if mode == "wrapper" else ""
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>
      * {{box-sizing:border-box}} body {{margin:0;min-height:1800px;background:#eef1f4}}
      header {{position:fixed;inset:0 0 auto;height:112px;z-index:10;background:#fff;padding:25px}}
      #course-frame {{position:absolute;left:28px;top:144px;width:calc(100% - 360px);
        height:calc(100vh - 200px);border:0;background:white}}
      #catalog {{position:fixed;right:20px;top:144px;width:270px;height:calc(100vh - 200px);
        overflow:auto;background:#fff;padding:16px}}
      #catalog section {{height:120px}}
      #blocking-overlay {{position:fixed;left:28px;top:144px;width:calc(100% - 360px);
        height:calc(100vh - 200px);background:#ddd;z-index:20}}
    </style></head><body>
      <header>静态章节阅读 <button>1 学习内容</button><button>2 学习检测</button></header>
      <iframe id='course-frame' name='course-frame' src='/course-shell.html?mode={mode}'></iframe>
      {hidden_reader}
      <aside id='catalog'>{''.join('<section>课程目录 %d</section>' % n for n in range(20))}</aside>
      {overlay}
      <script>{WHEEL_RECORDER}
        window.catalogWheelEvents = 0;
        document.querySelector('#catalog').addEventListener('wheel', () => ++window.catalogWheelEvents,
          {{passive:true}});
      </script>
    </body></html>"""


def course_shell(mode, *, document_origin=""):
    document_path = "/content-render.html" if mode in {"unknown", "plugin"} else "/document-reader.html"
    reader_style = "#reader{display:flex;flex-direction:column}iframe{flex:1;min-height:0;height:0}" if mode == "wrapper" else ""
    introduction = "<p>" + "课程附件正文如下，请阅读嵌套阅读器中的资料。" * 8 + "</p>" if mode == "wrapper" else ""
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>
      html,body {{height:100%;margin:0;overflow:hidden}}
      #reader {{position:absolute;inset:24px 20px;overflow:hidden}}
      iframe {{display:block;width:100%;height:100%;border:0}}
      {reader_style}
    </style></head><body>
      <div>静态资料</div>
      <div id='reader'>{introduction}<iframe name='document-frame' src='{document_origin}{document_path}?mode={mode}'></iframe></div>
    </body></html>"""


def document_page(mode):
    paragraphs = "".join(
        f"<section><h2>阅读内容 {n + 1}</h2><p>{'课程资料的真实可滚动正文。' * 15}</p></section>"
        for n in range(7)
    )
    script = WHEEL_RECORDER
    if mode in {"window", "blocked", "unknown", "wrapper"}:
        # The body's box moves above its viewport after genuine window scroll.
        # Unknown URLs deliberately inherit document meaning from the parent reader.
        body_class = "" if mode in {"unknown", "wrapper"} else "class='document-content'"
        style = "body{margin:0;padding:20px}section{height:240px}"
        content = paragraphs
        if mode == "wrapper":
            content = (f"<div class='document-content'>{content}</div>"
                       "<iframe hidden name='hidden-child-document' "
                       "src='/document-reader.html?mode=window'></iframe>")
    elif mode == "canvas":
        body_class = ""
        style = "body{margin:0}canvas{display:block;width:800px;max-width:100%;height:1700px}"
        # Canvas/image readers can have no body text and a full-height body box.
        # Only the actual document.scrollingElement exposes their scroll range.
        content = "<canvas width='800' height='1700' aria-label='静态文档图像'></canvas>"
    elif mode == "lazy":
        body_class = "class='document-content'"
        style = "body{margin:0;padding:20px}section{height:250px}"
        content = paragraphs[:paragraphs.index("<section><h2>阅读内容 5")]
        script += """
          window.lazyAppendCount = 0;
          addEventListener('scroll', () => {
            const root = document.scrollingElement;
            if (window.lazyAppendCount || root.scrollTop + root.clientHeight < root.scrollHeight - 80) return;
            window.lazyAppendCount = 1;
            setTimeout(() => {
              for (let n=0;n<4;n++) {
                const section=document.createElement('section');
                section.textContent='延迟加载的阅读内容 '+n;
                document.body.append(section);
              }
              window.lazyAppendCount = 2;
            }, 180);
          }, {passive:true});
        """
    elif mode in {"wide", "clipped"}:
        body_class = ""
        style = """
          html,body{height:100%;margin:0;overflow:hidden}
          section{height:240px}
          #reader{width:2400px;height:calc(100vh - 40px);overflow:auto;margin:20px}
        """
        content = f"<div id='reader'>{paragraphs}</div>"
        if mode == "clipped":
            style += """
              .document-content{width:560px;height:400px;overflow:hidden;margin:20px}
              #reader{width:1800px;height:560px;margin:0}
            """
            content = f"<div class='document-content'>{content}</div>"
    elif mode == "plugin":
        body_class = ""
        style = "html,body{height:100%;margin:0;overflow:hidden}embed{width:100%;height:100%}"
        content = "<embed type='application/x-unavailable-course-document'>"
    else:
        raise ValueError(mode)
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>{style}</style></head>
      <body {body_class}>{content}<script>{script}</script></body></html>"""


class DocumentHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        parsed = urlsplit(self.path)
        mode = parse_qs(parsed.query).get("mode", ["window"])[0]
        pages = {
            "/chapter.html": course_page,
            "/course-shell.html": course_shell,
            "/document-reader.html": document_page,
            "/content-render.html": document_page,
        }
        renderer = pages.get(parsed.path)
        if renderer is None:
            self.send_error(404)
            return
        if renderer is course_shell and mode == "wide":
            # The same loopback server under another hostname supplies a real
            # cross-origin document without relying on external network access.
            html = course_shell(mode, document_origin=f"http://localhost:{self.server.server_port}")
        else:
            html = renderer(mode)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright 未安装，跳过浏览器集成测试")
class DocumentScrollIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gaybar_tools.browser import BrowserSession, StopRequested

        cls.BrowserSession, cls.StopRequested = BrowserSession, StopRequested
        cls.executable = fixtures.browser_executable()
        if not cls.executable:
            raise unittest.SkipTest("未找到浏览器；请安装浏览器或设置 GAYBARTOOLS_BROWSER_PATH")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), DocumentHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=3)

    def setUp(self):
        fixtures.BrowserIntegrationTests.setUp(self)

    def launch_document(self, mode):
        self.browser.launch(f"{self.base_url}/chapter.html?mode={mode}", headless=True)
        self.browser.page.frame_locator("#course-frame").frame_locator("iframe").locator("body").wait_for()
        self.document = self.browser.page.frame(name="document-frame")
        self.assertIsNotNone(self.document)
        self.document.wait_for_function("Array.isArray(window.wheelEvents)")

    def document_metrics(self, selector="html"):
        return self.document.locator(selector).evaluate("""element => {
          const scroll = element === document.documentElement ? document.scrollingElement : element;
          return {top:scroll.scrollTop,height:scroll.scrollHeight,client:scroll.clientHeight,
            bodyY:document.body.getBoundingClientRect().y,events:window.wheelEvents};
        }""")

    def assert_document_at_bottom(self, selector="html"):
        metrics = self.document_metrics(selector)
        self.assertGreater(metrics["height"], metrics["client"] + 500, metrics)
        self.assertGreater(metrics["top"], 0, metrics)
        self.assertGreaterEqual(metrics["top"] + metrics["client"], metrics["height"] - 3, metrics)
        self.assertTrue(metrics["events"], "内容没有收到真实滚轮事件")
        self.assertTrue(all(event["trusted"] for event in metrics["events"]))
        self.assert_surrounding_page_unchanged()
        return metrics

    def assert_surrounding_page_unchanged(self):
        state = self.browser.page.evaluate("""() => ({
          top:document.scrollingElement.scrollTop,
          catalog:document.querySelector('#catalog').scrollTop,
          catalogWheels:window.catalogWheelEvents
        })""")
        self.assertEqual(state, {"top": 0, "catalog": 0, "catalogWheels": 0},
                         "滚轮应送到课程正文，不能滚动主页面或右侧目录")

    def test_nested_window_scroll_stays_inside_document_after_body_moves_above_header(self):
        self.launch_document("window")
        self.assertEqual(self.document_metrics()["top"], 0)
        self.browser.read_document()
        metrics = self.assert_document_at_bottom()
        self.assertLess(metrics["bodyY"], -500)

    def test_non_scrolling_reader_shell_cannot_complete_before_unknown_iframe_content(self):
        self.launch_document("unknown")
        self.browser.read_document()
        self.assert_document_at_bottom()

    def test_canvas_document_without_text_cannot_complete_from_wrapper_metrics(self):
        self.launch_document("canvas")
        self.assertEqual(self.document.locator("body").inner_text().strip(), "")
        root = self.document.evaluate("""() => ({
          tag:document.scrollingElement.tagName,
          bodyClient:document.body.clientHeight,bodyHeight:document.body.scrollHeight
        })""")
        self.assertEqual(root["tag"], "HTML")
        self.assertEqual(root["bodyClient"], root["bodyHeight"])
        self.browser.read_document()
        self.assert_document_at_bottom()

    def test_full_height_non_scrolling_content_wrapper_cannot_replace_document_root(self):
        self.launch_document("wrapper")
        wrapper = self.document.locator(".document-content")
        before = wrapper.evaluate("element => ({top:element.scrollTop,height:element.scrollHeight,client:element.clientHeight})")
        self.assertEqual(before["top"], 0)
        self.assertEqual(before["height"], before["client"])
        self.browser.read_document()
        self.assert_document_at_bottom()
        self.assertEqual(wrapper.evaluate("element => element.scrollTop"), 0)
        hidden = self.browser.page.frame(name="hidden-document")
        self.assertIsNotNone(hidden)
        self.assertEqual(hidden.evaluate("document.scrollingElement.scrollTop"), 0)
        self.assertEqual(hidden.evaluate("window.wheelEvents"), [])
        hidden_child = self.browser.page.frame(name="hidden-child-document")
        self.assertIsNotNone(hidden_child)
        self.assertEqual(hidden_child.evaluate("document.scrollingElement.scrollTop"), 0)
        self.assertEqual(hidden_child.evaluate("window.wheelEvents"), [])

    def test_reader_wider_than_iframe_receives_wheel_in_visible_content_not_right_catalog(self):
        self.launch_document("wide")
        self.assertNotEqual(urlsplit(self.document.url).hostname, urlsplit(self.browser.page.url).hostname)
        self.browser.read_document()
        self.assert_document_at_bottom("#reader")

    def test_reader_point_respects_an_overflow_hidden_ancestor(self):
        self.launch_document("clipped")
        self.browser.read_document()
        self.assert_document_at_bottom("#reader")

    def test_lazy_document_growth_is_read_before_reporting_the_final_bottom(self):
        self.launch_document("lazy")
        original_height = self.document_metrics()["height"]
        self.browser.read_document()
        metrics = self.assert_document_at_bottom()
        self.assertEqual(self.document.evaluate("window.lazyAppendCount"), 2)
        self.assertGreater(metrics["height"], original_height + 500)

    def test_blocking_overlay_cannot_produce_success(self):
        self.launch_document("blocked")
        with self.assertRaises(BrowserError):
            self.browser.read_document()
        metrics = self.document_metrics()
        self.assertEqual(metrics["top"], 0)
        self.assertEqual(metrics["events"], [])
        self.assert_surrounding_page_unchanged()

    def test_unmeasurable_plugin_inside_reader_shell_cannot_produce_success(self):
        self.launch_document("plugin")
        with self.assertRaises(BrowserError):
            self.browser.read_document()
        self.assert_surrounding_page_unchanged()


if __name__ == "__main__":
    unittest.main()
