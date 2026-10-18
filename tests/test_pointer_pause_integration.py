"""Real pointer-boundary events against an account-free, early-listener player."""

import functools
import importlib.util
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import test_browser_integration as integration


_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<script>
window.metrics = {boundary: [], internal: [], plays: [], pauses: [], ended: false,
                  seekEvents: 0, rateChanges: 0, startedAt: 0, elapsed: 0};
for (const owner of [window, document]) {
  for (const type of ['mouseout', 'mouseleave', 'pointerout', 'pointerleave']) {
    for (const capture of [true, false]) {
      owner.addEventListener(type, event => {
        const record = {type, capture, owner: owner === window ? 'window' : 'document',
                        trusted: event.isTrusted, related: event.relatedTarget !== null};
        if (event.relatedTarget === null) {
          metrics.boundary.push(record);
          const video = document.querySelector('video');
          if (video && !video.paused && !video.ended) video.pause();
        } else {
          metrics.internal.push(record);
        }
      }, capture);
    }
  }
}
window.baseline = {
  addListener: EventTarget.prototype.addEventListener.toString(),
  removeListener: EventTarget.prototype.removeEventListener.toString(),
  dispatch: EventTarget.prototype.dispatchEvent.toString(),
  play: HTMLMediaElement.prototype.play.toString(),
  pause: HTMLMediaElement.prototype.pause.toString(),
  hidden: Object.getOwnPropertyDescriptor(Document.prototype, 'hidden').get.toString(),
  visibility: Object.getOwnPropertyDescriptor(Document.prototype, 'visibilityState').get.toString()
};
</script><style>
html,body {margin:0;min-height:100%;background:#fff;color:#111}
#activity {width:640px;margin:60px auto}
video {width:640px;height:360px;background:#000}
#player {position:relative}
.vjs-big-play-button {position:absolute;left:260px;top:140px;width:120px;height:60px}
#left,#right {display:inline-block;width:300px;height:60px;background:#eee}
iframe {width:1100px;height:720px;border:0}
</style></head><body>
<main id="activity"><h1>Local video activity</h1>
<div id="player" class="video-js"><video id="video_html5_api" class="vjs-tech"
  controls muted preload="auto" src="/lesson.webm"></video>
  <button class="vjs-big-play-button" type="button">Play</button></div>
<div id="left">First local hover target</div><div id="right">Second local hover target</div>
</main>
<script>
const video = document.querySelector('video');
document.querySelector('.vjs-big-play-button').addEventListener('click', () => {
  video.play();
});
video.addEventListener('play', event => {
  metrics.plays.push(event.isTrusted);
  if (!metrics.startedAt) metrics.startedAt = performance.now();
});
video.addEventListener('pause', event => metrics.pauses.push(event.isTrusted));
video.addEventListener('seeking', () => metrics.seekEvents++);
video.addEventListener('ratechange', () => metrics.rateChanges++);
video.addEventListener('ended', () => {
  metrics.ended = true;
  metrics.elapsed = (performance.now() - metrics.startedAt) / 1000;
});
</script></body></html>"""

_OUTER = """<!doctype html><html><head><meta charset="utf-8"><script>
window.outerBoundary = [];
for (const owner of [window, document]) {
  for (const type of ['mouseout', 'mouseleave', 'pointerout', 'pointerleave']) {
    owner.addEventListener(type, event => {
      if (event.relatedTarget !== null) return;
      outerBoundary.push({type, trusted: event.isTrusted});
      const video = document.querySelector('iframe')?.contentDocument?.querySelector('video');
      if (video && !video.paused && !video.ended) video.pause();
    }, true);
  }
}
</script></head>
<body style="margin:0"><h1>Local chapter shell</h1>
<iframe title="Local player" style="width:1100px;height:800px;border:0"
 src="/ananas/modules/video/index.html"></iframe></body></html>"""


class PointerHandler(integration.QuietHandler):
    def do_GET(self):
        route = urlparse(self.path)
        if route.path in ("/mycourse/studentstudy", "/ordinary.html", "/login.html",
                          "/ananas/modules/video/index.html"):
            nested = parse_qs(route.query).get("nested") == ["1"]
            body = (_OUTER if nested else _PAGE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright 未安装")
class PointerPauseIntegrationTests(unittest.TestCase):
    setUp = integration.BrowserIntegrationTests.setUp

    @classmethod
    def setUpClass(cls):
        from gaybar_tools.browser import BrowserSession, StopRequested

        cls.BrowserSession, cls.StopRequested = BrowserSession, StopRequested
        cls.executable = integration.browser_executable()
        if not cls.executable:
            raise unittest.SkipTest("未找到 Chromium")
        handler = functools.partial(PointerHandler, directory=str(integration.DEMO))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=3)

    def launch(self, path="mycourse/studentstudy?chapterId=local-pointer&courseId=local"):
        self.browser.launch(f"{self.base_url}/{path}", headless=True)
        self.select_player()

    def select_player(self):
        for frame in self.browser.page.frames:
            if frame.locator("video").count():
                self.frame = frame
                self.video = frame.locator("video")
                self.video.wait_for(state="visible")
                break
        else:
            self.fail("本地页面未挂载播放器")

    def metrics(self):
        return self.frame.evaluate("JSON.parse(JSON.stringify(metrics))")

    def move_in(self):
        box = self.video.bounding_box()
        self.assertIsNotNone(box)
        self.browser.page.mouse.move(box["x"] + 100, box["y"] + 100)

    def leave_page(self):
        width = self.browser.page.viewport_size["width"]
        self.browser.page.mouse.move(width + 100, 200)
        self.browser.page.wait_for_timeout(80)

    def start_playback(self):
        self.move_in()
        self.frame.locator(".vjs-big-play-button").click()
        self.frame.wait_for_function("!document.querySelector('video').paused")

    def assert_native_interfaces(self):
        baseline = self.frame.evaluate("baseline")
        for name, source in baseline.items():
            self.assertIn("[native code]", source, name)
        self.assertTrue(self.frame.evaluate("!document.hidden"))
        self.assertEqual(self.frame.evaluate("document.visibilityState"), "visible")

    def test_ordinary_page_keeps_native_boundary_event_and_pause(self):
        self.launch("ordinary.html")
        self.start_playback()
        self.leave_page()
        metrics = self.metrics()
        self.assertTrue(metrics["boundary"], "负向用例须由真实移出视口触发站点暂停")
        self.assertTrue(all(event["trusted"] for event in metrics["boundary"]))
        self.assertTrue(self.video.evaluate("video => video.paused"))
        self.assert_native_interfaces()

    def test_chapter_suppresses_early_boundary_handlers_without_spoofing_events(self):
        self.launch()
        self.watch_with_native_leave()
        self.assertEqual(self.metrics()["boundary"], [], "播放期间离开页面不应触发站点暂停监听器")
        self.assertTrue(self.metrics()["ended"])
        self.assert_native_interfaces()

        self.start_playback()
        self.leave_page()
        self.assertTrue(self.metrics()["boundary"], "等待视频结束后必须恢复正常边界事件")
        self.assertTrue(self.video.evaluate("video => video.paused"))

    def watch_with_native_leave(self, internal_hover=False):
        original_wait = self.browser._wait
        moved = []

        def leave_while_playing(seconds):
            if not moved and self.video.evaluate("video => !video.paused && !video.ended"):
                moved.append(True)
                if internal_hover:
                    before = len(self.metrics()["internal"])
                    self.frame.locator("#left").hover()
                    self.frame.locator("#right").hover()
                    self.assertGreater(len(self.metrics()["internal"]), before, "页内悬停事件不能一并屏蔽")
                    self.assertTrue(all(event["trusted"] for event in self.metrics()["internal"]))
                self.move_in()
                self.leave_page()
                self.assertEqual(self.metrics()["boundary"], [])
                if self.frame != self.browser.page.main_frame:
                    self.assertEqual(self.browser.page.evaluate("outerBoundary"), [])
                self.assertFalse(self.video.evaluate("video => video.paused"))
            return original_wait(seconds)

        with patch.object(self.browser, "_wait", side_effect=leave_while_playing):
            self.browser.watch_video()
        self.assertEqual(moved, [True], "必须在真实播放期间执行一次移出视口")

    def test_nested_player_preserves_internal_hover_and_natural_playback(self):
        self.launch("mycourse/studentstudy?chapterId=local-pointer&courseId=local&nested=1")
        self.watch_with_native_leave(internal_hover=True)
        metrics = self.metrics()
        self.assertTrue(metrics["ended"])
        self.assertAlmostEqual(self.video.evaluate("v => v.currentTime"),
                               self.video.evaluate("v => v.duration"), places=2)
        self.assertGreaterEqual(metrics["elapsed"] + .05, self.video.evaluate("v => v.duration"))
        self.assertEqual(metrics["seekEvents"], 0)
        self.assertEqual(metrics["rateChanges"], 0)
        self.assertEqual(self.video.evaluate("v => v.playbackRate"), 1)
        self.assert_native_interfaces()

    def test_protection_is_installed_before_early_listeners_after_new_navigation(self):
        self.launch("login.html")
        self.start_playback()
        self.leave_page()
        self.assertTrue(self.metrics()["boundary"])
        self.browser.page.goto(f"{self.base_url}/mycourse/studentstudy?chapterId=second&courseId=local")
        self.select_player()
        self.watch_with_native_leave()
        self.assertEqual(self.metrics()["boundary"], [])
        self.assertTrue(self.metrics()["ended"])

    def test_stop_preserves_manual_pause_and_restores_boundary_events(self):
        self.launch()
        original_wait = self.browser._wait
        paused = []

        def pause_then_stop(seconds):
            if not paused and self.video.evaluate("video => !video.paused && !video.ended"):
                paused.append(True)
                self.video.focus()
                self.browser.page.keyboard.press("Space")
                self.assertTrue(self.video.evaluate("video => video.paused"), "真实暂停控件仍可工作")
                self.stop.set()
            return original_wait(seconds)

        with patch.object(self.browser, "_wait", side_effect=pause_then_stop):
            with self.assertRaises(self.StopRequested):
                self.browser.watch_video()
        self.assertEqual(paused, [True])
        self.assertTrue(self.video.evaluate("video => video.paused"))
        self.move_in()
        self.leave_page()
        self.assertTrue(self.metrics()["boundary"], "停止后必须撤销当前播放保护")

    def test_media_error_restores_boundary_events(self):
        from gaybar_tools.browser import BrowserError

        self.launch()
        self.video.evaluate("video => {video.src='/missing-media.webm'; video.load();}")
        self.frame.wait_for_function("document.querySelector('video').error !== null")
        with self.assertRaisesRegex(BrowserError, "播放器错误"):
            self.browser.watch_video()
        before = len(self.metrics()["boundary"])
        self.move_in()
        self.leave_page()
        self.assertGreater(len(self.metrics()["boundary"]), before, "播放器异常后也须恢复正常边界事件")


if __name__ == "__main__":
    unittest.main()
