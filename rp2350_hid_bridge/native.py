from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ABI_MAJOR = 1
MINIMUM_ABI_MINOR = 0
FEATURE_SHARED_SESSION = 1 << 0
FEATURE_PORT_DISCOVERY = 1 << 1
REQUIRED_FEATURES = FEATURE_SHARED_SESSION | FEATURE_PORT_DISCOVERY

STATUS_TIMEOUT = -2
STATUS_ERROR = -1
STATUS_OK = 0
STATUS_FOUND = 1


class _CHidAbiInfo(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_major", ctypes.c_uint32),
        ("abi_minor", ctypes.c_uint32),
        ("options_size", ctypes.c_uint32),
        ("feature_flags", ctypes.c_uint64),
    ]


class _CHidOptions(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("port", ctypes.c_char_p),
        ("baud", ctypes.c_uint32),
        ("timeout_ms", ctypes.c_uint32),
        ("retries", ctypes.c_int32),
        ("heartbeat_interval_ms", ctypes.c_uint32),
    ]


@dataclass(frozen=True)
class HidAbiInfo:
    abi_major: int
    abi_minor: int
    options_size: int
    feature_flags: int


@dataclass(frozen=True)
class NativeSessionOptions:
    port: str
    baudrate: int = 115200
    timeout_ms: int = 1000
    retries: int = 2
    heartbeat_interval_ms: int = 500


_REQUIRED_EXPORTS = (
    "rp2350_hid_get_abi_info",
    "rp2350_hid_last_error",
    "rp2350_hid_find_port",
    "rp2350_hid_session_create",
    "rp2350_hid_session_retain",
    "rp2350_hid_session_release",
    "rp2350_hid_session_open",
    "rp2350_hid_session_is_open",
    "rp2350_hid_session_ping",
    "rp2350_hid_session_info",
    "rp2350_hid_session_caps",
    "rp2350_hid_session_type_text",
    "rp2350_hid_session_key_tap",
    "rp2350_hid_session_key_down",
    "rp2350_hid_session_key_up",
    "rp2350_hid_session_mouse_move",
    "rp2350_hid_session_mouse_click",
    "rp2350_hid_session_mouse_down",
    "rp2350_hid_session_mouse_up",
    "rp2350_hid_session_mouse_wheel",
    "rp2350_hid_session_wait_ms",
    "rp2350_hid_session_stop_all",
    "rp2350_hid_session_run_script",
)


def _development_repository(package_dir: Path) -> Path:
    for parent in package_dir.parents:
        candidate = parent / "rp2350_hid_bridge_cpp"
        if candidate.is_dir():
            return candidate
    return package_dir.parents[4] / "rp2350_hid_bridge_cpp"


def find_hid_dll(
    *,
    app_dir: str | os.PathLike[str] | None = None,
    dll_path: str | os.PathLike[str] | None = None,
) -> Path:
    if dll_path is not None:
        candidate = Path(dll_path).resolve()
    elif app_dir is not None:
        candidate = Path(app_dir).resolve() / "rp2350_hid_bridge.dll"
    elif os.environ.get("RP2350_HID_BRIDGE_DLL"):
        candidate = Path(os.environ["RP2350_HID_BRIDGE_DLL"]).resolve()
    else:
        package_dir = Path(__file__).resolve().parent
        repository = _development_repository(package_dir)
        candidates = (
            repository / "build" / "Release" / "rp2350_hid_bridge.dll",
            repository / "build-shared-plan" / "Release" / "rp2350_hid_bridge.dll",
        )
        candidate = next((value for value in candidates if value.is_file()), candidates[0])
    if not candidate.is_file():
        raise FileNotFoundError(f"RP2350 HID DLL does not exist: {candidate}")
    return candidate


def _require_exports(dll: Any, path: Path) -> None:
    missing = [name for name in _REQUIRED_EXPORTS if not hasattr(dll, name)]
    if missing:
        exports = ", ".join(missing)
        raise RuntimeError(f"RP2350 HID DLL is missing exports at {path}: {exports}")


def _validate_abi(info: HidAbiInfo, path: Path) -> None:
    if info.abi_major != ABI_MAJOR:
        raise RuntimeError(
            f"RP2350 HID ABI major mismatch at {path}: "
            f"expected {ABI_MAJOR}, got {info.abi_major}"
        )
    if info.abi_minor < MINIMUM_ABI_MINOR:
        raise RuntimeError(
            f"RP2350 HID ABI minor is too old at {path}: "
            f"need {MINIMUM_ABI_MINOR}, got {info.abi_minor}"
        )
    expected_options_size = ctypes.sizeof(_CHidOptions)
    if info.options_size != expected_options_size:
        raise RuntimeError(
            f"RP2350 HID options size mismatch at {path}: "
            f"expected {expected_options_size}, got {info.options_size}"
        )
    missing = REQUIRED_FEATURES & ~info.feature_flags
    if missing:
        raise RuntimeError(
            f"RP2350 HID DLL is missing required features at {path}: 0x{missing:X}"
        )


