"""ToolPlatform 插件：网床日志分析。

原工具是 ``网床研究`` 目录下的独立 Tkinter 程序。Tkinter 界面无法嵌入 Qt 窗口，
因此本插件按路径动态加载原始脚本，复用其解析、统计与绘图辅助函数（原文件只读、
不做任何修改），界面部分用 PyQt6 重写为平台内嵌页面：

- 顶部：日志路径 / 网床 tag / 打印 tag / 进度条 / 状态行 / 导出 CSV
- 左侧页签：打印任务、网床记录（点击联动右侧）
- 右侧页签：网床热力图、相对基准差分、统计详情
"""

from __future__ import annotations

import contextlib
import csv
import functools
import importlib.util
import inspect
import os
import re
import sys
import threading
import traceback
import types
from pathlib import Path

import numpy as np

import matplotlib

# 平台内的绘图一律使用 Qt 后端（原脚本用的是 TkAgg，这里必须换掉）
matplotlib.use("QtAgg")
from matplotlib.backends.backend_qtagg import (  # noqa: E402
    FigureCanvasQTAgg,
    NavigationToolbar2QT,
)
from matplotlib.figure import Figure  # noqa: E402

from PyQt6.QtCore import Qt, QThread, pyqtSignal  # noqa: E402
from PyQt6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


PLUGIN_META = {
    "id": "mesh_log_analyzer",
    "name": "网床日志分析",
    "version": "1.1.0",
    "category": "网床研究",
    "description": "解析 Klipper 日志中的网床矩阵与打印事件，查看热力图、相对基准差分与统计详情，并导出 CSV。",
    "order": 50,
}


# 原始脚本来源：环境变量 > 仓库内置副本(vendor/网床研究) > 原开发路径
_PLUGIN_ROOT = Path(__file__).resolve().parent
_PLATFORM_ROOT = _PLUGIN_ROOT.parent
_BUNDLED_SOURCE_DIR = _PLATFORM_ROOT / "vendor" / "网床研究"
_LEGACY_SOURCE_DIR = Path(r"E:\2.Python_Project\Project4-tools\网床研究")


def resolve_source_dir() -> Path:
    """按优先级定位原始脚本目录，方便在其他机器上直接运行。"""
    env_dir = os.environ.get("TOOLPLATFORM_WANGCHUANG_DIR", "").strip()
    if env_dir and Path(env_dir).is_dir():
        return Path(env_dir)
    if _BUNDLED_SOURCE_DIR.is_dir():
        return _BUNDLED_SOURCE_DIR
    return _LEGACY_SOURCE_DIR


SOURCE_DIR = resolve_source_dir()
SOURCE_SCRIPT = SOURCE_DIR / "网床日志分析GUI.py"


# 原脚本中的默认 tag（原脚本无法加载时作为界面兜底显示值）
_FALLBACK_MESH_TAG = "[configfile:set:390] save_config: set [bed_mesh default] points ="
_FALLBACK_PRINT_TAG = "work_handler start print"
_FALLBACK_BASELINE_FIRST = "第一条网床"
_FALLBACK_BASELINE_PREV = "上一条网床"
_FALLBACK_EXPORT_NAME = "网床日志分析结果.csv"

# 中文字体：与原脚本保持一致，避免负号显示成方块
matplotlib.rcParams["font.sans-serif"] = [
    "Microsoft YaHei",
    "SimHei",
    "DengXian",
    "DejaVu Sans",
]
matplotlib.rcParams["axes.unicode_minus"] = False


# 对比基准下拉框中“#n 时间”文本的序号解析（与原脚本 _baseline_index 一致）
_BASELINE_INDEX_RE = re.compile(r"#(\d+)")

_MODULE_NAME = "toolplatform_wc_mesh_log"
# 必须复用的原脚本成员：解析/统计纯函数与数据类
_REQUIRED_FUNCTIONS = (
    "parse_log",
    "dedupe_meshes",
    "attach_prints",
    "mesh_coordinates",
    "grid_extent",
    "annotate_cells",
    "compute_stats",
    "cell_text_color",
    "model_name",
    "build_tag_pattern",
    "parse_timestamp",
)
_REQUIRED_CLASSES = ("MeshRecord", "PrintEvent", "MeshStats", "ParseResult")

_source_module = None  # type: ignore[var-annotated]
_source_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# 动态加载原始脚本
# --------------------------------------------------------------------------- #


class _StubObject:
    """tkinter 不可用时的极简占位对象（原脚本的 Tk 界面类不会被使用）。"""

    def __call__(self, *args, **kwargs):
        return _StubObject()

    def __getattr__(self, name):
        return _StubObject()

    def __iter__(self):
        return iter(())


def _tkinter_available() -> bool:
    try:
        import tkinter  # noqa: F401
    except Exception:  # noqa: BLE001 - 任何失败都视作不可用
        return False
    return True


@contextlib.contextmanager
def _keep_qt_backend():
    """原脚本在 import 阶段会调用 ``matplotlib.use("TkAgg")``。

    这里临时把它替换成空操作，保证平台画布仍然是 QtAgg 后端。
    """

    original_use = matplotlib.use

    def _noop(*args, **kwargs):
        return None

    matplotlib.use = _noop
    try:
        yield
    finally:
        matplotlib.use = original_use


@contextlib.contextmanager
def _tkinter_stub_if_missing():
    """缺少 tkinter 时注入占位模块，让原脚本的非界面部分仍可复用。"""

    if _tkinter_available():
        yield
        return

    created = {}
    names = (
        "tkinter",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "tkinter.ttk",
        "matplotlib.backends.backend_tkagg",
    )
    try:
        for name in names:
            if name in sys.modules:
                continue
            stub = types.ModuleType(name)
            stub.__getattr__ = lambda attr: _StubObject()  # noqa: E731
            sys.modules[name] = stub
            created[name] = stub
        tkinter_stub = sys.modules.get("tkinter")
        if tkinter_stub is not None:
            for sub in ("filedialog", "messagebox", "ttk"):
                submodule = sys.modules.get("tkinter." + sub)
                if submodule is not None:
                    setattr(tkinter_stub, sub, submodule)
        yield
    finally:
        for name, stub in created.items():
            if sys.modules.get(name) is stub:
                sys.modules.pop(name, None)


