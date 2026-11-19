#!F:\anaconda3\envs\Mrlzr\python.exe
# -*- coding: utf-8 -*-
"""Klipper 日志网床（bed_mesh）数据抓取与分析工具。

功能
----
1. 从 klippy 日志中抓取每一次床网测量的 points 矩阵：
       [configfile:set:390] save_config: set [bed_mesh default] points =
   后面紧跟的若干行数字（每行 x_count 个值，共 y_count 行）。
   同一次测量通常会在 0.5 秒内被连续保存两次，这里按“时间接近 + 数值相同”去重。
2. 抓取每一次打印的开始时间：
       work_handler start print, filename:xxx.gcode
3. 每次打印取“打印开始时间之前、时间上最近的一次”网床数据，并做统计分析：
   均值 / 极差 / 标准差 / RMS / 平面拟合倾斜量 / 平面残差 / 最凸与最凹位置 /
   相对基准网床的差分。
4. Tkinter 图形界面：任务表格、网床热力图、差分热力图、统计详情、CSV 导出。

用法
----
    python 网床日志分析GUI.py              # 打开 GUI
    python 网床日志分析GUI.py --cli 日志   # 命令行输出摘要（不开窗口）

说明：tag 中的数字片段（例如 configfile:set 后面的行号 390）会自动容错，
      匹配任意数字，所以 Klipper 版本升级导致行号变化时无需改 tag。
"""

from __future__ import annotations

import bisect
import csv
import queue
import re
import sys
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import matplotlib

matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import (  # noqa: E402
    FigureCanvasTkAgg,
    NavigationToolbar2Tk,
)
from matplotlib.figure import Figure  # noqa: E402

# --------------------------------------------------------------------------- #
# 默认配置
# --------------------------------------------------------------------------- #

DEFAULT_LOG = r"D:\BaiduNetdiskDownload\klippy.log.2026-11-18"
DEFAULT_MESH_TAG = "[configfile:set:390] save_config: set [bed_mesh default] points ="
DEFAULT_PRINT_TAG = "work_handler start print"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_EXPORT = SCRIPT_DIR / "网床日志分析结果.csv"

# 同一次测量被重复保存时，两次记录的最大时间间隔（秒）
DUPLICATE_WINDOW_S = 3.0
DUPLICATE_TOL = 1e-6
# 网床 points 之后这些行内出现的字段仍归属本次网床
META_LOOKAHEAD_LINES = 80
# 单个 points 块最多读取的行数（防御异常日志）
MAX_POINTS_ROWS = 200

BASELINE_FIRST = "第一条网床"
BASELINE_PREV = "上一条网床"

matplotlib.rcParams["font.sans-serif"] = [
    "Microsoft YaHei",
    "SimHei",
    "DengXian",
    "DejaVu Sans",
]
matplotlib.rcParams["axes.unicode_minus"] = False


# --------------------------------------------------------------------------- #
# 日志解析
# --------------------------------------------------------------------------- #

LEVEL_TS_RE = re.compile(
    r"^\[(?P<level>[^\]]*)\]\s+"
    r"(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"
)

# [bed_mesh default] min_x = 5.0 之类的键值对
MESH_KV_RE = re.compile(
    r"\[bed_mesh default\]\s+(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*?)\s*$"
)

FILENAME_RE = re.compile(r"filename\s*:\s*(?P<fn>.+?)\s*$")

SPLIT_RE = re.compile(r"[,\s]+")
DIGIT_PIECE_RE = re.compile(r"\d+|\D+")


@dataclass
class MeshRecord:
    """一次床网测量。"""

    timestamp: datetime
    line_no: int
    points: np.ndarray
    meta: Dict[str, str] = field(default_factory=dict)
    dup_count: int = 1          # 被重复保存的次数（去重合并了多少条）
    usage_count: int = 0        # 被多少次打印使用

    @property
    def rows(self) -> int:
        return int(self.points.shape[0])

    @property
    def cols(self) -> int:
        return int(self.points.shape[1])

    @property
    def clock(self) -> str:
        return self.timestamp.strftime("%H:%M:%S")

    @property
    def stamp(self) -> str:
        return self.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


@dataclass
class PrintEvent:
    """一次开始打印。"""

    timestamp: datetime
    line_no: int
    filename: str
    mesh_index: Optional[int] = None

    @property
    def stamp(self) -> str:
        return self.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    @property
    def clock(self) -> str:
        return self.timestamp.strftime("%H:%M:%S")

    @property
    def model(self) -> str:
        return model_name(self.filename)


@dataclass
class MeshStats:
    """单个网床的统计量。"""

    vmin: float
    vmax: float
    mean: float
    span: float
    std: float
    rms: float
    tilt_x: Optional[float] = None      # mm / 100 mm
    tilt_y: Optional[float] = None      # mm / 100 mm
    residual_rms: Optional[float] = None
    peak: Optional[Tuple[float, float, float]] = None   # (x, y, z)
    pit: Optional[Tuple[float, float, float]] = None
    physical: bool = False              # 坐标是否为真实 mm

    @property
    def axis_unit(self) -> str:
        return "mm" if self.physical else "索引"


@dataclass
class ParseResult:
    path: Path
    meshes: List[MeshRecord]
    prints: List[PrintEvent]
    raw_mesh_count: int
    stats: List[MeshStats]


def parse_timestamp(line: str) -> Optional[datetime]:
    """从日志行开头解析时间戳，失败返回 None。"""
    match = LEVEL_TS_RE.match(line)
    if match is None:
        return None
    text = match.group("ts").replace(",", ".")
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in text else "%Y-%m-%d %H:%M:%S"
    try:
        return datetime.strptime(text, fmt)
    except ValueError:
        return None


