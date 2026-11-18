"""喷嘴偏移标定插件（PyQt6）。

本模块可由 ToolPlatform 直接导入；只有直接运行本文件时才创建 QApplication。
"""

import csv
import json
import math
import os
import sys
import threading
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from PyQt6.QtCore import QPoint, QStandardPaths, Qt, QSize, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QDesktopServices,
    QDoubleValidator,
    QFontMetrics,
    QImage,
    QIntValidator,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QAbstractScrollArea,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

try:
    import cv2
except Exception:
    cv2 = None


PLUGIN_META = {
    "id": "nozzle_offset",
    "name": "喷嘴圆心与偏移标定",
    "version": "3.1",
    "category": "视觉工具",
    "description": "支持手动或自动查找喷嘴圆心，并完成基准、偏移计算、结果导出和图片比对。",
    "order": 20,
}


_ORPHANED_AUTO_WORKERS: set[QThread] = set()


def _release_orphan_worker(worker):
    if worker not in _ORPHANED_AUTO_WORKERS:
        return
    _ORPHANED_AUTO_WORKERS.discard(worker)
    worker.deleteLater()


def _context_member(context, name):
    """同时兼容对象式和字典式插件上下文。"""
    if context is None:
        return None
    if isinstance(context, dict):
        return context.get(name)
    return getattr(context, name, None)


def _plugin_data_root(context):
    """解析宿主提供的可写插件数据目录，并为独立运行提供安全兜底。"""
    for name in ("plugin_data_dir", "get_plugin_data_dir", "data_dir_for_plugin"):
        provider = _context_member(context, name)
        if not callable(provider):
            continue
        try:
            path = provider(PLUGIN_META["id"])
        except TypeError:
            path = provider()
        if path:
            return os.path.abspath(os.fspath(path))

    plugin_data_path = _context_member(context, "plugin_data_path")
    if plugin_data_path:
        return os.path.abspath(os.fspath(plugin_data_path))

    data_dir = _context_member(context, "data_dir")
    if callable(data_dir):
        for args in ((PLUGIN_META["id"],), ()):
            try:
                path = data_dir(*args)
            except TypeError:
                continue
            if path:
                return os.path.abspath(os.fspath(path))
    elif data_dir:
        # 当前 ToolPlatform 的 PluginContext.data_dir 已按插件 ID 隔离。
        return os.path.abspath(os.fspath(data_dir))

    base_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    if not base_dir:
        base_dir = os.path.join(os.path.expanduser("~"), ".toolplatform")
    return os.path.join(base_dir, PLUGIN_META["id"])


MODE_THREE_POINTS = "三点画圆"
MODE_TWO_POINTS = "两点画圆（直径两端）"
MODE_CENTER_POINT = "圆心画圆（圆心+边缘点）"

MODE_POINT_COUNT = {
    MODE_THREE_POINTS: 3,
    MODE_TWO_POINTS: 2,
    MODE_CENTER_POINT: 2,
}

DEFAULT_CAMERA_WIDTH = 3264
DEFAULT_CAMERA_HEIGHT = 2448
DEFAULT_CAMERA_FPS = 15


def default_params() -> dict:
    """返回自动找圆心的默认参数；实际喷嘴直径由偏移标定参数统一管理。"""
    return {
        "roi_x": 0,
        "roi_y": 0,
        "roi_w": 0,
        "roi_h": 0,
        "blur_ksize": 5,
        "blur_sigma": 1.0,
        "binarize_method": "fixed",
        "threshold": 180,
        "invert": False,
        "adaptive_block": 51,
        "adaptive_c": 5,
        "morph_open": 3,
        "morph_close": 15,
        "target_mode": "hole",
        "edge_mode": "contour",
        "canny_low": 40,
        "canny_high": 120,
        "subpixel_search": 0.45,
        "subpixel_angles": 360,
        "min_gradient": 8.0,
        "fit_method": "least_squares",
        "min_radius": 40.0,
        "max_radius": 300.0,
        "min_circularity": 0.50,
        "min_area": 500.0,
        "max_area": 0.0,
    }


def prepare_params(params: dict | None) -> dict:
    """补齐自动检测缺省项并做类型校正，忽略无法识别的字段。"""
    defaults = default_params()
    merged = defaults.copy()
    if params:
        for key in merged:
            if key in params and params[key] is not None:
                merged[key] = params[key]

    def as_int(key: str, lo: int, hi: int) -> None:
        try:
            value = float(merged[key])
            if not np.isfinite(value):
                raise ValueError("非有限数")
            merged[key] = int(round(value))
        except (TypeError, ValueError, OverflowError):
            merged[key] = defaults[key]
        merged[key] = max(lo, min(hi, merged[key]))

    def as_float(key: str, lo: float, hi: float) -> None:
        try:
            merged[key] = float(merged[key])
        except (TypeError, ValueError):
            merged[key] = defaults[key]
        if not np.isfinite(merged[key]):
            merged[key] = defaults[key]
        merged[key] = max(lo, min(hi, float(merged[key])))

    as_int("roi_x", 0, 100000)
    as_int("roi_y", 0, 100000)
    as_int("roi_w", 0, 100000)
    as_int("roi_h", 0, 100000)
    as_int("blur_ksize", 1, 99)
    as_float("blur_sigma", 0.0, 50.0)
    as_int("threshold", 0, 255)
    as_int("adaptive_block", 3, 999)
    as_int("adaptive_c", -100, 100)
    as_int("morph_open", 0, 999)
    as_int("morph_close", 0, 999)
    as_int("canny_low", 0, 500)
    as_int("canny_high", 0, 500)
    as_float("subpixel_search", 0.05, 0.9)
    as_int("subpixel_angles", 16, 1440)
    as_float("min_gradient", 0.0, 255.0)
    as_float("min_radius", 0.5, 100000.0)
    as_float("max_radius", 1.0, 100000.0)
    as_float("min_circularity", 0.0, 1.0)
    as_float("min_area", 0.0, 1e12)
    as_float("max_area", 0.0, 1e12)
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
            if self.ok
            else self.message
        )
        return {
            "ok": self.ok,
            "message": self.message,
            "summary": summary,
            "cx": float(self.center_x),
            "cy": float(self.center_y),
            "r": float(self.radius),
            "rms": float(self.rms),
            "circularity": float(self.circularity),
            "area": float(self.area),
            "edge_count": int(self.edge_count),
            "used_threshold": float(self.used_threshold),
            "edge_note": self.edge_note,
            "edge_points": None
            if self.edge_points is None
            else np.ascontiguousarray(self.edge_points, dtype=np.float32),
            "rejected_points": None
            if self.rejected_points is None
            else np.ascontiguousarray(self.rejected_points, dtype=np.float32),
        }


def detect_nozzle(image: np.ndarray, params: dict | None = None, debug: bool = False) -> DetectResult:
    """检测单张图片中的喷嘴圆心；成功坐标相对于整幅图像。"""
    if cv2 is None:
        return DetectResult(False, "未安装 OpenCV，无法自动检测。")
    settings = prepare_params(params)
    if image is None or not isinstance(image, np.ndarray) or image.size == 0:
        return DetectResult(False, "图片为空，无法检测。")

    color = _to_bgr(image)
    gray_full = cv2.cvtColor(color, cv2.COLOR_BGR2GRAY)
    roi = _normalize_roi(
        settings["roi_x"], settings["roi_y"], settings["roi_w"], settings["roi_h"],
        gray_full.shape[1], gray_full.shape[0],
    )
    if roi is None:
        result = DetectResult(False, "检测范围无效或过小，请重新设置，或使用全图。")
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
    offset = np.array([x0, y0], dtype=np.float64)
    if result.edge_points is not None and len(result.edge_points):
        result.edge_points = result.edge_points + offset
    if result.rejected_points is not None and len(result.rejected_points):
        result.rejected_points = result.rejected_points + offset
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
    if image.ndim != 3 or image.shape[2] not in (3, 4):
        raise ValueError("图片数组格式无效。")
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    return image


def _normalize_roi(x: int, y: int, w: int, h: int, img_w: int, img_h: int):
    if w <= 1 or h <= 1:
        return 0, 0, img_w, img_h
    if x < 0:
        w += x
        x = 0
    if y < 0:
        h += y
        y = 0
    if x >= img_w or y >= img_h:
        return None
    w = min(w, img_w - x)
    h = min(h, img_h - y)
    if w < 10 or h < 10:
        return None
    return int(x), int(y), int(w), int(h)


def _odd_ksize(value: int) -> int:
    k = int(value)
    if k <= 1:
        return 1
    return k + 1 if k % 2 == 0 else k


def _morph_ksize(value: int) -> int:
    k = int(value)
    if k <= 1:
        return 0
    return k + 1 if k % 2 == 0 else k


def _blur(gray: np.ndarray, ksize: int, sigma: float) -> np.ndarray:
    k = _odd_ksize(ksize)
    return gray if k <= 1 else cv2.GaussianBlur(gray, (k, k), float(sigma))


def _binarize(gray: np.ndarray, settings: dict) -> tuple[np.ndarray, float, str]:
    thresh_type = cv2.THRESH_BINARY_INV if settings["invert"] else cv2.THRESH_BINARY
    method = settings["binarize_method"]
    if method == "otsu":
        used, binary = cv2.threshold(gray, 0, 255, thresh_type | cv2.THRESH_OTSU)
        return binary, float(used), ""
    if method == "adaptive":
        block = max(3, _odd_ksize(settings["adaptive_block"]))
        limit = min(gray.shape[:2])
        if limit < 3:
            return gray, -1.0, "检测范围过小，无法做自适应阈值。"
        if block >= limit:
            block = limit - 1 if (limit - 1) % 2 == 1 else limit - 2
            block = max(3, block)
        binary = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, thresh_type,
            block, int(settings["adaptive_c"]),
        )
        return binary, -1.0, ""
    used, binary = cv2.threshold(gray, int(settings["threshold"]), 255, thresh_type)
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
        return DetectResult(False, "二值图全黑，没有前景。可降低阈值；若暗孔需变成白色斑块，请勾选反相。")
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
        if is_hole:
            hole_count += 1
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
        box_x, box_y, box_w, box_h = cv2.boundingRect(contour)
        if box_x <= 1 or box_y <= 1 or box_x + box_w >= width - 1 or box_y + box_h >= height - 1:
            continue
        (cx, cy), radius = cv2.minEnclosingCircle(contour)
        radius = float(radius)
        if radius < settings["min_radius"] or radius > settings["max_radius"]:
            continue
        norm_dist = float(np.hypot(cx - width / 2.0, cy - height / 2.0) / max(width, height))
        candidates.append((circularity - 0.2 * norm_dist, contour, circularity, area))

    if not candidates:
        if settings["target_mode"] == "hole" and hole_count == 0:
            return DetectResult(False, "没有检测到被前景包围的孔。请保持亮区孔洞并让检测范围包含完整亮环。")
        return DetectResult(False, "未找到满足半径、面积和圆度的目标。请放宽筛选参数。")

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
        if edge_note:
            message = edge_note + message
        return DetectResult(
            True, message, cx, cy, radius, circularity, area, rms, int(len(inliers)),
            edge_points=inliers, rejected_points=rejected, edge_note=edge_note,
        )
    return DetectResult(False, "边沿点无法拟合出符合半径范围的圆。请调整边沿方式或半径范围。")


def _extract_edges(blurred, contour, contour_points, settings):
    mode = settings["edge_mode"]
    if mode == "canny":
        points = _canny_points(blurred, contour, settings["canny_low"], settings["canny_high"])
        return (points, "") if points is not None else (contour_points, "Canny 边沿点不足，已改用二值轮廓。")
    if mode == "subpixel":
        points = _refine_radial(
            blurred, contour_points, settings["subpixel_angles"],
            settings["subpixel_search"], settings["min_gradient"],
        )
        return (points, "") if points is not None else (contour_points, "径向梯度边沿点不足，已改用二值轮廓。")
    return contour_points, ""


def _canny_points(blurred, contour, low, high):
    edges = cv2.Canny(blurred, int(low), int(high))
    band = np.zeros(blurred.shape, dtype=np.uint8)
    cv2.drawContours(band, [contour], -1, 255, thickness=5)
    ys, xs = np.where((edges > 0) & (band > 0))
    if len(xs) < 30:
        return None
    return np.column_stack((xs.astype(np.float64), ys.astype(np.float64)))


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
        valid = (xs >= 0.0) & (ys >= 0.0) & (xs <= width - 1.001) & (ys <= height - 1.001)
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
    v00, v10, v01, v11 = gray[y0, x0], gray[y0, x1], gray[y1, x0], gray[y1, x1]
    return v00 * (1.0 - wx) * (1.0 - wy) + v10 * wx * (1.0 - wy) + v01 * (1.0 - wx) * wy + v11 * wx * wy


def _parabola_delta(gradient, index):
    if index <= 0 or index >= len(gradient) - 1:
        return 0.0
    y0, y1, y2 = float(gradient[index - 1]), float(gradient[index]), float(gradient[index + 1])
    denom = y0 - 2.0 * y1 + y2
    if abs(denom) < 1e-9:
        return 0.0
    return float(np.clip(0.5 * (y0 - y2) / denom, -1.0, 1.0))


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
        rms = float(np.sqrt(np.mean((dist - radius) ** 2)))
        return cx, cy, radius, rms, points, np.empty((0, 2), dtype=np.float64)
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
    rms = float(np.sqrt(np.mean((dist - radius) ** 2)))
    return cx, cy, radius, rms, used, rejected


def _kasa(points):
    if len(points) < 3:
        return None
    x, y = points[:, 0], points[:, 1]
    design = np.column_stack((x, y, np.ones(len(points), dtype=np.float64)))
    target = -(x * x + y * y)
    try:
        solution, _residuals, rank, _singular = np.linalg.lstsq(design, target, rcond=None)
    except np.linalg.LinAlgError:
        return None
    if rank < 3:
        return None
    coeff_d, coeff_e, coeff_f = (float(v) for v in solution)
    cx, cy = -coeff_d / 2.0, -coeff_e / 2.0
    radius_sq = cx * cx + cy * cy - coeff_f
    if not np.isfinite(radius_sq) or radius_sq <= 1e-6:
        return None
    return cx, cy, float(np.sqrt(radius_sq))


def _select_inliers(points, cx, cy, radius):
    dist = np.hypot(points[:, 0] - cx, points[:, 1] - cy)
    residual = np.abs(dist - radius)
    median = float(np.median(residual))
    mad = float(np.median(np.abs(residual - median)))
    threshold = min(max(1.5, median + 3.0 * 1.4826 * mad), max(2.0, 0.08 * radius))
    mask = residual <= threshold
    if int(mask.sum()) < max(15, int(0.45 * len(points))):
        return points, np.empty((0, 2), dtype=np.float64)
    return points[mask], points[~mask]


def _resolve_auto_config_path(context) -> str:
    resolver = _context_member(context, "plugin_config_file") or _context_member(context, "get_plugin_config_file")
    if callable(resolver):
        for args in (("nozzle_offset", "auto_detect.json"), ("auto_detect.json",)):
            try:
                path = resolver(*args)
            except TypeError:
                continue
            if path:
                return os.path.abspath(os.fspath(path))
    config_dir = _context_member(context, "config_dir")
    if config_dir:
        return os.path.abspath(os.path.join(os.fspath(config_dir), "auto_detect.json"))
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".toolplatform")
    return os.path.abspath(os.path.join(base, "ToolPlatform", "nozzle_offset", "auto_detect.json"))


def pixmap_to_bgr(pixmap: QPixmap) -> np.ndarray:
    """GUI 线程中把完整 QPixmap 快照转换成独立 BGR 数组。"""
    if pixmap.isNull():
        raise ValueError("当前画面为空。")
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    raw = image.bits().asstring(image.sizeInBytes())
    rgba = np.frombuffer(raw, dtype=np.uint8).reshape(image.height(), image.width(), 4)
    return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR).copy()


class AutoDetectWorker(QThread):
    result_ready = pyqtSignal(object)

    def __init__(self, image_bgr: np.ndarray, params: dict):
        super().__init__()
        self.image_bgr = np.ascontiguousarray(image_bgr).copy()
        self.params = prepare_params(params)

    def run(self):
        if self.isInterruptionRequested():
            return
        try:
            result = detect_nozzle(self.image_bgr, self.params, debug=False)
        except Exception as exc:
            result = DetectResult(False, f"自动检测异常：{exc}")
        if not self.isInterruptionRequested():
            self.result_ready.emit(result)


