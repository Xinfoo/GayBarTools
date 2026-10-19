"""Execute compact readiness JS with a small deterministic DOM, without a browser."""

import json
from importlib.util import find_spec
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from gaybar_tools.browser import BrowserSession, StopRequested, _QUIZ_PROBE_SCRIPT
from gaybar_tools.config import AppSettings
from gaybar_tools.paths import AppPaths


# This DOM double implements just the selectors/layout APIs used by the probe.
# Node runs the production JavaScript itself; fixtures contain no copied probe logic.
_DOM_RUNNER = r"""
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
class Element {
    constructor(spec) {
        this.spec = spec; this.attrs = spec.attrs || {}; this.nodeType = 1;
        this.tagName = spec.tag.toUpperCase();
        this.children = (spec.children || []).map(child => new Element(child));
        for (const child of this.children) child.parentElement = this;
    }
    getAttribute(name) { return this.attrs[name] ?? null; }
    get hidden() { return 'hidden' in this.attrs; }
    get inert() { return 'inert' in this.attrs; }
    get readOnly() { return 'readonly' in this.attrs; }
    get innerText() { return [this.spec.text || '', ...this.children.map(e => e.innerText)].join('\n').trim(); }
    getBoundingClientRect() { return this.spec.zero ? {width: 0, height: 0} : {width: 200, height: 30}; }
    get contentDocument() {
        if (this.spec.foreign) throw new Error('cross-origin');
        if (!this.spec.frame) return null;
        if (!this.doc) this.doc = new Document(this.spec.frame);
        return this.doc;
    }
    simple(selector) {
        let s = selector.trim();
        if (s.includes(':disabled')) {
            if (!('disabled' in this.attrs) && !this.parentElement?.closest('fieldset[disabled]')) return false;
            s = s.replace(':disabled', '');
        }
        if (s.includes(':not([type])')) {
            if ('type' in this.attrs) return false;
            s = s.replace(':not([type])', '');
        }
        const attrs = [...s.matchAll(/\[([\w-]+)(?:=([^\]]+))?\]/g)];
        for (const [, name, value] of attrs) {
            if (!(name in this.attrs) || (value !== undefined && this.attrs[name] !== value.replace(/^['"]|['"]$/g, ''))) return false;
        }
        s = s.replace(/\[[^\]]+\]/g, '');
        for (const [, name] of s.matchAll(/\.([\w-]+)/g)) {
            if (!(this.attrs.class || '').split(/\s+/).includes(name)) return false;
        }
        for (const [, name] of s.matchAll(/#([\w-]+)/g)) if (this.attrs.id !== name) return false;
        const tag = s.match(/^[\w-]+/);
        return !tag || this.tagName.toLowerCase() === tag[0].toLowerCase();
    }
    matches(selector) {
        return selector.split(',').some(part => {
            const chain = part.trim().split(/\s*>\s*/);
            let node = this;
            for (let i = chain.length - 1; i >= 0; i--, node = node?.parentElement)
                if (!node || !node.simple(chain[i])) return false;
            return true;
        });
    }
    closest(selector) {
        for (let node = this; node; node = node.parentElement)
            if (node.matches(selector)) return node;
        return null;
    }
    contains(other) { return other === this || this.children.some(child => child.contains(other)); }
    querySelectorAll(selector) {
        return this.children.flatMap(child => [
            ...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
class Document {
    constructor(spec) {
        this.nodeType = 9;
        this.documentElement = new Element({tag: 'html', children: [spec]});
        this.body = this.documentElement.children[0];
    }
    querySelectorAll(selector) {
        return [...(this.documentElement.matches(selector) ? [this.documentElement] : []),
            ...this.documentElement.querySelectorAll(selector)];
    }
}
global.getComputedStyle = e => ({display: 'block', visibility: 'visible', opacity: '1', ...(e.spec.style || {})});
global.location = {pathname: '/work/doHomeWorkNew'};
const probe = eval('(' + input.script + ')');
const output = input.fixtures.map(spec => {
    global.document = new Document({tag: 'body', children: [spec]});
    return {document: probe(), element: probe(document.body)};
});
process.stdout.write(JSON.stringify(output));
"""


def element(tag, text="", *, attrs=None, children=(), **properties):
    return {"tag": tag, "text": text, "attrs": attrs or {}, "children": list(children), **properties}


