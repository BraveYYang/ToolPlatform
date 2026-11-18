# -*- coding: utf-8 -*-
"""ToolPlatform 批量图片圆心识别插件。

算法内嵌自“喷嘴圆心检测工具”V1.1；本模块可由平台安全发现，导入时不会
创建 QApplication、修改全局样式/字体，或访问原工具目录。
"""
from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QImage, QKeySequence, QPainter, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGraphicsItem, QGraphicsPixmapItem,
    QGraphicsRectItem, QGraphicsScene, QGraphicsView, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

PLUGIN_META = {
    "id": "nozzle_batch",
    "name": "批量图片圆心识别",
    "version": "1.2",
    "category": "视觉工具",
    "description": "目录图片批量圆心检测、参数调试、偏移计算及 CSV/叠加处理图导出。",
    "order": 10,
}
TOOL_NAME = "批量图片圆心识别"
VERSION = "1.2"
ALGORITHM_VERSION = "1.1"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
_ORPHANED_WORKERS: set[QThread] = set()


def default_params() -> dict:
    return {
        "roi_x": 0, "roi_y": 0, "roi_w": 0, "roi_h": 0,
        "blur_ksize": 5, "blur_sigma": 1.0,
        "binarize_method": "fixed", "threshold": 180, "invert": False,
        "adaptive_block": 51, "adaptive_c": 5,
        "morph_open": 3, "morph_close": 15, "target_mode": "hole",
        "edge_mode": "contour", "canny_low": 40, "canny_high": 120,
        "subpixel_search": 0.45, "subpixel_angles": 360, "min_gradient": 8.0,
        "fit_method": "least_squares", "min_radius": 40.0,
        "max_radius": 300.0, "min_circularity": 0.50,
        "min_area": 500.0, "max_area": 0.0, "nozzle_diameter_mm": 0.4,
    }


def prepare_params(params: dict | None) -> dict:
    merged = default_params()
    if params:
        for key in merged:
            if key in params and params[key] is not None:
                merged[key] = params[key]

    def as_int(key: str, lo: int, hi: int) -> None:
        try:
            merged[key] = int(round(float(merged[key])))
        except (TypeError, ValueError):
            merged[key] = default_params()[key]
        merged[key] = max(lo, min(hi, merged[key]))

    def as_float(key: str, lo: float, hi: float) -> None:
        try:
            merged[key] = float(merged[key])
        except (TypeError, ValueError):
            merged[key] = default_params()[key]
        if not np.isfinite(merged[key]):
            merged[key] = default_params()[key]
        merged[key] = max(lo, min(hi, float(merged[key])))

    for key, lo, hi in (
        ("roi_x", 0, 100000), ("roi_y", 0, 100000),
        ("roi_w", 0, 100000), ("roi_h", 0, 100000),
        ("blur_ksize", 1, 99), ("threshold", 0, 255),
        ("adaptive_block", 3, 999), ("adaptive_c", -100, 100),
        ("morph_open", 0, 999), ("morph_close", 0, 999),
        ("canny_low", 0, 500), ("canny_high", 0, 500),
        ("subpixel_angles", 16, 1440),
    ):
        as_int(key, lo, hi)
    for key, lo, hi in (
        ("blur_sigma", 0.0, 50.0), ("subpixel_search", 0.05, 0.9),
        ("min_gradient", 0.0, 255.0), ("min_radius", 0.5, 100000.0),
        ("max_radius", 1.0, 100000.0), ("min_circularity", 0.0, 1.0),
        ("min_area", 0.0, 1e12), ("max_area", 0.0, 1e12),
        ("nozzle_diameter_mm", 0.0001, 10000.0),
    ):
        as_float(key, lo, hi)
    merged["invert"] = bool(merged["invert"])
    if merged["binarize_method"] not in {"fixed", "otsu", "adaptive"}:
        merged["binarize_method"] = "fixed"
    if merged["target_mode"] not in {"hole", "blob"}:
        merged["target_mode"] = "hole"
    if merged["edge_mode"] not in {"contour", "canny", "subpixel"}:
        merged["edge_mode"] = "contour"
    if merged["fit_method"] not in {"least_squares", "min_enclosing"}:
        merged["fit_method"] = "least_squares"
    if merged["max_radius"] < merged["min_radius"]:
        merged["min_radius"], merged["max_radius"] = merged["max_radius"], merged["min_radius"]
    if merged["canny_high"] < merged["canny_low"]:
        merged["canny_low"], merged["canny_high"] = merged["canny_high"], merged["canny_low"]
    return merged


@dataclass
class DetectResult:
    ok: bool
    message: str
    center_x: float = 0.0
    center_y: float = 0.0
    radius: float = 0.0
    circularity: float = 0.0
    area: float = 0.0
    rms: float = 0.0
    edge_count: int = 0
    used_threshold: float = -1.0
    edge_points: np.ndarray | None = None
    rejected_points: np.ndarray | None = None
    binary: np.ndarray | None = None
    gray: np.ndarray | None = None
    edge_note: str = ""

    def as_dict(self) -> dict:
        threshold_text = "自适应" if self.used_threshold < 0 else f"{self.used_threshold:.0f}"
        summary = (
            f"圆度 {self.circularity:.3f}，RMS {self.rms:.2f}px，阈值 {threshold_text}"
            if self.ok else self.message
        )
        return {
            "ok": self.ok, "message": self.message, "summary": summary,
            "cx": float(self.center_x), "cy": float(self.center_y),
            "r": float(self.radius), "rms": float(self.rms),
            "circularity": float(self.circularity), "area": float(self.area),
            "edge_count": int(self.edge_count), "used_threshold": float(self.used_threshold),
            "edge_note": self.edge_note,
            "edge_points": None if self.edge_points is None else np.ascontiguousarray(self.edge_points, dtype=np.float32),
            "rejected_points": None if self.rejected_points is None else np.ascontiguousarray(self.rejected_points, dtype=np.float32),
        }


def imread_unicode(path: str) -> np.ndarray | None:
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None


def imwrite_unicode(path: str, image: np.ndarray) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext not in IMAGE_EXTENSIONS:
        ext = ".png"
    options = [int(cv2.IMWRITE_JPEG_QUALITY), 95] if ext in {".jpg", ".jpeg"} else []
    ok, buffer = cv2.imencode(ext, image, options)
    if not ok:
        return False
    try:
        buffer.tofile(path)
    except OSError:
        return False
    return True


def draw_fit_overlay(image: np.ndarray, result: DetectResult,
                     roi: tuple[int, int, int, int] | None = None) -> np.ndarray:
    canvas = _to_bgr(image).copy()
    height, width = canvas.shape[:2]
    thickness = max(2, int(round(min(height, width) / 700)))
    point_radius = max(2, thickness)
    if roi is not None:
        x, y, rw, rh = (int(v) for v in roi)
        uses_roi = rw > 1 and rh > 1 and (x > 0 or y > 0 or x + rw < width or y + rh < height)
        if uses_roi:
            cv2.rectangle(canvas, (x, y), (x + rw - 1, y + rh - 1), (230, 183, 0), thickness)
    _draw_points(canvas, result.edge_points, (214, 43, 255), point_radius)
    _draw_points(canvas, result.rejected_points, (60, 60, 255), point_radius)
    if result.ok and result.radius > 0:
        center = (int(round(result.center_x)), int(round(result.center_y)))
        cv2.circle(canvas, center, max(1, int(round(result.radius))), (80, 220, 40), thickness, cv2.LINE_AA)
        cv2.drawMarker(canvas, center, (48, 48, 255), cv2.MARKER_CROSS,
                       max(16, thickness * 10), thickness, cv2.LINE_AA)
    return canvas


def _draw_points(image: np.ndarray, points: np.ndarray | None,
                 color: tuple[int, int, int], radius: int) -> None:
    if points is None:
        return
    for x, y in np.asarray(points):
        cv2.circle(image, (int(round(float(x))), int(round(float(y)))),
                   radius, color, -1, lineType=cv2.LINE_AA)


def list_images(folder: str) -> list[str]:
    try:
        entries = os.listdir(folder)
    except OSError:
        return []
    names = [name for name in entries
             if os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS
             and os.path.isfile(os.path.join(folder, name))]
    names.sort(key=_natural_key)
    return [os.path.join(folder, name) for name in names]


