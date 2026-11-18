"""ToolPlatform 单文件插件发现与加载。"""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable


_REQUIRED_META = ("id", "name", "version", "category", "description", "order")
_VALID_PLUGIN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True, slots=True)
class PluginRecord:
    """已发现插件的元数据及其工厂；Widget 到首次访问时才创建。"""

    path: Path
    module: ModuleType
    meta: dict[str, Any]
    factory: Callable[..., Any]

    @property
    def plugin_id(self) -> str:
        return self.meta["id"]


@dataclass(frozen=True, slots=True)
class PluginLoadError:
    path: Path
    message: str


class PluginLoader:
    """使用 importlib 从指定目录发现符合统一协议的单文件插件。"""

    def __init__(self, plugins_dir: Path) -> None:
        self.plugins_dir = Path(plugins_dir).resolve()

    def discover(self) -> tuple[list[PluginRecord], list[PluginLoadError]]:
        records: list[PluginRecord] = []
        errors: list[PluginLoadError] = []
        seen_ids: set[str] = set()

        if not self.plugins_dir.is_dir():
            return records, [PluginLoadError(self.plugins_dir, "插件目录不存在")]

        for path in sorted(self.plugins_dir.glob("*.py"), key=lambda item: item.name.casefold()):
            if path.name == "__init__.py" or path.name.startswith("_"):
                continue
            try:
                record = self._load_record(path)
                if record.plugin_id in seen_ids:
                    raise ValueError(f"插件 id 重复: {record.plugin_id}")
                seen_ids.add(record.plugin_id)
                records.append(record)
            except Exception as exc:  # 隔离单个插件的导入及协议错误
                errors.append(PluginLoadError(path, f"{type(exc).__name__}: {exc}"))

        records.sort(
            key=lambda item: (
                str(item.meta["category"]).casefold(),
                int(item.meta["order"]),
                str(item.meta["name"]).casefold(),
            )
        )
        return records, errors

    def _load_record(self, path: Path) -> PluginRecord:
        module_name = f"toolplatform_plugin_{path.stem}_{abs(hash(path))}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError("无法创建模块加载器")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            meta = self._normalize_meta(getattr(module, "PLUGIN_META", None))
            factory = getattr(module, "create_plugin", None)
            if not callable(factory):
                raise TypeError("create_plugin 必须是可调用对象")
            return PluginRecord(path=path, module=module, meta=meta, factory=factory)
        except Exception:
            sys.modules.pop(module_name, None)
            raise

    @staticmethod
    def _normalize_meta(raw_meta: object) -> dict[str, Any]:
        if not isinstance(raw_meta, dict):
            raise TypeError("PLUGIN_META 必须是字典")

        missing = [key for key in _REQUIRED_META if key not in raw_meta]
        if missing:
            raise ValueError(f"PLUGIN_META 缺少字段: {', '.join(missing)}")

        meta = dict(raw_meta)
        for key in ("id", "name", "version", "category", "description"):
            if not isinstance(meta[key], str) or not meta[key].strip():
                raise TypeError(f"PLUGIN_META['{key}'] 必须是非空字符串")
            meta[key] = meta[key].strip()

        if not _VALID_PLUGIN_ID.fullmatch(meta["id"]):
            raise ValueError("插件 id 仅允许字母、数字、点、下划线和连字符")
        if isinstance(meta["order"], bool):
            raise TypeError("PLUGIN_META['order'] 必须是整数")
        try:
            meta["order"] = int(meta["order"])
        except (TypeError, ValueError) as exc:
            raise TypeError("PLUGIN_META['order'] 必须是整数") from exc
        return meta
