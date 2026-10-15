"""Private Cookie snapshots and worker-thread lifecycle checks with fake jars."""
from dataclasses import replace
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from gaybar_tools.browser import BrowserError, BrowserSession
from gaybar_tools.config import AppSettings
from gaybar_tools.cookies import CookieStore, CookieStoreError, valid_cookies
from gaybar_tools.paths import AppPaths


def cookie(**changes):
    result = {"name": "test-session", "value": "synthetic-value", "domain": ".example.test",
              "path": "/", "expires": -1, "httpOnly": True, "secure": True, "sameSite": "None"}
    result.update(changes)
    return result


class CookieValidationTests(unittest.TestCase):
    def test_session_security_and_partition_attributes_survive(self):
        source = cookie(partitionKey="https://example.test", _crHasCrossSiteAncestor=False,
                        ignored="untrusted extra data")
        expected = {key: value for key, value in source.items() if key != "ignored"}
        self.assertEqual(valid_cookies([source], now=1000), [expected])
        self.assertIn("ignored", source, "validation must not mutate the browser jar")

    def test_invalid_or_expired_entries_are_filtered(self):
        candidates = [None, "not-a-cookie", {}, cookie(name="bad\nname"), cookie(value="bad\0value"),
                      cookie(domain="https://example.test"), cookie(path="relative"),
                      cookie(expires=999), cookie(expires=0), cookie(expires=-2),
                      cookie(expires=True), cookie(expires=float("nan")),
                      cookie(expires=10 ** 1000), cookie(expires=1e100), cookie(secure="yes"),
                      cookie(httpOnly=1), cookie(sameSite="unknown"), cookie(partitionKey={}),
                      cookie(partitionKey="https://example.test", _crHasCrossSiteAncestor="false")]
        self.assertEqual(valid_cookies(candidates, now=1000), [])

    def test_absent_expiry_is_session_and_future_expiry_survives(self):
        session = cookie()
        del session["expires"]
        self.assertEqual(valid_cookies([session], now=1000)[0]["expires"], -1)
        self.assertEqual(valid_cookies([cookie(expires=2000)], now=1000)[0]["expires"], 2000)


class CookieStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gbt-cookie-unit-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.paths = AppPaths.for_root(Path(self.temporary.name) / "project")
        self.store = CookieStore(self.paths)
        self.snapshot = self.paths.profile / "cookies.json"

    def write_snapshot(self, content):
        self.paths.prepare()
        self.snapshot.write_text(content, encoding="utf-8")

    def test_missing_snapshot_does_not_create_directories(self):
        self.assertIsNone(self.store.load())
        self.assertFalse(self.paths.root.exists())

    def test_atomic_round_trip_and_private_permissions(self):
        self.assertTrue(self.store.save([cookie()]))
        self.assertEqual(CookieStore(self.paths).load(), [cookie()])
        self.assertEqual(json.loads(self.snapshot.read_text())["version"], 1)
        self.assertEqual(list(self.paths.profile.glob(".cookies-*.tmp")), [])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.snapshot.stat().st_mode), 0o600)

    def test_unchanged_state_skips_disk_replacement(self):
        self.store.save([cookie(name="b"), cookie(name="a")])
        with patch("gaybar_tools.cookies.os.replace") as replace_file:
            self.assertFalse(self.store.save([cookie(name="a"), cookie(name="b")]))
        replace_file.assert_not_called()

    def test_empty_jar_replaces_old_cookies_after_logout(self):
        self.store.save([cookie()])
        self.assertTrue(self.store.save([]))
        self.assertEqual(CookieStore(self.paths).load(), [])
        self.assertEqual(json.loads(self.snapshot.read_text())["cookies"], [])

    def test_failed_replace_keeps_original_and_removes_temporary(self):
        self.store.save([cookie(value="before")])
        with patch("gaybar_tools.cookies.os.replace", side_effect=OSError("sensitive synthetic detail")):
            with self.assertRaises(CookieStoreError) as caught:
                self.store.save([cookie(value="after")])
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertEqual(CookieStore(self.paths).load()[0]["value"], "before")
        self.assertEqual(list(self.paths.profile.glob(".cookies-*.tmp")), [])

    def test_malformed_or_unsupported_snapshot_is_not_executed(self):
        for content in ("{broken", "[]", '{"version":2,"cookies":[]}',
                        '{"version":true,"cookies":[]}', '{"version":1,"cookies":{}}',
                        '__import__("os").system("false")'):
            with self.subTest(content=content):
                self.write_snapshot(content)
                with self.assertRaisesRegex(CookieStoreError, "损坏或格式不支持"):
                    self.store.load()

    def test_valid_json_drops_expired_and_invalid_cookie_fields(self):
        self.write_snapshot(json.dumps({"version": 1, "cookies": [cookie(), cookie(expires=1),
                                                                  {"instruction": "do nothing"}]}))
        self.assertEqual(self.store.load(), [cookie()])

    def test_size_limit_prevents_unbounded_read(self):
        self.write_snapshot(" " * 1024)
        with patch("gaybar_tools.cookies._MAX_BYTES", 100):
            with self.assertRaisesRegex(CookieStoreError, "损坏或格式不支持"):
                self.store.load()

    @unittest.skipIf(os.name == "nt", "Unix file permissions")
    def test_existing_snapshot_permissions_are_repaired(self):
        self.store.save([cookie()])
        self.snapshot.chmod(0o644)
        self.store.load()
        self.assertEqual(stat.S_IMODE(self.snapshot.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "Unix symlink fixtures")
    def test_cookie_file_symlink_cannot_be_read_or_replaced(self):
        self.paths.prepare()
        external = Path(self.temporary.name) / "outside.json"
        external.write_text("untouched")
        self.snapshot.symlink_to(external)
        with self.assertRaisesRegex(CookieStoreError, "路径不安全"):
            self.store.load()
        with self.assertRaisesRegex(CookieStoreError, "路径不安全"):
            self.store.save([cookie()])
        self.assertEqual(external.read_text(), "untouched")
        self.assertTrue(self.snapshot.is_symlink())

    @unittest.skipIf(os.name == "nt", "Unix symlink fixtures")
    def test_profile_symlink_is_checked_before_creating_parent(self):
        self.paths.runtime.mkdir(parents=True)
        outside = Path(self.temporary.name) / "outside-absent"
        self.paths.profile.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(CookieStoreError, "路径不安全"):
            self.store.save([cookie()])
        with self.assertRaisesRegex(CookieStoreError, "路径不安全"):
            self.store.load()
        self.assertFalse(outside.exists())

    @unittest.skipIf(os.name == "nt", "Unix symlink fixtures")
    def test_runtime_symlink_cannot_create_files_outside_root(self):
        self.paths.root.mkdir(parents=True)
        outside = Path(self.temporary.name) / "outside-runtime"
        self.paths.runtime.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(CookieStoreError, "路径不安全"):
            self.store.save([cookie()])
        self.assertFalse(outside.exists())

    def test_profiles_keep_distinct_jars(self):
        demo_paths = replace(self.paths, profile=self.paths.runtime / "demo-browser-profile")
        self.store.save([cookie(value="real-profile")])
        CookieStore(demo_paths).save([cookie(value="demo-profile")])
        self.assertEqual(self.store.load()[0]["value"], "real-profile")
        self.assertEqual(CookieStore(demo_paths).load()[0]["value"], "demo-profile")


class BrowserCookieLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gbt-cookie-session-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.paths = AppPaths.for_root(Path(self.temporary.name))
        self.messages = []
        self.stop = threading.Event()
        self.session = BrowserSession(AppSettings(), self.paths, self.messages.append, self.stop)
        self.context = Mock()
        self.context.cookies.return_value = [cookie()]
        self.session._context = self.context
        self.session._cookie_state_ready = True
        self.session._owner_thread = threading.get_ident()

    def test_explicit_save_requires_open_context(self):
        self.session._context = None
        with self.assertRaisesRegex(BrowserError, "先打开浏览器"):
            self.session.save_login_state()

    def test_owner_thread_is_required_for_explicit_save(self):
        self.session._owner_thread = -1
        with self.assertRaisesRegex(BrowserError, "同一个工作线程"):
            self.session.save_login_state()
        self.context.cookies.assert_not_called()

    def test_close_saves_session_even_after_stop(self):
        self.stop.set()
        self.session.close()
        self.context.close.assert_called_once()
        self.assertEqual(CookieStore(self.paths).load(), [cookie()])

    def test_wait_checkpoints_when_due_without_rewriting_every_iteration(self):
        with patch("gaybar_tools.browser.time.monotonic", return_value=10):
            self.session._wait(0)
            self.session._wait(0)
        self.context.cookies.assert_called_once()
        self.assertEqual(CookieStore(self.paths).load(), [cookie()])

    def test_cookie_api_failure_never_leaks_its_arguments(self):
        self.context.cookies.side_effect = RuntimeError("synthetic-secret-value")
        with self.assertRaises(BrowserError) as caught:
            self.session.save_login_state()
        self.assertNotIn("synthetic-secret", str(caught.exception))
        self.assertFalse(any("synthetic-secret" in message for message in self.messages))

    def test_invalid_snapshot_can_be_skipped_without_blocking_browser(self):
        self.paths.prepare()
        (self.paths.profile / "cookies.json").write_text("{bad")
        self.session._restore_login_state()
        self.context.add_cookies.assert_not_called()
        self.context.clear_cookies.assert_not_called()
        self.assertTrue(self.session._cookie_state_ready)
        self.assertTrue(any("已跳过恢复" in message for message in self.messages))

    def test_empty_snapshot_clears_old_profile_jar_before_navigation(self):
        CookieStore(self.paths).save([])
        self.session._restore_login_state()
        self.context.clear_cookies.assert_called_once()
        self.context.add_cookies.assert_not_called()

    def fail_restore(self):
        CookieStore(self.paths).save([cookie(value="snapshot")])
        self.context.cookies.return_value = [cookie(value="native-profile")]
        self.context.add_cookies.side_effect = [RuntimeError("synthetic-private-api-detail"), None]
        self.session._restore_login_state()

    def test_failed_restore_rolls_back_native_jar_and_preserves_snapshot_on_close(self):
        self.fail_restore()
        self.assertEqual(self.context.clear_cookies.call_count, 2)
        self.assertEqual(self.context.add_cookies.call_args_list[-1].args,
                         ([cookie(value="native-profile")],))
        self.session.close()
        self.assertEqual(CookieStore(self.paths).load(), [cookie(value="snapshot")])
        self.assertFalse(any("synthetic-private" in message for message in self.messages))

    def test_changed_jar_after_failed_restore_requires_explicit_save(self):
        self.fail_restore()
        self.context.cookies.return_value = [cookie(value="new-login")]
        self.session._checkpoint_cookies(force=True)
        self.assertEqual(CookieStore(self.paths).load(), [cookie(value="snapshot")])
        self.assertTrue(self.session._cookie_restore_failed)
        self.session.save_login_state()
        self.assertEqual(CookieStore(self.paths).load(), [cookie(value="new-login")])
        self.assertFalse(self.session._cookie_restore_failed)

    def test_explicit_save_can_replace_snapshot_after_failed_restore(self):
        self.fail_restore()
        self.session.save_login_state()
        self.assertEqual(CookieStore(self.paths).load(), [cookie(value="native-profile")])
        self.assertFalse(self.session._cookie_restore_failed)

    def test_failed_final_save_still_cleans_up_browser_and_temporary_directory(self):
        driver, temporary = Mock(), Mock()
        self.session._playwright = driver
        self.session._session_tmp = temporary
        self.context.cookies.side_effect = RuntimeError("synthetic-private-api-detail")
        self.stop.set()
        self.session.close()
        self.context.close.assert_called_once()
        driver.stop.assert_called_once()
        temporary.cleanup.assert_called_once()
        self.assertIsNone(self.session._context)
        self.assertFalse(any("synthetic-private" in message for message in self.messages))


if __name__ == "__main__":
    unittest.main()
