"""Embeddable multi-device SSH management plugin for ToolPlatform.

This module is intentionally self-contained.  It does not import or write any
files in the legacy SSHRunner directory.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import html
import ipaddress
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

try:
    import paramiko
    from paramiko.ssh_exception import SSHException
except ImportError:  # Keep plugin discovery working; operations report the dependency error.
    paramiko = None  # type: ignore[assignment]

    class SSHException(Exception):
        pass

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

from PyQt6.QtCore import QObject, QThread, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


PLUGIN_META = {
    "id": "ssh_runner",
    "name": "多设备 SSH 管理",
    "version": "2.0",
    "category": "设备工具",
    "description": "多设备 SSH 命令、文件推送、并发上传下载与 Moonraker G-code 管理。",
    "order": 30,
}

PLUGIN_ID = "ssh_runner"
CHUNK_SIZE = 64 * 1024
MAX_WORKERS = 8
DEFAULT_PUSH_TARGET = "/usr/share/klipper/klippy/extras/prtouch_v3_wrapper.py"
_ORPHANED_WORKERS: set[QThread] = set()
_ORPHANED_THREADS: set[threading.Thread] = set()


def _as_path(value: Any) -> Path:
    if isinstance(value, Path):
        return value
    return Path(os.fspath(value))


def _context_config_file(context: Any, filename: str) -> Path:
    """Resolve config path while tolerating host API variations."""
    method = getattr(context, "plugin_config_file", None)
    if callable(method):
        for args in ((PLUGIN_ID, filename), (filename,)):
            try:
                value = method(*args)
                if value is not None:
                    return _as_path(value)
            except TypeError:
                continue
    # Current ToolPlatform exposes an already plugin-scoped Path property.
    config_dir = getattr(context, "config_dir", None)
    if config_dir is not None and not callable(config_dir):
        return _as_path(config_dir) / filename
    method = getattr(context, "plugin_config_dir", None)
    if callable(method):
        for args in ((PLUGIN_ID,), ()):
            try:
                value = method(*args)
                if value is not None:
                    return _as_path(value) / filename
            except TypeError:
                continue
    return Path(__file__).resolve().parents[1] / ".plugin_data" / PLUGIN_ID / filename


def _context_log_dir(context: Any) -> Path:
    """Resolve the plugin log directory with small host-signature variations."""
    method = getattr(context, "plugin_log_dir", None)
    if callable(method):
        for args in ((PLUGIN_ID,), ()):
            try:
                value = method(*args)
                if value is not None:
                    return _as_path(value)
            except TypeError:
                continue
    # Current ToolPlatform exposes an already plugin-scoped Path property.
    log_dir = getattr(context, "log_dir", None)
    if log_dir is not None and not callable(log_dir):
        return _as_path(log_dir)
    return Path(__file__).resolve().parents[1] / ".plugin_data" / PLUGIN_ID / "logs"


def now_str() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)


_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _safe_path_part(raw: str, fallback: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", raw.strip()).strip(" ._")
    cleaned = cleaned or fallback
    if cleaned.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        cleaned = "_" + cleaned
    return cleaned[:100]


def _safe_device_dir(device: "Device") -> str:
    identity = f"{device.name}\0{device.host}"
    label = _safe_path_part(device.name or device.host, "device")
    suffix = hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"{label}_{suffix}"


def _safe_download_name(remote_path: str) -> str:
    raw = remote_path.replace("\\", "/").rsplit("/", 1)[-1]
    return _safe_path_part(raw, "download.bin")


def _remote_dir(path: str) -> str:
    normalized = path.replace("\\", "/")
    return normalized.rsplit("/", 1)[0] if "/" in normalized else ""


def _join_remote(directory: str, filename: str) -> str:
    return f"{directory.rstrip('/')}/{filename}" if directory.rstrip("/") else filename


@dataclass(frozen=True)
class Device:
    name: str
    host: str
    port: int = 22
    username: str = "root"
    password: str = ""
    key_file: str = ""


class DeviceManager:
    def __init__(self, filename: Path):
        self.filename = filename
        self.devices: list[Device] = []
        self.load()

    def load(self) -> None:
        self.devices = []
        if not self.filename.exists():
            return
        with self.filename.open("r", encoding="utf-8-sig") as stream:
            data = json.load(stream)
        # Existing SSHRunner files are a list.  Accept {"devices": [...]} too.
        if isinstance(data, dict):
            data = data.get("devices", [])
        if not isinstance(data, list):
            raise ValueError("devices.json 顶层必须是设备数组")
        loaded: list[Device] = []
        for index, item in enumerate(data, 1):
            if not isinstance(item, dict):
                raise ValueError(f"第 {index} 个设备不是对象")
            host = str(item.get("host") or "").strip()
            if not host:
                continue
            try:
                port = int(item.get("port") or 22)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"设备 {host} 的端口无效") from exc
            if not 1 <= port <= 65535:
                raise ValueError(f"设备 {host} 的端口超出范围")
            loaded.append(
                Device(
                    name=str(item.get("name") or host),
                    host=host,
                    port=port,
                    username=str(item.get("username") or "root"),
                    password=str(item.get("password") or ""),
                    key_file=str(item.get("key_file") or ""),
                )
            )
        self.devices = loaded

    def save(self) -> None:
        self.filename.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.filename.with_suffix(self.filename.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump([asdict(device) for device in self.devices], stream, indent=4, ensure_ascii=False)
        os.replace(temporary, self.filename)

    def add_device(self, device: Device) -> None:
        self.devices.append(device)
        self.save()

    def remove_device(self, index: int) -> None:
        self.devices.pop(index)
        self.save()

    def import_iplist(
        self,
        path: str,
        default_username: str = "root",
        default_password: str = "",
        default_port: int = 22,
    ) -> int:
        existing = {device.host for device in self.devices}
        added = 0
        with open(path, "r", encoding="utf-8-sig") as stream:
            for raw_line in stream:
                host = raw_line.strip()
                if not host or host.startswith("#") or host in existing:
                    continue
                self.devices.append(
                    Device(host, host, default_port, default_username, default_password)
                )
                existing.add(host)
                added += 1
        if added:
            self.save()
        return added
    def import_pasted_devices(
        self,
        text: str,
        default_username: str = "root",
        default_password: str = "",
        default_port: int = 22,
    ) -> tuple[int, int]:
        """导入“机台号 IP”或仅 IP 的多行文本。"""
        if not 1 <= default_port <= 65535:
            raise ValueError("默认端口超出范围")

        existing_hosts = {device.host for device in self.devices}
        pending_hosts: set[str] = set()
        pending: list[Device] = []
        skipped_duplicates = 0

        for line_number, raw_line in enumerate(text.splitlines(), 1):
            content = raw_line.split("#", 1)[0].strip()
            if not content:
                continue
            fields = content.split()
            if len(fields) == 1:
                name = host = fields[0]
            elif len(fields) == 2:
                name, host = fields
            else:
                raise ValueError(
                    f"第 {line_number} 行格式错误：每行只能填写“IP”或“机台号 IP”"
                )
            try:
                host = str(ipaddress.ip_address(host))
            except ValueError as exc:
                raise ValueError(f"第 {line_number} 行 IP 地址无效：{host}") from exc
            if host in existing_hosts or host in pending_hosts:
                skipped_duplicates += 1
                continue
            pending.append(
                Device(name, host, default_port, default_username or "root", default_password)
            )
            pending_hosts.add(host)

        if pending:
            self.devices.extend(pending)
            self.save()
        return len(pending), skipped_duplicates


class CancelledError(RuntimeError):
    pass


class CancellationToken:
    """One cooperative cancellation mechanism for Python and QThread tasks."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.RLock()
        self._resources: set[Any] = set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self.is_set():
            raise CancelledError("任务已取消")

    def register(self, resource: Any) -> None:
        with self._lock:
            if self._event.is_set():
                self._close(resource)
                raise CancelledError("任务已取消")
            self._resources.add(resource)

    def unregister(self, resource: Any) -> None:
        with self._lock:
            self._resources.discard(resource)

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            resources = tuple(self._resources)
        for resource in resources:
            self._close(resource)

    @staticmethod
    def _close(resource: Any) -> None:
        try:
            if isinstance(resource, subprocess.Popen):
                if resource.poll() is None:
                    resource.terminate()
            else:
                close = getattr(resource, "close", None)
                if callable(close):
                    close()
        except Exception:
            pass


