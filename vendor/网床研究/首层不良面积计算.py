#!F:\anaconda3\envs\Mrlzr\python.exe
# -*- coding: utf-8 -*-
"""首层不良面积计算。

流程:
  1. 打开首层图片，S1 默认为整张图片宽×高
  2. 「全首层标定」（可选）: 依次点 4 个角，透视校正为矩形后，S1 改为校正矩形面积
  3. 「追加不良面积」: 依次点多边形顶点，点「闭合当前不良」得到 S2、S3…，并更新不良合计 / S1

运行:
  F:\\anaconda3\\envs\\Mrlzr\\python.exe 首层不良面积计算.py
  F:\\anaconda3\\envs\\Mrlzr\\python.exe 首层不良面积计算.py 首层.jpg
"""

from __future__ import annotations

import argparse
import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import List, Optional, Tuple

import cv2
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
from matplotlib.patches import Polygon as MplPolygon

Point = Tuple[float, float]
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
POINT_LABELS = ("左上", "右上", "右下", "左下")
DEFECT_COLORS = (
    "#d62728",
    "#ff7f0e",
    "#2ca02c",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#17becf",
    "#bcbd22",
)


def load_image(path: str) -> np.ndarray:
    data = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise FileNotFoundError("无法读取图像: %s" % path)
    return image


def bgr_to_rgb(image: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


def order_points(pts: np.ndarray) -> np.ndarray:
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    diff = np.diff(pts, axis=1).reshape(-1)
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]
    return rect


def output_size(rect: np.ndarray) -> Tuple[int, int]:
    tl, tr, br, bl = rect
    width = int(round(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))))
    height = int(round(max(np.linalg.norm(br - tr), np.linalg.norm(bl - tl))))
    return max(width, 1), max(height, 1)


def warp_image(image: np.ndarray, pts: List[Point]) -> np.ndarray:
    src = order_points(np.array(pts, dtype=np.float32))
    width, height = output_size(src)
    dst = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(image, matrix, (width, height))


def polygon_area(pts: List[Point]) -> float:
    arr = np.asarray(pts, dtype=np.float32)
    return float(abs(cv2.contourArea(arr)))