def compute_offsets(rows: list[dict], diameter_mm: float) -> tuple[list[dict], dict]:
    ref = rows[0] if rows else None
    ref_ok = bool(ref and ref.get("ok") and float(ref.get("r", 0)) > 0)
    ref_path = ref.get("path", "") if ref_ok else ""
    mm_per_px = float(diameter_mm) / (2.0 * float(ref["r"])) if ref_ok and diameter_mm > 0 else None
    offsets = []
    for index, row in enumerate(rows):
        item = {key: None for key in (
            "dx_px", "dy_px", "dx_mm", "dy_mm", "dist_px", "dist_mm", "radius_delta_ratio"
        )}
        if ref_ok and row.get("ok"):
            dx = float(row["cx"]) - float(ref["cx"])
            dy = float(row["cy"]) - float(ref["cy"])
            item.update(dx_px=dx, dy_px=dy, dist_px=float(np.hypot(dx, dy)))
            item["radius_delta_ratio"] = 0.0 if index == 0 else (
                float(row["r"]) - float(ref["r"])) / float(ref["r"])
            if mm_per_px is not None:
                item.update(dx_mm=dx * mm_per_px, dy_mm=dy * mm_per_px,
                            dist_mm=item["dist_px"] * mm_per_px)
        offsets.append(item)
    return offsets, {
        "ref_ok": ref_ok, "ref_name": os.path.basename(ref_path) if ref_path else "",
        "ref_cx": float(ref["cx"]) if ref_ok else None,
        "ref_cy": float(ref["cy"]) if ref_ok else None,
        "ref_r": float(ref["r"]) if ref_ok else None,
        "mm_per_px": mm_per_px, "diameter_mm": float(diameter_mm),
    }


def detect_nozzle(image: np.ndarray, params: dict | None = None,
                  debug: bool = False) -> DetectResult:
    settings = prepare_params(params)
    if image is None or not isinstance(image, np.ndarray) or image.size == 0:
        return DetectResult(False, "图片为空，无法检测。")
    color = _to_bgr(image)
    gray_full = cv2.cvtColor(color, cv2.COLOR_BGR2GRAY)
    roi = _normalize_roi(settings["roi_x"], settings["roi_y"], settings["roi_w"],
                         settings["roi_h"], gray_full.shape[1], gray_full.shape[0])
    if roi is None:
        result = DetectResult(False, "检测范围无效或过小，请重新框选，或使用全图。")
        if debug:
            result.gray = gray_full
        return result
    x0, y0, rw, rh = roi
    gray = gray_full[y0:y0 + rh, x0:x0 + rw]
    blurred = _blur(gray, settings["blur_ksize"], settings["blur_sigma"])
    binary, used_threshold, threshold_error = _binarize(blurred, settings)
    if threshold_error:
        result = DetectResult(False, threshold_error)
        if debug:
            result.gray = gray_full
        return result
    binary = _morphology(binary, settings["morph_open"], settings["morph_close"])
    result = _fit_from_binary(blurred, binary, settings)
    result.used_threshold = used_threshold
    shift = np.array([x0, y0], dtype=np.float64)
    if result.edge_points is not None and len(result.edge_points):
        result.edge_points = result.edge_points + shift
    if result.rejected_points is not None and len(result.rejected_points):
        result.rejected_points = result.rejected_points + shift
    if result.ok:
        result.center_x += x0
        result.center_y += y0
    if debug:
        canvas = np.zeros(gray_full.shape, dtype=np.uint8)
        canvas[y0:y0 + rh, x0:x0 + rw] = binary
        result.binary = canvas
        result.gray = gray_full
    return result


def _to_bgr(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    return image


def _normalize_roi(x: int, y: int, w: int, h: int, img_w: int, img_h: int):
    if w <= 1 or h <= 1:
        return 0, 0, img_w, img_h
    if x < 0:
        w += x; x = 0
    if y < 0:
        h += y; y = 0
    if x >= img_w or y >= img_h:
        return None
    w, h = min(w, img_w - x), min(h, img_h - y)
    return None if w < 10 or h < 10 else (int(x), int(y), int(w), int(h))


def _odd_ksize(value: int) -> int:
    k = int(value)
    return 1 if k <= 1 else (k + 1 if k % 2 == 0 else k)


def _morph_ksize(value: int) -> int:
    k = int(value)
    return 0 if k <= 1 else (k + 1 if k % 2 == 0 else k)


def _blur(gray: np.ndarray, ksize: int, sigma: float) -> np.ndarray:
    k = _odd_ksize(ksize)
    return gray if k <= 1 else cv2.GaussianBlur(gray, (k, k), float(sigma))


def _binarize(gray: np.ndarray, settings: dict) -> tuple[np.ndarray, float, str]:
    mode = cv2.THRESH_BINARY_INV if settings["invert"] else cv2.THRESH_BINARY
    method = settings["binarize_method"]
    if method == "otsu":
        used, binary = cv2.threshold(gray, 0, 255, mode | cv2.THRESH_OTSU)
        return binary, float(used), ""
    if method == "adaptive":
        block = max(3, _odd_ksize(settings["adaptive_block"]))
        limit = min(gray.shape[:2])
        if limit < 3:
            return gray, -1.0, "检测范围过小，无法做自适应阈值。"
        if block >= limit:
            block = max(3, limit - 1 if (limit - 1) % 2 else limit - 2)
        binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       mode, block, int(settings["adaptive_c"]))
        return binary, -1.0, ""
    used, binary = cv2.threshold(gray, int(settings["threshold"]), 255, mode)
    return binary, float(used), ""


def _morphology(binary: np.ndarray, open_k: int, close_k: int) -> np.ndarray:
    opened = _odd_apply(binary, _morph_ksize(open_k), cv2.MORPH_OPEN)
    return _odd_apply(opened, _morph_ksize(close_k), cv2.MORPH_CLOSE)


def _odd_apply(binary: np.ndarray, ksize: int, operation: int) -> np.ndarray:
    if ksize < 3:
        return binary
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksize, ksize))
    return cv2.morphologyEx(binary, operation, kernel)


