"""Native typing into account-free chapter text questions and editor frames."""

import html
import importlib.util
import unittest
from unittest.mock import patch

import test_browser_integration as integration
from gaybar_tools.browser import BrowserError
from gaybar_tools.models import Answer


def editor(field):
    document = """<!doctype html><html><head><style>
        body {min-height:90px; margin:8px; font:16px sans-serif} p {margin:0}
        </style><script>
        for(const type of ['input','keydown','paste','click'])
            document.addEventListener(type, event => {
                parent.recordAnswerEvent(FIELD, type, event.isTrusted);
                if(type==='input')parent.editorValueChanged?.(FIELD,document.body.innerText);
            }, true);
        document.addEventListener('keydown',event=>{
            if(event.key==='Tab'){
                parent.recordAnswerEvent(FIELD,'tab-indent',event.isTrusted);
                event.preventDefault();
            }
        });
        </script></head><body class="view" contenteditable="true"><p>旧答案</p></body></html>""".replace("FIELD", repr(field))
    return f'<iframe data-field="{field}" title="合成编辑器" srcdoc="{html.escape(document, quote=True)}"></iframe>'


def question_fixture(*, deferred=False):
    fixture = """<!doctype html><html><head><meta charset="utf-8"><style>
        body {margin:30px; font:16px sans-serif} .TiMu {padding:10px; max-width:900px}
        input[type=text], textarea {display:block; width:600px; margin:8px; padding:8px}
        textarea {height:70px} iframe {display:block; width:650px; height:140px; margin:8px}
        .edui-toolbar {height:45px} [contenteditable=true] {padding:10px; border:1px solid #ccc}
        </style><script>
        window.answerEvents=[];
        window.recordAnswerEvent=(field,type,trusted)=>answerEvents.push({field,type,trusted});
        for(const type of ['input','keydown','paste','click','change'])
            document.addEventListener(type,event=>{
                const field=event.target.getAttribute('data-field');
                if(field)recordAnswerEvent(field,type,event.isTrusted);
            },true);
        </script></head><body><form id="workForm">
        <section class="TiMu" data-question-id="choice">
          <h3 class="Zy_TItle">1 [单选题] 选择本地选项。</h3>
          <label data-field="choice-a"><input type="radio" name="choice" value="A">A. 选项甲</label>
          <label data-field="choice-b"><input type="radio" name="choice" value="B">B. 选项乙</label>
        </section>
        <section class="TiMu" data-question-id="native-fill">
          <h3 class="Zy_TItle">2 【填空题】依次填写两个空。</h3>
          <input type="text" data-field="native-one" value="旧文本">
          <textarea data-field="native-two">旧文本</textarea>
          <textarea style="display:none" data-field="hidden-native">隐藏旧内容</textarea>
          <input readonly value="只读说明" data-field="read-only">
        </section>
        <section class="TiMu" data-question-id="editor-fill">
          <h3 class="Zy_TItle">3 [填空题] 依次填写两个编辑器。</h3>
          <div class="edui-toolbar"><input type="text" data-field="toolbar" value="工具栏"></div>
          EDITOR_ONE
          <textarea style="display:none" name="answerEditorLocal1" data-field="backing-one">序列化旧内容</textarea>
          EDITOR_TWO
          <textarea style="display:none" name="answerEditorLocal2" data-field="backing-two">序列化旧内容</textarea>
        </section>
        <section class="TiMu" data-question-id="short">
          <h3 class="Zy_TItle">4 [简答题] 用两行说明本地流程。</h3>
          EDITOR_SHORT
          <textarea style="display:none" name="answerEditorLocal3" data-field="backing-short">序列化旧内容</textarea>
        </section>
        </form></body></html>"""
    one, two = editor("editor-one"), editor("editor-two")
    if deferred:
        def blank(field, frame):
            return f'''<div class="blankItemDiv"><span>第一个空：</span><div class="blankItemInp">
                <div class="InpDIV" data-field="activate-{field}" style="min-height:60px;border:1px solid #888">旧答案</div>
                <div class="textDIV" style="display:none">{frame}
                <textarea style="display:none" name="answerEditor-{field}"></textarea></div></div></div>'''
        one, two = blank("editor-one", one), blank("editor-two", two)
        fixture = fixture.replace("</head>", """<script>
            document.addEventListener('click',event=>{
                const display=event.target.closest('.InpDIV');
                if(!display)return;
                for(const other of document.querySelectorAll('.InpDIV')){
                    other.style.display='block';other.nextElementSibling.style.display='none';
                }
                display.style.display='none';display.nextElementSibling.style.display='block';
                display.nextElementSibling.querySelector('iframe').contentDocument.body.focus();
            });
            window.editorValueChanged=(field,value)=>{
                const frame=document.querySelector(`iframe[data-field="${field}"]`);
                const display=frame.closest('.blankItemInp')?.querySelector('.InpDIV');
                if(display)display.innerText=value;
            };
            </script></head>""")
    return fixture.replace("EDITOR_ONE", one).replace("EDITOR_TWO", two).replace(
        "EDITOR_SHORT", editor("editor-short"))


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright 未安装")
class TextQuestionsIntegrationTests(unittest.TestCase):
    setUpClass = integration.BrowserIntegrationTests.__dict__["setUpClass"]
    tearDownClass = integration.BrowserIntegrationTests.__dict__["tearDownClass"]
    setUp = integration.BrowserIntegrationTests.setUp

    def launch_text(self, fixture=None):
        # All documents, editor frames and answers are generated locally; no
        # account cookies or external model provider are involved.
        self.browser.launch(f"{self.base_url}/complete.html", headless=True)
        self.browser.page.route("**/text_questions.html*", lambda route: route.fulfill(
            content_type="text/html; charset=utf-8", body=fixture or question_fixture()))
        self.browser.page.goto(f"{self.base_url}/text_questions.html?chapterId=text-local&courseId=local")
        self.browser.page.wait_for_function("document.querySelectorAll('iframe').length === 3")
        for frame in self.browser.page.frames[1:]:
            frame.wait_for_selector("body[contenteditable=true]", state="attached")
        return self.browser.get_questions()

    def answers(self, *, replacement=False):
        return [Answer("choice", ("B",)),
                Answer("native-fill", (), texts=("新的第一空" if replacement else "第一空", "换行\n第二行")),
                Answer("editor-fill", (), texts=("新的编辑器一" if replacement else "编辑器一", "编辑器二")),
                Answer("short", (), texts=("本地流程第一行\n本地流程第二行",))]

    def events(self):
        return self.browser.page.evaluate("answerEvents")

    def editor_text(self, field):
        text = self.browser.page.frame_locator(f'iframe[data-field="{field}"]').locator("body").inner_text()
        return text.replace("\n\n", "\n")

    def test_text_questions_count_actual_fields_and_ignore_editor_backing_and_toolbar(self):
        questions = self.launch_text()
        self.assertEqual([question.kind for question in questions], ["single", "fill", "fill", "short"])
        self.assertEqual([question.blank_count for question in questions], [0, 2, 2, 0])
        self.assertTrue(all(not question.options for question in questions[1:]))
        self.assertEqual([len(self.browser._question_controls[q.id]) for q in questions], [2, 2, 2, 1])
        self.assertEqual(self.events(), [], "读取题目不应提前点击或输入")

    def test_native_and_rich_fields_use_trusted_input_replace_and_preserve_multiline(self):
        self.launch_text()
        self.browser.apply_answers(self.answers())
        self.browser.apply_answers(self.answers(replacement=True))
        self.assertEqual(self.browser.page.locator('[data-field="native-one"]').input_value(), "新的第一空")
        self.assertEqual(self.browser.page.locator('[data-field="native-two"]').input_value(), "换行\n第二行")
        self.assertEqual(self.editor_text("editor-one"), "新的编辑器一")
        self.assertEqual(self.editor_text("editor-two"), "编辑器二")
        self.assertEqual(self.editor_text("editor-short"), "本地流程第一行\n本地流程第二行")
        events = self.events()
        typed = {event["field"] for event in events if event["type"] == "input"}
        self.assertEqual(typed, {"native-one", "native-two", "editor-one", "editor-two", "editor-short"})
        self.assertTrue(all(event["trusted"] for event in events))
        self.assertFalse(any(event["type"] == "paste" for event in events))
        self.assertFalse(any(event["type"] == "tab-indent" for event in events), "不能用编辑器的缩进键离开输入框")
        for field in ("backing-one", "backing-two", "backing-short"):
            self.assertEqual(self.browser.page.locator(f'[data-field="{field}"]').input_value(), "序列化旧内容")
        self.assertEqual(self.browser.page.locator('[data-field="toolbar"]').input_value(), "工具栏")

    def test_multi_blank_display_activates_hidden_editor_one_at_a_time(self):
        questions = self.launch_text(question_fixture(deferred=True))
        self.assertEqual([q.blank_count for q in questions if q.kind == "fill"], [2, 2])
        self.assertEqual(self.browser.page.locator('.textDIV:visible').count(), 0)
        self.assertEqual(self.events(), [], "不能为了识别空数而预先激活编辑器")
        self.browser.apply_answers(self.answers())
        self.assertEqual(self.browser.page.locator('.textDIV:visible').count(), 1)
        self.assertEqual(self.editor_text("editor-one"), "编辑器一")
        self.assertEqual(self.editor_text("editor-two"), "编辑器二")
        activations = [event for event in self.events() if event["field"].startswith("activate-")]
        self.assertEqual([event["field"] for event in activations], ["activate-editor-one", "activate-editor-two"])
        self.assertTrue(all(event["trusted"] for event in activations))
        self.browser.apply_answers(self.answers(replacement=True))
        self.assertEqual(self.editor_text("editor-one"), "新的编辑器一")

    def test_bad_later_text_answer_prevents_all_prior_clicks_and_typing(self):
        self.launch_text()
        invalid = [
            Answer("short", (), texts=()),
            Answer("short", (), texts=("内容", "多余答案")),
            Answer("short", (), texts=("   ",)),
            Answer("short", ("A",), texts=("内容",)),
        ]
        for answer in invalid:
            with self.subTest(answer=answer):
                answers = self.answers()[:-1] + [answer]
                with self.assertRaisesRegex(BrowserError, "文本答案"):
                    self.browser.apply_answers(answers)
                self.assertEqual(self.events(), [])
                self.assertFalse(self.browser.page.locator('input[value="B"]').is_checked())

    def test_bad_blank_count_and_text_on_choice_are_atomic(self):
        self.launch_text()
        answers = self.answers()
        answers[1] = Answer("native-fill", (), texts=("只有一个空",))
        with self.assertRaisesRegex(BrowserError, "文本答案"):
            self.browser.apply_answers(answers)
        answers = self.answers()
        answers[0] = Answer("choice", ("B",), texts=("不适用文本",))
        with self.assertRaisesRegex(BrowserError, "选项答案"):
            self.browser.apply_answers(answers)
        self.assertEqual(self.events(), [])

    def test_stop_after_focusing_editor_prevents_all_typing(self):
        self.launch_text()
        short = self.browser._questions["short"]
        self.browser._questions = {"short": short}
        self.browser._question_controls = {"short": self.browser._question_controls["short"]}
        click = self.browser._click

        def focus_and_stop(control):
            click(control)
            self.stop.set()

        with patch.object(self.browser, "_click", side_effect=focus_and_stop):
            with self.assertRaises(self.StopRequested):
                self.browser.apply_answers([Answer("short", (), texts=("停止后不能输入",))])
        self.assertEqual(self.editor_text("editor-short"), "旧答案")
        self.assertFalse(any(event["type"] == "input" for event in self.events()))

    def test_video_insertions_do_not_inherit_chapter_text_support(self):
        self.launch_text()
        block = self.browser.page.locator('[data-question-id="short"]')
        with patch.object(self.browser, "_video_question_blocks", return_value=[block]):
            with self.assertRaisesRegex(BrowserError, "视频插题目前仅支持"):
                self.browser._extract_questions([self.browser.page.main_frame], video=object())
        self.assertEqual(self.events(), [])

