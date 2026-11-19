# ToolPlatform 插件开发指南

本文档描述如何为 ToolPlatform 开发和集成新插件。按照本指南规范开发的插件可以无缝接入平台，与其他工具共享统一的启动入口和界面风格。

---

## 目录

1. [平台架构概览](#1-平台架构概览)
2. [插件协议规范](#2-插件协议规范)
3. [快速开始模板](#3-快速开始模板)
4. [完整开发指南](#4-完整开发指南)
5. [目录结构约定](#5-目录结构约定)
6. [PluginContext API 参考](#6-plugincontext-api-参考)
7. [生命周期管理](#7-生命周期管理)
8. [最佳实践](#8-最佳实践)
9. [常见问题](#9-常见问题)
10. [检查清单](#10-检查清单)

---

## 1. 平台架构概览

### 1.1 技术栈

| 组件 | 技术 |
|------|------|
| GUI 框架 | PyQt6 |
| Python 版本 | 建议 3.14（64位） |
| 插件形式 | 单文件 Python 模块 |

### 1.2 核心组件

```
ToolPlatform/
├── app.py                    # 平台主程序入口
├── core/
│   ├── __init__.py          # 导出核心模块
│   ├── plugin_loader.py     # 插件发现与加载器
│   ├── plugin_context.py    # 插件运行时上下文
│   └── theme.py             # 主题管理器
├── plugins/                  # 插件目录（放置你的插件）
│   ├── __init__.py
│   ├── nozzle_batch.py      # 示例：批量圆心识别
│   ├── nozzle_offset.py     # 示例：喷嘴标定
│   └── ssh_runner.py        # 示例：SSH管理
├── config/
│   ├── platform.json        # 平台配置
│   └── plugins/             # 各插件配置目录
├── themes/                   # QSS 主题样式
├── data/                     # 各插件数据目录
└── logs/                     # 各插件日志目录
```

### 1.3 工作流程

```
平台启动
    ↓
PluginLoader 扫描 plugins/ 目录
    ↓
加载所有 *.py 文件（排除 __init__.py 和 _ 开头的文件）
    ↓
验证 PLUGIN_META 和 create_plugin
    ↓
按 category → order → name 排序生成导航标签
    ↓
用户点击标签时，首次调用 create_plugin() 创建 Widget
    ↓
切换标签时调用 activate() / deactivate()
    ↓
平台关闭时调用 shutdown()
```

---

## 2. 插件协议规范

### 2.1 必需导出

每个插件文件**必须**导出以下两个对象：

#### 2.1.1 `PLUGIN_META` 字典

```python
PLUGIN_META = {
    "id": "your_plugin_id",      # 必需：唯一标识符
    "name": "插件显示名称",        # 必需：导航标签显示的名称
    "version": "1.0.0",          # 必需：版本号
    "category": "工具分类",       # 必需：用于排序分组
    "description": "插件功能描述", # 必需：悬停提示显示
    "order": 10,                 # 必需：同分类内的排序权重（数字越小越靠前）
}
```

**字段规则：**

| 字段 | 类型 | 规则 |
|------|------|------|
| `id` | str | 唯一，仅允许字母、数字、点(.)、下划线(_)、连字符(-)，必须以字母或数字开头 |
| `name` | str | 非空字符串 |
| `version` | str | 非空字符串，建议遵循语义化版本 |
| `category` | str | 非空字符串，相同分类的插件会排列在一起 |
| `description` | str | 非空字符串 |
| `order` | int | 整数，决定同分类内的排列顺序 |

#### 2.1.2 `create_plugin` 工厂函数

```python
def create_plugin(context, parent=None) -> QWidget:
    """
    插件工厂函数，平台在用户首次选择该插件时调用。
    
    Args:
        context: PluginContext 实例，提供插件运行时服务
        parent: 父 QWidget，通常传给你的 Widget 构造函数
    
    Returns:
        QWidget: 插件的主界面 Widget
    """
    return YourPluginWidget(context, parent)
```

### 2.2 可选生命周期方法

插件 Widget 可以实现以下方法（均为可选）：

```python
class YourPluginWidget(QWidget):
    def activate(self):
        """当插件页面被切换到前台时调用"""
        pass
    
    def deactivate(self):
        """当插件页面从前台切走时调用"""
        pass
    
    def shutdown(self):
        """平台关闭时调用，用于清理资源"""
        pass
```

---

## 3. 快速开始模板

### 3.1 最小插件示例

创建文件 `plugins/hello_world.py`：

```python
"""Hello World 示例插件。"""

from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

PLUGIN_META = {
    "id": "hello_world",
    "name": "Hello World",
    "version": "1.0.0",
    "category": "示例",
    "description": "最简单的插件示例",
    "order": 100,
}


class HelloWidget(QWidget):
    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Hello, ToolPlatform!"))
    
    def activate(self):
        self.context.show_status("Hello World 插件已激活", 3000)
    
    def deactivate(self):
        pass
    
    def shutdown(self):
        pass


def create_plugin(context, parent=None) -> QWidget:
    return HelloWidget(context, parent)
```

### 3.2 带配置和日志的插件模板

```python
"""带配置和日志功能的插件模板。"""

from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

PLUGIN_META = {
    "id": "my_tool",
    "name": "我的工具",
    "version": "1.0.0",
    "category": "自定义工具",
    "description": "带配置和日志的插件模板",
    "order": 50,
}


class MyToolWidget(QWidget):
    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        
        # 配置文件路径
        self.config_path = self.context.plugin_config_file("settings.json")
        
        self._build_ui()
        self._load_config()
    
    def _build_ui(self):
        layout = QVBoxLayout(self)
        
        # 输入区域
        input_row = QHBoxLayout()
        input_row.addWidget(QLabel("设置项:"))
        self.input_field = QLineEdit()
        input_row.addWidget(self.input_field)
        layout.addLayout(input_row)
        
        # 按钮区域
        button_row = QHBoxLayout()
        self.btn_save = QPushButton("保存配置")
        self.btn_save.clicked.connect(self._save_config)
        self.btn_action = QPushButton("执行操作")
        self.btn_action.clicked.connect(self._do_action)
        button_row.addWidget(self.btn_save)
        button_row.addWidget(self.btn_action)
        layout.addLayout(button_row)
        
        layout.addStretch()
    
    def _load_config(self):
        """加载配置"""
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                self.input_field.setText(data.get("setting", ""))
            except Exception as e:
                self.context.show_status(f"配置加载失败: {e}", 5000)
    
    def _save_config(self):
        """保存配置"""
        try:
            data = {"setting": self.input_field.text()}
            self.config_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            self.context.show_status("配置已保存", 3000)
        except Exception as e:
            self.context.show_status(f"配置保存失败: {e}", 5000)
    
    def _do_action(self):
        """执行某个操作并记录日志"""
        log_file = self.context.log_dir / "operations.log"
        try:
            with log_file.open("a", encoding="utf-8") as f:
                f.write(f"执行操作: {self.input_field.text()}\n")
            self.context.show_status("操作完成", 3000)
        except Exception as e:
            self.context.show_status(f"操作失败: {e}", 5000)
    
    def activate(self):
        self.context.show_status("我的工具已激活", 3000)
    
    def deactivate(self):
        pass
    
    def shutdown(self):
        # 清理资源，如关闭线程、保存状态等
        pass


def create_plugin(context, parent=None) -> QWidget:
    return MyToolWidget(context, parent)
```

---

## 4. 完整开发指南

### 4.1 文件命名规则

- 插件文件放置在 `plugins/` 目录下
- 文件名使用小写字母、数字和下划线，如 `my_tool.py`
- **不要**以下划线开头（`_private.py` 会被忽略）
- **不要**命名为 `__init__.py`

### 4.2 导入规范

```python
# 标准库
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any, Optional

# 第三方库（确保在 requirements.txt 中声明）
import numpy as np
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QPushButton

# 不要在顶层导入可能失败的可选依赖
# 正确做法：
try:
    import cv2
except ImportError:
    cv2 = None

# 然后在使用时检查
def some_function():
    if cv2 is None:
        raise RuntimeError("需要安装 opencv-python")
    # 使用 cv2...
```

### 4.3 UI 构建指南

#### 4.3.1 推荐的 UI 组件

```python
from PyQt6.QtWidgets import (
    # 布局
    QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    # 容器
    QWidget, QGroupBox, QTabWidget, QSplitter, QScrollArea,
    # 输入
    QLineEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QCheckBox,
    # 按钮
    QPushButton,
    # 显示
    QLabel, QProgressBar, QTableWidget,
    # 对话框
    QMessageBox, QFileDialog, QInputDialog,
)
```

#### 4.3.2 使用分隔条实现可调整布局

```python
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QSplitter

# 水平分隔
horizontal_splitter = QSplitter(Qt.Orientation.Horizontal)
horizontal_splitter.addWidget(left_panel)
horizontal_splitter.addWidget(right_panel)
horizontal_splitter.setStretchFactor(0, 1)
horizontal_splitter.setStretchFactor(1, 2)

# 垂直分隔
vertical_splitter = QSplitter(Qt.Orientation.Vertical)
vertical_splitter.addWidget(top_panel)
vertical_splitter.addWidget(bottom_panel)
```

### 4.4 后台任务处理

使用 `QThread` 处理耗时操作，避免阻塞 UI：

```python
from PyQt6.QtCore import QThread, pyqtSignal


class WorkerThread(QThread):
    progress = pyqtSignal(int, int)      # 当前, 总数
    result = pyqtSignal(object)          # 结果
    error = pyqtSignal(str)              # 错误信息
    
    def __init__(self, data, parent=None):
        super().__init__(parent)
        self.data = data
        self._stop = False
    
    def stop(self):
        self._stop = True
        self.requestInterruption()
    
    def run(self):
        try:
            total = len(self.data)
            results = []
            for i, item in enumerate(self.data):
                if self._stop or self.isInterruptionRequested():
                    break
                # 处理数据...
                result = self.process(item)
                results.append(result)
                self.progress.emit(i + 1, total)
            self.result.emit(results)
        except Exception as e:
            self.error.emit(str(e))
    
    def process(self, item):
        # 实际处理逻辑
        return item


class MyPluginWidget(QWidget):
    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        self.worker = None
        # ... UI 初始化
    
    def start_task(self):
        if self.worker and self.worker.isRunning():
            return
        
        self.worker = WorkerThread(self.data, parent=self)
        self.worker.progress.connect(self._on_progress)
        self.worker.result.connect(self._on_result)
        self.worker.error.connect(self._on_error)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()
    
    def stop_task(self):
        if self.worker:
            self.worker.stop()
    
    def _on_progress(self, current, total):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
    
    def _on_result(self, results):
        self.context.show_status(f"完成，共处理 {len(results)} 项")
    
    def _on_error(self, message):
        self.context.show_status(f"错误: {message}", 10000)
    
    def _on_finished(self):
        self.worker = None
    
    def shutdown(self):
        # 确保关闭时停止后台任务
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(2000)
```

---

## 5. 目录结构约定

平台会自动为每个插件创建和管理以下目录：

| 目录 | 用途 | 访问方式 |
|------|------|----------|
| `data/<plugin-id>/` | 存放插件运行时数据 | `context.data_dir` |
| `config/plugins/<plugin-id>/` | 存放插件配置文件 | `context.config_dir` |
| `logs/<plugin-id>/` | 存放插件日志文件 | `context.log_dir` |

**示例：**

插件 ID 为 `my_tool` 时：
- 数据目录：`data/my_tool/`
- 配置目录：`config/plugins/my_tool/`
- 日志目录：`logs/my_tool/`

---

## 6. PluginContext API 参考

`PluginContext` 是平台传递给插件的运行时上下文对象，提供以下 API：

### 6.1 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `plugin_id` | `str` | 当前插件的 ID |
| `platform_root` | `Path` | 平台根目录的绝对路径 |
| `data_dir` | `Path` | 当前插件的数据目录（已自动创建） |
| `config_dir` | `Path` | 当前插件的配置目录（已自动创建） |
| `log_dir` | `Path` | 当前插件的日志目录（已自动创建） |

### 6.2 方法

#### `show_status(message: str, timeout_ms: int = 5000) -> None`

向平台状态栏发送消息。

```python
self.context.show_status("操作完成", 3000)      # 显示 3 秒
self.context.show_status("持续显示的消息", 0)   # timeout_ms=0 表示持续显示
```

别名：`post_status`（功能相同）

#### `plugin_data_dir(plugin_id: str | None = None) -> Path`

获取指定插件的数据目录。省略 `plugin_id` 时返回当前插件的数据目录。

```python
my_data = self.context.plugin_data_dir()                    # 当前插件
other_data = self.context.plugin_data_dir("other_plugin")   # 其他插件
```

#### `plugin_config_file(filename: str) -> Path`
#### `plugin_config_file(plugin_id: str, filename: str) -> Path`

获取插件配置文件的完整路径。

```python
# 当前插件的配置文件
config_path = self.context.plugin_config_file("settings.json")

# 指定插件的配置文件
other_config = self.context.plugin_config_file("other_plugin", "config.json")
```

#### `plugin_log_dir(plugin_id: str | None = None) -> Path`

获取指定插件的日志目录。省略 `plugin_id` 时返回当前插件的日志目录。

```python
log_dir = self.context.plugin_log_dir()
log_file = log_dir / "debug.log"
```

### 6.3 信号

| 信号 | 参数 | 说明 |
|------|------|------|
| `status_message` | `(str, int)` | 状态消息和超时时间，由 `show_status` 触发 |

---

## 7. 生命周期管理

### 7.1 生命周期流程图

```
插件文件被发现
      ↓
执行顶层代码（导入时）← 避免耗时操作！
      ↓
读取 PLUGIN_META
      ↓
验证协议合规性
      ↓
（等待用户选择）
      ↓
首次选择 → create_plugin() → Widget 实例化
      ↓
activate() ←──────────────┐
      ↓                   │
（用户使用中）              │
      ↓                   │
切换到其他插件 → deactivate()
      ↓                   │
切换回来 ─────────────────┘
      ↓
平台关闭 → shutdown()
```

### 7.2 各阶段职责

| 阶段 | 触发时机 | 应该做什么 | 不应该做什么 |
|------|----------|------------|--------------|
| **导入时** | 平台扫描插件 | 定义类和函数、导入依赖 | 创建窗口、启动线程、读写大文件 |
| **create_plugin** | 用户首次选择 | 创建 UI、加载配置、初始化状态 | 启动长时间任务（可放到按钮触发） |
| **activate** | 切换到此插件 | 恢复定时器、刷新显示、获取焦点 | 重建 UI |
| **deactivate** | 切换离开 | 暂停定时器、保存临时状态 | 销毁 Widget |
| **shutdown** | 平台关闭 | 停止线程、关闭连接、保存状态 | 阻塞太久（应有超时） |

### 7.3 错误隔离

平台对单个插件的错误进行隔离：

- 导入/元数据验证失败：插件不会出现在导航中，状态栏提示错误
- `create_plugin` 失败：显示错误占位页面，状态栏提示
- `activate`/`deactivate`/`shutdown` 失败：状态栏提示，不影响其他插件

---

## 8. 最佳实践

### 8.1 代码规范

```python
# 使用类型注解
def process_data(self, items: list[dict]) -> list[str]:
    ...

# 使用 f-string 而非 % 或 .format()
message = f"处理完成，共 {count} 项"

# 使用 Path 而非字符串拼接路径
config_path = self.context.config_dir / "settings.json"
```

### 8.2 错误处理

```python
def load_config(self):
    try:
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        return data
    except FileNotFoundError:
        return {}  # 首次运行，返回默认值
    except json.JSONDecodeError as e:
        self.context.show_status(f"配置文件格式错误: {e}", 10000)
        return {}
    except Exception as e:
        self.context.show_status(f"配置加载失败: {e}", 10000)
        return {}
```

### 8.3 用户反馈

```python
# 操作开始
self.context.show_status("正在处理...", 0)
self.btn_start.setEnabled(False)

# 操作进行中（通过信号更新）
self.progress_bar.setValue(current)

# 操作完成
self.context.show_status(f"处理完成，耗时 {elapsed:.1f} 秒", 5000)
self.btn_start.setEnabled(True)

# 操作失败
self.context.show_status(f"处理失败: {error}", 10000)
```

### 8.4 资源清理

```python
def shutdown(self):
    # 1. 停止后台线程
    if self.worker and self.worker.isRunning():
        self.worker.stop()
        if not self.worker.wait(2000):  # 等待最多 2 秒
            self.worker.terminate()
    
    # 2. 关闭网络连接
    if self.connection:
        try:
            self.connection.close()
        except Exception:
            pass
    
    # 3. 保存状态（可选）
    try:
        self._save_state()
    except Exception:
        pass
```

### 8.5 主题兼容

平台提供三种主题：`industrial`（工业深色）、`light`（浅色）、`dark`（深色）。

为确保插件在所有主题下显示正常：

```python
# 使用 objectName 配合 QSS 样式
self.status_label = QLabel()
self.status_label.setObjectName("pluginDescription")  # 使用平台预定义样式

# 避免硬编码颜色，使用 palette
palette = self.palette()
text_color = palette.color(QPalette.ColorRole.Text)
```

---

## 9. 常见问题

### Q1: 插件没有出现在导航栏中

**检查项：**
1. 文件是否在 `plugins/` 目录下
2. 文件名是否以 `.py` 结尾
3. 文件名是否以下划线开头（会被忽略）
4. `PLUGIN_META` 是否完整（检查所有必需字段）
5. `PLUGIN_META["id"]` 是否符合格式要求
6. 是否有语法错误（查看控制台输出）

### Q2: 插件创建时报错

**检查项：**
1. `create_plugin` 是否返回 `QWidget` 实例
2. 依赖库是否已安装
3. 初始化代码是否有异常

### Q3: 如何调试插件

```python
# 方法1：在代码中添加打印
print(f"DEBUG: value = {value}")

# 方法2：写入日志文件
log_file = self.context.log_dir / "debug.log"
with log_file.open("a", encoding="utf-8") as f:
    f.write(f"[{datetime.now()}] DEBUG: {message}\n")

# 方法3：使用状态栏临时显示
self.context.show_status(f"DEBUG: {value}", 10000)
```

### Q4: 如何访问其他插件的数据

```python
# 获取其他插件的数据目录（只读访问）
other_data_dir = self.context.plugin_data_dir("other_plugin_id")
if other_data_dir.exists():
    # 读取数据...
    pass
```

### Q5: 如何添加快捷键

```python
from PyQt6.QtGui import QAction, QKeySequence

def _setup_shortcuts(self):
    # Ctrl+S 保存
    save_action = QAction(self)
    save_action.setShortcut(QKeySequence("Ctrl+S"))
    save_action.triggered.connect(self._save)
    self.addAction(save_action)
    
    # F5 刷新
    refresh_action = QAction(self)
    refresh_action.setShortcut(QKeySequence("F5"))
    refresh_action.triggered.connect(self._refresh)
    self.addAction(refresh_action)
```

---

## 10. 检查清单

在提交插件前，请确认以下各项：

### 元数据检查
- [ ] `PLUGIN_META` 包含所有必需字段：`id`, `name`, `version`, `category`, `description`, `order`
- [ ] `id` 唯一且格式正确（字母数字开头，可含 `.` `_` `-`）
- [ ] `name` 简洁明了
- [ ] `version` 使用语义化版本号
- [ ] `category` 与同类插件一致
- [ ] `description` 描述清楚功能
- [ ] `order` 数值合理

### 代码检查
- [ ] `create_plugin(context, parent=None)` 函数存在且返回 `QWidget`
- [ ] 顶层代码不包含耗时操作
- [ ] 使用 `try/except` 处理可能的异常
- [ ] 可选依赖使用 `try/import/except ImportError` 模式
- [ ] 后台任务使用 `QThread`，不阻塞 UI
- [ ] `shutdown()` 正确清理资源

### 功能检查
- [ ] 插件能正常加载和显示
- [ ] 在三种主题下显示正常
- [ ] 切换到其他插件再切回来，状态正常
- [ ] 关闭平台时不报错
- [ ] 配置文件读写正常
- [ ] 状态消息正确显示

### 文件检查
- [ ] 文件名符合规范（小写字母、数字、下划线）
- [ ] 文件编码为 UTF-8
- [ ] 依赖库已添加到 `requirements.txt`

---

## 附录：现有插件参考

| 插件 ID | 名称 | 分类 | 主要功能 |
|---------|------|------|----------|
| `nozzle_batch` | 批量图片圆心识别 | 视觉工具 | 目录图片批量圆心检测、参数调试、CSV 导出 |
| `nozzle_offset` | 喷嘴圆心与偏移标定 | 视觉工具 | 相机实时/图片圆心识别、偏移标定、结果导出 |
| `ssh_runner` | 多设备 SSH 管理 | 设备工具 | SSH 命令执行、文件推送、批量管理 |

建议参考这些插件的源码了解更复杂的实现模式。

---

*本文档版本: 1.0*  
*最后更新: 2024年*  
*适用平台版本: ToolPlatform (PyQt6)*
