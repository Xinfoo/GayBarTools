"""Browser-independent data exchanged with the answer provider."""

from dataclasses import dataclass


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
    confidence: float
    reason: str = ""