def _require_ssh() -> None:
    if paramiko is None:
        raise RuntimeError("缺少依赖 paramiko，无法执行 SSH 操作")


def open_ssh(device: Device, timeout: int, token: CancellationToken):
    _require_ssh()
    token.check()
    base_kwargs: dict[str, Any] = {
        "hostname": device.host,
        "port": device.port,
        "username": device.username,
        "timeout": timeout,
        "banner_timeout": max(timeout, 20),
        "auth_timeout": max(timeout, 20),
        "look_for_keys": not bool(device.password),
        "allow_agent": not bool(device.password),
    }
    if device.key_file:
        base_kwargs["key_filename"] = device.key_file
    if device.password:
        base_kwargs["password"] = device.password

    def connect(extra: Optional[dict[str, Any]] = None):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = dict(base_kwargs)
        if extra:
            kwargs.update(extra)
        sock = None
        try:
            socket_timeout = int(kwargs.get("timeout", timeout))
            sock = socket.create_connection((device.host, device.port), timeout=socket_timeout)
            token.register(sock)
            kwargs["sock"] = sock
            client.connect(**kwargs)
            token.unregister(sock)
            sock = None  # The connected SSH client now owns it.
            token.register(client)
            return client
        except Exception:
            if sock is not None:
                token.unregister(sock)
                try:
                    sock.close()
                except OSError:
                    pass
            client.close()
            raise

    try:
        return connect()
    except Exception as first_error:
        token.check()
        message = str(first_error).lower()
        if "eof during negotiation" not in message and not isinstance(first_error, SSHException):
            raise
        try:
            return connect(
                {
                    "timeout": max(timeout, 30),
                    "banner_timeout": max(timeout, 30),
                    "auth_timeout": max(timeout, 30),
                    "disabled_algorithms": {"pubkeys": ["rsa-sha2-256", "rsa-sha2-512"]},
                }
            )
        except Exception:
            token.check()
            if not device.password:
                raise
            sock = socket.create_connection((device.host, device.port), timeout=max(timeout, 30))
            token.register(sock)
            transport = None
            try:
                transport = paramiko.Transport(sock)
                token.unregister(sock)
                token.register(transport)
                transport.local_version = "SSH-2.0-OpenSSH_8.9"
                transport.banner_timeout = max(timeout, 30)
                transport.auth_timeout = max(timeout, 30)
                transport.start_client(timeout=max(timeout, 30))
                token.check()
                transport.auth_password(device.username, device.password)
                if not transport.is_authenticated():
                    raise SSHException("password authentication failed")
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client._transport = transport
                token.unregister(transport)
                token.register(client)
                return client
            except Exception:
                token.unregister(sock)
                if transport is not None:
                    token.unregister(transport)
                    transport.close()
                else:
                    sock.close()
                raise


def _close_ssh(client: Any, token: CancellationToken) -> None:
    if client is not None:
        token.unregister(client)
        try:
            client.close()
        except Exception:
            pass


def _exec_checked(client: Any, command: str, timeout: int) -> tuple[Any, Any, Any]:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    return stdin, stdout, stderr


def _stream_command_output(
    stdin: Any,
    stdout: Any,
    stderr: Any,
    timeout: int,
    token: CancellationToken,
    log: Callable[[str], None],
) -> int:
    """Drain stdout/stderr together so neither SSH channel stream can block the other."""
    try:
        stdin.channel.shutdown_write()
    except Exception:
        pass
    channel = stdout.channel
    deadline = time.monotonic() + timeout
    while True:
        token.check()
        drained = False
        while channel.recv_ready():
            drained = True
            text = _decode(channel.recv(CHUNK_SIZE))
            if text:
                log("[stdout] " + text.rstrip("\r\n"))
        while channel.recv_stderr_ready():
            drained = True
            text = _decode(channel.recv_stderr(CHUNK_SIZE))
            if text:
                log("[stderr] " + text.rstrip("\r\n"))
        if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
            return channel.recv_exit_status()
        if time.monotonic() >= deadline:
            channel.close()
            raise TimeoutError(f"远程命令超时，超过 {timeout} 秒")
        if not drained:
            time.sleep(0.03)


def _write_askpass(password: str) -> str:
    if any(char in password for char in "\r\n\0"):
        raise ValueError("SCP 密码不能包含换行或 NUL 字符")
    if os.name == "nt":
        fd, path = tempfile.mkstemp(prefix="ssh_runner_askpass_", suffix=".cmd", text=True)
        escaped = password.replace("^", "^^").replace("%", "%%")
        for char in "&|<>()":
            escaped = escaped.replace(char, "^" + char)
        content = f"@echo off\nsetlocal DisableDelayedExpansion\necho({escaped}\n"
    else:
        fd, path = tempfile.mkstemp(prefix="ssh_runner_askpass_", suffix=".sh", text=True)
        content = f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(password)}\n"
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path


def build_scp_cmd(device: Device, local_file: str, target_path: str) -> list[str]:
    known_hosts = "NUL" if os.name == "nt" else "/dev/null"
    command = [
        "scp",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-O",
    ]
    if device.port != 22:
        command.extend(["-P", str(device.port)])
    if device.key_file:
        command.extend(["-i", device.key_file])
    # The path portion is interpreted by the remote shell.
    remote = f"{device.username}@{device.host}:{shlex.quote(target_path)}"
    command.extend([local_file, remote])
    return command


def run_scp(
    command: list[str],
    password: str,
    timeout: int,
    token: CancellationToken,
    log: Callable[[str], None],
) -> int:
    askpass_path = ""
    process: Optional[subprocess.Popen[str]] = None
    reader: Optional[threading.Thread] = None
    environment = os.environ.copy()
    try:
        if password:
            askpass_path = _write_askpass(password)
            environment.update(
                {"SSH_ASKPASS": askpass_path, "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": "dummy"}
            )
        creationflags = (
            subprocess.CREATE_NO_WINDOW
            if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
            else 0
        )
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            env=environment,
            creationflags=creationflags,
            bufsize=1,
        )
        token.register(process)

        def consume() -> None:
            assert process is not None
            if process.stdout:
                for line in iter(process.stdout.readline, ""):
                    if line:
                        log(line.rstrip("\r\n"))

        reader = threading.Thread(target=consume, name="ssh-runner-scp-output", daemon=True)
        reader.start()
        started = time.monotonic()
        while process.poll() is None:
            token.check()
            if time.monotonic() - started > timeout:
                process.kill()
                raise TimeoutError(f"SCP 超时，超过 {timeout} 秒")
            time.sleep(0.1)
        return process.returncode if process.returncode is not None else 1
    finally:
        if process is not None:
            token.unregister(process)
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=0.8)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
            if process.stdout:
                try:
                    process.stdout.close()
                except OSError:
                    pass
        if reader is not None:
            reader.join(timeout=1.0)
        if askpass_path:
            try:
                os.remove(askpass_path)
            except OSError:
                pass


