"""
PRTouch 探点日志分析工具插件

功能：
1. 解析 klippy.log 中的探点数据
2. 按坐标筛选、按调平筛选、按精归零筛选
3. 绘制波形数据（支持双Y轴显示SRC_WAVE）
4. 实时读取 dbg_ippt 数据并显示

适配 ToolPlatform 插件协议
"""

import re
import os
import json
import socket
import time
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from pathlib import Path
import ast

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QGroupBox, QLabel, QPushButton, QLineEdit, QComboBox,
    QRadioButton, QButtonGroup, QCheckBox, QTextEdit,
    QTreeWidget, QTreeWidgetItem, QFileDialog, QMessageBox,
    QHeaderView, QSpinBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont

# 可选依赖：matplotlib
try:
    import matplotlib
    matplotlib.use('QtAgg')
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
    from matplotlib.figure import Figure
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False


# ============================================================================
# 插件元数据
# ============================================================================

PLUGIN_META = {
    "id": "prtouch_log_analyzer",
    "name": "PRTouch日志分析",
    "version": "1.0.0",
    "category": "调试工具",
    "description": "分析PRTouch探点日志数据，绘制波形，支持实时监控",
    "order": 10,
}


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class ProbePoint:
    """单次探点数据"""
    timestamp: str = ""
    line_number: int = 0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    z_compensation: float = 0.0
    z_final: float = 0.0
    
    # 波形数据
    pres_data_ch0_ftr_wave: List[int] = field(default_factory=list)
    pres_data_ch0_src_wave: List[int] = field(default_factory=list)
    pres_data_ch0_cal_wave: List[float] = field(default_factory=list)
    pres_data_ch0_cal_turn: int = 0
    
    # 标志和卸力
    pres_data_flag: bool = True
    pres_unload_cnt: int = 0
    pres_unload_bst_indx: int = 0
    pres_unload_tol: int = 0
    
    # 所属分组
    group_type: str = ""  # "leveling", "accurate_g28", "probe"
    group_id: int = 0


@dataclass
class ProbeGroup:
    """探点组（一次调平或精归零）"""
    group_type: str = ""
    group_id: int = 0
    start_line: int = 0
    end_line: int = 0
    start_time: str = ""
    end_time: str = ""
    points: List[ProbePoint] = field(default_factory=list)
    probe_stats: Dict = field(default_factory=dict)


# ============================================================================
# 日志解析器
# ============================================================================

class LogParser:
    """日志解析器"""
    
    # 正则表达式模式
    TIMESTAMP_PATTERN = re.compile(r'\[INFO\] (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+)')
    
    PRES_DATA_CH0_FTR_WAVE = re.compile(r'\[PRES_DATA_CH0_FTR_WAVE\] \[(.+?)\]')
    PRES_DATA_CH0_SRC_WAVE = re.compile(r'\[PRES_DATA_CH0_SRC_WAVE\] \[(.+?)\]')
    PRES_DATA_CH0_CAL_WAVE = re.compile(r'\[PRES_DATA_CH0_CAL_WAVE\] \[(.+?)\]')
    PRES_DATA_CH0_CAL_TURN = re.compile(r'\[PRES_DATA_CH0_CAL_TURN\]?\s*(\d+)')
    PRES_DATA_FLAG = re.compile(r'\[PRES_DATA_FLAG\]?\s*(True|False)')
    PRES_UNLOAD = re.compile(r'\[PRES_UNLOAD\]?\s*unload_cnt=(\d+)\s+bst_indx=(\d+)\s+tol=(\d+)')
    
    PROBE_AT = re.compile(r'probe at ([\d.]+),([\d.]+) is z=([\d.-]+)(?: z_compensation=([\d.-]+))?')
    PROBE_AT_FINAL = re.compile(r'probe at ([\d.]+),([\d.]+) is z=([\d.-]+)$')
    
    BED_MESH_START = re.compile(r'BED_MESH_CALIBRATE|G29|bed_mesh: generated points')
    BED_MESH_END = re.compile(r'bed_mesh: Mesh Coverage|Mesh bed leveling complete')
    
    ACCURATE_G28_START = re.compile(r'ACCURATE_G28|accurate_g28|精归零.*开始|accu_g28_z')
    ACCURATE_G28_END = re.compile(r'精归零.*完成|accu_g28.*finish|accurate_g28.*done')
    
    PROBE_STATS = re.compile(r'PROBE_STATS[:\s]+(.+)')
    
    def __init__(self):
        self.probe_points: List[ProbePoint] = []
        self.probe_groups: List[ProbeGroup] = []
        self.current_point: Optional[ProbePoint] = None
        self.current_group: Optional[ProbeGroup] = None
        self.group_counter = 0
        
    def parse_file(self, filepath: str) -> Tuple[List[ProbePoint], List[ProbeGroup]]:
        """解析日志文件"""
        self.probe_points = []
        self.probe_groups = []
        self.current_point = None
        self.current_group = None
        self.group_counter = 0
        
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            for line_num, line in enumerate(f, 1):
                self._parse_line(line_num, line)
        
        # 完成最后一个点和组
        if self.current_point and self.current_point.pres_data_ch0_src_wave:
            self.probe_points.append(self.current_point)
            if self.current_group:
                self.current_group.points.append(self.current_point)
        
        if self.current_group and self.current_group.points:
            self.probe_groups.append(self.current_group)
        
        return self.probe_points, self.probe_groups
    
    def _parse_line(self, line_num: int, line: str):
        """解析单行"""
        ts_match = self.TIMESTAMP_PATTERN.search(line)
        timestamp = ts_match.group(1) if ts_match else ""
        
        # 检测调平开始
        if self.BED_MESH_START.search(line):
            self._finish_current_group()
            self.group_counter += 1
            self.current_group = ProbeGroup(
                group_type="leveling",
                group_id=self.group_counter,
                start_line=line_num,
                start_time=timestamp
            )
        
        # 检测调平结束
        elif self.BED_MESH_END.search(line):
            if self.current_group and self.current_group.group_type == "leveling":
                self.current_group.end_line = line_num
                self.current_group.end_time = timestamp
                self._finish_current_group()
        
        # 检测精归零开始
        elif self.ACCURATE_G28_START.search(line):
            self._finish_current_group()
            self.group_counter += 1
            self.current_group = ProbeGroup(
                group_type="accurate_g28",
                group_id=self.group_counter,
                start_line=line_num,
                start_time=timestamp
            )
        
        # 检测精归零结束
        elif self.ACCURATE_G28_END.search(line):
            if self.current_group and self.current_group.group_type == "accurate_g28":
                self.current_group.end_line = line_num
                self.current_group.end_time = timestamp
                self._finish_current_group()
        
        # PROBE_STATS
        stats_match = self.PROBE_STATS.search(line)
        if stats_match and self.current_group:
            try:
                stats_str = stats_match.group(1).strip()
                if stats_str.startswith('{'):
                    self.current_group.probe_stats = ast.literal_eval(stats_str)
            except:
                pass
        
        # 解析波形数据 - FTR_WAVE 标志新探点开始
        ftr_match = self.PRES_DATA_CH0_FTR_WAVE.search(line)
        if ftr_match:
            if self.current_point and self.current_point.pres_data_ch0_src_wave:
                self.probe_points.append(self.current_point)
                if self.current_group:
                    self.current_group.points.append(self.current_point)
            
            self.current_point = ProbePoint(
                timestamp=timestamp,
                line_number=line_num
            )
            if self.current_group:
                self.current_point.group_type = self.current_group.group_type
                self.current_point.group_id = self.current_group.group_id
            
            try:
                self.current_point.pres_data_ch0_ftr_wave = [int(x.strip()) for x in ftr_match.group(1).split(',')]
            except:
                pass
            return
        
        if not self.current_point:
            return
        
        # SRC_WAVE
        src_match = self.PRES_DATA_CH0_SRC_WAVE.search(line)
        if src_match:
            try:
                self.current_point.pres_data_ch0_src_wave = [int(x.strip()) for x in src_match.group(1).split(',')]
            except:
                pass
            return
        
        # CAL_WAVE
        cal_match = self.PRES_DATA_CH0_CAL_WAVE.search(line)
        if cal_match:
            try:
                self.current_point.pres_data_ch0_cal_wave = [float(x.strip()) for x in cal_match.group(1).split(',')]
            except:
                pass
            return
        
        # CAL_TURN
        turn_match = self.PRES_DATA_CH0_CAL_TURN.search(line)
        if turn_match:
            self.current_point.pres_data_ch0_cal_turn = int(turn_match.group(1))
            return
        
        # DATA_FLAG
        flag_match = self.PRES_DATA_FLAG.search(line)
        if flag_match:
            self.current_point.pres_data_flag = flag_match.group(1) == 'True'
            return
        
        # UNLOAD
        unload_match = self.PRES_UNLOAD.search(line)
        if unload_match:
            self.current_point.pres_unload_cnt = int(unload_match.group(1))
            self.current_point.pres_unload_bst_indx = int(unload_match.group(2))
            self.current_point.pres_unload_tol = int(unload_match.group(3))
            return
        
        # probe at (带 z_compensation)
        probe_match = self.PROBE_AT.search(line)
        if probe_match:
            self.current_point.x = float(probe_match.group(1))
            self.current_point.y = float(probe_match.group(2))
            self.current_point.z = float(probe_match.group(3))
            if probe_match.group(4):
                self.current_point.z_compensation = float(probe_match.group(4))
            return
        
        # probe at (最终z值)
        final_match = self.PROBE_AT_FINAL.search(line)
        if final_match and self.current_point.z != 0:
            self.current_point.z_final = float(final_match.group(3))
    
    def _finish_current_group(self):
        """完成当前组"""
        if self.current_point and self.current_point.pres_data_ch0_src_wave:
            self.probe_points.append(self.current_point)
            if self.current_group:
                self.current_group.points.append(self.current_point)
            self.current_point = None
        
        if self.current_group and self.current_group.points:
            self.probe_groups.append(self.current_group)
        self.current_group = None