def _fit_from_binary(blurred: np.ndarray, binary: np.ndarray, settings: dict) -> DetectResult:
    if int(binary.max()) == 0:
        return DetectResult(False, "二值图全黑，没有前景。可降低阈值；若暗孔需要变成白色斑块，请勾选反相。")
    if int(binary.min()) == 255 and settings["target_mode"] == "hole":
        return DetectResult(False, "二值图全白，找不到被亮区包围的孔。请提高阈值。")
    contours, hierarchy = cv2.findContours(binary.copy(), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if hierarchy is None or not contours:
        return DetectResult(False, "二值图中没有轮廓。请调整阈值或检测范围。")
    hierarchy = hierarchy[0]
    height, width = binary.shape[:2]
    candidates = []
    hole_count = 0
    for index, contour in enumerate(contours):
        is_hole = int(hierarchy[index][3]) >= 0
        hole_count += int(is_hole)
        if settings["target_mode"] == "hole" and not is_hole:
            continue
        if settings["target_mode"] == "blob" and is_hole:
            continue
        area = float(cv2.contourArea(contour))
        if area < settings["min_area"] or (settings["max_area"] > 0 and area > settings["max_area"]):
            continue
        perimeter = float(cv2.arcLength(contour, True))
        if perimeter <= 1e-3:
            continue
        circularity = float(4.0 * np.pi * area / (perimeter * perimeter))
        if circularity < settings["min_circularity"] or circularity > 1.2:
            continue
        bx, by, bw, bh = cv2.boundingRect(contour)
        if bx <= 1 or by <= 1 or bx + bw >= width - 1 or by + bh >= height - 1:
            continue
        (cx, cy), radius = cv2.minEnclosingCircle(contour)
        radius = float(radius)
        if radius < settings["min_radius"] or radius > settings["max_radius"]:
            continue
        score = circularity - 0.2 * float(np.hypot(cx - width / 2, cy - height / 2) / max(width, height))
        candidates.append((score, contour, circularity, area))
    if not candidates:
        if settings["target_mode"] == "hole" and hole_count == 0:
            return DetectResult(False, "没有检测到被前景包围的孔。请保持不反相并让检测范围包住完整亮环。")
        return DetectResult(False, "未找到满足半径、面积和圆度的目标。可放宽筛选条件。")
    candidates.sort(key=lambda item: item[0], reverse=True)
    for _score, contour, circularity, area in candidates:
        contour_points = contour.reshape(-1, 2).astype(np.float64)
        edge_points, edge_note = _extract_edges(blurred, contour, contour_points, settings)
        if edge_points is None or len(edge_points) < 15:
            continue
        if len(edge_points) > 20000:
            edge_points = edge_points[::int(np.ceil(len(edge_points) / 20000))]
        fitted = _fit_circle(edge_points, settings["fit_method"])
        if fitted is None:
            continue
        cx, cy, radius, rms, inliers, rejected = fitted
        if radius < settings["min_radius"] or radius > settings["max_radius"]:
            continue
        message = f"检测成功。边沿点 {len(inliers)}，RMS {rms:.3f} px。"
        return DetectResult(True, edge_note + message, cx, cy, radius, circularity,
                            area, rms, len(inliers), edge_points=inliers,
                            rejected_points=rejected, edge_note=edge_note)
    return DetectResult(False, "边沿点无法拟合出符合半径范围的圆。请调整边沿方式或半径范围。")


def _extract_edges(blurred, contour, contour_points, settings):
    if settings["edge_mode"] == "canny":
        points = _canny_points(blurred, contour, settings["canny_low"], settings["canny_high"])
        return (points, "") if points is not None else (contour_points, "Canny 边沿点不足，已改用二值轮廓。")
    if settings["edge_mode"] == "subpixel":
        points = _refine_radial(blurred, contour_points, settings["subpixel_angles"],
                                settings["subpixel_search"], settings["min_gradient"])
        return (points, "") if points is not None else (contour_points, "径向梯度边沿点不足，已改用二值轮廓。")
    return contour_points, ""


def _canny_points(blurred, contour, low, high):
    edges = cv2.Canny(blurred, int(low), int(high))
    band = np.zeros(blurred.shape, dtype=np.uint8)
    cv2.drawContours(band, [contour], -1, 255, thickness=5)
    ys, xs = np.where((edges > 0) & (band > 0))
    return None if len(xs) < 30 else np.column_stack((xs.astype(np.float64), ys.astype(np.float64)))


def _refine_radial(gray, contour_points, angles, search_ratio, min_gradient):
    initial = _kasa(contour_points)
    if initial is None:
        (cx, cy), radius = cv2.minEnclosingCircle(contour_points.reshape(-1, 1, 2).astype(np.float32))
        cx, cy, radius = float(cx), float(cy), float(radius)
    else:
        cx, cy, radius = initial
    points = None
    for _ in range(2):
        points = _radial_edge_points(gray, cx, cy, radius, angles, search_ratio, min_gradient)
        if points is None:
            return None
        refined = _kasa(points)
        if refined is None:
            return points
        cx, cy, radius = refined
    return points


def _radial_edge_points(gray, cx, cy, radius, angles, search_ratio, min_gradient):
    if radius < 2:
        return None
    ratio = float(np.clip(search_ratio, 0.05, 0.9))
    t0, t1 = radius * (1.0 - ratio), radius * (1.0 + ratio)
    sample_count = int(max(12, min(400, round(t1 - t0))))
    if sample_count < 8 or t1 <= t0:
        return None
    angle_count = int(max(16, angles))
    height, width = gray.shape[:2]
    gray64 = gray.astype(np.float64)
    points = []
    for index in range(angle_count):
        angle = 2.0 * np.pi * index / angle_count
        dx, dy = float(np.cos(angle)), float(np.sin(angle))
        ts = np.linspace(t0, t1, sample_count)
        xs, ys = cx + ts * dx, cy + ts * dy
        valid = (xs >= 0) & (ys >= 0) & (xs <= width - 1.001) & (ys <= height - 1.001)
        if not bool(valid[0]) or int(valid.sum()) < 8:
            continue
        if not bool(valid.all()):
            last = int(np.argmax(~valid))
            if last < 8:
                continue
            xs, ys, ts = xs[:last], ys[:last], ts[:last]
        values = _bilinear(gray64, xs, ys)
        gradient = np.diff(values)
        peak = int(np.argmax(gradient))
        if float(gradient[peak]) < min_gradient:
            continue
        delta = _parabola_delta(gradient, peak)
        distance = float(ts[peak] + (0.5 + delta) * float(ts[1] - ts[0]))
        points.append((cx + distance * dx, cy + distance * dy))
    if len(points) < max(20, angle_count // 8):
        return None
    return np.asarray(points, dtype=np.float64)


def _bilinear(gray, xs, ys):
    x0, y0 = np.floor(xs).astype(np.int32), np.floor(ys).astype(np.int32)
    x1, y1 = x0 + 1, y0 + 1
    wx, wy = xs - x0, ys - y0
    height, width = gray.shape[:2]
    x0, x1 = np.clip(x0, 0, width - 1), np.clip(x1, 0, width - 1)
    y0, y1 = np.clip(y0, 0, height - 1), np.clip(y1, 0, height - 1)
    return (gray[y0, x0] * (1 - wx) * (1 - wy) + gray[y0, x1] * wx * (1 - wy)
            + gray[y1, x0] * (1 - wx) * wy + gray[y1, x1] * wx * wy)


def _parabola_delta(gradient, index):
    if index <= 0 or index >= len(gradient) - 1:
        return 0.0
    y0, y1, y2 = map(float, gradient[index - 1:index + 2])
    denom = y0 - 2.0 * y1 + y2
    return 0.0 if abs(denom) < 1e-9 else float(np.clip(0.5 * (y0 - y2) / denom, -1, 1))


def _fit_circle(points, method):
    points = np.asarray(points, dtype=np.float64)
    if len(points) < 10:
        return None
    if method == "min_enclosing":
        (cx, cy), radius = cv2.minEnclosingCircle(points.reshape(-1, 1, 2).astype(np.float32))
        cx, cy, radius = float(cx), float(cy), float(radius)
        if radius <= 0:
            return None
        dist = np.hypot(points[:, 0] - cx, points[:, 1] - cy)
        return cx, cy, radius, float(np.sqrt(np.mean((dist - radius) ** 2))), points, np.empty((0, 2))
    initial = _kasa(points)
    if initial is None:
        return None
    inliers, rejected = _select_inliers(points, *initial)
    refined = _kasa(inliers)
    if refined is None:
        cx, cy, radius = initial
        used, rejected = points, np.empty((0, 2), dtype=np.float64)
    else:
        cx, cy, radius = refined
        used = inliers
    dist = np.hypot(used[:, 0] - cx, used[:, 1] - cy)
    return cx, cy, radius, float(np.sqrt(np.mean((dist - radius) ** 2))), used, rejected


def _kasa(points):
    if len(points) < 3:
        return None
    x, y = points[:, 0], points[:, 1]
    design = np.column_stack((x, y, np.ones(len(points))))
    try:
        solution, _residuals, rank, _singular = np.linalg.lstsq(design, -(x * x + y * y), rcond=None)
    except np.linalg.LinAlgError:
        return None
    if rank < 3:
        return None
    d, e, f = map(float, solution)
    cx, cy = -d / 2, -e / 2
    radius_sq = cx * cx + cy * cy - f
    return None if not np.isfinite(radius_sq) or radius_sq <= 1e-6 else (cx, cy, float(np.sqrt(radius_sq)))


def _select_inliers(points, cx, cy, radius):
    residual = np.abs(np.hypot(points[:, 0] - cx, points[:, 1] - cy) - radius)
    median = float(np.median(residual))
    mad = float(np.median(np.abs(residual - median)))
    threshold = min(max(1.5, median + 3.0 * 1.4826 * mad), max(2.0, 0.08 * radius))
    mask = residual <= threshold
    if int(mask.sum()) < max(15, int(0.45 * len(points))):
        return points, np.empty((0, 2), dtype=np.float64)
    return points[mask], points[~mask]


def _natural_key(text: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def fail_payload(path: str, message: str) -> dict:
    return {"ok": False, "message": message, "summary": message, "cx": 0.0,
            "cy": 0.0, "r": 0.0, "rms": 0.0, "circularity": 0.0,
            "area": 0.0, "edge_count": 0, "used_threshold": -1.0,
            "edge_note": "", "edge_points": None, "rejected_points": None, "path": path}


def fmt_px(value, signed=False):
    return "" if value is None else (f"{value:+.2f}" if signed else f"{value:.2f}")


def fmt_mm(value, signed=False):
    return "" if value is None else (f"{value:+.4f}" if signed else f"{value:.4f}")


def _write_results_csv(path: str, rows: list[dict], diameter_mm: float) -> None:
    offsets, info = compute_offsets(rows, diameter_mm)
    with open(path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["工具", TOOL_NAME, "版本", VERSION, "算法版本", ALGORITHM_VERSION])
        writer.writerow(["喷嘴直径(mm)", f"{diameter_mm:.4f}"])
        writer.writerow(["基准文件", info["ref_name"]])
        writer.writerow(["基准圆心X(px)", "" if info["ref_cx"] is None else f"{info['ref_cx']:.4f}"])
        writer.writerow(["基准圆心Y(px)", "" if info["ref_cy"] is None else f"{info['ref_cy']:.4f}"])
        writer.writerow(["基准半径(px)", "" if info["ref_r"] is None else f"{info['ref_r']:.4f}"])
        writer.writerow(["比例尺(mm/px)", "" if info["mm_per_px"] is None else f"{info['mm_per_px']:.8f}"])
        writer.writerow(["坐标说明", "X向右为正，Y向下为正，偏移=当前圆心-第一张圆心"])
        writer.writerow([])
        writer.writerow(["序号", "文件名", "状态", "圆心X(px)", "圆心Y(px)", "半径(px)",
                         "ΔX(px)", "ΔY(px)", "ΔX(mm)", "ΔY(mm)", "距离(mm)", "说明"])
        for index, (row, offset) in enumerate(zip(rows, offsets), 1):
            writer.writerow([index, os.path.basename(row.get("path", "")),
                "成功" if row.get("ok") else row.get("message", "失败"),
                fmt_px(row.get("cx")) if row.get("ok") else "",
                fmt_px(row.get("cy")) if row.get("ok") else "",
                fmt_px(row.get("r")) if row.get("ok") else "",
                fmt_px(offset["dx_px"], True), fmt_px(offset["dy_px"], True),
                fmt_mm(offset["dx_mm"], True), fmt_mm(offset["dy_mm"], True),
                fmt_mm(offset["dist_mm"]), row.get("summary", "")])


def numpy_to_qimage(image: np.ndarray) -> QImage:
    arr = np.ascontiguousarray(cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
                               if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    height, width = arr.shape[:2]
    return QImage(arr.data, width, height, 3 * width, QImage.Format.Format_RGB888).copy()


def points_to_polygon(points, limit=4000):
    if points is None or np.asarray(points).size == 0:
        return None
    array = np.asarray(points)
    if len(array) > limit:
        array = array[::int(np.ceil(len(array) / limit))]
    return QPolygonF([QPointF(float(x), float(y)) for x, y in array])


class OverlayItem(QGraphicsItem):
    def __init__(self):
        super().__init__()
        self.image_rect = QRectF(0, 0, 1, 1)
        self.edge_polygon = self.rejected_polygon = self.circle = None
        self.show_edges = self.show_circle = False
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def boundingRect(self):
        return self.image_rect

    def set_image_rect(self, width, height):
        self.prepareGeometryChange(); self.image_rect = QRectF(0, 0, max(1, width), max(1, height))

    def set_data(self, edges, rejected, circle, show_edges, show_circle):
        self.edge_polygon = points_to_polygon(edges) if show_edges else None
        self.rejected_polygon = points_to_polygon(rejected) if show_edges else None
        self.circle, self.show_edges, self.show_circle = circle, show_edges, show_circle
        self.update()

    def paint(self, painter, option, widget=None):
        del option, widget
        for polygon, color in ((self.edge_polygon, "#ff2bd6"), (self.rejected_polygon, "#ff4d4f")):
            if self.show_edges and polygon is not None and not polygon.isEmpty():
                pen = QPen(QColor(color)); pen.setCosmetic(True); pen.setWidth(4)
                painter.setPen(pen); painter.drawPoints(polygon)
        if not self.show_circle or not self.circle or self.circle[2] <= 0:
            return
        cx, cy, radius = self.circle
        pen = QPen(QColor("#2ee56b")); pen.setCosmetic(True); pen.setWidth(2)
        painter.setPen(pen); painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(cx, cy), radius, radius)
        scale = abs(painter.transform().m11()) or 1.0
        arm = 12 / scale; pen.setColor(QColor("#ff3b30")); painter.setPen(pen)
        painter.drawLine(QPointF(cx - arm, cy), QPointF(cx + arm, cy))
        painter.drawLine(QPointF(cx, cy - arm), QPointF(cx, cy + arm))


class ImageView(QGraphicsView):
    roiChanged = pyqtSignal(int, int, int, int)
    cursorMoved = pyqtSignal(int, int)
    zoomChanged = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.graphics_scene = QGraphicsScene(self); self.setScene(self.graphics_scene)
        self.pixmap_item = QGraphicsPixmapItem(); self.overlay = OverlayItem(); self.roi_item = QGraphicsRectItem()
        self.roi_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        pen = QPen(QColor("#00b7e6")); pen.setCosmetic(True); pen.setWidth(2); pen.setStyle(Qt.PenStyle.DashLine)
        self.roi_item.setPen(pen); self.roi_item.setBrush(QColor(0, 183, 230, 36)); self.roi_item.hide()
        self.graphics_scene.addItem(self.pixmap_item); self.graphics_scene.addItem(self.overlay); self.graphics_scene.addItem(self.roi_item)
        self.overlay.setZValue(1); self.roi_item.setZValue(2)
        self.setBackgroundBrush(QColor("#1e2430")); self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True); self.viewport().setMouseTracking(True)
        self.roi_mode = self._dragging = self._panning = False
        self._drag_start = QPointF(); self._pan_anchor = None; self._auto_fit = True

    def has_image(self): return not self.pixmap_item.pixmap().isNull()
    def clear_image(self):
        self.pixmap_item.setPixmap(QPixmap()); self.overlay.set_data(None, None, None, False, False); self.roi_item.hide()
    def set_base(self, image, reset_view=False):
        pixmap = QPixmap.fromImage(numpy_to_qimage(image)); self.pixmap_item.setPixmap(pixmap)
        self.graphics_scene.setSceneRect(0, 0, pixmap.width(), pixmap.height()); self.overlay.set_image_rect(pixmap.width(), pixmap.height())
        if reset_view: self._auto_fit = True; self.fit_image()
    def set_overlay(self, *args): self.overlay.set_data(*args)
    def set_roi_rect(self, x, y, w, h):
        self.roi_item.setVisible(w > 1 and h > 1)
        if w > 1 and h > 1: self.roi_item.setRect(x, y, w, h)
    def set_roi_mode(self, enabled):
        self.roi_mode = enabled; self.setDragMode(QGraphicsView.DragMode.NoDrag if enabled else QGraphicsView.DragMode.ScrollHandDrag)
        self.setCursor(Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.ArrowCursor)
    def fit_image(self):
        if self.has_image(): self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio); self.zoomChanged.emit(abs(self.transform().m11()))
    def actual_size(self): self._auto_fit = False; self.resetTransform(); self.zoomChanged.emit(1.0)
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._auto_fit and self.has_image(): self.fit_image()
    def wheelEvent(self, event):
        if not self.has_image(): return
        self._auto_fit = False; factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        if 0.02 <= abs(self.transform().m11()) * factor <= 40:
            self.scale(factor, factor); self.zoomChanged.emit(abs(self.transform().m11()))
    def mousePressEvent(self, event):
        if self.has_image() and (event.button() == Qt.MouseButton.MiddleButton or (self.roi_mode and event.button() == Qt.MouseButton.RightButton)):
            self._panning = True; self._pan_anchor = event.position().toPoint(); event.accept(); return
        if self.has_image() and self.roi_mode and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True; self._drag_start = self.mapToScene(event.position().toPoint()); self._update_drag(self._drag_start); event.accept(); return
        super().mousePressEvent(event)
    def mouseMoveEvent(self, event):
        pos = self.mapToScene(event.position().toPoint()); self.cursorMoved.emit(round(pos.x()), round(pos.y()))
        if self._panning:
            delta = event.position().toPoint() - self._pan_anchor; self._pan_anchor = event.position().toPoint()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y()); return
        if self._dragging: self._update_drag(pos); return
        super().mouseMoveEvent(event)
    def mouseReleaseEvent(self, event):
        if self._panning:
            self._panning = False; self._pan_anchor = None; event.accept(); return
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False; rect = self.roi_item.rect()
            if rect.width() >= 5 and rect.height() >= 5:
                self.roiChanged.emit(round(rect.x()), round(rect.y()), round(rect.width()), round(rect.height()))
            event.accept(); return
        super().mouseReleaseEvent(event)
    def _update_drag(self, pos):
        bounds = self.sceneRect()
        x1 = min(max(self._drag_start.x(), bounds.left()), bounds.right()); y1 = min(max(self._drag_start.y(), bounds.top()), bounds.bottom())
        x2 = min(max(pos.x(), bounds.left()), bounds.right()); y2 = min(max(pos.y(), bounds.top()), bounds.bottom())
        rect = QRectF(QPointF(x1, y1), QPointF(x2, y2)).normalized()
        if rect.width() >= 1 and rect.height() >= 1: self.roi_item.setRect(rect); self.roi_item.show()


