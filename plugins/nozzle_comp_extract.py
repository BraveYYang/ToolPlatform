"""ToolPlatform 插件：提取喷嘴补偿与打印事件。

复用 网床研究 目录下原始脚本中的解析逻辑（按路径动态加载，原文件保持只读不修改），
在平台页面内提供日志选择、事件表格（按事件类型着色）与 CSV 导出。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


PLUGIN_META = {
    "id": "nozzle_comp_extract",
    "name": "喷嘴补偿与打印事件提取",
    "version": "1.2.0",
    "category": "网床研究",
    "description": "解析 Klipper 日志中的喷嘴补偿与开始打印事件，按时间戳排序导出 CSV。",
    "order": 40,
}


# 原始脚本目录：默认指向 网床研究 目录，可用环境变量覆盖；原脚本不被修改
SOURCE_DIR = Path(
    os.environ.get(
        "TOOLPLATFORM_WANGCHUANG_DIR",
        r"E:\2.Python_Project\Project4-tools\网床研究",
    )
)
SOURCE_SCRIPT = SOURCE_DIR / "提取喷嘴补偿与打印事件.py"

# 表格最多显示的条数，避免超大日志把界面撑爆（CSV 仍会完整导出）
MAX_TABLE_ROWS = 5000

COLUMNS = ("时间", "类型", "now_comp", "idle_comp", "target_temp", "G28_temp", "文件", "行号")

# 不同事件类型的行样式：半透明背景在深/浅主题下都能看清
EVENT_STYLES = {
    "NOZZLE_COMP": {
        "background": QColor(70, 130, 180, 50),
        "legend": "#4a90d9",
        "label": "喷嘴补偿",
    },
    "PRINT_START": {
        "background": QColor(60, 160, 90, 55),
        "legend": "#3fa45b",
        "label": "开始打印",
    },
}


def load_source_module():
    """按文件路径加载原始脚本模块；只导入其中的函数，不修改原文件。"""
    if not SOURCE_SCRIPT.is_file():
        raise FileNotFoundError(f"找不到原始脚本：{SOURCE_SCRIPT}")
    spec = importlib.util.spec_from_file_location(
        "toolplatform_wangchuang_nozzle_comp", SOURCE_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise ImportError("无法创建原始脚本的模块加载器")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ExtractWorker(QThread):
    """在后台线程中执行日志解析，避免阻塞界面。"""

    done = pyqtSignal(bool, str, object)  # 成功与否、摘要文本、事件列表

    def __init__(self, input_path: Path, output_path: Path, parent=None) -> None:
        super().__init__(parent)
        self._input = input_path
        self._output = output_path

    def run(self) -> None:
        try:
            module = load_source_module()
            events = module.extract_events(self._input)
            self._output.parent.mkdir(parents=True, exist_ok=True)
            module.write_csv(events, self._output)
            exported = 0
            with self._output.open("r", encoding="utf-8-sig") as handle:
                exported = max(0, sum(1 for _ in handle) - 1)
        except Exception as exc:  # 原始脚本的任何异常都要回传界面
            self.done.emit(False, f"{type(exc).__name__}: {exc}", [])
            return

        nozzle_count = sum(1 for event in events if event.get("event_type") == "NOZZLE_COMP")
        print_count = sum(1 for event in events if event.get("event_type") == "PRINT_START")
        summary = (
            "提取 %d 条事件（喷嘴补偿 %d，开始打印 %d）；"
            "CSV 按原脚本规则导出 %d 行至：%s"
            % (len(events), nozzle_count, print_count, exported, self._output)
        )
        self.done.emit(True, summary, events)


class NozzleCompExtractWidget(QWidget):
    """日志选择 + 输出路径 + 事件表格（按类型着色）。"""

    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        self._worker: ExtractWorker | None = None
        self._build_ui()

    # ------------------------------------------------------------------ 界面

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)

        title = QLabel("喷嘴补偿与打印事件提取")
        title.setObjectName("pluginTitle")
        layout.addWidget(title)

        description = QLabel(
            "复用 网床研究 目录下原始脚本的解析逻辑（原文件不改动），"
            "选择 Klipper 日志后按时间戳排序提取事件；表格按事件类型着色，CSV 导出遵循原脚本的过滤规则。"
        )
        description.setObjectName("pluginDescription")
        description.setWordWrap(True)
        layout.addWidget(description)

        self.input_edit = QLineEdit(str(self._default_input()))
        layout.addLayout(self._path_row("输入日志：", self.input_edit, self._pick_input))

        default_output = self.context.data_dir / "喷嘴补偿与打印事件.csv"
        self.output_edit = QLineEdit(str(default_output))
        layout.addLayout(self._path_row("输出 CSV：", self.output_edit, self._pick_output))

        buttons = QHBoxLayout()
        self.run_button = QPushButton("开始提取")
        self.run_button.clicked.connect(self._run)
        buttons.addWidget(self.run_button)

        self.open_button = QPushButton("打开输出目录")
        self.open_button.clicked.connect(self._open_output_dir)
        buttons.addWidget(self.open_button)
        buttons.addStretch()
        layout.addLayout(buttons)

        self.status_label = QLabel("尚未运行：选择日志后点击「开始提取」。")
        self.status_label.setObjectName("infoLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        legend = QLabel(self._legend_html())
        legend.setObjectName("infoLabel")
        legend.setTextFormat(Qt.TextFormat.RichText)
        legend.setWordWrap(True)
        layout.addWidget(legend)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        for column, width in enumerate((170, 110, 90, 90, 90, 90)):
            self.table.setColumnWidth(column, width)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(7, 70)
        layout.addWidget(self.table, 1)

    def _legend_html(self) -> str:
        pieces = []
        for event_type, style in EVENT_STYLES.items():
            pieces.append(
                '<span style="color:%s">■</span> %s（%s）'
                % (style["legend"], event_type, style["label"])
            )
        return "　　".join(pieces)

    def _path_row(self, caption: str, edit: QLineEdit, picker):
        row = QHBoxLayout()
        label = QLabel(caption)
        label.setMinimumWidth(80)
        row.addWidget(label)
        row.addWidget(edit, 1)
        button = QPushButton("浏览…")
        button.clicked.connect(picker)
        row.addWidget(button)
        return row

    def _default_input(self) -> str:
        """优先沿用原脚本里的默认日志路径（若文件确实存在）。"""
        try:
            module = load_source_module()
            candidate = getattr(module, "DEFAULT_INPUT", None)
            if isinstance(candidate, Path) and candidate.is_file():
                return str(candidate)
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------ 动作

    def _pick_input(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Klipper 日志", str(SOURCE_DIR), "日志文件 (*.log* *.txt);;所有文件 (*.*)"
        )
        if path:
            self.input_edit.setText(path)

    def _pick_output(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "选择输出 CSV", self.output_edit.text(), "CSV 文件 (*.csv)"
        )
        if path:
            self.output_edit.setText(path)

    def _run(self) -> None:
        input_path = Path(self.input_edit.text().strip())
        output_path = Path(self.output_edit.text().strip())
        if not input_path.is_file():
            self._report(False, f"找不到输入日志：{input_path}")
            return
        if not self.output_edit.text().strip():
            self._report(False, "请填写输出 CSV 路径。")
            return
        if self._worker is not None and self._worker.isRunning():
            return

        self.run_button.setEnabled(False)
        self.status_label.setText("正在提取…")
        self._clear_table()

        self._worker = ExtractWorker(input_path, output_path, self)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, ok: bool, message: str, events: object) -> None:
        self.run_button.setEnabled(True)
        if ok and isinstance(events, list):
            self._fill_table(events)
        self._report(ok, message)

    def _report(self, ok: bool, message: str) -> None:
        self.status_label.setText(message)
        self.context.show_status(message.splitlines()[0], 8000)

    # ------------------------------------------------------------------ 表格

    def _clear_table(self) -> None:
        self.table.setSortingEnabled(False)
        self.table.clearContents()
        self.table.setRowCount(0)
        self.table.setSortingEnabled(True)

    def _fill_table(self, events: list) -> None:
        rows = list(events)[:MAX_TABLE_ROWS]
        self.table.setSortingEnabled(False)
        self.table.clearContents()
        self.table.setRowCount(len(rows))

        for row, event in enumerate(rows):
            event_type = str(event.get("event_type") or "")
            style = EVENT_STYLES.get(event_type)
            values = [
                self._format_timestamp(event.get("timestamp")),
                event_type,
                str(event.get("now_comp") or ""),
                str(event.get("idle_comp") or ""),
                str(event.get("target_temp") or ""),
                str(event.get("G28_temp") or ""),
                str(event.get("filename") or ""),
            ]
            raw_line = str(event.get("raw_line") or "")
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if style:
                    item.setBackground(style["background"])
                    if column == 1:
                        item.setForeground(QColor(style["legend"]))
                if raw_line:
                    item.setToolTip(raw_line)
                self.table.setItem(row, column, item)

            line_item = QTableWidgetItem()
            line_item.setData(
                Qt.ItemDataRole.DisplayRole, int(event.get("line_number") or 0)
            )
            if style:
                line_item.setBackground(style["background"])
            if raw_line:
                line_item.setToolTip(raw_line)
            self.table.setItem(row, 7, line_item)

        self.table.setSortingEnabled(True)
        # 默认按时间升序展示（用户仍可点击表头改排序）
        self.table.sortItems(0, Qt.SortOrder.AscendingOrder)
        if len(events) > MAX_TABLE_ROWS:
            self.status_label.setText(
                "%s（表格仅显示前 %d 条，CSV 包含全部）"
                % (self.status_label.text(), MAX_TABLE_ROWS)
            )

    @staticmethod
    def _format_timestamp(value: object) -> str:
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return str(value or "")

    def _open_output_dir(self) -> None:
        output_path = Path(self.output_edit.text().strip())
        directory = output_path.parent if str(output_path) else self.context.data_dir
        if directory.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def shutdown(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.requestInterruption()
            self._worker.wait(3000)


def create_plugin(context, parent=None) -> QWidget:
    return NozzleCompExtractWidget(context, parent)