# ============================================================================
# 实时数据接收器
# ============================================================================

class RealtimeReceiverThread(QThread):
    """实时数据接收线程（UDP）"""
    data_received = pyqtSignal(object)      # 发送 ProbePoint
    raw_received = pyqtSignal(str, str)     # (sender_addr, raw_text)
    error_occurred = pyqtSignal(str)
    status_update = pyqtSignal(str)         # 线程状态消息
    
    # 一个完整探点必须收齐的字段
    REQUIRED_FIELDS = ("ftr", "src", "cal", "turn", "probe")
    # 单个探点的组装超时（秒）
    POINT_TIMEOUT_SEC = 10.0
    
    def __init__(self, port=21021, parent=None):
        super().__init__(parent)
        self.port = port
        self.running = False
        self.sock = None
        self.current_point = None
        self.current_fields: set = set()   # 已收到的字段
        self.current_started_at = 0.0
        self.packet_count = 0
    
    def run(self):
        """线程主循环"""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # 允许广播接收
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except Exception:
                pass
            self.sock.bind(('0.0.0.0', self.port))
            self.sock.settimeout(1.0)
            self.running = True
            self.status_update.emit(f"已绑定 0.0.0.0:{self.port}，等待数据...")
            
            while self.running:
                try:
                    data, addr = self.sock.recvfrom(65535)
                    self.packet_count += 1
                    text = data.decode('utf-8', errors='ignore')
                    sender = f"{addr[0]}:{addr[1]}"
                    self.raw_received.emit(sender, text)
                    self._parse_data(text)
                except socket.timeout:
                    continue
                except Exception as e:
                    if self.running:
                        self.error_occurred.emit(f"接收异常: {e}")
        except OSError as e:
            diag = self._diagnose_bind_error(e)
            self.error_occurred.emit(f"绑定端口 {self.port} 失败：{e}\n\n{diag}")
        except Exception as e:
            self.error_occurred.emit(f"启动失败: {e}")
        finally:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
    
    def _diagnose_bind_error(self, err: OSError) -> str:
        """诊断绑定错误并给出建议"""
        winerr = getattr(err, 'winerror', None)
        
        if winerr == 10013:  # WSAEACCES
            reasons = [
                "端口 {} 被系统禁止绑定。常见原因：".format(self.port),
                "",
                "1. 端口落在 Windows 保留端口范围（Hyper-V/WSL/Docker）",
                "   查看保留范围：",
                "     netsh int ipv4 show excludedportrange protocol=udp",
                "",
                "2. 端口被其他程序占用",
                "   查看占用：",
                "     netstat -ano | findstr :{}".format(self.port),
                "",
                "3. 防火墙/杀毒软件拦截 python.exe 的入站",
                "",
                "解决方法：",
                "  · 换一个端口（推荐 10000~20000 之间的高位端口，如 10021、15001）",
                "  · 或以管理员身份运行平台",
                "  · 或将 Python 加入防火墙白名单",
            ]
            # 尝试找一个可用端口推荐给用户
            free_port = self._find_free_udp_port()
            if free_port:
                reasons.append(f"")
                reasons.append(f"检测到可用端口建议：{free_port}")
            return "\n".join(reasons)
        
        if winerr == 10048:  # WSAEADDRINUSE
            return (
                f"端口 {self.port} 已被其他程序占用。\n"
                f"查看占用：netstat -ano | findstr :{self.port}\n"
                f"请换一个端口试试。"
            )
        
        return "请换一个端口（推荐 10000 以上），或检查防火墙设置。"
    
    @staticmethod
    def _find_free_udp_port(start: int = 10021, end: int = 10100) -> Optional[int]:
        """从 start 开始找一个可以 bind 的 UDP 端口"""
        for p in range(start, end):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.bind(('0.0.0.0', p))
                s.close()
                return p
            except OSError:
                try:
                    s.close()
                except Exception:
                    pass
                continue
        return None
    
    def stop(self):
        """停止接收"""
        self.running = False
        self.wait(2000)
    
    def _parse_data(self, data: str):
        """解析接收到的数据。
        
        兼容多种常见格式：
        - "TITLE$TIME$MSG"         （$ 分隔）
        - "TITLE|TIME|MSG"         （| 分隔）
        - "TITLE: MSG"
        - 原始日志行（含 [TAG] [...]）
        """
        text = data.strip()
        if not text:
            return
        
        # 检查是否包含感兴趣的关键字（提高容错）
        has_keyword = any(k in text for k in (
            'PRES_DATA_CH0_FTR_WAVE', 'PRES_DATA_CH0_SRC_WAVE',
            'PRES_DATA_CH0_CAL_WAVE', 'PRES_DATA_CH0_CAL_TURN',
            'PRES_DATA_FLAG', 'PRES_UNLOAD', 'probe at'
        ))
        if not has_keyword:
            return
        
        # 分隔符兼容
        if '$' in text and text.count('$') >= 2:
            parts = text.split('$')
            title, time_str, msg = parts[0], parts[1], '$'.join(parts[2:])
        elif '|' in text and text.count('|') >= 2:
            parts = text.split('|')
            title, time_str, msg = parts[0], parts[1], '|'.join(parts[2:])
        else:
            # 没有明确分隔符，整段当作 msg
            title = text
            time_str = ""
            msg = text
        
        now = time.time()
        
        # 过期清理：组装太久还没齐，丢弃
        if (self.current_point and
                now - self.current_started_at > self.POINT_TIMEOUT_SEC):
            self.status_update.emit(
                f"探点组装超时（已持续 {now - self.current_started_at:.1f}s"
                f"，缺失 {self.REQUIRED_FIELDS - frozenset(self.current_fields)}），丢弃"
            )
            self._reset_point()
        
        # FTR_WAVE 表示一个新探点开始
        if 'PRES_DATA_CH0_FTR_WAVE' in title or 'PRES_DATA_CH0_FTR_WAVE' in msg:
            # 若上一个点没组装完就来了新 FTR_WAVE，视为不完整，丢弃
            if self.current_point and not self._is_complete():
                missing = set(self.REQUIRED_FIELDS) - self.current_fields
                self.status_update.emit(f"上一个探点不完整（缺 {missing}），已丢弃")
            
            self.current_point = ProbePoint(timestamp=time_str)
            self.current_fields = set()
            self.current_started_at = now
            
            match = re.search(r'\[([^\[\]]+)\]', msg)
            if match:
                try:
                    self.current_point.pres_data_ch0_ftr_wave = [
                        int(float(x.strip())) for x in match.group(1).split(',') if x.strip()
                    ]
                    self.current_fields.add('ftr')
                except Exception:
                    pass
            return
        
        if not self.current_point:
            # 没有正在组装的点，直接丢弃（等下一个 FTR_WAVE）
            return
        
        matched = False
        
        if 'PRES_DATA_CH0_SRC_WAVE' in title or 'PRES_DATA_CH0_SRC_WAVE' in msg:
            match = re.search(r'\[([^\[\]]+)\]', msg)
            if match:
                try:
                    self.current_point.pres_data_ch0_src_wave = [
                        int(float(x.strip())) for x in match.group(1).split(',') if x.strip()
                    ]
                    self.current_fields.add('src')
                    matched = True
                except Exception:
                    pass
        
        elif 'PRES_DATA_CH0_CAL_WAVE' in title or 'PRES_DATA_CH0_CAL_WAVE' in msg:
            match = re.search(r'\[([^\[\]]+)\]', msg)
            if match:
                try:
                    self.current_point.pres_data_ch0_cal_wave = [
                        float(x.strip()) for x in match.group(1).split(',') if x.strip()
                    ]
                    self.current_fields.add('cal')
                    matched = True
                except Exception:
                    pass
        
        elif 'PRES_DATA_CH0_CAL_TURN' in title or 'PRES_DATA_CH0_CAL_TURN' in msg:
            m = re.search(r'(\d+)', msg)
            if m:
                self.current_point.pres_data_ch0_cal_turn = int(m.group(1))
                self.current_fields.add('turn')
                matched = True
        
        elif 'PRES_DATA_FLAG' in title or 'PRES_DATA_FLAG' in msg:
            self.current_point.pres_data_flag = 'True' in msg
            # FLAG 可选，不计入必备字段
            matched = True
        
        elif 'PRES_UNLOAD' in title or 'PRES_UNLOAD' in msg:
            match = re.search(r'unload_cnt=(\d+)\s+bst_indx=(\d+)\s+tol=(\d+)', msg)
            if match:
                self.current_point.pres_unload_cnt = int(match.group(1))
                self.current_point.pres_unload_bst_indx = int(match.group(2))
                self.current_point.pres_unload_tol = int(match.group(3))
                matched = True
        
        elif 'probe at' in msg.lower():
            match = re.search(r'probe at ([\d.]+),([\d.]+) is z=([\d.-]+)', msg)
            if match:
                self.current_point.x = float(match.group(1))
                self.current_point.y = float(match.group(2))
                self.current_point.z = float(match.group(3))
                self.current_fields.add('probe')
                matched = True
        
        # 字段齐了才发射
        if matched and self._is_complete():
            self.data_received.emit(self.current_point)
            self._reset_point()
    
    def _is_complete(self) -> bool:
        """当前探点是否已收齐所有必备字段"""
        return set(self.REQUIRED_FIELDS).issubset(self.current_fields)
    
    def _reset_point(self):
        """重置当前组装状态"""
        self.current_point = None
        self.current_fields = set()
        self.current_started_at = 0.0