class BatchWorker(QThread):
    itemReady = pyqtSignal(int, object)
    progress = pyqtSignal(int, int, str)
    batchFinished = pyqtSignal(bool)
    def __init__(self, paths: list[str], params: dict, parent=None):
        super().__init__(parent); self.paths = list(paths); self.params = prepare_params(params); self._stop = False
    def stop(self): self._stop = True; self.requestInterruption()
    def run(self):
        stopped = False
        for index, path in enumerate(self.paths):
            if self._stop or self.isInterruptionRequested(): stopped = True; break
            try:
                image = imread_unicode(path)
                payload = fail_payload(path, "无法读取图片。") if image is None else detect_nozzle(image, self.params).as_dict()
                payload["path"] = path
            except Exception as exc:
                payload = fail_payload(path, f"检测异常：{exc}")
            self.itemReady.emit(index, payload); self.progress.emit(index + 1, len(self.paths), os.path.basename(path))
        self.batchFinished.emit(stopped)


class ExportWorker(QThread):
    progress = pyqtSignal(int, int, str)
    exportFinished = pyqtSignal(object)
    def __init__(self, paths, params, folder, source_dir, parent=None):
        super().__init__(parent); self.paths = list(paths); self.params = prepare_params(params)
        self.folder, self.source_dir, self._stop = folder, source_dir, False
    def stop(self): self._stop = True; self.requestInterruption()
    def run(self):
        rows = []; ok_count = 0; stopped = False
        try:
            os.makedirs(self.folder, exist_ok=False)
            with open(os.path.join(self.folder, "参数配置.json"), "w", encoding="utf-8") as file:
                json.dump({"version": VERSION, "algorithm_version": ALGORITHM_VERSION,
                           "tool": TOOL_NAME, "source_directory": self.source_dir,
                           "params": self.params}, file, ensure_ascii=False, indent=2)
            for index, path in enumerate(self.paths):
                if self._stop or self.isInterruptionRequested(): stopped = True; break
                image = imread_unicode(path)
                if image is None:
                    row = fail_payload(path, "无法读取图片。")
                else:
                    result = detect_nozzle(image, self.params)
                    row = result.as_dict(); row["path"] = path
                    canvas = draw_fit_overlay(image, result, tuple(self.params[k] for k in ("roi_x", "roi_y", "roi_w", "roi_h"))) if result.ok else image
                    if not imwrite_unicode(os.path.join(self.folder, os.path.basename(path)), canvas):
                        row = fail_payload(path, "效果图保存失败。")
                    elif result.ok: ok_count += 1
                rows.append(row); self.progress.emit(index + 1, len(self.paths), os.path.basename(path))
            _write_results_csv(os.path.join(self.folder, "处理记录.csv"), rows, self.params["nozzle_diameter_mm"])
        except Exception as exc:
            self.exportFinished.emit({"ok": False, "stopped": stopped, "folder": self.folder,
                                      "message": str(exc), "ok_count": ok_count, "total": len(self.paths)}); return
        self.exportFinished.emit({"ok": True, "stopped": stopped, "folder": self.folder,
                                  "message": "", "ok_count": ok_count, "total": len(self.paths)})


