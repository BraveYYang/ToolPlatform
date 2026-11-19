"""ToolPlatform PyQt6 桌面宿主 —— 单一启动入口。

启动时会：
  1. 检测 Python 版本
  2. 自动安装 requirements.txt 中缺失的依赖
  3. 启动平台主窗口

直接运行:
    python app.py
"""

from __future__ import annotations

# ============================================================================
# 启动前环境自检与依赖安装
# （放在最前面；PyQt6 等第三方导入必须发生在依赖安装之后）
# ============================================================================

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

_BOOTSTRAP_ROOT = Path(__file__).resolve().parent
_REQUIREMENTS_FILE = _BOOTSTRAP_ROOT / "requirements.txt"

# 最低 / 推荐 Python 版本
_MIN_PY = (3, 10)
_REC_PY = (3, 14)

# 包安装名 → 导入名
_PKG_IMPORT_MAP = {
    "PyQt6": "PyQt6",
    "numpy": "numpy",
    "opencv-python": "cv2",
    "paramiko": "paramiko",
    "requests": "requests",
    "Pillow": "PIL",
    "matplotlib": "matplotlib",
}


class _Colors:
    RESET = "\033[0m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"


def _enable_windows_ansi() -> None:
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass


def _ok(msg: str) -> None:
    print(f"{_Colors.GREEN}[OK]{_Colors.RESET} {msg}")


def _info(msg: str) -> None:
    print(f"{_Colors.BLUE}[INFO]{_Colors.RESET} {msg}")


def _warn(msg: str) -> None:
    print(f"{_Colors.YELLOW}[WARN]{_Colors.RESET} {msg}")


def _err(msg: str) -> None:
    print(f"{_Colors.RED}[ERR]{_Colors.RESET} {msg}")


def _check_python_version() -> bool:
    current = sys.version_info[:2]
    if current < _MIN_PY:
        _err(f"Python 版本 {current[0]}.{current[1]} 过低（需要 >= {_MIN_PY[0]}.{_MIN_PY[1]}）")
        return False
    if current < _REC_PY:
        _warn(f"建议使用 Python {_REC_PY[0]}.{_REC_PY[1]} 以获得最佳兼容性")
    else:
        _ok(f"Python {sys.version.split()[0]}")
    return True


def _is_installed(import_name: str) -> bool:
    try:
        return importlib.util.find_spec(import_name.split(".")[0]) is not None
    except (ModuleNotFoundError, ValueError):
        return False


def _installed_version(import_name: str) -> str:
    try:
        from importlib.metadata import version as _ver
        pkg = {"cv2": "opencv-python", "PIL": "Pillow"}.get(import_name, import_name)
        return _ver(pkg)
    except Exception:
        try:
            m = importlib.import_module(import_name.split(".")[0])
            return getattr(m, "__version__", "?")
        except Exception:
            return "?"


def _parse_requirements() -> list[tuple[str, str]]:
    """返回 [(pkg_name, full_spec), ...]"""
    if not _REQUIREMENTS_FILE.exists():
        return []
    result = []
    for raw in _REQUIREMENTS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split("==")[0].split(">=")[0].split("<=")[0].split("~=")[0].strip()
        result.append((name, line))
    return result


def _install_packages(specs: list[str]) -> list[str]:
    """逐个 pip install，返回失败列表。"""
    failed = []
    for spec in specs:
        name = spec.split("==")[0].split(">=")[0].strip()
        _info(f"安装 {name}...")
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", spec, "-q"],
                capture_output=True, text=True, timeout=600,
            )
            if r.returncode == 0:
                _ok(f"{name} 安装成功")
            else:
                _err(f"{name} 安装失败")
                if r.stderr.strip():
                    print(f"    {r.stderr.strip()[:300]}")
                failed.append(name)
        except subprocess.TimeoutExpired:
            _err(f"{name} 安装超时")
            failed.append(name)
        except Exception as e:
            _err(f"{name} 安装异常: {e}")
            failed.append(name)
    return failed