def _verify_module(module) -> None:
    """检查原脚本是否包含插件需要的成员，缺任何一项都直接报错。"""

    missing = [
        name
        for name in _REQUIRED_FUNCTIONS + _REQUIRED_CLASSES
        if not hasattr(module, name)
    ]
    if missing:
        raise AttributeError("原始脚本缺少所需成员：" + "、".join(missing))


def load_source_module():
    """按文件路径加载原始脚本；只导出其中的函数/数据类，原文件不被修改。"""

    global _source_module
    with _source_lock:
        if _source_module is not None:
            return _source_module
        if not SOURCE_SCRIPT.is_file():
            raise FileNotFoundError(f"找不到原始脚本：{SOURCE_SCRIPT}")
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, SOURCE_SCRIPT)
        if spec is None or spec.loader is None:
            raise ImportError("无法创建原始脚本的模块加载器")
        module = importlib.util.module_from_spec(spec)
        previous = sys.modules.get(_MODULE_NAME)
        sys.modules[_MODULE_NAME] = module
        try:
            with _keep_qt_backend():
                with _tkinter_stub_if_missing():
                    spec.loader.exec_module(module)
            _verify_module(module)
        except Exception:
            if previous is not None:
                sys.modules[_MODULE_NAME] = previous
            else:
                sys.modules.pop(_MODULE_NAME, None)
            raise
        _source_module = module
        return module


def _text_constant(module, name: str, fallback: str) -> str:
    """读取原脚本里的文本常量，缺失时用兜底值。"""

    value = getattr(module, name, None) if module is not None else None
    return value if isinstance(value, str) and value else fallback


class ParseCancelled(Exception):
    """解析被主动中止（页面关闭或重新解析）。"""


def _guarded(action: str):
    """把槽函数里的异常转成界面提示。

    PyQt6 中槽函数抛出未捕获异常会调用 ``qFatal`` 直接终止整个平台，
    因此绘图/交互槽必须自己兜住异常（日志内容异常时只提示、不崩溃）。

    信号可能附带参数（``QPushButton.clicked`` 带 checked、
    ``currentIndexChanged`` 带 index），而部分槽不接收这些参数；
    这里按原函数签名裁剪，避免
    ``TypeError: takes 1 positional argument but 2 were given``。
    """

    def decorator(func):
        try:
            parameters = list(inspect.signature(func).parameters.values())[1:]
            accepts_varargs = any(
                parameter.kind
                in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
                for parameter in parameters
            )
            positional_limit = sum(
                1
                for parameter in parameters
                if parameter.kind
                in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            )
            keyword_names = {
                parameter.name
                for parameter in parameters
                if parameter.kind
                in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            }
        except (TypeError, ValueError):
            accepts_varargs = True
            positional_limit = 0
            keyword_names = set()

        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            if not accepts_varargs:
                args = args[:positional_limit]
                kwargs = {key: value for key, value in kwargs.items() if key in keyword_names}
            try:
                return func(self, *args, **kwargs)
            except Exception:  # noqa: BLE001 - 兜底只为提示，不吞掉信息
                detail = traceback.format_exc()
                try:
                    self.status_label.setText(f"{action}时发生异常")
                    self._set_stats_text(
                        [(f"{action}时发生异常", "warn"), ("", "")]
                        + [(line, "") for line in detail.splitlines()]
                    )
                    self.right_tabs.setCurrentWidget(self.stats_page)
                    self.context.show_status(f"网床日志分析：{action}异常", 8000)
                except Exception:  # noqa: BLE001 - 提示本身失败也不再向外抛
                    pass
                return None

        return wrapper

    return decorator


class ParseWorker(QThread):
    """在后台线程执行原脚本的 ``parse_log`` 与 ``compute_stats``。

    进度通过 ``progressed`` 信号回到界面线程，不使用队列轮询。
    """

    progressed = pyqtSignal(int, int)
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, log_path: Path, mesh_tag: str, print_tag: str, parent=None) -> None:
        super().__init__(parent)
        self._log_path = Path(log_path)
        self._mesh_tag = mesh_tag
        self._print_tag = print_tag

    def run(self) -> None:
        try:
            module = load_source_module()
            meshes, prints, raw_count = module.parse_log(
                self._log_path,
                self._mesh_tag,
                self._print_tag,
                progress=self._report_progress,
            )
            stats = [module.compute_stats(mesh) for mesh in meshes]
            result = module.ParseResult(self._log_path, meshes, prints, raw_count, stats)
        except ParseCancelled:
            return
        except Exception:  # noqa: BLE001 - 完整堆栈回传界面比静默失败有用
            self.failed.emit(traceback.format_exc())
            return
        if self.isInterruptionRequested():
            return
        self.succeeded.emit(result)

    def _report_progress(self, done: int, total: int) -> None:
        """原脚本每 5000 行回调一次；顺便作为取消检查点。"""

        if self.isInterruptionRequested():
            raise ParseCancelled()
        self.progressed.emit(int(done), int(total))


# --------------------------------------------------------------------------- #
# 平台内嵌界面
# --------------------------------------------------------------------------- #


