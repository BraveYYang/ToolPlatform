"""ToolPlatform 插件：首层不良面积计算。

原工具是 网床研究 目录下的独立 Tkinter 程序（首层不良面积计算.py）。
本插件把界面重写为 PyQt6，业务计算仍按文件路径动态加载原脚本中的纯函数
（load_image / bgr_to_rgb / order_points / output_size / warp_image /
polygon_area / POINT_LABELS / DEFECT_COLORS），原脚本保持只读、不做修改。

流程（与原文一致）:
  1. 打开首层图片，S1 默认为整张图片宽×高
  2. 「全首层标定」（可选）: 依次点 4 个角，透视校正为矩形后，S1 改为校正矩形面积
  3. 「追加不良面积」: 依次点多边形顶点，点「闭合当前不良」得到 S2、S3…，
     并更新不良合计 / S1
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import matplotlib

# 平台内必须使用 Qt 后端，避免与宿主窗口混用 Tk 事件循环
matplotlib.use("QtAgg")

from matplotlib.backends.backend_qtagg import (  # noqa: E402
    FigureCanvasQTAgg,
    NavigationToolbar2QT,
)
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.patches import Polygon as MplPolygon  # noqa: E402

from PyQt6.QtGui import QFont  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


PLUGIN_META = {
    "id": "first_layer_defect",
    "name": "首层不良面积计算",
    "version": "1.1.0",
    "category": "网床研究",
    "description": (
        "打开首层图片，标定首层四角并框选不良多边形，"
        "实时统计 S1、各不良面积与占比（单位：像素²）。"
    ),
    "order": 30,
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
SOURCE_SCRIPT = SOURCE_DIR / "首层不良面积计算.py"

_SOURCE_MODULE_NAME = "toolplatform_wc_first_layer"
_source_module: Optional[object] = None


def apply_matplotlib_style() -> None:
    """统一中文字体与负号显示，避免图中中文变成方块。"""
    matplotlib.rcParams["font.sans-serif"] = [
        "Microsoft YaHei",
        "SimHei",
        "Noto Sans CJK SC",
        "DejaVu Sans",
    ]
    matplotlib.rcParams["axes.unicode_minus"] = False


apply_matplotlib_style()


def load_source_module():
    """按文件路径加载原始脚本模块；只取纯函数，不创建 Tkinter 窗口。"""
    global _source_module
    if _source_module is not None:
        return _source_module
    if not SOURCE_SCRIPT.is_file():
        raise FileNotFoundError(f"找不到原始脚本：{SOURCE_SCRIPT}")
    spec = importlib.util.spec_from_file_location(_SOURCE_MODULE_NAME, SOURCE_SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法创建原始脚本的模块加载器：{SOURCE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_SOURCE_MODULE_NAME] = module
    spec.loader.exec_module(module)

    # 原脚本在模块顶层导入了 TkAgg 后端，加载后把全局后端与字体设置恢复为 Qt，
    # 保证平台内的图形始终由 QtAgg 渲染。
    matplotlib.use("QtAgg")
    apply_matplotlib_style()

    _source_module = module
    return module


class FirstLayerDefectWidget(QWidget):
    """首层不良面积计算的 Qt 界面：工具栏 + 汇总行 + matplotlib 画布。"""

    IMAGE_FILTER = "图像文件 (*.jpg *.jpeg *.png *.bmp *.tif *.tiff);;全部文件 (*.*)"
    HINT_TEXT = (
        "标定: 左键依次点 4 角（建议左上→右上→右下→左下）  |  "
        "不良: 左键加点，至少 3 点后点「闭合当前不良」  |  "
        "右键撤销一点"
    )

    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        self._module = None

        # 业务状态，命名与原始脚本保持一致
        self.image_path = ""
        self.orig = None
        self.warped = None
        self.s1 = 0.0
        self.mode = "idle"  # idle / calib / defect
        self.calib_pts: List[Tuple[float, float]] = []
        self.draft_pts: List[Tuple[float, float]] = []
        self.defects: List[List[Tuple[float, float]]] = []

        self._build_ui()
        self._refresh_source_state()
        if not SOURCE_SCRIPT.is_file():
            self._set_status("未找到原始脚本，本插件功能不可用。请按上方提示检查路径。")
        self._redraw()

    # ------------------------------------------------------------------ 界面

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(8)

        title = QLabel(PLUGIN_META["name"])
        title.setObjectName("pluginTitle")
        layout.addWidget(title)

        description = QLabel(
            "打开首层图片后可直接框选不良区域；也可先点选 4 个角做透视校正，"
            "把 S1 换成校正矩形面积。计算逻辑复用 网床研究 目录下原脚本的纯函数，"
            "原文件保持不变。"
        )
        description.setObjectName("pluginDescription")
        description.setWordWrap(True)
        layout.addWidget(description)

        self.source_label = QLabel()
        self.source_label.setObjectName("infoLabel")
        self.source_label.setWordWrap(True)
        layout.addWidget(self.source_label)

        path_row = QHBoxLayout()
        self.open_button = QPushButton("打开图片…")
        self.open_button.clicked.connect(self.open_image)
        path_row.addWidget(self.open_button)

        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("尚未选择图片")
        path_row.addWidget(self.path_edit, 1)
        layout.addLayout(path_row)

        action_row = QHBoxLayout()
        self.calib_button = QPushButton("全首层标定")
        self.calib_button.clicked.connect(self.start_calib)
        action_row.addWidget(self.calib_button)

        self.defect_button = QPushButton("追加不良面积")
        self.defect_button.clicked.connect(self.start_defect)
        action_row.addWidget(self.defect_button)

        self.close_button = QPushButton("闭合当前不良")
        self.close_button.clicked.connect(self.close_defect)
        action_row.addWidget(self.close_button)

        self.undo_point_button = QPushButton("撤销一点")
        self.undo_point_button.clicked.connect(self.undo_point)
        action_row.addWidget(self.undo_point_button)

        self.undo_defect_button = QPushButton("撤销上一块不良")
        self.undo_defect_button.clicked.connect(self.undo_defect)
        action_row.addWidget(self.undo_defect_button)

        self.clear_button = QPushButton("清空不良")
        self.clear_button.clicked.connect(self.clear_defects)
        action_row.addWidget(self.clear_button)

        action_row.addStretch()
        layout.addLayout(action_row)

        self.status_label = QLabel("请打开一张首层图片")
        self.status_label.setObjectName("infoLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.summary_label = QLabel("S1 未标定")
        self.summary_label.setObjectName("strongLabel")
        summary_font = QFont(self.summary_label.font())
        summary_font.setPointSize(11)
        summary_font.setBold(True)
        self.summary_label.setFont(summary_font)
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        chart = QVBoxLayout()
        chart.setSpacing(4)
        self.fig = Figure(figsize=(11, 7), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_axis_off()
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.canvas.setMinimumHeight(360)
        chart.addWidget(self.canvas, 1)
        self.toolbar = NavigationToolbar2QT(self.canvas, self)
        chart.addWidget(self.toolbar)
        layout.addLayout(chart, 1)

        hint = QLabel(self.HINT_TEXT)
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._click_cid = self.canvas.mpl_connect("button_press_event", self._on_click)

    def shutdown(self) -> None:
        """宿主退出时断开画布事件，避免回调持有已销毁的控件。"""
        try:
            self.canvas.mpl_disconnect(self._click_cid)
        except Exception:
            pass

    def _refresh_source_state(self) -> None:
        """展示原始脚本位置；缺失时给出清晰提示（按钮仍可点，点击时再提示）。"""
        if SOURCE_SCRIPT.is_file():
            self.source_label.setStyleSheet("")
            self.source_label.setText(f"原始脚本：{SOURCE_SCRIPT}")
        else:
            self.source_label.setStyleSheet("color: #d9534f;")
            self.source_label.setText(
                f"未找到原始脚本：{SOURCE_SCRIPT}\n"
                "请确认路径存在，或设置环境变量 TOOLPLATFORM_WANGCHUANG_DIR "
                "指向 网床研究 目录。"
            )

    def _require_module(self):
        """惰性加载原始脚本模块；失败时在界面上给出提示并返回 None。"""
        if self._module is not None:
            return self._module
        try:
            self._module = load_source_module()
        except Exception as exc:  # 原脚本缺失或依赖不全都不能影响平台
            self.source_label.setStyleSheet("color: #d9534f;")
            self.source_label.setText(
                f"原始脚本不可用：{type(exc).__name__}: {exc}\n"
                "请确认路径存在，或设置环境变量 TOOLPLATFORM_WANGCHUANG_DIR "
                "指向 网床研究 目录。"
            )
            self._set_status("无法加载原始脚本，本插件功能不可用。请按上方提示检查路径。", notify=True)
            return None
        self._refresh_source_state()
        return self._module

    # ------------------------------------------------------------------ 工具

    def _set_status(self, message: str, notify: bool = False) -> None:
        self.status_label.setText(message)
        if notify:
            try:
                self.context.show_status(message, 6000)
            except Exception:
                pass

    def _initial_dir(self) -> Path:
        if SOURCE_DIR.is_dir():
            return SOURCE_DIR
        data_dir = getattr(self.context, "data_dir", None)
        if data_dir:
            return Path(data_dir)
        return Path.home()

    # ------------------------------------------------------------------ 动作

    def open_image(self) -> None:
        if self._require_module() is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择首层图片", str(self._initial_dir()), self.IMAGE_FILTER
        )
        if path:
            self._load_path(path)

    def _load_path(self, path: str) -> None:
        module = self._require_module()
        if module is None:
            return
        try:
            image = module.load_image(str(path))
        except Exception as exc:
            QMessageBox.critical(self, "打开失败", str(exc))
            self._set_status(f"打开失败：{exc}")
            return
        self._install_image(image, str(path))

    def _install_image(self, image, path: str) -> None:
        """装载已解码的 BGR 图像并重置全部标注状态。"""
        if self._require_module() is None:
            return
        self.image_path = str(path)
        self.path_edit.setText(self.image_path)
        self.orig = image
        self.warped = None
        self.mode = "idle"
        self.calib_pts = []
        self.draft_pts = []
        self.defects = []
        h, w = image.shape[:2]
        self.s1 = float(w * h)
        name = os.path.basename(self.image_path) or "未命名"
        self._set_status(
            "已加载 %s  (%dx%d)，S1 默认为整图面积 %.1f。"
            "可直接「追加不良面积」，或先「全首层标定」。" % (name, w, h, self.s1),
            notify=True,
        )
        self._refresh_summary()
        self._redraw()

    def start_calib(self) -> None:
        if self._require_module() is None:
            return
        if self.orig is None:
            QMessageBox.warning(self, "提示", "请先打开首层图片")
            return
        self.mode = "calib"
        self.calib_pts = []
        self.warped = None
        h, w = self.orig.shape[:2]
        self.s1 = float(w * h)
        self.defects = []
        self.draft_pts = []
        self._set_status(
            "全首层标定：依次点击 4 个角点（左上→右上→右下→左下）。完成前 S1 仍为整图面积。",
            notify=True,
        )
        self._refresh_summary()
        self._redraw()

    def start_defect(self) -> None:
        if self._require_module() is None:
            return
        if self.orig is None or self.s1 <= 0:
            QMessageBox.warning(self, "提示", "请先打开首层图片")
            return
        self.mode = "defect"
        self.draft_pts = []
        n = len(self.defects) + 2
        self._set_status(
            "追加不良 S%d：左键依次点多边形顶点，至少 3 点后点「闭合当前不良」" % n,
            notify=True,
        )
        self._redraw()

    def close_defect(self) -> None:
        module = self._require_module()
        if module is None:
            return
        if self.mode != "defect":
            return
        if len(self.draft_pts) < 3:
            QMessageBox.warning(self, "提示", "多边形至少需要 3 个点")
            return
        self.defects.append(list(self.draft_pts))
        self.draft_pts = []
        self.mode = "idle"
        area = module.polygon_area(self.defects[-1])
        idx = len(self.defects) + 1
        self._set_status(
            "已追加 S%d = %.1f 像素²。可继续点「追加不良面积」。" % (idx, area),
            notify=True,
        )
        self._refresh_summary()
        self._redraw()

    def undo_point(self) -> None:
        if self.mode == "calib" and self.calib_pts:
            self.calib_pts.pop()
            self._set_status("全首层标定：已选 %d/4 点" % len(self.calib_pts))
        elif self.mode == "defect" and self.draft_pts:
            self.draft_pts.pop()
            self._set_status("当前不良已选 %d 点" % len(self.draft_pts))
        self._redraw()

    def undo_defect(self) -> None:
        module = self._require_module()
        if module is None:
            return
        if not self.defects:
            return
        self.defects.pop()
        self.draft_pts = []
        self.mode = "idle"
        self._set_status(
            "已撤销上一块不良。剩余 %d 块。" % len(self.defects), notify=True
        )
        self._refresh_summary()
        self._redraw()

    def clear_defects(self) -> None:
        if self._require_module() is None:
            return
        self.defects = []
        self.draft_pts = []
        if self.mode == "defect":
            self.mode = "idle"
        self._set_status("已清空全部不良面积", notify=True)
        self._refresh_summary()
        self._redraw()

    # ------------------------------------------------------------- 画布交互

    def _on_click(self, event) -> None:
        if event.inaxes is not self.ax:
            return
        if event.xdata is None or event.ydata is None:
            return
        if event.button == 3:
            self.undo_point()
            return
        if event.button != 1:
            return
        module = self._module
        if module is None:
            return
        pt = (float(event.xdata), float(event.ydata))
        if self.mode == "calib":
            if len(self.calib_pts) >= 4:
                return
            self.calib_pts.append(pt)
            n = len(self.calib_pts)
            labels = module.POINT_LABELS
            self._set_status("全首层标定：已选 %d/4 点（%s）" % (n, labels[n - 1]))
            self._redraw()
            if n == 4:
                self._finish_calib()
        elif self.mode == "defect":
            self.draft_pts.append(pt)
            self._set_status(
                "当前不良 S%d：已选 %d 点（≥3 点后闭合）"
                % (len(self.defects) + 2, len(self.draft_pts))
            )
            self._redraw()

    def _finish_calib(self) -> None:
        module = self._require_module()
        if module is None or self.orig is None:
            return
        try:
            self.warped = module.warp_image(self.orig, self.calib_pts)
        except Exception as exc:
            QMessageBox.critical(self, "校正失败", str(exc))
            self.calib_pts = []
            self._redraw()
            return
        h, w = self.warped.shape[:2]
        self.s1 = float(w * h)
        self.mode = "idle"
        self.defects = []
        self.draft_pts = []
        self._set_status(
            "标定完成：矩形 %d×%d，S1=%.1f 像素²。可点「追加不良面积」。" % (w, h, self.s1),
            notify=True,
        )
        self._refresh_summary()
        self._redraw()

    # --------------------------------------------------------------- 绘制

    def _current_image(self):
        if self.warped is not None:
            return self.warped
        return self.orig

    def _refresh_summary(self) -> None:
        module = self._module
        if module is None or self.s1 <= 0:
            self.summary_label.setText("S1 未标定")
            return
        parts = ["S1=%.1f" % self.s1]
        total_bad = 0.0
        for i, poly in enumerate(self.defects, start=2):
            area = module.polygon_area(poly)
            total_bad += area
            parts.append("S%d=%.1f" % (i, area))
        ratio = 100.0 * total_bad / self.s1
        parts.append("不良合计=%.1f" % total_bad)
        parts.append("占比=%.3f%%" % ratio)
        self.summary_label.setText("  |  ".join(parts) + "   （单位: 像素²）")

    def _redraw(self) -> None:
        self.ax.clear()
        self.ax.set_axis_off()
        module = self._module
        image = self._current_image()
        if module is None or image is None:
            self.ax.set_title("未加载图片")
            self.canvas.draw_idle()
            return

        self.ax.imshow(module.bgr_to_rgb(image))
        title = os.path.basename(self.image_path) or "未命名"
        if self.warped is not None:
            h, w = self.warped.shape[:2]
            title += "  |  校正 %dx%d" % (w, h)
        self.ax.set_title(title)

        labels = module.POINT_LABELS
        colors = module.DEFECT_COLORS

        if self.warped is None and self.calib_pts:
            xs = [p[0] for p in self.calib_pts]
            ys = [p[1] for p in self.calib_pts]
            self.ax.plot(xs, ys, "o-", color="#00c800", markersize=8, linewidth=2)
            if len(self.calib_pts) == 4:
                self.ax.plot(
                    [xs[-1], xs[0]], [ys[-1], ys[0]], "-", color="#00c800", linewidth=2
                )
            for i, (x, y) in enumerate(self.calib_pts):
                self.ax.text(
                    x + 8,
                    y - 8,
                    "%d:%s" % (i + 1, labels[i]),
                    color="#00c800",
                    fontsize=10,
                )

        for i, poly in enumerate(self.defects):
            color = colors[i % len(colors)]
            patch = MplPolygon(
                poly,
                closed=True,
                facecolor=color,
                alpha=0.28,
                edgecolor=color,
                linewidth=2,
            )
            self.ax.add_patch(patch)
            area = module.polygon_area(poly)
            cx = sum(p[0] for p in poly) / len(poly)
            cy = sum(p[1] for p in poly) / len(poly)
            self.ax.text(
                cx,
                cy,
                "S%d\n%.0f" % (i + 2, area),
                color=color,
                ha="center",
                va="center",
                fontsize=9,
            )

        if self.draft_pts:
            xs = [p[0] for p in self.draft_pts]
            ys = [p[1] for p in self.draft_pts]
            self.ax.plot(xs, ys, "o--", color="#ffdd00", markersize=7, linewidth=1.5)
            for i, (x, y) in enumerate(self.draft_pts):
                self.ax.text(x + 6, y - 6, str(i + 1), color="#ffdd00", fontsize=9)

        self.canvas.draw_idle()


def create_plugin(context, parent=None) -> QWidget:
    return FirstLayerDefectWidget(context, parent)
