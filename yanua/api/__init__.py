"""Web画面とクライアントアプリから使う HTTP API（K-041）。設計は docs/screens.md。

    from yanua.api import Settings, create_app
"""

from .app import create_app
from .settings import Settings

__all__ = ["Settings", "create_app"]
