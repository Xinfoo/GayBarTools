"""Verify only a child process is confined, using a genuinely writable sibling."""
import errno
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from gaybar_tools.browser import BrowserSession
from gaybar_tools.config import AppSettings
from gaybar_tools.isolation import (IsolationError, _gpu_device_nodes, bubblewrap_command,
                                    prepare_browser_executable)
from gaybar_tools.paths import AppPaths


PROJECT = Path(__file__).resolve().parents[1]


class GpuDeviceDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gbt-gpu-inventory-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.devices = self.base / "dev"
        self.devices.mkdir()
        self.character_nodes = set()
        original_lstat = Path.lstat

        def simulated_lstat(path, *args, **kwargs):
            result = original_lstat(path, *args, **kwargs)
            if path in self.character_nodes and not stat.S_ISLNK(result.st_mode):
                return SimpleNamespace(st_mode=stat.S_IFCHR | 0o600)
            return result

        # Unprivileged tests cannot mknod; only the character-device metadata
        # is simulated. Names, directory traversal, files and symlinks are real.
        self.lstat_patch = patch.object(Path, "lstat", simulated_lstat)
        self.lstat_patch.start()
        self.addCleanup(self.lstat_patch.stop)

    def node(self, name, *, character=True):
        path = self.devices / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        if character:
            self.character_nodes.add(path)
        return path

    def test_only_known_gpu_character_devices_are_selected(self):
        expected = [self.node(name) for name in (
            "dri/card0", "dri/card12", "dri/renderD128", "nvidia0", "nvidia10",
            "nvidiactl", "nvidia-modeset", "nvidia-uvm", "nvidia-uvm-tools",
            "nvidia-caps/nvidia-cap1")]
        for name in ("dri/card0extra", "dri/controlD64", "nvidia0extra", "nvidia-log",
                     "nvidia-caps/other", "sda", "input/event0"):
            self.node(name)
        self.node("dri/renderD129", character=False)
        self.node("nvidia1", character=False)
        (self.devices / "dri/card3").mkdir()
        target = self.node("unrelated-device")
        (self.devices / "nvidia2").symlink_to(target)
        (self.devices / "dri/card4").symlink_to(self.base / "missing")
        self.assertEqual(_gpu_device_nodes(self.devices), sorted(expected))

    def test_symlinked_device_directory_cannot_escape_inventory(self):
        external = self.base / "outside-dev"
        external.mkdir()
        (external / "card0").touch()
        (self.devices / "dri").symlink_to(external, target_is_directory=True)
        self.character_nodes.add(self.devices / "dri/card0")
        self.assertEqual(_gpu_device_nodes(self.devices), [])
        self.assertEqual(_gpu_device_nodes(self.base / "nonexistent"), [])


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

    def test_quoted_launcher_keeps_home_arguments_and_only_two_writable_filesystem_binds(self):
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

    def test_gpu_passthrough_binds_individual_nodes_after_private_dev(self):
        devices = [Path("/dev/dri/card0"), Path("/dev/dri/renderD128"),
                   Path("/dev/nvidiactl"), Path("/dev/nvidia-caps/nvidia-cap1")]
        with patch("gaybar_tools.isolation._gpu_device_nodes", return_value=devices):
            command = bubblewrap_command(sys.executable, self.paths)
        device_mounts = [(command[index + 1], command[index + 2])
                         for index, argument in enumerate(command) if argument == "--dev-bind-try"]
        self.assertEqual(device_mounts, [(str(device), str(device)) for device in devices])
        self.assertLess(command.index("--dev"), command.index("--dev-bind-try"))
        self.assertNotIn("--dev-bind", command)
        # A whole /dev, /dev/dri or NVIDIA directory must never become a
        # writable host filesystem mount just to provide device access.
        writable = [(command[index + 1], command[index + 2])
                    for index, argument in enumerate(command) if argument == "--bind"]
        self.assertEqual(writable, [(str(self.root), str(self.root)), ("/tmp", "/tmp")])
        with patch("gaybar_tools.isolation._gpu_device_nodes", return_value=[]):
            self.assertNotIn("--dev-bind-try", bubblewrap_command(sys.executable, self.paths))

    def test_available_gpu_character_devices_are_visible_inside_sandbox(self):
        devices = _gpu_device_nodes()
        if not devices:
            self.skipTest("当前环境没有可透传的 GPU 设备节点")
        expected = {str(path): path.stat().st_rdev for path in devices}
        launcher = prepare_browser_executable(sys.executable, self.paths)
        script = """import json,pathlib,stat,sys
devices={}
for name in sys.argv[1:]:
    metadata=pathlib.Path(name).stat()
    if not stat.S_ISCHR(metadata.st_mode): raise SystemExit('not a character device: '+name)
    devices[name]=metadata.st_rdev
mounts={line.split()[4] for line in pathlib.Path('/proc/self/mountinfo').read_text().splitlines()}
if {'/dev/dri','/dev/nvidia-caps'} & mounts: raise SystemExit('host device directory was mounted')
print(json.dumps(devices))
"""
        result = self.execute(launcher, script, *devices)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), expected)

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