def push_file_via_ssh_pipe(
    device: Device,
    local_file: str,
    target_path: str,
    timeout: int,
    token: CancellationToken,
    log: Callable[[str], None],
    progress: Optional[Callable[[str, int, int], None]] = None,
) -> None:
    log("[fallback] 使用 SSH 管道推送 (cat > 目标路径)")
    client = open_ssh(device, timeout, token)
    total = os.path.getsize(local_file)
    try:
        directory = _remote_dir(target_path)
        if directory and directory != ".":
            _stdin, stdout, stderr = _exec_checked(
                client, f"mkdir -p {shlex.quote(directory)}", timeout
            )
            if stdout.channel.recv_exit_status() != 0:
                raise RuntimeError(f"创建远端目录失败: {_decode(stderr.read()).strip() or 'unknown'}")
        stdin, stdout, stderr = _exec_checked(
            client, f"cat > {shlex.quote(target_path)}", timeout
        )
        sent = 0
        with open(local_file, "rb") as stream:
            while True:
                token.check()
                chunk = stream.read(CHUNK_SIZE)
                if not chunk:
                    break
                stdin.write(chunk)
                sent += len(chunk)
                if progress:
                    progress(device.name, sent, max(total, 1))
        stdin.channel.shutdown_write()
        exit_code = stdout.channel.recv_exit_status()
        error = _decode(stderr.read()).strip()
        if exit_code != 0:
            raise RuntimeError(f"SSH 管道写入失败 exit={exit_code}: {error}")
        log(f"[fallback_ok] {device.host} -> {target_path}")
    finally:
        _close_ssh(client, token)


def _log_writer(log_file: Path, ui_log: Callable[[str], None]) -> tuple[Callable[[str], None], threading.Lock]:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()

    def write(message: str) -> None:
        line = message.rstrip("\n")
        with lock:
            with log_file.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        ui_log(line)

    return write, lock


def run_ssh_commands(
    devices: list[Device],
    commands: list[str],
    connect_timeout: int,
    command_timeout: int,
    log_file: Path,
    token: CancellationToken,
    ui_log: Callable[[str], None],
    batch_progress: Callable[[int, int], None],
) -> list[dict[str, str]]:
    log, _lock = _log_writer(log_file, ui_log)
    log(f"[{now_str()}] START SSH COMMANDS")
    results: list[dict[str, str]] = []
    total = len(devices)
    batch_progress(0, total)
    for index, device in enumerate(devices, 1):
        if token.is_set():
            break
        client = None
        status = "success"
        message = "Commands completed"
        log(f"\n[{now_str()}] ({index}/{total}) Connecting {device.name} {device.host}:{device.port}")
        try:
            client = open_ssh(device, connect_timeout, token)
            for command_index, command in enumerate(commands, 1):
                token.check()
                log(f"--- CMD {command_index}/{len(commands)}: {command}")
                stdin, stdout, stderr = _exec_checked(client, command, command_timeout)
                code = _stream_command_output(
                    stdin, stdout, stderr, command_timeout, token, log
                )
                log(f"[exit_code] {code}")
                if code != 0:
                    status, message = "failed", f"command exit={code}"
        except CancelledError:
            status, message = "cancelled", "Cancelled"
            log(f"[{now_str()}] STOPPED by user")
        except Exception as exc:
            if token.is_set():
                status, message = "cancelled", "Cancelled"
                log(f"[{now_str()}] STOPPED by user")
            else:
                status, message = "failed", str(exc)
                log(f"[{now_str()}] [error] {device.host}: {exc}")
        finally:
            _close_ssh(client, token)
        results.append({"device": device.name, "status": status, "msg": message})
        batch_progress(len(results), total)
        if status == "cancelled":
            break
    log(f"[{now_str()}] DONE; completed={len(results)}/{total}")
    return results


def run_file_push(
    devices: list[Device],
    local_file: str,
    target_path: str,
    timeout: int,
    log_file: Path,
    token: CancellationToken,
    ui_log: Callable[[str], None],
    batch_progress: Callable[[int, int], None],
    transfer_progress: Callable[[str, int, int], None],
) -> list[dict[str, str]]:
    log, _lock = _log_writer(log_file, ui_log)
    log(f"[{now_str()}] START FILE PUSH; {local_file} -> {target_path}")
    results: list[dict[str, str]] = []
    total = len(devices)
    batch_progress(0, total)
    for device in devices:
        if token.is_set():
            break
        status, message = "success", "Push OK"
        try:
            command = build_scp_cmd(device, local_file, target_path)
            log("[scp_cmd] " + subprocess.list2cmdline(command))
            try:
                code = run_scp(command, device.password, timeout, token, log)
            except CancelledError:
                raise
            except Exception as scp_error:
                code = -1
                log(f"[retry] scp 无法完成({scp_error})，改用 SSH 管道")
            if code != 0:
                if code != -1:
                    log(f"[retry] scp 失败(exit={code})，改用 SSH 管道")
                push_file_via_ssh_pipe(
                    device, local_file, target_path, timeout, token, log, transfer_progress
                )
            log(f"[push_ok] {device.host} -> {target_path}")
        except CancelledError:
            status, message = "cancelled", "Cancelled"
            log(f"[{now_str()}] STOPPED by user")
        except Exception as exc:
            if token.is_set():
                status, message = "cancelled", "Cancelled"
                log(f"[{now_str()}] STOPPED by user")
            else:
                status, message = "failed", str(exc)
                log(f"[{now_str()}] [push_error] {device.host}: {exc}")
        results.append({"device": device.name, "status": status, "msg": message})
        batch_progress(len(results), total)
        if status == "cancelled":
            break
    log(f"[{now_str()}] DONE; completed={len(results)}/{total}")
    return results


class _NativeBridge(QObject):
    log_signal = pyqtSignal(str)
    batch_progress_signal = pyqtSignal(int, int)
    transfer_progress_signal = pyqtSignal(str, int, int)
    result_signal = pyqtSignal(list)


