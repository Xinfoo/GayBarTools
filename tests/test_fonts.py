"""Keep user-installed fonts visible while runtime writes stay in the project."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

from gaybar_tools.browser import BrowserSession
from gaybar_tools.config import AppSettings
from gaybar_tools.paths import AppPaths


@unittest.skipUnless(sys.platform.startswith("linux"), "Fontconfig 桥接仅适用于 Linux")
class FontEnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gaybar-font-test-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.home = self.base / "用户 & home"
        self.home.mkdir()
        self.paths = AppPaths.for_root(self.base / "project").prepare()
        self.bridge = (self.paths.runtime / "config/fontconfig/conf.d/"
                       "00-gaybar-host-fonts.conf")

    def test_default_user_fonts_and_config_survive_runtime_redirection(self):
        with patch.dict(os.environ, {"HOME": str(self.home)}, clear=True):
            env = self.paths.environment()
        document = ElementTree.parse(self.bridge).getroot()
        self.assertEqual(document.findtext("cachedir"), str(self.paths.cache / "fontconfig"))
        self.assertEqual(document.findtext("dir"), str(self.home / ".local/share/fonts"))
        includes = document.findall("include")
        self.assertEqual([item.text for item in includes], [
            str(self.home / ".config/fontconfig/conf.d"),
            str(self.home / ".config/fontconfig/fonts.conf"),
        ])
        self.assertTrue(all(item.get("ignore_missing") == "yes" for item in includes))
        for name in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
            self.assertTrue(Path(env[name]).is_relative_to(self.paths.runtime), name)
        self.assertEqual(env["HOME"], str(self.home))

    def test_custom_paths_are_xml_safe_and_fontconfig_overrides_are_preserved(self):
        data = self.home / "字体 & <data>"
        config = self.home / "设置 & <config>"
        overrides = {"FONTCONFIG_FILE": "/custom/fonts.conf", "FONTCONFIG_PATH": "/custom/conf"}
        host = {"HOME": str(self.home), "XDG_DATA_HOME": str(data),
                "XDG_CONFIG_HOME": str(config), **overrides}
        with patch.dict(os.environ, host, clear=True):
            env = self.paths.environment()
        document = ElementTree.parse(self.bridge).getroot()
        self.assertEqual(document.findtext("dir"), str(data / "fonts"))
        self.assertEqual([node.text for node in document.findall("include")], [
            str(config / "fontconfig/conf.d"), str(config / "fontconfig/fonts.conf"),
        ])
        self.assertEqual({key: env[key] for key in overrides}, overrides)

    def test_activation_and_browser_environment_do_not_replace_host_bridge_with_itself(self):
        with patch.dict(os.environ, {"HOME": str(self.home)}, clear=True):
            original_bytecode = sys.dont_write_bytecode
            try:
                self.paths.activate()
                original = self.bridge.read_bytes()
                self.paths.activate()
                self.paths.environment()
                session = BrowserSession(AppSettings(), self.paths, lambda _: None,
                                         threading.Event())
                try:
                    browser_env = session._browser_env()
                finally:
                    session.close()
            finally:
                sys.dont_write_bytecode = original_bytecode
        self.assertEqual(self.bridge.read_bytes(), original)
        self.assertEqual(browser_env["XDG_CONFIG_HOME"], str(self.paths.runtime / "config"))
        document = ElementTree.parse(self.bridge).getroot()
        self.assertFalse(any(str(self.paths.runtime) in node.text
                             for node in document.findall("include")))

    def test_switching_between_projects_preserves_original_host_sources(self):
        other = AppPaths.for_root(self.base / "other-project").prepare()
        with patch.dict(os.environ, {"HOME": str(self.home)}, clear=True):
            original_bytecode = sys.dont_write_bytecode
            try:
                self.paths.activate()
                original = self.bridge.read_bytes()
                other.activate()
                self.paths.environment()
            finally:
                sys.dont_write_bytecode = original_bytecode
        self.assertEqual(self.bridge.read_bytes(), original)
        document = ElementTree.parse(self.bridge).getroot()
        self.assertEqual(document.findtext("dir"), str(self.home / ".local/share/fonts"))

    def test_config_symlink_cannot_redirect_generated_files_outside_project(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.paths.runtime / "config").symlink_to(outside, target_is_directory=True)
        with patch.dict(os.environ, {"HOME": str(self.home)}, clear=True):
            with self.assertRaises(ValueError):
                self.paths.environment()
        self.assertEqual(list(outside.iterdir()), [])

    def test_other_platforms_do_not_generate_fontconfig_bridge(self):
        with patch("gaybar_tools.paths.sys.platform", "win32"):
            with patch.dict(os.environ, {"HOME": str(self.home)}, clear=True):
                self.paths.environment()
        self.assertFalse(self.bridge.exists())


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("fc-list")
                     and shutil.which("fc-match"), "需要 Linux 和 Fontconfig 命令")
class UserFontDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gaybar-user-font-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.paths = AppPaths.for_root(self.base / "project").prepare()
        self.home = self.base / "home"
        self.data = self.home / "自定义 & data"
        self.config = self.home / "自定义 & config"
        self.fonts = self.data / "fonts"
        self.fonts.mkdir(parents=True)
        (self.config / "fontconfig/conf.d").mkdir(parents=True)
        self.fontconfig = self.base / "only-xdg-fonts.conf"
        # No system directories: a test machine's global CJK fonts cannot hide
        # the regression caused by redirecting XDG_DATA_HOME.
        self.fontconfig.write_text('''<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig>
  <dir prefix="xdg">fonts</dir>
  <include prefix="xdg" ignore_missing="yes">fontconfig/conf.d</include>
  <cachedir prefix="xdg">fontconfig</cachedir>
</fontconfig>
''', encoding="utf-8")
        match = subprocess.run(["fc-match", "-f", "%{file}\n", ":lang=zh-cn"],
                               capture_output=True, text=True, timeout=10, check=True)
        source = next((Path(line) for line in match.stdout.splitlines()
                       if Path(line).is_file()), None)
        if source is None:
            self.skipTest("当前系统没有可用于用户字体回归测试的字体")
        self.font = self.fonts / source.name
        shutil.copyfile(source, self.font)
        empty_system_data = self.base / "empty-system-data"
        empty_system_data.mkdir()
        self.host = {**os.environ, "HOME": str(self.home),
                     "XDG_CONFIG_HOME": str(self.config), "XDG_DATA_HOME": str(self.data),
                     # Recent Fontconfig also searches XDG_DATA_DIRS/fonts
                     # for <dir prefix="xdg">; exclude the real system fonts.
                     "XDG_DATA_DIRS": str(empty_system_data),
                     "FONTCONFIG_FILE": str(self.fontconfig),
                     "XDG_CACHE_HOME": str(self.paths.cache)}
        self.host.pop("FONTCONFIG_PATH", None)

    def command(self, arguments, env):
        result = subprocess.run(arguments, env=env, capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def repaired_environment(self):
        with patch.dict(os.environ, self.host, clear=True):
            return self.paths.environment()

    def test_user_font_disappears_without_bridge_and_returns_with_bridge(self):
        broken = {**self.host,
                  "XDG_CONFIG_HOME": str(self.paths.runtime / "config"),
                  "XDG_DATA_HOME": str(self.paths.runtime / "data")}
        self.assertEqual(self.command(["fc-list", "-f", "%{file}\n"], broken), "")
        repaired = self.repaired_environment()
        discovered = self.command(["fc-list", "-f", "%{file}\n"], repaired).splitlines()
        self.assertEqual(set(discovered), {str(self.font)})
        self.assertEqual(self.command(["fc-match", "-f", "%{file}", ":lang=zh-cn"], repaired),
                         str(self.font))
        self.assertFalse((self.home / ".cache").exists())

    @unittest.skipUnless(shutil.which("fc-pattern"), "需要 fc-pattern 检查用户字体别名")
    def test_original_user_font_configuration_is_applied(self):
        alias = ElementTree.Element("fontconfig")
        match = ElementTree.SubElement(alias, "match", {"target": "pattern"})
        test = ElementTree.SubElement(match, "test", {"name": "family"})
        ElementTree.SubElement(test, "string").text = "Gaybar Test Font"
        edit = ElementTree.SubElement(match, "edit", {"name": "family", "mode": "assign"})
        ElementTree.SubElement(edit, "string").text = "User Config Was Read"
        ElementTree.ElementTree(alias).write(self.config / "fontconfig/fonts.conf", encoding="utf-8")
        result = self.command(["fc-pattern", "--config", "-f", "%{family}",
                               "Gaybar Test Font"], self.repaired_environment())
        self.assertEqual(result, "User Config Was Read")

    def test_user_reset_dirs_rule_does_not_remove_restored_user_fonts(self):
        (self.config / "fontconfig/fonts.conf").write_text(
            "<fontconfig><reset-dirs/></fontconfig>", encoding="utf-8")
        discovered = self.command(["fc-list", "-f", "%{file}\n"],
                                  self.repaired_environment()).splitlines()
        self.assertEqual(set(discovered), {str(self.font)})

    def test_user_configuration_does_not_move_font_cache_outside_project(self):
        host_cache = self.home / "host-font-cache"
        host_cache.mkdir()
        document = ElementTree.Element("fontconfig")
        ElementTree.SubElement(document, "cachedir").text = str(host_cache)
        ElementTree.ElementTree(document).write(self.config / "fontconfig/fonts.conf",
                                                encoding="utf-8")
        self.command(["fc-list", "-f", "%{file}\n"], self.repaired_environment())
        self.assertEqual(list(host_cache.iterdir()), [])
        self.assertTrue(list((self.paths.cache / "fontconfig").glob("*.cache-*")))

    def test_fresh_qt_process_can_render_chinese_from_user_font(self):
        repaired = self.repaired_environment()
        charset = self.command(["fc-match", "-f", "%{charset}", ":lang=zh-cn"], repaired)
        codepoint = ord("中")
        supported = False
        for item in charset.split():
            bounds = item.split("-")
            low, high = int(bounds[0], 16), int(bounds[-1], 16)
            supported |= low <= codepoint <= high
        if not supported:
            self.skipTest("当前系统未安装中文字体，已通过独立 Fontconfig 发现测试")
        repaired["QT_QPA_PLATFORM"] = "offscreen"
        script = '''import json
from PySide6.QtGui import QFont, QFontInfo, QFontMetrics
from PySide6.QtWidgets import QApplication
app = QApplication([])
font = QFont("sans-serif")
metrics = QFontMetrics(font)
print(json.dumps({"glyphs": all(metrics.inFontUcs4(ord(char)) for char in "学习通章节"),
                  "family": QFontInfo(font).family()}))
'''
        result = json.loads(self.command([sys.executable, "-B", "-c", script], repaired))
        self.assertTrue(result["glyphs"], result)
        expected_family = self.command(["fc-match", "-f", "%{family}", ":lang=zh-cn"], repaired)
        self.assertIn(result["family"], expected_family.split(","))


if __name__ == "__main__":
    unittest.main()
