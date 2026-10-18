"""Process one learning page at a time, following the site's Next control."""

import threading
from collections.abc import Callable

from .config import AppSettings
from .models import Answer, Question


class WorkflowError(RuntimeError):
    pass


class WorkflowStopped(WorkflowError):
    pass


class Workflow:
    def __init__(self, browser, solver, settings: AppSettings,
                 stop_event: threading.Event, log: Callable[[str], None],
                 state: Callable[[str], None] | None = None):
        self.browser, self.solver, self.settings = browser, solver, settings
        self.stop_event, self.log = stop_event, log
        self.state = state or (lambda value: None)

    def check_stop(self):
        if self.stop_event.is_set():
            raise WorkflowStopped("已停止，浏览器保留在当前页面。")

    def _solve_video_quiz(self, questions: list[Question], screenshot: bytes | None, *,
                          video_quiz_feedback: str | None = None) -> list[Answer]:
        self.check_stop()
        self.state(f"识别并解答视频插入题（{len(questions)} 道）")
        self.log("视频出现插入题，正在调用模型作答。")
        if video_quiz_feedback:
            answers = self.solver.solve(questions, screenshot, video_quiz_feedback=video_quiz_feedback)
        else:
            answers = self.solver.solve(questions, screenshot)
        self.check_stop()
        self.state("视频播放与插入题处理中")
        return answers

    def _content_task_state(self, kind: str, index: int, total: int) -> None:
        self.check_stop()
        action = "视频正常播放中（自动处理插入题）" if kind == "video" else "阅读静态页面"
        self.state(f"任务点 {index}/{total}：{action}")

    def run(self) -> int:
        visited: set[str] = set()
        completed = 0
        while completed < self.settings.max_chapters:
            self.check_stop()
            # An explicit Start chooses a page once. Later iterations inspect
            # the chapter already selected by next_chapter, rather than switch
            # to an unrelated tab opened while the task was running.
            kind = self.browser.chapter_kind() if visited else self.browser.prepare_start()
            if kind not in {"video", "document", "quiz"}:
                raise WorkflowError("当前页面未识别到视频、文档或学习检测，请进入课程章节后重试；可查看诊断截图。")
            key = self.browser.chapter_key()
            if not key or key in visited:
                raise WorkflowError("学习页面没有变化或发生循环，已停止。")
            visited.add(key)
            self.state(f"正在处理第 {completed + 1} 个学习页面")
            if kind in {"video", "document"}:
                self.browser.process_content(on_quiz=self._solve_video_quiz,
                                             on_task=self._content_task_state)
            elif kind == "quiz":
                if self.browser.quiz_completed():
                    self.log("已识别当前检测的提交结果，继续下一节。")
                else:
                    questions = self.browser.get_questions()
                    if not questions:
                        raise WorkflowError("未识别到可作答题目，已停止，请检查是否已提交或页面尚未加载。")
                    self.state(f"识别并解答 {len(questions)} 道题")
                    screenshot = self.browser.quiz_screenshot() if self.settings.vision or self.settings.ocr else None
                    answers = self.solver.solve(questions, screenshot)
                    self.check_stop()
                    self.browser.apply_answers(answers)
                    if not self.settings.auto_submit:
                        self.log("答案已填写；自动提交已关闭，请手动检查并提交。")
                        self.state("等待手动提交")
                        return completed
                    self.state("提交当前答题页")
                    self.check_stop()
                    if not self.browser.submit_quiz():
                        raise WorkflowError("尚未确认提交成功，已停止，不会切换到下一节。")
            self.check_stop()
            completed += 1
            self.log(f"第 {completed} 个学习页面已处理完成，准备点击下一节。")
            self.check_stop()
            if not self.browser.next_chapter():
                self.state("课程处理结束")
                self.log(f"未发现可进入的下一节，本次完成 {completed} 个学习页面。")
                return completed
        self.state("已达到学习页面上限")
        self.log(f"已处理 {completed} 个学习页面，达到配置上限。")
        return completed