class AutoDetectParamsDialog(QDialog):
    """紧凑呈现全部自动检测参数，不占用插件左侧常驻空间。"""

    params_saved = pyqtSignal(object)

    def __init__(self, params: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("自动找圆心参数")
        self.setModal(True)
        self.resize(520, 610)
        self.controls = {}
        root = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._build_roi_binary_tab(), "范围与二值化")
        tabs.addTab(self._build_edge_fit_tab(), "边沿与拟合")
        root.addWidget(tabs)
        hint = QLabel("宽或高为 0 表示全图；实际喷嘴直径仍使用主界面的“实际直径(mm)”。")
        hint.setWordWrap(True)
        hint.setObjectName("hintLabel")
        root.addWidget(hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.RestoreDefaults
            | QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).setText("恢复默认")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.StandardButton.Save).clicked.connect(
            lambda: self.params_saved.emit(self.params())
        )
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(
            lambda: self.set_params(default_params())
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).clicked.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).clicked.connect(self.reject)
        root.addWidget(buttons)
        self.set_params(params)
        self.controls["binarize_method"].currentIndexChanged.connect(self._update_enables)
        self.controls["edge_mode"].currentIndexChanged.connect(self._update_enables)
        self._update_enables()

    def _int(self, key, minimum, maximum, step=1):
        widget = QSpinBox()
        widget.setRange(minimum, maximum)
        widget.setSingleStep(step)
        widget.setKeyboardTracking(False)
        self.controls[key] = widget
        return widget

    def _float(self, key, minimum, maximum, step, decimals):
        widget = QDoubleSpinBox()
        widget.setRange(minimum, maximum)
        widget.setSingleStep(step)
        widget.setDecimals(decimals)
        widget.setKeyboardTracking(False)
        self.controls[key] = widget
        return widget

    def _combo(self, key, items):
        widget = QComboBox()
        for text, value in items:
            widget.addItem(text, value)
        self.controls[key] = widget
        return widget

    def _build_roi_binary_tab(self):
        host = QWidget()
        layout = QVBoxLayout(host)
        roi = QGroupBox("ROI（像素）")
        form = QFormLayout(roi)
        form.addRow("X", self._int("roi_x", 0, 100000))
        form.addRow("Y", self._int("roi_y", 0, 100000))
        form.addRow("宽", self._int("roi_w", 0, 100000))
        form.addRow("高", self._int("roi_h", 0, 100000))
        layout.addWidget(roi)
        binary = QGroupBox("预处理与候选")
        form = QFormLayout(binary)
        form.addRow("平滑核", self._int("blur_ksize", 1, 99, 2))
        form.addRow("平滑 σ", self._float("blur_sigma", 0, 50, 0.1, 2))
        form.addRow("二值化", self._combo("binarize_method", [
            ("固定阈值", "fixed"), ("Otsu", "otsu"), ("自适应阈值", "adaptive")
        ]))
        form.addRow("阈值", self._int("threshold", 0, 255))
        invert = QCheckBox("反相（暗像素作为前景）")
        self.controls["invert"] = invert
        form.addRow("", invert)
        form.addRow("自适应邻域", self._int("adaptive_block", 3, 999, 2))
        form.addRow("自适应 C", self._int("adaptive_c", -100, 100))
        form.addRow("开运算核", self._int("morph_open", 0, 999))
        form.addRow("闭运算核", self._int("morph_close", 0, 999))
        form.addRow("目标类型", self._combo("target_mode", [
            ("亮区孔洞", "hole"), ("前景斑块", "blob")
        ]))
        layout.addWidget(binary)
        layout.addStretch(1)
        return host

    def _build_edge_fit_tab(self):
        host = QWidget()
        layout = QVBoxLayout(host)
        edge = QGroupBox("边沿提取")
        form = QFormLayout(edge)
        form.addRow("边沿来源", self._combo("edge_mode", [
            ("二值轮廓", "contour"), ("Canny 边沿", "canny"),
            ("径向梯度亚像素", "subpixel")
        ]))
        form.addRow("Canny 低阈值", self._int("canny_low", 0, 500))
        form.addRow("Canny 高阈值", self._int("canny_high", 0, 500))
        form.addRow("径向搜索比例", self._float("subpixel_search", 0.05, 0.9, 0.05, 2))
        form.addRow("径向射线数", self._int("subpixel_angles", 16, 1440, 12))
        form.addRow("最小梯度", self._float("min_gradient", 0, 255, 1, 1))
        layout.addWidget(edge)
        fit = QGroupBox("圆拟合与筛选")
        form = QFormLayout(fit)
        form.addRow("拟合方法", self._combo("fit_method", [
            ("最小二乘", "least_squares"), ("最小外接圆", "min_enclosing")
        ]))
        form.addRow("最小半径", self._float("min_radius", 0.5, 100000, 1, 1))
        form.addRow("最大半径", self._float("max_radius", 1, 100000, 1, 1))
        form.addRow("最小圆度", self._float("min_circularity", 0, 1, 0.01, 3))
        form.addRow("最小面积", self._float("min_area", 0, 1e12, 100, 1))
        form.addRow("最大面积（0 不限）", self._float("max_area", 0, 1e12, 100, 1))
        layout.addWidget(fit)
        layout.addStretch(1)
        return host

    def set_params(self, params):
        values = prepare_params(params)
        for key, widget in self.controls.items():
            value = values[key]
            if isinstance(widget, QComboBox):
                index = widget.findData(value)
                if index >= 0:
                    widget.setCurrentIndex(index)
            elif isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            else:
                widget.setValue(value)
        self._update_enables()

    def params(self):
        values = {}
        for key, widget in self.controls.items():
            if isinstance(widget, QComboBox):
                values[key] = widget.currentData()
            elif isinstance(widget, QCheckBox):
                values[key] = widget.isChecked()
            else:
                values[key] = widget.value()
        return prepare_params(values)

    def _update_enables(self):
        method = self.controls["binarize_method"].currentData()
        self.controls["threshold"].setEnabled(method == "fixed")
        self.controls["adaptive_block"].setEnabled(method == "adaptive")
        self.controls["adaptive_c"].setEnabled(method == "adaptive")
        edge = self.controls["edge_mode"].currentData()
        self.controls["canny_low"].setEnabled(edge == "canny")
        self.controls["canny_high"].setEnabled(edge == "canny")
        subpixel = edge == "subpixel"
        self.controls["subpixel_search"].setEnabled(subpixel)
        self.controls["subpixel_angles"].setEnabled(subpixel)
        self.controls["min_gradient"].setEnabled(subpixel)


class CameraFramePump:
    """后台取帧。没取到新画面，或夹杂了空帧时，不覆盖上一张。"""

    def __init__(self, capture):
        self.capture = capture
        self._lock = threading.Lock()
        self._latest = None
        self._stop = threading.Event()
        self._stable_size = None
        self._pending_size = None
        self._pending_count = 0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        """幂等停止取帧，并先释放设备以唤醒可能阻塞的 read。"""
        self._stop.set()
        try:
            self.capture.release()
        except Exception:
            pass
        if threading.current_thread() is not self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def take(self):
        with self._lock:
            frame = self._latest
            self._latest = None
            return frame

    def _run(self):
        while not self._stop.is_set():
            ok, frame = self.capture.read()
            if not ok or frame is None or getattr(frame, "size", 0) == 0:
                self._stop.wait(0.01)
                continue
            if getattr(frame, "ndim", 0) != 3 or frame.shape[2] < 3:
                continue
            if int(frame.max()) < 2:
                continue
            size = (int(frame.shape[1]), int(frame.shape[0]))
            if self._stable_size is None:
                self._stable_size = size
            elif size != self._stable_size:
                if size == self._pending_size:
                    self._pending_count += 1
                else:
                    self._pending_size = size
                    self._pending_count = 1
                if self._pending_count < 3:
                    continue
                self._stable_size = size
                self._pending_size = None
                self._pending_count = 0
            else:
                self._pending_size = None
                self._pending_count = 0
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).copy()
            with self._lock:
                self._latest = rgb


@dataclass
class CircleMeasurement:
    center_x: float
    center_y: float
    radius_px: float
    mode: str
    points: list

    @property
    def diameter_px(self):
        return self.radius_px * 2.0

    def summary(self):
        return (
            f"圆心=({self.center_x:.2f}, {self.center_y:.2f}) px, "
            f"半径={self.radius_px:.2f} px, 直径={self.diameter_px:.2f} px"
        )


@dataclass
class ImportedImage:
    path: str
    circle: CircleMeasurement = None
    diameter_px: float = 0.0


@dataclass
class CircleSample:
    name: str
    source_path: str
    pixmap: QPixmap
    circle: CircleMeasurement
    diameter_px: float
    use_in_calc: bool = True


@dataclass
class ComparisonRecord:
    image_path: str
    label: str
    circle: CircleMeasurement
    is_baseline: bool = False
    dx_mm: float = 0.0
    dy_mm: float = 0.0
    total_mm: float = 0.0
    label_offset_x: float = 0.0
    label_offset_y: float = 0.0


def distance(p1, p2):
    return math.hypot(p1[0] - p2[0], p1[1] - p2[1])


def circle_from_two_points(p1, p2):
    radius = distance(p1, p2) / 2.0
    if radius <= 0:
        raise ValueError("两点重合，无法生成圆。")
    center_x = (p1[0] + p2[0]) / 2.0
    center_y = (p1[1] + p2[1]) / 2.0
    return center_x, center_y, radius


def circle_from_center_point(center, edge_point):
    radius = distance(center, edge_point)
    if radius <= 0:
        raise ValueError("圆心点与边缘点重合，无法生成圆。")
    return center[0], center[1], radius


def circle_from_three_points(p1, p2, p3):
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    det = 2.0 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(det) < 1e-6:
        raise ValueError("三点接近共线，无法生成圆。")

    ux = (
        (x1 * x1 + y1 * y1) * (y2 - y3)
        + (x2 * x2 + y2 * y2) * (y3 - y1)
        + (x3 * x3 + y3 * y3) * (y1 - y2)
    ) / det
    uy = (
        (x1 * x1 + y1 * y1) * (x3 - x2)
        + (x2 * x2 + y2 * y2) * (x1 - x3)
        + (x3 * x3 + y3 * y3) * (x2 - x1)
    ) / det
    radius = distance((ux, uy), p1)
    if radius <= 0:
        raise ValueError("生成的圆半径无效。")
    return ux, uy, radius


def load_image_pixmap(image_path):
    """加载图片为 QPixmap；QPixmap 失败时用 Pillow 兜底（兼容 exe 中 JPG 插件缺失）。"""
    pixmap = QPixmap(image_path)
    if not pixmap.isNull():
        return pixmap

    try:
        from PIL import Image

        with Image.open(image_path) as img:
            img = img.convert("RGBA")
            width, height = img.size
            qimage = QImage(
                img.tobytes("raw", "RGBA"),
                width,
                height,
                QImage.Format.Format_RGBA8888,
            )
            qimage = qimage.copy()
            pixmap = QPixmap.fromImage(qimage)
            if not pixmap.isNull():
                return pixmap
    except Exception:
        pass

    raise ValueError("图片加载失败，请确认格式是否受支持。")


