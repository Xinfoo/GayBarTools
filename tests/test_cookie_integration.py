"""Check login persistence using a real browser and an account-free local site.

Only synthetic cookies are used. Every browser profile and snapshot lives in a
temporary directory, and the test server never logs request headers or values.
"""

from dataclasses import replace
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

import test_browser_integration as browser_fixtures

from gaybar_tools.config import AppSettings
from gaybar_tools.paths import AppPaths


SESSION = "test_session"
PERSISTENT = "test_persistent"
DOMAIN = "test_domain"
CHAPTER = "test_chapter"
ROOT_COOKIES = {SESSION, PERSISTENT, DOMAIN}


class LoginHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        requested = urlsplit(self.path)
        cookie_headers = []
        if requested.path == "/login":
            account = parse_qs(requested.query).get("account", ["normal"])[0]
            if account not in {"normal", "demo"}:
                self.send_error(400)
                return
            cookie_headers = [
                f"{SESSION}=synthetic-session-{account}; Path=/; HttpOnly; SameSite=Lax",
                f"{PERSISTENT}=synthetic-persistent-{account}; Path=/; Max-Age=86400; HttpOnly; SameSite=Lax",
                f"{DOMAIN}=synthetic-domain; Domain=localhost; Path=/; HttpOnly; SameSite=Lax",
                f"{CHAPTER}=synthetic-chapter; Path=/course; HttpOnly; SameSite=Lax",
            ]
        elif requested.path == "/logout":
            cookie_headers = [
                f"{SESSION}=; Path=/; Max-Age=0",
                f"{PERSISTENT}=; Path=/; Max-Age=0",
                f"{DOMAIN}=; Domain=localhost; Path=/; Max-Age=0",
                f"{CHAPTER}=; Path=/course; Max-Age=0",
            ]

        supplied = SimpleCookie()
        supplied.load(self.headers.get("Cookie", ""))
        account = None
        for candidate in ("normal", "demo"):
            if (supplied.get(SESSION) is not None and supplied.get(PERSISTENT) is not None
                    and supplied[SESSION].value == f"synthetic-session-{candidate}"
                    and supplied[PERSISTENT].value == f"synthetic-persistent-{candidate}"):
                account = candidate
        body = json.dumps({"authenticated": account is not None, "account": account,
                           "cookie_names": sorted(supplied)}).encode("utf-8")
        self.send_response(200)
        for header in cookie_headers:
            self.send_header("Set-Cookie", header)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright 未安装，跳过 Cookie 集成测试")
class CookieIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gaybar_tools.browser import BrowserSession

        cls.BrowserSession = BrowserSession
        cls.executable = browser_fixtures.browser_executable()
        if not cls.executable:
            raise unittest.SkipTest("未找到浏览器；请指定 GAYBARTOOLS_BROWSER_PATH")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), LoginHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://localhost:{cls.server.server_port}"
        cls.other_host_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=3)

    def setUp(self):
        temporary_root = browser_fixtures.PROJECT / ".runtime" / "tmp" if os.name == "nt" else Path("/tmp")
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="gaybar-cookie-test-", dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = AppPaths.for_root(self.root / "normal").prepare()
        self.settings = AppSettings(browser_path=self.executable, headless=True,
                                    action_delay_min=0, action_delay_max=0)
        self.logs = []

    def launch(self, paths=None, path="/state"):
        session = self.BrowserSession(self.settings, paths or self.paths,
                                      self.logs.append, threading.Event())
        self.addCleanup(session.close)
        session.launch(self.base_url + path, headless=True)
        return session

    def state(self, session, path="/state", *, other_host=False):
        origin = self.other_host_url if other_host else self.base_url
        session.page.goto(origin + path, wait_until="domcontentloaded")
        return json.loads(session.page.locator("body").inner_text())

    def snapshot(self, paths=None):
        return (paths or self.paths).profile / "cookies.json"

    def copied_snapshot_paths(self, source=None, name="snapshot-only"):
        target = AppPaths.for_root(self.root / name).prepare()
        shutil.copy2(self.snapshot(source), self.snapshot(target))
        self.assertEqual({entry.name for entry in target.profile.iterdir()}, {"cookies.json"},
                         "恢复测试必须仅依赖 Cookie 快照，不能复制 Chromium 配置或数据库")
        return target

    def wait_for_snapshot(self, session, expected_names):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            session.poll_ready()
            try:
                names = {cookie["name"] for cookie in json.loads(self.snapshot().read_text())["cookies"]}
            except (OSError, ValueError, KeyError):
                names = None
            if names == expected_names:
                return
            time.sleep(0.05)
        self.fail("浏览器空闲时未自动保存预期的 Cookie 状态")

    def assert_logged_in(self, session, account="normal"):
        state = self.state(session)
        self.assertTrue(state["authenticated"], "服务端没有收到恢复后的完整登录 Cookie")
        self.assertEqual(state["account"], account)
        self.assertEqual(set(state["cookie_names"]), ROOT_COOKIES)

    def test_manual_save_restores_session_cookies_and_preserves_http_only_path_and_domain(self):
        session = self.launch(path="/login")
        self.assert_logged_in(session)
        self.assertEqual(session.page.evaluate("document.cookie"), "", "HttpOnly Cookie 不能向页面脚本泄露")
        self.assertEqual(set(self.state(session, "/course/state")["cookie_names"]), ROOT_COOKIES | {CHAPTER})
        self.assertIsNone(session.save_login_state())
        saved = json.loads(self.snapshot().read_text())["cookies"]
        by_name = {cookie["name"]: cookie for cookie in saved}
        self.assertTrue(all(cookie["httpOnly"] for cookie in saved))
        self.assertLess(by_name[SESSION]["expires"], 0, "测试必须包含原生会话 Cookie")
        self.assertGreater(by_name[PERSISTENT]["expires"], time.time())
        self.assertEqual(by_name[CHAPTER]["path"], "/course")
        # Chromium may canonicalize a Domain=localhost cookie without a
        # leading dot because localhost is a special, single-label host.
        self.assertEqual(by_name[DOMAIN]["domain"].lstrip("."), "localhost")
        session.close()

        same_profile = self.launch()
        self.assert_logged_in(same_profile)
        same_profile.close()

        restored = self.launch(self.copied_snapshot_paths())
        self.assert_logged_in(restored)
        self.assertEqual(set(self.state(restored, "/course/state")["cookie_names"]), ROOT_COOKIES | {CHAPTER})
        alternate_host = self.state(restored, other_host=True)
        self.assertFalse(alternate_host["authenticated"])
        self.assertEqual(alternate_host["cookie_names"], [], "Cookie 不得泄露到另一个本地域名")

    def test_idle_checkpoint_survives_a_browser_window_close_without_manual_save(self):
        session = self.launch(path="/login")
        self.assert_logged_in(session)
        self.wait_for_snapshot(session, ROOT_COOKIES | {CHAPTER})
        # Closing the browser context models the native window disappearing,
        # so a subsequent application close cannot query its Cookie store.
        session.page.context.close()
        session.close()
        restored = self.launch(self.copied_snapshot_paths())
        self.assert_logged_in(restored)

    def test_close_saves_login_even_after_stop_was_requested(self):
        session = self.launch(path="/login")
        self.assert_logged_in(session)
        session.stop_event.set()
        session.close()
        restored = self.launch(self.copied_snapshot_paths())
        self.assert_logged_in(restored)

    def test_expired_cookie_from_a_snapshot_is_not_restored(self):
        session = self.launch(path="/login")
        session.save_login_state()
        session.close()
        target = self.copied_snapshot_paths()
        payload = json.loads(self.snapshot(target).read_text())
        for cookie in payload["cookies"]:
            if cookie["name"] == SESSION:
                cookie["expires"] = time.time() - 60
        self.snapshot(target).write_text(json.dumps(payload), encoding="utf-8")
        restored = self.launch(target)
        state = self.state(restored)
        self.assertFalse(state["authenticated"])
        self.assertNotIn(SESSION, state["cookie_names"])
        self.assertIn(PERSISTENT, state["cookie_names"], "过滤过期 Cookie 不应丢弃其余有效 Cookie")

    def test_partitioned_cookie_keeps_first_party_partition_and_scope_after_snapshot_restore(self):
        session = self.launch()
        name = "test_partitioned"
        scope = {"domain": ".example.test", "path": "/learning", "httpOnly": True,
                 "secure": True, "sameSite": "None", "partitionKey": "https://example.test",
                 "_crHasCrossSiteAncestor": False, "expires": int(time.time()) + 3600}
        # The browser API creates a synthetic CHIPS cookie without visiting
        # this reserved test domain or making any third-party network request.
        session.page.context.add_cookies([
            {"name": name, "value": "synthetic-partitioned", **scope},
        ])

        def assert_partition(browser):
            matching = [cookie for cookie in browser.page.context.cookies() if cookie["name"] == name]
            self.assertEqual(len(matching), 1, "分区 Cookie 在真实浏览器中必须保持唯一")
            self.assertEqual({key: matching[0].get(key) for key in scope}, scope,
                             "恢复必须保留原始分区、路径、域名和安全属性")

        assert_partition(session)
        session.save_login_state()
        session.close()
        restored = self.launch(self.copied_snapshot_paths())
        assert_partition(restored)

    def test_website_logout_saves_empty_snapshot_and_does_not_revive_on_relaunch(self):
        session = self.launch(path="/login")
        self.wait_for_snapshot(session, ROOT_COOKIES | {CHAPTER})
        session.page.goto(self.base_url + "/logout", wait_until="domcontentloaded")
        self.assertFalse(self.state(session)["authenticated"])
        self.wait_for_snapshot(session, set())
        session.close()
        self.assertEqual(json.loads(self.snapshot().read_text())["cookies"], [])
        same_profile = self.launch()
        self.assertFalse(self.state(same_profile)["authenticated"])
        same_profile.close()
        restored = self.launch(self.copied_snapshot_paths())
        self.assertEqual(self.state(restored)["cookie_names"], [])

    def test_normal_and_demo_profiles_keep_independent_logins_and_snapshots(self):
        normal = self.launch(path="/login?account=normal")
        normal.close()
        demo_paths = replace(self.paths, profile=self.paths.runtime / "demo-browser-profile").prepare()
        demo = self.launch(demo_paths)
        self.assertFalse(self.state(demo)["authenticated"], "演示会话不能恢复正常模式的登录信息")
        demo.page.goto(self.base_url + "/login?account=demo", wait_until="domcontentloaded")
        self.assert_logged_in(demo, "demo")
        demo.close()
        self.assertTrue(self.snapshot().is_file())
        self.assertTrue(self.snapshot(demo_paths).is_file())
        normal_again = self.launch()
        self.assert_logged_in(normal_again, "normal")
        normal_again.close()
        demo_again = self.launch(demo_paths)
        self.assert_logged_in(demo_again, "demo")


if __name__ == "__main__":
    unittest.main()