class FirstLayerDefectApp:
    def __init__(self, root: tk.Tk, image_path: Optional[str] = None) -> None:
        self.root = root
        self.root.title("首层不良面积计算")
        self.root.geometry("1280x860")

        self.image_path = tk.StringVar(value=image_path or "")
        self.status = tk.StringVar(value="请打开一张首层图片")
        self.summary = tk.StringVar(value="S1 未标定")

        self.orig: Optional[np.ndarray] = None
        self.warped: Optional[np.ndarray] = None
        self.s1 = 0.0  # 默认整张当前图面积；标定后改为校正矩形面积
        self.mode = "idle"  # idle / calib / defect
        self.calib_pts: List[Point] = []
        self.draft_pts: List[Point] = []
        self.defects: List[List[Point]] = []

        self._build_ui()
        if image_path and os.path.isfile(image_path):
            self._load_path(image_path)

    def _build_ui(self) -> None:
        bar = ttk.Frame(self.root, padding=6)
        bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(bar, text="打开图片…", command=self.open_image).pack(side=tk.LEFT, padx=2)
        ttk.Entry(bar, textvariable=self.image_path, width=56).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="全首层标定", command=self.start_calib).pack(side=tk.LEFT, padx=6)
        ttk.Button(bar, text="追加不良面积", command=self.start_defect).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="闭合当前不良", command=self.close_defect).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="撤销一点", command=self.undo_point).pack(side=tk.LEFT, padx=6)
        ttk.Button(bar, text="撤销上一块不良", command=self.undo_defect).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="清空不良", command=self.clear_defects).pack(side=tk.LEFT, padx=2)

        ttk.Label(self.root, textvariable=self.status).pack(side=tk.TOP, anchor="w", padx=8, pady=(0, 2))
        ttk.Label(self.root, textvariable=self.summary, font=("Microsoft YaHei", 11, "bold")).pack(
            side=tk.TOP, anchor="w", padx=8, pady=(0, 4)
        )

        frame = ttk.Frame(self.root)
        frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.fig = Figure(figsize=(11, 7), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_axis_off()
        self.canvas = FigureCanvasTkAgg(self.fig, master=frame)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.toolbar = NavigationToolbar2Tk(self.canvas, frame)
        self.toolbar.update()
        self.canvas.mpl_connect("button_press_event", self._on_click)

        hint = (
            "标定: 左键依次点 4 角（建议左上→右上→右下→左下）  |  "
            "不良: 左键加点，至少 3 点后点「闭合当前不良」  |  "
            "右键撤销一点"
        )
        ttk.Label(self.root, text=hint).pack(side=tk.BOTTOM, anchor="w", padx=8, pady=4)

    def open_image(self) -> None:
        path = filedialog.askopenfilename(
            title="选择首层图片",
            filetypes=[
                ("图像文件", "*.jpg *.jpeg *.png *.bmp *.tif *.tiff"),
                ("全部文件", "*.*"),
            ],
            initialdir=_SCRIPT_DIR,
        )
        if path:
            self._load_path(path)

    def _load_path(self, path: str) -> None:
        try:
            image = load_image(path)
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))
            return
        self.image_path.set(path)
        self.orig = image
        self.warped = None
        self.mode = "idle"
        self.calib_pts = []
        self.draft_pts = []
        self.defects = []
        h, w = image.shape[:2]
        self.s1 = float(w * h)
        self.status.set(
            "已加载 %s  (%dx%d)，S1 默认为整图面积 %.1f。可直接「追加不良面积」，或先「全首层标定」。"
            % (os.path.basename(path), w, h, self.s1)
        )
        self._refresh_summary()
        self._redraw()

    def start_calib(self) -> None:
        if self.orig is None:
            messagebox.showwarning("提示", "请先打开首层图片")
            return
        self.mode = "calib"
        self.calib_pts = []
        self.warped = None
        h, w = self.orig.shape[:2]
        self.s1 = float(w * h)
        self.defects = []
        self.draft_pts = []
        self.status.set("全首层标定：依次点击 4 个角点（左上→右上→右下→左下）。完成前 S1 仍为整图面积。")
        self._refresh_summary()
        self._redraw()

    def start_defect(self) -> None:
        if self.orig is None or self.s1 <= 0:
            messagebox.showwarning("提示", "请先打开首层图片")
            return
        self.mode = "defect"
        self.draft_pts = []
        n = len(self.defects) + 2
        self.status.set("追加不良 S%d：左键依次点多边形顶点，至少 3 点后点「闭合当前不良」" % n)
        self._redraw()

    def close_defect(self) -> None:
        if self.mode != "defect":
            return
        if len(self.draft_pts) < 3:
            messagebox.showwarning("提示", "多边形至少需要 3 个点")
            return
        self.defects.append(list(self.draft_pts))
        self.draft_pts = []
        self.mode = "idle"
        area = polygon_area(self.defects[-1])
        idx = len(self.defects) + 1
        self.status.set("已追加 S%d = %.1f 像素²。可继续点「追加不良面积」。" % (idx, area))
        self._refresh_summary()
        self._redraw()

    def undo_point(self) -> None:
        if self.mode == "calib" and self.calib_pts:
            self.calib_pts.pop()
            self.status.set("全首层标定：已选 %d/4 点" % len(self.calib_pts))
        elif self.mode == "defect" and self.draft_pts:
            self.draft_pts.pop()
            self.status.set("当前不良已选 %d 点" % len(self.draft_pts))
        self._redraw()

    def undo_defect(self) -> None:
        if not self.defects:
            return
        self.defects.pop()
        self.draft_pts = []
        self.mode = "idle"
        self.status.set("已撤销上一块不良。剩余 %d 块。" % len(self.defects))
        self._refresh_summary()
        self._redraw()

    def clear_defects(self) -> None:
        self.defects = []
        self.draft_pts = []
        if self.mode == "defect":
            self.mode = "idle"
        self.status.set("已清空全部不良面积")
        self._refresh_summary()
        self._redraw()

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
        pt = (float(event.xdata), float(event.ydata))
        if self.mode == "calib":
            if len(self.calib_pts) >= 4:
                return
            self.calib_pts.append(pt)
            n = len(self.calib_pts)
            self.status.set("全首层标定：已选 %d/4 点（%s）" % (n, POINT_LABELS[n - 1]))
            self._redraw()
            if n == 4:
                self._finish_calib()
        elif self.mode == "defect":
            self.draft_pts.append(pt)
            self.status.set("当前不良 S%d：已选 %d 点（≥3 点后闭合）" % (len(self.defects) + 2, len(self.draft_pts)))
            self._redraw()

    def _finish_calib(self) -> None:
        assert self.orig is not None
        try:
            self.warped = warp_image(self.orig, self.calib_pts)
        except Exception as exc:
            messagebox.showerror("校正失败", str(exc))
            self.calib_pts = []
            self._redraw()
            return
        h, w = self.warped.shape[:2]
        self.s1 = float(w * h)
        self.mode = "idle"
        self.defects = []
        self.draft_pts = []
        self.status.set(
            "标定完成：矩形 %d×%d，S1=%.1f 像素²。可点「追加不良面积」。" % (w, h, self.s1)
        )
        self._refresh_summary()
        self._redraw()

    def _current_image(self) -> Optional[np.ndarray]:
        if self.warped is not None:
            return self.warped
        return self.orig

    def _refresh_summary(self) -> None:
        if self.s1 <= 0:
            self.summary.set("S1 未标定")
            return
        parts = ["S1=%.1f" % self.s1]
        total_bad = 0.0
        for i, poly in enumerate(self.defects, start=2):
            area = polygon_area(poly)
            total_bad += area
            parts.append("S%d=%.1f" % (i, area))
        ratio = 100.0 * total_bad / self.s1
        parts.append("不良合计=%.1f" % total_bad)
        parts.append("占比=%.3f%%" % ratio)
        self.summary.set("  |  ".join(parts) + "   （单位: 像素²）")

    def _redraw(self) -> None:
        self.ax.clear()
        self.ax.set_axis_off()
        image = self._current_image()
        if image is None:
            self.ax.set_title("未加载图片")
            self.canvas.draw_idle()
            return

        self.ax.imshow(bgr_to_rgb(image))
        title = os.path.basename(self.image_path.get()) or "未命名"
        if self.warped is not None:
            h, w = self.warped.shape[:2]
            title += "  |  校正 %dx%d" % (w, h)
        self.ax.set_title(title)

        if self.warped is None and self.calib_pts:
            xs = [p[0] for p in self.calib_pts]
            ys = [p[1] for p in self.calib_pts]
            self.ax.plot(xs, ys, "o-", color="#00c800", markersize=8, linewidth=2)
            if len(self.calib_pts) == 4:
                self.ax.plot(
                    [xs[-1], xs[0]],
                    [ys[-1], ys[0]],
                    "-",
                    color="#00c800",
                    linewidth=2,
                )
            for i, (x, y) in enumerate(self.calib_pts):
                self.ax.text(x + 8, y - 8, "%d:%s" % (i + 1, POINT_LABELS[i]), color="#00c800", fontsize=10)

        for i, poly in enumerate(self.defects):
            color = DEFECT_COLORS[i % len(DEFECT_COLORS)]
            patch = MplPolygon(poly, closed=True, facecolor=color, alpha=0.28, edgecolor=color, linewidth=2)
            self.ax.add_patch(patch)
            area = polygon_area(poly)
            cx = sum(p[0] for p in poly) / len(poly)
            cy = sum(p[1] for p in poly) / len(poly)
            self.ax.text(cx, cy, "S%d\n%.0f" % (i + 2, area), color=color, ha="center", va="center", fontsize=9)

        if self.draft_pts:
            xs = [p[0] for p in self.draft_pts]
            ys = [p[1] for p in self.draft_pts]
            self.ax.plot(xs, ys, "o--", color="#ffdd00", markersize=7, linewidth=1.5)
            for i, (x, y) in enumerate(self.draft_pts):
                self.ax.text(x + 6, y - 6, str(i + 1), color="#ffdd00", fontsize=9)

        self.canvas.draw_idle()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="首层不良面积计算")
    parser.add_argument("image", nargs="?", help="首层图片路径")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = tk.Tk()
    try:
        style = ttk.Style()
        if sys.platform.startswith("win"):
            style.theme_use("vista")
    except Exception:
        pass
    FirstLayerDefectApp(root, args.image)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
