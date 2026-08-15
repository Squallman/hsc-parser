"""Exam-type selection (практичний іспит)."""

from __future__ import annotations

from .base_page import BasePage


class ExamPage(BasePage):
    PRACTICAL_EXAM = "exam.practical_exam"

    async def select_practical_exam(self) -> None:
        await self.click(self.PRACTICAL_EXAM, step="exam.practical_exam")