def _bootstrap_environment() -> None:
    """检查 Python 版本，自动安装缺失依赖。失败时给出清晰提示并退出。"""
    _enable_windows_ansi()
    print(f"\n{_Colors.CYAN}{_Colors.BOLD}=== ToolPlatform 启动 ==={_Colors.RESET}")
    
    if not _check_python_version():
        input("\n按回车键退出...")
        sys.exit(1)
    
    requirements = _parse_requirements()
    if not requirements:
        _warn("未找到 requirements.txt，跳过依赖检查")
        return
    
    missing: list[str] = []
    for name, spec in requirements:
        import_name = _PKG_IMPORT_MAP.get(name, name)
        if _is_installed(import_name):
            print(f"  {_Colors.GREEN}✓{_Colors.RESET} {name} ({_installed_version(import_name)})")
        else:
            print(f"  {_Colors.RED}✗{_Colors.RESET} {name} {_Colors.YELLOW}未安装{_Colors.RESET}")
            missing.append(spec)
    
    if not missing:
        _ok("依赖检查通过")
        return
    
    print()
    _warn(f"发现 {len(missing)} 个缺失依赖")
    try:
        ans = input(f"{_Colors.YELLOW}是否自动安装缺失依赖? [Y/n]: {_Colors.RESET}").strip().lower()
    except EOFError:
        ans = "y"
    
    if ans not in ("", "y", "yes", "是"):
        _warn("已跳过依赖安装，平台可能无法正常运行")
        return
    
    failed = _install_packages(missing)
    if failed:
        print()
        _err(f"以下 {len(failed)} 个包安装失败: {', '.join(failed)}")
        print()
        _warn("可以尝试手动安装（推荐国内镜像）:")
        print(f"  {sys.executable} -m pip install -r requirements.txt "
              f"-i https://pypi.tuna.tsinghua.edu.cn/simple")
        print()
        try:
            ans = input(f"{_Colors.YELLOW}是否仍要尝试启动? [y/N]: {_Colors.RESET}").strip().lower()
        except EOFError:
            ans = "n"
        if ans not in ("y", "yes", "是"):
            input("\n按回车键退出...")
            sys.exit(1)


# 先完成环境自检，再开始导入 Qt 等重量级依赖
_bootstrap_environment()


# ============================================================================
# 下面是平台主程序代码（原 app.py 内容）
# ============================================================================

import json
from copy import deepcopy
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
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

# 插件加载配置：disabled 中的插件不加载；enabled 非空时只加载白名单内的插件
PLUGIN_CONFIG_PATH = PLATFORM_ROOT / "config" / "plugins.json"
DEFAULT_PLUGIN_CONFIG: dict[str, Any] = {"enabled": [], "disabled": []}


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


