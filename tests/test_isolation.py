"""Verify only a child process is confined, using a genuinely writable sibling."""
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from gaybar_tools.browser import BrowserSession
from gaybar_tools.config import AppSettings
from gaybar_tools.isolation import IsolationError, bubblewrap_command, prepare_browser_executable
from gaybar_tools.paths import AppPaths


PROJECT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bwrap"), "需要 Linux 和 bubblewrap")
class LinuxIsolationTests(unittest.TestCase):
    def setUp(self):
        directory = PROJECT / ".runtime" / "isolation-tests"
        directory.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="case-", dir=directory)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "课 程's ; $(must-not-run)"
        self.root.mkdir()
        self.paths = AppPaths.for_root(self.root).prepare()

    def execute(self, launcher, script, *arguments):
        return subprocess.run([launcher, "-I", "-B", "-c", script, *map(str, arguments)],
                              capture_output=True, text=True, timeout=12,
                              env=self.paths.environment(), cwd=self.root)

    def test_quoted_launcher_keeps_home_arguments_and_only_two_writable_binds(self):
        command = bubblewrap_command(sys.executable, self.paths)
        writable = [(command[index + 1], command[index + 2])
                    for index, argument in enumerate(command) if argument == "--bind"]
        self.assertEqual(writable, [(str(self.root), str(self.root)), ("/tmp", "/tmp")])
        self.assertIn("--unshare-pid", command)
        self.assertIn("--dev", command)
        launcher = prepare_browser_executable(sys.executable, self.paths)
        self.assertTrue(Path(launcher).is_relative_to(self.paths.runtime))
        self.assertIn('"$@"', Path(launcher).read_text())
        argument = "字 符's ; $(not-executed)"
        result = self.execute(launcher, "import json,os,sys; print(json.dumps([os.environ.get('HOME'),sys.argv[1]]))", argument)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [os.environ.get("HOME"), argument])

    def test_external_writes_and_symlink_escape_fail_but_project_and_tmp_work(self):
        if self.base.is_relative_to(Path("/tmp")):
            self.skipTest("项目本身位于 /tmp，无法创建不在全局 /tmp 可写范围内的测试文件")
        outside = self.base / "existing-external.txt"
        outside.write_text("unchanged")
        link = self.root / "link-to-external.txt"
        link.symlink_to(outside)
        launcher = prepare_browser_executable(sys.executable, self.paths)
        script = """import errno,json,pathlib,sys,tempfile
root,outside,link=map(pathlib.Path,sys.argv[1:])
(root/'allowed.txt').write_text('allowed')
with tempfile.TemporaryFile(dir='/tmp') as stream: stream.write(b'allowed')
blocked=[]
for path in [outside,link,outside.with_name('new-external.txt')]:
    try: path.write_text('must not write')
    except OSError as error: blocked.append(error.errno)
    else: raise SystemExit('external write unexpectedly succeeded')
try: outside.unlink()
except OSError as error: blocked.append(error.errno)
else: raise SystemExit('external delete unexpectedly succeeded')
print(json.dumps(blocked))
"""
        result = self.execute(launcher, script, self.root, outside, link)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [errno.EROFS] * 4)
        self.assertEqual(outside.read_text(), "unchanged")
        self.assertFalse(outside.with_name("new-external.txt").exists())
        self.assertEqual((self.root / "allowed.txt").read_text(), "allowed")
        # The test runner itself was never sandboxed.
        outside.write_text("parent remains writable")
        self.assertEqual(outside.read_text(), "parent remains writable")

    def test_missing_tool_fails_closed(self):
        with patch("gaybar_tools.isolation.shutil.which", return_value=None):
            with self.assertRaisesRegex(IsolationError, "未找到"):
                prepare_browser_executable(sys.executable, self.paths)

    def test_failed_namespace_probe_fails_closed(self):
        failed = SimpleNamespace(returncode=1, stderr="user namespace disabled", stdout="")
        with patch("gaybar_tools.isolation.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(IsolationError, "浏览器未启动"):
                prepare_browser_executable(sys.executable, self.paths)

    def test_launcher_creation_ignores_preexisting_temporary_symlinks(self):
        first = Path(prepare_browser_executable(sys.executable, self.paths))
        external = self.base / "must-not-truncate.txt"
        external.write_text("unchanged")
        predictable = first.parent / f".{first.name}.{os.getpid()}.tmp"
        predictable.symlink_to(external)
        second = Path(prepare_browser_executable(sys.executable, self.paths))
        self.assertEqual(external.read_text(), "unchanged")
        self.assertEqual(first, second)
        self.assertFalse(second.is_symlink())

    def test_long_project_uses_private_short_tmp_and_cleans_on_close_failure(self):
        deep = self.root / ("long-project-" * 12)
        deep.mkdir()
        paths = AppPaths.for_root(deep).prepare()
        session = BrowserSession(AppSettings(), paths, lambda _message: None, threading.Event())
        self.addCleanup(session.close)
        env = session._browser_env()
        temporary = Path(env["TMPDIR"])
        self.assertTrue(str(temporary).startswith("/tmp/gbt-"))
        self.assertLess(len(str(temporary)), 30)
        self.assertEqual(temporary.stat().st_mode & 0o777, 0o700)
        self.assertEqual(env["TMP"], env["TMPDIR"])
        self.assertEqual(env["TEMP"], env["TMPDIR"])
        self.assertEqual(env["HOME"], os.environ["HOME"])
        self.assertEqual(env["XDG_CACHE_HOME"], str(paths.cache))
        session._context = Mock()
        session._context.close.side_effect = RuntimeError("simulated close failure")
        with self.assertRaisesRegex(RuntimeError, "simulated close failure"):
            session.close()
        self.assertFalse(temporary.exists())


if __name__ == "__main__":
    unittest.main()