def build_tag_pattern(tag: str) -> Optional[re.Pattern]:
    """把用户输入的 tag 文本编译成正则。

    - 空白按一个或多个空白匹配；
    - 纯数字片段替换成 \\d+，从而容忍 Klipper 行号变化（如 set:390 -> set:362）。
    """
    tokens = tag.split()
    if not tokens:
        return None

    groups: List[str] = []
    for token in tokens:
        pieces: List[str] = []
        for piece in DIGIT_PIECE_RE.findall(token):
            pieces.append(r"\d+" if piece.isdigit() else re.escape(piece))
        groups.append("".join(pieces))
    try:
        return re.compile(r"\s+".join(groups))
    except re.error:
        return None


def parse_number_row(line: str) -> Optional[List[float]]:
    """把 points 块中的一行数字解析成 float 列表；不是数字行则返回 None。"""
    text = line.strip()
    if text.startswith("//"):
        text = text[2:].strip()
    if not text or not any(ch.isdigit() for ch in text):
        return None
    values: List[float] = []
    for token in SPLIT_RE.split(text):
        if not token:
            continue
        try:
            values.append(float(token))
        except ValueError:
            return None
    return values or None


class LineReader:
    """带行号、可回退一行、可读进度的行迭代器。"""

    def __init__(self, path: Path) -> None:
        self._file = path.open("r", encoding="utf-8", errors="replace")
        self.line_no = 0
        self._pending: Optional[Tuple[int, str]] = None
        self._eof = False

    def __iter__(self) -> "LineReader":
        return self

    def __next__(self) -> Tuple[int, str]:
        if self._pending is not None:
            item = self._pending
            self._pending = None
            self.line_no = item[0]
            return item
        if self._eof:
            raise StopIteration
        line = self._file.readline()
        if not line:
            self._eof = True
            raise StopIteration
        self.line_no += 1
        return self.line_no, line

    def push(self, line_no: int, line: str) -> None:
        self._pending = (line_no, line)
        self.line_no = line_no - 1

    def tell(self) -> int:
        try:
            return self._file.tell()
        except OSError:
            return 0

    def close(self) -> None:
        try:
            self._file.close()
        except OSError:
            pass


def _rows_to_matrix(rows: List[List[float]]) -> Optional[np.ndarray]:
    """把 points 块的行列表转成矩阵，形状不合法时返回 None。"""
    if len(rows) < 2:
        return None
    width = len(rows[0])
    if width < 2:
        return None
    for row in rows:
        if len(row) != width:
            return None
    return np.asarray(rows, dtype=float)


def _extract_filename(line: str) -> str:
    match = FILENAME_RE.search(line)
    return match.group("fn") if match else ""


def model_name(filename: str, max_len: int = 40) -> str:
    """从 gcode 全路径里取出模型名（去掉目录和扩展名）。"""
    text = (filename or "").replace("\\", "/").strip()
    if not text:
        return "（未记录模型）"
    text = text.rsplit("/", 1)[-1]
    lowered = text.lower()
    for suffix in (".gcode", ".gco", ".g"):
        if lowered.endswith(suffix):
            text = text[: -len(suffix)]
            break
    if not text:
        return "（未记录模型）"
    if max_len and len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


