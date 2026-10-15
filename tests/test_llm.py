"""No paid calls: exercise provider requests and fail-closed answer handling."""

import base64
import io
import json
import socket
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from gaybar_tools.config import AppSettings
from gaybar_tools.llm import QuizSolver, SolverCancelled, SolverError, _SameOriginRedirectHandler, _completion_url
from gaybar_tools.models import Option, Question


PNG = b"\x89PNG\r\n\x1a\nmock-screenshot"


def make_question(question_id="q1", kind="single"):
    return Question(question_id, "Which statements are true?", kind, (Option("A", "First"), Option("B", "Second")))


def answer(question_id="q1", choices=None, confidence=0.95):
    return {"question_id": question_id, "choices": choices if choices is not None else ["A"], "confidence": confidence}


def api_response(content, **choice_fields):
    return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}, **choice_fields}]}).encode())


class QuizSolverTests(unittest.TestCase):
    def setUp(self):
        self.settings = AppSettings(base_url="http://localhost:11434/v1", model="local-vision", api_key="")
        self.messages = []
        self.stop = threading.Event()
        self.solver = QuizSolver(self.settings, self.messages.append, self.stop)

    def solve_with(self, answers, questions=None, screenshot=None):
        with patch("gaybar_tools.llm.request.OpenerDirector.open", return_value=api_response(json.dumps({"answers": answers}))) as mocked:
            result = self.solver.solve(questions or [make_question()], screenshot)
            return result, mocked

    def test_single_multiple_and_boolean_are_returned_in_question_order(self):
        questions = [make_question("single"), make_question("multi", "multiple"), make_question("judge", "boolean")]
        result, _ = self.solve_with(
            [answer("judge", ["B"]), answer("multi", ["B", "A"]), answer("single")], questions
        )
        self.assertEqual([item.question_id for item in result], ["single", "multi", "judge"])
        self.assertEqual(result[1].choices, ("B", "A"))

    def test_judge_alias_and_single_correct_multiple_option(self):
        self.solve_with([answer()], [make_question(kind="judge")])
        self.solve_with([answer()], [make_question(kind="multiple")])

    def test_request_fuses_dom_and_base64_screenshot_without_local_key(self):
        result, mocked = self.solve_with([answer()], screenshot=PNG)
        req = mocked.call_args.args[0]
        self.assertEqual(req.full_url, "http://localhost:11434/v1/chat/completions")
        self.assertEqual(mocked.call_args.kwargs["timeout"], self.settings.request_timeout)
        self.assertIsNone(req.get_header("Authorization"))
        payload = json.loads(req.data)
        self.assertEqual(payload["model"], "local-vision")
        content = payload["messages"][1]["content"]
        self.assertIn("Which statements are true?", content[0]["text"])
        self.assertEqual(content[1]["image_url"]["url"], "data:image/png;base64," + base64.b64encode(PNG).decode())
        self.assertEqual(result[0].confidence, 0.95)
        self.assertNotIn("response_format", payload)

    def test_key_is_sent_in_header_but_not_logged(self):
        self.settings.api_key = "secret-not-for-logs"
        _, mocked = self.solve_with([answer()])
        self.assertEqual(mocked.call_args.args[0].get_header("Authorization"), "Bearer secret-not-for-logs")
        self.assertNotIn(self.settings.api_key, "\n".join(self.messages))

    def test_json_fence_and_content_array(self):
        content = [{"type": "text", "text": "```json\n"}, {"type": "text", "text": json.dumps({"answers": [answer()]}) + "\n```"}]
        with patch("gaybar_tools.llm.request.OpenerDirector.open", return_value=api_response(content)):
            self.assertEqual(self.solver.solve([make_question()])[0].choices, ("A",))

    def test_top_level_array_is_unambiguous(self):
        with patch("gaybar_tools.llm.request.OpenerDirector.open", return_value=api_response(json.dumps([answer()]))):
            self.assertEqual(len(self.solver.solve([make_question()])), 1)

    def test_ocr_only_does_not_send_image_but_keeps_dom(self):
        self.settings.vision = False
        self.settings.ocr = True
        with patch.object(self.solver, "_ocr_screenshot", return_value="识别到的文字") as ocr:
            _, mocked = self.solve_with([answer()], screenshot=PNG)
        ocr.assert_called_once_with(PNG)
        content = json.loads(mocked.call_args.args[0].data)["messages"][1]["content"]
        self.assertIsInstance(content, str)
        self.assertIn("识别到的文字", content)
        self.assertIn("Which statements are true?", content)

    def test_both_ocr_and_vision_are_fused(self):
        self.settings.ocr = True
        with patch.object(self.solver, "_ocr_screenshot", return_value="OCR evidence"):
            _, mocked = self.solve_with([answer()], screenshot=PNG)
        content = json.loads(mocked.call_args.args[0].data)["messages"][1]["content"]
        self.assertIn("OCR evidence", content[0]["text"])
        self.assertEqual(content[1]["type"], "image_url")

    def test_invalid_model_outputs_never_return_partial_answers(self):
        invalid = [
            ("not json", "JSON"),
            ("{}", "答案列表"),
            (json.dumps({"answers": []}), "答案列表"),
            (json.dumps({"answers": [answer("unknown")]}), "未知题目"),
            (json.dumps({"answers": [answer(), answer()]}), "重复"),
            (json.dumps({"answers": [answer(choices=[])]}), "空答案"),
            (json.dumps({"answers": [answer(choices=["C"])]}), "未知选项"),
            (json.dumps({"answers": [answer(choices=["A", "A"])]}), "重复选项"),
            (json.dumps({"answers": [answer(choices=["A", "B"])]}), "只能选择一个"),
            (json.dumps({"answers": [answer(confidence=True)]}), "无效置信度"),
            (json.dumps({"answers": [answer(confidence="0.9")]}), "无效置信度"),
            (json.dumps({"answers": [answer(confidence=-0.1)]}), "无效置信度"),
            (json.dumps({"answers": [answer(confidence=1.1)]}), "无效置信度"),
            (json.dumps({"answers": [answer(confidence=float("nan"))]}), "无效置信度"),
            (json.dumps({"answers": [answer(confidence=0.2)]}), "低于"),
            (json.dumps({"answers": [{**answer(), "reason": []}]}), "解释格式"),
        ]
        for content, expected in invalid:
            with self.subTest(content=content), patch("gaybar_tools.llm.request.OpenerDirector.open", return_value=api_response(content)):
                with self.assertRaisesRegex(SolverError, expected):
                    self.solver.solve([make_question()])

    def test_missing_question_rejected(self):
        with self.assertRaisesRegex(SolverError, "遗漏"):
            self.solve_with([answer()], [make_question(), make_question("q2")])

    def test_threshold_boundary_is_accepted(self):
        result, _ = self.solve_with([answer(confidence=self.settings.confidence_threshold)])
        self.assertEqual(result[0].confidence, self.settings.confidence_threshold)

    def test_bad_dom_is_rejected_before_network(self):
        invalid_questions = [
            [],
            [make_question(), make_question()],
            [Question("q1", "", "single", (Option("A", "yes"), Option("B", "no")))],
            [make_question(kind="essay")],
            [Question("q1", "Q?", "single", (Option("A", "yes"),))],
            [Question("q1", "Q?", "single", (Option("A", "yes"), Option("A", "no")))],
            [Question("q1", "Q?", "single", (Option("A", ""), Option("B", "no")))],
        ]
        with patch("gaybar_tools.llm.request.OpenerDirector.open") as mocked:
            for questions in invalid_questions:
                with self.subTest(questions=questions), self.assertRaises(SolverError):
                    self.solver.solve(questions)
            mocked.assert_not_called()

    def test_empty_and_invalid_screenshot_rejected(self):
        with patch("gaybar_tools.llm.request.OpenerDirector.open") as mocked:
            for screenshot in [b"", b"not-an-image", "not-bytes"]:
                with self.subTest(screenshot=screenshot), self.assertRaises(SolverError):
                    self.solver.solve([make_question()], screenshot)
            mocked.assert_not_called()

    def test_disabled_recognition_rejected(self):
        self.settings.vision = False
        self.settings.ocr = False
        with self.assertRaisesRegex(SolverError, "至少启用"):
            self.solver.solve([make_question()], PNG)

    def test_bad_settings_rejected_before_network(self):
        for field, value in [("model", ""), ("request_timeout", 0), ("confidence_threshold", 2), ("api_key", "bad\nkey")]:
            with self.subTest(field=field):
                original = getattr(self.settings, field)
                setattr(self.settings, field, value)
                with patch("gaybar_tools.llm.request.OpenerDirector.open") as mocked, self.assertRaises(SolverError):
                    self.solver.test_connection()
                mocked.assert_not_called()
                setattr(self.settings, field, original)

    def test_http_errors_are_readable_and_never_expose_response_body(self):
        self.settings.api_key = "private-key-value"
        for status in [400, 401, 403, 404, 408, 413, 429, 500]:
            failure = HTTPError("http://localhost", status, "private-key-value", {}, io.BytesIO(b"private-key-value"))
            with self.subTest(status=status), patch("gaybar_tools.llm.request.OpenerDirector.open", side_effect=failure):
                with self.assertRaises(SolverError) as caught:
                    self.solver.test_connection()
                self.assertIn(str(status), str(caught.exception))
                self.assertNotIn(self.settings.api_key, str(caught.exception))

    def test_network_failures_are_sanitized(self):
        for failure in [socket.timeout("private-data"), URLError(socket.timeout()), URLError("private-data")]:
            with self.subTest(failure=type(failure)), patch("gaybar_tools.llm.request.OpenerDirector.open", side_effect=failure):
                with self.assertRaises(SolverError) as caught:
                    self.solver.test_connection()
                self.assertNotIn("private-data", str(caught.exception))

    def test_invalid_provider_envelopes_rejected(self):
        raw_values = [b"not json", b"[]", b"{}", b'{"error":{"message":"private"}}', b'{"choices":[]}', b'{"choices":[{"message":{"content":null}}]}']
        for raw in raw_values:
            with self.subTest(raw=raw), patch("gaybar_tools.llm.request.OpenerDirector.open", return_value=io.BytesIO(raw)):
                with self.assertRaisesRegex(SolverError, "有效的 Chat"):
                    self.solver.test_connection()

    def test_truncated_output_and_refusal_rejected(self):
        with patch("gaybar_tools.llm.request.OpenerDirector.open", return_value=api_response("{}", finish_reason="length")):
            with self.assertRaisesRegex(SolverError, "截断"):
                self.solver.test_connection()
        raw = io.BytesIO(json.dumps({"choices": [{"message": {"content": "", "refusal": "private reason"}}]}).encode())
        with patch("gaybar_tools.llm.request.OpenerDirector.open", return_value=raw):
            with self.assertRaisesRegex(SolverError, "拒绝"):
                self.solver.test_connection()

    def test_connection_sends_no_page_or_screenshot(self):
        with patch("gaybar_tools.llm.request.OpenerDirector.open", return_value=api_response("OK")) as mocked:
            self.assertIn("成功", self.solver.test_connection())
        payload = json.loads(mocked.call_args.args[0].data)
        self.assertEqual(payload["messages"], [{"role": "user", "content": "Reply with OK only."}])

    def test_cancel_before_request(self):
        self.stop.set()
        with patch("gaybar_tools.llm.request.OpenerDirector.open") as mocked, self.assertRaises(SolverCancelled):
            self.solver.solve([make_question()], PNG)
        mocked.assert_not_called()

    def test_cancel_during_request_is_observed_before_return(self):
        def response(*args, **kwargs):
            self.stop.set()
            return api_response("OK")

        with patch("gaybar_tools.llm.request.OpenerDirector.open", side_effect=response), self.assertRaises(SolverCancelled):
            self.solver.test_connection()

    def test_ocr_missing_binary(self):
        with patch("gaybar_tools.llm.shutil.which", return_value=None), self.assertRaisesRegex(SolverError, "未找到 tesseract"):
            self.solver._ocr_screenshot(PNG)

    def test_ocr_uses_only_pipes_and_reports_no_stderr(self):
        process = Mock()
        process.communicate.return_value = ("截图文字".encode(), None)
        process.returncode = 0
        process.poll.return_value = 0
        with patch("gaybar_tools.llm.shutil.which", return_value="/usr/bin/tesseract"), patch("gaybar_tools.llm.subprocess.Popen", return_value=process) as launch:
            self.assertEqual(self.solver._ocr_screenshot(PNG), "截图文字")
        self.assertEqual(launch.call_args.args[0], ["/usr/bin/tesseract", "stdin", "stdout", "-l", "chi_sim+eng"])
        self.assertEqual(process.communicate.call_args.kwargs["input"], PNG)
        process.returncode = 1
        with patch("gaybar_tools.llm.shutil.which", return_value="tesseract"), patch("gaybar_tools.llm.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(SolverError, "语言包"):
                self.solver._ocr_screenshot(PNG)

    def test_ocr_cancellation_kills_child(self):
        process = Mock()
        process.poll.return_value = None

        def launch(*args, **kwargs):
            self.stop.set()
            return process

        with patch("gaybar_tools.llm.shutil.which", return_value="tesseract"), patch("gaybar_tools.llm.subprocess.Popen", side_effect=launch):
            with self.assertRaises(SolverCancelled):
                self.solver._ocr_screenshot(PNG)
        process.kill.assert_called_once()


class EndpointTests(unittest.TestCase):
    def test_supported_base_forms(self):
        cases = {
            "https://api.openai.com": "https://api.openai.com/v1/chat/completions",
            "https://example.com/v1/": "https://example.com/v1/chat/completions",
            "http://127.0.0.1:1234/v1/chat/completions/": "http://127.0.0.1:1234/v1/chat/completions",
            "https://example.com/openai/v1": "https://example.com/openai/v1/chat/completions",
        }
        for base, expected in cases.items():
            with self.subTest(base=base):
                self.assertEqual(_completion_url(base), expected)

    def test_invalid_addresses_do_not_echo_sensitive_url(self):
        for base in ["file:///private", "https://secret@example.com", "https://example.com?key=secret", "https://example.com/#secret", "http://example.com:invalid", "https://[broken"]:
            with self.subTest(base=base), self.assertRaises(SolverError) as caught:
                _completion_url(base)
            self.assertNotIn("secret", str(caught.exception))


class RedirectTests(unittest.TestCase):
    def setUp(self):
        self.handler = _SameOriginRedirectHandler()
        self.original = Request("https://provider.example/v1/chat/completions", data=b"private-request", headers={"Authorization": "Bearer private-key"})

    def test_cross_origin_redirect_is_rejected_before_any_request(self):
        for target in ["https://other.example/v1/chat/completions", "http://provider.example/v1/chat/completions", "https://provider.example:444/v1/chat/completions"]:
            with self.subTest(target=target), self.assertRaisesRegex(SolverError, "跨源重定向") as caught:
                self.handler.redirect_request(self.original, io.BytesIO(), 302, "Found", {}, target)
            self.assertNotIn("private-key", str(caught.exception))

    def test_same_origin_redirect_retains_authentication(self):
        redirect = self.handler.redirect_request(self.original, io.BytesIO(), 302, "Found", {}, "https://provider.example:443/normalized")
        self.assertEqual(redirect.get_header("Authorization"), "Bearer private-key")

    def test_rejection_is_a_readable_solver_error(self):
        settings = AppSettings(base_url="https://provider.example/v1", model="mock", api_key="private-key")
        solver = QuizSolver(settings, lambda message: None, threading.Event())

        def redirected(req, **kwargs):
            return self.handler.redirect_request(req, io.BytesIO(), 302, "Found", {}, "https://other.example/collect")

        with patch("gaybar_tools.llm.request.OpenerDirector.open", side_effect=redirected):
            with self.assertRaisesRegex(SolverError, "跨源重定向"):
                solver.test_connection()


if __name__ == "__main__":
    unittest.main()