class ImageCanvas(QWidget):
    circle_changed = pyqtSignal(object)
    status_changed = pyqtSignal(str)
    zoom_changed = pyqtSignal(float)
    drawing_state_changed = pyqtSignal(bool)
    zoom_request = pyqtSignal(float, object)
    roiChanged = pyqtSignal(int, int, int, int)  # ROI框选完成信号 (x, y, w, h)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pixmap = QPixmap()
        self.full_pixmap = QPixmap()
        self.view_origin_x = 0.0
        self.view_origin_y = 0.0
        self.circle_focused = False
        self.image_path = ""
        self.draw_mode = MODE_THREE_POINTS
        self.pending_points = []
        self.current_circle = None
        self.retained_circles = []
        self.overlay_items = []
        self.overlay_active = False
        self.preview_circle = None
        self.hover_point = None
        self.zoom_factor = 1.0
        self.drawing_enabled = False
        self.comparison_mode = False
        self.comparison_records = []
        self.dragging_label_index = None
        self.drag_offset_x = 0.0
        self.drag_offset_y = 0.0
        
        # ROI框选相关状态
        self.roi_enabled = False  # 是否启用ROI框选
        self.roi_selecting = False  # 是否正在框选ROI
        self.roi_start_pos = None  # ROI框选起始位置（图像坐标）
        self.roi_current_pos = None  # ROI框选当前位置（图像坐标）
        self.roi_rect = None  # 当前选中的ROI矩形(x, y, w, h)（图像坐标）
        
        self.setMouseTracking(True)
        self.setStyleSheet("background-color: #202020;")

    def sizeHint(self):
        if self.pixmap.isNull():
            return QSize(900, 700)
        return QSize(
            max(1, int(self.pixmap.width() * self.zoom_factor)),
            max(1, int(self.pixmap.height() * self.zoom_factor)),
        )

    def update_canvas_size(self):
        self.resize(self.sizeHint())
        self.updateGeometry()
        self.update()

    def set_image(self, image_path):
        pixmap = load_image_pixmap(image_path)
        self.full_pixmap = pixmap
        self.pixmap = pixmap
        self.view_origin_x = 0.0
        self.view_origin_y = 0.0
        self.circle_focused = False
        self.image_path = image_path
        self.comparison_mode = False
        self.comparison_records = []
        self.dragging_label_index = None
        self.pending_points = []
        self.current_circle = None
        self.retained_circles = []
        self.preview_circle = None
        self.hover_point = None
        # 重置ROI状态
        self.roi_selecting = False
        self.roi_start_pos = None
        self.roi_current_pos = None
        self.roi_rect = None
        self.update_canvas_size()
        self.drawing_state_changed.emit(self.drawing_enabled)
        self.circle_changed.emit(None)
        if self.drawing_enabled:
            self.status_changed.emit("图片已加载，当前为画圆状态，可直接在图片上标记。")
        else:
            self.status_changed.emit("图片已加载，点击“开始画圆”后再进行标记。")

    def accept_measurement(self, circle: CircleMeasurement):
        """提交外部圆测量，并复用与手动画圆相同的后续数据链。"""
        if circle is None or self.full_pixmap.isNull():
            return False
        self.show_full_image()
        self.pending_points = []
        self.preview_circle = None
        self.hover_point = None
        self.comparison_mode = False
        if self.drawing_enabled:
            self.drawing_enabled = False
            self.drawing_state_changed.emit(False)
        self.current_circle = circle
        self.retained_circles.append(circle)
        self.circle_changed.emit(circle)
        self.update()
        return True

    # ROI相关方法
    def set_roi_enabled(self, enabled):
        """启用或禁用ROI框选功能"""
        self.roi_enabled = enabled
        if not enabled:
            self.roi_selecting = False
            self.roi_start_pos = None
            self.roi_current_pos = None
            self.roi_rect = None
        self.update()

    def get_roi_rect(self):
        """获取ROI矩形（图像坐标），如果没有则返回None"""
        return self.roi_rect

    def set_roi_rect(self, rect):
        """设置ROI矩形（图像坐标）"""
        if rect is None:
            self.roi_rect = None
        else:
            x, y, w, h = rect
            # 确保矩形在图像范围内
            img_width = self.full_pixmap.width()
            img_height = self.full_pixmap.height()
            x = max(0, min(x, img_width - 1))
            y = max(0, min(y, img_height - 1))
            w = max(1, min(w, img_width - x))
            h = max(1, min(h, img_height - y))
            self.roi_rect = (x, y, w, h)
        self.update()

    def clear_roi(self):
        """清除ROI选择"""
        self.roi_selecting = False
        self.roi_start_pos = None
        self.roi_current_pos = None
        self.roi_rect = None
        self.update()

    def normalize_roi_rect(self, start_pos, current_pos):
        """将起始和当前位置转换为规范化矩形（图像坐标）"""
        if start_pos is None or current_pos is None:
            return None
        
        x1, y1 = start_pos
        x2, y2 = current_pos
        
        # 确保矩形有效
        x = min(x1, x2)
        y = min(y1, y2)
        w = max(1, abs(x2 - x1))
        h = max(1, abs(y2 - y1))
        
        # 限制在图像范围内
        img_width = self.full_pixmap.width()
        img_height = self.full_pixmap.height()
        x = max(0, min(x, img_width - 1))
        y = max(0, min(y, img_height - 1))
        w = min(w, img_width - x)
        h = min(h, img_height - y)
        
        return (x, y, w, h) if w > 0 and h > 0 else None

    def set_draw_mode(self, mode):
        self.draw_mode = mode
        self.pending_points = []
        self.current_circle = None
        self.preview_circle = None
        self.hover_point = None
        self.circle_changed.emit(None)
        self.status_changed.emit(
            self.mode_help_text() if self.drawing_enabled else f"已切换模式。{self.mode_help_text()}"
        )
        self.update()

    def clear_measurement(self):
        self.pending_points = []
        self.current_circle = None
        self.retained_circles = []
        self.overlay_items = []
        self.overlay_active = False
        self.preview_circle = None
        self.hover_point = None
        self.comparison_mode = False
        self.comparison_records = []
        self.dragging_label_index = None
        # 清除ROI状态
        self.roi_selecting = False
        self.roi_start_pos = None
        self.roi_current_pos = None
        self.roi_rect = None
        self.circle_changed.emit(None)
        self.status_changed.emit("已清除当前标定。")
        self.update()

    def update_live_frame(self, pixmap):
        """刷新实时画面，不清除已经画好的圆。"""
        if pixmap.isNull():
            return
        size_changed = (
            self.pixmap.isNull()
            or pixmap.width() != self.pixmap.width()
            or pixmap.height() != self.pixmap.height()
        )
        self.full_pixmap = pixmap
        self.pixmap = pixmap
        self.view_origin_x = 0.0
        self.view_origin_y = 0.0
        self.circle_focused = False
        self.image_path = "camera://live"
        self.comparison_mode = False
        self.comparison_records = []
        self.dragging_label_index = None
        if size_changed:
            self.update_canvas_size()
        else:
            self.update()

    def set_comparison_view(self, image_path, records):
        try:
            pixmap = load_image_pixmap(image_path)
        except ValueError as exc:
            raise ValueError("比对底图加载失败。") from exc
        self.pixmap = pixmap
        self.image_path = image_path
        self.comparison_mode = True
        self.comparison_records = list(records)
        self.dragging_label_index = None
        self.pending_points = []
        self.current_circle = None
        self.preview_circle = None
        self.hover_point = None
        self.drawing_enabled = False
        self.drawing_state_changed.emit(False)
        self.circle_changed.emit(None)
        self.update_canvas_size()

    def get_comparison_label_geometry(self, record):
        cx, cy = self.image_pos_to_widget_pos((record.circle.center_x, record.circle.center_y))
        radius = record.circle.radius_px * self.zoom_factor
        default_x = radius + 18
        default_y = -radius - 12
        title_text = f"{record.label}{'（标定图）' if record.is_baseline else ''}"
        body_lines = [
            f"x偏移: {record.dx_mm:.4f}",
            f"y偏移: {record.dy_mm:.4f}",
            f"总偏移: {record.total_mm:.4f}",
        ]
        metrics = QFontMetrics(self.font())
        title_width = metrics.horizontalAdvance(title_text)
        body_width = max(metrics.horizontalAdvance(line) for line in body_lines)
        label_width = max(title_width, body_width) + 20
        label_height = 22 + len(body_lines) * metrics.height() + 12
        text_x = int(cx + default_x + record.label_offset_x)
        text_y = int(cy + default_y + record.label_offset_y)
        rect_x = text_x - 8
        rect_y = text_y - 22
        return {
            "cx": cx,
            "cy": cy,
            "radius": radius,
            "text_x": text_x,
            "text_y": text_y,
            "rect_x": rect_x,
            "rect_y": rect_y,
            "label_width": label_width,
            "label_height": label_height,
            "title_text": title_text,
            "body_lines": body_lines,
            "line_height": metrics.height(),
        }

    def get_rect_anchor_point(self, rect_x, rect_y, width, height, target_x, target_y):
        anchor_x = min(max(target_x, rect_x), rect_x + width)
        anchor_y = min(max(target_y, rect_y), rect_y + height)
        return anchor_x, anchor_y

    def clamp_label_position(self, record, rect_x, rect_y, label_width, label_height):
        max_x = max(0, int(self.pixmap.width() * self.zoom_factor) - label_width)
        max_y = max(0, int(self.pixmap.height() * self.zoom_factor) - label_height)
        rect_x = min(max(0, rect_x), max_x)
        rect_y = min(max(0, rect_y), max_y)

        cx, cy = self.image_pos_to_widget_pos((record.circle.center_x, record.circle.center_y))
        radius = record.circle.radius_px * self.zoom_factor
        default_x = radius + 18
        default_y = -radius - 12
        text_x = rect_x + 8
        text_y = rect_y + 22
        record.label_offset_x = text_x - (cx + default_x)
        record.label_offset_y = text_y - (cy + default_y)
        return rect_x, rect_y

    def draw_arrow(self, painter, start_x, start_y, end_x, end_y, color):
        pen = QPen(color)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawLine(int(start_x), int(start_y), int(end_x), int(end_y))

        angle = math.atan2(end_y - start_y, end_x - start_x)
        arrow_size = 10
        left_x = end_x - arrow_size * math.cos(angle - math.pi / 6)
        left_y = end_y - arrow_size * math.sin(angle - math.pi / 6)
        right_x = end_x - arrow_size * math.cos(angle + math.pi / 6)
        right_y = end_y - arrow_size * math.sin(angle + math.pi / 6)
        painter.setBrush(color)
        painter.drawPolygon(
            QPoint(int(end_x), int(end_y)),
            QPoint(int(left_x), int(left_y)),
            QPoint(int(right_x), int(right_y)),
        )

    def hit_test_comparison_label(self, pos):
        for index in range(len(self.comparison_records) - 1, -1, -1):
            geometry = self.get_comparison_label_geometry(self.comparison_records[index])
            within_x = geometry["rect_x"] <= pos.x() <= geometry["rect_x"] + geometry["label_width"]
            within_y = geometry["rect_y"] <= pos.y() <= geometry["rect_y"] + geometry["label_height"]
            if within_x and within_y:
                return index, geometry
        return None, None

    def draw_comparison_overlay(self, painter):
        legend_top = 14
        legend_left = 14
        legend_width = 300
        legend_height = 22 + len(self.comparison_records) * 34
        painter.fillRect(legend_left - 8, legend_top - 10, legend_width, legend_height, QColor(0, 0, 0, 150))
        painter.setPen(QColor("#ffffff"))
        painter.drawText(legend_left, legend_top + 4, "颜色对应关系")

        colors = [
            "#ff4d4f",
            "#1677ff",
            "#52c41a",
            "#fa8c16",
            "#722ed1",
            "#13c2c2",
            "#eb2f96",
            "#a0d911",
        ]

        for index, record in enumerate(self.comparison_records):
            color = QColor(colors[index % len(colors)])
            circle_pen = QPen(color)
            circle_pen.setWidthF(2.0 if record.is_baseline else 1.4)
            circle_pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(circle_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            geometry = self.get_comparison_label_geometry(record)
            cx = geometry["cx"]
            cy = geometry["cy"]
            radius = geometry["radius"]
            painter.drawEllipse(
                int(cx - radius),
                int(cy - radius),
                int(radius * 2.0),
                int(radius * 2.0),
            )

            center_pen = QPen(color)
            center_pen.setWidthF(1.5)
            painter.setPen(center_pen)
            painter.setBrush(color)
            painter.drawEllipse(int(cx - 2), int(cy - 2), 4, 4)

            text_x = geometry["text_x"]
            text_y = geometry["text_y"]
            rect_x = geometry["rect_x"]
            rect_y = geometry["rect_y"]
            label_width = geometry["label_width"]
            label_height = geometry["label_height"]
            title_text = geometry["title_text"]
            body_lines = geometry["body_lines"]
            line_height = geometry["line_height"]

            anchor_x, anchor_y = self.get_rect_anchor_point(
                rect_x,
                rect_y,
                label_width,
                label_height,
                cx,
                cy,
            )
            circle_anchor_x = cx
            circle_anchor_y = cy
            if radius > 0:
                angle = math.atan2(anchor_y - cy, anchor_x - cx)
                circle_anchor_x = cx + radius * math.cos(angle)
                circle_anchor_y = cy + radius * math.sin(angle)

            self.draw_arrow(
                painter,
                anchor_x,
                anchor_y,
                circle_anchor_x,
                circle_anchor_y,
                color,
            )

            box_bg = QColor(0, 0, 0, 175)
            title_bg = QColor(0, 0, 0, 210)
            if self.dragging_label_index == index:
                box_bg = QColor(0, 0, 0, 205)
                title_bg = QColor(0, 0, 0, 225)
            painter.fillRect(rect_x, rect_y, label_width, label_height, box_bg)
            painter.fillRect(rect_x, rect_y, label_width, 22, title_bg)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(rect_x + 8, rect_y + 15, title_text)
            painter.setPen(color)
            painter.drawRect(rect_x, rect_y, label_width, label_height)
            painter.setPen(QColor("#ffffff"))
            for line_index, line_text in enumerate(body_lines):
                painter.drawText(text_x, rect_y + 38 + line_index * line_height, line_text)

            legend_y = legend_top + 24 + index * 34
            painter.fillRect(legend_left, legend_y - 12, 18, 18, color)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(
                legend_left + 28,
                legend_y + 2,
                f"{record.label}  X:{record.dx_mm:.4f}  Y:{record.dy_mm:.4f}  总:{record.total_mm:.4f}",
            )

    def set_drawing_enabled(self, enabled):
        if self.pixmap.isNull():
            self.drawing_enabled = False
            self.drawing_state_changed.emit(False)
            self.status_changed.emit("请先打开图片或相机，再开始画圆。")
            return False

        self.drawing_enabled = enabled
        self.pending_points = []
        self.preview_circle = None
        self.hover_point = None
        self.drawing_state_changed.emit(self.drawing_enabled)
        if enabled:
            self.show_full_image()
            self.current_circle = None
            self.circle_changed.emit(None)
            self.status_changed.emit(f"已进入画圆状态。{self.mode_help_text()}")
        else:
            self.status_changed.emit("已退出画圆状态。")
        self.update()
        return True

    def set_zoom(self, zoom_factor):
        zoom_factor = max(0.1, min(10.0, zoom_factor))
        if abs(self.zoom_factor - zoom_factor) < 1e-9:
            return
        self.zoom_factor = zoom_factor
        self.update_canvas_size()
        self.zoom_changed.emit(self.zoom_factor)

    def zoom_in(self):
        self.set_zoom(self.zoom_factor * 1.25)

    def zoom_out(self):
        self.set_zoom(self.zoom_factor / 1.25)

    def reset_zoom(self):
        self.set_zoom(1.0)

    def show_full_image(self):
        if self.full_pixmap.isNull():
            return
        self.pixmap = self.full_pixmap
        self.view_origin_x = 0.0
        self.view_origin_y = 0.0
        self.circle_focused = False
        self.update_canvas_size()

    def focus_on_circle(self, circle):
        """只保留圆心所在圆的像素，圆外内容不再显示。"""
        source = self.full_pixmap if not self.full_pixmap.isNull() else self.pixmap
        if source.isNull() or circle is None:
            return
        self.full_pixmap = source
        margin = 1
        x0 = max(0, int(math.floor(circle.center_x - circle.radius_px)) - margin)
        y0 = max(0, int(math.floor(circle.center_y - circle.radius_px)) - margin)
        x1 = min(source.width(), int(math.ceil(circle.center_x + circle.radius_px)) + margin)
        y1 = min(source.height(), int(math.ceil(circle.center_y + circle.radius_px)) + margin)
        self.view_origin_x = float(x0)
        self.view_origin_y = float(y0)
        self.pixmap = source.copy(x0, y0, max(1, x1 - x0), max(1, y1 - y0))
        self.circle_focused = True
        self.current_circle = circle
        self.retained_circles = [circle]
        self.comparison_mode = False
        self.update_canvas_size()

    def load_for_job(self, image_path, circle):
        pixmap = load_image_pixmap(image_path)
        self.full_pixmap = pixmap
        self.pixmap = pixmap
        self.image_path = image_path
        self.comparison_mode = False
        self.comparison_records = []
        self.dragging_label_index = None
        self.pending_points = []
        self.preview_circle = None
        self.hover_point = None
        self.view_origin_x = 0.0
        self.view_origin_y = 0.0
        self.circle_focused = False
        if circle is None:
            self.current_circle = None
            self.retained_circles = []
            self.update_canvas_size()
            self.circle_changed.emit(None)
            self.status_changed.emit("图片已加载，点击“开始画圆”后标记圆心圆。")
            return
        self.focus_on_circle(circle)
        self.circle_changed.emit(circle)
        self.status_changed.emit("已只显示该圆内的像素。")

    def widget_pos_to_image_pos(self, pos):
        if self.pixmap.isNull():
            return None
        x = pos.x() / self.zoom_factor + self.view_origin_x
        y = pos.y() / self.zoom_factor + self.view_origin_y
        if not (
            self.view_origin_x <= x < self.view_origin_x + self.pixmap.width()
            and self.view_origin_y <= y < self.view_origin_y + self.pixmap.height()
        ):
            return None
        return float(x), float(y)

    def image_pos_to_widget_pos(self, point):
        return (
            (point[0] - self.view_origin_x) * self.zoom_factor,
            (point[1] - self.view_origin_y) * self.zoom_factor,
        )

    def update_preview_circle(self):
        self.preview_circle = None
        if self.hover_point is None:
            return

        try:
            if self.draw_mode == MODE_THREE_POINTS and len(self.pending_points) == 2:
                center_x, center_y, radius = circle_from_three_points(
                    self.pending_points[0], self.pending_points[1], self.hover_point
                )
            elif self.draw_mode == MODE_TWO_POINTS and len(self.pending_points) == 1:
                center_x, center_y, radius = circle_from_two_points(
                    self.pending_points[0], self.hover_point
                )
            elif self.draw_mode == MODE_CENTER_POINT and len(self.pending_points) == 1:
                center_x, center_y, radius = circle_from_center_point(
                    self.pending_points[0], self.hover_point
                )
            else:
                return
        except ValueError:
            return

        self.preview_circle = CircleMeasurement(
            center_x=center_x,
            center_y=center_y,
            radius_px=radius,
            mode=self.draw_mode,
            points=list(self.pending_points) + [self.hover_point],
        )

    def mode_help_text(self):
        if self.draw_mode == MODE_THREE_POINTS:
            return "当前模式：三点画圆。请依次点击圆上的 3 个点。"
        if self.draw_mode == MODE_TWO_POINTS:
            return "当前模式：两点画圆。请点击圆的直径两端。"
        return "当前模式：圆心画圆。请先点圆心，再点圆边。"

    def mousePressEvent(self, event):
        if self.pixmap.isNull():
            return
        
        # 比对模式下处理标签拖动
        if self.comparison_mode:
            if event.button() == Qt.MouseButton.LeftButton:
                label_index, geometry = self.hit_test_comparison_label(event.position().toPoint())
                if label_index is not None:
                    self.dragging_label_index = label_index
                    self.drag_offset_x = event.position().toPoint().x() - geometry["rect_x"]
                    self.drag_offset_y = event.position().toPoint().y() - geometry["rect_y"]
                    self.status_changed.emit("已选中比对数据框，可拖动到合适位置。")
                    self.update()
            return
        
        # ROI模式下处理框选
        if self.roi_enabled and event.button() == Qt.MouseButton.LeftButton:
            image_pos = self.widget_pos_to_image_pos(event.position().toPoint())
            if image_pos is None:
                return
            
            self.roi_selecting = True
            self.roi_start_pos = image_pos
            self.roi_current_pos = image_pos
            self.status_changed.emit("开始拖拽框选ROI区域，释放鼠标完成选择。")
            self.update()
            return
        
        # 画圆模式下处理画点
        if not self.drawing_enabled:
            return

        if event.button() == Qt.MouseButton.RightButton:
            if self.pending_points:
                self.pending_points.pop()
                self.preview_circle = None
                self.status_changed.emit("已撤销上一个点。")
                self.update_preview_circle()
                self.update()
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        image_pos = self.widget_pos_to_image_pos(event.position().toPoint())
        if image_pos is None:
            return

        if self.current_circle is not None and not self.pending_points:
            self.current_circle = None
            self.preview_circle = None
            self.circle_changed.emit(None)

        self.pending_points.append(image_pos)
        required = MODE_POINT_COUNT[self.draw_mode]
        if len(self.pending_points) < required:
            left = required - len(self.pending_points)
            self.update_preview_circle()
            self.status_changed.emit(f"已记录 {len(self.pending_points)} 个点，还需要 {left} 个点。")
            self.update()
            return

        try:
            if self.draw_mode == MODE_THREE_POINTS:
                center_x, center_y, radius = circle_from_three_points(*self.pending_points)
            elif self.draw_mode == MODE_TWO_POINTS:
                center_x, center_y, radius = circle_from_two_points(*self.pending_points)
            else:
                center_x, center_y, radius = circle_from_center_point(*self.pending_points)
        except ValueError as exc:
            self.pending_points = []
            self.current_circle = None
            self.circle_changed.emit(None)
            self.status_changed.emit(str(exc))
            QMessageBox.warning(self, "画圆失败", str(exc))
            self.update()
            return

        self.current_circle = CircleMeasurement(
            center_x=center_x,
            center_y=center_y,
            radius_px=radius,
            mode=self.draw_mode,
            points=list(self.pending_points),
        )
        self.retained_circles.append(self.current_circle)
        self.pending_points = []
        self.preview_circle = None
        self.circle_changed.emit(self.current_circle)
        self.status_changed.emit(
            f"标定完成并已保留（共 {len(self.retained_circles)} 个）：{self.current_circle.summary()}"
        )
        self.update()

    def mouseMoveEvent(self, event):
        if self.pixmap.isNull():
            return
        
        # 比对模式下处理标签拖动
        if self.comparison_mode:
            if self.dragging_label_index is not None:
                record = self.comparison_records[self.dragging_label_index]
                geometry = self.get_comparison_label_geometry(record)
                rect_x = event.position().toPoint().x() - self.drag_offset_x
                rect_y = event.position().toPoint().y() - self.drag_offset_y
                self.clamp_label_position(
                    record,
                    rect_x,
                    rect_y,
                    geometry["label_width"],
                    geometry["label_height"],
                )
                self.update()
                return
            hover_index, _ = self.hit_test_comparison_label(event.position().toPoint())
            if hover_index is not None:
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            else:
                self.unsetCursor()
            return
        
        # ROI模式下处理框选拖动
        if self.roi_enabled and self.roi_selecting:
            image_pos = self.widget_pos_to_image_pos(event.position().toPoint())
            if image_pos is not None:
                self.roi_current_pos = image_pos
                self.update()
            return
        
        # 画圆模式下处理预览
        self.hover_point = self.widget_pos_to_image_pos(event.position().toPoint())
        self.update_preview_circle()
        if self.pending_points:
            self.update()

    def mouseReleaseEvent(self, event):
        # 比对模式下处理标签拖动结束
        if self.comparison_mode and event.button() == Qt.MouseButton.LeftButton:
            if self.dragging_label_index is not None:
                self.dragging_label_index = None
                self.setCursor(Qt.CursorShape.ArrowCursor)
                self.status_changed.emit("比对数据框位置已更新。")
                self.update()
            return
        
        # ROI模式下处理框选结束
        if self.roi_enabled and event.button() == Qt.MouseButton.LeftButton and self.roi_selecting:
            image_pos = self.widget_pos_to_image_pos(event.position().toPoint())
            if image_pos is not None and self.roi_start_pos is not None:
                self.roi_current_pos = image_pos
                rect = self.normalize_roi_rect(self.roi_start_pos, self.roi_current_pos)
                if rect:
                    self.roi_rect = rect
                    x, y, w, h = rect
                    self.status_changed.emit(f"已框选ROI区域：({x}, {y}), 尺寸：{w}×{h} 像素")
                    # 发射ROI框选完成信号
                    self.roiChanged.emit(x, y, w, h)
                else:
                    self.roi_rect = None
                    self.status_changed.emit("ROI选择无效，已清除")
            
            self.roi_selecting = False
            self.roi_start_pos = None
            self.roi_current_pos = None
            self.update()
            return
        
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        self.hover_point = None
        self.preview_circle = None
        if self.comparison_mode and self.dragging_label_index is None:
            self.unsetCursor()
        if self.pending_points:
            self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            scale_factor = 1.25 if event.angleDelta().y() > 0 else 1 / 1.25
            self.zoom_request.emit(scale_factor, event.position().toPoint())
            event.accept()
            return
        super().wheelEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#202020"))

        if self.pixmap.isNull():
            painter.setPen(QColor("#d0d0d0"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "请先打开一张图片，或点击“打开相机”")
            return

        target_width = int(self.pixmap.width() * self.zoom_factor)
        target_height = int(self.pixmap.height() * self.zoom_factor)
        painter.drawPixmap(0, 0, target_width, target_height, self.pixmap)

        if self.comparison_mode:
            self.draw_comparison_overlay(painter)
            return

        point_pen = QPen(QColor("#ffd24d"))
        point_pen.setWidth(8)
        painter.setPen(point_pen)
        for point in self.pending_points:
            x, y = self.image_pos_to_widget_pos(point)
            painter.drawPoint(int(x), int(y))

        if len(self.pending_points) >= 2:
            helper_pen = QPen(QColor("#66d9ef"))
            helper_pen.setWidth(2)
            painter.setPen(helper_pen)
            for index in range(len(self.pending_points) - 1):
                p1 = self.pending_points[index]
                p2 = self.pending_points[index + 1]
                x1, y1 = self.image_pos_to_widget_pos(p1)
                x2, y2 = self.image_pos_to_widget_pos(p2)
                painter.drawLine(int(x1), int(y1), int(x2), int(y2))

        if self.pending_points and self.hover_point is not None:
            hover_pen = QPen(QColor("#9cdcfe"))
            hover_pen.setWidth(6)
            painter.setPen(hover_pen)
            hx, hy = self.image_pos_to_widget_pos(self.hover_point)
            painter.drawPoint(int(hx), int(hy))

        circle_colors = [
            "#ff4d4f",
            "#1677ff",
            "#52c41a",
            "#fa8c16",
            "#722ed1",
            "#13c2c2",
            "#eb2f96",
            "#a0d911",
        ]
        if self.overlay_active:
            for circle, color, label, selected in self.overlay_items:
                self.draw_measurement_circle(painter, circle, color, label, selected)
        else:
            for index, circle in enumerate(self.retained_circles):
                self.draw_measurement_circle(
                    painter,
                    circle,
                    QColor(circle_colors[index % len(circle_colors)]),
                    str(index + 1),
                    False,
                )

        if self.preview_circle is not None:
            preview_pen = QPen(QColor("#ffd166"))
            preview_pen.setWidth(2)
            preview_pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(preview_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            pcx, pcy = self.image_pos_to_widget_pos(
                (self.preview_circle.center_x, self.preview_circle.center_y)
            )
            preview_radius = self.preview_circle.radius_px * self.zoom_factor
            painter.drawEllipse(
                int(pcx - preview_radius),
                int(pcy - preview_radius),
                int(preview_radius * 2.0),
                int(preview_radius * 2.0),
            )

        # 绘制ROI矩形
        if self.roi_enabled:
            # 绘制当前正在选择的ROI矩形
            if self.roi_selecting and self.roi_start_pos is not None and self.roi_current_pos is not None:
                rect = self.normalize_roi_rect(self.roi_start_pos, self.roi_current_pos)
                if rect:
                    x, y, w, h = rect
                    x_widget, y_widget = self.image_pos_to_widget_pos((x, y))
                    w_widget = w * self.zoom_factor
                    h_widget = h * self.zoom_factor
                    
                    # 绘制半透明填充
                    roi_fill_pen = QPen(Qt.PenStyle.NoPen)
                    painter.setPen(roi_fill_pen)
                    painter.setBrush(QColor(0, 100, 255, 30))  # 浅蓝色半透明填充
                    painter.drawRect(int(x_widget), int(y_widget), int(w_widget), int(h_widget))
                    
                    # 绘制边框
                    roi_border_pen = QPen(QColor(0, 150, 255))
                    roi_border_pen.setWidth(2)
                    roi_border_pen.setStyle(Qt.PenStyle.DashLine)
                    painter.setPen(roi_border_pen)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRect(int(x_widget), int(y_widget), int(w_widget), int(h_widget))
            
            # 绘制已确定的ROI矩形
            if self.roi_rect is not None and not self.roi_selecting:
                x, y, w, h = self.roi_rect
                x_widget, y_widget = self.image_pos_to_widget_pos((x, y))
                w_widget = w * self.zoom_factor
                h_widget = h * self.zoom_factor
                
                # 绘制半透明填充
                roi_fill_pen = QPen(Qt.PenStyle.NoPen)
                painter.setPen(roi_fill_pen)
                painter.setBrush(QColor(100, 200, 100, 40))  # 浅绿色半透明填充
                painter.drawRect(int(x_widget), int(y_widget), int(w_widget), int(h_widget))
                
                # 绘制边框
                roi_border_pen = QPen(QColor(50, 200, 50))
                roi_border_pen.setWidth(3)
                painter.setPen(roi_border_pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(int(x_widget), int(y_widget), int(w_widget), int(h_widget))
                
                # 绘制ROI文本标签
                painter.setPen(QColor("#ffffff"))
                painter.drawText(
                    int(x_widget + 5),
                    int(y_widget + 20),
                    f"ROI: {int(x)}, {int(y)}, {int(w)}×{int(h)}"
                )

    def draw_measurement_circle(self, painter, circle, color, index=None, selected=False):
        circle_pen = QPen(color)
        circle_pen.setWidth(2)
        painter.setPen(circle_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        cx, cy = self.image_pos_to_widget_pos((circle.center_x, circle.center_y))
        radius = circle.radius_px * self.zoom_factor
        painter.drawEllipse(
            int(cx - radius),
            int(cy - radius),
            int(radius * 2.0),
            int(radius * 2.0),
        )

        center_pen = QPen(QColor("#00ff7f"))
        center_pen.setWidth(2)
        painter.setPen(center_pen)
        painter.drawLine(int(cx - 6), int(cy), int(cx + 6), int(cy))
        painter.drawLine(int(cx), int(cy - 6), int(cx), int(cy + 6))

        if index is not None:
            painter.setPen(color)
            painter.drawText(int(cx + 8), int(cy - 8), str(index))

        marker_pen = QPen(color)
        marker_pen.setWidth(2)
        marker_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(marker_pen)
        for point in circle.points:
            x, y = self.image_pos_to_widget_pos(point)
            painter.drawPoint(int(x), int(y))


class ComparisonCanvas(QWidget):
    COLORS = [
        "#ff4d4f",
        "#1677ff",
        "#52c41a",
        "#fa8c16",
        "#722ed1",
        "#13c2c2",
        "#eb2f96",
        "#a0d911",
    ]

    def __init__(self, pixmap, records, parent=None):
        super().__init__(parent)
        self.pixmap = pixmap
        self.records = records
        self.setStyleSheet("background-color: #202020;")
        if not self.pixmap.isNull():
            self.resize(self.pixmap.size())

    def set_content(self, pixmap, records):
        self.pixmap = pixmap
        self.records = records
        if not self.pixmap.isNull():
            self.resize(self.pixmap.size())
        else:
            self.resize(self.sizeHint())
        self.update()

    def sizeHint(self):
        if self.pixmap.isNull():
            return QSize(1000, 800)
        return self.pixmap.size()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#202020"))

        if self.pixmap.isNull():
            painter.setPen(QColor("#d0d0d0"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "比对图片加载失败")
            return

        painter.drawPixmap(0, 0, self.pixmap)
        legend_top = 14
        legend_left = 14
        legend_width = 300
        legend_height = 22 + len(self.records) * 34
        painter.fillRect(legend_left - 8, legend_top - 10, legend_width, legend_height, QColor(0, 0, 0, 150))
        painter.setPen(QColor("#ffffff"))
        painter.drawText(legend_left, legend_top + 4, "颜色对应关系")

        for index, record in enumerate(self.records):
            color = QColor(self.COLORS[index % len(self.COLORS)])
            circle_pen = QPen(color)
            circle_pen.setWidthF(2.0 if record.is_baseline else 1.4)
            circle_pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(circle_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(
                int(record.circle.center_x - record.circle.radius_px),
                int(record.circle.center_y - record.circle.radius_px),
                int(record.circle.diameter_px),
                int(record.circle.diameter_px),
            )

            center_pen = QPen(color)
            center_pen.setWidthF(2.0)
            painter.setPen(center_pen)
            painter.drawEllipse(
                int(record.circle.center_x - 4),
                int(record.circle.center_y - 4),
                8,
                8,
            )

            label_text = (
                f"{record.label}{'（标定图）' if record.is_baseline else ''}\n"
                f"X: {record.dx_mm:.4f}\n"
                f"Y: {record.dy_mm:.4f}\n"
                f"总: {record.total_mm:.4f}"
            )
            text_x = int(record.circle.center_x + record.circle.radius_px + 18)
            text_y = int(record.circle.center_y - record.circle.radius_px - 12)
            label_width = 145
            label_height = 74

            painter.setPen(QPen(color, 2))
            painter.drawLine(
                int(record.circle.center_x + record.circle.radius_px * 0.75),
                int(record.circle.center_y - record.circle.radius_px * 0.75),
                text_x - 8,
                text_y - 8,
            )
            painter.fillRect(text_x - 8, text_y - 22, label_width, label_height, QColor(0, 0, 0, 175))
            painter.setPen(QColor("#ffffff"))
            painter.drawText(text_x, text_y - 2, label_text)

            legend_y = legend_top + 24 + index * 34
            painter.fillRect(legend_left, legend_y - 12, 18, 18, color)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(
                legend_left + 28,
                legend_y + 2,
                f"{record.label}  X:{record.dx_mm:.4f}  Y:{record.dy_mm:.4f}  总:{record.total_mm:.4f}",
            )


class NozzleOffsetTool(QWidget):
    """可嵌入宿主页面的喷嘴圆心偏移标定工具。"""

    def __init__(self, context=None, parent=None):
        super().__init__(parent)
        self.context = context
        self._active = False
        self._shutdown = False
        self.setObjectName("nozzleOffsetPlugin")

        self.current_image_path = ""
        self.current_circle = None
        self.baseline_image_path = ""
        self.baseline_circle = None
        self.comparison_records = []
        self.comparison_visible = False
        self.baseline_result_written = False
        self.output_session_stamp = ""
        self.image_jobs = []
        self.current_job_index = -1
        self.primary_index = -1
        self.circle_samples = []
        self.current_sample_index = -1
        self.import_queue = []
        self.frame_frozen = False
        self._loading_sample = False
        self.camera_capture = None
        self.frame_pump = None
        self._running_camera_format = None
        self._last_format_notice = (
            DEFAULT_CAMERA_WIDTH,
            DEFAULT_CAMERA_HEIGHT,
            float(DEFAULT_CAMERA_FPS),
        )
        self.camera_timer = QTimer(self)
        self.camera_timer.timeout.connect(self.grab_camera_frame)

        self.auto_config_path = _resolve_auto_config_path(self.context)
        self.auto_params = default_params()
        self.auto_worker = None
        self._detect_generation = 0
        self._detect_snapshot = None
        self._auto_config_load_error = ""

        self.default_output_dir = os.path.join(_plugin_data_root(self.context), "outputs")
        self.output_dir = self.default_output_dir
        self.output_dir_is_custom = False

        self.canvas = ImageCanvas(self)
        self.canvas.circle_changed.connect(self.on_circle_changed)
        self.canvas.status_changed.connect(self.set_status)
        self.canvas.zoom_changed.connect(self.on_zoom_changed)
        self.canvas.drawing_state_changed.connect(self.on_drawing_state_changed)
        self.canvas.zoom_request.connect(self.handle_zoom_request)

        self.open_button = QPushButton("导入图片")
        self.open_button.clicked.connect(self.open_image)

        self.image_list = QListWidget()
        self.image_list.setMinimumHeight(140)
        self.image_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.image_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.image_list.setToolTip("勾选显示圆，取消勾选会去掉画面上的痕迹。双击或右键可修改名称。")
        self.image_list.currentRowChanged.connect(self.on_circle_sample_selected)
        self.image_list.itemChanged.connect(self.on_circle_check_changed)
        self.image_list.itemDoubleClicked.connect(self.rename_circle_item)
        self.image_list.customContextMenuRequested.connect(self.show_circle_menu)

        self.freeze_button = QPushButton("固定当前帧")
        self.freeze_button.setCheckable(True)
        self.freeze_button.clicked.connect(self.toggle_freeze)

        self.camera_button = QPushButton("打开相机")
        self.camera_button.setCheckable(True)
        self.camera_button.clicked.connect(self.toggle_camera)

        self.camera_combo = QComboBox()
        for camera_index in range(4):
            self.camera_combo.addItem(f"相机 {camera_index}", camera_index)
        self.camera_combo.currentIndexChanged.connect(self.on_camera_changed)

        self.refresh_camera_button = QPushButton("刷新")
        self.refresh_camera_button.clicked.connect(self.refresh_camera_list)

        self.camera_width_edit = QLineEdit(str(DEFAULT_CAMERA_WIDTH))
        self.camera_width_edit.setValidator(QIntValidator(2, 10000, self))
        self.camera_width_edit.setPlaceholderText("宽")
        self.camera_width_edit.editingFinished.connect(self.on_camera_format_edited)

        self.camera_height_edit = QLineEdit(str(DEFAULT_CAMERA_HEIGHT))
        self.camera_height_edit.setValidator(QIntValidator(2, 10000, self))
        self.camera_height_edit.setPlaceholderText("高")
        self.camera_height_edit.editingFinished.connect(self.on_camera_format_edited)

        self.camera_fps_edit = QLineEdit(str(DEFAULT_CAMERA_FPS))
        self.camera_fps_edit.setValidator(QDoubleValidator(0.1, 240.0, 2, self))
        self.camera_fps_edit.setPlaceholderText("帧率")
        self.camera_fps_edit.editingFinished.connect(self.on_camera_format_edited)

        self.location_mode_combo = QComboBox()
        self.location_mode_combo.addItem("手动画圆", "manual")
        self.location_mode_combo.addItem("自动找圆心", "auto")
        self.location_mode_combo.currentIndexChanged.connect(self.on_location_mode_changed)

        self.start_draw_button = QPushButton("开始画圆")
        self.start_draw_button.setCheckable(True)
        self.start_draw_button.clicked.connect(self.toggle_drawing)

        self.auto_detect_button = QPushButton("自动检测当前画面")
        self.auto_detect_button.setObjectName("primary")
        self.auto_detect_button.clicked.connect(self.start_auto_detection)

        self.auto_params_button = QPushButton("自动参数")
        self.auto_params_button.clicked.connect(self.show_auto_params_dialog)

        # ROI控制按钮
        self.roi_select_button = QPushButton("框选 ROI")
        self.roi_select_button.setToolTip("启用ROI框选模式，在图像上拖拽选择检测区域")
        self.roi_select_button.setCheckable(True)
        
        self.roi_clear_button = QPushButton("清除ROI/全图")
        self.roi_clear_button.setToolTip("清除ROI选择，使用全图进行检测")
        self.roi_clear_button.clicked.connect(self.clear_roi)

        self.zoom_in_button = QPushButton("放大")
        self.zoom_in_button.clicked.connect(self.zoom_in_view)

        self.zoom_out_button = QPushButton("缩小")
        self.zoom_out_button.clicked.connect(self.zoom_out_view)

        self.reset_zoom_button = QPushButton("恢复初始视图")
        self.reset_zoom_button.clicked.connect(self.reset_zoom_view)

        self.clear_button = QPushButton("清除标定点")
        self.clear_button.clicked.connect(self.clear_all_measurements)

        self.delete_button = QPushButton("删除选中")
        self.delete_button.clicked.connect(self.delete_selected_samples)

        self.save_frames_button = QPushButton("保存选中帧")
        self.save_frames_button.clicked.connect(self.save_selected_frames)

        self.baseline_button = QPushButton("设为标定圆")
        self.baseline_button.clicked.connect(self.set_baseline)

        self.calculate_button = QPushButton("计算偏移并写入结果")
        self.calculate_button.clicked.connect(self.calculate_offset)

        self.compare_button = QPushButton("偏移图片比对")
        self.compare_button.clicked.connect(self.show_comparison)

        self.open_result_file_button = QPushButton("打开偏移值文件")
        self.open_result_file_button.clicked.connect(self.open_result_file)

        self.draw_mode_combo = QComboBox()
        self.draw_mode_combo.addItems([
            MODE_THREE_POINTS,
            MODE_TWO_POINTS,
            MODE_CENTER_POINT,
        ])
        self.draw_mode_combo.currentTextChanged.connect(self.change_draw_mode)

        self.real_diameter_edit = QLineEdit("0.4")
        self.real_diameter_edit.setValidator(QDoubleValidator(0.000001, 999999.0, 6, self))
        self.real_diameter_edit.editingFinished.connect(lambda: self.refresh_offsets(write_file=False))

        self.diameter_px_edit = QLineEdit("")
        self.diameter_px_edit.setValidator(QDoubleValidator(0.000001, 999999.0, 3, self))
        self.diameter_px_edit.setPlaceholderText("画圆后自动填入，可修改")
        self.diameter_px_edit.editingFinished.connect(self.apply_diameter_px)

        self.output_dir_edit = QLineEdit(self.output_dir)
        self.output_dir_edit.editingFinished.connect(self.apply_output_dir_text)

        self.choose_dir_button = QPushButton("选择目录")
        self.choose_dir_button.clicked.connect(self.choose_output_dir)

        self.image_label = QLabel("当前图片：未打开")
        self.image_label.setWordWrap(True)
        self.zoom_label = QLabel("当前缩放：100%")
        self.drawing_state_label = QLabel("画圆状态：未开始")
        self.current_circle_label = QLabel("当前标定：未完成")
        self.current_circle_label.setWordWrap(True)
        self.baseline_label = QLabel("初始位置：未设置")
        self.baseline_label.setWordWrap(True)
        self.result_label = QPlainTextEdit()
        self.result_label.setReadOnly(True)
        self.result_label.setPlainText("结果：等待计算")
        self.result_label.setMinimumHeight(160)
        self.result_label.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.status_label = QLabel(self.canvas.mode_help_text())
        self.status_label.setWordWrap(True)

        self.apply_button_styles()
        self.apply_field_styles()
        self.build_ui()
        self._load_auto_config()
        self.change_draw_mode(self.draw_mode_combo.currentText())
        self.on_drawing_state_changed(False)
        self.on_location_mode_changed()

    def apply_button_styles(self):
        primary_buttons = [
            self.open_button,
            self.camera_button,
            self.freeze_button,
            self.start_draw_button,
            self.auto_detect_button,
            self.auto_params_button,
            self.roi_select_button,
            self.roi_clear_button,
            self.clear_button,
            self.delete_button,
            self.save_frames_button,
            self.baseline_button,
            self.calculate_button,
            self.compare_button,
            self.open_result_file_button,
            self.choose_dir_button,
        ]
        zoom_buttons = [
            self.zoom_in_button,
            self.zoom_out_button,
            self.reset_zoom_button,
        ]

        for button in primary_buttons:
            button.setMinimumHeight(28)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        for button in zoom_buttons:
            button.setMinimumHeight(28)
            button.setMinimumWidth(74)

        self.calculate_button.setObjectName("primary")
        self.clear_button.setObjectName("danger")
        self.delete_button.setObjectName("danger")
        self.refresh_camera_button.setMinimumHeight(28)

    def apply_field_styles(self):
        for label in (self.image_label, self.current_circle_label, self.baseline_label):
            label.setObjectName("infoLabel")
        for label in (self.zoom_label, self.drawing_state_label):
            label.setObjectName("strongLabel")
        self.result_label.setObjectName("resultBox")
        self.status_label.setObjectName("statusHint")
        self._set_result_state("neutral")

    def _set_result_state(self, state):
        if self.result_label.property("resultState") == state:
            return
        self.result_label.setProperty("resultState", state)
        style = self.result_label.style()
        style.unpolish(self.result_label)
        style.polish(self.result_label)
        self.result_label.update()

    def build_ui(self):
        source_group = QGroupBox("画面")
        source_layout = QVBoxLayout()
        source_layout.setSpacing(8)
        source_row = QHBoxLayout()
        source_row.addWidget(self.open_button)
        source_row.addWidget(self.camera_button)
        source_layout.addLayout(source_row)
        camera_row = QHBoxLayout()
        camera_row.addWidget(self.camera_combo, 1)
        camera_row.addWidget(self.refresh_camera_button)
        source_layout.addLayout(camera_row)
        format_row = QHBoxLayout()
        format_row.addWidget(QLabel("清晰度"))
        format_row.addWidget(self.camera_width_edit)
        format_row.addWidget(QLabel("×"))
        format_row.addWidget(self.camera_height_edit)
        source_layout.addLayout(format_row)
        fps_row = QHBoxLayout()
        fps_row.addWidget(QLabel("帧率"))
        fps_row.addWidget(self.camera_fps_edit)
        source_layout.addLayout(fps_row)
        locate_row = QHBoxLayout()
        locate_row.addWidget(QLabel("定位方式"))
        locate_row.addWidget(self.location_mode_combo, 1)
        source_layout.addLayout(locate_row)
        tool_row = QHBoxLayout()
        tool_row.addWidget(self.freeze_button)
        tool_row.addWidget(self.start_draw_button)
        source_layout.addLayout(tool_row)
        source_layout.addWidget(self.draw_mode_combo)
        auto_row = QHBoxLayout()
        auto_row.addWidget(self.auto_detect_button, 2)
        auto_row.addWidget(self.auto_params_button, 1)
        auto_row.addWidget(self.roi_select_button, 1)
        auto_row.addWidget(self.roi_clear_button, 1)
        source_layout.addLayout(auto_row)
        source_group.setLayout(source_layout)

        circle_group = QGroupBox("圆列表")
        circle_layout = QVBoxLayout()
        self.image_list.setMinimumHeight(160)
        self.image_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        circle_layout.addWidget(self.image_list)
        circle_action_row = QHBoxLayout()
        circle_action_row.addWidget(self.baseline_button)
        circle_action_row.addWidget(self.delete_button)
        circle_layout.addLayout(circle_action_row)
        circle_file_row = QHBoxLayout()
        circle_file_row.addWidget(self.save_frames_button)
        circle_file_row.addWidget(self.clear_button)
        circle_layout.addLayout(circle_file_row)
        circle_group.setLayout(circle_layout)

        setting_group = QGroupBox("参数")
        setting_layout = QFormLayout()
        setting_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        setting_layout.addRow("实际直径(mm)：", self.real_diameter_edit)
        setting_layout.addRow("直径像素：", self.diameter_px_edit)
        setting_group.setLayout(setting_layout)

        output_group = QGroupBox("结果")
        output_layout = QVBoxLayout()
        self.result_label.setMinimumHeight(96)
        self.result_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        output_layout.addWidget(self.result_label)
        output_layout.addWidget(self.calculate_button)
        output_layout.addWidget(self.status_label)
        output_group.setLayout(output_layout)

        file_group = QGroupBox("文件")
        file_layout = QVBoxLayout()
        file_layout.addWidget(self.output_dir_edit)
        file_row = QHBoxLayout()
        file_row.addWidget(self.choose_dir_button)
        file_row.addWidget(self.compare_button)
        file_row.addWidget(self.open_result_file_button)
        file_layout.addLayout(file_row)
        file_group.setLayout(file_layout)

        left_layout = QVBoxLayout()
        left_layout.setContentsMargins(4, 4, 4, 4)
        left_layout.setSpacing(8)
        left_layout.addWidget(source_group)
        left_layout.addWidget(circle_group, 1)
        left_layout.addWidget(setting_group)
        left_layout.addWidget(output_group)
        left_layout.addWidget(file_group)

        left_panel = QWidget()
        left_panel.setLayout(left_layout)
        left_panel.setMinimumWidth(310)

        self.left_scroll = QScrollArea()
        self.left_scroll.setWidgetResizable(True)
        self.left_scroll.setWidget(left_panel)
        self.left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.left_scroll.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
        self.left_scroll.setMinimumWidth(330)
        self.left_scroll.setMaximumWidth(500)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidget(self.canvas)
        self.scroll_area.setWidgetResizable(False)
        self.scroll_area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll_area.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
        self.scroll_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.scroll_area.setFrameShape(QFrame.Shape.StyledPanel)

        zoom_bar = QHBoxLayout()
        zoom_bar.setContentsMargins(8, 0, 8, 0)
        zoom_bar.addWidget(self.zoom_label)
        zoom_bar.addStretch(1)
        zoom_bar.addWidget(self.zoom_out_button)
        zoom_bar.addWidget(self.zoom_in_button)
        zoom_bar.addWidget(self.reset_zoom_button)

        right_layout = QVBoxLayout()
        right_layout.setContentsMargins(0, 8, 8, 8)
        right_layout.setSpacing(8)
        right_layout.addWidget(self.scroll_area, 1)
        right_layout.addLayout(zoom_bar)

        right_panel = QWidget()
        right_panel.setLayout(right_layout)
        right_panel.setMinimumWidth(420)
        right_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.main_splitter.setObjectName("nozzleMainSplitter")
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.addWidget(self.left_scroll)
        self.main_splitter.addWidget(right_panel)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setCollapsible(0, False)
        self.main_splitter.setCollapsible(1, False)
        self.main_splitter.setSizes([370, 1030])

        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(self.main_splitter)
        self.setLayout(main_layout)
        
        # 连接信号
        self.roi_select_button.toggled.connect(self.toggle_roi_selection)
        self.canvas.roiChanged.connect(self.on_roi_drawn)

    def set_status(self, text):
        message = str(text)
        self.status_label.setText(f"提示：{message}")
        callback = _context_member(self.context, "show_status") or _context_member(
            self.context, "post_status"
        )
        if callable(callback):
            try:
                callback(message)
            except Exception:
                pass

    def _load_auto_config(self):
        self.auto_params = default_params()
        if not os.path.isfile(self.auto_config_path):
            return
        try:
            with open(self.auto_config_path, "r", encoding="utf-8") as file:
                document = json.load(file)
            if not isinstance(document, dict):
                raise ValueError("配置文件根节点不是对象")
            raw = document.get("params", document)
            if not isinstance(raw, dict):
                raise ValueError("配置中没有参数对象")
            self.auto_params = prepare_params(raw)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self._auto_config_load_error = str(exc)
            self.auto_params = default_params()
            self.set_status("自动检测配置损坏，已安全使用默认参数。")

    def _save_auto_config(self):
        directory = os.path.dirname(self.auto_config_path)
        os.makedirs(directory, exist_ok=True)
        temporary = self.auto_config_path + ".tmp"
        document = {
            "version": 1,
            "plugin_version": PLUGIN_META["version"],
            "params": prepare_params(self.auto_params),
        }
        try:
            with open(temporary, "w", encoding="utf-8") as file:
                json.dump(document, file, ensure_ascii=False, indent=2)
            os.replace(temporary, self.auto_config_path)
        except OSError:
            try:
                if os.path.exists(temporary):
                    os.remove(temporary)
            except OSError:
                pass
            raise

    def show_auto_params_dialog(self):
        dialog = AutoDetectParamsDialog(self.auto_params, self)

        def save_from_dialog(params):
            self.auto_params = prepare_params(params)
            try:
                self._save_auto_config()
            except OSError as exc:
                QMessageBox.warning(self, "保存失败", f"自动参数保存失败：{exc}")
                return
            self.set_status(f"自动参数已保存：{self.auto_config_path}")

        dialog.params_saved.connect(save_from_dialog)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            save_from_dialog(dialog.params())

    def on_location_mode_changed(self, *_args):
        automatic = self.location_mode_combo.currentData() == "auto"
        if automatic and self.canvas.drawing_enabled:
            self.canvas.set_drawing_enabled(False)
        self.start_draw_button.setVisible(not automatic)
        self.draw_mode_combo.setVisible(not automatic)
        self.auto_detect_button.setVisible(automatic)
        self.auto_params_button.setVisible(automatic)
        self.roi_select_button.setVisible(automatic)
        self.roi_clear_button.setVisible(automatic)
        busy = bool(self.auto_worker is not None and self.auto_worker.isRunning())
        self.start_draw_button.setEnabled((not automatic) and not busy)
        self.draw_mode_combo.setEnabled((not automatic) and not busy)
        self.auto_detect_button.setEnabled(automatic and not busy and cv2 is not None)
        self.auto_params_button.setEnabled(automatic)
        self.roi_select_button.setEnabled(automatic)
        self.roi_clear_button.setEnabled(automatic)
        
        # 设置ROI框选状态
        self.canvas.set_roi_enabled(automatic)
        
        if automatic:
            self.set_status("自动找圆心：可检测当前导入图片或固定当前相机帧。可以在图片上拖拽框选ROI区域。")
        else:
            self.set_status(self.canvas.mode_help_text())

    def toggle_roi_selection(self, checked):
        """切换ROI框选模式"""
        if checked:
            # 启用ROI框选模式
            self.canvas.set_roi_enabled(True)
            self.set_status("已启用ROI框选模式。请在图像上拖拽选择检测区域。")
        else:
            # 禁用ROI框选模式，但不清除已选的ROI
            self.canvas.set_roi_enabled(False)
            self.set_status("已禁用ROI框选模式。")

    def on_roi_drawn(self, x, y, w, h):
        """处理ROI框选完成事件"""
        # 更新自动参数中的ROI设置
        self.auto_params["roi_x"] = x
        self.auto_params["roi_y"] = y
        self.auto_params["roi_w"] = w
        self.auto_params["roi_h"] = h
        self.set_status(f"已框选ROI区域：X={x}, Y={y}, 宽={w}, 高={h}")

    def clear_roi(self):
        """清除ROI选择，恢复全图检测"""
        self.canvas.clear_roi()
        # 取消ROI框选按钮的选中状态
        self.roi_select_button.setChecked(False)
        # 更新自动参数中的ROI设置
        self.auto_params["roi_x"] = 0
        self.auto_params["roi_y"] = 0
        self.auto_params["roi_w"] = 0
        self.auto_params["roi_h"] = 0
        self.set_status("已清除ROI选择，将使用全图进行检测。")

    def _source_signature(self):
        pixmap = self.canvas.full_pixmap
        return (self.current_image_path, pixmap.width(), pixmap.height())

    def _invalidate_detection(self, request_interruption=False):
        self._detect_generation += 1
        if request_interruption and self.auto_worker is not None and self.auto_worker.isRunning():
            self.auto_worker.requestInterruption()

    def _set_auto_busy(self, busy):
        self.open_button.setEnabled(not busy)
        self.camera_button.setEnabled(not busy)
        self.freeze_button.setEnabled(not busy)
        self.camera_combo.setEnabled(not busy)
        self.refresh_camera_button.setEnabled(not busy)
        self.camera_width_edit.setEnabled(not busy)
        self.camera_height_edit.setEnabled(not busy)
        self.camera_fps_edit.setEnabled(not busy)
        self.image_list.setEnabled(not busy)
        self.location_mode_combo.setEnabled(not busy)
        self.start_draw_button.setEnabled(not busy and self.location_mode_combo.currentData() == "manual")
        self.draw_mode_combo.setEnabled(not busy and self.location_mode_combo.currentData() == "manual")
        self.auto_detect_button.setEnabled(
            not busy and self.location_mode_combo.currentData() == "auto" and cv2 is not None
        )
        self.auto_detect_button.setText("检测中…" if busy else "自动检测当前画面")

    def start_auto_detection(self):
        if self.auto_worker is not None and self.auto_worker.isRunning():
            return
        if cv2 is None:
            QMessageBox.warning(self, "自动检测失败", "未安装 OpenCV，无法自动找圆心。")
            return
        if self.canvas.full_pixmap.isNull() or not self.current_image_path:
            QMessageBox.information(self, "自动检测", "请先导入图片或打开相机。")
            return
        if self.canvas.comparison_mode:
            QMessageBox.information(self, "自动检测", "请先返回普通图片或相机画面。")
            return

        if self.camera_capture is not None and not self.frame_frozen:
            self.camera_timer.stop()
            self.frame_frozen = True
            self.freeze_button.blockSignals(True)
            self.freeze_button.setChecked(True)
            self.freeze_button.setText("取消固定")
            self.freeze_button.blockSignals(False)
            self.current_image_path = "camera://frozen"
            self.canvas.image_path = self.current_image_path
            self.image_label.setText("当前画面：相机固定帧（自动检测快照）")

        snapshot = self.canvas.full_pixmap.copy()
        self.canvas.full_pixmap = snapshot
        self.canvas.pixmap = snapshot
        self.canvas.show_full_image()
        try:
            image_bgr = pixmap_to_bgr(snapshot)
        except (ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "自动检测失败", str(exc))
            return

        # 从画布获取ROI矩形并更新自动参数
        roi_rect = self.canvas.get_roi_rect()
        if roi_rect:
            x, y, w, h = roi_rect
            self.auto_params["roi_x"] = int(x)
            self.auto_params["roi_y"] = int(y)
            self.auto_params["roi_w"] = int(w)
            self.auto_params["roi_h"] = int(h)
            self.set_status(f"使用ROI区域：({x}, {y}), 尺寸：{w}×{h} 像素")
        else:
            # 清除ROI设置，使用全图
            self.auto_params["roi_x"] = 0
            self.auto_params["roi_y"] = 0
            self.auto_params["roi_w"] = 0
            self.auto_params["roi_h"] = 0
            self.set_status("未选择ROI区域，将使用全图进行检测。")

        self._invalidate_detection()
        generation = self._detect_generation
        source = self._source_signature()
        worker = AutoDetectWorker(image_bgr, self.auto_params)
        self.auto_worker = worker
        self._detect_snapshot = (generation, source)
        worker.result_ready.connect(lambda result, current=worker: self._on_auto_result(current, result))
        worker.finished.connect(lambda current=worker: self._on_auto_thread_finished(current))
        self._set_auto_busy(True)
        self.set_status("正在后台自动检测当前画面…")
        worker.start()

    def _on_auto_result(self, worker, result):
        snapshot = self._detect_snapshot
        valid = (
            worker is self.auto_worker
            and snapshot is not None
            and snapshot[0] == self._detect_generation
            and snapshot[1] == self._source_signature()
            and self._active
            and not self._shutdown
        )
        if not valid:
            return
        if not isinstance(result, DetectResult) or not result.ok:
            message = result.message if isinstance(result, DetectResult) else "自动检测没有返回有效结果。"
            self.set_status(f"自动检测失败：{message}")
            QMessageBox.warning(self, "自动检测失败", message + "\n\n可打开“自动参数”调整后重试。")
            return
        measurement = CircleMeasurement(
            center_x=result.center_x,
            center_y=result.center_y,
            radius_px=result.radius,
            mode="自动圆心检测",
            points=[],
        )
        if not self.canvas.accept_measurement(measurement):
            return
        threshold = "自适应" if result.used_threshold < 0 else f"{result.used_threshold:.0f}"
        self.set_status(
            f"自动检测成功：圆度 {result.circularity:.3f}，RMS {result.rms:.3f}px，"
            f"边沿点 {result.edge_count}，阈值 {threshold}。"
        )

    def _on_auto_thread_finished(self, worker):
        if worker is not self.auto_worker:
            return
        self.auto_worker = None
        self._detect_snapshot = None
        self._set_auto_busy(False)
        worker.deleteLater()

    def _dispose_auto_worker(self, wait_ms=2000):
        worker = self.auto_worker
        if worker is None:
            return
        worker.requestInterruption()
        if worker.isRunning() and not worker.wait(wait_ms):
            for signal in (worker.result_ready, worker.finished):
                try:
                    signal.disconnect()
                except (TypeError, RuntimeError):
                    pass
            _ORPHANED_AUTO_WORKERS.add(worker)
            worker.finished.connect(lambda current=worker: _release_orphan_worker(current))
            if worker.isFinished():
                _release_orphan_worker(worker)
        else:
            worker.deleteLater()
        self.auto_worker = None
        self._detect_snapshot = None

    def on_zoom_changed(self, zoom_factor):
        self.zoom_label.setText(f"当前缩放：{zoom_factor * 100:.0f}%")

    def resolve_zoom_anchor(self):
        if not hasattr(self, "scroll_area"):
            return None
        global_pos = QCursor.pos()
        viewport_pos = self.scroll_area.viewport().mapFromGlobal(global_pos)
        if self.scroll_area.viewport().rect().contains(viewport_pos):
            return self.canvas.mapFromGlobal(global_pos)
        return None

    def zoom_canvas_with_anchor(self, scale_factor=None, target_zoom=None, anchor_pos=None):
        if self.canvas.pixmap.isNull():
            return

        old_zoom = self.canvas.zoom_factor
        if target_zoom is None:
            target_zoom = old_zoom * scale_factor
        target_zoom = max(0.1, min(10.0, target_zoom))
        if abs(target_zoom - old_zoom) < 1e-9:
            return

        hbar = self.scroll_area.horizontalScrollBar()
        vbar = self.scroll_area.verticalScrollBar()
        old_hbar = hbar.value()
        old_vbar = vbar.value()

        if anchor_pos is None:
            viewport_center = self.scroll_area.viewport().rect().center()
            anchor_pos = self.canvas.mapFrom(self.scroll_area.viewport(), viewport_center)

        image_pos = self.canvas.widget_pos_to_image_pos(anchor_pos)
        viewport_x = anchor_pos.x() - old_hbar
        viewport_y = anchor_pos.y() - old_vbar

        self.canvas.set_zoom(target_zoom)

        if image_pos is not None:
            new_canvas_x = image_pos[0] * self.canvas.zoom_factor
            new_canvas_y = image_pos[1] * self.canvas.zoom_factor
            hbar.setValue(int(round(new_canvas_x - viewport_x)))
            vbar.setValue(int(round(new_canvas_y - viewport_y)))

    def zoom_in_view(self):
        self.zoom_canvas_with_anchor(scale_factor=1.25, anchor_pos=self.resolve_zoom_anchor())

    def zoom_out_view(self):
        self.zoom_canvas_with_anchor(scale_factor=1 / 1.25, anchor_pos=self.resolve_zoom_anchor())

    def reset_zoom_view(self):
        self.zoom_canvas_with_anchor(target_zoom=1.0, anchor_pos=self.resolve_zoom_anchor())

    def handle_zoom_request(self, scale_factor, anchor_pos):
        self.zoom_canvas_with_anchor(scale_factor=scale_factor, anchor_pos=anchor_pos)

    def on_drawing_state_changed(self, drawing_enabled):
        self.drawing_state_label.setText(
            "画圆状态：绘制中" if drawing_enabled else "画圆状态：未开始"
        )
        if not drawing_enabled:
            job = self.current_job()
            if job is not None and job.circle is not None and not self.canvas.circle_focused:
                self.canvas.focus_on_circle(job.circle)
                self.current_circle = job.circle
        self.start_draw_button.blockSignals(True)
        self.start_draw_button.setChecked(drawing_enabled)
        self.start_draw_button.blockSignals(False)

    def toggle_drawing(self, checked):
        if not self.current_image_path:
            self.start_draw_button.blockSignals(True)
            self.start_draw_button.setChecked(False)
            self.start_draw_button.blockSignals(False)
            QMessageBox.information(self, "提示", "请先打开一张图片或相机。")
            return
        self.canvas.set_drawing_enabled(checked)

    def change_draw_mode(self, mode):
        self.canvas.set_draw_mode(mode)
        self.current_circle = None
        self.current_circle_label.setText("当前标定：未完成")

    def toggle_camera(self, checked):
        if checked:
            self.start_camera()
        else:
            self.stop_camera()

    def selected_camera_index(self):
        camera_index = self.camera_combo.currentData()
        return 0 if camera_index is None else int(camera_index)

    def open_capture(self, camera_index):
        capture = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
        if capture.isOpened():
            return capture
        capture.release()
        capture = cv2.VideoCapture(camera_index)
        if capture.isOpened():
            return capture
        capture.release()
        return None

    def read_camera_format(self):
        try:
            width = int(float(self.camera_width_edit.text().strip()))
            height = int(float(self.camera_height_edit.text().strip()))
            fps = float(self.camera_fps_edit.text().strip())
        except ValueError as exc:
            raise ValueError("清晰度和帧率请直接填写数字。") from exc
        if width < 2 or height < 2 or fps <= 0:
            raise ValueError("清晰度和帧率必须大于 0。")
        return width, height, fps

    def apply_camera_format(self, capture, width, height, fps):
        fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        capture.set(cv2.CAP_PROP_FOURCC, fourcc)
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
        capture.set(cv2.CAP_PROP_FPS, float(fps))
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def on_camera_format_edited(self):
        try:
            width, height, fps = self.read_camera_format()
        except ValueError as exc:
            self.set_status(str(exc))
            return
        requested = (width, height, fps)
        if requested == self._last_format_notice:
            if self.camera_capture is None or self.frame_frozen or requested == self._running_camera_format:
                return
        self._last_format_notice = requested
        if self.camera_capture is None:
            self.set_status(f"下次打开相机使用 {width}×{height} @ {fps:g} fps。")
            return
        if self.frame_frozen:
            self.set_status("当前帧已固定。取消固定后会按新的清晰度和帧率重新打开。")
            return
        self.start_camera()

    def on_camera_changed(self, _index):
        if self.camera_capture is not None:
            self.start_camera()

    def refresh_camera_list(self):
        if cv2 is None:
            QMessageBox.warning(self, "刷新相机失败", "未安装 OpenCV，请先执行：pip install opencv-python")
            return

        was_running = self.camera_capture is not None
        selected_index = self.selected_camera_index()
        self.stop_camera()
        self.setCursor(Qt.CursorShape.WaitCursor)
        found = []
        try:
            for camera_index in range(6):
                capture = self.open_capture(camera_index)
                if capture is None:
                    continue
                found.append(camera_index)
                capture.release()
        finally:
            self.unsetCursor()

        self.camera_combo.blockSignals(True)
        self.camera_combo.clear()
        indexes = found or list(range(4))
        for camera_index in indexes:
            suffix = "" if found else "（未探测到）"
            self.camera_combo.addItem(f"相机 {camera_index}{suffix}", camera_index)
        match = self.camera_combo.findData(selected_index)
        self.camera_combo.setCurrentIndex(match if match >= 0 else 0)
        self.camera_combo.blockSignals(False)

        if found:
            names = "、".join(f"相机 {camera_index}" for camera_index in found)
            self.set_status(f"已找到 {len(found)} 个相机：{names}。可在下拉框中切换。")
        else:
            self.set_status("没有探测到可用相机，仍可手动选择编号后再打开。")

        if was_running:
            self.camera_button.setChecked(True)
            self.start_camera()

    def start_camera(self):
        if cv2 is None:
            self._reset_camera_button()
            QMessageBox.warning(self, "打开相机失败", "未安装 OpenCV，请先执行：pip install opencv-python")
            return

        camera_index = self.selected_camera_index()
        try:
            width, height, fps = self.read_camera_format()
        except ValueError as exc:
            self._reset_camera_button()
            QMessageBox.warning(self, "相机参数错误", str(exc))
            return
        self.stop_camera(update_button=False)
        capture = self.open_capture(camera_index)
        if capture is None:
            self._reset_camera_button()
            QMessageBox.warning(
                self,
                "打开相机失败",
                f"相机 {camera_index} 无法打开。请点击“刷新”查看可用相机，或确认它没有被其他程序占用。",
            )
            return

        self.apply_camera_format(capture, width, height, fps)
        self.camera_capture = capture
        self.frame_pump = CameraFramePump(capture)
        self.frame_pump.start()
        self._running_camera_format = (width, height, fps)
        self._last_format_notice = (width, height, fps)
        actual_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        self.comparison_visible = False
        self.current_image_path = "camera://live"
        self.canvas.retained_circles = []
        self.canvas.current_circle = None
        self.canvas.pending_points = []
        self.current_circle = None
        self.current_circle_label.setText("当前标定：未完成")
        self.image_label.setText(f"当前画面：相机 {camera_index} 实时视频")
        self.camera_button.setText("关闭相机")
        self.frame_frozen = False
        self.freeze_button.blockSignals(True)
        self.freeze_button.setChecked(False)
        self.freeze_button.setText("固定当前帧")
        self.freeze_button.blockSignals(False)
        self.grab_camera_frame()
        self.camera_timer.start(33)
        self.sync_circle_overlay()
        self.set_status(
            f"相机已打开。请求 {width}×{height} @ {fps:g} fps，"
            f"设备返回 {actual_width}×{actual_height} @ {actual_fps:g} fps。"
            "分辨率高、帧率跟不上时会保持上一张画面。"
        )

    def stop_camera(self, update_button=True):
        self._invalidate_detection(request_interruption=True)
        self.camera_timer.stop()
        if self.frame_pump is not None:
            self.frame_pump.stop()
            self.frame_pump = None
        if self.camera_capture is not None:
            self.camera_capture.release()
            self.camera_capture = None
        if update_button:
            self._reset_camera_button()
        elif self.camera_button.text() != "打开相机":
            self.camera_button.setText("打开相机")

    def _reset_camera_button(self):
        self.camera_button.blockSignals(True)
        self.camera_button.setChecked(False)
        self.camera_button.setText("打开相机")
        self.camera_button.blockSignals(False)

    def toggle_freeze(self, checked):
        if checked and self.camera_capture is None:
            self.freeze_button.blockSignals(True)
            self.freeze_button.setChecked(False)
            self.freeze_button.blockSignals(False)
            QMessageBox.information(self, "提示", "请先打开相机，再固定当前帧。")
            return
        self._invalidate_detection(request_interruption=True)
        self.frame_frozen = checked
        if checked:
            self.camera_timer.stop()
            if not self.canvas.pixmap.isNull():
                self.canvas.full_pixmap = self.canvas.pixmap.copy()
            self.current_image_path = "camera://frozen"
            self.freeze_button.setText("取消固定")
            self.set_status("已固定当前帧，画面保持不动。可以在这一帧上画圆。")
            return
        self.freeze_button.setText("固定当前帧")
        if self.camera_capture is not None:
            try:
                requested = self.read_camera_format()
            except ValueError:
                requested = self._running_camera_format
            if requested != self._running_camera_format:
                self.start_camera()
                return
            self.canvas.show_full_image()
            self.current_image_path = "camera://live"
            self.camera_timer.start(33)
            self.set_status("已取消固定，恢复视频流。")

    def grab_camera_frame(self):
        if self.frame_frozen or self.frame_pump is None:
            return
        frame = self.frame_pump.take()
        if frame is None:
            return
        height, width, channels = frame.shape
        image = QImage(frame.data, width, height, channels * width, QImage.Format.Format_RGB888).copy()
        self.canvas.update_live_frame(QPixmap.fromImage(image))

    def activate(self):
        """通知插件已进入前台；不自动占用相机。"""
        if self._shutdown:
            return
        self._active = True
        self._set_auto_busy(bool(self.auto_worker is not None and self.auto_worker.isRunning()))

    def deactivate(self):
        """离开插件页面时作废自动结果并立即释放相机资源。"""
        self._active = False
        self._invalidate_detection(request_interruption=True)
        self.stop_camera(update_button=True)

    def shutdown(self):
        """幂等停止图源，协作取消自动检测并安全托管未结束线程。"""
        if self._shutdown:
            return True
        self._shutdown = True
        self._active = False
        self._invalidate_detection(request_interruption=True)
        self.camera_timer.stop()
        self.stop_camera(update_button=False)
        try:
            self._save_auto_config()
        except OSError:
            pass
        self._dispose_auto_worker(wait_ms=2000)
        return True

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

    def current_job(self):
        if not (0 <= self.current_job_index < len(self.image_jobs)):
            return None
        return self.image_jobs[self.current_job_index]

    def refresh_image_list(self):
        row = self.current_job_index
        self.image_list.blockSignals(True)
        self.image_list.clear()
        for index, job in enumerate(self.image_jobs):
            prefix = "★ " if index == self.primary_index else ""
            state = "已画圆" if job.circle is not None else "未画圆"
            self.image_list.addItem(f"{prefix}图{index + 1} {state}  {os.path.basename(job.path)}")
        if 0 <= row < self.image_list.count():
            self.image_list.setCurrentRow(row)
        self.image_list.blockSignals(False)

    def refresh_circle_list(self):
        row = self.current_sample_index
        self.image_list.blockSignals(True)
        self.image_list.clear()
        for index, sample in enumerate(self.circle_samples):
            prefix = "★标定 " if index == self.primary_index else ""
            item_text = f"{prefix}圆{index + 1}  {sample.name}"
            self.image_list.addItem(item_text)
            item = self.image_list.item(index)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if sample.use_in_calc else Qt.CheckState.Unchecked)
            item.setForeground(QColor(self.circle_color(index)))
        if 0 <= row < self.image_list.count():
            self.image_list.setCurrentRow(row)
        self.image_list.blockSignals(False)

    def on_circle_check_changed(self, item):
        row = self.image_list.row(item)
        if not (0 <= row < len(self.circle_samples)):
            return
        self.circle_samples[row].use_in_calc = item.checkState() == Qt.CheckState.Checked
        self.sync_circle_overlay()
        self.refresh_offsets(write_file=False)

    def circle_color(self, index):
        colors = (
            "#ff4d4f",
            "#1677ff",
            "#52c41a",
            "#fa8c16",
            "#722ed1",
            "#13c2c2",
            "#eb2f96",
            "#a0d911",
        )
        return colors[index % len(colors)]

    def same_circle_view(self, sample):
        if sample.source_path == self.current_image_path:
            return True
        return sample.source_path.startswith("camera://") and self.current_image_path.startswith("camera://")

    def sync_circle_overlay(self):
        items = []
        for index, sample in enumerate(self.circle_samples):
            if not sample.use_in_calc or not self.same_circle_view(sample):
                continue
            label = sample.name
            if index == self.primary_index:
                label = f"标定 {label}"
            items.append((
                sample.circle,
                QColor(self.circle_color(index)),
                label,
                index == self.current_sample_index,
            ))
        self.canvas.circle_focused = False
        self.canvas.retained_circles = []
        self.canvas.overlay_active = True
        self.canvas.overlay_items = items
        self.canvas.update()

    def show_sample_background(self, sample):
        zoom = self.canvas.zoom_factor
        self.canvas.full_pixmap = sample.pixmap
        self.canvas.pixmap = sample.pixmap
        self.canvas.view_origin_x = 0.0
        self.canvas.view_origin_y = 0.0
        self.canvas.circle_focused = False
        self.canvas.zoom_factor = zoom
        self.canvas.update_canvas_size()

    def on_circle_sample_selected(self, row):
        if row < 0 or row >= len(self.circle_samples):
            return
        self._invalidate_detection(request_interruption=True)
        sample = self.circle_samples[row]
        self.current_sample_index = row
        self.current_image_path = sample.source_path
        if self.camera_capture is not None:
            self.camera_timer.stop()
            self.frame_frozen = True
            self.freeze_button.blockSignals(True)
            self.freeze_button.setChecked(True)
            self.freeze_button.setText("取消固定")
            self.freeze_button.blockSignals(False)
        self.show_sample_background(sample)
        self.current_circle = sample.circle
        self.diameter_px_edit.setText(f"{sample.diameter_px:.2f}")
        self.sync_circle_overlay()
        role = "标定圆" if row == self.primary_index else ("显示中" if sample.use_in_calc else "已隐藏")
        self.set_status(f"正在查看圆{row + 1}（{role}）。双击或右键列表项可以改名。")

    def show_circle_menu(self, pos):
        item = self.image_list.itemAt(pos)
        if item is None:
            return
        if not item.isSelected():
            self.image_list.clearSelection()
            item.setSelected(True)
        self.image_list.setCurrentItem(item)
        menu = QMenu(self)
        rename_action = menu.addAction("修改名称")
        delete_action = menu.addAction("删除选中")
        save_action = menu.addAction("保存选中帧")
        chosen = menu.exec(self.image_list.viewport().mapToGlobal(pos))
        if chosen == rename_action:
            self.rename_circle_item(item)
        elif chosen == delete_action:
            self.delete_selected_samples()
        elif chosen == save_action:
            self.save_selected_frames()

    def rename_circle_item(self, item):
        row = self.image_list.row(item)
        if not (0 <= row < len(self.circle_samples)):
            return
        sample = self.circle_samples[row]
        name, ok = QInputDialog.getText(self, "修改圆名称", "圆名称：", QLineEdit.EchoMode.Normal, sample.name)
        if not ok:
            return
        name = name.strip()
        if not name or name == sample.name:
            return
        sample.name = name
        self.refresh_circle_list()
        self.sync_circle_overlay()
        self.refresh_offsets(write_file=False)
        self.set_status(f"已把圆{row + 1}改名为“{name}”。")

    def selected_sample_rows(self):
        rows = sorted({
            self.image_list.row(item)
            for item in self.image_list.selectedItems()
            if 0 <= self.image_list.row(item) < len(self.circle_samples)
        })
        if rows:
            return rows
        row = self.image_list.currentRow()
        if 0 <= row < len(self.circle_samples):
            return [row]
        return []

    def delete_selected_samples(self):
        rows = self.selected_sample_rows()
        if not rows:
            QMessageBox.information(self, "提示", "请先在列表中选择要删除的帧。")
            return
        names = "、".join(self.circle_samples[row].name for row in rows[:6])
        if len(rows) > 6:
            names += "…"
        answer = QMessageBox.question(
            self,
            "删除帧",
            f"确定删除选中的 {len(rows)} 个帧？\n{names}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        deleted = set(rows)
        for row in reversed(rows):
            del self.circle_samples[row]

        if self.primary_index in deleted:
            self.primary_index = -1
            self.baseline_circle = None
            self.baseline_image_path = ""
            self.baseline_label.setText("初始位置：未设置")
        elif self.primary_index >= 0:
            self.primary_index -= sum(1 for row in deleted if row < self.primary_index)

        if not self.circle_samples:
            self.current_sample_index = -1
            self.current_circle = None
            self.diameter_px_edit.clear()
        elif self.current_sample_index in deleted:
            self.current_sample_index = min(min(deleted), len(self.circle_samples) - 1)
        elif self.current_sample_index >= 0:
            self.current_sample_index -= sum(1 for row in deleted if row < self.current_sample_index)

        self.refresh_circle_list()
        self.sync_circle_overlay()
        self.refresh_offsets(write_file=False)
        self.set_status(f"已删除 {len(deleted)} 个帧，对应圆已经从画面上去掉。")

    def save_selected_frames(self):
        rows = self.selected_sample_rows()
        if not rows:
            QMessageBox.information(self, "提示", "请先在列表中选择要保存的帧。")
            return
        directory = QFileDialog.getExistingDirectory(self, "选择帧保存目录", self.output_dir)
        if not directory:
            return

        saved = []
        failed = []
        for row in rows:
            sample = self.circle_samples[row]
            if sample.pixmap.isNull():
                failed.append(sample.name)
                continue
            pixmap = self.render_saved_frame(sample, self.circle_color(row))
            filename = f"{row + 1:02d}_{self.safe_frame_name(sample.name)}.png"
            path = os.path.join(directory, filename)
            if os.path.exists(path):
                stamp = datetime.now().strftime("%H%M%S")
                filename = f"{row + 1:02d}_{self.safe_frame_name(sample.name)}_{stamp}.png"
                path = os.path.join(directory, filename)
            if pixmap.save(path, "PNG"):
                saved.append(path)
            else:
                failed.append(sample.name)

        if saved:
            self.set_status(f"已保存 {len(saved)} 张帧到：{directory}")
        if failed:
            QMessageBox.warning(self, "部分保存失败", "以下帧没有保存成功：\n" + "、".join(failed))
        elif saved:
            QMessageBox.information(self, "保存完成", f"已保存 {len(saved)} 张帧到：\n{directory}")

    def safe_frame_name(self, name):
        cleaned = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in name).strip().rstrip(".")
        return cleaned or "frame"

    def render_saved_frame(self, sample, color):
        pixmap = sample.pixmap.copy()
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        circle = sample.circle
        pen = QPen(QColor(color))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(
            int(circle.center_x - circle.radius_px),
            int(circle.center_y - circle.radius_px),
            int(circle.diameter_px),
            int(circle.diameter_px),
        )
        painter.drawText(int(circle.center_x + 8), int(circle.center_y - 8), sample.name)
        painter.end()
        return pixmap

    def apply_diameter_px(self):
        if not (0 <= self.current_sample_index < len(self.circle_samples)):
            return
        text = self.diameter_px_edit.text().strip()
        if not text:
            return
        try:
            value = float(text)
        except ValueError:
            return
        if value <= 0:
            return
        self.circle_samples[self.current_sample_index].diameter_px = value
        self.refresh_offsets(write_file=False)

    def open_image(self):
        self.stop_camera()
        image_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择图片，可一次导入多张",
            "",
            "Image Files (*.png *.jpg *.jpeg *.bmp *.tif *.tiff);;All Files (*)",
        )
        if not image_paths:
            return

        if self.camera_capture is not None:
            self.camera_timer.stop()
            self.frame_frozen = True
            self.freeze_button.blockSignals(True)
            self.freeze_button.setChecked(True)
            self.freeze_button.setText("取消固定")
            self.freeze_button.blockSignals(False)
        self.import_queue = list(image_paths)
        self.show_next_import()
        if len(image_paths) == 1:
            self.set_status("图片已导入，可使用当前定位方式测量圆心。")
        else:
            self.set_status(
                f"已导入 {len(image_paths)} 张图片，当前显示第 1 张；本次不会自动推进，"
                "需要时请重新导入或从已保存圆列表切换。"
            )

    def show_next_import(self):
        if not self.import_queue:
            return
        self._invalidate_detection(request_interruption=True)
        image_path = self.import_queue.pop(0)
        try:
            self.canvas.set_image(image_path)
        except ValueError as exc:
            QMessageBox.warning(self, "打开失败", str(exc))
            self.show_next_import()
            return
        self.comparison_visible = False
        self.current_image_path = image_path
        self.current_circle = None
        self.update_output_dir_default(image_path)
        self.image_label.setText(f"当前图片：{image_path}")
        self.current_circle_label.setText("当前标定：未完成")
        self.sync_circle_overlay()

    def clear_all_measurements(self):
        self._invalidate_detection(request_interruption=True)
        self.circle_samples = []
        self.current_sample_index = -1
        self.primary_index = -1
        self.import_queue = []
        self.diameter_px_edit.clear()
        self.canvas.show_full_image()
        self.canvas.clear_measurement()
        self.refresh_circle_list()
        self.comparison_visible = False
        self.current_circle = None
        self.baseline_circle = None
        self.baseline_image_path = ""
        self.comparison_records = []
        self.baseline_result_written = False
        self.current_circle_label.setText("当前标定：未完成")
        self.baseline_label.setText("初始位置：未设置")
        self.result_label.setPlainText("结果：等待计算")
        self._set_result_state("neutral")
        self.refresh_comparison_view()
        self.set_status("已清除全部标定点。再次设置初始位置后会开始新一轮计算。")

    def choose_output_dir(self):
        directory = QFileDialog.getExistingDirectory(
            self,
            "选择结果保存目录",
            self.output_dir,
        )
        if not directory:
            return

        self.output_dir = directory
        self.output_dir_is_custom = True
        self.output_dir_edit.setText(directory)
        self.set_status(f"结果输出目录已更新：{directory}")

    def get_output_file_paths(self):
        stamp = self.output_session_stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = os.path.join(self.output_dir, f"offset_results_{stamp}.csv")
        txt_path = os.path.join(self.output_dir, f"latest_result_{stamp}.txt")
        return csv_path, txt_path

    def find_latest_output_file(self):
        if not os.path.isdir(self.output_dir):
            return None

        if self.output_session_stamp:
            current_txt = os.path.join(self.output_dir, f"latest_result_{self.output_session_stamp}.txt")
            current_csv = os.path.join(self.output_dir, f"offset_results_{self.output_session_stamp}.csv")
            if os.path.isfile(current_txt):
                return current_txt
            if os.path.isfile(current_csv):
                return current_csv

        candidates = []
        for file_name in os.listdir(self.output_dir):
            if file_name.startswith("latest_result_") and file_name.endswith(".txt"):
                file_path = os.path.join(self.output_dir, file_name)
                if os.path.isfile(file_path):
                    candidates.append((os.path.getmtime(file_path), file_path))
            elif file_name.startswith("offset_results_") and file_name.endswith(".csv"):
                file_path = os.path.join(self.output_dir, file_name)
                if os.path.isfile(file_path):
                    candidates.append((os.path.getmtime(file_path), file_path))

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def open_result_file(self):
        target_path = self.find_latest_output_file()

        if target_path is None:
            QMessageBox.information(self, "提示", "当前输出目录下还没有偏移值文件。")
            return

        try:
            opener = _context_member(self.context, "open_file") or _context_member(
                self.context, "open_path"
            )
            if callable(opener):
                opened = opener(target_path)
                if opened is False:
                    raise OSError("宿主拒绝打开该文件")
            elif not QDesktopServices.openUrl(QUrl.fromLocalFile(target_path)):
                raise OSError("系统没有可用的文件打开程序")
        except (OSError, RuntimeError) as exc:
            QMessageBox.warning(self, "打开失败", f"无法打开结果文件：{exc}")
            return

        self.set_status(f"已打开结果文件：{target_path}")

    def apply_output_dir_text(self):
        directory = self.output_dir_edit.text().strip()
        if not directory:
            self.output_dir_edit.setText(self.output_dir)
            return
        self.output_dir = directory
        self.output_dir_is_custom = True
        self.set_status(f"结果输出目录已更新：{directory}")

    def update_output_dir_default(self, _image_path):
        """保持宿主分配的插件输出目录；只有用户明确选择时才改写。"""
        if self.output_dir_is_custom:
            return
        self.output_dir = self.default_output_dir
        self.output_dir_edit.setText(self.default_output_dir)

    def format_result_name(self):
        file_name = os.path.basename(self.current_image_path)
        name, _ = os.path.splitext(file_name)
        if name.isdigit():
            return f"图{name}"
        return name or "当前图片"

    def ensure_baseline_result_written(self, real_diameter_mm):
        if self.baseline_result_written or self.baseline_circle is None:
            return
        base_scale = real_diameter_mm / self.baseline_circle.diameter_px
        self.write_result(
            result_name="图1",
            real_diameter_mm=real_diameter_mm,
            mm_per_px=base_scale,
            dx_px=0.0,
            dy_px=0.0,
            total_px=0.0,
            dx_mm=0.0,
            dy_mm=0.0,
            total_mm=0.0,
            image_path=self.baseline_image_path,
            circle=self.baseline_circle,
        )
        self.baseline_result_written = True

    def get_or_create_record_label(self, image_path, is_baseline=False):
        for record in self.comparison_records:
            if record.image_path == image_path:
                return record.label
        if is_baseline or not self.comparison_records:
            return "图1"
        return f"图{len(self.comparison_records) + 1}"

    def format_label_for_path(self, image_path):
        file_name = os.path.basename(image_path)
        name, _ = os.path.splitext(file_name)
        if name.isdigit():
            return f"图{name}"
        return name or "当前图片"

    def clone_circle(self, circle):
        return CircleMeasurement(
            center_x=circle.center_x,
            center_y=circle.center_y,
            radius_px=circle.radius_px,
            mode=circle.mode,
            points=list(circle.points),
        )

    def upsert_comparison_record(self, image_path, label, circle, is_baseline=False, dx_mm=0.0, dy_mm=0.0, total_mm=0.0):
        copied_circle = self.clone_circle(circle)
        for index, record in enumerate(self.comparison_records):
            if record.image_path == image_path:
                self.comparison_records[index] = ComparisonRecord(
                    image_path=image_path,
                    label=label,
                    circle=copied_circle,
                    is_baseline=is_baseline,
                    dx_mm=dx_mm,
                    dy_mm=dy_mm,
                    total_mm=total_mm,
                    label_offset_x=record.label_offset_x,
                    label_offset_y=record.label_offset_y,
                )
                return
        new_index = len(self.comparison_records)
        self.comparison_records.append(
            ComparisonRecord(
                image_path=image_path,
                label=label,
                circle=copied_circle,
                is_baseline=is_baseline,
                dx_mm=dx_mm,
                dy_mm=dy_mm,
                total_mm=total_mm,
                label_offset_x=(new_index % 3) * 18,
                label_offset_y=(new_index % 4) * 26,
            )
        )

    def on_circle_changed(self, circle):
        self.current_circle = circle
        if circle is None:
            self.current_circle_label.setText("当前标定：未完成")
            return
        self.current_circle_label.setText(
            f"当前标定：{circle.mode}；{circle.summary()}"
        )
        if self._loading_sample:
            return
        source = self.canvas.full_pixmap if not self.canvas.full_pixmap.isNull() else self.canvas.pixmap
        if source.isNull():
            return
        if self.current_image_path.startswith("camera://"):
            sample_name = f"相机帧 {datetime.now().strftime('%H:%M:%S')}"
        else:
            sample_name = os.path.basename(self.current_image_path) or "图片"
        sample = CircleSample(
            name=sample_name,
            source_path=self.current_image_path,
            pixmap=source.copy(),
            circle=self.clone_circle(circle),
            diameter_px=circle.diameter_px,
        )
        self.circle_samples.append(sample)
        self.current_sample_index = len(self.circle_samples) - 1
        self.diameter_px_edit.setText(f"{sample.diameter_px:.2f}")
        self.canvas.show_full_image()
        self.refresh_circle_list()
        self.sync_circle_overlay()
        self.refresh_offsets(write_file=False)
        self.set_status(f"已保存圆{self.current_sample_index + 1}。取消勾选后，画面上的圆和点也会去掉。")

    def get_real_diameter_mm(self):
        text = self.real_diameter_edit.text().strip()
        try:
            value = float(text)
        except ValueError as exc:
            raise ValueError("实际圆直径必须是数字。") from exc

        if value <= 0:
            raise ValueError("实际圆直径必须大于 0。")
        return value

    def set_baseline(self):
        if self.circle_samples:
            row = self.image_list.currentRow()
            if row < 0:
                QMessageBox.information(self, "提示", "请先在列表中选择一个圆。")
                return
            self.primary_index = row
            sample = self.circle_samples[row]
            sample.use_in_calc = True
            self.baseline_image_path = sample.source_path
            self.baseline_circle = sample.circle
            self.baseline_label.setText(f"标定圆：圆{row + 1} {sample.name}\n{sample.circle.summary()}")
            self.refresh_circle_list()
            self.sync_circle_overlay()
            self.refresh_offsets(write_file=False)
            self.set_status("已把该圆设为标定圆。勾选的其他圆会相对它计算偏移。")
            return
        if not self.current_image_path:
            QMessageBox.information(self, "提示", "请先打开一张图片或相机。")
            return
        baseline_image_path = self.current_image_path
        if self.current_image_path.startswith("camera://"):
            snapshot_path = os.path.join(
                self.output_dir,
                f"camera_snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png",
            )
            os.makedirs(self.output_dir, exist_ok=True)
            if self.canvas.pixmap.isNull() or not self.canvas.pixmap.save(snapshot_path):
                QMessageBox.warning(self, "保存失败", "无法保存当前相机画面，初始位置未设置。")
                return
            baseline_image_path = snapshot_path
        if self.current_circle is None:
            QMessageBox.information(self, "提示", "请先在当前图片上完成画圆标定。")
            return

        self.baseline_image_path = baseline_image_path
        self.baseline_circle = CircleMeasurement(
            center_x=self.current_circle.center_x,
            center_y=self.current_circle.center_y,
            radius_px=self.current_circle.radius_px,
            mode=self.current_circle.mode,
            points=list(self.current_circle.points),
        )
        self.baseline_label.setText(
            f"初始位置：{self.baseline_image_path}\n{self.baseline_circle.summary()}"
        )
        self.baseline_result_written = False
        self.output_session_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.comparison_records = []
        self.upsert_comparison_record(
            image_path=self.baseline_image_path,
            label="图1",
            circle=self.baseline_circle,
            is_baseline=True,
            dx_mm=0.0,
            dy_mm=0.0,
            total_mm=0.0,
        )
        self.comparison_visible = False
        self.result_label.setPlainText("结果：已设置图1（标定图）")
        self._set_result_state("neutral")
        self.set_status(
            f"标定图已保存。本轮结果会写入带时间戳的新文件：{self.output_session_stamp}"
        )

    def refresh_offsets(self, write_file=False):
        if not (0 <= self.primary_index < len(self.circle_samples)):
            return
        primary = self.circle_samples[self.primary_index]
        try:
            real_diameter_mm = self.get_real_diameter_mm()
        except ValueError as exc:
            if write_file:
                QMessageBox.warning(self, "参数错误", str(exc))
            return

        diameter_px = primary.diameter_px or primary.circle.diameter_px
        if diameter_px <= 0:
            return
        mm_per_px = real_diameter_mm / diameter_px
        lines = [
            f"标定圆：圆{self.primary_index + 1} {primary.name}",
            f"直径像素：{diameter_px:.2f} px",
            f"每像素实际距离：{mm_per_px:.6f} mm/px",
        ]
        compared = 0
        for index, sample in enumerate(self.circle_samples):
            if index == self.primary_index or not sample.use_in_calc:
                continue
            dx_px = sample.circle.center_x - primary.circle.center_x
            dy_px = sample.circle.center_y - primary.circle.center_y
            total_px = math.hypot(dx_px, dy_px)
            dx_mm = dx_px * mm_per_px
            dy_mm = dy_px * mm_per_px
            total_mm = math.hypot(dx_mm, dy_mm)
            compared += 1
            sample_diameter_px = sample.diameter_px or sample.circle.diameter_px
            lines.append(
                f"圆{index + 1} {sample.name}\n"
                f"  直径像素：{sample_diameter_px:.2f} px\n"
                f"  X:{dx_mm:.4f} mm  Y:{dy_mm:.4f} mm  总:{total_mm:.4f} mm"
            )
            if write_file:
                self.baseline_circle = primary.circle
                self.baseline_image_path = primary.source_path
                self.write_result(
                    result_name=f"圆{index + 1} {sample.name}",
                    real_diameter_mm=real_diameter_mm,
                    mm_per_px=mm_per_px,
                    dx_px=dx_px,
                    dy_px=dy_px,
                    total_px=total_px,
                    dx_mm=dx_mm,
                    dy_mm=dy_mm,
                    total_mm=total_mm,
                    image_path=sample.source_path,
                    circle=sample.circle,
                )
        if compared == 0:
            lines.append("请勾选要参与计算的其他圆。")
        self.result_label.setPlainText("\n".join(lines))
        self._set_result_state("success" if compared else "neutral")
        self.baseline_label.setText(
            f"标定圆：圆{self.primary_index + 1}\n每像素 {mm_per_px:.6f} mm"
        )

    def calculate_offset(self):
        if self.circle_samples:
            if self.primary_index < 0:
                QMessageBox.information(self, "提示", "请先在列表中选择一个圆，并点击“设为标定圆”。")
                return
            self.refresh_offsets(write_file=True)
            self.set_status("已按基准图计算其余图片的偏移，并给出每像素实际距离。")
            return
        if self.baseline_circle is None:
            QMessageBox.information(self, "提示", "请先完成初始位置标定。")
            return
        if not self.current_image_path:
            QMessageBox.information(self, "提示", "请先打开待计算图片。")
            return
        if self.current_circle is None:
            QMessageBox.information(self, "提示", "请先在当前图片上完成画圆标定。")
            return

        try:
            real_diameter_mm = self.get_real_diameter_mm()
        except ValueError as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            return

        base_scale = real_diameter_mm / self.baseline_circle.diameter_px
        current_scale = real_diameter_mm / self.current_circle.diameter_px
        mm_per_px = (base_scale + current_scale) / 2.0

        dx_px = self.current_circle.center_x - self.baseline_circle.center_x
        dy_px = self.current_circle.center_y - self.baseline_circle.center_y
        total_px = math.hypot(dx_px, dy_px)

        dx_mm = dx_px * mm_per_px
        dy_mm = dy_px * mm_per_px
        total_mm = math.hypot(dx_mm, dy_mm)
        result_name = self.get_or_create_record_label(self.current_image_path)

        result_text = (
            f"结果：{result_name}\n"
            f"直径像素：{self.current_circle.diameter_px:.2f} px\n"
            f"X:{dx_mm:.4f} mm  Y:{dy_mm:.4f} mm  总:{total_mm:.4f} mm"
        )
        self.result_label.setPlainText(result_text)

        try:
            self.ensure_baseline_result_written(real_diameter_mm)
            csv_path, txt_path = self.write_result(
                result_name=result_name,
                real_diameter_mm=real_diameter_mm,
                mm_per_px=mm_per_px,
                dx_px=dx_px,
                dy_px=dy_px,
                total_px=total_px,
                dx_mm=dx_mm,
                dy_mm=dy_mm,
                total_mm=total_mm,
            )
        except OSError as exc:
            self._set_result_state("error")
            QMessageBox.warning(self, "写入失败", f"结果文件写入失败：{exc}")
            return

        self._set_result_state("success")
        self.upsert_comparison_record(
            image_path=self.current_image_path,
            label=result_name,
            circle=self.current_circle,
            is_baseline=(self.current_image_path == self.baseline_image_path),
            dx_mm=dx_mm,
            dy_mm=dy_mm,
            total_mm=total_mm,
        )
        if self.comparison_visible:
            self.refresh_comparison_view()
        self.set_status(f"计算完成，结果已写入：{csv_path} 和 {txt_path}")

    def show_comparison(self):
        self.stop_camera()
        if self.baseline_circle is None or not self.baseline_image_path:
            QMessageBox.information(self, "提示", "请先设置初始位置后再进行偏移图片比对。")
            return
        if not self.comparison_records:
            self.upsert_comparison_record(
                image_path=self.baseline_image_path,
                label="图1",
                circle=self.baseline_circle,
                is_baseline=True,
                dx_mm=0.0,
                dy_mm=0.0,
                total_mm=0.0,
            )
        self.comparison_visible = True
        self.refresh_comparison_view()
        self.set_status("偏移图片比对已在当前主图区域显示。")

    def refresh_comparison_view(self):
        if self.baseline_circle is None or not self.baseline_image_path:
            return

        self.canvas.set_comparison_view(
            self.baseline_image_path,
            self.comparison_records,
        )

    def write_result(
        self,
        result_name,
        real_diameter_mm,
        mm_per_px,
        dx_px,
        dy_px,
        total_px,
        dx_mm,
        dy_mm,
        total_mm,
        image_path=None,
        circle=None,
    ):
        os.makedirs(self.output_dir, exist_ok=True)
        csv_path, txt_path = self.get_output_file_paths()
        image_path = image_path or self.current_image_path
        circle = circle or self.current_circle

        row = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "baseline_image": self.baseline_image_path,
            "current_image": image_path,
            "baseline_mode": self.baseline_circle.mode,
            "current_mode": circle.mode,
            "real_diameter_mm": f"{real_diameter_mm:.6f}",
            "baseline_center_x_px": f"{self.baseline_circle.center_x:.6f}",
            "baseline_center_y_px": f"{self.baseline_circle.center_y:.6f}",
            "baseline_diameter_px": f"{self.baseline_circle.diameter_px:.6f}",
            "current_center_x_px": f"{circle.center_x:.6f}",
            "current_center_y_px": f"{circle.center_y:.6f}",
            "current_diameter_px": f"{circle.diameter_px:.6f}",
            "avg_mm_per_px": f"{mm_per_px:.9f}",
            "delta_x_px": f"{dx_px:.6f}",
            "delta_y_px": f"{dy_px:.6f}",
            "total_offset_px": f"{total_px:.6f}",
            "delta_x_mm": f"{dx_mm:.6f}",
            "delta_y_mm": f"{dy_mm:.6f}",
            "total_offset_mm": f"{total_mm:.6f}",
        }

        file_exists = os.path.exists(csv_path)
        with open(csv_path, "a", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=list(row.keys()))
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)

        txt_exists = os.path.exists(txt_path)
        with open(txt_path, "a", encoding="utf-8") as file:
            if txt_exists and os.path.getsize(txt_path) > 0:
                file.write("\n")
            file.write(
                f"{result_name} x偏移: {dx_mm:.4f}, y偏移: {dy_mm:.4f}, 总偏移: {total_mm:.4f}"
            )

        return csv_path, txt_path


def create_plugin(context, parent=None):
    """ToolPlatform 插件工厂。"""
    return NozzleOffsetTool(context=context, parent=parent)


def main():
    """独立调试入口；插件导入和 create_plugin 均不会创建 QApplication。"""
    app = QApplication(sys.argv)
    window = NozzleOffsetTool()
    window.setWindowTitle("喷嘴圆心偏移标定工具")
    window.setMinimumSize(1180, 760)
    window.resize(1440, 900)
    window.activate()
    window.show()
    exit_code = app.exec()
    window.shutdown()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