# ============================================================================
# Matplotlib 绘图组件
# ============================================================================

class PlotCanvas(FigureCanvas if HAS_MATPLOTLIB else QWidget):
    """Matplotlib 绘图画布"""
    
    def __init__(self, parent=None):
        if HAS_MATPLOTLIB:
            self.fig = Figure(figsize=(8, 5), dpi=100)
            super().__init__(self.fig)
            self.setParent(parent)
        else:
            super().__init__(parent)
            layout = QVBoxLayout(self)
            layout.addWidget(QLabel("matplotlib 未安装，绘图功能不可用\n请运行: pip install matplotlib"))
    
    def plot_point(self, point: ProbePoint, plot_type: str = "all", dual_axis: bool = True):
        """绘制探点数据
        
        标注规则：
        - CAL_WAVE 上所有点都是普通小圆点
        - CAL_TURN 处点用稍大的点 + 文字标注
        - CAL_TURN 之后的每个点标注 (索引, pres_data值)，文字放在点左侧
          pres_data = SRC_WAVE[i]（真实压力值）
        """
        if not HAS_MATPLOTLIB:
            return
        
        self.fig.clear()
        cal_turn = point.pres_data_ch0_cal_turn
        src_wave = point.pres_data_ch0_src_wave
        
        # 线径与点径配置（细线、小点，不花哨）
        line_width = 0.9
        marker_size = 3            # 普通采样点
        marker_size_turn = 7       # CAL_TURN 点
        marker_size_after = 5      # CAL_TURN 之后的点
        
        if plot_type == "all":
            if dual_axis:
                ax1 = self.fig.add_subplot(111)
                ax2 = ax1.twinx()
            else:
                ax1 = self.fig.add_subplot(111)
                ax2 = ax1
            
            # FTR_WAVE
            if point.pres_data_ch0_ftr_wave:
                x = list(range(len(point.pres_data_ch0_ftr_wave)))
                ax1.plot(x, point.pres_data_ch0_ftr_wave, 'b-',
                         marker='o', markersize=marker_size,
                         linewidth=line_width, label='FTR_WAVE', alpha=0.85)
            
            # CAL_WAVE
            if point.pres_data_ch0_cal_wave:
                x = list(range(len(point.pres_data_ch0_cal_wave)))
                ax1.plot(x, point.pres_data_ch0_cal_wave, 'g-',
                         marker='o', markersize=marker_size,
                         linewidth=line_width, label='CAL_WAVE', alpha=0.85)
            
            # SRC_WAVE
            if src_wave:
                x = list(range(len(src_wave)))
                target_ax = ax2 if dual_axis else ax1
                target_ax.plot(x, src_wave, 'r-',
                               marker='o', markersize=marker_size,
                               linewidth=line_width, label='SRC_WAVE', alpha=0.85)
                if dual_axis:
                    ax2.set_ylabel('SRC_WAVE (ADC)', color='r')
                    ax2.tick_params(axis='y', labelcolor='r')
            
            # CAL_TURN 标注（在 CAL_WAVE 上）
            self._annotate_cal_turn(
                ax1,
                cal_wave=point.pres_data_ch0_cal_wave,
                src_wave=src_wave,
                cal_turn=cal_turn,
                marker_size_turn=marker_size_turn,
                marker_size_after=marker_size_after,
            )
            
            ax1.set_xlabel('Sample Index')
            ax1.set_ylabel('FTR/CAL Value')
            ax1.grid(True, alpha=0.3)
            ax1.set_title(f'Probe ({point.x:.2f}, {point.y:.2f}) Z={point.z:.4f}')
            
            # 图例放到图外顶部，不占用数据空间
            lines1, labels1 = ax1.get_legend_handles_labels()
            if dual_axis and src_wave:
                lines2, labels2 = ax2.get_legend_handles_labels()
                lines1 += lines2
                labels1 += labels2
            if lines1:
                ax1.legend(lines1, labels1,
                           loc='lower center', bbox_to_anchor=(0.5, 1.02),
                           ncol=len(lines1), frameon=False, fontsize=9)
            
        else:
            ax = self.fig.add_subplot(111)
            data_list = None
            color = 'b'
            label = ''
            
            if plot_type == "ftr" and point.pres_data_ch0_ftr_wave:
                data_list = point.pres_data_ch0_ftr_wave
                color = 'b'
                label = 'FTR_WAVE'
                ax.set_ylabel('FTR Value')
            elif plot_type == "src" and src_wave:
                data_list = src_wave
                color = 'r'
                label = 'SRC_WAVE'
                ax.set_ylabel('SRC Value (ADC)')
            elif plot_type == "cal" and point.pres_data_ch0_cal_wave:
                data_list = point.pres_data_ch0_cal_wave
                color = 'g'
                label = 'CAL_WAVE'
                ax.set_ylabel('CAL Value')
            
            if data_list:
                x = list(range(len(data_list)))
                ax.plot(x, data_list, color=color, linestyle='-',
                        marker='o', markersize=marker_size,
                        linewidth=line_width, label=label, alpha=0.85)
                
                # 单图模式也标注 CAL_TURN（基于当前曲线）
                self._annotate_cal_turn(
                    ax,
                    cal_wave=data_list,
                    src_wave=src_wave,
                    cal_turn=cal_turn,
                    marker_size_turn=marker_size_turn,
                    marker_size_after=marker_size_after,
                )
            
            ax.set_xlabel('Sample Index')
            ax.grid(True, alpha=0.3)
            ax.set_title(label if label else 'Waveform')
            
            if data_list:
                ax.legend(loc='lower center', bbox_to_anchor=(0.5, 1.02),
                          ncol=1, frameon=False, fontsize=9)
        
        # 顶部仅为图例留出必要空间，不再过度扩 Y 轴
        self.fig.subplots_adjust(top=0.92, bottom=0.1, left=0.08, right=0.95)
        # Y 轴给上下各留一点安全带（用于标注的基础偏移）
        for ax_obj in self.fig.axes:
            self._expand_ylim(ax_obj, top_ratio=0.06, bottom_ratio=0.06)
        self.draw()
        
        # 画完一次后对主轴的标注做像素级避让（必须先 draw 才能拿到文字框）
        if self.fig.axes:
            self._resolve_label_overlaps(self.fig.axes[0])
            self.draw()
    
    @staticmethod
    def _expand_ylim(ax, top_ratio=0.06, bottom_ratio=0.06):
        """轻微扩展 Y 轴上下空间"""
        lo, hi = ax.get_ylim()
        span = hi - lo
        if span <= 0:
            return
        ax.set_ylim(lo - span * bottom_ratio, hi + span * top_ratio)
    
    def _resolve_label_overlaps(self, ax, max_iter=8, step=6):
        """像素级标注避让：
        
        先拿到所有候选标注的像素外接矩形，与同轴其他标注及画布边界比较，
        发生重叠就沿已有方向的 y（向上为主）迭代推开，最多 max_iter 轮。
        """
        anns = getattr(ax, '_pres_annotations', None)
        if not anns:
            return
        renderer = self.fig.canvas.get_renderer()
        fig_bbox = self.fig.bbox
        
        for _ in range(max_iter):
            moved = False
            # 先更新所有 bbox
            bboxes = []
            for a in anns:
                bb = a.get_window_extent(renderer=renderer)
                bboxes.append(bb)
            
            for idx, a in enumerate(anns):
                bb = bboxes[idx]
                # 1) 若超出画布顶部/底部，拉回画布内
                if bb.ymax > fig_bbox.ymax - 4:
                    self._shift_annotation(a, 0, -(bb.ymax - (fig_bbox.ymax - 4)))
                    moved = True
                    continue
                if bb.ymin < fig_bbox.ymin + 4:
                    self._shift_annotation(a, 0, (fig_bbox.ymin + 4 - bb.ymin))
                    moved = True
                    continue
                # 2) 与其他标注比较
                for jdx, b in enumerate(anns):
                    if jdx <= idx:
                        continue
                    bb2 = bboxes[jdx]
                    if not bb.overlaps(bb2):
                        continue
                    # 决定谁向上谁向下：y 大的继续向上，y 小的向下
                    overlap_y = min(bb.ymax, bb2.ymax) - max(bb.ymin, bb2.ymin)
                    if overlap_y <= 0:
                        continue
                    push = overlap_y / 2 + step
                    if bb.y0 >= bb2.y0:
                        self._shift_annotation(a, 0, push)
                        self._shift_annotation(b, 0, -push)
                    else:
                        self._shift_annotation(a, 0, -push)
                        self._shift_annotation(b, 0, push)
                    moved = True
            if not moved:
                break
    
    @staticmethod
    def _shift_annotation(annotation, dx: float, dy: float):
        """在当前偏移量上再叠加 (dx, dy) 像素位移"""
        cur = annotation.xyann  # 当前 (x_offset, y_offset) in points
        # xyann 是 points 不是 pixels；1pt ≈ 1.333px。这里做近似处理。
        annotation.set_position((cur[0] + dx * 0.75, cur[1] + dy * 0.75))
    
    @staticmethod
    def _annotate_cal_turn(ax, cal_wave, src_wave, cal_turn,
                            marker_size_turn=7, marker_size_after=5):
        """标注 CAL_TURN 及其后的点。
        
        - 蓝色小圆点
        - 文字优先放在点的**左侧**；若左侧有曲线数据遮挡则改放**右侧**
        - 重叠由 PlotCanvas._resolve_label_overlaps 迭代解决（只做上下推开）
        """
        if cal_turn <= 0 or not cal_wave:
            return
        
        # 竖直参考线
        ax.axvline(x=cal_turn, color='purple', linestyle='--',
                   linewidth=1.0, alpha=0.5, zorder=3)
        
        def _fmt(v):
            if isinstance(v, float):
                return f'{v:.0f}' if abs(v) >= 10 else f'{v:.2f}'
            return f'{v}'
        
        annotate_color = '#1f6fe0'
        indices = list(range(cal_turn, len(cal_wave)))
        if not indices:
            return
        
        # 估算文字宽度覆盖的 x 范围（近似：一个字符 0.6 单位）
        # 实际宽度由字体决定，这里只用于决策方向
        def _label_x_span(i: int, y: float) -> float:
            text = f'({i}, {_fmt(y)})'
            return max(0.8, len(text) * 0.4)  # x 轴单位
        
        def _side_for(i: int) -> str:
            """返回 'left' 或 'right'：
            
            优先左侧；若点 i 左侧 _label_x_span 范围内有曲线采样点（即 cal_wave
            在 [i - span, i - 1] 里存在），则改放右侧。
            """
            y = cal_wave[i]
            span = _label_x_span(i, y)
            # 左侧是否有曲线？只要 i >= 1 就说明左侧必有曲线
            # 但如果左侧的曲线上下与当前点差距很小，会被文字框压住
            if i - 1 < 0:
                return 'left'   # 真·无左侧
            # 查看左侧 span 内曲线的 y 值范围与当前 y 的垂直距离
            start = max(0, int(i - span))
            end = i  # 不含当前
            left_segment = cal_wave[start:end]
            if not left_segment:
                return 'left'
            # 若左侧曲线有任何一点 y 与当前 y 很近（垂直距离小于 25% Y 跨度），
            # 认为左侧会被文字框遮挡，改右侧
            y_span = (max(cal_wave) - min(cal_wave)) or 1
            min_dy = min(abs(ly - y) for ly in left_segment)
            if min_dy < y_span * 0.08:
                # 再检查右侧是否也有曲线（通常也有，但右侧只需避开本点右边一小段）
                return 'right'
            return 'left'
        
        annotations = []
        for i in indices:
            y_val = cal_wave[i]
            if i == cal_turn:
                ax.plot(i, y_val, 'o',
                        color=annotate_color, markersize=marker_size_turn,
                        markeredgecolor='white', markeredgewidth=1.2,
                        zorder=11)
                prefix = 'TURN '
                fw = 'bold'
            else:
                ax.plot(i, y_val, 'o',
                        color=annotate_color, markersize=marker_size_after,
                        markeredgecolor='white', markeredgewidth=0.6,
                        zorder=9)
                prefix = ''
                fw = 'normal'
            
            side = _side_for(i)
            # 左侧：文字右端贴近点 → xytext 负 x，ha='right'
            # 右侧：文字左端贴近点 → xytext 正 x，ha='left'
            if side == 'left':
                xytext = (-8, 0)
                ha = 'right'
            else:
                xytext = (8, 0)
                ha = 'left'
            
            ann = ax.annotate(f'{prefix}({i}, {_fmt(y_val)})',
                              xy=(i, y_val),
                              xytext=xytext, textcoords='offset points',
                              fontsize=8, color=annotate_color, fontweight=fw,
                              ha=ha, va='center',
                              bbox=dict(boxstyle='round,pad=0.15',
                                        facecolor='white', edgecolor='none', alpha=0.85),
                              zorder=12)
            annotations.append(ann)
        
        # 挂到 ax 上让 PlotCanvas._resolve_label_overlaps 能拿到
        ax._pres_annotations = annotations
    
    def clear_plot(self):
        """清空图表"""
        if HAS_MATPLOTLIB:
            self.fig.clear()
            self.draw()