def question(kind, controls):
    return element("section", attrs={"data-question-id": "q1"}, children=[
        element("h3", f"1【{kind}题】合成题目", attrs={"class": "stem"}), *controls])


class QuizProbeRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        node = shutil.which("node")
        if node is None:
            spec = find_spec("playwright")
            if spec is not None and spec.origin:
                driver = Path(spec.origin).parent / "driver"
                node = next((str(driver / name) for name in ("node", "node.exe")
                             if (driver / name).is_file()), None)
        if node is None:
            raise unittest.SkipTest("执行探测脚本单元测试需要 Node.js；不启动浏览器")
        option = lambda text: element("div", attrs={"class": "option"}, children=[
            element("label", text, children=[element("input", attrs={"type": "radio"}, zero=True)])])
        editor = element("body", attrs={"contenteditable": "true"})
        cls.names = [
            "native_wrappers", "native_inputs", "one_nested_option", "two_nested_options",
            "hidden_textarea", "readonly_textarea", "toolbar_fields", "nested_editable",
            "plaintext_editor", "loaded_editor", "unloaded_editor", "foreign_editor", "blank_previews",
            "short_two_fields", "disabled_fieldset", "choice_text_mentioning_fill",
        ]
        fixtures = [
            question("单选", [element("p", f"{label}. 选项", children=[
                element("input", attrs={"type": "radio"}, zero=True)]) for label in "AB"]),
            question("单选", [element("input", attrs={"type": "radio", "aria-label": label}) for label in "AB"]),
            question("单选", [option("A. 单个选项")]),
            question("单选", [option("A. 选项一"), option("B. 选项二")]),
            question("填空", [element("textarea", style={"display": "none"})]),
            question("填空", [element("textarea", attrs={"readonly": ""})]),
            question("填空", [element("div", attrs={"role": "toolbar"}, children=[element("input")]), element("textarea")]),
            question("简答", [element("div", attrs={"contenteditable": "true"}, children=[
                element("p", attrs={"contenteditable": "true"}), element("textarea")])]),
            question("简答", [element("div", attrs={"contenteditable": "plaintext-only"})]),
            question("简答", [element("textarea", style={"display": "none"}), element("iframe", frame=editor)]),
            question("简答", [element("iframe", frame=element("body"))]),
            question("简答", [element("iframe", foreign=True)]),
            question("填空", [element("div", attrs={"class": "blankItemInp"}, children=[
                element("div", "点击填写", attrs={"class": "InpDIV"}),
                element("div", attrs={"class": "textDIV"}, style={"display": "none"}, children=[
                    element("textarea"), element("iframe", frame=editor)])]) for _ in range(2)]),
            question("简答", [element("textarea"), element("textarea")]),
            question("填空", [element("fieldset", attrs={"disabled": ""}, children=[element("textarea")])]),
            element("section", attrs={"data-question-id": "q1"}, children=[
                element("h3", "1【单选题】以下哪项描述填空题？", attrs={"class": "stem"}), option("A. 一"), option("B. 二")]),
        ]
        result = subprocess.run([node, "-e", _DOM_RUNNER], input=json.dumps({
            "script": _QUIZ_PROBE_SCRIPT, "fixtures": fixtures}, ensure_ascii=False),
            text=True, capture_output=True, timeout=5, check=True)
        cls.results = dict(zip(cls.names, json.loads(result.stdout)))

    def assert_probe(self, name, count, ready):
        for scope, result in self.results[name].items():
            with self.subTest(scope=scope):
                self.assertEqual(result["rows"][0][3], count)
                self.assertEqual(result["ready"], ready)

    def test_native_choice_rows_and_inputs_match_full_extractor(self):
        self.assert_probe("native_wrappers", 2, True)
        self.assert_probe("native_inputs", 2, True)

    def test_nested_option_labels_are_counted_once(self):
        self.assert_probe("one_nested_option", 0, False)
        self.assert_probe("two_nested_options", 2, True)

    def test_hidden_readonly_and_disabled_fields_do_not_mark_ready(self):
        for name in ("hidden_textarea", "readonly_textarea", "disabled_fieldset"):
            with self.subTest(name=name):
                self.assert_probe(name, 0, False)

    def test_toolbars_and_nested_editables_are_not_extra_blanks(self):
        self.assert_probe("toolbar_fields", 1, True)
        self.assert_probe("nested_editable", 1, True)
        self.assert_probe("plaintext_editor", 1, True)

    def test_editor_iframe_requires_loaded_editable_body(self):
        self.assert_probe("loaded_editor", 1, True)
        self.assert_probe("unloaded_editor", 0, False)

    def test_foreign_editor_requests_only_its_exact_question_fallback(self):
        self.assert_probe("foreign_editor", 0, False)
        self.assertEqual(self.results["foreign_editor"]["document"]["pendingText"], [{
            "index": 0, "path": "html > :nth-child(1) > :nth-child(1)", "short": True}])
        self.assertEqual(self.results["foreign_editor"]["element"]["pendingText"], [{
            "index": 0, "path": ":scope > :nth-child(1)", "short": True}])

    def test_inactive_blank_preview_is_one_field_without_clicking(self):
        self.assert_probe("blank_previews", 2, True)

    def test_short_answer_requires_one_editor(self):
        self.assert_probe("short_two_fields", 2, False)

    def test_choice_stem_mentioning_fill_question_stays_a_choice(self):
        self.assert_probe("choice_text_mentioning_fill", 2, True)
        self.assertEqual(self.results["choice_text_mentioning_fill"]["document"]["rows"][0][2], "choice")