def _configure_exports(dll: Any) -> None:
    handle = ctypes.c_void_p
    uint8_pointer = ctypes.POINTER(ctypes.c_uint8)

    dll.rp2350_hid_get_abi_info.argtypes = [ctypes.POINTER(_CHidAbiInfo)]
    dll.rp2350_hid_get_abi_info.restype = ctypes.c_int32
    dll.rp2350_hid_last_error.argtypes = []
    dll.rp2350_hid_last_error.restype = ctypes.c_char_p
    dll.rp2350_hid_find_port.argtypes = [
        ctypes.c_uint16,
        ctypes.c_uint16,
        ctypes.POINTER(ctypes.c_char),
        ctypes.c_uint32,
    ]
    dll.rp2350_hid_find_port.restype = ctypes.c_int32
    dll.rp2350_hid_session_create.argtypes = [
        ctypes.POINTER(_CHidOptions),
        ctypes.POINTER(handle),
    ]
    dll.rp2350_hid_session_create.restype = ctypes.c_int32
    dll.rp2350_hid_session_retain.argtypes = [handle]
    dll.rp2350_hid_session_retain.restype = ctypes.c_int32
    dll.rp2350_hid_session_release.argtypes = [handle]
    dll.rp2350_hid_session_release.restype = None
    dll.rp2350_hid_session_open.argtypes = [handle]
    dll.rp2350_hid_session_open.restype = ctypes.c_int32
    dll.rp2350_hid_session_is_open.argtypes = [handle, ctypes.POINTER(ctypes.c_int32)]
    dll.rp2350_hid_session_is_open.restype = ctypes.c_int32
    dll.rp2350_hid_session_ping.argtypes = [handle]
    dll.rp2350_hid_session_ping.restype = ctypes.c_int32
    for name in ("rp2350_hid_session_info", "rp2350_hid_session_caps"):
        function = getattr(dll, name)
        function.argtypes = [
            handle,
            uint8_pointer,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        function.restype = ctypes.c_int32
    for name in (
        "rp2350_hid_session_type_text",
        "rp2350_hid_session_key_tap",
        "rp2350_hid_session_key_down",
        "rp2350_hid_session_key_up",
        "rp2350_hid_session_mouse_click",
        "rp2350_hid_session_mouse_down",
        "rp2350_hid_session_mouse_up",
        "rp2350_hid_session_run_script",
    ):
        function = getattr(dll, name)
        function.argtypes = [handle, ctypes.c_char_p]
        function.restype = ctypes.c_int32
    dll.rp2350_hid_session_mouse_move.argtypes = [
        handle,
        ctypes.c_int16,
        ctypes.c_int16,
    ]
    dll.rp2350_hid_session_mouse_move.restype = ctypes.c_int32
    dll.rp2350_hid_session_mouse_wheel.argtypes = [handle, ctypes.c_int8]
    dll.rp2350_hid_session_mouse_wheel.restype = ctypes.c_int32
    dll.rp2350_hid_session_wait_ms.argtypes = [handle, ctypes.c_uint32]
    dll.rp2350_hid_session_wait_ms.restype = ctypes.c_int32
    dll.rp2350_hid_session_stop_all.argtypes = [handle]
    dll.rp2350_hid_session_stop_all.restype = ctypes.c_int32


class _NativeApi:
    def __init__(
        self,
        *,
        app_dir: str | os.PathLike[str] | None = None,
        dll_path: str | os.PathLike[str] | None = None,
    ):
        self.path = find_hid_dll(app_dir=app_dir, dll_path=dll_path)
        self._dll = ctypes.CDLL(str(self.path))
        _require_exports(self._dll, self.path)
        _configure_exports(self._dll)

        raw = _CHidAbiInfo()
        raw.struct_size = ctypes.sizeof(raw)
        self._check(self._dll.rp2350_hid_get_abi_info(ctypes.byref(raw)))
        self.abi_info = HidAbiInfo(
            abi_major=int(raw.abi_major),
            abi_minor=int(raw.abi_minor),
            options_size=int(raw.options_size),
            feature_flags=int(raw.feature_flags),
        )
        _validate_abi(self.abi_info, self.path)

    def _message(self) -> str:
        value = self._dll.rp2350_hid_last_error()
        if not value:
            return "RP2350 HID command failed"
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    def _check(self, status: int) -> None:
        if status == STATUS_OK:
            return
        if status == STATUS_TIMEOUT:
            raise TimeoutError(self._message())
        raise RuntimeError(self._message())

    @staticmethod
    def _handle(value: int) -> ctypes.c_void_p:
        if not value:
            raise RuntimeError("RP2350 HID session handle is closed")
        return ctypes.c_void_p(value)

    @staticmethod
    def _text(value: str) -> bytes:
        return value.encode("utf-8")

    def find_port(self, vid: int, pid: int) -> str | None:
        output = ctypes.create_string_buffer(64)
        status = int(
            self._dll.rp2350_hid_find_port(
                vid,
                pid,
                output,
                ctypes.sizeof(output),
            )
        )
        if status == STATUS_OK:
            return None
        if status != STATUS_FOUND:
            self._check(status)
        return output.value.decode("ascii")

    def create(self, options: NativeSessionOptions) -> int:
        port = self._text(options.port)
        native_options = _CHidOptions(
            struct_size=ctypes.sizeof(_CHidOptions),
            port=port,
            baud=options.baudrate,
            timeout_ms=options.timeout_ms,
            retries=options.retries,
            heartbeat_interval_ms=options.heartbeat_interval_ms,
        )
        handle = ctypes.c_void_p()
        self._check(
            self._dll.rp2350_hid_session_create(
                ctypes.byref(native_options),
                ctypes.byref(handle),
            )
        )
        if not handle.value:
            raise RuntimeError("RP2350 HID DLL returned a null session handle")
        return int(handle.value)

    def retain(self, handle: int) -> None:
        self._check(self._dll.rp2350_hid_session_retain(self._handle(handle)))

    def release(self, handle: int) -> None:
        if handle:
            self._dll.rp2350_hid_session_release(ctypes.c_void_p(handle))

    def open(self, handle: int) -> None:
        self._check(self._dll.rp2350_hid_session_open(self._handle(handle)))

    def is_open(self, handle: int) -> bool:
        value = ctypes.c_int32()
        self._check(
            self._dll.rp2350_hid_session_is_open(
                self._handle(handle),
                ctypes.byref(value),
            )
        )
        return bool(value.value)

    def ping(self, handle: int) -> None:
        self._check(self._dll.rp2350_hid_session_ping(self._handle(handle)))

    def _bytes_command(self, name: str, handle: int) -> bytes:
        output = (ctypes.c_uint8 * 256)()
        bytes_written = ctypes.c_uint32()
        function = getattr(self._dll, name)
        self._check(
            function(
                self._handle(handle),
                output,
                len(output),
                ctypes.byref(bytes_written),
            )
        )
        return bytes(output[: bytes_written.value])

    def info(self, handle: int) -> bytes:
        return self._bytes_command("rp2350_hid_session_info", handle)

    def caps(self, handle: int) -> bytes:
        return self._bytes_command("rp2350_hid_session_caps", handle)

    def _string_command(self, name: str, handle: int, value: str) -> None:
        function = getattr(self._dll, name)
        self._check(function(self._handle(handle), self._text(value)))

    def type_text(self, handle: int, text: str) -> None:
        self._string_command("rp2350_hid_session_type_text", handle, text)

    def key_tap(self, handle: int, combo: str) -> None:
        self._string_command("rp2350_hid_session_key_tap", handle, combo)

    def key_down(self, handle: int, combo: str) -> None:
        self._string_command("rp2350_hid_session_key_down", handle, combo)

    def key_up(self, handle: int, combo: str) -> None:
        self._string_command("rp2350_hid_session_key_up", handle, combo)

    def mouse_move(self, handle: int, dx: int, dy: int) -> None:
        self._check(
            self._dll.rp2350_hid_session_mouse_move(self._handle(handle), dx, dy)
        )

    def mouse_click(self, handle: int, button: str) -> None:
        self._string_command("rp2350_hid_session_mouse_click", handle, button)

    def mouse_down(self, handle: int, button: str) -> None:
        self._string_command("rp2350_hid_session_mouse_down", handle, button)

    def mouse_up(self, handle: int, button: str) -> None:
        self._string_command("rp2350_hid_session_mouse_up", handle, button)

    def mouse_wheel(self, handle: int, delta: int) -> None:
        self._check(
            self._dll.rp2350_hid_session_mouse_wheel(self._handle(handle), delta)
        )

    def wait_ms(self, handle: int, milliseconds: int) -> None:
        self._check(
            self._dll.rp2350_hid_session_wait_ms(
                self._handle(handle),
                milliseconds,
            )
        )

    def stop_all(self, handle: int) -> None:
        self._check(self._dll.rp2350_hid_session_stop_all(self._handle(handle)))

    def run_script(self, handle: int, script: str) -> None:
        self._string_command("rp2350_hid_session_run_script", handle, script)
