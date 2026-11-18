"""ToolPlatform PyQt6 桌面宿主。"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

try:  # 同时支持 python app.py 和 python -m ToolPlatform.app
    from .core.plugin_context import PluginContext
    from .core.plugin_loader import PluginLoadError, PluginLoader, PluginRecord
    from .core.theme import ThemeManager
except ImportError:
    from core.plugin_context import PluginContext
    from core.plugin_loader import PluginLoadError, PluginLoader, PluginRecord
    from core.theme import ThemeManager


PLATFORM_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PLATFORM_ROOT / "config" / "platform.json"
DEFAULT_CONFIG: dict[str, Any] = {
    "title": "ToolPlatform",
    "theme": "industrial",
    "window": {"width": 1440, "height": 900},
}


def load_config() -> tuple[dict[str, Any], str | None]:
    config = deepcopy(DEFAULT_CONFIG)
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise TypeError("根节点必须是对象")
        if isinstance(loaded.get("title"), str) and loaded["title"].strip():
            config["title"] = loaded["title"].strip()
        if loaded.get("theme") in ThemeManager.THEMES:
            config["theme"] = loaded["theme"]
        window = loaded.get("window")
        if isinstance(window, dict):
            for key in ("width", "height"):
                value = window.get(key)
                if isinstance(value, int) and value > 0:
                    config["window"][key] = value
        return config, None
    except Exception as exc:
        return config, f"平台配置读取失败，已使用默认值：{type(exc).__name__}: {exc}"


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class MessagePage(QWidget):
    def __init__(self, title: str, message: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.addStretch()

        title_label = QLabel(title)
        title_label.setObjectName("pluginTitle")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setWordWrap(True)
        layout.addWidget(title_label)

        message_label = QLabel(message)
        message_label.setObjectName("pluginDescription")
        message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        message_label.setWordWrap(True)
        layout.addWidget(message_label)
        layout.addStretch()


class ToolPlatformWindow(QMainWindow):
    def __init__(
        self,
        app: QApplication,
        config: dict[str, Any],
        theme_manager: ThemeManager,
        config_warning: str | None = None,
    ) -> None:
        super().__init__()
        self._app = app
        self._config = config
        self._theme_manager = theme_manager
        self._records: dict[str, PluginRecord] = {}
        self._loaded_widgets: dict[str, QWidget] = {}
        self._contexts: dict[str, PluginContext] = {}
        self._pages: dict[str, QWidget] = {}
        self._current_plugin_id: str | None = None

        self.setWindowTitle(str(config["title"]))
        window = config["window"]
        available = self._app.primaryScreen().availableGeometry() if self._app.primaryScreen() else None
        if available is None:
            target_width = int(window["width"])
            target_height = int(window["height"])
            minimum_width, minimum_height = 960, 640
        else:
            target_width = min(int(window["width"]), available.width())
            target_height = min(int(window["height"]), available.height())
            minimum_width = min(960, available.width())
            minimum_height = min(640, available.height())
        self.setMinimumSize(minimum_width, minimum_height)
        self.resize(target_width, target_height)

        self._build_ui()
        self._discover_plugins()
        if config_warning:
            self.statusBar().showMessage(config_warning, 10000)

    def _build_ui(self) -> None:
        central = QWidget(self)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        top_bar = QFrame(central)
        top_bar.setObjectName("topBar")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(10, 3, 10, 3)
        top_layout.setSpacing(5)

        platform_title = QLabel(str(self._config["title"]), top_bar)
        platform_title.setObjectName("platformTitle")
        top_layout.addWidget(platform_title)
        top_layout.addStretch()
        top_layout.addWidget(QLabel("主题：", top_bar))

        self.theme_combo = QComboBox(top_bar)
        for theme_id, display_name in ThemeManager.THEMES.items():
            self.theme_combo.addItem(display_name, theme_id)
        current_index = self.theme_combo.findData(self._config["theme"])
        self.theme_combo.setCurrentIndex(max(0, current_index))
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        top_layout.addWidget(self.theme_combo)
        outer.addWidget(top_bar)

        navigation_bar = QFrame(central)
        navigation_bar.setObjectName("toolNavigationBar")
        navigation_layout = QHBoxLayout(navigation_bar)
        navigation_layout.setContentsMargins(8, 1, 8, 0)
        navigation_layout.setSpacing(3)

        self.tool_tabs = QTabBar(navigation_bar)
        self.tool_tabs.setObjectName("toolTabs")
        self.tool_tabs.setExpanding(False)
        self.tool_tabs.setUsesScrollButtons(True)
        self.tool_tabs.setDrawBase(False)
        self.tool_tabs.currentChanged.connect(self._on_tool_tab_changed)
        navigation_layout.addWidget(self.tool_tabs, 1)
        outer.addWidget(navigation_bar)

        content = QWidget(central)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(6, 3, 6, 5)
        content_layout.setSpacing(3)

        plugin_info = QFrame(content)
        plugin_info.setObjectName("pluginInfoBar")
        info_layout = QHBoxLayout(plugin_info)
        info_layout.setContentsMargins(4, 1, 4, 1)
        info_layout.setSpacing(6)

        self.plugin_title = QLabel("欢迎使用", plugin_info)
        self.plugin_title.setObjectName("pluginTitle")
        info_layout.addWidget(self.plugin_title)

        self.plugin_description = QLabel("请从上方选择一个工具插件。", plugin_info)
        self.plugin_description.setObjectName("pluginDescription")
        self.plugin_description.setWordWrap(False)
        self.plugin_description.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        info_layout.addWidget(self.plugin_description, 1)
        content_layout.addWidget(plugin_info)

        self.stack = QStackedWidget(content)
        self.stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.welcome_page = MessagePage(
            "暂无已选择的插件",
            "从上方选择工具；也可将符合协议的单文件插件放入 plugins 目录，重启平台后使用。",
            self.stack,
        )
        self.stack.addWidget(self.welcome_page)
        content_layout.addWidget(self.stack, 1)
        outer.addWidget(content, 1)

        self.setCentralWidget(central)
        status_bar = QStatusBar(self)
        self.setStatusBar(status_bar)
        status_bar.showMessage("平台已就绪")

    def _discover_plugins(self) -> None:
        loader = PluginLoader(PLATFORM_ROOT / "plugins")
        records, errors = loader.discover()
        self._records = {record.plugin_id: record for record in records}
        self._populate_navigation(records, errors)

        if errors:
            self.statusBar().showMessage(
                f"发现 {len(records)} 个插件，{len(errors)} 个插件加载失败；平台仍可正常使用",
                10000,
            )
        elif records:
            self.statusBar().showMessage(f"已发现 {len(records)} 个插件", 5000)
        else:
            self.statusBar().showMessage("未发现插件，请向 plugins 目录添加插件", 7000)

    def _populate_navigation(
        self,
        records: list[PluginRecord],
        _errors: list[PluginLoadError],
    ) -> None:
        signals_were_blocked = self.tool_tabs.blockSignals(True)
        while self.tool_tabs.count():
            self.tool_tabs.removeTab(self.tool_tabs.count() - 1)
        for record in records:
            index = self.tool_tabs.addTab(str(record.meta["name"]))
            self.tool_tabs.setTabData(index, record.plugin_id)
            self.tool_tabs.setTabToolTip(
                index,
                (
                    f"分类：{record.meta['category']}\n"
                    f"版本：{record.meta['version']}\n"
                    f"{record.meta['description']}"
                ),
            )
        self.tool_tabs.blockSignals(signals_were_blocked)
        if records:
            self.tool_tabs.setCurrentIndex(0)
            self._switch_to_plugin(records[0].plugin_id)

    def _on_tool_tab_changed(self, index: int) -> None:
        plugin_id = self.tool_tabs.tabData(index) if index >= 0 else None
        if isinstance(plugin_id, str):
            self._switch_to_plugin(plugin_id)

    def _switch_to_plugin(self, plugin_id: str) -> None:
        record = self._records.get(plugin_id)
        if record is None:
            return

        tab_index = next(
            (
                index
                for index in range(self.tool_tabs.count())
                if self.tool_tabs.tabData(index) == plugin_id
            ),
            -1,
        )
        if tab_index >= 0 and self.tool_tabs.currentIndex() != tab_index:
            signals_were_blocked = self.tool_tabs.blockSignals(True)
            self.tool_tabs.setCurrentIndex(tab_index)
            self.tool_tabs.blockSignals(signals_were_blocked)

        if plugin_id == self._current_plugin_id:
            return

        if self._current_plugin_id in self._loaded_widgets:
            previous = self._loaded_widgets[self._current_plugin_id]
            self._call_lifecycle(previous, "deactivate", self._current_plugin_id)

        self.plugin_title.setText(
            f"{record.meta['name']}  ·  v{record.meta['version']}"
        )
        self.plugin_description.setText(str(record.meta["description"]))
        page = self._get_or_create_page(record)
        self.stack.setCurrentWidget(page)
        self._current_plugin_id = plugin_id

        widget = self._loaded_widgets.get(plugin_id)
        if widget is not None:
            self._call_lifecycle(widget, "activate", plugin_id)

    def _get_or_create_page(self, record: PluginRecord) -> QWidget:
        existing = self._pages.get(record.plugin_id)
        if existing is not None:
            return existing

        context = PluginContext(record.plugin_id, PLATFORM_ROOT, parent=self)
        context.status_message.connect(self.statusBar().showMessage)
        self._contexts[record.plugin_id] = context

        try:
            widget = record.factory(context, parent=self.stack)
            if not isinstance(widget, QWidget):
                raise TypeError("create_plugin 必须返回 QWidget 实例")
            self._loaded_widgets[record.plugin_id] = widget
            page = widget
            self.statusBar().showMessage(f"已加载插件：{record.meta['name']}", 5000)
        except Exception as exc:  # 单插件创建失败不影响宿主
            message = f"{type(exc).__name__}: {exc}"
            page = MessagePage("插件创建失败", message, self.stack)
            self.statusBar().showMessage(
                f"插件 {record.meta['name']} 创建失败：{message}",
                10000,
            )

        self._pages[record.plugin_id] = page
        self.stack.addWidget(page)
        return page

    def _call_lifecycle(self, widget: QWidget, method_name: str, plugin_id: str) -> None:
        method = getattr(widget, method_name, None)
        if not callable(method):
            return
        try:
            method()
        except Exception as exc:  # 生命周期故障也必须隔离
            name = self._records[plugin_id].meta["name"]
            self.statusBar().showMessage(
                f"插件 {name} 的 {method_name}() 执行失败：{type(exc).__name__}: {exc}",
                10000,
            )

    def _on_theme_changed(self) -> None:
        theme_id = self.theme_combo.currentData()
        if not isinstance(theme_id, str):
            return
        try:
            self._theme_manager.apply(self._app, theme_id)
            self._config["theme"] = theme_id
            save_config(self._config)
            self.statusBar().showMessage(
                f"已切换为{ThemeManager.THEMES[theme_id]}主题",
                3000,
            )
        except Exception as exc:
            self.statusBar().showMessage(
                f"主题切换失败：{type(exc).__name__}: {exc}",
                10000,
            )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API 命名
        for plugin_id, widget in tuple(self._loaded_widgets.items()):
            self._call_lifecycle(widget, "shutdown", plugin_id)
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("ToolPlatform")
    app.setOrganizationName("ToolPlatform")

    config, warning = load_config()
    theme_manager = ThemeManager(PLATFORM_ROOT / "themes")
    try:
        theme_manager.apply(app, str(config["theme"]))
    except Exception as exc:
        warning = f"主题加载失败：{type(exc).__name__}: {exc}"

    window = ToolPlatformWindow(app, config, theme_manager, warning)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