# ============================================================================
# 主插件 Widget
# ============================================================================

class PRTouchLogAnalyzerWidget(QWidget):
    """PRTouch 日志分析插件主界面"""
    
    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        
        # 配置文件路径 (使用 ToolPlatform 的 PluginContext API)
        self.config_path = self.context.plugin_config_file("settings.json")
        
        # 数据
        self.parser = LogParser()
        self.probe_points: List[ProbePoint] = []
        self.probe_groups: List[ProbeGroup] = []
        self.filtered_points: List[ProbePoint] = []
        
        # 实时监控
        self.realtime_thread: Optional[RealtimeReceiverThread] = None
        self.realtime_points: List[ProbePoint] = []
        self._rt_packet_count = 0
        
        # 配置
        self.config = self._load_config()
        
        self._setup_ui()
        self._apply_config()
    
    def _load_config(self) -> dict:
        """加载配置"""
        default_config = {
            "last_log_path": "",
            "realtime_port": 21021,
            "dual_axis": True,
            "plot_type": "all"
        }
        try:
            if self.config_path.exists():
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                default_config.update(data)
        except Exception:
            pass
        return default_config
    
    def _save_config(self):
        """保存配置"""
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(
                json.dumps(self.config, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            self.context.show_status(f"配置保存失败: {e}", 5000)
    
    def _apply_config(self):
        """应用配置到界面"""
        if self.config.get("last_log_path"):
            self.file_path_edit.setText(self.config["last_log_path"])
        self.realtime_port_spin.setValue(self.config.get("realtime_port", 21021))
        self.dual_axis_cb.setChecked(self.config.get("dual_axis", True))
    
    def _setup_ui(self):
        """构建界面"""
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(5, 5, 5, 5)
        
        # 使用 Splitter 分割左右面板
        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)
        
        # ==================== 左侧控制面板 ====================
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        # 左侧只保留一个基础最小宽度，筛选条件行能完整展示
        left_widget.setMinimumWidth(360)
        
        # --- 文件加载区 ---
        file_group = QGroupBox("日志文件")
        file_layout = QHBoxLayout(file_group)
        
        self.file_path_edit = QLineEdit()
        self.file_path_edit.setPlaceholderText("选择 klippy.log 文件...")
        file_layout.addWidget(self.file_path_edit, 1)
        
        browse_btn = QPushButton("浏览")
        browse_btn.clicked.connect(self._browse_file)
        file_layout.addWidget(browse_btn)
        
        load_btn = QPushButton("加载")
        load_btn.clicked.connect(self._load_file)
        file_layout.addWidget(load_btn)
        
        left_layout.addWidget(file_group)
        
        # --- 筛选条件（合一） ---
        filter_group = QGroupBox("筛选条件")
        fg_v = QVBoxLayout(filter_group)
        fg_v.setSpacing(4)
        
        # 第一行：分组选择 + 重置
        row_group = QHBoxLayout()
        row_group.addWidget(QLabel("分组:"))
        self.group_combo = QComboBox()
        self.group_combo.setMinimumWidth(180)
        self.group_combo.currentIndexChanged.connect(self._apply_filter)
        row_group.addWidget(self.group_combo, 1)
        
        self.btn_reset_filter = QPushButton("重置")
        self.btn_reset_filter.setToolTip("一键重置所有筛选条件，显示全部探点")
        self.btn_reset_filter.clicked.connect(self._reset_filter)
        row_group.addWidget(self.btn_reset_filter)
        fg_v.addLayout(row_group)
        
        # 第二行：坐标筛选
        row_coord = QHBoxLayout()
        row_coord.addWidget(QLabel("X:"))
        self.coord_x_combo = QComboBox()
        self.coord_x_combo.setMinimumWidth(90)
        self.coord_x_combo.currentIndexChanged.connect(self._apply_filter)
        row_coord.addWidget(self.coord_x_combo, 1)
        
        row_coord.addSpacing(8)
        row_coord.addWidget(QLabel("Y:"))
        self.coord_y_combo = QComboBox()
        self.coord_y_combo.setMinimumWidth(90)
        self.coord_y_combo.currentIndexChanged.connect(self._apply_filter)
        row_coord.addWidget(self.coord_y_combo, 1)
        fg_v.addLayout(row_coord)
        
        # 筛选结果摘要
        self.filter_summary_label = QLabel("请先加载日志文件")
        self.filter_summary_label.setStyleSheet("color: #888; font-size: 11px;")
        fg_v.addWidget(self.filter_summary_label)
        
        left_layout.addWidget(filter_group)
        
        # --- 探点列表 ---
        list_group = QGroupBox("探点列表")
        list_layout = QVBoxLayout(list_group)
        list_layout.setContentsMargins(6, 10, 6, 6)
        
        self.point_tree = QTreeWidget()
        self.point_tree.setHeaderLabels([
            '#', '分组', '坐标(X,Y)', 'Z', '补偿', '拐点', 'FLAG', '卸力'
        ])
        self.point_tree.setAlternatingRowColors(True)
        self.point_tree.setRootIsDecorated(False)
        self.point_tree.setUniformRowHeights(True)
        self.point_tree.itemClicked.connect(self._on_point_select)
        
        # 更大的字体、更舒展的行高
        tree_font = QFont("Consolas", 10)
        self.point_tree.setFont(tree_font)
        
        # 列宽：让所有列都显示；每列按内容自适应，且用户可以继续拉动
        header = self.point_tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setMinimumSectionSize(28)
        
        list_layout.addWidget(self.point_tree)
        # 列表区占据剩余所有空间
        left_layout.addWidget(list_group, 10)
        
        # --- 实时监控 ---
        realtime_group = QGroupBox("实时监控 (UDP)")
        rt_v = QVBoxLayout(realtime_group)
        
        # 第一行：端口 + 按钮
        rt_row1 = QHBoxLayout()
        rt_row1.addWidget(QLabel("UDP端口:"))
        self.realtime_port_spin = QSpinBox()
        self.realtime_port_spin.setRange(1024, 65535)
        self.realtime_port_spin.setValue(21021)
        rt_row1.addWidget(self.realtime_port_spin)
        
        self.realtime_btn = QPushButton("开始监控")
        self.realtime_btn.clicked.connect(self._toggle_realtime)
        rt_row1.addWidget(self.realtime_btn)
        
        self.btn_clear_rt_log = QPushButton("清空日志")
        self.btn_clear_rt_log.clicked.connect(lambda: self.rt_log_text.clear())
        rt_row1.addWidget(self.btn_clear_rt_log)
        
        rt_row1.addStretch()
        rt_v.addLayout(rt_row1)
        
        # 第二行：状态 + 包数
        rt_row2 = QHBoxLayout()
        self.realtime_status_label = QLabel("未启动")
        self.realtime_status_label.setStyleSheet("color: gray;")
        rt_row2.addWidget(self.realtime_status_label)
        rt_row2.addStretch()
        self.realtime_count_label = QLabel("包:0 | 点:0")
        self.realtime_count_label.setStyleSheet("color: #888;")
        rt_row2.addWidget(self.realtime_count_label)
        rt_v.addLayout(rt_row2)
        
        # 第三行：原始数据日志（用于调试没数据的问题）
        self.rt_log_text = QTextEdit()
        self.rt_log_text.setReadOnly(True)
        self.rt_log_text.setMaximumHeight(140)
        self.rt_log_text.setFont(QFont("Consolas", 8))
        self.rt_log_text.setPlaceholderText(
            "监听开启后，这里会显示收到的原始 UDP 数据。\n"
            "若此处无数据，说明没有包到达此端口。\n"
            "请确认：\n"
            "  1. 发送端 IP/端口 指向本机\n"
            "  2. Windows 防火墙已放行 python.exe 的入站 UDP\n"
            "  3. 同一网络 / VPN 可达"
        )
        rt_v.addWidget(self.rt_log_text)
        
        left_layout.addWidget(realtime_group)
        
        splitter.addWidget(left_widget)
        
        # ==================== 右侧绘图区 ====================
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        
        # --- 绘图控制 ---
        plot_ctrl_layout = QHBoxLayout()
        
        plot_ctrl_layout.addWidget(QLabel("数据类型:"))
        
        self.plot_type_group = QButtonGroup(self)
        
        self.plot_all_rb = QRadioButton("全部")
        self.plot_all_rb.setChecked(True)
        self.plot_type_group.addButton(self.plot_all_rb, 0)
        plot_ctrl_layout.addWidget(self.plot_all_rb)
        
        self.plot_ftr_rb = QRadioButton("FTR_WAVE")
        self.plot_type_group.addButton(self.plot_ftr_rb, 1)
        plot_ctrl_layout.addWidget(self.plot_ftr_rb)
        
        self.plot_src_rb = QRadioButton("SRC_WAVE")
        self.plot_type_group.addButton(self.plot_src_rb, 2)
        plot_ctrl_layout.addWidget(self.plot_src_rb)
        
        self.plot_cal_rb = QRadioButton("CAL_WAVE")
        self.plot_type_group.addButton(self.plot_cal_rb, 3)
        plot_ctrl_layout.addWidget(self.plot_cal_rb)
        
        self.plot_type_group.buttonClicked.connect(self._update_plot)
        
        plot_ctrl_layout.addSpacing(20)
        
        self.dual_axis_cb = QCheckBox("双Y轴显示SRC")
        self.dual_axis_cb.setChecked(True)
        self.dual_axis_cb.stateChanged.connect(self._update_plot)
        plot_ctrl_layout.addWidget(self.dual_axis_cb)
        
        plot_ctrl_layout.addStretch()
        right_layout.addLayout(plot_ctrl_layout)
        
        # --- 绘图画布 ---
        self.plot_canvas = PlotCanvas(self)
        right_layout.addWidget(self.plot_canvas, 1)
        
        # 工具栏
        if HAS_MATPLOTLIB:
            self.plot_toolbar = NavigationToolbar(self.plot_canvas, self)
            right_layout.addWidget(self.plot_toolbar)
        
        # --- 详情面板 ---
        detail_group = QGroupBox("探点详情")
        detail_layout = QVBoxLayout(detail_group)
        
        self.detail_text = QTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setMaximumHeight(120)
        self.detail_text.setFont(QFont("Consolas", 9))
        detail_layout.addWidget(self.detail_text)
        
        right_layout.addWidget(detail_group)
        
        splitter.addWidget(right_widget)
        
        # 设置分割比例
        # 初始比例 3:7；用户可自由拖动
        splitter.setSizes([420, 980])
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 7)
        splitter.setChildrenCollapsible(False)
    
    def _browse_file(self):
        """浏览文件"""
        filepath, _ = QFileDialog.getOpenFileName(
            self, "选择日志文件", "",
            "Log files (*.log);;All files (*.*)"
        )
        if filepath:
            self.file_path_edit.setText(filepath)
    
    def _load_file(self):
        """加载日志文件"""
        filepath = self.file_path_edit.text()
        if not filepath or not os.path.exists(filepath):
            QMessageBox.warning(self, "错误", "请选择有效的日志文件")
            return
        
        try:
            self.context.show_status(f"正在解析: {os.path.basename(filepath)}...", 0)
            self.probe_points, self.probe_groups = self.parser.parse_file(filepath)
            self._update_ui_after_load()
            
            # 保存配置
            self.config["last_log_path"] = filepath
            self._save_config()
            
            msg = f"已加载 {len(self.probe_points)} 个探点，{len(self.probe_groups)} 个分组"
            self.context.show_status(msg, 5000)
            QMessageBox.information(self, "成功", msg)
        except Exception as e:
            self.context.show_status(f"加载失败: {e}", 10000)
            QMessageBox.critical(self, "错误", f"加载失败: {e}")
    
    def _update_ui_after_load(self):
        """加载后更新UI。使用统一的筛选器，坐标和分组可以自由组合。"""
        # —— 更新坐标下拉框 ——
        x_coords = sorted(set(f"{p.x:.3f}" for p in self.probe_points if p.x != 0),
                          key=lambda s: float(s))
        y_coords = sorted(set(f"{p.y:.3f}" for p in self.probe_points if p.y != 0),
                          key=lambda s: float(s))
        
        # 暂停信号，避免每次 addItem 都触发 _apply_filter
        self.coord_x_combo.blockSignals(True)
        self.coord_y_combo.blockSignals(True)
        self.group_combo.blockSignals(True)
        
        self.coord_x_combo.clear()
        self.coord_x_combo.addItem("全部 X")
        self.coord_x_combo.addItems(x_coords)
        
        self.coord_y_combo.clear()
        self.coord_y_combo.addItem("全部 Y")
        self.coord_y_combo.addItems(y_coords)
        
        # —— 更新分组下拉框 ——
        # 第一项为「全部探点」；其后按调平、精归零分区展示
        self.group_combo.clear()
        self.group_combo.addItem(f"全部探点（共 {len(self.probe_points)} 点）", userData=None)
        
        leveling_groups = [g for g in self.probe_groups if g.group_type == "leveling"]
        g28_groups = [g for g in self.probe_groups if g.group_type == "accurate_g28"]
        
        if leveling_groups:
            total = sum(len(g.points) for g in leveling_groups)
            self.group_combo.addItem(f"── 所有调平（{len(leveling_groups)} 组 / {total} 点）",
                                      userData=("leveling_all", None))
            for g in leveling_groups:
                label = f"  调平 #{g.group_id} · {len(g.points)}点 · {g.start_time}"
                self.group_combo.addItem(label, userData=("group", g.group_id))
        
        if g28_groups:
            total = sum(len(g.points) for g in g28_groups)
            self.group_combo.addItem(f"── 所有精归零（{len(g28_groups)} 组 / {total} 点）",
                                      userData=("g28_all", None))
            for g in g28_groups:
                label = f"  精归零 #{g.group_id} · {len(g.points)}点 · {g.start_time}"
                self.group_combo.addItem(label, userData=("group", g.group_id))
        
        self.coord_x_combo.blockSignals(False)
        self.coord_y_combo.blockSignals(False)
        self.group_combo.blockSignals(False)
        
        # 应用筛选
        self._apply_filter()
    
    def _reset_filter(self):
        """一键重置所有筛选条件"""
        self.coord_x_combo.blockSignals(True)
        self.coord_y_combo.blockSignals(True)
        self.group_combo.blockSignals(True)
        self.coord_x_combo.setCurrentIndex(0)
        self.coord_y_combo.setCurrentIndex(0)
        self.group_combo.setCurrentIndex(0)
        self.coord_x_combo.blockSignals(False)
        self.coord_y_combo.blockSignals(False)
        self.group_combo.blockSignals(False)
        self._apply_filter()
    
    def _apply_filter(self):
        """统一筛选：分组 ∩ X坐标 ∩ Y坐标。参数任意组合自由联动。"""
        if not self.probe_points:
            self.filtered_points = []
            self._update_point_list()
            self._update_filter_summary(source="")
            return
        
        # 1) 从分组决定基础集合
        source_desc = "全部探点"
        group_data = self.group_combo.currentData()
        if group_data is None:
            base = self.probe_points
        else:
            kind, gid = group_data
            if kind == "leveling_all":
                base = [p for p in self.probe_points if p.group_type == "leveling"]
                source_desc = "所有调平"
            elif kind == "g28_all":
                base = [p for p in self.probe_points if p.group_type == "accurate_g28"]
                source_desc = "所有精归零"
            elif kind == "group":
                target = next((g for g in self.probe_groups if g.group_id == gid), None)
                if target:
                    base = list(target.points)
                    type_name = "调平" if target.group_type == "leveling" else "精归零"
                    source_desc = f"{type_name} #{target.group_id}"
                    # 顺便显示 PROBE_STATS
                    if target.probe_stats:
                        stats_str = json.dumps(target.probe_stats, indent=2, ensure_ascii=False)
                        self.detail_text.setText(f"PROBE_STATS ({source_desc}):\n{stats_str}")
                else:
                    base = []
            else:
                base = self.probe_points
        
        # 2) 叠加坐标筛选
        x_str = self.coord_x_combo.currentText()
        y_str = self.coord_y_combo.currentText()
        x_all = (x_str == '' or x_str.startswith('全部'))
        y_all = (y_str == '' or y_str.startswith('全部'))
        
        if x_all and y_all:
            filtered = list(base)
        else:
            filtered = []
            for p in base:
                x_match = x_all or f"{p.x:.3f}" == x_str
                y_match = y_all or f"{p.y:.3f}" == y_str
                if x_match and y_match:
                    filtered.append(p)
        
        self.filtered_points = filtered
        self._update_point_list()
        self._update_filter_summary(
            source=source_desc,
            coord=(x_str if not x_all else None, y_str if not y_all else None)
        )
    
    def _update_filter_summary(self, source: str = "", coord=(None, None)):
        """刷新筛选结果摘要文字"""
        if not self.probe_points:
            self.filter_summary_label.setText("请先加载日志文件")
            return
        count = len(self.filtered_points)
        total = len(self.probe_points)
        parts = [f"命中 <b>{count}</b> / {total}"]
        if source:
            parts.append(f"来源: {source}")
        if coord and (coord[0] or coord[1]):
            cs = []
            if coord[0]:
                cs.append(f"X={coord[0]}")
            if coord[1]:
                cs.append(f"Y={coord[1]}")
            parts.append("坐标: " + ", ".join(cs))
        self.filter_summary_label.setText(" | ".join(parts))
    
    def _update_point_list(self):
        """更新探点列表"""
        self.point_tree.clear()
        
        for i, p in enumerate(self.filtered_points):
            flag_str = "✓" if p.pres_data_flag else "✗"
            if p.group_type == "leveling":
                group_str = f"调平#{p.group_id}"
            elif p.group_type == "accurate_g28":
                group_str = f"精归零#{p.group_id}"
            else:
                group_str = "-"
            item = QTreeWidgetItem([
                str(i + 1),
                group_str,
                f"({p.x:.2f}, {p.y:.2f})",
                f"{p.z:.4f}",
                f"{p.z_compensation:.4f}",
                str(p.pres_data_ch0_cal_turn),
                flag_str,
                str(p.pres_unload_cnt)
            ])
            # 让数字列右对齐、字段列左对齐
            item.setTextAlignment(0, Qt.AlignmentFlag.AlignCenter)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignCenter)
            item.setTextAlignment(3, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item.setTextAlignment(4, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item.setTextAlignment(5, Qt.AlignmentFlag.AlignCenter)
            item.setTextAlignment(6, Qt.AlignmentFlag.AlignCenter)
            item.setTextAlignment(7, Qt.AlignmentFlag.AlignCenter)
            self.point_tree.addTopLevelItem(item)
        
        # 填充完毕后强制按内容重新计算每一列的宽度
        for col in range(self.point_tree.columnCount()):
            self.point_tree.resizeColumnToContents(col)
    
    def _on_point_select(self, item: QTreeWidgetItem, column: int):
        """探点选择"""
        idx = self.point_tree.indexOfTopLevelItem(item)
        if 0 <= idx < len(self.filtered_points):
            point = self.filtered_points[idx]
            self._show_point_detail(point)
            self._plot_point(point)
    
    def _show_point_detail(self, point: ProbePoint):
        """显示探点详情"""
        detail = f"""时间: {point.timestamp}  |  行号: {point.line_number}
坐标: X={point.x:.4f}, Y={point.y:.4f}
Z值: {point.z:.6f}  |  Z补偿: {point.z_compensation:.6f}  |  Z最终: {point.z_final:.6f}
CAL_TURN(拐点): {point.pres_data_ch0_cal_turn}  |  DATA_FLAG: {point.pres_data_flag}
UNLOAD: cnt={point.pres_unload_cnt}, bst_indx={point.pres_unload_bst_indx}, tol={point.pres_unload_tol}
数据点数: FTR={len(point.pres_data_ch0_ftr_wave)} SRC={len(point.pres_data_ch0_src_wave)} CAL={len(point.pres_data_ch0_cal_wave)}
分组: {point.group_type} #{point.group_id}"""
        
        self.detail_text.setText(detail)
    
    def _get_plot_type(self) -> str:
        """获取当前绘图类型"""
        btn_id = self.plot_type_group.checkedId()
        return ["all", "ftr", "src", "cal"][btn_id]
    
    def _plot_point(self, point: ProbePoint):
        """绘制探点数据"""
        plot_type = self._get_plot_type()
        dual_axis = self.dual_axis_cb.isChecked()
        self.plot_canvas.plot_point(point, plot_type, dual_axis)
    
    def _update_plot(self):
        """更新绘图"""
        current = self.point_tree.currentItem()
        if current:
            idx = self.point_tree.indexOfTopLevelItem(current)
            if 0 <= idx < len(self.filtered_points):
                self._plot_point(self.filtered_points[idx])
    
    def _probe_port_available(self, port: int) -> Tuple[bool, str]:
        """预检端口可用性，返回 (ok, 错误说明)"""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('0.0.0.0', port))
            s.close()
            return True, ""
        except OSError as e:
            try:
                s.close()
            except Exception:
                pass
            winerr = getattr(e, 'winerror', None)
            if winerr == 10013:
                return False, f"端口 {port} 被系统禁止绑定（WinError 10013，常见于被 Hyper-V/WSL 保留）"
            if winerr == 10048:
                return False, f"端口 {port} 已被占用（WinError 10048）"
            return False, f"端口 {port} 不可用: {e}"
    
    def _toggle_realtime(self):
        """切换实时监控"""
        if self.realtime_thread and self.realtime_thread.isRunning():
            self.realtime_thread.stop()
            self.realtime_thread = None
            self.realtime_btn.setText("开始监控")
            self.realtime_status_label.setText("已停止")
            self.realtime_status_label.setStyleSheet("color: gray;")
            self.context.show_status("实时监控已停止", 3000)
        else:
            port = self.realtime_port_spin.value()
            
            # 预检端口
            ok, err_msg = self._probe_port_available(port)
            if not ok:
                self._rt_log_append(f"[ERROR] {err_msg}")
                # 尝试找一个替代端口
                free_port = RealtimeReceiverThread._find_free_udp_port()
                suggest = f"\n\n建议换成端口：{free_port}" if free_port else ""
                reply = QMessageBox.question(
                    self, "端口不可用",
                    f"{err_msg}{suggest}\n\n是否自动切换到端口 {free_port} 并继续？" if free_port else
                    f"{err_msg}\n\n请修改端口后重试。",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                    if free_port else QMessageBox.StandardButton.Ok
                )
                if free_port and reply == QMessageBox.StandardButton.Yes:
                    port = free_port
                    self.realtime_port_spin.setValue(port)
                    self._rt_log_append(f"[INFO] 已切换到端口 {port}")
                else:
                    return
            
            # 重置包数和点数
            self._rt_packet_count = 0
            self.realtime_points.clear()
            self._update_rt_count()
            self._rt_log_append(f"=== 开始监听 UDP:{port} ===")
            
            self.realtime_thread = RealtimeReceiverThread(port, self)
            self.realtime_thread.data_received.connect(self._on_realtime_data)
            self.realtime_thread.raw_received.connect(self._on_realtime_raw)
            self.realtime_thread.status_update.connect(self._on_realtime_status)
            self.realtime_thread.error_occurred.connect(self._on_realtime_error)
            self.realtime_thread.start()
            
            self.realtime_btn.setText("停止监控")
            self.realtime_status_label.setText(f"监听端口 {port}")
            self.realtime_status_label.setStyleSheet("color: green;")
            self.context.show_status(f"实时监控已启动，端口: {port}", 5000)
            
            # 保存端口配置
            self.config["realtime_port"] = port
            self._save_config()
    
    def _update_rt_count(self):
        """更新实时统计"""
        pkt = getattr(self, '_rt_packet_count', 0)
        pts = len(self.realtime_points)
        self.realtime_count_label.setText(f"包:{pkt} | 点:{pts}")
    
    def _rt_log_append(self, text: str):
        """向原始日志区追加一行（限长，避免内存膨胀）"""
        if not hasattr(self, 'rt_log_text'):
            return
        # 限制单条长度，避免超长数组刷屏
        line = text if len(text) < 500 else (text[:500] + f"... ({len(text)} chars)")
        self.rt_log_text.append(line)
        # 保留最近 300 行
        doc = self.rt_log_text.document()
        if doc.blockCount() > 300:
            cursor = self.rt_log_text.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            for _ in range(doc.blockCount() - 300):
                cursor.select(cursor.SelectionType.LineUnderCursor)
                cursor.removeSelectedText()
                cursor.deleteChar()
    
    def _on_realtime_raw(self, sender: str, text: str):
        """收到原始 UDP 数据包"""
        self._rt_packet_count = getattr(self, '_rt_packet_count', 0) + 1
        self._update_rt_count()
        self._rt_log_append(f"[{sender}] {text.strip()}")
    
    def _on_realtime_status(self, msg: str):
        """线程状态更新"""
        self._rt_log_append(f"[STATUS] {msg}")
    
    def _on_realtime_data(self, point: ProbePoint):
        """收到完整实时数据（已组装好的探点）"""
        self.realtime_points.append(point)
        if len(self.realtime_points) > 100:
            self.realtime_points = self.realtime_points[-100:]
        
        # 添加到列表
        flag_str = "✓" if point.pres_data_flag else "✗"
        item = QTreeWidgetItem([
            "RT",
            f"({point.x:.2f}, {point.y:.2f})",
            f"{point.z:.4f}",
            f"{point.z_compensation:.4f}",
            str(point.pres_data_ch0_cal_turn),
            flag_str,
            str(point.pres_unload_cnt)
        ])
        self.point_tree.insertTopLevelItem(0, item)
        
        # 更新绘图和详情
        self._plot_point(point)
        self._show_point_detail(point)
        self._update_rt_count()
        self._rt_log_append(
            f"[POINT] ({point.x:.2f},{point.y:.2f}) z={point.z:.4f} "
            f"turn={point.pres_data_ch0_cal_turn} "
            f"ftr={len(point.pres_data_ch0_ftr_wave)} "
            f"src={len(point.pres_data_ch0_src_wave)} "
            f"cal={len(point.pres_data_ch0_cal_wave)}"
        )
    
    def _on_realtime_error(self, error: str):
        """实时监控错误"""
        self.context.show_status(f"实时监控错误: {error}", 10000)
        self._rt_log_append(f"[ERROR] {error}")
        # 严重错误（绑定失败）才弹窗
        if '绑定' in error or '失败' in error:
            QMessageBox.warning(self, "实时监控错误", error)
    
    # ==================== 生命周期方法 ====================
    
    def activate(self):
        """插件被激活时调用"""
        self.context.show_status("PRTouch日志分析插件已激活", 3000)
    
    def deactivate(self):
        """插件被切走时调用"""
        pass
    
    def shutdown(self):
        """平台关闭时调用"""
        # 停止实时监控
        if self.realtime_thread and self.realtime_thread.isRunning():
            self.realtime_thread.stop()
        
        # 保存配置
        self.config["dual_axis"] = self.dual_axis_cb.isChecked()
        self._save_config()


# ============================================================================
# 插件工厂函数
# ============================================================================

def create_plugin(context, parent=None) -> QWidget:
    """
    插件工厂函数
    
    Args:
        context: PluginContext 实例
        parent: 父 QWidget
    
    Returns:
        PRTouchLogAnalyzerWidget 实例
    """
    return PRTouchLogAnalyzerWidget(context, parent)