class MeshLogAnalyzerWidget(QWidget):
    """网床日志分析页面。"""

    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context

        # 解析结果（结构与原脚本 MeshApp 一致）
        self.meshes = []
        self.prints = []
        self.stats = []
        self.current_index = None

        self._module = None
        self._load_error = ""
        self._worker: ParseWorker | None = None
        self._syncing = False

        self._mesh_tag_default = _FALLBACK_MESH_TAG
        self._print_tag_default = _FALLBACK_PRINT_TAG
        self.baseline_first = _FALLBACK_BASELINE_FIRST
        self.baseline_prev = _FALLBACK_BASELINE_PREV

        self._load_module()
        self._build_ui()

    # ---------------- 模块与常量 ----------------

    def _load_module(self) -> None:
        """尽早加载原脚本，取得默认 tag 等常量；失败时记录原因供界面提示。"""

        try:
            module = load_source_module()
        except Exception as exc:  # noqa: BLE001 - 界面必须能显示失败原因
            self._load_error = f"{type(exc).__name__}: {exc}"
            return
        self._module = module
        self._mesh_tag_default = _text_constant(
            module, "DEFAULT_MESH_TAG", _FALLBACK_MESH_TAG
        )
        self._print_tag_default = _text_constant(
            module, "DEFAULT_PRINT_TAG", _FALLBACK_PRINT_TAG
        )
        self.baseline_first = _text_constant(
            module, "BASELINE_FIRST", _FALLBACK_BASELINE_FIRST
        )
        self.baseline_prev = _text_constant(
            module, "BASELINE_PREV", _FALLBACK_BASELINE_PREV
        )

    def _default_export_name(self) -> str:
        candidate = getattr(self._module, "DEFAULT_EXPORT", None)
        if isinstance(candidate, Path) and candidate.name:
            return candidate.name
        return _FALLBACK_EXPORT_NAME

    # ---------------- 界面搭建 ----------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 10, 16, 10)
        root.setSpacing(6)

        title = QLabel("网床日志分析")
        title.setObjectName("pluginTitle")
        root.addWidget(title)

        description = QLabel(
            "复用 网床研究 目录下原始脚本的解析与统计逻辑（原文件不改动），"
            "在平台内查看网床热力图、相对基准差分与统计详情，并可导出 CSV。"
        )
        description.setObjectName("pluginDescription")
        description.setWordWrap(True)
        root.addWidget(description)

        if self._load_error:
            warning = QLabel(
                f"未能加载原始脚本 {SOURCE_SCRIPT}：{self._load_error}\n"
                "请确认路径，或设置环境变量 TOOLPLATFORM_WANGCHUANG_DIR 后重启平台。"
            )
            warning.setObjectName("pluginDescription")
            warning.setWordWrap(True)
            warning.setStyleSheet("color: #b00000;")
            root.addWidget(warning)

        root.addLayout(self._build_path_row())
        root.addLayout(self._build_tag_rows())
        root.addWidget(self._build_splitter(), 1)
        root.addLayout(self._build_status_row())

        # 初始占位内容
        self._draw_placeholder(
            self.mesh_figure,
            self.mesh_canvas,
            "选择日志并点击“开始解析”后显示网床热力图",
        )
        self._draw_placeholder(
            self.diff_figure,
            self.diff_canvas,
            "请先选择一条网床记录",
        )
        self.stats_view.setPlaceholderText("解析日志后，这里显示所选网床的统计详情。")

        if self._module is None:
            self.parse_button.setEnabled(False)
            self.status_label.setText("原始脚本不可用，无法解析。")
        else:
            self.status_label.setText("就绪")

    def _build_path_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        caption = QLabel("日志文件")
        caption.setMinimumWidth(64)
        row.addWidget(caption)

        self.log_edit = QLineEdit()
        self.log_edit.setPlaceholderText("选择或填写 klippy 日志路径…")
        self.log_edit.returnPressed.connect(self.start_parse)
        row.addWidget(self.log_edit, 1)

        self.browse_button = QPushButton("浏览…")
        self.browse_button.clicked.connect(self._browse)
        row.addWidget(self.browse_button)

        self.parse_button = QPushButton("开始解析")
        self.parse_button.clicked.connect(self.start_parse)
        self.parse_button.setDefault(True)
        row.addWidget(self.parse_button)

        # 若原脚本里的默认日志确实存在，直接填好路径（与原脚本行为一致）
        default_log = getattr(self._module, "DEFAULT_LOG", None)
        if isinstance(default_log, str) and default_log.strip():
            try:
                if Path(default_log).is_file():
                    self.log_edit.setText(default_log)
            except OSError:
                pass
        return row

    def _build_tag_rows(self) -> QVBoxLayout:
        wrapper = QVBoxLayout()
        wrapper.setSpacing(4)

        mesh_row = QHBoxLayout()
        mesh_row.setSpacing(6)
        mesh_caption = QLabel("网床 tag")
        mesh_caption.setMinimumWidth(64)
        mesh_row.addWidget(mesh_caption)
        self.mesh_tag_edit = QLineEdit(self._mesh_tag_default)
        self.mesh_tag_edit.setClearButtonEnabled(True)
        mesh_row.addWidget(self.mesh_tag_edit, 1)
        hint = QLabel("数字片段自动容错（:390 可匹配任意行号）")
        hint.setObjectName("hintLabel")
        mesh_row.addWidget(hint)
        wrapper.addLayout(mesh_row)

        print_row = QHBoxLayout()
        print_row.setSpacing(6)
        print_caption = QLabel("打印 tag")
        print_caption.setMinimumWidth(64)
        print_row.addWidget(print_caption)
        self.print_tag_edit = QLineEdit(self._print_tag_default)
        self.print_tag_edit.setClearButtonEnabled(True)
        print_row.addWidget(self.print_tag_edit, 1)
        wrapper.addLayout(print_row)
        return wrapper

    def _build_splitter(self) -> QSplitter:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 6)
        splitter.setStretchFactor(1, 7)
        # 左侧初始宽度要够放下“打印模型”列，之后仍可拖动分隔条
        splitter.setSizes([560, 680])
        return splitter

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.left_tabs = QTabWidget()

        prints_page = QWidget()
        prints_layout = QVBoxLayout(prints_page)
        prints_layout.setContentsMargins(4, 6, 4, 4)
        prints_layout.setSpacing(3)
        note = QLabel("间隔 = 打印开始 − 网床保存时间（秒），越大说明用的网床越旧")
        note.setObjectName("hintLabel")
        note.setWordWrap(True)
        prints_layout.addWidget(note)
        self.prints_table = QTableWidget(0, 5)
        self.prints_table.setHorizontalHeaderLabels(
            ["序号", "打印开始", "网床", "间隔(秒)", "打印模型"]
        )
        self._configure_table(self.prints_table, (40, 92, 48, 84, 180))
        self.prints_table.itemSelectionChanged.connect(self._on_print_select)
        prints_layout.addWidget(self.prints_table, 1)
        self.left_tabs.addTab(prints_page, "打印任务")

        mesh_page = QWidget()
        mesh_layout = QVBoxLayout(mesh_page)
        mesh_layout.setContentsMargins(4, 6, 4, 4)
        mesh_layout.setSpacing(3)
        self.mesh_table = QTableWidget(0, 5)
        self.mesh_table.setHorizontalHeaderLabels(
            ["序号", "保存时间", "均值", "极差", "使用"]
        )
        self._configure_table(self.mesh_table, (46, 100, 78, 78, 56))
        self.mesh_table.itemSelectionChanged.connect(self._on_mesh_select)
        mesh_layout.addWidget(self.mesh_table, 1)
        self.left_tabs.addTab(mesh_page, "网床记录")

        layout.addWidget(self.left_tabs)
        return panel

    @staticmethod
    def _configure_table(table: QTableWidget, widths: tuple[int, ...]) -> None:
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.setShowGrid(True)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setMinimumSectionSize(32)
        for column, width in enumerate(widths):
            table.setColumnWidth(column, width)

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.right_tabs = QTabWidget()

        # 网床热力图
        heat_page = QWidget()
        heat_layout = QVBoxLayout(heat_page)
        heat_layout.setContentsMargins(4, 6, 4, 4)
        heat_layout.setSpacing(2)
        self.mesh_figure = Figure(figsize=(7.5, 5.4), dpi=100, constrained_layout=True)
        self.mesh_canvas = FigureCanvasQTAgg(self.mesh_figure)
        self.mesh_canvas.setMinimumSize(320, 240)
        heat_layout.addWidget(self.mesh_canvas, 1)
        self.mesh_toolbar = NavigationToolbar2QT(self.mesh_canvas, heat_page)
        heat_layout.addWidget(self.mesh_toolbar)
        self.right_tabs.addTab(heat_page, "网床热力图")

        # 相对基准差分
        diff_page = QWidget()
        diff_layout = QVBoxLayout(diff_page)
        diff_layout.setContentsMargins(4, 6, 4, 4)
        diff_layout.setSpacing(2)
        base_row = QHBoxLayout()
        base_row.setSpacing(6)
        base_row.addWidget(QLabel("对比基准"))
        self.baseline_combo = QComboBox()
        self.baseline_combo.setMinimumWidth(200)
        self.baseline_combo.addItem(self.baseline_first)
        self.baseline_combo.currentIndexChanged.connect(self._refresh_diff)
        base_row.addWidget(self.baseline_combo, 1)
        base_row.addStretch(1)
        diff_layout.addLayout(base_row)
        self.diff_figure = Figure(figsize=(7.5, 4.8), dpi=100, constrained_layout=True)
        self.diff_canvas = FigureCanvasQTAgg(self.diff_figure)
        self.diff_canvas.setMinimumSize(320, 220)
        diff_layout.addWidget(self.diff_canvas, 1)
        self.diff_toolbar = NavigationToolbar2QT(self.diff_canvas, diff_page)
        diff_layout.addWidget(self.diff_toolbar)
        self.right_tabs.addTab(diff_page, "相对基准差分")

        # 统计详情
        stats_page = QWidget()
        stats_layout = QVBoxLayout(stats_page)
        stats_layout.setContentsMargins(4, 6, 4, 4)
        stats_layout.setSpacing(2)
        self.stats_view = QTextEdit()
        self.stats_view.setReadOnly(True)
        self.stats_view.setFont(QFont("Microsoft YaHei", 10))
        stats_layout.addWidget(self.stats_view, 1)
        self.stats_page = stats_page
        self.right_tabs.addTab(stats_page, "统计详情")

        layout.addWidget(self.right_tabs)
        return panel

    def _build_status_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self.status_label = QLabel("就绪")
        self.status_label.setWordWrap(True)
        row.addWidget(self.status_label, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setMaximumWidth(240)
        self.progress.setFormat("%p%")
        row.addWidget(self.progress)

        self.export_button = QPushButton("导出 CSV")
        self.export_button.clicked.connect(self.export_csv)
        row.addWidget(self.export_button)
        return row

    # ---------------- 解析流程 ----------------

    @_guarded("打开文件对话框")
    def _browse(self) -> None:
        text = self.log_edit.text().strip()
        initial = Path(text) if text else SOURCE_DIR / "klippy.log"
        directory = initial.parent
        if not directory.is_dir():
            directory = SOURCE_DIR if SOURCE_DIR.is_dir() else Path.home()
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "选择 klippy 日志",
            str(directory),
            "日志文件 (*.log*);;所有文件 (*.*)",
        )
        if chosen:
            self.log_edit.setText(chosen)

    @_guarded("启动解析")
    def start_parse(self) -> None:
        """启动后台解析线程（对应原脚本 start_parse）。"""

        if self._module is None:
            self.status_label.setText("原始脚本不可用，无法解析。")
            self.context.show_status("网床日志分析：原始脚本不可用", 8000)
            return
        if self._worker is not None:
            return

        text = self.log_edit.text().strip()
        if not text:
            self.status_label.setText("请先选择日志文件。")
            QMessageBox.warning(self, "缺少日志", "请先选择 klippy 日志文件。")
            return
        path = Path(text)
        if not path.is_file():
            self.status_label.setText("找不到日志：%s" % path)
            QMessageBox.critical(self, "找不到日志", "日志文件不存在：\n%s" % path)
            return

        self.progress.setValue(0)
        self.status_label.setText("正在解析 %s ..." % path.name)
        self.parse_button.setEnabled(False)

        worker = ParseWorker(
            path,
            self.mesh_tag_edit.text(),
            self.print_tag_edit.text(),
            self,
        )
        worker.progressed.connect(self._on_progress)
        worker.succeeded.connect(self._on_parse_succeeded)
        worker.failed.connect(self._on_parse_failed)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress.setValue(int(min(100, done * 100.0 / total)))

    def _on_worker_finished(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
        self.parse_button.setEnabled(self._module is not None)

    @_guarded("刷新解析结果")
    def _on_parse_succeeded(self, result) -> None:
        self.progress.setValue(100)
        self._apply_result(result)

    @_guarded("显示解析错误")
    def _on_parse_failed(self, message: str) -> None:
        self.status_label.setText("解析失败")
        self.progress.setValue(0)
        self._set_stats_text(
            [("解析失败", "warn"), ("", "")]
            + [(line, "") for line in str(message).splitlines()]
        )
        self.right_tabs.setCurrentWidget(self.stats_page)
        self.context.show_status("网床日志分析：解析失败", 8000)
        QMessageBox.critical(self, "解析失败", str(message))

    def _apply_result(self, result) -> None:
        """把解析结果填进两个列表并刷新右侧（对应原脚本 _apply_result）。"""

        module = self._module
        self.meshes = list(result.meshes)
        self.prints = list(result.prints)
        self.stats = list(result.stats)
        self.current_index = None

        self.mesh_table.blockSignals(True)
        self.prints_table.blockSignals(True)
        try:
            self.mesh_table.clearContents()
            self.prints_table.clearContents()
            self.mesh_table.setRowCount(len(self.meshes))
            self.prints_table.setRowCount(len(self.prints))

            for row, mesh in enumerate(self.meshes):
                stat = self.stats[row]
                cells = (
                    "#%d" % (row + 1),
                    mesh.clock,
                    "%+.4f" % stat.mean,
                    "%.4f" % stat.span,
                    str(mesh.usage_count),
                )
                for column, text in enumerate(cells):
                    self.mesh_table.setItem(row, column, self._cell(text))

            for row, event in enumerate(self.prints):
                if event.mesh_index is None:
                    mesh_text, gap_text = "无", "-"
                else:
                    mesh = self.meshes[event.mesh_index]
                    gap_text = "%.1f" % (
                        event.timestamp - mesh.timestamp
                    ).total_seconds()
                    mesh_text = "#%d" % (event.mesh_index + 1)
                cells = (
                    "#%d" % (row + 1),
                    event.clock,
                    mesh_text,
                    gap_text,
                    module.model_name(event.filename, 30),
                )
                for column, text in enumerate(cells):
                    alignment = (
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                        if column == len(cells) - 1
                        else Qt.AlignmentFlag.AlignCenter
                    )
                    self.prints_table.setItem(row, column, self._cell(text, alignment))
        finally:
            self.mesh_table.blockSignals(False)
            self.prints_table.blockSignals(False)

        self.mesh_table.scrollToTop()
        self.prints_table.scrollToTop()
        self._fill_baseline_combo()

        message = (
            "解析完成：%s ｜ 网床 %d 次（原始 %d 个 points 块，合并重复 %d 个）｜ 打印 %d 次"
            % (
                result.path.name,
                len(self.meshes),
                result.raw_mesh_count,
                result.raw_mesh_count - len(self.meshes),
                len(self.prints),
            )
        )
        self.status_label.setText(message)
        self.context.show_status(message, 8000)

        if self.prints:
            self._select_table_row(self.prints_table, 0)
            self._show_mesh(self.prints[0].mesh_index, source=self.prints[0])
        elif self.meshes:
            self._show_mesh(0)

    def _fill_baseline_combo(self) -> None:
        values = [self.baseline_first, self.baseline_prev]
        for index, mesh in enumerate(self.meshes, start=1):
            values.append("#%d %s" % (index, mesh.clock))
        blocked = self.baseline_combo.blockSignals(True)
        try:
            self.baseline_combo.clear()
            self.baseline_combo.addItems(values)
            self.baseline_combo.setCurrentIndex(0)
        finally:
            self.baseline_combo.blockSignals(blocked)

    @staticmethod
    def _cell(text: str, alignment=Qt.AlignmentFlag.AlignCenter) -> QTableWidgetItem:
        item = QTableWidgetItem(str(text))
        item.setTextAlignment(alignment)
        return item

    @staticmethod
    def _select_table_row(table: QTableWidget, row: int) -> None:
        """程序化选中一行；临时屏蔽信号，避免与用户选择形成回环。"""

        if not 0 <= row < table.rowCount():
            return
        blocked = table.blockSignals(True)
        try:
            table.selectRow(row)
            item = table.item(row, 0)
            if item is not None:
                table.scrollToItem(item)
        finally:
            table.blockSignals(blocked)

    # ---------------- 选择与绘图 ----------------

    @_guarded("显示打印任务")
    def _on_print_select(self) -> None:
        rows = self.prints_table.selectionModel().selectedRows()
        if not rows:
            return
        index = rows[0].row()
        if not 0 <= index < len(self.prints):
            return
        event = self.prints[index]
        self._show_mesh(event.mesh_index, source=event)

    @_guarded("显示网床记录")
    def _on_mesh_select(self) -> None:
        if self._syncing:
            return
        rows = self.mesh_table.selectionModel().selectedRows()
        if not rows:
            return
        index = rows[0].row()
        # 若已经显示的是这条网床，直接返回，避免重绘循环
        if index == self.current_index:
            return
        if 0 <= index < len(self.meshes):
            self._show_mesh(index)

    @_guarded("绘制网床详情")
    def _show_mesh(self, index, source=None) -> None:
        """显示指定网床（对应原脚本 _show_mesh）。"""

        if index is None or not 0 <= index < len(self.meshes):
            self._draw_placeholder(
                self.mesh_figure,
                self.mesh_canvas,
                "该次打印之前没有任何网床记录",
                "#b00000",
                13,
            )
            if source is not None:
                self._set_stats_text(self._print_lines(source, None))
            return

        self.current_index = index
        mesh = self.meshes[index]
        stat = self.stats[index]

        self._syncing = True
        try:
            self._select_table_row(self.mesh_table, index)
        finally:
            self._syncing = False

        self._draw_mesh_plot(mesh, stat)
        self._refresh_diff()
        self._set_stats_text(self._print_lines(source, index))

    @staticmethod
    def _draw_placeholder(
        figure: Figure,
        canvas: FigureCanvasQTAgg,
        text: str,
        color: str = "#666666",
        fontsize: float = 12,
    ) -> None:
        figure.clear()
        ax = figure.add_subplot(111)
        ax.text(
            0.5,
            0.5,
            text,
            ha="center",
            va="center",
            fontsize=fontsize,
            color=color,
            transform=ax.transAxes,
        )
        ax.set_axis_off()
        canvas.draw_idle()

    def _draw_mesh_plot(self, mesh, stat) -> None:
        """网床热力图（复用原 mesh_coordinates / grid_extent / annotate_cells）。"""

        module = self._module
        figure = self.mesh_figure
        figure.clear()
        ax = figure.add_subplot(111)

        z = mesh.points
        rows, cols = z.shape
        xs, ys, physical = module.mesh_coordinates(mesh)
        extent = module.grid_extent(xs, ys)

        limit = max(abs(stat.vmax - stat.mean), abs(stat.vmin - stat.mean), 1e-9)
        image = ax.imshow(
            z,
            origin="lower",
            extent=extent,
            cmap="coolwarm",
            vmin=stat.mean - limit,
            vmax=stat.mean + limit,
            aspect="auto",
            interpolation="nearest",
        )
        module.annotate_cells(ax, z, xs, ys, image)

        number = (self.current_index + 1) if self.current_index is not None else 0
        ax.set_title(
            "网床 #%d   %s   %d×%d   单位：μm   均值 %.4f mm"
            % (number, mesh.stamp, rows, cols, stat.mean),
            fontsize=11,
        )
        ax.set_xlabel("X (mm)" if physical else "X 列索引")
        ax.set_ylabel("Y (mm)" if physical else "Y 行索引")
        ax.set_xticks(np.round(xs, 1))
        ax.set_yticks(np.round(ys, 1))
        ax.tick_params(labelsize=7)
        figure.colorbar(image, ax=ax, fraction=0.046, pad=0.03, label="高度 (mm)")
        self.mesh_canvas.draw_idle()

    def _baseline_index(self):
        """按“对比基准”下拉框算出基准序号（对应原脚本 _baseline_index）。"""

        if self.current_index is None or not self.meshes:
            return None
        text = self.baseline_combo.currentText()
        if text == self.baseline_first:
            return 0
        if text == self.baseline_prev:
            return self.current_index - 1 if self.current_index > 0 else None
        match = _BASELINE_INDEX_RE.match(text)
        if match is None:
            return None
        index = int(match.group(1)) - 1
        return index if 0 <= index < len(self.meshes) else None

    @_guarded("绘制差分图")
    def _refresh_diff(self, *_args) -> None:
        """相对基准差分图（对应原脚本 _refresh_diff）。"""

        module = self._module
        figure = self.diff_figure
        figure.clear()
        ax = figure.add_subplot(111)

        if self.current_index is None or self._module is None:
            ax.text(
                0.5,
                0.5,
                "请先选择一条网床记录",
                ha="center",
                va="center",
                transform=ax.transAxes,
                fontsize=12,
                color="#666666",
            )
            ax.set_axis_off()
            self.diff_canvas.draw_idle()
            return

        base_index = self._baseline_index()
        mesh = self.meshes[self.current_index]

        if base_index is None:
            ax.text(
                0.5,
                0.5,
                "没有可对比的基准网床",
                ha="center",
                va="center",
                transform=ax.transAxes,
                fontsize=12,
                color="#b00000",
            )
            ax.set_axis_off()
            self.diff_canvas.draw_idle()
            return

        if base_index == self.current_index:
            ax.text(
                0.5,
                0.5,
                "当前网床就是基准本身，换一条网床或修改“对比基准”后可见差分",
                ha="center",
                va="center",
                transform=ax.transAxes,
                fontsize=11,
                color="#666666",
            )
            ax.set_axis_off()
            self.diff_canvas.draw_idle()
            return

        base = self.meshes[base_index]
        if base.points.shape != mesh.points.shape:
            ax.text(
                0.5,
                0.5,
                "两次网床尺寸不同，无法逐点比较（%dx%d 对 %dx%d）"
                % (mesh.rows, mesh.cols, base.rows, base.cols),
                ha="center",
                va="center",
                transform=ax.transAxes,
                fontsize=11,
                color="#b00000",
            )
            ax.set_axis_off()
            self.diff_canvas.draw_idle()
            return

        diff = mesh.points - base.points
        limit = float(np.abs(diff).max()) or 1e-9
        xs, ys, physical = module.mesh_coordinates(mesh)
        extent = module.grid_extent(xs, ys)

        image = ax.imshow(
            diff,
            origin="lower",
            extent=extent,
            cmap="RdBu_r",
            vmin=-limit,
            vmax=limit,
            aspect="auto",
            interpolation="nearest",
        )
        module.annotate_cells(ax, diff, xs, ys, image)

        ax.set_title(
            "网床 #%d − 基准 #%d   最大差 %.4f mm   均值差 %+.4f mm   RMS %.4f mm"
            % (
                self.current_index + 1,
                base_index + 1,
                float(np.abs(diff).max()),
                float(diff.mean()),
                float(np.sqrt(np.mean(diff**2))),
            ),
            fontsize=11,
        )
        ax.set_xlabel("X (mm)" if physical else "X 列索引")
        ax.set_ylabel("Y (mm)" if physical else "Y 行索引")
        ax.set_xticks(np.round(xs, 1))
        ax.set_yticks(np.round(ys, 1))
        ax.tick_params(labelsize=7)
        figure.colorbar(image, ax=ax, fraction=0.046, pad=0.03, label="差值 (mm)")
        self.diff_canvas.draw_idle()

    # ---------------- 文本详情 ----------------

    def _print_lines(self, source, index):
        """生成统计详情文本（(text, tag) 列表），与原脚本 _print_lines 一致。"""

        module = self._module
        lines = []

        if source is not None:
            lines.append(("打印任务", "title"))
            lines.append(("", ""))
            lines.append(("  打印开始时间：%s" % source.stamp, ""))
            lines.append(("  打印模型：%s" % module.model_name(source.filename, 0), "head"))
            lines.append(("  G-code 路径：%s" % (source.filename or "（未记录）"), ""))
            lines.append(("  日志行号：%d" % source.line_no, ""))
            lines.append(("", ""))

        if index is None or not 0 <= index < len(self.meshes):
            lines.append(("本次打印之前没有任何网床记录", "warn"))
            lines.append(("", ""))
            lines.append(("可能原因：", "head"))
            lines.append(("  1. 该次打印发生在本次日志记录范围之前；", ""))
            lines.append(("  2. 日志中没有出现 save_config 的 bed_mesh 保存记录；", ""))
            lines.append(("  3. 网床 tag 与实际日志格式不一致。", ""))
            return lines

        mesh = self.meshes[index]
        stat = self.stats[index]
        number = index + 1

        if source is not None:
            gap = (source.timestamp - mesh.timestamp).total_seconds()
            lines.append(("本次打印使用的网床", "title"))
            lines.append(("", ""))
            lines.append(("  网床序号：#%d" % number, "head"))
            lines.append(("  保存时间：%s" % mesh.stamp, ""))
            lines.append(("  网床保存于打印开始前 %.1f 秒" % gap, ""))
            if mesh.usage_count > 1:
                lines.append(
                    (
                        "  该网床共被 %d 次打印使用（日志中相邻的几次打印共用同一份网床）"
                        % mesh.usage_count,
                        "",
                    )
                )
            if mesh.usage_count == 0:
                lines.append(("  注意：该网床未匹配到任何打印", "warn"))
        else:
            lines.append(("网床记录 #%d" % number, "title"))
            lines.append(("", ""))
            lines.append(("  保存时间：%s" % mesh.stamp, "head"))
            lines.append(("  日志行号：%d" % mesh.line_no, ""))

        lines.append(("", ""))
        lines.append(("网床数据", "head"))
        lines.append(("", ""))
        lines.append(("  网格尺寸：%d 行 × %d 列" % (mesh.rows, mesh.cols), ""))
        if mesh.meta.get("min_x") and mesh.meta.get("max_x"):
            lines.append(
                (
                    "  测量范围：X %s ~ %s mm ／ Y %s ~ %s mm"
                    % (
                        mesh.meta.get("min_x"),
                        mesh.meta.get("max_x"),
                        mesh.meta.get("min_y", "?"),
                        mesh.meta.get("max_y", "?"),
                    ),
                    "",
                )
            )
        if mesh.meta.get("algo"):
            lines.append(
                (
                    "  插值算法：%s    张力：%s"
                    % (mesh.meta.get("algo"), mesh.meta.get("tension", "?")),
                    "",
                )
            )
        lines.append(("  最低点：%+.6f mm" % stat.vmin, ""))
        lines.append(("  最高点：%+.6f mm" % stat.vmax, ""))
        lines.append(("  平均值：%+.6f mm" % stat.mean, ""))
        lines.append(("  极差（峰谷差）：%.6f mm" % stat.span, ""))
        lines.append(("  标准差：%.6f mm" % stat.std, ""))
        lines.append(("  RMS（相对零点）：%.6f mm" % stat.rms, ""))

        lines.append(("", ""))
        lines.append(("形貌分析", "head"))
        lines.append(("", ""))
        if stat.tilt_x is not None and stat.tilt_y is not None:
            lines.append(
                (
                    "  平面拟合倾斜：X 方向 %+.4f mm/100mm（约 %.3f°），"
                    "Y 方向 %+.4f mm/100mm（约 %.3f°）"
                    % (
                        stat.tilt_x,
                        np.degrees(np.arctan(stat.tilt_x / 100.0)),
                        stat.tilt_y,
                        np.degrees(np.arctan(stat.tilt_y / 100.0)),
                    ),
                    "",
                )
            )
        if stat.residual_rms is not None:
            share = (stat.residual_rms / stat.std * 100.0) if stat.std > 1e-12 else 0.0
            lines.append(
                (
                    "  去除平面后的残差 RMS：%.6f mm（占整体标准差 %.1f%%）"
                    % (stat.residual_rms, share),
                    "",
                )
            )
        if stat.peak is not None:
            lines.append(
                (
                    "  最凸点：X=%.1f Y=%.1f（%s）  z=%+.6f mm"
                    % (stat.peak[0], stat.peak[1], stat.axis_unit, stat.peak[2]),
                    "",
                )
            )
        if stat.pit is not None:
            lines.append(
                (
                    "  最凹点：X=%.1f Y=%.1f（%s）  z=%+.6f mm"
                    % (stat.pit[0], stat.pit[1], stat.axis_unit, stat.pit[2]),
                    "",
                )
            )

        base_index = self._baseline_index()
        if base_index is not None and base_index != index:
            base = self.meshes[base_index]
            if base.points.shape == mesh.points.shape:
                diff = mesh.points - base.points
                lines.append(("", ""))
                lines.append(
                    ("相对基准 #%d（%s）的差异" % (base_index + 1, base.clock), "head")
                )
                lines.append(("", ""))
                lines.append(("  平均差：%+.6f mm" % float(diff.mean()), ""))
                lines.append(("  最大绝对差：%.6f mm" % float(np.abs(diff).max()), ""))
                lines.append(
                    ("  差分 RMS：%.6f mm" % float(np.sqrt(np.mean(diff**2))), "")
                )
                lines.append(
                    (
                        "  相对基准的整体抬升/下沉：%s %.6f mm"
                        % (
                            "抬升" if diff.mean() >= 0 else "下沉",
                            abs(float(diff.mean())),
                        ),
                        "",
                    )
                )

        lines.append(("", ""))
        lines.append(
            ("  该网床在日志中被保存 %d 次（重复保存已合并）" % mesh.dup_count, "")
        )
        return lines

    def _set_stats_text(self, lines) -> None:
        """把 (text, tag) 列表写入统计视图，对应原脚本 _set_stats_text。"""

        view = self.stats_view
        view.clear()
        cursor = view.textCursor()
        formats = {
            "": QTextCharFormat(),
            "title": self._char_format(bold=True, size=12),
            "head": self._char_format(bold=True, size=10),
            "warn": self._char_format(color="#b00000"),
        }
        plain = formats[""]
        for text, tag in lines:
            cursor.insertText(text + "\n", formats.get(tag) or plain)
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        view.setTextCursor(cursor)

    @staticmethod
    def _char_format(bold: bool = False, size: float = 0.0, color: str = ""):
        fmt = QTextCharFormat()
        if bold:
            fmt.setFontWeight(QFont.Weight.Bold)
        if size:
            fmt.setFontPointSize(float(size))
        if color:
            fmt.setForeground(QColor(color))
        return fmt

    # ---------------- 导出 ----------------

    @_guarded("导出 CSV")
    def export_csv(self) -> None:
        """选择保存位置并导出 4 类 section（对应原脚本 export_csv）。"""

        if not self.meshes and not self.prints:
            QMessageBox.information(self, "无数据", "请先解析日志。")
            return

        default_path = self.context.data_dir / self._default_export_name()
        try:
            self.context.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            "导出分析结果",
            str(default_path),
            "CSV 文件 (*.csv);;所有文件 (*.*)",
        )
        if not chosen:
            return

        target = Path(chosen)
        try:
            self.write_csv(target)
        except OSError as exc:
            self.status_label.setText("导出失败：%s" % exc)
            QMessageBox.critical(self, "导出失败", str(exc))
            return

        message = "已导出：%s" % target
        self.status_label.setText(message)
        self.context.show_status("网床日志分析：已导出 %s" % target.name, 6000)
        QMessageBox.information(self, "导出完成", "已导出到：\n%s" % target)

    def write_csv(self, target: Path) -> None:
        """写出 4 类 section；供导出与自检共用，不弹窗。"""

        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            self._write_task_section(writer)
            self._write_mesh_section(writer)
            self._write_matrix_section(writer)
            self._write_diff_section(writer)

    def _write_task_section(self, writer) -> None:
        writer.writerow(["# 打印任务（每次打印取开始时间之前最近的一次网床）"])
        writer.writerow(
            [
                "序号",
                "打印开始",
                "打印模型",
                "gcode 文件",
                "网床序号",
                "网床保存时间",
                "间隔(秒)",
                "网床均值(mm)",
                "极差(mm)",
                "标准差(mm)",
                "倾斜X(mm/100mm)",
                "倾斜Y(mm/100mm)",
            ]
        )
        for index, event in enumerate(self.prints, start=1):
            if event.mesh_index is None:
                writer.writerow(
                    [
                        index,
                        event.stamp,
                        event.model,
                        event.filename,
                        "无",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                    ]
                )
                continue
            mesh = self.meshes[event.mesh_index]
            stat = self.stats[event.mesh_index]
            writer.writerow(
                [
                    index,
                    event.stamp,
                    event.model,
                    event.filename,
                    "#%d" % (event.mesh_index + 1),
                    mesh.stamp,
                    "%.1f" % (event.timestamp - mesh.timestamp).total_seconds(),
                    "%.6f" % stat.mean,
                    "%.6f" % stat.span,
                    "%.6f" % stat.std,
                    "" if stat.tilt_x is None else "%.6f" % stat.tilt_x,
                    "" if stat.tilt_y is None else "%.6f" % stat.tilt_y,
                ]
            )
        writer.writerow([])

    def _write_mesh_section(self, writer) -> None:
        writer.writerow(["# 网床记录统计"])
        writer.writerow(
            [
                "序号",
                "保存时间",
                "日志行号",
                "行数",
                "列数",
                "最低(mm)",
                "最高(mm)",
                "均值(mm)",
                "极差(mm)",
                "标准差(mm)",
                "RMS(mm)",
                "平面残差RMS(mm)",
                "倾斜X(mm/100mm)",
                "倾斜Y(mm/100mm)",
                "最凸点",
                "最凹点",
                "重复保存次数",
                "被打印使用次数",
            ]
        )
        for index, (mesh, stat) in enumerate(zip(self.meshes, self.stats), start=1):
            writer.writerow(
                [
                    index,
                    mesh.stamp,
                    mesh.line_no,
                    mesh.rows,
                    mesh.cols,
                    "%.6f" % stat.vmin,
                    "%.6f" % stat.vmax,
                    "%.6f" % stat.mean,
                    "%.6f" % stat.span,
                    "%.6f" % stat.std,
                    "%.6f" % stat.rms,
                    "" if stat.residual_rms is None else "%.6f" % stat.residual_rms,
                    "" if stat.tilt_x is None else "%.6f" % stat.tilt_x,
                    "" if stat.tilt_y is None else "%.6f" % stat.tilt_y,
                    "" if stat.peak is None else "(%.1f, %.1f) %+.6f" % stat.peak,
                    "" if stat.pit is None else "(%.1f, %.1f) %+.6f" % stat.pit,
                    mesh.dup_count,
                    mesh.usage_count,
                ]
            )
        writer.writerow([])

    def _write_matrix_section(self, writer) -> None:
        writer.writerow(["# 网床矩阵（单位 mm）"])
        for index, (mesh, stat) in enumerate(zip(self.meshes, self.stats), start=1):
            writer.writerow(
                [
                    "mesh_%d  %s  均值=%.6f  极差=%.6f"
                    % (index, mesh.stamp, stat.mean, stat.span)
                ]
            )
            writer.writerow(
                ["row%d\\col%d" % (0, col) for col in range(mesh.cols)] + ["均值"]
            )
            for row_index in range(mesh.rows):
                values = ["%.6f" % value for value in mesh.points[row_index]]
                writer.writerow(values + ["%.6f" % float(mesh.points[row_index].mean())])
            writer.writerow([])
        writer.writerow([])

    def _write_diff_section(self, writer) -> None:
        if len(self.meshes) < 2:
            return
        base = self.meshes[0]
        writer.writerow(["# 差分矩阵（相对第一条网床 mesh_1）"])
        for index, mesh in enumerate(self.meshes, start=1):
            if index == 1 or mesh.points.shape != base.points.shape:
                continue
            diff = mesh.points - base.points
            writer.writerow(
                [
                    "mesh_%d_minus_mesh_1  %s  均值差=%+.6f  最大绝对差=%.6f  RMS=%.6f"
                    % (
                        index,
                        mesh.stamp,
                        float(diff.mean()),
                        float(np.abs(diff).max()),
                        float(np.sqrt(np.mean(diff**2))),
                    )
                ]
            )
            writer.writerow(["col%d" % col for col in range(mesh.cols)] + ["均值"])
            for row_index in range(mesh.rows):
                values = ["%.6f" % value for value in diff[row_index]]
                writer.writerow(values + ["%.6f" % float(diff[row_index].mean())])
            writer.writerow([])

    # ---------------- 生命周期 ----------------

    def shutdown(self) -> None:
        """平台退出时停止后台解析线程。"""

        worker = self._worker
        if worker is None or not worker.isRunning():
            return
        worker.requestInterruption()
        # 原脚本的解析循环每 5000 行回调一次进度，那里会响应中断请求
        if worker.wait(4000):
            return
        # 仍未退出（超大日志卡在单次回调之间）：强制结束，
        # 避免 Qt 销毁仍在运行的线程对象时直接崩溃
        worker.terminate()
        worker.wait(1000)

    def deactivate(self) -> None:
        """页面被切走时无需特殊处理（解析线程独立运行，不停止解析）。"""

        return None


def create_plugin(context, parent=None) -> QWidget:
    return MeshLogAnalyzerWidget(context, parent)
