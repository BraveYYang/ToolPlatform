#!F:\anaconda3\envs\Mrlzr\python.exe
# -*- coding: utf-8 -*-
"""提取 Klipper 日志中的喷嘴补偿和开始打印事件，并按时间戳排序导出 CSV。"""

from __future__ import annotations

import argparse
import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = Path(r"D:\BaiduNetdiskDownload\klippy.log.2026-11-18")
DEFAULT_OUTPUT = SCRIPT_DIR / "喷嘴补偿与打印事件.csv"

# 目标行的通用时间戳。毫秒可有可无，以兼容不同日志格式。
TIMESTAMP_RE = re.compile(
    r"^\[[^\]]*\]\s+"
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:,\d+)?)"
)

# 目标一：喷嘴 Z 补偿记录。
NOZZLE_COMP_RE = re.compile(
    r"\[root\]\s+\[gcode:respond_info:\d+\]\s+"
    r"\[NOZZLE_COMP\]get_z_now_comp\s+"
    r"now_comp\s*:\s*(?P<now_comp>[-+]?\d+(?:\.\d+)?)"
)

# 目标二：开始打印记录。
PRINT_START_RE = re.compile(
    r"\[root\]\s+\[virtual_sdcard:work_handler:\d+\]\s+"
    r"work_handler\s+start\s+print,\s+filename\s*:\s*(?P<filename>.*)$"
)

# 补偿行中附带的数值字段，例如 idle_comp、target_temp、G28_temp。
NUMBER_FIELD_RE = re.compile(
    r"\b(?P<key>idle_comp|target_temp|G28_temp)\s*:\s*"
    r"(?P<value>[-+]?\d+(?:\.\d+)?)"
)

CSV_FIELDS = [
    "timestamp",
    "event_type",
    "now_comp",
    "idle_comp",
    "target_temp",
    "G28_temp",
    "filename",
    "line_number",
    "raw_line",
]


def parse_timestamp(line: str) -> Optional[datetime]:
    """提取日志行时间戳，无法解析时返回 None。"""
    match = TIMESTAMP_RE.search(line)
    if match is None:
        return None

    timestamp_text = match.group("timestamp")
    time_format = "%Y-%m-%d %H:%M:%S,%f" if "," in timestamp_text else "%Y-%m-%d %H:%M:%S"
    try:
        return datetime.strptime(timestamp_text, time_format)
    except ValueError:
        return None


def parse_nozzle_comp(line: str) -> Optional[dict[str, str]]:
    """解析 [NOZZLE_COMP]get_z_now_comp 记录。"""
    match = NOZZLE_COMP_RE.search(line)
    if match is None:
        return None

    event = {
        "event_type": "NOZZLE_COMP",
        "now_comp": match.group("now_comp"),
        "idle_comp": "",
        "target_temp": "",
        "G28_temp": "",
        "filename": "",
    }
    for field_match in NUMBER_FIELD_RE.finditer(line):
        event[field_match.group("key")] = field_match.group("value")
    return event


def parse_print_start(line: str) -> Optional[dict[str, str]]:
    """解析 work_handler start print 记录。"""
    match = PRINT_START_RE.search(line)
    if match is None:
        return None
    return {
        "event_type": "PRINT_START",
        "now_comp": "",
        "idle_comp": "",
        "target_temp": "",
        "G28_temp": "",
        "filename": match.group("filename").strip(),
    }


def extract_events(log_path: Path) -> list[dict[str, object]]:
    """扫描日志，提取目标事件，并按时间戳和原始行号升序排列。"""
    events: list[dict[str, object]] = []

    with log_path.open("r", encoding="utf-8", errors="replace") as log_file:
        for line_number, line in enumerate(log_file, start=1):
            timestamp = parse_timestamp(line)
            if timestamp is None:
                continue

            event = parse_nozzle_comp(line) or parse_print_start(line)
            if event is None:
                continue

            event["timestamp"] = timestamp
            event["line_number"] = line_number
            event["raw_line"] = line.rstrip("\r\n")
            events.append(event)

    return sorted(events, key=lambda event: (event["timestamp"], event["line_number"]))


def _parse_temp(value: object) -> float | None:
    """把任意 target_temp 值解析为 float，解析失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            # 兜底：从 "M104 S210" / "210℃" / "210/0" 之类里抠出第一个数字
            m = re.search(r"-?\d+(?:\.\d+)?", s)
            return float(m.group()) if m else None
    return None

def write_csv(events: list[dict[str, object]], output_path: Path) -> None:
    """将已排序事件以 UTF-8 BOM CSV 写入文件。"""
    with output_path.open("w", newline="", encoding="utf-8-sig") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for event in events:
            row = dict(event)

            target_temp = row["target_temp"]
            temp = _parse_temp(target_temp)
            if temp is not None:
                if temp <= 200.0 and row["event_type"] == "NOZZLE_COMP":
                    continue

            timestamp = row["timestamp"]
            if isinstance(timestamp, datetime):
                row["timestamp"] = timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            writer.writerow(row)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="提取 Klipper 日志中的喷嘴补偿与开始打印事件，并按时间戳排序。"
    )
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="输入日志路径，默认: %(default)s",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="输出 CSV 路径，默认: %(default)s",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    input_path = args.input.expanduser()
    output_path = args.output.expanduser()

    if not input_path.is_file():
        print("找不到输入日志: %s" % input_path)
        return 1

    try:
        events = extract_events(input_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        write_csv(events, output_path)
    except OSError as exc:
        print("处理失败: %s" % exc)
        return 1

    nozzle_comp_count = sum(event["event_type"] == "NOZZLE_COMP" for event in events)
    print_start_count = sum(event["event_type"] == "PRINT_START" for event in events)
    print(
        "已导出 %d 条事件至: %s（NOZZLE_COMP: %d，PRINT_START: %d）"
        % (len(events), output_path, nozzle_comp_count, print_start_count)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
