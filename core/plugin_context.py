"""传递给单文件插件的运行时上下文。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal


StatusCallback = Callable[[str, int], None]


class PluginContext(QObject):
    """为一个插件提供隔离目录和统一状态消息通道。

    插件通常调用 ``context.show_status("处理完成")``。宿主既可以连接
    ``status_message`` 信号，也可以在创建上下文时传入回调。
    """

    status_message = pyqtSignal(str, int)

    def __init__(
        self,
        plugin_id: str,
        platform_root: Path,
        status_callback: StatusCallback | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.plugin_id = plugin_id
        self.platform_root = Path(platform_root).resolve()
        self._status_callback = status_callback

        self.data_dir = self.platform_root / "data" / plugin_id
        self.config_dir = self.platform_root / "config" / "plugins" / plugin_id
        self.log_dir = self.platform_root / "logs" / plugin_id
        for directory in (self.data_dir, self.config_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def plugin_data_dir(self, plugin_id: str | None = None) -> Path:
        """返回指定插件的数据目录；省略 id 时使用当前插件。"""
        path = self.platform_root / "data" / (plugin_id or self.plugin_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def plugin_config_file(
        self,
        plugin_id_or_filename: str,
        filename: str | None = None,
    ) -> Path:
        """返回插件配置文件路径，兼容 (filename) 和 (plugin_id, filename)。"""
        if filename is None:
            plugin_id = self.plugin_id
            filename = plugin_id_or_filename
        else:
            plugin_id = plugin_id_or_filename
        directory = self.platform_root / "config" / "plugins" / plugin_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory / filename

    def plugin_log_dir(self, plugin_id: str | None = None) -> Path:
        """返回指定插件的日志目录；省略 id 时使用当前插件。"""
        path = self.platform_root / "logs" / (plugin_id or self.plugin_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def show_status(self, message: str, timeout_ms: int = 5000) -> None:
        """向宿主发送状态消息；``timeout_ms=0`` 表示持续显示。"""
        text = str(message)
        timeout = max(0, int(timeout_ms))
        self.status_message.emit(text, timeout)
        if self._status_callback is not None:
            self._status_callback(text, timeout)

    post_status = show_status