class QuizProbeFallbackTests(unittest.TestCase):
    def setUp(self):
        self.session = BrowserSession(AppSettings(), AppPaths.for_root(Path("/tmp/unused-probe-rules")),
                                      lambda _message: None, threading.Event())
        self.session._navigation_mode = True

    def test_foreign_editor_uses_scoped_extractor_and_poll_cache(self):
        for path in ("html > :nth-child(1) > :nth-child(2)", ":scope > :nth-child(2)"):
            with self.subTest(path=path):
                self.session._reset_navigation_caches()
                root = Mock()
                root.evaluate.return_value = {"rows": [["q1", "1【简答题】题目", "text", 0, [], 0]],
                                              "ready": False,
                                              "pendingText": [{"index": 0, "path": path, "short": True}]}
                with patch.object(self.session, "_text_answer_controls", return_value=[Mock()]) as fields:
                    first = self.session._quiz_probe(root)
                    self.assertIs(first, self.session._quiz_probe(root))
                    self.assertTrue(first["ready"])
                    self.assertEqual(first["rows"][0][3], 1)
                    fields.assert_called_once_with(root.locator.return_value)
                root.evaluate.assert_called_once()
                root.locator.assert_called_once_with(path)

    def test_unavailable_foreign_editor_remains_pending(self):
        root = Mock()
        for fields in ([], [Mock(), Mock()]):
            with self.subTest(count=len(fields)):
                self.session._reset_navigation_caches()
                root.evaluate.return_value = {"rows": [["q1", "1【简答题】题目", "text", 0, [], 0]],
                                              "ready": False,
                                              "pendingText": [{"index": 0, "path": ":scope > :nth-child(1)", "short": True}]}
                with patch.object(self.session, "_text_answer_controls", return_value=fields):
                    self.assertFalse(self.session._quiz_probe(root)["ready"])

    def test_ordinary_probe_does_not_run_field_extraction(self):
        root = Mock()
        root.evaluate.return_value = {"rows": [["q1", "1【填空题】题目", "text", 1, [], 1]], "ready": True}
        with patch.object(self.session, "_text_answer_controls", side_effect=AssertionError("slow path")):
            self.assertTrue(self.session._quiz_probe(root)["ready"])
        root.locator.assert_not_called()

    def test_foreign_editor_in_question_root_uses_root_itself(self):
        root = Mock()
        root.evaluate.return_value = {"rows": [["q1", "1【填空题】题目", "text", 0, [], 0]],
                                      "ready": False, "pendingText": [{"index": 0, "path": ":scope"}]}
        with patch.object(self.session, "_text_answer_controls", return_value=[Mock()]) as fields:
            self.assertTrue(self.session._quiz_probe(root)["ready"])
            fields.assert_called_once_with(root)
        root.locator.assert_not_called()

    def test_stop_during_foreign_editor_fallback_is_preserved(self):
        root = Mock()
        root.evaluate.return_value = {"rows": [["q1", "1【填空题】题目", "text", 0, [], 0]],
                                      "ready": False, "pendingText": [{"index": 0, "path": ":scope"}]}
        with patch.object(self.session, "_text_answer_controls", side_effect=StopRequested("stopped")):
            with self.assertRaises(StopRequested):
                self.session._quiz_probe(root)


if __name__ == "__main__":
    unittest.main()
