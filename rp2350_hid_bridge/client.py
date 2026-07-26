from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .native import NativeSessionOptions, _NativeApi

DEFAULT_VID = 0xCAFE
DEFAULT_PID = 0x2350


@dataclass
class HidBridgeOptions:
    port: str | None = None
    baudrate: int = 115200
    timeout: float = 1.0
    retries: int = 2
    vid: int = DEFAULT_VID
    pid: int = DEFAULT_PID


class HidSession:
    def __init__(
        self,
        port: str | None = None,
        *,
        app_dir: str | os.PathLike[str] | None = None,
        dll_path: str | os.PathLike[str] | None = None,
        baudrate: int = 115200,
        timeout: float = 1.0,
        retries: int = 2,
        vid: int = DEFAULT_VID,
        pid: int = DEFAULT_PID,
        _api: Any = None,
    ):
        self.options = HidBridgeOptions(
            port=port,
            baudrate=baudrate,
            timeout=timeout,
            retries=retries,
            vid=vid,
            pid=pid,
        )
        self._api = _api or _NativeApi(app_dir=app_dir, dll_path=dll_path)
        self._handle = 0
        self._lock = threading.RLock()

    def __enter__(self) -> "HidSession":
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _native_options(self, port: str) -> NativeSessionOptions:
        if self.options.baudrate <= 0:
            raise ValueError("baudrate must be positive")
        if self.options.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.options.retries < 0:
            raise ValueError("retries must not be negative")
        timeout_ms = round(self.options.timeout * 1000)
        if timeout_ms <= 0:
            raise ValueError("timeout is too small to represent in milliseconds")
        return NativeSessionOptions(
            port=port,
            baudrate=self.options.baudrate,
            timeout_ms=timeout_ms,
            retries=self.options.retries,
        )

    def open(self) -> None:
        with self._lock:
            if self._handle:
                return
            port = self.options.port or self._api.find_port(
                self.options.vid,
                self.options.pid,
            )
            if not port:
                raise RuntimeError("RP2350 HID bridge serial port not found")
            candidate = self._api.create(self._native_options(port))
            try:
                self._api.open(candidate)
            except Exception:
                self._api.release(candidate)
                raise
            self._handle = candidate

    def close(self) -> None:
        with self._lock:
            handle = self._handle
            self._handle = 0
            if handle:
                self._api.release(handle)

    def is_open(self) -> bool:
        with self._lock:
            return bool(self._handle and self._api.is_open(self._handle))

    def _require_handle(self) -> int:
        if not self._handle:
            raise RuntimeError("RP2350 HID session is closed")
        return self._handle

    def _call(self, method: str, *args: object):
        with self._lock:
            handle = self._require_handle()
            return getattr(self._api, method)(handle, *args)

    @property
    def native_handle(self) -> int:
        with self._lock:
            handle = self._require_handle()
            if not self._api.is_open(handle):
                raise RuntimeError("RP2350 HID session is closed")
            return int(handle)

    @property
    def dll_path(self) -> Path:
        return Path(self._api.path).resolve()

    def ping(self) -> None:
        self._call("ping")

    def info(self) -> bytes:
        return self._call("info")

    def caps(self) -> bytes:
        return self._call("caps")

    def type_text(self, text: str) -> None:
        self._call("type_text", text)

    def key_tap(self, combo: str) -> None:
        self._call("key_tap", combo)

    def key_down(self, combo: str) -> None:
        self._call("key_down", combo)

    def key_up(self, combo: str) -> None:
        self._call("key_up", combo)

    def mouse_move(self, dx: int, dy: int) -> None:
        if not -32768 <= dx <= 32767 or not -32768 <= dy <= 32767:
            raise ValueError("mouse movement must fit signed 16-bit integers")
        self._call("mouse_move", dx, dy)

    def mouse_click(self, button: str = "left") -> None:
        self._call("mouse_click", button)

    def mouse_down(self, button: str = "left") -> None:
        self._call("mouse_down", button)

    def mouse_up(self, button: str = "left") -> None:
        self._call("mouse_up", button)

    def mouse_wheel(self, delta: int) -> None:
        if not -128 <= delta <= 127:
            raise ValueError("mouse wheel delta must fit signed 8-bit integer")
        self._call("mouse_wheel", delta)

    def wait_ms(self, milliseconds: int) -> None:
        if not 0 <= milliseconds <= 0xFFFFFFFF:
            raise ValueError("wait duration must fit unsigned 32-bit milliseconds")
        self._call("wait_ms", milliseconds)

    def stop_all(self) -> None:
        self._call("stop_all")

    def run_script(self, script: str) -> None:
        self._call("run_script", script)


class HidBridge(HidSession):
    def __init__(self, options: HidBridgeOptions | None = None, **kwargs: Any):
        selected = options or HidBridgeOptions()
        super().__init__(
            selected.port,
            baudrate=selected.baudrate,
            timeout=selected.timeout,
            retries=selected.retries,
            vid=selected.vid,
            pid=selected.pid,
            **kwargs,
        )


def find_port(
    vid: int = DEFAULT_VID,
    pid: int = DEFAULT_PID,
    *,
    app_dir: str | os.PathLike[str] | None = None,
    dll_path: str | os.PathLike[str] | None = None,
    _api: Any = None,
) -> str | None:
    api = _api or _NativeApi(app_dir=app_dir, dll_path=dll_path)
    return api.find_port(vid, pid)


def list_ports(
    vid: int = DEFAULT_VID,
    pid: int = DEFAULT_PID,
    *,
    app_dir: str | os.PathLike[str] | None = None,
    dll_path: str | os.PathLike[str] | None = None,
    _api: Any = None,
) -> list[str]:
    port = find_port(
        vid,
        pid,
        app_dir=app_dir,
        dll_path=dll_path,
        _api=_api,
    )
    return [port] if port else []