def parse_log(
    log_path: Path,
    mesh_tag: str,
    print_tag: str,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Tuple[List[MeshRecord], List[PrintEvent], int]:
    """扫描日志，返回（去重后的网床列表, 打印事件列表, 去重前的网床条数）。"""
    mesh_re = build_tag_pattern(mesh_tag)
    print_re = build_tag_pattern(print_tag)

    try:
        total_bytes = log_path.stat().st_size
    except OSError:
        total_bytes = 0

    meshes: List[MeshRecord] = []
    prints: List[PrintEvent] = []
    current: Optional[MeshRecord] = None
    meta_deadline = 0

    reader = LineReader(log_path)
    try:
        for line_no, line in reader:
            if progress is not None and line_no % 5000 == 0:
                progress(reader.tell(), total_bytes)

            # 1) 网床 points 起始行
            if mesh_re is not None and mesh_re.search(line):
                timestamp = parse_timestamp(line)
                rows: List[List[float]] = []
                last_line = line_no
                while True:
                    try:
                        next_no, next_line = next(reader)
                    except StopIteration:
                        break
                    values = parse_number_row(next_line)
                    if values is None:
                        reader.push(next_no, next_line)
                        break
                    rows.append(values)
                    last_line = next_no
                    if len(rows) >= MAX_POINTS_ROWS:
                        break

                current = None
                if timestamp is not None:
                    matrix = _rows_to_matrix(rows)
                    if matrix is not None:
                        current = MeshRecord(
                            timestamp=timestamp, line_no=line_no, points=matrix
                        )
                        meshes.append(current)
                        meta_deadline = last_line + META_LOOKAHEAD_LINES
                continue

            # 2) 打印开始
            if print_re is not None and print_re.search(line):
                timestamp = parse_timestamp(line)
                if timestamp is not None:
                    prints.append(
                        PrintEvent(
                            timestamp=timestamp,
                            line_no=line_no,
                            filename=_extract_filename(line),
                        )
                    )
                continue

            # 3) 本次网床的元数据（x_count / min_x / max_x ...）
            if current is not None:
                if line_no > meta_deadline:
                    current = None
                else:
                    match = MESH_KV_RE.search(line)
                    if match is not None:
                        current.meta[match.group("key")] = match.group("value").strip()
    finally:
        reader.close()

    if progress is not None:
        progress(total_bytes, total_bytes)

    raw_count = len(meshes)
    merged = dedupe_meshes(meshes)
    prints.sort(key=lambda item: (item.timestamp, item.line_no))
    attach_prints(prints, merged)
    return merged, prints, raw_count


def dedupe_meshes(
    meshes: List[MeshRecord],
    window_s: float = DUPLICATE_WINDOW_S,
    tol: float = DUPLICATE_TOL,
) -> List[MeshRecord]:
    """合并同一次测量的重复保存记录（时间接近且数值完全相同）。"""
    ordered = sorted(meshes, key=lambda item: (item.timestamp, item.line_no))
    merged: List[MeshRecord] = []
    for record in ordered:
        duplicate_of: Optional[MeshRecord] = None
        for previous in reversed(merged[-4:]):
            if (record.timestamp - previous.timestamp).total_seconds() > window_s:
                break
            if previous.points.shape != record.points.shape:
                continue
            if np.allclose(previous.points, record.points, rtol=0.0, atol=tol):
                duplicate_of = previous
                break
        if duplicate_of is not None:
            duplicate_of.dup_count += 1
            continue
        merged.append(record)
    return merged


def attach_prints(prints: List[PrintEvent], meshes: List[MeshRecord]) -> None:
    """为每次打印匹配“打印开始前最近的一次”网床。"""
    for mesh in meshes:
        mesh.usage_count = 0

    times = [mesh.timestamp for mesh in meshes]
    for event in prints:
        event.mesh_index = None
        if times:
            index = bisect.bisect_right(times, event.timestamp) - 1
            if index >= 0:
                event.mesh_index = index
                meshes[index].usage_count += 1


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #

def mesh_coordinates(mesh: MeshRecord) -> Tuple[np.ndarray, np.ndarray, bool]:
    """返回网格的 x、y 坐标；没有物理范围时退化为索引。"""
    rows, cols = mesh.points.shape

    def axis(lo_key: str, hi_key: str, count: int) -> Tuple[np.ndarray, bool]:
        try:
            low = float(mesh.meta[lo_key])
            high = float(mesh.meta[hi_key])
        except (KeyError, TypeError, ValueError):
            return np.arange(count, dtype=float), False
        if not high > low:
            return np.arange(count, dtype=float), False
        return np.linspace(low, high, count), True

    xs, ok_x = axis("min_x", "max_x", cols)
    ys, ok_y = axis("min_y", "max_y", rows)
    return xs, ys, bool(ok_x and ok_y)


def grid_extent(xs: np.ndarray, ys: np.ndarray) -> Tuple[float, float, float, float]:
    """把采样点坐标换算成 imshow 的 extent，使每个采样点落在格子中心。"""
    dx = float(xs[-1] - xs[0]) / (len(xs) - 1) if len(xs) > 1 else 1.0
    dy = float(ys[-1] - ys[0]) / (len(ys) - 1) if len(ys) > 1 else 1.0
    return (
        float(xs[0]) - dx / 2.0,
        float(xs[-1]) + dx / 2.0,
        float(ys[0]) - dy / 2.0,
        float(ys[-1]) + dy / 2.0,
    )


def cell_text_color(image: object, value: float) -> str:
    """按格子底色亮度选深色或浅色文字，保证标注可读。"""
    try:
        rgba = image.cmap(image.norm(value))  # type: ignore[attr-defined]
        luminance = 0.299 * rgba[0] + 0.587 * rgba[1] + 0.114 * rgba[2]
    except Exception:  # noqa: BLE001 - 取不到颜色时用深色即可
        return "#101010"
    return "#101010" if luminance > 0.55 else "#f7f7f7"


def annotate_cells(
    ax: object,
    z: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    image: object,
    scale: float = 1000.0,
) -> None:
    """在每个采样点标注数值（默认换算成 μm）。"""
    rows, cols = z.shape
    for iy in range(rows):
        for ix in range(cols):
            ax.text(  # type: ignore[attr-defined]
                float(xs[ix]),
                float(ys[iy]),
                "%.0f" % (float(z[iy, ix]) * scale),
                ha="center",
                va="center",
                fontsize=7.5,
                color=cell_text_color(image, float(z[iy, ix])),
            )


def compute_stats(mesh: MeshRecord) -> MeshStats:
    """计算单个网床的统计量。"""
    z = mesh.points
    flat = z.ravel()
    xs, ys, physical = mesh_coordinates(mesh)
    grid_x, grid_y = np.meshgrid(xs, ys)

    stats = MeshStats(
        vmin=float(flat.min()),
        vmax=float(flat.max()),
        mean=float(flat.mean()),
        span=float(flat.max() - flat.min()),
        std=float(flat.std(ddof=0)),
        rms=float(np.sqrt(np.mean(flat ** 2))),
        physical=physical,
    )

    design = np.column_stack([grid_x.ravel(), grid_y.ravel(), np.ones(flat.size)])
    try:
        coef, *_ = np.linalg.lstsq(design, flat, rcond=None)
        a, b, _c = (float(value) for value in coef)
        residual = flat - design @ coef
        stats.tilt_x = a * 100.0
        stats.tilt_y = b * 100.0
        stats.residual_rms = float(np.sqrt(np.mean(residual ** 2)))
    except (np.linalg.LinAlgError, ValueError):
        pass

    iy, ix = np.unravel_index(int(np.argmax(z)), z.shape)
    stats.peak = (float(xs[ix]), float(ys[iy]), float(z[iy, ix]))
    iy, ix = np.unravel_index(int(np.argmin(z)), z.shape)
    stats.pit = (float(xs[ix]), float(ys[iy]), float(z[iy, ix]))
    return stats


# --------------------------------------------------------------------------- #
# 命令行摘要
# --------------------------------------------------------------------------- #

def _safe_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass


def cli_main(argv: List[str]) -> int:
    _safe_stdout()
    args = [item for item in argv[1:] if item != "--cli"]
    log_path = Path(args[0]) if args else Path(DEFAULT_LOG)
    if not log_path.is_file():
        print("找不到日志文件: %s" % log_path)
        return 1

    print("日志文件: %s" % log_path)
    meshes, prints, raw_count = parse_log(log_path, DEFAULT_MESH_TAG, DEFAULT_PRINT_TAG)
    stats = [compute_stats(mesh) for mesh in meshes]

    print(
        "网床记录: %d 次（原始 points 块 %d 个，合并重复保存 %d 个）"
        % (len(meshes), raw_count, raw_count - len(meshes))
    )
    print("打印事件: %d 次" % len(prints))
    print("")

    header = "%-4s %-23s %-8s %-10s %10s  %s" % (
        "序号", "打印开始", "网床", "间隔(秒)", "网床均值", "打印模型",
    )
    print(header)
    print("-" * 110)
    for index, event in enumerate(prints, start=1):
        if event.mesh_index is None:
            mesh_text, gap_text, mean_text = "无", "-", "-"
        else:
            mesh = meshes[event.mesh_index]
            mesh_text = "#%d" % (event.mesh_index + 1)
            gap_text = "%.1f" % (event.timestamp - mesh.timestamp).total_seconds()
            mean_text = "%+.4f" % stats[event.mesh_index].mean
        print(
            "%-4d %-23s %-8s %-10s %10s  %s"
            % (index, event.stamp, mesh_text, gap_text, mean_text, event.model)
        )

    print("")
    print("%-4s %-23s %-6s %10s %10s %10s %8s" % (
        "序号", "网床保存时间", "尺寸", "均值", "极差", "标准差", "使用数",
    ))
    print("-" * 90)
    for index, (mesh, stat) in enumerate(zip(meshes, stats), start=1):
        print(
            "%-4d %-23s %-6s %10.4f %10.4f %10.4f %8d"
            % (
                index,
                mesh.stamp,
                "%dx%d" % (mesh.rows, mesh.cols),
                stat.mean,
                stat.span,
                stat.std,
                mesh.usage_count,
            )
        )
    return 0


# --------------------------------------------------------------------------- #
# 图形界面
# --------------------------------------------------------------------------- #

class MeshApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.meshes: List[MeshRecord] = []
        self.prints: List[PrintEvent] = []
        self.stats: List[MeshStats] = []
        self.current_index: Optional[int] = None
        self._queue: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        self._syncing = False

        self._build_ui()
        self.root.after(100, self._poll_queue)
        if Path(DEFAULT_LOG).is_file():
            self.log_var.set(DEFAULT_LOG)
            self.start_parse()

    # ---------------- 界面搭建 ----------------

    def _build_ui(self) -> None:
        root = self.root
        root.title("Klipper 网床日志分析")
        root.geometry("1400x900")
        root.minsize(1080, 680)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        # 顶部配置区
        top = ttk.Frame(root, padding=(10, 8, 10, 4))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="日志文件").grid(row=0, column=0, sticky="w")
        self.log_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.log_var).grid(
            row=0, column=1, sticky="ew", padx=6
        )
        ttk.Button(top, text="浏览...", command=self._browse).grid(row=0, column=2)
        ttk.Button(top, text="开始解析", command=self.start_parse).grid(
            row=0, column=3, padx=(6, 0)
        )

        ttk.Label(top, text="网床 tag").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.mesh_tag_var = tk.StringVar(value=DEFAULT_MESH_TAG)
        ttk.Entry(top, textvariable=self.mesh_tag_var).grid(
            row=1, column=1, sticky="ew", padx=6, pady=(6, 0)
        )
        ttk.Label(
            top,
            text="数字片段自动容错（:390 可匹配任意行号）",
            foreground="#666666",
        ).grid(row=1, column=2, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Label(top, text="打印 tag").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.print_tag_var = tk.StringVar(value=DEFAULT_PRINT_TAG)
        ttk.Entry(top, textvariable=self.print_tag_var).grid(
            row=2, column=1, sticky="ew", padx=6, pady=(6, 0)
        )

        # 中部左右分栏
        paned = ttk.PanedWindow(root, orient="horizontal")
        paned.grid(row=1, column=0, sticky="nsew", padx=10, pady=4)
        self.paned = paned

        left = ttk.Frame(paned)
        right = ttk.Frame(paned)
        paned.add(left, weight=6)
        paned.add(right, weight=7)
        # 左侧初始宽度要够放下“打印模型”列，否则模型名会被裁掉；之后仍可拖动分隔条
        self._sash_done = False
        paned.bind("<Configure>", self._on_paned_configure)

        self._build_left(left)
        self._build_right(right)

        # 底部状态栏
        bottom = ttk.Frame(root, padding=(10, 2, 10, 8))
        bottom.grid(row=2, column=0, sticky="ew")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left")
        ttk.Button(bottom, text="导出 CSV", command=self.export_csv).pack(side="right")
        self.progress = ttk.Progressbar(bottom, mode="determinate", length=240)
        self.progress.pack(side="right", padx=10)

    def _on_paned_configure(self, event: "tk.Event") -> None:
        """面板第一次拿到真实宽度后，把分隔条放到合适位置（只做一次）。"""
        if self._sash_done or event.width < 400:
            return
        self._sash_done = True
        target = min(max(event.width * 0.42, 540), event.width - 520)
        if 200 < target < event.width:
            try:
                self.paned.sashpos(0, int(target))
            except tk.TclError:
                pass

    def _build_left(self, parent: ttk.Frame) -> None:
        notebook = ttk.Notebook(parent)
        notebook.pack(fill="both", expand=True)

        prints_tab = ttk.Frame(notebook)
        notebook.add(prints_tab, text="打印任务")
        ttk.Label(
            prints_tab,
            text="间隔 = 打印开始 − 网床保存时间（秒），越大说明用的网床越旧",
            foreground="#666666",
        ).pack(side="bottom", fill="x", padx=6, pady=(2, 4))
        self.prints_tree = self._make_tree(
            prints_tab,
            columns=("no", "print_time", "mesh", "gap", "model"),
            headings=("序号", "打印开始", "网床", "间隔(秒)", "打印模型"),
            widths=(40, 92, 48, 84, 180),
        )
        self.prints_tree.column("model", anchor="w")
        self.prints_tree.bind("<<TreeviewSelect>>", self._on_print_select)

        mesh_tab = ttk.Frame(notebook)
        notebook.add(mesh_tab, text="网床记录")
        self.mesh_tree = self._make_tree(
            mesh_tab,
            columns=("no", "time", "mean", "span", "use"),
            headings=("序号", "保存时间", "均值", "极差", "使用"),
            widths=(46, 100, 78, 78, 56),
        )
        self.mesh_tree.bind("<<TreeviewSelect>>", self._on_mesh_select)

    @staticmethod
    def _make_tree(
        parent: ttk.Frame,
        columns: Tuple[str, ...],
        headings: Tuple[str, ...],
        widths: Tuple[int, ...],
    ) -> ttk.Treeview:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)

        tree = ttk.Treeview(frame, columns=columns, show="headings", height=20)
        for name, title, width in zip(columns, headings, widths):
            tree.heading(name, text=title)
            tree.column(name, width=width, minwidth=36, anchor="center", stretch=False)
        tree.column(columns[-1], stretch=True)

        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return tree

    def _build_right(self, parent: ttk.Frame) -> None:
        notebook = ttk.Notebook(parent)
        notebook.pack(fill="both", expand=True)

        # 热力图
        heat_tab = ttk.Frame(notebook)
        notebook.add(heat_tab, text="网床热力图")
        self.mesh_fig = Figure(figsize=(7.5, 5.4), dpi=100, constrained_layout=True)
        self.mesh_canvas = FigureCanvasTkAgg(self.mesh_fig, master=heat_tab)
        self.mesh_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.mesh_toolbar = NavigationToolbar2Tk(self.mesh_canvas, heat_tab)
        self.mesh_toolbar.update()
        self.mesh_toolbar.pack(side="bottom", fill="x")

        # 差分
        diff_tab = ttk.Frame(notebook)
        notebook.add(diff_tab, text="相对基准差分")
        bar = ttk.Frame(diff_tab, padding=(0, 4, 0, 0))
        bar.pack(fill="x")
        ttk.Label(bar, text="对比基准").pack(side="left")
        self.base_var = tk.StringVar(value=BASELINE_FIRST)
        self.base_combo = ttk.Combobox(
            bar, textvariable=self.base_var, state="readonly", width=26,
            values=[BASELINE_FIRST],
        )
        self.base_combo.pack(side="left", padx=6)
        self.base_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_diff())

        self.diff_fig = Figure(figsize=(7.5, 4.8), dpi=100, constrained_layout=True)
        self.diff_canvas = FigureCanvasTkAgg(self.diff_fig, master=diff_tab)
        self.diff_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.diff_toolbar = NavigationToolbar2Tk(self.diff_canvas, diff_tab)
        self.diff_toolbar.update()
        self.diff_toolbar.pack(side="bottom", fill="x")

        # 统计详情
        stats_tab = ttk.Frame(notebook)
        notebook.add(stats_tab, text="统计详情")
        self.stats_text = tk.Text(
            stats_tab,
            wrap="word",
            font=("Microsoft YaHei", 10),
            padx=10,
            pady=8,
            background="#fbfbfb",
        )
        scroll = ttk.Scrollbar(stats_tab, orient="vertical", command=self.stats_text.yview)
        self.stats_text.configure(yscrollcommand=scroll.set, state="disabled")
        self.stats_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.stats_text.tag_configure("title", font=("Microsoft YaHei", 12, "bold"))
        self.stats_text.tag_configure("head", font=("Microsoft YaHei", 10, "bold"))
        self.stats_text.tag_configure("warn", foreground="#b00000")

    # ---------------- 解析流程 ----------------

    def _browse(self) -> None:
        initial = Path(self.log_var.get().strip() or ".")
        chosen = filedialog.askopenfilename(
            title="选择 klippy 日志",
            initialdir=str(initial.parent if initial.parent.exists() else SCRIPT_DIR),
            filetypes=[("日志文件", "*.log*"), ("所有文件", "*.*")],
        )
        if chosen:
            self.log_var.set(chosen)

    def start_parse(self) -> None:
        path = Path(self.log_var.get().strip())
        if not path.is_file():
            messagebox.showerror("找不到日志", "日志文件不存在：\n%s" % path)
            return
        self.progress["value"] = 0
        self.status_var.set("正在解析 %s ..." % path.name)
        worker = threading.Thread(
            target=self._parse_worker,
            args=(path, self.mesh_tag_var.get(), self.print_tag_var.get()),
            daemon=True,
        )
        worker.start()

    def _parse_worker(self, path: Path, mesh_tag: str, print_tag: str) -> None:
        try:
            meshes, prints, raw_count = parse_log(
                path,
                mesh_tag,
                print_tag,
                progress=lambda done, total: self._queue.put(("progress", (done, total))),
            )
            stats = [compute_stats(mesh) for mesh in meshes]
            self._queue.put(
                ("parsed", ParseResult(path, meshes, prints, raw_count, stats))
            )
        except Exception:  # noqa: BLE001 - 展示完整堆栈比静默失败有用
            self._queue.put(("error", traceback.format_exc()))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "progress":
                    done, total = payload  # type: ignore[misc]
                    if total:
                        self.progress["value"] = min(100.0, done * 100.0 / total)
                elif kind == "parsed":
                    self._apply_result(payload)  # type: ignore[arg-type]
                elif kind == "error":
                    self.status_var.set("解析失败")
                    self.progress["value"] = 0
                    messagebox.showerror("解析失败", str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _apply_result(self, result: ParseResult) -> None:
        self.meshes = result.meshes
        self.prints = result.prints
        self.stats = result.stats
        self.current_index = None
        self.progress["value"] = 100

        self.prints_tree.delete(*self.prints_tree.get_children())
        self.mesh_tree.delete(*self.mesh_tree.get_children())

        for index, mesh in enumerate(self.meshes, start=1):
            stat = self.stats[index - 1]
            self.mesh_tree.insert(
                "", "end", iid=str(index - 1),
                values=(
                    "#%d" % index,
                    mesh.clock,
                    "%+.4f" % stat.mean,
                    "%.4f" % stat.span,
                    mesh.usage_count,
                ),
            )

        for index, event in enumerate(self.prints, start=1):
            if event.mesh_index is None:
                mesh_text, gap_text = "无", "-"
            else:
                mesh = self.meshes[event.mesh_index]
                gap_text = "%.1f" % (event.timestamp - mesh.timestamp).total_seconds()
                mesh_text = "#%d" % (event.mesh_index + 1)
            self.prints_tree.insert(
                "", "end", iid=str(index - 1),
                values=(
                    "#%d" % index,
                    event.clock,
                    mesh_text,
                    gap_text,
                    model_name(event.filename, 30),
                ),
            )

        self._fill_baseline_combo()

        self.status_var.set(
            "解析完成：%s ｜ 网床 %d 次（原始 %d 个 points 块，合并重复 %d 个）｜ 打印 %d 次"
            % (
                result.path.name,
                len(self.meshes),
                result.raw_mesh_count,
                result.raw_mesh_count - len(self.meshes),
                len(self.prints),
            )
        )

        if self.prints:
            self.prints_tree.selection_set("0")
            self.prints_tree.focus("0")
            self.prints_tree.see("0")
            self._show_mesh(self.prints[0].mesh_index, source=self.prints[0])
        elif self.meshes:
            self._show_mesh(0)

    def _fill_baseline_combo(self) -> None:
        values = [BASELINE_FIRST, BASELINE_PREV]
        for index, mesh in enumerate(self.meshes, start=1):
            values.append("#%d %s" % (index, mesh.clock))
        self.base_combo.configure(values=values)
        self.base_var.set(BASELINE_FIRST)

    # ---------------- 选择与绘图 ----------------

    def _on_print_select(self, _event: object = None) -> None:
        selection = self.prints_tree.selection()
        if not selection:
            return
        index = int(selection[0])
        if not 0 <= index < len(self.prints):
            return
        event = self.prints[index]
        self._show_mesh(event.mesh_index, source=event)

    def _on_mesh_select(self, _event: object = None) -> None:
        if self._syncing:
            return
        selection = self.mesh_tree.selection()
        if not selection:
            return
        index = int(selection[0])
        # 若已经显示的是这条网床，直接返回：SelectionSet 触发的虚拟事件是异步投递的，
        # 不能靠 _syncing 挡住，否则会和 _show_mesh 里的选中同步形成无限重绘循环。
        if index == self.current_index:
            return
        if 0 <= index < len(self.meshes):
            self._show_mesh(index)

    def _show_mesh(
        self, index: Optional[int], source: Optional[PrintEvent] = None
    ) -> None:
        if index is None or not 0 <= index < len(self.meshes):
            self.mesh_fig.clear()
            ax = self.mesh_fig.add_subplot(111)
            ax.text(
                0.5, 0.5,
                "该次打印之前没有任何网床记录",
                ha="center", va="center", fontsize=13, color="#b00000",
                transform=ax.transAxes,
            )
            ax.set_axis_off()
            self.mesh_canvas.draw_idle()
            if source is not None:
                self._set_stats_text(self._print_lines(source, None))
            return

        self.current_index = index
        mesh = self.meshes[index]
        stat = self.stats[index]

        if not self._syncing:
            self._syncing = True
            try:
                self.mesh_tree.selection_set(str(index))
                self.mesh_tree.see(str(index))
            finally:
                self._syncing = False

        self._draw_mesh_plot(mesh, stat)
        self._refresh_diff()
        self._set_stats_text(self._print_lines(source, index))

    def _draw_mesh_plot(self, mesh: MeshRecord, stat: MeshStats) -> None:
        figure = self.mesh_fig
        figure.clear()
        ax = figure.add_subplot(111)

        z = mesh.points
        rows, cols = z.shape
        xs, ys, physical = mesh_coordinates(mesh)
        extent = grid_extent(xs, ys)

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
        annotate_cells(ax, z, xs, ys, image)

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

    def _baseline_index(self) -> Optional[int]:
        if self.current_index is None or not self.meshes:
            return None
        text = self.base_var.get()
        if text == BASELINE_FIRST:
            return 0
        if text == BASELINE_PREV:
            return self.current_index - 1 if self.current_index > 0 else None
        match = re.match(r"#(\d+)", text)
        if match is None:
            return None
        index = int(match.group(1)) - 1
        return index if 0 <= index < len(self.meshes) else None

    def _refresh_diff(self) -> None:
        figure = self.diff_fig
        figure.clear()
        ax = figure.add_subplot(111)

        if self.current_index is None:
            ax.text(0.5, 0.5, "请先选择一条网床记录", ha="center", va="center",
                    transform=ax.transAxes, fontsize=12, color="#666666")
            ax.set_axis_off()
            self.diff_canvas.draw_idle()
            return

        base_index = self._baseline_index()
        mesh = self.meshes[self.current_index]

        if base_index is None:
            ax.text(0.5, 0.5, "没有可对比的基准网床", ha="center", va="center",
                    transform=ax.transAxes, fontsize=12, color="#b00000")
            ax.set_axis_off()
            self.diff_canvas.draw_idle()
            return

        if base_index == self.current_index:
            ax.text(0.5, 0.5, "当前网床就是基准本身，换一条网床或修改“对比基准”后可见差分",
                    ha="center", va="center",
                    transform=ax.transAxes, fontsize=11, color="#666666")
            ax.set_axis_off()
            self.diff_canvas.draw_idle()
            return

        base = self.meshes[base_index]
        if base.points.shape != mesh.points.shape:
            ax.text(0.5, 0.5, "两次网床尺寸不同，无法逐点比较（%dx%d 对 %dx%d）"
                    % (mesh.rows, mesh.cols, base.rows, base.cols),
                    ha="center", va="center", transform=ax.transAxes,
                    fontsize=11, color="#b00000")
            ax.set_axis_off()
            self.diff_canvas.draw_idle()
            return

        diff = mesh.points - base.points
        limit = float(np.abs(diff).max()) or 1e-9
        xs, ys, physical = mesh_coordinates(mesh)
        extent = grid_extent(xs, ys)

        image = ax.imshow(
            diff, origin="lower", extent=extent, cmap="RdBu_r",
            vmin=-limit, vmax=limit, aspect="auto", interpolation="nearest",
        )
        annotate_cells(ax, diff, xs, ys, image)

        ax.set_title(
            "网床 #%d − 基准 #%d   最大差 %.4f mm   均值差 %+.4f mm   RMS %.4f mm"
            % (
                self.current_index + 1,
                base_index + 1,
                float(np.abs(diff).max()),
                float(diff.mean()),
                float(np.sqrt(np.mean(diff ** 2))),
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

    def _print_lines(
        self, source: Optional[PrintEvent], index: Optional[int]
    ) -> List[Tuple[str, str]]:
        """生成统计详情文本（(text, tag) 列表）。"""
        lines: List[Tuple[str, str]] = []

        if source is not None:
            lines.append(("打印任务", "title"))
            lines.append(("", ""))
            lines.append(("  打印开始时间：%s" % source.stamp, ""))
            lines.append(("  打印模型：%s" % model_name(source.filename, 0), "head"))
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
                    ("  该网床共被 %d 次打印使用（日志中相邻的几次打印共用同一份网床）"
                     % mesh.usage_count, "")
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
                ("  测量范围：X %s ~ %s mm ／ Y %s ~ %s mm" % (
                    mesh.meta.get("min_x"), mesh.meta.get("max_x"),
                    mesh.meta.get("min_y", "?"), mesh.meta.get("max_y", "?"),
                ), "")
            )
        if mesh.meta.get("algo"):
            lines.append((
                "  插值算法：%s    张力：%s"
                % (mesh.meta.get("algo"), mesh.meta.get("tension", "?")), ""
            ))
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
            lines.append((
                "  平面拟合倾斜：X 方向 %+.4f mm/100mm（约 %.3f°），"
                "Y 方向 %+.4f mm/100mm（约 %.3f°）"
                % (stat.tilt_x, np.degrees(np.arctan(stat.tilt_x / 100.0)),
                   stat.tilt_y, np.degrees(np.arctan(stat.tilt_y / 100.0))), ""
            ))
        if stat.residual_rms is not None:
            share = (stat.residual_rms / stat.std * 100.0) if stat.std > 1e-12 else 0.0
            lines.append((
                "  去除平面后的残差 RMS：%.6f mm（占整体标准差 %.1f%%）"
                % (stat.residual_rms, share), ""
            ))
        if stat.peak is not None:
            lines.append((
                "  最凸点：X=%.1f Y=%.1f（%s）  z=%+.6f mm"
                % (stat.peak[0], stat.peak[1], stat.axis_unit, stat.peak[2]), ""
            ))
        if stat.pit is not None:
            lines.append((
                "  最凹点：X=%.1f Y=%.1f（%s）  z=%+.6f mm"
                % (stat.pit[0], stat.pit[1], stat.axis_unit, stat.pit[2]), ""
            ))

        base_index = self._baseline_index()
        if base_index is not None and base_index != index:
            base = self.meshes[base_index]
            if base.points.shape == mesh.points.shape:
                diff = mesh.points - base.points
                lines.append(("", ""))
                lines.append(("相对基准 #%d（%s）的差异" % (base_index + 1, base.clock), "head"))
                lines.append(("", ""))
                lines.append(("  平均差：%+.6f mm" % float(diff.mean()), ""))
                lines.append(("  最大绝对差：%.6f mm" % float(np.abs(diff).max()), ""))
                lines.append(
                    ("  差分 RMS：%.6f mm" % float(np.sqrt(np.mean(diff ** 2))), "")
                )
                lines.append((
                    "  相对基准的整体抬升/下沉：%s %.6f mm"
                    % ("抬升" if diff.mean() >= 0 else "下沉", abs(float(diff.mean()))), ""
                ))

        lines.append(("", ""))
        lines.append((
            "  该网床在日志中被保存 %d 次（重复保存已合并）" % mesh.dup_count, ""
        ))
        return lines

    def _set_stats_text(self, lines: List[Tuple[str, str]]) -> None:
        self.stats_text.configure(state="normal")
        self.stats_text.delete("1.0", "end")
        for text, tag in lines:
            if tag:
                self.stats_text.insert("end", text + "\n", tag)
            else:
                self.stats_text.insert("end", text + "\n")
        self.stats_text.configure(state="disabled")

    # ---------------- 导出 ----------------

    def export_csv(self) -> None:
        if not self.meshes and not self.prints:
            messagebox.showinfo("无数据", "请先解析日志。")
            return

        target = filedialog.asksaveasfilename(
            title="导出分析结果",
            defaultextension=".csv",
            initialdir=str(SCRIPT_DIR),
            initialfile=DEFAULT_EXPORT.name,
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not target:
            return

        try:
            with Path(target).open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                self._write_task_section(writer)
                self._write_mesh_section(writer)
                self._write_matrix_section(writer)
                self._write_diff_section(writer)
        except OSError as error:
            messagebox.showerror("导出失败", str(error))
            return

        self.status_var.set("已导出：%s" % target)
        messagebox.showinfo("导出完成", "已导出到：\n%s" % target)

    def _write_task_section(self, writer: "csv.writer") -> None:
        writer.writerow(["# 打印任务（每次打印取开始时间之前最近的一次网床）"])
        writer.writerow([
            "序号", "打印开始", "打印模型", "gcode 文件", "网床序号", "网床保存时间",
            "间隔(秒)", "网床均值(mm)", "极差(mm)", "标准差(mm)",
            "倾斜X(mm/100mm)", "倾斜Y(mm/100mm)",
        ])
        for index, event in enumerate(self.prints, start=1):
            if event.mesh_index is None:
                writer.writerow([
                    index, event.stamp, event.model, event.filename,
                    "无", "", "", "", "", "", "", "", "",
                ])
                continue
            mesh = self.meshes[event.mesh_index]
            stat = self.stats[event.mesh_index]
            writer.writerow([
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
            ])
        writer.writerow([])

    def _write_mesh_section(self, writer: "csv.writer") -> None:
        writer.writerow(["# 网床记录统计"])
        writer.writerow([
            "序号", "保存时间", "日志行号", "行数", "列数", "最低(mm)", "最高(mm)",
            "均值(mm)", "极差(mm)", "标准差(mm)", "RMS(mm)", "平面残差RMS(mm)",
            "倾斜X(mm/100mm)", "倾斜Y(mm/100mm)", "最凸点", "最凹点",
            "重复保存次数", "被打印使用次数",
        ])
        for index, (mesh, stat) in enumerate(zip(self.meshes, self.stats), start=1):
            writer.writerow([
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
            ])
        writer.writerow([])

    def _write_matrix_section(self, writer: "csv.writer") -> None:
        writer.writerow(["# 网床矩阵（单位 mm）"])
        for index, (mesh, stat) in enumerate(zip(self.meshes, self.stats), start=1):
            writer.writerow([
                "mesh_%d  %s  均值=%.6f  极差=%.6f" % (index, mesh.stamp, stat.mean, stat.span)
            ])
            writer.writerow(
                ["row%d\\col%d" % (0, col) for col in range(mesh.cols)] + ["均值"]
            )
            for row_index in range(mesh.rows):
                values = ["%.6f" % value for value in mesh.points[row_index]]
                writer.writerow(values + ["%.6f" % float(mesh.points[row_index].mean())])
            writer.writerow([])
        writer.writerow([])

    def _write_diff_section(self, writer: "csv.writer") -> None:
        if len(self.meshes) < 2:
            return
        base = self.meshes[0]
        writer.writerow(["# 差分矩阵（相对第一条网床 mesh_1）"])
        for index, mesh in enumerate(self.meshes, start=1):
            if index == 1 or mesh.points.shape != base.points.shape:
                continue
            diff = mesh.points - base.points
            writer.writerow([
                "mesh_%d_minus_mesh_1  %s  均值差=%+.6f  最大绝对差=%.6f  RMS=%.6f"
                % (
                    index,
                    mesh.stamp,
                    float(diff.mean()),
                    float(np.abs(diff).max()),
                    float(np.sqrt(np.mean(diff ** 2))),
                )
            ])
            writer.writerow(["col%d" % col for col in range(mesh.cols)] + ["均值"])
            for row_index in range(mesh.rows):
                values = ["%.6f" % value for value in diff[row_index]]
                writer.writerow(values + ["%.6f" % float(diff[row_index].mean())])
            writer.writerow([])


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def enable_high_dpi() -> None:
    """Windows 高 DPI 下避免界面发虚。"""
    if sys.platform != "win32":
        return
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - 高 DPI 只是锦上添花
        pass


def main() -> int:
    if "--cli" in sys.argv:
        return cli_main(sys.argv)

    enable_high_dpi()
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista")
    except tk.TclError:
        pass
    MeshApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