class NozzleBatchPlugin(QWidget):
    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context; self._active = False; self._shutdown = False; self._loading = False
        self.paths = []; self.batch_paths = []; self.measurements = []; self.image_dir = ""; self.current_path = ""
        self.image_bgr = None; self.live_result = None; self.live_meas = None; self.batch_stale = False
        self.batch_worker: BatchWorker | None = None; self.export_worker: ExportWorker | None = None
        self._batch_detect = None; self._active_stage = None; self._busy_debug = False; self._pending_reset = False
        self._preview_timer = QTimer(self); self._preview_timer.setSingleShot(True); self._preview_timer.setInterval(120)
        self.setAcceptDrops(True)
        self.config_path = self._resolve_config_path()
        self._build_ui(); self._connect_signals(); self.apply_params(default_params()); self._load_startup(); self._set_busy(False)

    def _resolve_config_path(self) -> Path:
        method = getattr(self.context, "plugin_config_file", None)
        if callable(method):
            try: return Path(method("nozzle_batch", "nozzle_config.json"))
            except TypeError: return Path(method("nozzle_config.json"))
        directory = Path(getattr(self.context, "config_dir", Path.cwd() / "config")); directory.mkdir(parents=True, exist_ok=True)
        return directory / "nozzle_config.json"

    def _status(self, text, timeout=5000):
        method = getattr(self.context, "show_status", None) or getattr(self.context, "post_status", None)
        if callable(method): method(text, timeout)
        self.local_status.setText(text)

    def _button(self, text):
        button = QPushButton(text); button.setMinimumHeight(28); return button

    def _build_ui(self):
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(4)
        top = QHBoxLayout(); top.setContentsMargins(0, 0, 0, 0); top.setSpacing(4)
        self.btn_open = self._button("选择图片目录"); self.dir_edit = QLineEdit(); self.dir_edit.setReadOnly(True)
        self.diameter_spin = QDoubleSpinBox(); self.diameter_spin.setRange(.001, 1000); self.diameter_spin.setDecimals(3); self.diameter_spin.setValue(.4)
        self.btn_import = self._button("导入参数"); self.btn_save = self._button("保存参数")
        top.addWidget(self.btn_open); top.addWidget(self.dir_edit, 1); top.addWidget(QLabel("喷嘴直径")); top.addWidget(self.diameter_spin); top.addWidget(QLabel("mm")); top.addWidget(self.btn_import); top.addWidget(self.btn_save)
        root.addLayout(top)
        self.horizontal_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.horizontal_splitter.setObjectName("nozzleBatchHorizontalSplitter")
        self.horizontal_splitter.setChildrenCollapsible(False)
        self.horizontal_splitter.addWidget(self._build_list_panel()); self.horizontal_splitter.addWidget(self._build_view_panel()); self.horizontal_splitter.addWidget(self._build_param_panel())
        self.horizontal_splitter.setStretchFactor(0, 0); self.horizontal_splitter.setStretchFactor(1, 1); self.horizontal_splitter.setStretchFactor(2, 0); self.horizontal_splitter.setSizes([210, 900, 290])
        upper = QWidget(); upper_layout = QVBoxLayout(upper); upper_layout.setContentsMargins(0, 0, 0, 0); upper_layout.setSpacing(0); upper_layout.addWidget(self.horizontal_splitter)
        results = self._build_results_panel()
        self.vertical_splitter = QSplitter(Qt.Orientation.Vertical); self.vertical_splitter.setObjectName("nozzleBatchVerticalSplitter"); self.vertical_splitter.setChildrenCollapsible(False)
        self.vertical_splitter.addWidget(upper); self.vertical_splitter.addWidget(results); self.vertical_splitter.setStretchFactor(0, 1); self.vertical_splitter.setStretchFactor(1, 0); self.vertical_splitter.setSizes([650, 230]); root.addWidget(self.vertical_splitter, 1)
        self.local_status = QLabel("就绪"); self.local_status.setObjectName("pluginDescription"); root.addWidget(self.local_status)
        for shortcut, slot in (("Ctrl+O", self.choose_directory), ("F5", self.debug_current), ("F6", self.start_batch)):
            action = QAction(self); action.setShortcut(QKeySequence(shortcut)); action.triggered.connect(slot); self.addAction(action)

    def _build_list_panel(self):
        box = QGroupBox("图片列表（0）"); layout = QVBoxLayout(box)
        hint = QLabel("自然排序；第一张加粗并作为偏移基准。"); hint.setWordWrap(True)
        self.file_list = QListWidget(); self.file_list.setAlternatingRowColors(True)
        layout.addWidget(hint); layout.addWidget(self.file_list); box.setMinimumWidth(190); return box

    def _build_view_panel(self):
        panel = QWidget(); layout = QVBoxLayout(panel); layout.setContentsMargins(0, 0, 0, 0)
        tools = QHBoxLayout(); self.view_mode = QComboBox()
        for text, value in (("原图", "raw"), ("灰度", "gray"), ("二值图", "binary"), ("边沿点", "edge"), ("叠加结果", "overlay")): self.view_mode.addItem(text, value)
        self.btn_fit = self._button("适应"); self.btn_actual = self._button("1:1"); self.btn_debug = self._button("调试当前图片")
        tools.addWidget(QLabel("显示")); tools.addWidget(self.view_mode); tools.addWidget(self.btn_fit); tools.addWidget(self.btn_actual); tools.addStretch(); tools.addWidget(self.btn_debug)
        self.view = ImageView(); self.view.setMinimumSize(300, 240)
        self.result_label = QLabel("选择图片后可调试当前图片；品红为拟合边沿，红色为剔除点，绿色为拟合圆。"); self.result_label.setWordWrap(True)
        layout.addLayout(tools); layout.addWidget(self.view, 1); layout.addWidget(self.result_label); return panel

    def _build_param_panel(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setMinimumWidth(270); scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        host = QWidget(); layout = QVBoxLayout(host); layout.setContentsMargins(2, 0, 2, 0); layout.setSpacing(5)
        roi = QGroupBox("ROI 检测范围"); grid = QGridLayout(roi)
        self.roi_x = self._int(0, 100000); self.roi_y = self._int(0, 100000); self.roi_w = self._int(0, 100000); self.roi_h = self._int(0, 100000)
        for row, (label1, widget1, label2, widget2) in enumerate((("X", self.roi_x, "Y", self.roi_y), ("宽", self.roi_w, "高", self.roi_h))):
            grid.addWidget(QLabel(label1), row, 0); grid.addWidget(widget1, row, 1); grid.addWidget(QLabel(label2), row, 2); grid.addWidget(widget2, row, 3)
        self.btn_roi = self._button("框选 ROI"); self.btn_roi.setCheckable(True); self.btn_full = self._button("全图")
        grid.addWidget(self.btn_roi, 2, 0, 1, 2); grid.addWidget(self.btn_full, 2, 2, 1, 2); layout.addWidget(roi)
        binary = QGroupBox("平滑 / 二值化"); form = QFormLayout(binary); self.chk_preview_binary = QCheckBox("显示效果")
        self.blur_ksize = self._int(1, 99, 2); self.blur_sigma = self._float(0, 20, .1, 1); self.binarize_method = QComboBox()
        for t, v in (("固定阈值", "fixed"), ("Otsu", "otsu"), ("自适应", "adaptive")): self.binarize_method.addItem(t, v)
        self.threshold = self._int(0, 255); self.invert = QCheckBox("反相"); self.adaptive_block = self._int(3, 999, 2); self.adaptive_c = self._int(-100, 100)
        self.morph_open = self._int(0, 999); self.morph_close = self._int(0, 999); self.target_mode = QComboBox(); self.target_mode.addItem("亮区孔洞", "hole"); self.target_mode.addItem("前景斑块", "blob")
        for label, widget in (("", self.chk_preview_binary), ("平滑核", self.blur_ksize), ("平滑 σ", self.blur_sigma), ("二值化", self.binarize_method), ("阈值", self.threshold), ("", self.invert), ("自适应邻域", self.adaptive_block), ("自适应 C", self.adaptive_c), ("开运算核", self.morph_open), ("闭运算核", self.morph_close), ("目标", self.target_mode)): form.addRow(label, widget)
        layout.addWidget(binary)
        edge = QGroupBox("边沿提取"); form = QFormLayout(edge); self.chk_preview_edge = QCheckBox("显示效果"); self.edge_mode = QComboBox()
        for t, v in (("二值轮廓", "contour"), ("Canny", "canny"), ("径向亚像素", "subpixel")): self.edge_mode.addItem(t, v)
        self.canny_low = self._int(0, 500); self.canny_high = self._int(0, 500); self.subpixel_search = self._float(.05, .9, .05, 2); self.subpixel_angles = self._int(16, 1440, 12); self.min_gradient = self._float(0, 255, 1, 1)
        for label, widget in (("", self.chk_preview_edge), ("来源", self.edge_mode), ("Canny 低", self.canny_low), ("Canny 高", self.canny_high), ("搜索比例", self.subpixel_search), ("射线数", self.subpixel_angles), ("最小梯度", self.min_gradient)): form.addRow(label, widget)
        layout.addWidget(edge)
        fit = QGroupBox("圆拟合筛选"); form = QFormLayout(fit); self.chk_preview_fit = QCheckBox("显示效果"); self.fit_method = QComboBox(); self.fit_method.addItem("最小二乘", "least_squares"); self.fit_method.addItem("最小外接圆", "min_enclosing")
        self.min_radius = self._float(.5, 100000, 1, 1); self.max_radius = self._float(1, 100000, 1, 1); self.min_circularity = self._float(0, 1, .01, 2); self.min_area = self._float(0, 1e9, 100, 0); self.max_area = self._float(0, 1e9, 100, 0)
        for label, widget in (("", self.chk_preview_fit), ("拟合", self.fit_method), ("最小半径", self.min_radius), ("最大半径", self.max_radius), ("最小圆度", self.min_circularity), ("最小面积", self.min_area), ("最大面积", self.max_area)): form.addRow(label, widget)
        layout.addWidget(fit); self.btn_reset = self._button("恢复默认参数"); layout.addWidget(self.btn_reset); layout.addStretch(); scroll.setWidget(host)
        self._detect_spins = [self.roi_x, self.roi_y, self.roi_w, self.roi_h, self.blur_ksize, self.blur_sigma, self.threshold, self.adaptive_block, self.adaptive_c, self.morph_open, self.morph_close, self.canny_low, self.canny_high, self.subpixel_search, self.subpixel_angles, self.min_gradient, self.min_radius, self.max_radius, self.min_circularity, self.min_area, self.max_area]
        self._detect_combos = [self.binarize_method, self.target_mode, self.edge_mode, self.fit_method]
        self._preview_boxes = {"binary": self.chk_preview_binary, "edge": self.chk_preview_edge, "fit": self.chk_preview_fit}; return scroll

    def _build_results_panel(self):
        panel = QWidget(); layout = QVBoxLayout(panel); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(4)
        self.summary_label = QLabel("尚未批量计算。第一张图片是偏移基准。"); self.summary_label.setWordWrap(True)
        row = QHBoxLayout(); self.btn_batch = self._button("批量识别"); self.btn_stop = self._button("停止"); self.progress = QProgressBar(); self.progress_label = QLabel("就绪"); self.btn_export = self._button("导出 CSV"); self.btn_export_images = self._button("导出处理图")
        for widget, stretch in ((self.btn_batch, 0), (self.btn_stop, 0), (self.progress, 1), (self.progress_label, 0), (self.btn_export, 0), (self.btn_export_images, 0)): row.addWidget(widget, stretch)
        self.table = QTableWidget(0, 12); self.table.setHorizontalHeaderLabels(["序号", "文件名", "状态", "圆心X", "圆心Y", "半径", "ΔX(px)", "ΔY(px)", "ΔX(mm)", "ΔY(mm)", "距离(mm)", "说明"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows); self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive); self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch); self.table.horizontalHeader().setSectionResizeMode(11, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.summary_label); layout.addLayout(row); layout.addWidget(self.table, 1); return panel

    def _int(self, lo, hi, step=1):
        widget = QSpinBox(); widget.setRange(lo, hi); widget.setSingleStep(step); widget.setKeyboardTracking(False); return widget
    def _float(self, lo, hi, step, decimals):
        widget = QDoubleSpinBox(); widget.setRange(lo, hi); widget.setSingleStep(step); widget.setDecimals(decimals); widget.setKeyboardTracking(False); return widget
    def _set_combo(self, combo, value):
        index = combo.findData(value)
        if index >= 0: combo.setCurrentIndex(index)

    def _connect_signals(self):
        for button, slot in ((self.btn_open, self.choose_directory), (self.btn_import, self.import_params), (self.btn_save, self.save_params), (self.btn_debug, self.debug_current), (self.btn_batch, self.start_batch), (self.btn_stop, self.stop_all), (self.btn_export, self.export_csv), (self.btn_export_images, self.export_images), (self.btn_reset, self.reset_params), (self.btn_fit, self.view.fit_image), (self.btn_actual, self.view.actual_size), (self.btn_full, self._use_full_image)): button.clicked.connect(slot)
        self.btn_roi.toggled.connect(self.view.set_roi_mode); self.file_list.currentRowChanged.connect(self._on_list_row_changed); self.table.cellClicked.connect(lambda row, _col: self.file_list.setCurrentRow(row)); self.view_mode.currentIndexChanged.connect(self.render); self.view.roiChanged.connect(self._on_roi_drawn); self.view.cursorMoved.connect(lambda x, y: self.local_status.setText(f"坐标 ({x}, {y})")); self.view.zoomChanged.connect(lambda z: self.local_status.setText(f"缩放 {z * 100:.0f}%")); self.diameter_spin.valueChanged.connect(self._on_diameter_changed)
        for widget in self._detect_spins: widget.valueChanged.connect(self._on_detect_param_changed)
        for combo in self._detect_combos: combo.currentIndexChanged.connect(self._on_detect_param_changed)
        self.invert.toggled.connect(self._on_detect_param_changed); self._preview_timer.timeout.connect(self.debug_current)
        for name, box in self._preview_boxes.items(): box.toggled.connect(lambda checked, stage=name: self._on_preview(stage, checked))

    def collect_params(self):
        return prepare_params({"roi_x": self.roi_x.value(), "roi_y": self.roi_y.value(), "roi_w": self.roi_w.value(), "roi_h": self.roi_h.value(), "blur_ksize": self.blur_ksize.value(), "blur_sigma": self.blur_sigma.value(), "binarize_method": self.binarize_method.currentData(), "threshold": self.threshold.value(), "invert": self.invert.isChecked(), "adaptive_block": self.adaptive_block.value(), "adaptive_c": self.adaptive_c.value(), "morph_open": self.morph_open.value(), "morph_close": self.morph_close.value(), "target_mode": self.target_mode.currentData(), "edge_mode": self.edge_mode.currentData(), "canny_low": self.canny_low.value(), "canny_high": self.canny_high.value(), "subpixel_search": self.subpixel_search.value(), "subpixel_angles": self.subpixel_angles.value(), "min_gradient": self.min_gradient.value(), "fit_method": self.fit_method.currentData(), "min_radius": self.min_radius.value(), "max_radius": self.max_radius.value(), "min_circularity": self.min_circularity.value(), "min_area": self.min_area.value(), "max_area": self.max_area.value(), "nozzle_diameter_mm": self.diameter_spin.value()})

    def apply_params(self, params):
        p = prepare_params(params); self._loading = True
        try:
            for name in ("roi_x", "roi_y", "roi_w", "roi_h", "blur_ksize", "blur_sigma", "threshold", "adaptive_block", "adaptive_c", "morph_open", "morph_close", "canny_low", "canny_high", "subpixel_search", "subpixel_angles", "min_gradient", "min_radius", "max_radius", "min_circularity", "min_area", "max_area"): getattr(self, name).setValue(p[name])
            for name in ("binarize_method", "target_mode", "edge_mode", "fit_method"): self._set_combo(getattr(self, name), p[name])
            self.invert.setChecked(p["invert"]); self.diameter_spin.setValue(p["nozzle_diameter_mm"])
        finally: self._loading = False
        self._update_enables(); self.view.set_roi_rect(p["roi_x"], p["roi_y"], p["roi_w"], p["roi_h"])

    def _update_enables(self):
        method = self.binarize_method.currentData(); self.threshold.setEnabled(method == "fixed"); self.adaptive_block.setEnabled(method == "adaptive"); self.adaptive_c.setEnabled(method == "adaptive")
        edge = self.edge_mode.currentData(); self.canny_low.setEnabled(edge == "canny"); self.canny_high.setEnabled(edge == "canny")
        for widget in (self.subpixel_search, self.subpixel_angles, self.min_gradient): widget.setEnabled(edge == "subpixel")

    def _on_detect_param_changed(self, *_):
        if self._loading: return
        self._update_enables(); self.view.set_roi_rect(self.roi_x.value(), self.roi_y.value(), self.roi_w.value(), self.roi_h.value())
        if any(x is not None for x in self.measurements): self.batch_stale = True; self.update_summary()
        if self._active_stage and self.image_bgr is not None and not self._workers_running(): self._preview_timer.start()

    def _on_diameter_changed(self, *_):
        if self._loading: return
        if self.measurements:
            for index, value in enumerate(self.measurements):
                if value is not None: self._fill_row(index)
            self.update_summary()

    def _on_preview(self, stage, checked):
        if self._loading: return
        if checked:
            self._loading = True
            for name, box in self._preview_boxes.items(): box.setChecked(name == stage)
            self._loading = False; self._active_stage = stage
            self._set_combo(self.view_mode, {"binary": "binary", "edge": "edge", "fit": "overlay"}[stage]); self.debug_current()
        elif self._active_stage == stage: self._active_stage = None; self.render()

    def _on_roi_drawn(self, x, y, w, h):
        self._loading = True
        for widget, value in zip((self.roi_x, self.roi_y, self.roi_w, self.roi_h), (x, y, w, h)): widget.setValue(value)
        self._loading = False; self._on_detect_param_changed(); self._status(f"ROI：X={x}, Y={y}, 宽={w}, 高={h}")
    def _use_full_image(self):
        self._loading = True
        for widget in (self.roi_x, self.roi_y, self.roi_w, self.roi_h): widget.setValue(0)
        self._loading = False; self._on_detect_param_changed()

    def choose_directory(self):
        folder = QFileDialog.getExistingDirectory(self, "选择图片目录", self.image_dir or str(Path.home()))
        if folder: self.load_directory(folder)
    def load_directory(self, folder, quiet=False):
        paths = list_images(folder)
        if not paths:
            if not quiet: QMessageBox.information(self, "没有图片", "目录中没有支持的图片。")
            return
        self.image_dir, self.paths, self.batch_paths, self.measurements = folder, paths, [], []
        self.batch_stale = False; self.dir_edit.setText(folder); self.file_list.clear(); self.table.setRowCount(0)
        for index, path in enumerate(paths):
            item = QListWidgetItem(os.path.basename(path)); item.setToolTip(path)
            if index == 0: font = item.font(); font.setBold(True); item.setFont(font)
            self.file_list.addItem(item)
        self.file_list.parentWidget().setTitle(f"图片列表（{len(paths)}）") if isinstance(self.file_list.parentWidget(), QGroupBox) else None
        self.file_list.setCurrentRow(0); self.load_image(paths[0]); self.update_summary(); self._set_busy(False); self._status(f"已载入 {len(paths)} 张图片")
    def _on_list_row_changed(self, row):
        if 0 <= row < len(self.paths): self.load_image(self.paths[row]); self.table.selectRow(row) if row < self.table.rowCount() else None
    def load_image(self, path):
        image = imread_unicode(path); self.current_path = path; self.live_result = self.live_meas = None
        if image is None: self.image_bgr = None; self.view.clear_image(); self.result_label.setText("无法读取图片"); return
        self._pending_reset = self.image_bgr is None or self.image_bgr.shape[:2] != image.shape[:2]; self.image_bgr = image; self.render(); self._status(f"{os.path.basename(path)}  {image.shape[1]}×{image.shape[0]}")

    def debug_current(self, *_):
        if self._busy_debug or self._workers_running() or self.image_bgr is None: return
        self._busy_debug = True
        try:
            result = detect_nozzle(self.image_bgr, self.collect_params(), debug=True); self.live_result = result; self.live_meas = result.as_dict(); self.live_meas["path"] = self.current_path
            if result.ok and self.view_mode.currentData() in {"raw", "gray"}: self._set_combo(self.view_mode, "overlay")
            self.render()
        except Exception as exc: QMessageBox.warning(self, "调试失败", str(exc))
        finally: self._busy_debug = False

    def render(self, *_):
        if self.image_bgr is None: return
        mode = self.view_mode.currentData(); result = self.live_result; meas = self.live_meas
        if meas is None:
            index = self.paths.index(self.current_path) if self.current_path in self.paths else -1
            meas = self.measurements[index] if 0 <= index < len(self.measurements) else None
        if mode == "binary" and result is not None and result.binary is not None: base = cv2.cvtColor(result.binary, cv2.COLOR_GRAY2BGR)
        elif mode == "gray": base = cv2.cvtColor(cv2.cvtColor(self.image_bgr, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
        else: base = self.image_bgr
        edges = rejected = circle = None; show_edges = show_circle = False
        if mode in {"edge", "overlay"} and meas and meas.get("ok"):
            edges, rejected = meas.get("edge_points"), meas.get("rejected_points"); show_edges = True
            if mode == "overlay": circle = (meas["cx"], meas["cy"], meas["r"]); show_circle = True
        self.view.set_base(base, self._pending_reset); self._pending_reset = False; self.view.set_overlay(edges, rejected, circle, show_edges, show_circle)
        self.view.set_roi_rect(self.roi_x.value(), self.roi_y.value(), self.roi_w.value(), self.roi_h.value())
        if meas and meas.get("ok"): self.result_label.setText(f"圆心 ({meas['cx']:.2f}, {meas['cy']:.2f}) px  半径 {meas['r']:.2f} px  圆度 {meas['circularity']:.3f}  RMS {meas['rms']:.3f} px")
        elif meas: self.result_label.setText("检测失败：" + meas.get("message", ""))

    def start_batch(self):
        if self._workers_running() or not self.paths: return
        params = self.collect_params(); self.batch_paths = list(self.paths); self.measurements = [None] * len(self.paths); self._batch_detect = {k: v for k, v in params.items() if k != "nozzle_diameter_mm"}; self.batch_stale = False
        self.table.setRowCount(len(self.paths)); self.progress.setRange(0, len(self.paths)); self.progress.setValue(0); self._set_busy(True)
        worker = BatchWorker(self.batch_paths, params); self.batch_worker = worker
        worker.itemReady.connect(self._on_batch_item); worker.progress.connect(self._on_progress); worker.batchFinished.connect(self._on_batch_finished); worker.finished.connect(lambda w=worker: self._worker_done("batch_worker", w)); worker.start(); self._status("正在批量识别…", 0)
    def _on_batch_item(self, index, payload):
        if self._shutdown or index >= len(self.measurements): return
        self.measurements[index] = payload; self._fill_row(index); self.update_summary()
    def _on_progress(self, done, total, name):
        if self._shutdown: return
        self.progress.setMaximum(total); self.progress.setValue(done); self.progress_label.setText(f"{done}/{total} {name}")
    def _on_batch_finished(self, stopped):
        if self._shutdown: return
        if stopped:
            for index, value in enumerate(self.measurements):
                if value is None: self.measurements[index] = fail_payload(self.batch_paths[index], "已取消"); self._fill_row(index)
        self.batch_stale = {k: v for k, v in self.collect_params().items() if k != "nozzle_diameter_mm"} != self._batch_detect
        self._set_busy(False); self.update_summary(); self.progress_label.setText("已停止" if stopped else "完成"); self._status("批量识别已停止" if stopped else "批量识别完成")
    def stop_all(self):
        for worker in (self.batch_worker, self.export_worker):
            if worker is not None and worker.isRunning(): worker.stop()
        self.btn_stop.setEnabled(False); self._status("正在停止，等待当前图片处理完成…", 0)
    def _worker_done(self, attribute, worker):
        if getattr(self, attribute, None) is worker: setattr(self, attribute, None)
        worker.deleteLater()

    def _offset_rows(self):
        return [value if value is not None else fail_payload(self.batch_paths[i] if i < len(self.batch_paths) else "", "") for i, value in enumerate(self.measurements)]
    def _fill_row(self, index):
        row = self.measurements[index]; offsets, _ = compute_offsets(self._offset_rows(), self.diameter_spin.value()); off = offsets[index]
        values = [str(index + 1), ("基准  " if index == 0 else "") + os.path.basename(row.get("path", "")), "成功" if row.get("ok") else row.get("message", "失败"), fmt_px(row.get("cx")) if row.get("ok") else "", fmt_px(row.get("cy")) if row.get("ok") else "", fmt_px(row.get("r")) if row.get("ok") else "", fmt_px(off["dx_px"], True), fmt_px(off["dy_px"], True), fmt_mm(off["dx_mm"], True), fmt_mm(off["dy_mm"], True), fmt_mm(off["dist_mm"]), row.get("summary", "")]
        for column, value in enumerate(values):
            item = self.table.item(index, column) or QTableWidgetItem(); item.setText(value); self.table.setItem(index, column, item)
    def update_summary(self):
        if not self.measurements: self.summary_label.setText("尚未批量计算。第一张图片是偏移基准。"); return
        done = [x for x in self.measurements if x is not None]; offsets, info = compute_offsets(self._offset_rows(), self.diameter_spin.value()); text = f"成功 {sum(bool(x.get('ok')) for x in done)}/{len(self.measurements)}"
        if info["ref_ok"]: text += f"  基准 {info['ref_name']}  比例尺 {info['mm_per_px']:.6f} mm/px"; distances = [x["dist_mm"] for x in offsets if x["dist_mm"] is not None]; text += f"  最大偏移 {max(distances):.4f} mm" if distances else ""
        elif self.measurements[0] is not None: text += "  第一张未检测成功，无法计算偏移"
        if self.batch_stale: text += "  参数已修改，请重新批量识别"
        self.summary_label.setText(text)

    def export_csv(self):
        if not self.measurements or any(x is None for x in self.measurements): QMessageBox.information(self, "导出 CSV", "请先完成批量识别。"); return
        path, _ = QFileDialog.getSaveFileName(self, "导出 CSV", os.path.join(self.image_dir, "喷嘴偏移结果.csv"), "CSV (*.csv)")
        if not path: return
        if not path.lower().endswith(".csv"): path += ".csv"
        try: _write_results_csv(path, self.measurements, self.diameter_spin.value())
        except OSError as exc: QMessageBox.warning(self, "导出失败", str(exc)); return
        self._status(f"已导出：{path}")
    def export_images(self):
        if self._workers_running() or not self.paths: return
        parent = QFileDialog.getExistingDirectory(self, "选择处理图输出位置", self.image_dir)
        if not parent: return
        folder = os.path.join(parent, "处理结果_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19])
        worker = ExportWorker(self.paths, self.collect_params(), folder, self.image_dir); self.export_worker = worker; self._set_busy(True)
        worker.progress.connect(self._on_progress); worker.exportFinished.connect(self._on_export_finished); worker.finished.connect(lambda w=worker: self._worker_done("export_worker", w)); worker.start(); self._status(f"正在导出处理图：{folder}", 0)
    def _on_export_finished(self, payload):
        if self._shutdown: return
        self._set_busy(False); self.progress_label.setText("导出完成" if payload.get("ok") else "导出失败")
        if payload.get("ok"):
            self._status(f"处理图已导出：{payload['folder']}")
            if self._active: QMessageBox.information(self, "导出处理图", f"已保存到：\n{payload['folder']}\n成功 {payload['ok_count']}/{payload['total']} 张")
        else: QMessageBox.warning(self, "导出处理图", payload.get("message", "导出失败")) if self._active else self._status("导出失败：" + payload.get("message", ""))

    def _config_document(self): return {"version": VERSION, "algorithm_version": ALGORITHM_VERSION, "tool": TOOL_NAME, "last_directory": self.image_dir, "params": self.collect_params()}
    def _write_config(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True); temp = str(path) + ".tmp"
        with open(temp, "w", encoding="utf-8") as file: json.dump(self._config_document(), file, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    def save_params(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存参数", str(self.config_path), "JSON (*.json)")
        if not path: return
        if not path.lower().endswith(".json"): path += ".json"
        try: self._write_config(path); self._write_config(self.config_path)
        except OSError as exc: QMessageBox.warning(self, "保存失败", str(exc)); return
        self._status(f"参数已保存：{path}")
    def import_params(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入参数", str(self.config_path.parent), "JSON (*.json)")
        if not path: return
        try:
            with open(path, encoding="utf-8") as file: data = json.load(file)
            self.apply_params(data.get("params", data)); self._write_config(self.config_path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc: QMessageBox.warning(self, "导入失败", str(exc)); return
        self._on_detect_param_changed(); self._status(f"已导入参数：{path}")
    def reset_params(self):
        self.apply_params(default_params()); self._on_detect_param_changed(); self._status("已恢复默认参数")
    def _load_startup(self):
        if not self.config_path.is_file(): return
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8")); self.apply_params(data.get("params", data)); folder = data.get("last_directory", "")
            if folder and os.path.isdir(folder): self.load_directory(folder, quiet=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError): self._status("插件配置读取失败，已使用默认参数")

    def _workers_running(self): return any(w is not None and w.isRunning() for w in (self.batch_worker, self.export_worker))
    def _set_busy(self, busy):
        for widget in (self.btn_batch, self.btn_debug, self.btn_open, self.btn_import, self.btn_save, self.btn_reset, self.btn_export_images): widget.setEnabled(not busy)
        self.btn_stop.setEnabled(busy); self.btn_export.setEnabled(not busy and bool(self.measurements) and all(x is not None for x in self.measurements))
    def activate(self): self._active = True
    def deactivate(self): self._active = False; self._preview_timer.stop()
    def shutdown(self):
        if self._shutdown: return
        self._shutdown = True; self._active = False; self._preview_timer.stop()
        try: self._write_config(self.config_path)
        except OSError: pass
        for attribute in ("batch_worker", "export_worker"):
            worker = getattr(self, attribute)
            if worker is None:
                continue
            if worker.isRunning():
                worker.stop()
            still_running = not worker.wait(5000)
            # 去掉所有指向页面的回调，防止宿主销毁页面后收到迟到信号。
            for signal in (
                worker.finished,
                getattr(worker, "progress", None),
                getattr(worker, "itemReady", None),
                getattr(worker, "batchFinished", None),
                getattr(worker, "exportFinished", None),
            ):
                if signal is None:
                    continue
                try:
                    signal.disconnect()
                except (TypeError, RuntimeError):
                    pass
            if still_running:
                _ORPHANED_WORKERS.add(worker)
                worker.finished.connect(
                    lambda w=worker: (_ORPHANED_WORKERS.discard(w), w.deleteLater())
                )
            else:
                worker.deleteLater()
            setattr(self, attribute, None)
    def closeEvent(self, event): self.shutdown(); super().closeEvent(event)
    def dragEnterEvent(self, event): event.acceptProposedAction() if event.mimeData().hasUrls() else event.ignore()
    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile(); self.load_directory(path if os.path.isdir(path) else os.path.dirname(path)) if path else None


def create_plugin(context, parent=None):
    return NozzleBatchPlugin(context, parent)
