"""Page objects. None of them may contain a hardcoded selector."""

from .base_page import BasePage, build_locator
from .calendar_page import CalendarPage
from .category_page import CategoryPage
from .department_page import DepartmentPage
from .exam_page import ExamPage
from .login_page import LoginPage
from .queue_page import QueuePage

__all__ = [
    "BasePage",
    "CalendarPage",
    "CategoryPage",
    "DepartmentPage",
    "ExamPage",
    "LoginPage",
    "QueuePage",
    "build_locator",
]
