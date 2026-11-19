"""
日志曲线绘图插件（双模式）

模式一：Klipper Stats 解析
    专门处理 klippy.log 里形如：
        [INFO] 2026-11-09 10:28:59,143 ... Stats 2692.6: gcodein=0 mcu: ... extruder1: target=0 temp=28.6 pwm=0.000 ...
    的行。提供「组选择（extruder1/mcu/...）」+「字段多选（target/temp/pwm）」，
    以日志时间戳作为 X 轴绘制曲线。

模式二：通用正则匹配
    用户任意上传日志，写一个正则表达式，用命名捕获组 (?P<name>...) 定义
    要抽取的字段。然后选择哪个字段作为 X、哪些字段作为 Y 绘制。

两个模式都通过 PyQt6 + matplotlib 展示。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

try:
    import matplotlib
    matplotlib.use('QtAgg')
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
    from matplotlib.figure import Figure
    import matplotlib.dates as mdates
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False


# ============================================================================
# 插件元数据
# ============================================================================

PLUGIN_META = {
    "id": "log_curve_plotter",
    "name": "日志曲线绘图",
    "version": "1.0.0",
    "category": "调试工具",
    "description": "解析 Klipper Stats 日志绘制参数曲线；支持通用正则抽取自定义字段",
    "order": 20,
}


# ============================================================================
# Klipper Stats 行解析
# ============================================================================

# 一行示例：
#   [INFO] 2026-11-09 10:28:59,143 [root] [statistics_ext:generate_stats:78] Stats 2692.6: gcodein=0 ...
#     mcu: mcu_awake=0.001 ...
#     nozzle_mcu: ... freq=120000084 adj=120000933
#     print_stats: standby
#     extruder: target=0 temp=26.5 pwm=0.000
#     extruder1: target=0 temp=28.6 pwm=0.000

TS_PATTERN = re.compile(r'\[\w+\]\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}),(\d+)')
STATS_MARK = re.compile(r'Stats\s+([\d.]+):')


@dataclass
class StatsRecord:
    """一行 Stats 解析结果：时间 + 多个「group -> {field: value}」"""
    timestamp: datetime
    stats_time: float
    groups: Dict[str, Dict[str, float]] = field(default_factory=dict)


def _parse_stats_line(line: str) -> Optional[StatsRecord]:
    """尝试解析一行 Stats 日志，解析失败返回 None"""
    stats_m = STATS_MARK.search(line)
    if not stats_m:
        return None
    ts_m = TS_PATTERN.search(line)
    if not ts_m:
        return None
    try:
        ts_str = ts_m.group(1) + '.' + ts_m.group(2)
        ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S.%f')
    except Exception:
        return None
    
    stats_time = float(stats_m.group(1))
    
    # 从 Stats xxxx: 后面开始的 payload
    payload = line[stats_m.end():].strip()
    
    # Tokenize：一行是「group: field=value field=value ... group: ...」交织的
    # 识别规则：形如 "name:" 的 token（名字只含字母数字下划线）开启一个新组
    # 组名单独成段，一个组内可能只有「standby」这种非 kv 文本，我们忽略之
    groups: Dict[str, Dict[str, float]] = {}
    tokens = payload.split()
    
    current_group = "default"
    for tok in tokens:
        if tok.endswith(':') and '=' not in tok:
            name = tok[:-1]
            # 合法组名
            if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
                current_group = name
                groups.setdefault(current_group, {})
                continue
        # kv 对
        if '=' in tok:
            k, _, v = tok.partition('=')
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k):
                continue
            try:
                val = float(v)
            except ValueError:
                continue
            groups.setdefault(current_group, {})[k] = val
    
    return StatsRecord(timestamp=ts, stats_time=stats_time, groups=groups)


class StatsParseThread(QThread):
    """后台解析线程，避免大文件阻塞 UI"""
    progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal(list)         # List[StatsRecord]
    failed = pyqtSignal(str)
    
    def __init__(self, filepath: Path, parent=None):
        super().__init__(parent)
        self.filepath = filepath
        self._stop = False
    
    def stop(self):
        self._stop = True
    
    def run(self):
        try:
            records: List[StatsRecord] = []
            total = self.filepath.stat().st_size
            read = 0
            report_every = max(1, total // 50)
            next_report = report_every
            
            with self.filepath.open('r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if self._stop:
                        break
                    read += len(line.encode('utf-8', errors='ignore'))
                    if 'Stats' not in line:
                        pass
                    else:
                        rec = _parse_stats_line(line)
                        if rec:
                            records.append(rec)
                    if read >= next_report:
                        self.progress.emit(min(read, total), total)
                        next_report += report_every
            
            self.progress.emit(total, total)
            self.finished_ok.emit(records)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


# ============================================================================
# Matplotlib 画布
# ============================================================================

class TimeSeriesCanvas(FigureCanvas if HAS_MATPLOTLIB else QWidget):
    """支持多条时间序列叠加显示的画布"""
    
    def __init__(self, parent=None):
        if HAS_MATPLOTLIB:
            self.fig = Figure(figsize=(8, 5), dpi=100)
            super().__init__(self.fig)
            self.setParent(parent)
        else:
            super().__init__(parent)
            layout = QVBoxLayout(self)
            layout.addWidget(QLabel("matplotlib 未安装，无法绘图\n请运行: pip install matplotlib"))
    
    def plot_series(self, series: List[Tuple[str, list, list]],
                    xlabel: str = 'Time', ylabel: str = 'Value',
                    title: str = '',
                    x_is_time: bool = False) -> None:
        """series: List of (label, xs, ys)"""
        if not HAS_MATPLOTLIB:
            return
        self.fig.clear()
        ax = self.fig.add_subplot(111)
        
        for label, xs, ys in series:
            ax.plot(xs, ys, linewidth=1.2, label=label, alpha=0.9)
        
        if x_is_time:
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
            self.fig.autofmt_xdate()
        
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.grid(True, alpha=0.3)
        if series:
            ax.legend(loc='best', fontsize=9)
        
        self.fig.subplots_adjust(top=0.92, bottom=0.14, left=0.1, right=0.95)
        self.draw()
    
    def clear_plot(self):
        if HAS_MATPLOTLIB:
            self.fig.clear()
            self.draw()


# ============================================================================
# 模式一：Klipper Stats Tab
# ============================================================================

class KlipperStatsTab(QWidget):
    """针对 Klipper Stats 日志的专用解析界面"""
    
    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        self.records: List[StatsRecord] = []
        self.parse_thread: Optional[StatsParseThread] = None
        self._group_fields_cache: Dict[str, List[str]] = {}  # 组名 -> 字段列表
        
        self._setup_ui()
    
    def _setup_ui(self):
        root = QVBoxLayout(self)
        
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)
        
        # ---------- 左侧控制区 ----------
        left = QWidget()
        left_v = QVBoxLayout(left)
        left_v.setContentsMargins(0, 0, 0, 0)
        
        # 文件选择
        fg = QGroupBox("Klippy 日志文件")
        fh = QHBoxLayout(fg)
        self.file_edit = QLineEdit()
        self.file_edit.setPlaceholderText("选择 klippy.log ...")
        fh.addWidget(self.file_edit, 1)
        btn_browse = QPushButton("浏览")
        btn_browse.clicked.connect(self._browse)
        fh.addWidget(btn_browse)
        btn_load = QPushButton("加载解析")
        btn_load.clicked.connect(self._load_file)
        fh.addWidget(btn_load)
        left_v.addWidget(fg)
        
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #666;")
        left_v.addWidget(self.status_label)
        
        # 组选择
        gg = QGroupBox("组（关键字）")
        gv = QVBoxLayout(gg)
        hint = QLabel("点击一个组，右边会列出该组所有字段。多选字段后点「绘制」。")
        hint.setStyleSheet("color: #888; font-size: 11px;")
        hint.setWordWrap(True)
        gv.addWidget(hint)
        self.group_list = QListWidget()
        self.group_list.itemSelectionChanged.connect(self._on_group_select)
        gv.addWidget(self.group_list, 1)
        left_v.addWidget(gg, 2)
        
        # 字段多选
        fg2 = QGroupBox("字段（多选）")
        fv = QVBoxLayout(fg2)
        self.field_list = QListWidget()
        self.field_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        fv.addWidget(self.field_list, 1)
        
        btn_row = QHBoxLayout()
        self.btn_plot = QPushButton("绘制")
        self.btn_plot.clicked.connect(self._plot)
        btn_row.addWidget(self.btn_plot)
        self.btn_select_all = QPushButton("全选")
        self.btn_select_all.clicked.connect(lambda: self._set_all_selected(True))
        btn_row.addWidget(self.btn_select_all)
        self.btn_select_none = QPushButton("清空")
        self.btn_select_none.clicked.connect(lambda: self._set_all_selected(False))
        btn_row.addWidget(self.btn_select_none)
        fv.addLayout(btn_row)
        
        left_v.addWidget(fg2, 2)
        
        splitter.addWidget(left)
        
        # ---------- 右侧绘图 ----------
        right = QWidget()
        right_v = QVBoxLayout(right)
        right_v.setContentsMargins(0, 0, 0, 0)
        
        self.canvas = TimeSeriesCanvas(self)
        right_v.addWidget(self.canvas, 1)
        if HAS_MATPLOTLIB:
            self.toolbar = NavigationToolbar(self.canvas, self)
            right_v.addWidget(self.toolbar)
        
        splitter.addWidget(right)
        splitter.setSizes([420, 980])
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 7)
        splitter.setChildrenCollapsible(False)
    
    def _browse(self):
        fp, _ = QFileDialog.getOpenFileName(
            self, "选择 Klippy 日志", "",
            "Log files (*.log);;All files (*.*)"
        )
        if fp:
            self.file_edit.setText(fp)
    
    def _load_file(self):
        fp = self.file_edit.text().strip()
        if not fp or not os.path.exists(fp):
            QMessageBox.warning(self, "错误", "请选择有效的日志文件")
            return
        
        # 停止旧线程
        if self.parse_thread and self.parse_thread.isRunning():
            self.parse_thread.stop()
            self.parse_thread.wait(2000)
        
        self.records = []
        self.group_list.clear()
        self.field_list.clear()
        self._group_fields_cache.clear()
        self.canvas.clear_plot()
        
        self.status_label.setText("正在解析...")
        self.context.show_status("开始解析日志...", 0)
        
        self.parse_thread = StatsParseThread(Path(fp), self)
        self.parse_thread.progress.connect(self._on_progress)
        self.parse_thread.finished_ok.connect(self._on_parse_done)
        self.parse_thread.failed.connect(self._on_parse_failed)
        self.parse_thread.start()
    
    def _on_progress(self, cur: int, total: int):
        if total > 0:
            pct = cur * 100 / total
            self.status_label.setText(f"解析进度: {pct:.1f}%")
    
    def _on_parse_done(self, records: list):
        self.records = records
        self.context.show_status(f"解析完成，共 {len(records)} 条 Stats 记录", 5000)
        self.status_label.setText(f"已加载 {len(records)} 条记录")
        self._build_group_list()
    
    def _on_parse_failed(self, msg: str):
        self.status_label.setText(f"解析失败: {msg}")
        self.context.show_status(f"日志解析失败: {msg}", 10000)
        QMessageBox.critical(self, "解析失败", msg)
    
    def _build_group_list(self):
        """汇总所有记录出现过的组名，并收集每组的字段全集"""
        group_fields: Dict[str, set] = {}
        for rec in self.records:
            for g, fields in rec.groups.items():
                group_fields.setdefault(g, set()).update(fields.keys())
        
        self._group_fields_cache = {
            g: sorted(fs) for g, fs in group_fields.items()
        }
        
        self.group_list.clear()
        for g in sorted(self._group_fields_cache.keys()):
            count = len(self._group_fields_cache[g])
            item = QListWidgetItem(f"{g}  ({count} 字段)")
            item.setData(Qt.ItemDataRole.UserRole, g)
            self.group_list.addItem(item)
    
    def _on_group_select(self):
        items = self.group_list.selectedItems()
        self.field_list.clear()
        if not items:
            return
        g = items[0].data(Qt.ItemDataRole.UserRole)
        for f in self._group_fields_cache.get(g, []):
            self.field_list.addItem(QListWidgetItem(f))
    
    def _set_all_selected(self, flag: bool):
        for i in range(self.field_list.count()):
            self.field_list.item(i).setSelected(flag)
    
    def _plot(self):
        items = self.group_list.selectedItems()
        if not items:
            QMessageBox.information(self, "提示", "请先选择一个组")
            return
        g = items[0].data(Qt.ItemDataRole.UserRole)
        
        fields = [self.field_list.item(i).text()
                  for i in range(self.field_list.count())
                  if self.field_list.item(i).isSelected()]
        if not fields:
            QMessageBox.information(self, "提示", "请至少选择一个字段")
            return
        
        series = []
        for fld in fields:
            xs, ys = [], []
            for rec in self.records:
                vals = rec.groups.get(g)
                if vals is None:
                    continue
                if fld not in vals:
                    continue
                xs.append(rec.timestamp)
                ys.append(vals[fld])
            if xs:
                series.append((f"{g}.{fld}", xs, ys))
        
        if not series:
            QMessageBox.information(self, "提示", "选中的字段在数据中不存在")
            return
        
        self.canvas.plot_series(
            series,
            xlabel='Time',
            ylabel='Value',
            title=f'{g}',
            x_is_time=True,
        )
        self.context.show_status(f"已绘制 {len(series)} 条曲线", 5000)


# ============================================================================
# 模式二：通用检索 Tab —— 表单式，不写正则
# ============================================================================

# 字段类型选项
FIELD_TYPE_TIME = "时间"
FIELD_TYPE_NUMBER = "数字"

# 常见时间格式（按复杂度从高到低尝试）
_TIME_FORMATS = [
    '%Y-%m-%d %H:%M:%S,%f',
    '%Y-%m-%d %H:%M:%S.%f',
    '%Y-%m-%d %H:%M:%S',
    '%Y/%m/%d %H:%M:%S',
    '%H:%M:%S,%f',
    '%H:%M:%S.%f',
    '%H:%M:%S',
]

# 时间串的通用捕获正则（覆盖上面所有格式）
_TIME_CAPTURE = r'(\d{4}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2}:\d{2}(?:[,\.]\d+)?|\d{2}:\d{2}:\d{2}(?:[,\.]\d+)?)'
# 数值串：带可选负号 / 小数 / 科学计数
_NUMBER_CAPTURE = r'(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)'


def _parse_time(s: str) -> Optional[datetime]:
    """多格式尝试解析时间字符串"""
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


class GenericSearchThread(QThread):
    """后台执行通用关键字 + 字段检索"""
    progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal(list)           # List[dict]
    failed = pyqtSignal(str)
    
    def __init__(self, filepath: Path, keyword: str, fields: List[dict], parent=None):
        """fields: [{'name': str, 'type': '时间'|'数字'}]"""
        super().__init__(parent)
        self.filepath = filepath
        self.keyword = keyword
        self.fields = fields
        self._stop = False
    
    def stop(self):
        self._stop = True
    
    def run(self):
        try:
            # 为每个字段预编译正则
            field_patterns = []
            for f in self.fields:
                name = f['name']
                ftype = f['type']
                if ftype == FIELD_TYPE_TIME:
                    pat = re.compile(_TIME_CAPTURE)
                else:
                    pat = re.compile(
                        rf'{re.escape(name)}\s*[=:]\s*{_NUMBER_CAPTURE}'
                    )
                field_patterns.append((name, ftype, pat))
            
            total = self.filepath.stat().st_size
            read = 0
            report_every = max(1, total // 50)
            next_report = report_every
            
            results: List[Dict[str, str]] = []
            kw = self.keyword
            
            with self.filepath.open('r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if self._stop:
                        break
                    read += len(line.encode('utf-8', errors='ignore'))
                    
                    # 关键字过滤：定位关键字位置，只在其后抽字段
                    if kw:
                        kw_pos = line.find(kw)
                        if kw_pos < 0:
                            # 进度统计还是要更新
                            if read >= next_report:
                                self.progress.emit(min(read, total), total)
                                next_report += report_every
                            continue
                        # 关键字之后的文本作为「数字字段」的搜索区
                        # （时间字段仍在整行里找，因为时间通常在行首）
                        after_kw = line[kw_pos:]
                    else:
                        after_kw = line
                    
                    row: Dict[str, str] = {}
                    for name, ftype, pat in field_patterns:
                        if ftype == FIELD_TYPE_TIME:
                            # 时间固定在行首附近
                            m = pat.search(line)
                        else:
                            m = pat.search(after_kw)
                        if m:
                            row[name] = m.group(1)
                    if row:
                        results.append(row)
                    
                    if read >= next_report:
                        self.progress.emit(min(read, total), total)
                        next_report += report_every
            
            self.progress.emit(total, total)
            self.finished_ok.emit(results)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class GenericRegexTab(QWidget):
    """点选式检索：粘贴样本 → 选中词标为关键字/字段 → 自动应用到整个文件
    
    内部仍维护 keyword(str) 和 fields(List[dict]) 与后台线程的接口。
    """
    
    COLOR_KEYWORD = "#fff3cd"   # 关键字背景色（淡黄）
    COLOR_FIELD = "#d1e7ff"     # 字段背景色（淡蓝）
    
    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        self.last_matches: List[Dict[str, str]] = []
        self.search_thread: Optional[GenericSearchThread] = None
        
        # 用户标记：关键字 str、字段 list[{'name','type','token'}]
        self.keyword: str = ""
        self.fields: List[dict] = []
        
        self._setup_ui()
    
    # ---------------------- UI ----------------------
    def _setup_ui(self):
        root = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)
        
        # ---------- 左侧 ----------
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        
        # 步骤 1：选文件
        step1 = QGroupBox("1. 日志文件")
        s1 = QHBoxLayout(step1)
        self.file_edit = QLineEdit()
        self.file_edit.setPlaceholderText("任意 .log / .txt 文件")
        s1.addWidget(self.file_edit, 1)
        btn_browse = QPushButton("浏览")
        btn_browse.clicked.connect(self._browse)
        s1.addWidget(btn_browse)
        btn_load_sample = QPushButton("取首行作样本")
        btn_load_sample.setToolTip("从日志中读一条有数据的行放到下方样本框")
        btn_load_sample.clicked.connect(self._auto_load_sample)
        s1.addWidget(btn_load_sample)
        lv.addWidget(step1)
        
        # 步骤 2：粘贴样本行并点选
        step2 = QGroupBox("2. 样本行（选中词，右键或下方按钮标记）")
        s2 = QVBoxLayout(step2)
        
        hint = QLabel(
            "把日志里的一行粘到下面，用鼠标选中要作为关键字 / 要抽取的词，\n"
            "再点下方对应按钮（或右键菜单）。关键字=黄色，字段=蓝色。"
        )
        hint.setStyleSheet("color: #888; font-size: 11px;")
        hint.setWordWrap(True)
        s2.addWidget(hint)
        
        self.sample_edit = QTextEdit()
        self.sample_edit.setFont(QFont("Consolas", 10))
        self.sample_edit.setMinimumHeight(160)
        self.sample_edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.sample_edit.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.sample_edit.customContextMenuRequested.connect(self._sample_context_menu)
        s2.addWidget(self.sample_edit)
        
        # 快捷按钮：对选中文本打标
        tag_row = QHBoxLayout()
        btn_mark_kw = QPushButton("标为关键字（🟡）")
        btn_mark_kw.clicked.connect(lambda: self._mark_selection('keyword'))
        tag_row.addWidget(btn_mark_kw)
        btn_mark_fld = QPushButton("标为字段（🔵）")
        btn_mark_fld.clicked.connect(lambda: self._mark_selection('field'))
        tag_row.addWidget(btn_mark_fld)
        btn_clear = QPushButton("清除全部标记")
        btn_clear.clicked.connect(self._clear_all_marks)
        tag_row.addWidget(btn_clear)
        s2.addLayout(tag_row)
        
        lv.addWidget(step2)
        
        # 步骤 3：已标记项列表
        step3 = QGroupBox("3. 已标记")
        s3 = QVBoxLayout(step3)
        
        s3.addWidget(QLabel("关键字："))
        self.keyword_label = QLabel("（未设置）")
        self.keyword_label.setStyleSheet(
            f"background:{self.COLOR_KEYWORD}; padding:4px; border-radius:3px;")
        self.keyword_label.setWordWrap(True)
        s3.addWidget(self.keyword_label)
        
        s3.addWidget(QLabel("字段："))
        self.field_tree = QTreeWidget()
        self.field_tree.setHeaderLabels(['字段名', '类型', '原文本'])
        self.field_tree.setRootIsDecorated(False)
        self.field_tree.setAlternatingRowColors(True)
        self.field_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.field_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.field_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.field_tree.setMinimumHeight(160)
        s3.addWidget(self.field_tree, 1)
        
        del_row = QHBoxLayout()
        btn_rename = QPushButton("改名")
        btn_rename.clicked.connect(self._rename_field)
        del_row.addWidget(btn_rename)
        btn_del_field = QPushButton("删除选中字段")
        btn_del_field.clicked.connect(self._delete_selected_field)
        del_row.addWidget(btn_del_field)
        s3.addLayout(del_row)
        
        lv.addWidget(step3, 2)
        
        # 步骤 4：开始检索（独立按钮）
        step4 = QGroupBox("4. 检索数据")
        s4 = QVBoxLayout(step4)
        self.btn_search = QPushButton("开始检索")
        self.btn_search.setStyleSheet("font-weight:bold;")
        self.btn_search.clicked.connect(self._run_search)
        s4.addWidget(self.btn_search)
        self.search_status = QLabel("")
        self.search_status.setStyleSheet("color: #666; font-size: 11px;")
        s4.addWidget(self.search_status)
        lv.addWidget(step4)
        
        # 步骤 5：绘图（独立）
        step5 = QGroupBox("5. 绘图")
        s5 = QVBoxLayout(step5)
        
        x_row = QHBoxLayout()
        x_row.addWidget(QLabel("X 轴:"))
        self.x_combo = QComboBox()
        self.x_combo.setMinimumWidth(140)
        self.x_combo.setEnabled(False)
        x_row.addWidget(self.x_combo, 1)
        s5.addLayout(x_row)
        
        s5.addWidget(QLabel("Y 轴（多选）:"))
        self.y_list = QListWidget()
        self.y_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        self.y_list.setMinimumHeight(140)
        self.y_list.setEnabled(False)
        s5.addWidget(self.y_list)
        
        self.btn_plot = QPushButton("绘制曲线")
        self.btn_plot.setEnabled(False)
        self.btn_plot.clicked.connect(self._plot)
        s5.addWidget(self.btn_plot)
        
        self.plot_hint = QLabel("提示：先完成第 4 步「开始检索」后，这里会自动填充可选轴")
        self.plot_hint.setStyleSheet("color: #888; font-size: 11px;")
        self.plot_hint.setWordWrap(True)
        s5.addWidget(self.plot_hint)
        
        lv.addWidget(step5)
        lv.addStretch()
        
        # 把左侧整块放进滚动区，让各步骤可以给更多空间
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        left_scroll.setWidget(left)
        splitter.addWidget(left_scroll)
        
        # ---------- 右侧绘图 ----------
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.canvas = TimeSeriesCanvas(self)
        rv.addWidget(self.canvas, 1)
        if HAS_MATPLOTLIB:
            self.toolbar = NavigationToolbar(self.canvas, self)
            rv.addWidget(self.toolbar)
        
        # 匹配样本预览
        preview_g = QGroupBox("前 5 条匹配样本")
        pg_v = QVBoxLayout(preview_g)
        self.preview_text = QPlainTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setFont(QFont("Consolas", 9))
        self.preview_text.setMaximumHeight(110)
        pg_v.addWidget(self.preview_text)
        rv.addWidget(preview_g)
        
        splitter.addWidget(right)
        splitter.setSizes([520, 880])
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 7)
        splitter.setChildrenCollapsible(False)
    
    # ---------------------- 文件 / 样本 ----------------------
    def _browse(self):
        fp, _ = QFileDialog.getOpenFileName(
            self, "选择日志文件", "",
            "Log files (*.log *.txt);;All files (*.*)"
        )
        if fp:
            self.file_edit.setText(fp)
    
    def _auto_load_sample(self):
        """读日志第一条含字母+等号/冒号的行作为样本"""
        fp = self.file_edit.text().strip()
        if not fp or not os.path.exists(fp):
            QMessageBox.warning(self, "错误", "请先选择日志文件")
            return
        try:
            with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    # 跳过空行和纯分隔行
                    if '=' in line or ':' in line:
                        self.sample_edit.setPlainText(line.rstrip('\n'))
                        self._clear_all_marks()
                        return
            QMessageBox.information(self, "提示", "未找到合适的样本行")
        except Exception as e:
            QMessageBox.critical(self, "读取错误", str(e))
    
    # ---------------------- 标记交互 ----------------------
    def _sample_context_menu(self, pos):
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        a_kw = menu.addAction("标为关键字")
        a_fld = menu.addAction("标为字段")
        menu.addSeparator()
        a_clear = menu.addAction("清除全部标记")
        action = menu.exec(self.sample_edit.mapToGlobal(pos))
        if action == a_kw:
            self._mark_selection('keyword')
        elif action == a_fld:
            self._mark_selection('field')
        elif action == a_clear:
            self._clear_all_marks()
    
    def _mark_selection(self, kind: str):
        """给选中文本打标。kind: 'keyword' | 'field'"""
        cursor = self.sample_edit.textCursor()
        text = cursor.selectedText().strip()
        if not text:
            QMessageBox.information(self, "提示", "请先用鼠标选中一段文本")
            return
        
        if kind == 'keyword':
            # 只能有一个关键字；先清除旧的关键字高亮
            self._remove_keyword_highlight()
            self.keyword = text
            self.keyword_label.setText(text)
            self._apply_highlight(text, self.COLOR_KEYWORD)
        else:
            # 判断类型：选中文本是不是时间格式？
            ftype, raw_token = self._infer_field_type(text)
            # 推断字段名：如果用户选中的是 "key=value"，取 key；否则用用户选中文本
            # 如果选中纯值且周围有 "key=" 结构，也取 key
            name = self._infer_field_name(text, cursor)
            
            # 重名检查
            if any(f['name'] == name for f in self.fields):
                QMessageBox.information(self, "提示", f"字段 '{name}' 已存在；请改选其他词或先删除")
                return
            
            self.fields.append({
                'name': name,
                'type': ftype,
                'token': raw_token or text,
            })
            self._refresh_field_tree()
            self._apply_highlight(text, self.COLOR_FIELD)
    
    def _apply_highlight(self, text: str, color_hex: str):
        """给样本框中所有出现的 text 加背景色"""
        from PyQt6.QtGui import QTextCharFormat, QColor, QTextCursor
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(color_hex))
        doc = self.sample_edit.document()
        cursor = QTextCursor(doc)
        # 为了避免重复高亮同一片段，先搜索匹配
        full = doc.toPlainText()
        start = 0
        while True:
            idx = full.find(text, start)
            if idx < 0:
                break
            c = QTextCursor(doc)
            c.setPosition(idx)
            c.setPosition(idx + len(text), QTextCursor.MoveMode.KeepAnchor)
            c.mergeCharFormat(fmt)
            start = idx + len(text)
    
    def _remove_keyword_highlight(self):
        """只清除关键字的高亮（不动字段）"""
        if not self.keyword:
            return
        from PyQt6.QtGui import QTextCharFormat, QColor, QTextCursor
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#ffffff"))
        doc = self.sample_edit.document()
        full = doc.toPlainText()
        start = 0
        while True:
            idx = full.find(self.keyword, start)
            if idx < 0:
                break
            c = QTextCursor(doc)
            c.setPosition(idx)
            c.setPosition(idx + len(self.keyword), QTextCursor.MoveMode.KeepAnchor)
            c.mergeCharFormat(fmt)
            start = idx + len(self.keyword)
    
    def _clear_all_marks(self):
        """清除所有标记"""
        from PyQt6.QtGui import QTextCharFormat, QColor, QTextCursor
        self.keyword = ""
        self.fields.clear()
        self.keyword_label.setText("（未设置）")
        self.field_tree.clear()
        # 清除所有背景色
        doc = self.sample_edit.document()
        cursor = QTextCursor(doc)
        cursor.select(QTextCursor.SelectionType.Document)
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#ffffff"))
        cursor.mergeCharFormat(fmt)
    
    def _refresh_field_tree(self):
        self.field_tree.clear()
        for f in self.fields:
            item = QTreeWidgetItem([f['name'], f['type'], f.get('token', '')])
            self.field_tree.addTopLevelItem(item)
    
    def _rename_field(self):
        """重命名选中的字段"""
        idx = self.field_tree.indexOfTopLevelItem(self.field_tree.currentItem())
        if idx < 0:
            QMessageBox.information(self, "提示", "请先选中一个字段")
            return
        from PyQt6.QtWidgets import QInputDialog
        old = self.fields[idx]['name']
        new, ok = QInputDialog.getText(self, "改名", f"将 '{old}' 改为：", text=old)
        new = new.strip() if new else ""
        if not ok or not new:
            return
        if new != old and any(f['name'] == new for f in self.fields):
            QMessageBox.warning(self, "错误", f"字段 '{new}' 已存在")
            return
        self.fields[idx]['name'] = new
        self._refresh_field_tree()
    
    def _delete_selected_field(self):
        idx = self.field_tree.indexOfTopLevelItem(self.field_tree.currentItem())
        if idx < 0:
            return
        self.fields.pop(idx)
        # 重新渲染高亮
        self._rerender_highlights()
        self._refresh_field_tree()
    
    def _rerender_highlights(self):
        """根据当前 keyword/fields 状态重算所有高亮"""
        from PyQt6.QtGui import QTextCharFormat, QColor, QTextCursor
        doc = self.sample_edit.document()
        cursor = QTextCursor(doc)
        cursor.select(QTextCursor.SelectionType.Document)
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#ffffff"))
        cursor.mergeCharFormat(fmt)
        if self.keyword:
            self._apply_highlight(self.keyword, self.COLOR_KEYWORD)
        for f in self.fields:
            self._apply_highlight(f.get('token', f['name']), self.COLOR_FIELD)
    
    # ---------------------- 推断 ----------------------
    @staticmethod
    def _infer_field_type(text: str) -> Tuple[str, str]:
        """根据选中文本推断类型，并返回 (type, token_for_highlight)
        
        - 若含时间格式 → 时间
        - 若形如 "key=value" / "key:value" → 数字（并返回 key 作 token）
        - 若是纯数字 → 数字
        - 其他 → 数字（兜底）
        """
        # 时间
        if re.search(_TIME_CAPTURE, text):
            return FIELD_TYPE_TIME, text
        # key=value / key:value
        m = re.match(r'^([A-Za-z_][\w]*)[=:]\s*' + _NUMBER_CAPTURE + r'$', text)
        if m:
            return FIELD_TYPE_NUMBER, text
        # 纯数字
        if re.fullmatch(r'-?\d+(\.\d+)?', text):
            return FIELD_TYPE_NUMBER, text
        return FIELD_TYPE_NUMBER, text
    
    def _infer_field_name(self, text: str, cursor) -> str:
        """推断字段名：
        
        - 若 text 含时间串 → 'time'
        - 若 text 是 "key=value" → 'key'
        - 若 text 是纯数字 → 查看左侧是否有 "xxx=" 结构，取 xxx；否则 'value{N}'
        - 若 text 是标识符 → 直接用它
        """
        if re.search(_TIME_CAPTURE, text):
            # 已有同名就追加数字
            return self._unique_name('time')
        m = re.match(r'^([A-Za-z_][\w]*)[=:]\s*' + _NUMBER_CAPTURE + r'$', text)
        if m:
            return self._unique_name(m.group(1))
        if re.fullmatch(r'-?\d+(\.\d+)?', text):
            # 看选中位置左侧
            doc_text = self.sample_edit.toPlainText()
            pos = cursor.selectionStart()
            left = doc_text[max(0, pos - 40):pos]
            lm = re.search(r'([A-Za-z_][\w]*)\s*[=:]\s*$', left)
            if lm:
                return self._unique_name(lm.group(1))
            return self._unique_name('value')
        if re.fullmatch(r'[A-Za-z_][\w]*', text):
            return self._unique_name(text)
        return self._unique_name('field')
    
    def _unique_name(self, base: str) -> str:
        """保证字段名不重复，重复时加后缀 _2 _3"""
        existing = {f['name'] for f in self.fields}
        if base not in existing:
            return base
        i = 2
        while f'{base}_{i}' in existing:
            i += 1
        return f'{base}_{i}'
    
    # ---------------------- 检索 ----------------------
    def _collect_fields(self) -> List[dict]:
        """给后台线程用的字段结构（兼容旧接口）"""
        return [{'name': f['name'], 'type': f['type']} for f in self.fields]
    
    def _run_search(self):
        fp = self.file_edit.text().strip()
        if not fp or not os.path.exists(fp):
            QMessageBox.warning(self, "错误", "请选择有效的日志文件")
            return
        
        fields = self._collect_fields()
        if not fields:
            QMessageBox.information(self, "提示",
                                    "请先在样本行里选中并「标为字段」至少一项")
            return
        
        kw = self.keyword
        
        if self.search_thread and self.search_thread.isRunning():
            self.search_thread.stop()
            self.search_thread.wait(2000)
        
        self.last_matches = []
        self.x_combo.clear()
        self.y_list.clear()
        self.preview_text.clear()
        
        # 检索进行中：禁用绘图相关控件
        self.x_combo.setEnabled(False)
        self.y_list.setEnabled(False)
        self.btn_plot.setEnabled(False)
        self.btn_search.setEnabled(False)
        self.plot_hint.setText("检索进行中...")
        
        self.search_status.setText("检索中...")
        self.context.show_status("开始后台检索...", 0)
        
        self.search_thread = GenericSearchThread(Path(fp), kw, fields, self)
        self.search_thread.progress.connect(self._on_search_progress)
        self.search_thread.finished_ok.connect(self._on_search_done)
        self.search_thread.failed.connect(self._on_search_failed)
        self.search_thread.start()
    
    def _on_search_progress(self, cur: int, total: int):
        if total > 0:
            pct = cur * 100 / total
            self.search_status.setText(f"进度: {pct:.1f}%")
    
    def _on_search_done(self, matches: list):
        self.last_matches = matches
        fields = self._collect_fields()
        field_names = [f['name'] for f in fields]
        
        self.search_status.setText(f"匹配 {len(matches)} 行")
        self.context.show_status(f"检索完成，共 {len(matches)} 行", 5000)
        
        # 恢复按钮
        self.btn_search.setEnabled(True)
        
        # 填充 X/Y 选择
        self.x_combo.addItems(field_names)
        for i, f in enumerate(fields):
            if f['type'] == FIELD_TYPE_TIME:
                self.x_combo.setCurrentIndex(i)
                break
        for f in fields:
            if f['type'] == FIELD_TYPE_NUMBER:
                item = QListWidgetItem(f['name'])
                item.setSelected(True)  # 默认全选所有数字字段
                self.y_list.addItem(item)
        
        if matches:
            preview_lines = [json.dumps(m, ensure_ascii=False) for m in matches[:5]]
            self.preview_text.setPlainText("\n".join(preview_lines))
            # 启用绘图控件（不自动绘图，让用户先挑 Y 轴）
            self.x_combo.setEnabled(True)
            self.y_list.setEnabled(True)
            self.btn_plot.setEnabled(True)
            self.plot_hint.setText(
                f"已就绪：选择 X 轴、勾选 Y 轴（默认全选 {self.y_list.count()} 项数字字段），点「绘制曲线」"
            )
        else:
            self.preview_text.setPlainText(
                "（未匹配到任何行）\n"
                "可能原因：\n"
                "  · 关键字拼写与真实日志不一致\n"
                "  · 字段名在真实行中不是「name=value」格式\n"
                "试试：在样本行里用右键先取消再重新选"
            )
            self.plot_hint.setText("未匹配到数据，无法绘图")
    
    def _on_search_failed(self, msg: str):
        self.search_status.setText(f"失败: {msg}")
        self.btn_search.setEnabled(True)
        self.plot_hint.setText("检索失败，请检查日志文件和标记")
        QMessageBox.critical(self, "检索失败", msg)
    
    def _plot(self):
        if not self.last_matches:
            QMessageBox.information(self, "提示", "请先完成检索")
            return
        
        x_field = self.x_combo.currentText()
        if not x_field:
            QMessageBox.warning(self, "错误", "请选择 X 轴字段")
            return
        
        y_fields = [self.y_list.item(i).text()
                    for i in range(self.y_list.count())
                    if self.y_list.item(i).isSelected()]
        if not y_fields:
            QMessageBox.information(self, "提示", "请至少选择一个 Y 轴字段")
            return
        
        # 根据字段类型决定 X 是否为时间
        fields = self._collect_fields()
        x_type = next((f['type'] for f in fields if f['name'] == x_field), FIELD_TYPE_NUMBER)
        x_is_time = (x_type == FIELD_TYPE_TIME)
        
        series = []
        for y_field in y_fields:
            xs, ys = [], []
            for row in self.last_matches:
                xv_str = row.get(x_field)
                yv_str = row.get(y_field)
                if xv_str is None or yv_str is None:
                    continue
                if x_is_time:
                    x_val = _parse_time(xv_str)
                    if x_val is None:
                        continue
                else:
                    try:
                        x_val = float(xv_str)
                    except ValueError:
                        continue
                try:
                    y_val = float(yv_str)
                except ValueError:
                    continue
                xs.append(x_val)
                ys.append(y_val)
            if xs:
                series.append((y_field, xs, ys))
        
        if not series:
            QMessageBox.information(self, "提示",
                                    "没有可绘制的数据。\n"
                                    "常见原因：\n"
                                    "  · Y 字段类型不是「数字」\n"
                                    "  · 字段名在日志里拼写不一致")
            return
        
        self.canvas.plot_series(
            series,
            xlabel=x_field,
            ylabel='Value',
            title=f'Y vs {x_field}',
            x_is_time=x_is_time,
        )
        self.context.show_status(f"已绘制 {len(series)} 条曲线", 5000)


# ============================================================================
# 主 Widget：Tab 容器
# ============================================================================

class LogCurvePlotterWidget(QWidget):
    """插件主界面：Tab 切换两种模式"""
    
    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)
        
        self.stats_tab = KlipperStatsTab(context, self)
        self.generic_tab = GenericRegexTab(context, self)
        
        self.tabs.addTab(self.stats_tab, "Klipper Stats 解析")
        self.tabs.addTab(self.generic_tab, "通用关键字检索")
    
    def activate(self):
        self.context.show_status("日志曲线绘图插件已激活", 3000)
    
    def deactivate(self):
        pass
    
    def shutdown(self):
        if self.stats_tab.parse_thread and self.stats_tab.parse_thread.isRunning():
            self.stats_tab.parse_thread.stop()
            self.stats_tab.parse_thread.wait(2000)
        if self.generic_tab.search_thread and self.generic_tab.search_thread.isRunning():
            self.generic_tab.search_thread.stop()
            self.generic_tab.search_thread.wait(2000)


def create_plugin(context, parent=None) -> QWidget:
    return LogCurvePlotterWidget(context, parent)
