"""Licence category selection (категорія A)."""

from __future__ import annotations

from .base_page import BasePage


class CategoryPage(BasePage):
    CATEGORY_A = "category.category_a"

    async def select_category_a(self) -> None:
        await self.click(self.CATEGORY_A, step="category.category_a")
