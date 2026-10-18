"""Browser-independent data exchanged with the answer provider."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Option:
    label: str
    text: str


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    kind: str
    options: tuple[Option, ...]


@dataclass(frozen=True)
class Answer:
    question_id: str
    choices: tuple[str, ...]
    reason: str = ""


class VideoQuizSolver(Protocol):
    def __call__(self, questions: list[Question], screenshot: bytes | None, *,
                 video_quiz_feedback: str | None = None) -> list[Answer]: ...