def _string_list(raw: object) -> list[str]:
    """把配置中的字符串数组规整为去空白后的列表，忽略非法项。"""
    if not isinstance(raw, (list, tuple)):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def load_plugin_config() -> tuple[dict[str, Any], str | None]:
    """读取 config/plugins.json；文件不存在时返回空配置（加载全部插件）。"""
    config = deepcopy(DEFAULT_PLUGIN_CONFIG)
    if not PLUGIN_CONFIG_PATH.exists():
        return config, None
    try:
        loaded = json.loads(PLUGIN_CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise TypeError("根节点必须是对象")
        config["enabled"] = _string_list(loaded.get("enabled"))
        config["disabled"] = _string_list(loaded.get("disabled"))
        return config, None
    except Exception as exc:
        return config, f"插件配置读取失败，已加载全部插件：{type(exc).__name__}: {exc}"


def save_plugin_config(enabled: list[str], disabled: list[str]) -> None:
    """把插件启用/禁用配置写回 config/plugins.json，保留文件里的说明字段。"""
    data: dict[str, Any] = {"enabled": list(enabled), "disabled": list(disabled)}
    try:
        original = json.loads(PLUGIN_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(original, dict) and "_comment" in original:
            data["_comment"] = original["_comment"]
    except Exception:
        pass
    PLUGIN_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLUGIN_CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class PluginPreferencesDialog(QDialog):
    """插件加载首选项：勾选要加载的插件，取消勾选即禁用。"""

    def __init__(
        self,
        parent: QWidget | None,
        records: list[PluginRecord],
        config: dict[str, Any],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("首选项 · 插件加载")
        self.setMinimumSize(560, 520)
        self._records = sorted(
            records,
            key=lambda record: (
                str(record.meta["category"]).casefold(),
                int(record.meta["order"]),
                str(record.meta["name"]).casefold(),
            ),
        )
        self._config = config
        self._whitelist = bool(config.get("enabled"))
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(8)

        heading = QLabel("插件加载")
        heading.setObjectName("pluginTitle")
        layout.addWidget(heading)

        mode = "白名单（只加载勾选的插件）" if self._whitelist else "黑名单（勾选的插件加载，取消勾选即禁用）"
        description = QLabel(
            "勾选需要加载的插件，取消勾选即可暂时禁用。"
            "当前模式：%s。保存后平台会立即重新加载插件。" % mode
        )
        description.setObjectName("pluginDescription")
        description.setWordWrap(True)
        layout.addWidget(description)

        self.list_widget = QListWidget()
        self.list_widget.setAlternatingRowColors(True)
        enabled, disabled = self._current_names()
        for record in self._records:
            plugin_id = record.plugin_id
            key = plugin_id.casefold()
            stem = record.path.stem.casefold()
            if self._whitelist:
                checked = key in enabled or stem in enabled
            else:
                checked = not (key in disabled or stem in disabled)
            item = QListWidgetItem(
                "%s　·　%s　·　%s" % (record.meta["name"], record.meta["category"], plugin_id)
            )
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, plugin_id)
            item.setToolTip(str(record.meta.get("description") or ""))
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget, 1)

        quick = QHBoxLayout()
        check_all = QPushButton("全选")
        check_all.clicked.connect(self._check_all)
        quick.addWidget(check_all)
        uncheck_all = QPushButton("全不选")
        uncheck_all.clicked.connect(self._uncheck_all)
        quick.addWidget(uncheck_all)
        quick.addStretch()
        layout.addLayout(quick)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存并重新加载")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _current_names(self) -> tuple[set[str], set[str]]:
        enabled = {str(name).strip().casefold() for name in self._config.get("enabled") or []}
        disabled = {str(name).strip().casefold() for name in self._config.get("disabled") or []}
        return enabled, disabled

    def _check_all(self) -> None:
        self._set_all(Qt.CheckState.Checked)

    def _uncheck_all(self) -> None:
        self._set_all(Qt.CheckState.Unchecked)

    def _set_all(self, state: Qt.CheckState) -> None:
        for index in range(self.list_widget.count()):
            self.list_widget.item(index).setCheckState(state)

    def selection(self) -> tuple[list[str], list[str]]:
        """返回 (enabled, disabled)，沿用当前配置的模式。"""
        checked = {
            str(self.list_widget.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.list_widget.count())
            if self.list_widget.item(index).checkState() == Qt.CheckState.Checked
        }
        all_ids = [record.plugin_id for record in self._records]
        if self._whitelist:
            return [plugin_id for plugin_id in all_ids if plugin_id in checked], []
        return [], [plugin_id for plugin_id in all_ids if plugin_id not in checked]


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

        self.preferences_button = QPushButton("首选项", top_bar)
        self.preferences_button.setToolTip("配置哪些插件启用 / 禁用")
        self.preferences_button.clicked.connect(self._open_preferences)
        top_layout.addWidget(self.preferences_button)

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

    def _open_preferences(self) -> None:
        """打开插件加载首选项；保存后立即重新加载插件。"""
        all_loader = PluginLoader(PLATFORM_ROOT / "plugins")
        records, errors = all_loader.discover()
        if not records:
            self.statusBar().showMessage("没有找到可配置的插件", 5000)
            return

        config, warning = load_plugin_config()
        if warning:
            self.statusBar().showMessage(warning, 10000)

        dialog = PluginPreferencesDialog(self, records, config)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        enabled, disabled = dialog.selection()
        save_plugin_config(enabled, disabled)
        self._reload_plugins(enabled, disabled)

        if errors:
            self.statusBar().showMessage(
                "插件配置已保存；另有 %d 个插件加载失败，未列入首选项" % len(errors),
                10000,
            )
        else:
            self.statusBar().showMessage("插件配置已保存并重新加载", 5000)

    def _reload_plugins(self, enabled: list[str], disabled: list[str]) -> None:
        """按新配置清理并重新加载全部插件页面。"""
        for plugin_id, widget in list(self._loaded_widgets.items()):
            self._call_lifecycle(widget, "shutdown", plugin_id)
        for page in list(self._pages.values()):
            self.stack.removeWidget(page)
            page.deleteLater()
        self._pages.clear()
        self._loaded_widgets.clear()
        self._contexts.clear()
        self._records.clear()
        self._current_plugin_id = None
        self.stack.setCurrentWidget(self.welcome_page)

        loader = PluginLoader(
            PLATFORM_ROOT / "plugins",
            disabled=disabled,
            enabled=enabled,
        )
        records, errors = loader.discover()
        self._records = {record.plugin_id: record for record in records}
        self._populate_navigation(records, errors)

        skipped = list(loader.skipped)
        details = [f"发现 {len(records)} 个插件"]
        if errors:
            details.append(f"{len(errors)} 个加载失败")
        if skipped:
            details.append(f"按配置跳过 {len(skipped)} 个")
        self.statusBar().showMessage("，".join(details) + "；平台仍可正常使用", 10000)

    def _discover_plugins(self) -> None:
        plugin_config, plugin_warning = load_plugin_config()
        loader = PluginLoader(
            PLATFORM_ROOT / "plugins",
            disabled=plugin_config["disabled"],
            enabled=plugin_config["enabled"],
        )
        records, errors = loader.discover()
        self._records = {record.plugin_id: record for record in records}
        self._populate_navigation(records, errors)

        skipped = list(loader.skipped)
        details = [f"发现 {len(records)} 个插件"]
        if errors:
            details.append(f"{len(errors)} 个加载失败")
        if skipped:
            details.append(f"按配置跳过 {len(skipped)} 个")
        message = "，".join(details)
        if plugin_warning:
            message = f"{plugin_warning}；{message}"
        if errors or skipped or plugin_warning:
            self.statusBar().showMessage(f"{message}；平台仍可正常使用", 10000)
        elif records:
            self.statusBar().showMessage(message, 5000)
        else:
            self.statusBar().showMessage(
                "未发现插件，请向 plugins 目录添加插件，或检查 config/plugins.json 配置",
                7000,
            )

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
