# ToolPlatform

基于 PyQt6 的本地桌面工具插件平台。宿主提供可横向滚动的顶部工具标签、全宽插件页面栈、默认工业深色主题（并保留浅色/深色切换）、统一状态栏，以及插件生命周期管理。插件之间相互隔离：单个插件导入、创建或生命周期调用失败时，平台会通过状态栏提示并继续运行。

工具导航位于窗口顶部；工具较多时可使用标签栏两侧的滚动按钮继续选择。插件主内容占用完整窗口宽度，喷嘴标定和 SSH 管理插件内部均使用可拖动分隔条，可按当前任务调整控制区、图像区、设备操作区与日志区的比例。

## 环境与启动

建议使用 Windows 和 Python 3.14 的 64 位虚拟环境。第三方库能否安装取决于其是否已发布对应 Python 3.14 的 wheel。

```powershell
cd d:\zhengyiyang\ToolPlatform
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

也可从工作区根目录启动：

```powershell
cd d:\zhengyiyang
python -m ToolPlatform.app
```

## 内置工具

- **批量图片圆心识别（`nozzle_batch`）**：批量读取目录图片，提供圆心检测参数调试、偏移计算、CSV 导出与叠加处理图导出。
- **喷嘴圆心与偏移标定（`nozzle_offset`）**：支持相机实时画面、固定帧和导入图片，可手动或自动识别圆心并完成基准与偏移标定；结果可保存、导出和图片比对。
- 自动找圆心可配置 ROI、平滑/二值化/形态学、候选筛选、轮廓/Canny/亚像素边沿和圆拟合参数。配置保存在 `config/plugins/nozzle_offset/auto_detect.json`；实际喷嘴直径统一使用插件主界面的“实际直径(mm)”。
- **多设备 SSH 管理（`ssh_runner`）**：SSH IP 与登录信息保存在 `config/plugins/ssh_runner/devices.json`。可在设备按钮区点击“打开设备配置”直接打开该文件；文件尚不存在时会先自动创建。界面仍提供“打开日志目录”快捷按钮。
- **首层不良面积计算（`first_layer_defect`）**：在平台页面内打开首层图片，标定四角做透视校正并框选不良多边形，实时统计 S1、各不良面积与占比。界面已重写为 PyQt6，计算逻辑复用 `网床研究` 目录下原脚本的纯函数，**原文件不改动**。
- **喷嘴补偿与打印事件提取（`nozzle_comp_extract`）**：解析 Klipper 日志中的喷嘴补偿与开始打印事件，在页面内以表格展示（**喷嘴补偿与开始打印用不同颜色区分**），按时间升序排列并可点击表头排序，同时导出 CSV。复用 `网床研究` 目录下原脚本的解析逻辑（动态加载，原文件不改动）。
- **网床日志分析（`mesh_log_analyzer`）**：在平台页面内分析网床调平日志，查看网床热力图、相对基准差分、打印事件列表与统计详情，支持导出 CSV。界面已重写为 PyQt6，解析与统计逻辑复用原脚本，**原文件不改动**。

> 以上三个插件默认从 `E:\2.Python_Project\Project4-tools\网床研究` 读取原始脚本；如需换位置，设置环境变量 `TOOLPLATFORM_WANGCHUANG_DIR` 指向新目录即可。

## 启用与禁用插件

插件加载由 `config/plugins.json` 控制，修改后重启平台生效；也可以直接点标题旁的「首选项」按钮在界面里勾选，保存后**立即重新加载**，无需重启。

```json
{
  "disabled": ["prtouch_log_analyzer"],
  "enabled": []
}
```

- `disabled`：不想加载的插件，填写 `plugins` 目录下的文件名（不含 `.py`）或插件 id。这些插件**不会被导入**，平台的工具标签栏也不会出现。
- `enabled`：填写后进入白名单模式，只加载列表中的插件；留空表示加载除 `disabled` 之外的全部插件。
- 配置文件不存在或格式有误时，平台会回退为加载全部插件，并在状态栏提示原因。
- 当前插件文件名：`nozzle_batch`、`nozzle_offset`、`ssh_runner`、`prtouch_log_analyzer`。

## 新增单文件插件

在 `plugins` 目录新增一个**不以下划线开头**的 `.py` 文件。每个插件必须暴露 `PLUGIN_META` 和 `create_plugin`：

```python
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

PLUGIN_META = {
    "id": "demo.hello",
    "name": "Hello 插件",
    "version": "1.0.0",
    "category": "示例",
    "description": "最小插件示例",
    "order": 10,
}


class HelloWidget(QWidget):
    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Hello, ToolPlatform!"))

    def activate(self):
        self.context.show_status("Hello 插件已激活")

    def deactivate(self):
        pass

    def shutdown(self):
        pass


def create_plugin(context, parent=None) -> QWidget:
    return HelloWidget(context, parent)
```

协议说明：

- `PLUGIN_META` 必须包含 `id/name/version/category/description/order`；`id` 必须唯一，并仅使用字母、数字、点、下划线或连字符。
- `create_plugin(context, parent=None)` 必须返回 `QWidget`。
- 页面在用户第一次选择插件时才创建。Widget 可选实现 `activate()`、`deactivate()`、`shutdown()`。
- `context.data_dir`、`context.config_dir`、`context.log_dir` 是当前插件按 id 隔离的 `pathlib.Path` 目录，使用时已自动建立。
- 也可调用 `context.plugin_data_dir()`、`context.plugin_config_file(filename)`、`context.plugin_log_dir()`；需要访问指定插件目录时可显式传入插件 id。
- 用 `context.show_status(message, timeout_ms=5000)`（或别名 `post_status`）向底部状态栏发送消息。
- 插件文件的顶层代码会在平台发现元数据时执行；耗时初始化应放到 `create_plugin` 或 Widget 内。

## 目录约定

- `config/platform.json`：平台标题、主题和窗口尺寸。
- `themes/industrial.qss`、`themes/light.qss`、`themes/dark.qss`：全局工业深色、浅色与深色样式。
- `data/<plugin-id>/`：插件数据。
- `config/plugins/<plugin-id>/`：插件配置。
- `logs/<plugin-id>/`：插件日志。

运行时生成的数据、配置和日志目录不会影响原有工具文件。
