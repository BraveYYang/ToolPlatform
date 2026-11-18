"""统一 QSS 主题管理。"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import QApplication


class ThemeManager:
    THEMES = {
        "industrial": "工业深色",
        "light": "浅色",
        "dark": "深色",
    }

    def __init__(self, themes_dir: Path) -> None:
        self.themes_dir = Path(themes_dir).resolve()
        self.current_theme = ""

    def apply(self, app: QApplication, theme_name: str) -> None:
        if theme_name not in self.THEMES:
            raise ValueError(f"未知主题: {theme_name}")
        qss_path = self.themes_dir / f"{theme_name}.qss"
        stylesheet = qss_path.read_text(encoding="utf-8")
        app.setStyleSheet(stylesheet)
        self.current_theme = theme_name