class SSHWorker(QThread):
    progress_signal = pyqtSignal(str, str, str)
    transfer_progress_signal = pyqtSignal(str, int, int)
    batch_progress_signal = pyqtSignal(int, int)
    result_signal = pyqtSignal(list)

    def __init__(
        self,
        devices: Iterable[Device],
        task_type: str,
        token: CancellationToken,
        log_file: Path,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.devices = list(devices)
        self.task_type = task_type
        self.token = token
        self.kwargs = kwargs
        self.log_file = log_file
        self._log_lock = threading.Lock()

    def request_cancel(self) -> None:
        self.requestInterruption()
        self.token.cancel()

    def _cancelled(self) -> bool:
        return self.isInterruptionRequested() or self.token.is_set()

    def _check_cancelled(self) -> None:
        if self._cancelled():
            raise CancelledError("任务已取消")

    def _log(self, device: str, level: str, message: str) -> None:
        display_message = message
        try:
            with self._log_lock:
                self.log_file.parent.mkdir(parents=True, exist_ok=True)
                with self.log_file.open("a", encoding="utf-8") as stream:
                    stream.write(f"[{now_str()}] [{device}] [{level}] {message}\n")
        except Exception as log_error:
            display_message = f"{message}（日志写入失败: {log_error}）"
        try:
            self.progress_signal.emit(device, level, display_message)
        except RuntimeError:
            pass

    def run(self) -> None:
        total = len(self.devices)
        self.batch_progress_signal.emit(0, total)
        results: list[dict[str, str]] = []
        try:
            with ThreadPoolExecutor(max_workers=min(max(total, 1), MAX_WORKERS)) as executor:
                futures = {}
                for device in self.devices:
                    if self._cancelled():
                        break
                    operation = {
                        "upload": self.upload_file,
                        "download": self.download_file,
                        "command": self.execute_gcode,
                    }.get(self.task_type)
                    if operation:
                        futures[executor.submit(operation, device)] = device
                for future in as_completed(futures):
                    device = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        status = "cancelled" if self._cancelled() else "failed"
                        result = {"device": device.name, "status": status, "msg": str(exc)}
                        self._log(device.name, "info" if status == "cancelled" else "error", str(exc))
                    results.append(result)
                    self.batch_progress_signal.emit(len(results), total)
        except Exception as exc:
            self._log("系统", "error", f"后台任务异常: {exc}")
        finally:
            try:
                self.result_signal.emit(results)
            except RuntimeError:
                pass

    def _emit_transfer(self, name: str, transferred: int, total: int) -> None:
        self.transfer_progress_signal.emit(name, transferred, max(total, transferred, 1))

    def upload_file(self, device: Device) -> dict[str, str]:
        local_path = str(self.kwargs["local_path"])
        remote_path = str(self.kwargs["remote_path"])
        timeout = int(self.kwargs.get("timeout", 15))
        client = None
        try:
            self._check_cancelled()
            total = os.path.getsize(local_path)
            self._emit_transfer(device.name, 0, total)
            client = open_ssh(device, timeout, self.token)
            self._log(device.name, "info", "SSH 连接成功")
            directory = _remote_dir(remote_path)
            if directory:
                _stdin, stdout, stderr = _exec_checked(
                    client, f"mkdir -p {shlex.quote(directory)}", timeout
                )
                if stdout.channel.recv_exit_status() != 0:
                    raise RuntimeError(f"创建目录失败: {_decode(stderr.read()).strip()}")
            stdin, stdout, stderr = _exec_checked(
                client, f"dd of={shlex.quote(remote_path)} bs=65536", timeout
            )
            sent, last_percent = 0, -1
            with open(local_path, "rb") as stream:
                while True:
                    self._check_cancelled()
                    chunk = stream.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    stdin.write(chunk)
                    sent += len(chunk)
                    percent = int(sent * 100 / total) if total else 100
                    if percent >= last_percent + 2 or sent >= total:
                        last_percent = percent
                        self._emit_transfer(device.name, sent, total)
            stdin.channel.shutdown_write()
            if stdout.channel.recv_exit_status() != 0:
                raise RuntimeError(f"写入文件失败: {_decode(stderr.read()).strip()}")
            self._emit_transfer(device.name, total, max(total, 1))
            self._log(device.name, "success", f"文件上传成功: {local_path} -> {remote_path}")
            return {"device": device.name, "status": "success", "msg": "Upload OK"}
        except CancelledError as exc:
            self._log(device.name, "info", str(exc))
            return {"device": device.name, "status": "cancelled", "msg": str(exc)}
        except Exception as exc:
            if self._cancelled():
                self._log(device.name, "info", "任务已取消")
                return {"device": device.name, "status": "cancelled", "msg": "任务已取消"}
            self._log(device.name, "error", f"上传失败: {exc}")
            return {"device": device.name, "status": "failed", "msg": str(exc)}
        finally:
            _close_ssh(client, self.token)

    def download_file(self, device: Device) -> dict[str, str]:
        remote_path = str(self.kwargs["remote_path"])
        local_root = Path(self.kwargs["local_dir"])
        timeout = int(self.kwargs.get("timeout", 15))
        client = None
        temporary: Optional[Path] = None
        try:
            self._check_cancelled()
            client = open_ssh(device, timeout, self.token)
            self._log(device.name, "info", "SSH 连接成功")
            quoted = shlex.quote(remote_path)
            _stdin, stdout, _stderr = _exec_checked(
                client, f"test -f {quoted} && printf exists", timeout
            )
            if _decode(stdout.read()) != "exists":
                raise FileNotFoundError(f"远程文件不存在或无法访问: {remote_path}")
            _stdin, stdout, _stderr = _exec_checked(client, f"wc -c < {quoted}", timeout)
            try:
                total = int(_decode(stdout.read()).strip())
            except ValueError:
                total = 0
            root_resolved = local_root.resolve()
            device_dir = root_resolved / _safe_device_dir(device)
            device_dir.mkdir(parents=True, exist_ok=True)
            device_dir = device_dir.resolve()
            try:
                device_dir.relative_to(root_resolved)
            except ValueError as exc:
                raise RuntimeError("设备下载目录越出所选保存目录") from exc
            filename = _safe_download_name(remote_path)
            destination = device_dir / filename
            temp_fd, temp_name = tempfile.mkstemp(
                prefix=f".{filename}.", suffix=".part", dir=device_dir
            )
            os.close(temp_fd)
            temporary = Path(temp_name)
            self._emit_transfer(device.name, 0, total)
            _stdin, stdout, stderr = _exec_checked(
                client, f"dd if={quoted} bs=65536", timeout
            )
            received, last_percent = 0, -1
            with temporary.open("wb") as stream:
                while True:
                    self._check_cancelled()
                    chunk = stdout.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    stream.write(chunk)
                    received += len(chunk)
                    denominator = total or max(received, 1)
                    percent = int(received * 100 / denominator)
                    if percent >= last_percent + 2 or (total and received >= total):
                        last_percent = percent
                        self._emit_transfer(device.name, received, denominator)
            if stdout.channel.recv_exit_status() != 0:
                raise RuntimeError(f"读取文件失败: {_decode(stderr.read()).strip()}")
            os.replace(temporary, destination)
            temporary = None
            self._emit_transfer(device.name, received, total or max(received, 1))
            self._log(device.name, "success", f"文件下载成功: {remote_path} -> {destination}")
            return {"device": device.name, "status": "success", "msg": str(destination)}
        except CancelledError as exc:
            self._log(device.name, "info", str(exc))
            return {"device": device.name, "status": "cancelled", "msg": str(exc)}
        except Exception as exc:
            if self._cancelled():
                self._log(device.name, "info", "任务已取消")
                return {"device": device.name, "status": "cancelled", "msg": "任务已取消"}
            self._log(device.name, "error", f"下载失败: {exc}")
            return {"device": device.name, "status": "failed", "msg": str(exc)}
        finally:
            _close_ssh(client, self.token)
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def execute_gcode(self, device: Device) -> dict[str, str]:
        command = str(self.kwargs.get("command", ""))
        timeout = max(1, min(int(self.kwargs.get("timeout", 30)), 10))
        if requests is None:
            message = "缺少依赖 requests，无法调用 Moonraker"
            self._log(device.name, "error", message)
            return {"device": device.name, "status": "failed", "msg": message}
        session = requests.Session()
        response = None
        try:
            self._check_cancelled()
            self.token.register(session)
            host = f"[{device.host}]" if ":" in device.host and not device.host.startswith("[") else device.host
            url = f"http://{host}:7125/printer/gcode/script"
            self._log(device.name, "info", f"正在连接 {url}")
            response = session.post(url, json={"script": command}, timeout=(timeout, timeout))
            self._check_cancelled()
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
            self._log(device.name, "success", f"命令发送成功: {command}")
            return {"device": device.name, "status": "success", "msg": "Command sent"}
        except CancelledError as exc:
            self._log(device.name, "info", str(exc))
            return {"device": device.name, "status": "cancelled", "msg": str(exc)}
        except Exception as exc:
            if self._cancelled():
                self._log(device.name, "info", "任务已取消")
                return {"device": device.name, "status": "cancelled", "msg": "任务已取消"}
            self._log(device.name, "error", f"命令发送失败: {exc}")
            return {"device": device.name, "status": "failed", "msg": str(exc)}
        finally:
            self.token.unregister(session)
            if response is not None:
                response.close()
            session.close()


class SSHRunnerPlugin(QWidget):
    """ToolPlatform-embeddable SSH runner widget."""

    def __init__(self, context: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.context = context
        self.config_file = _context_config_file(context, "devices.json")
        self.log_dir = _context_log_dir(context)
        self.device_manager = DeviceManager(self.config_file)
        self.worker: Optional[SSHWorker] = None
        self.bg_thread: Optional[threading.Thread] = None
        self._native_bridge: Optional[_NativeBridge] = None
        self._token: Optional[CancellationToken] = None
        self._task_total = 0
        self._alive = True
        self._active = True
        self._build_ui()
        self.refresh_device_table()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(3)

        self.device_group = QGroupBox("设备列表（共用）")
        self.device_group.setMinimumWidth(420)
        device_layout = QVBoxLayout(self.device_group)
        device_layout.setContentsMargins(6, 10, 6, 6)
        device_layout.setSpacing(4)
        self.device_table = QTableWidget(0, 6)
        self.device_table.setHorizontalHeaderLabels(["名称", "主机/IP", "端口", "用户名", "密码", "私钥"])
        self.device_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.device_table.verticalHeader().setDefaultSectionSize(24)
        self.device_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # 点击任意单元格都选中整行；支持多选
        self.device_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.device_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        device_layout.addWidget(self.device_table)
        device_buttons = QHBoxLayout()
        device_buttons.setContentsMargins(0, 0, 0, 0)
        device_buttons.setSpacing(4)
        self.btn_add = QPushButton("添加设备")
        self.btn_remove = QPushButton("删除选中")
        self.btn_clear_all = QPushButton("清空全部")
        self.btn_clear_all.setToolTip("清空所有设备 IP（需要确认）")
        self.btn_import_ip = QPushButton("从 iplist 导入")
        self.btn_import_text = QPushButton("粘贴文本导入")
        self.btn_import_text.setToolTip("粘贴每行“机台号 IP”或仅“IP”的设备清单")
        self.btn_reload = QPushButton("重新加载")
        self.btn_open_config_file = QPushButton("打开设备配置")
        self.btn_open_config_file.setObjectName("sshOpenDeviceConfigButton")
        self.btn_open_config_file.setToolTip(str(self.config_file.resolve()))
        for button in (
            self.btn_add,
            self.btn_remove,
            self.btn_clear_all,
            self.btn_import_ip,
            self.btn_import_text,
            self.btn_reload,
            self.btn_open_config_file,
        ):
            button.setMinimumHeight(26)
            device_buttons.addWidget(button)
        device_buttons.addStretch(1)
        device_layout.addLayout(device_buttons)

        operation_panel = QWidget()
        operation_panel.setMinimumWidth(380)
        operation_layout = QVBoxLayout(operation_panel)
        operation_layout.setContentsMargins(0, 0, 0, 0)
        operation_layout.setSpacing(3)

        self.progress_group = QGroupBox("进度")
        progress_form = QFormLayout(self.progress_group)
        progress_form.setContentsMargins(6, 10, 6, 6)
        progress_form.setHorizontalSpacing(6)
        progress_form.setVerticalSpacing(3)
        self.progress_status = QLabel("就绪")
        self.batch_progress = QProgressBar()
        self.batch_progress.setFormat("批量进度: 0/0")
        self.transfer_progress = QProgressBar()
        self.transfer_progress.setFormat("文件传输: %p%")
        progress_form.addRow("状态:", self.progress_status)
        progress_form.addRow("批量:", self.batch_progress)
        progress_form.addRow("文件:", self.transfer_progress)
        operation_layout.addWidget(self.progress_group)

        self.operation_tabs = QTabWidget()
        self.operation_tabs.addTab(self._build_ssh_tab(), "1. SSH 远程命令")
        self.operation_tabs.addTab(self._build_push_tab(), "2. 文件推送 (SCP)")
        self.operation_tabs.addTab(self._build_transfer_tab(), "3. 上传下载 / G-code")
        operation_layout.addWidget(self.operation_tabs, 1)

        self.horizontal_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.horizontal_splitter.setObjectName("sshHorizontalSplitter")
        self.horizontal_splitter.setChildrenCollapsible(False)
        self.horizontal_splitter.addWidget(self.device_group)
        self.horizontal_splitter.addWidget(operation_panel)
        self.horizontal_splitter.setStretchFactor(0, 1)
        self.horizontal_splitter.setStretchFactor(1, 1)
        self.horizontal_splitter.setSizes([600, 640])
        self.horizontal_splitter.setMinimumHeight(330)

        self.log_group = QGroupBox("执行日志")
        self.log_group.setMinimumHeight(150)
        log_layout = QVBoxLayout(self.log_group)
        log_layout.setContentsMargins(6, 10, 6, 6)
        log_layout.setSpacing(4)
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        log_layout.addWidget(self.log_display)
        log_buttons = QHBoxLayout()
        log_buttons.setContentsMargins(0, 0, 0, 0)
        log_buttons.setSpacing(4)
        self.btn_clear_log = QPushButton("清空日志")
        self.btn_open_log_dir = QPushButton("打开日志目录")
        log_buttons.addWidget(self.btn_clear_log)
        log_buttons.addWidget(self.btn_open_log_dir)
        log_buttons.addStretch(1)
        log_layout.addLayout(log_buttons)

        self.vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        self.vertical_splitter.setObjectName("sshVerticalSplitter")
        self.vertical_splitter.setChildrenCollapsible(False)
        self.vertical_splitter.addWidget(self.horizontal_splitter)
        self.vertical_splitter.addWidget(self.log_group)
        self.vertical_splitter.setStretchFactor(0, 2)
        self.vertical_splitter.setStretchFactor(1, 1)
        self.vertical_splitter.setSizes([580, 240])
        root.addWidget(self.vertical_splitter, 1)

        self.btn_add.clicked.connect(self.add_device)
        self.btn_remove.clicked.connect(self.remove_device)
        self.btn_clear_all.clicked.connect(self.clear_all_devices)
        self.btn_import_ip.clicked.connect(self.import_iplist)
        self.btn_import_text.clicked.connect(self.import_pasted_devices)
        self.btn_reload.clicked.connect(self.reload_devices)
        self.btn_open_config_file.clicked.connect(self.open_config_file)
        self.btn_clear_log.clicked.connect(self.log_display.clear)
        self.btn_open_log_dir.clicked.connect(self.open_log_dir)

    def _build_ssh_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(6)
        form.setVerticalSpacing(4)
        self.connect_timeout = QSpinBox()
        self.connect_timeout.setRange(3, 120)
        self.connect_timeout.setValue(10)
        self.cmd_timeout = QSpinBox()
        self.cmd_timeout.setRange(5, 600)
        self.cmd_timeout.setValue(60)
        form.addRow("连接超时(s):", self.connect_timeout)
        form.addRow("操作超时(s):", self.cmd_timeout)
        layout.addLayout(form)
        layout.addWidget(QLabel("远程命令（每行一条）:"))
        self.ssh_cmd_text = QTextEdit()
        self.ssh_cmd_text.setPlaceholderText("例如:\nuptime\nls /usr/share/klipper/klippy/extras/")
        self.ssh_cmd_text.setMaximumHeight(160)
        layout.addWidget(self.ssh_cmd_text)
        row = QHBoxLayout()
        self.btn_run_ssh = QPushButton("批量执行 SSH 命令")
        self.btn_stop = QPushButton("停止当前任务")
        row.addWidget(self.btn_run_ssh)
        row.addWidget(self.btn_stop)
        layout.addLayout(row)
        self.btn_run_ssh.clicked.connect(self.start_ssh_commands)
        self.btn_stop.clicked.connect(self.stop_task)
        return widget

    def _build_push_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(6)
        form.setVerticalSpacing(4)
        self.push_local = QLineEdit()
        self.push_local.setPlaceholderText("本地文件路径")
        browse = QPushButton("浏览")
        row = QHBoxLayout()
        row.addWidget(self.push_local)
        row.addWidget(browse)
        form.addRow("本地文件:", row)
        self.push_remote = QLineEdit(DEFAULT_PUSH_TARGET)
        form.addRow("远端完整路径:", self.push_remote)
        layout.addLayout(form)
        layout.addWidget(QLabel("优先 scp -O（兼容无 sftp-server），失败则 SSH 管道写入。"))
        self.btn_push = QPushButton("批量推送文件")
        layout.addWidget(self.btn_push)
        layout.addStretch(1)
        browse.clicked.connect(self._browse_push_local)
        self.btn_push.clicked.connect(self.start_file_push)
        return widget

    def _build_transfer_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(6)
        form.setVerticalSpacing(4)
        self.local_file_path = QLineEdit()
        self.local_file_path.setPlaceholderText("上传用：本地文件")
        browse = QPushButton("浏览")
        file_row = QHBoxLayout()
        file_row.addWidget(self.local_file_path)
        file_row.addWidget(browse)
        form.addRow("本地文件:", file_row)
        self.remote_dir_path = QLineEdit()
        self.remote_dir_path.setPlaceholderText("上传用：远端目录，如 /usr/share/klipper/klippy/extras/")
        form.addRow("远端目录:", self.remote_dir_path)
        transfer_row = QHBoxLayout()
        self.btn_upload = QPushButton("批量上传")
        self.btn_download = QPushButton("批量下载")
        transfer_row.addWidget(self.btn_upload)
        transfer_row.addWidget(self.btn_download)
        form.addRow(transfer_row)
        self.command_input = QLineEdit()
        self.command_input.setPlaceholderText("例如: G28")
        form.addRow("G-code:", self.command_input)
        self.btn_gcode = QPushButton("批量发送 G-code (Moonraker :7125)")
        form.addRow(self.btn_gcode)
        layout.addLayout(form)
        layout.addStretch(1)
        browse.clicked.connect(self._browse_local_file)
        self.btn_upload.clicked.connect(self.start_batch_upload)
        self.btn_download.clicked.connect(self.start_batch_download)
        self.btn_gcode.clicked.connect(self.start_batch_gcode)
        return widget

    def _is_busy(self) -> bool:
        return bool(
            (self.worker and self.worker.isRunning())
            or (self.bg_thread and self.bg_thread.is_alive())
        )

    def _set_op_enabled(self, enabled: bool) -> None:
        for button in (
            self.btn_run_ssh,
            self.btn_push,
            self.btn_upload,
            self.btn_download,
            self.btn_gcode,
            self.btn_add,
            self.btn_remove,
            self.btn_clear_all,
            self.btn_import_ip,
            self.btn_import_text,
        ):
            button.setEnabled(enabled)
        self.btn_stop.setEnabled(not enabled)

    def _session_log_path(self, prefix: str) -> Path:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return self.log_dir / f"{prefix}_{stamp}.log"

    def _new_task(self, status: str, total: int) -> CancellationToken:
        token = CancellationToken()
        self._token = token
        self._task_total = total
        self._set_op_enabled(False)
        self.progress_status.setText(status)
        self.batch_progress.setRange(0, max(total, 1))
        self.batch_progress.setValue(0)
        self.batch_progress.setFormat(f"批量进度: 0/{total}")
        self.transfer_progress.setValue(0)
        return token

    def _ready_for_task(self) -> bool:
        if self._is_busy():
            QMessageBox.information(self, "提示", "任务正在运行中")
            return False
        if not self.device_manager.devices:
            self.log("系统", "error", "设备列表为空")
            return False
        return True

    def open_config_file(self) -> None:
        try:
            if not self.config_file.exists():
                self.device_manager.save()
        except Exception as exc:
            QMessageBox.warning(self, "创建设备配置失败", str(exc))
            return
        opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.config_file.resolve())))
        if not opened:
            QMessageBox.warning(self, "打开失败", f"无法打开设备配置：\n{self.config_file.resolve()}")

    def open_log_dir(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.log_dir.resolve())))

    def refresh_device_table(self) -> None:
        self.device_table.setRowCount(len(self.device_manager.devices))
        for row, device in enumerate(self.device_manager.devices):
            values = (
                device.name,
                device.host,
                str(device.port),
                device.username,
                "••••••" if device.password else "",
                device.key_file,
            )
            for column, value in enumerate(values):
                self.device_table.setItem(row, column, QTableWidgetItem(value))

    def reload_devices(self) -> None:
        try:
            self.device_manager.load()
            self.refresh_device_table()
            self.log("系统", "info", f"已重新加载设备，共 {len(self.device_manager.devices)} 台")
        except Exception as exc:
            QMessageBox.critical(self, "加载失败", str(exc))

    def add_device(self) -> None:
        name, ok = QInputDialog.getText(self, "添加设备", "名称:")
        if not ok or not name.strip():
            return
        host, ok = QInputDialog.getText(self, "添加设备", "主机/IP:")
        if not ok or not host.strip():
            return
        port, ok = QInputDialog.getInt(self, "添加设备", "端口:", 22, 1, 65535)
        if not ok:
            return
        username, ok = QInputDialog.getText(self, "添加设备", "用户名:", QLineEdit.EchoMode.Normal, "root")
        if not ok:
            return
        password, ok = QInputDialog.getText(self, "添加设备", "密码（可留空）:", QLineEdit.EchoMode.Password)
        if not ok:
            return
        self.device_manager.add_device(
            Device(name.strip(), host.strip(), port, username.strip() or "root", password)
        )
        self.refresh_device_table()
        self.log("系统", "info", f"已添加设备: {name.strip()}")

    def remove_device(self) -> None:
        # 收集选中的所有行（支持多选）。selectedRows 是真正选中的；currentRow 只跟聚焦相关
        selection_model = self.device_table.selectionModel()
        rows = sorted({idx.row() for idx in selection_model.selectedRows()})
        # 回退：selectedRows 为空时再看 currentRow
        if not rows:
            row = self.device_table.currentRow()
            if row >= 0:
                rows = [row]
        
        if not rows:
            QMessageBox.warning(self, "提示", "请先在设备列表里点选一行或多行")
            return
        
        if any(r < 0 or r >= len(self.device_manager.devices) for r in rows):
            QMessageBox.warning(self, "提示", "选中行与设备列表不一致，请点「重新加载」后再试")
            return
        
        names = [self.device_manager.devices[r].name for r in rows]
        if len(rows) == 1:
            msg = f"删除设备 {names[0]}？"
        else:
            preview = "\n".join(f"  · {n}" for n in names[:6])
            more = "" if len(names) <= 6 else f"\n  … 共 {len(names)} 台"
            msg = f"将删除以下 {len(rows)} 台设备：\n{preview}{more}"
        answer = QMessageBox.question(
            self, "确认删除", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        
        # 从大到小删除，避免索引错位
        for r in reversed(rows):
            self.device_manager.remove_device(r)
        self.refresh_device_table()
        self.log("系统", "info", f"已删除 {len(rows)} 台设备: {', '.join(names)}")

    def clear_all_devices(self) -> None:
        """清空所有设备 IP。二次确认避免误操作。"""
        total = len(self.device_manager.devices)
        if total == 0:
            QMessageBox.information(self, "提示", "设备列表已为空")
            return
        answer = QMessageBox.question(
            self,
            "确认清空",
            f"将删除全部 {total} 台设备，此操作不可撤销（配置文件会被覆盖）。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.device_manager.devices.clear()
        try:
            self.device_manager.save()
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", f"清空后写入配置失败：\n{exc}")
        self.refresh_device_table()
        self.log("系统", "info", f"已清空全部设备（原共 {total} 台）")

    def import_iplist(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, "选择 iplist.txt", "", "Text (*.txt);;All (*.*)")
        if not path:
            return
        username, ok = QInputDialog.getText(self, "默认用户名", "用户名:", QLineEdit.EchoMode.Normal, "root")
        if not ok:
            return
        password, ok = QInputDialog.getText(self, "默认密码", "密码（可留空）:", QLineEdit.EchoMode.Password)
        if not ok:
            return
        try:
            count = self.device_manager.import_iplist(path, username.strip() or "root", password, 22)
            self.refresh_device_table()
            self.log("系统", "info", f"从 iplist 新增 {count} 台设备")
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", str(exc))

    def import_pasted_devices(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("粘贴设备清单")
        dialog.resize(600, 430)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("每行填写“机台号 IP”或仅“IP”。仅 IP 时将以 IP 作为设备名称。"))
        text_edit = QTextEdit()
        text_edit.setPlaceholderText(
            "n1-12-50 172.16.180.148\n172.16.180.155\n# 以 # 开头或行内 # 之后的内容会被忽略"
        )
        layout.addWidget(text_edit, 1)

        defaults = QFormLayout()
        username_edit = QLineEdit("root")
        password_edit = QLineEdit()
        password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        port_spin = QSpinBox()
        port_spin.setRange(1, 65535)
        port_spin.setValue(22)
        defaults.addRow("默认用户名:", username_edit)
        defaults.addRow("默认密码:", password_edit)
        defaults.addRow("默认端口:", port_spin)
        layout.addLayout(defaults)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("导入")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if not text_edit.toPlainText().strip():
            QMessageBox.warning(self, "提示", "请先粘贴至少一个 IP 地址")
            return
        try:
            added, skipped = self.device_manager.import_pasted_devices(
                text_edit.toPlainText(),
                username_edit.text().strip() or "root",
                password_edit.text(),
                port_spin.value(),
            )
            self.refresh_device_table()
            detail = f"已从粘贴文本新增 {added} 台设备"
            if skipped:
                detail += f"，跳过 {skipped} 条重复 IP"
            self.log("系统", "info", detail)
            QMessageBox.information(self, "导入完成", detail)
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", str(exc))

    def _start_native(self, target: Callable[..., list[dict[str, str]]], kwargs: dict[str, Any]) -> None:
        bridge = _NativeBridge()
        self._native_bridge = bridge
        bridge.log_signal.connect(self._append_plain_log)
        bridge.batch_progress_signal.connect(self.on_batch_progress)
        bridge.transfer_progress_signal.connect(self.on_transfer_progress)
        bridge.result_signal.connect(self.on_batch_finished)

        def work() -> None:
            try:
                results = target(
                    **kwargs,
                    ui_log=bridge.log_signal.emit,
                    batch_progress=bridge.batch_progress_signal.emit,
                    **({"transfer_progress": bridge.transfer_progress_signal.emit} if target is run_file_push else {}),
                )
            except Exception as exc:
                try:
                    bridge.log_signal.emit(f"[{now_str()}] [error] {exc}")
                except RuntimeError:
                    pass
                results = [{"device": "系统", "status": "failed", "msg": str(exc)}]
            try:
                bridge.result_signal.emit(results)
            except RuntimeError:
                pass
            finally:
                _ORPHANED_THREADS.discard(threading.current_thread())

        thread = threading.Thread(target=work, name="ssh-runner-native", daemon=True)
        self.bg_thread = thread
        thread.start()

    def stop_task(self) -> None:
        if self._token:
            self._token.cancel()
        if self.worker and self.worker.isRunning():
            self.worker.request_cancel()
        self.log("系统", "info", "已请求停止，正在关闭活动连接…")

    def start_ssh_commands(self) -> None:
        if not self._ready_for_task():
            return
        commands = [
            line.strip()
            for line in self.ssh_cmd_text.toPlainText().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not commands:
            self.log("系统", "error", "请输入至少一条 SSH 命令")
            return
        # Read every QWidget value before the background thread starts.
        connect_timeout = int(self.connect_timeout.value())
        command_timeout = int(self.cmd_timeout.value())
        devices = list(self.device_manager.devices)
        log_file = self._session_log_path("ssh_cmd")
        token = self._new_task("SSH 命令执行中…", len(devices))
        self._start_native(
            run_ssh_commands,
            {
                "devices": devices,
                "commands": commands,
                "connect_timeout": connect_timeout,
                "command_timeout": command_timeout,
                "log_file": log_file,
                "token": token,
            },
        )
        self.log("系统", "info", f"开始 SSH 批量命令，共 {len(devices)} 台，日志: {log_file}")

    def start_file_push(self) -> None:
        if not self._ready_for_task():
            return
        local_file = self.push_local.text().strip()
        target_path = self.push_remote.text().strip()
        if not os.path.isfile(local_file):
            self.log("系统", "error", f"本地文件不存在: {local_file}")
            return
        if not target_path:
            self.log("系统", "error", "请填写远端完整路径")
            return
        timeout = int(self.cmd_timeout.value())
        devices = list(self.device_manager.devices)
        log_file = self._session_log_path("file_push")
        token = self._new_task("文件推送中…", len(devices))
        self._start_native(
            run_file_push,
            {
                "devices": devices,
                "local_file": local_file,
                "target_path": target_path,
                "timeout": timeout,
                "log_file": log_file,
                "token": token,
            },
        )
        self.log("系统", "info", f"开始文件推送 -> {target_path}，共 {len(devices)} 台")

    def _browse_push_local(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, "选择本地文件")
        if path:
            self.push_local.setText(path)

    def _browse_local_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, "选择本地文件")
        if path:
            self.local_file_path.setText(path)

    def _connect_worker(self, worker: SSHWorker) -> None:
        worker.progress_signal.connect(self.log)
        worker.batch_progress_signal.connect(self.on_batch_progress)
        worker.transfer_progress_signal.connect(self.on_transfer_progress)
        worker.result_signal.connect(self.on_batch_finished)
        worker.finished.connect(self._worker_thread_finished)

    def _start_worker(self, task_type: str, status: str, log_prefix: str, **kwargs: Any) -> None:
        devices = list(self.device_manager.devices)
        token = self._new_task(status, len(devices))
        worker = SSHWorker(devices, task_type, token, self._session_log_path(log_prefix), **kwargs)
        self.worker = worker
        self._connect_worker(worker)
        worker.start()

    def start_batch_upload(self) -> None:
        if not self._ready_for_task():
            return
        local_path = self.local_file_path.text().strip()
        remote_dir = self.remote_dir_path.text().strip()
        if not os.path.isfile(local_path) or not remote_dir:
            self.log("系统", "error", "请填写有效的本地文件和远端目录")
            return
        timeout = int(self.connect_timeout.value())
        remote_path = _join_remote(remote_dir, os.path.basename(local_path))
        self._start_worker(
            "upload", "上传中…", "upload", local_path=local_path, remote_path=remote_path, timeout=timeout
        )
        self.log("系统", "info", f"开始并发上传到 {remote_path}")

    def start_batch_download(self) -> None:
        if not self._ready_for_task():
            return
        remote_path, ok = QInputDialog.getText(self, "批量下载", "远端文件完整路径:")
        if not ok or not remote_path.strip():
            return
        local_dir = QFileDialog.getExistingDirectory(self, "选择保存目录")
        if not local_dir:
            return
        timeout = int(self.connect_timeout.value())
        self._start_worker(
            "download",
            "下载中…",
            "download",
            remote_path=remote_path.strip(),
            local_dir=local_dir,
            timeout=timeout,
        )
        self.log("系统", "info", f"开始并发下载 {remote_path.strip()}")

    def start_batch_gcode(self) -> None:
        if not self._ready_for_task():
            return
        command = self.command_input.text().strip()
        if not command:
            self.log("系统", "error", "请输入 G-code")
            return
        timeout = int(self.cmd_timeout.value())
        self._start_worker("command", "发送 G-code…", "gcode", command=command, timeout=timeout)
        self.log("系统", "info", f"开始批量发送 G-code: {command}")

    def log(self, device_name: str, level: str, message: str) -> None:
        if not self._alive:
            return
        colors = {"info": "#1f2937", "success": "#15803d", "error": "#b91c1c", "progress": "#c2410c"}
        safe_text = html.escape(f"[{now_str()}] [{device_name}] {message}").replace("\n", "<br>")
        self.log_display.append(f'<span style="color:{colors.get(level, "#1f2937")}">{safe_text}</span>')
        scrollbar = self.log_display.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _append_plain_log(self, text: str) -> None:
        if self._alive:
            self.log_display.append(html.escape(text).replace("\n", "<br>"))

    def on_batch_progress(self, done: int, total: int) -> None:
        if not self._alive:
            return
        self.batch_progress.setRange(0, max(total, 1))
        self.batch_progress.setValue(done)
        self.batch_progress.setFormat(f"批量进度: {done}/{total}")
        self.progress_status.setText(f"已完成 {done}/{total}")

    def on_transfer_progress(self, device_name: str, transferred: int, total: int) -> None:
        if not self._alive:
            return
        percent = max(0, min(100, int(transferred * 100 / total) if total else 0))
        self.transfer_progress.setValue(percent)
        self.transfer_progress.setFormat(
            f"{device_name}: {percent}% ({transferred / 1048576:.2f}/{total / 1048576:.2f} MB)"
        )

    def on_batch_finished(self, results: list[dict[str, str]]) -> None:
        if not self._alive:
            return
        success = sum(result.get("status") == "success" for result in results)
        returned_cancelled = sum(result.get("status") == "cancelled" for result in results)
        returned_failed = len(results) - success - returned_cancelled
        missing = max(0, self._task_total - len(results))
        was_cancelled = bool(self._token and self._token.is_set())
        cancelled = returned_cancelled + (missing if was_cancelled else 0)
        failed = returned_failed + (0 if was_cancelled else missing)
        completed = len(results)
        self._set_op_enabled(True)
        self._token = None
        self._task_total = 0
        self.batch_progress.setValue(completed)
        self.batch_progress.setFormat(f"批量进度: {completed}/{completed + missing}")
        self.progress_status.setText(f"完成：成功 {success}，失败 {failed}，取消 {cancelled}")
        self.log("系统", "info", f"批量操作完成：成功 {success}，失败 {failed}，取消 {cancelled}")
        if self._active:
            if failed:
                QMessageBox.warning(self, "部分失败", f"成功 {success}，失败 {failed}，取消 {cancelled}")
            elif not cancelled:
                QMessageBox.information(self, "完成", "全部成功")

    def _worker_thread_finished(self) -> None:
        worker = self.sender()
        if worker is self.worker:
            self.worker = None
        if isinstance(worker, QThread):
            worker.deleteLater()

    def deactivate(self) -> None:
        """Pause nonessential UI notifications; running work is not killed."""
        self._active = False

    def activate(self) -> None:
        self._active = True

    def shutdown(self) -> None:
        """Request cooperative stop and wait briefly without force-terminating work."""
        if not self._alive:
            return
        self._alive = False
        self._active = False
        if self._token:
            self._token.cancel()
        worker = self.worker
        if worker and worker.isRunning():
            worker.request_cancel()
            if not worker.wait(12000):
                for signal, slot in (
                    (worker.progress_signal, self.log),
                    (worker.batch_progress_signal, self.on_batch_progress),
                    (worker.transfer_progress_signal, self.on_transfer_progress),
                    (worker.result_signal, self.on_batch_finished),
                    (worker.finished, self._worker_thread_finished),
                ):
                    try:
                        signal.disconnect(slot)
                    except (TypeError, RuntimeError):
                        pass
                _ORPHANED_WORKERS.add(worker)
                worker.finished.connect(worker.deleteLater)
                worker.finished.connect(lambda w=worker: _ORPHANED_WORKERS.discard(w))
            self.worker = None
        thread = self.bg_thread
        if thread and thread.is_alive():
            thread.join(timeout=5.0)
            if thread.is_alive():
                _ORPHANED_THREADS.add(thread)
        bridge = self._native_bridge
        if bridge is not None:
            for signal, slot in (
                (bridge.log_signal, self._append_plain_log),
                (bridge.batch_progress_signal, self.on_batch_progress),
                (bridge.transfer_progress_signal, self.on_transfer_progress),
                (bridge.result_signal, self.on_batch_finished),
            ):
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
        self.bg_thread = None
        self._native_bridge = None

    def closeEvent(self, event: Any) -> None:
        self.shutdown()
        event.accept()


def create_plugin(context: Any, parent: Optional[QWidget] = None) -> SSHRunnerPlugin:
    return SSHRunnerPlugin(context, parent)


class _DebugContext:
    def __init__(self) -> None:
        self.root = Path(__file__).resolve().parent / "_debug_data"

    def plugin_config_file(self, plugin_id: str, filename: str) -> Path:
        return self.root / plugin_id / filename

    def plugin_log_dir(self, plugin_id: str) -> Path:
        return self.root / plugin_id / "logs"


def _debug_main() -> int:
    app = QApplication(sys.argv)
    widget = create_plugin(_DebugContext())
    widget.setWindowTitle(PLUGIN_META["name"])
    widget.resize(1180, 860)
    widget.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(_debug_main())
