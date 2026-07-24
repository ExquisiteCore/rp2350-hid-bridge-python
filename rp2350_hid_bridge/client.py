from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .keys import parse_combo
from .protocol import (
    FLAG_NO_RESPONSE,
    MAGIC,
    MAX_PAYLOAD_SIZE,
    CommandType,
    DecodeError,
    Response,
    ascii_payload,
    byte_payload,
    decode_frame,
    encode_frame,
    expected_response_type,
    i16_pair_payload,
    u32_payload,
)
from .script import ScriptCommand, mouse_button_mask, parse_script

DEFAULT_VID = 0xCAFE
DEFAULT_PID = 0x2350
HEARTBEAT_INTERVAL_SECONDS = 0.5
TRANSPORT_MARGIN_SECONDS = 0.5
KEY_TAP_DELAY_SECONDS = 0.008
MOUSE_CLICK_DELAY_SECONDS = 0.020
MOUSE_REPORT_ESTIMATE_SECONDS = 0.001

_NACK_ERROR_NAMES = {
    1: "bad frame",
    2: "bad command",
    3: "unsupported ASCII",
    4: "HID write failure",
    5: "transport failure",
    6: "frame too long",
    7: "unsupported version",
    8: "unsupported flags",
    9: "invalid sequence",
    10: "sequence conflict",
    11: "invalid batch state",
    12: "batch capacity exceeded",
    13: "too many keys",
    14: "wait too long",
    15: "keyboard busy",
    16: "cancelled",
}

_BUSY_REASON_NAMES = {
    1: "active duplicate",
    2: "batch executing",
    3: "executor occupied",
}


@dataclass
class HidBridgeOptions:
    port: str | None = None
    baudrate: int = 115200
    timeout: float = 1.0
    retries: int = 2
    vid: int = DEFAULT_VID
    pid: int = DEFAULT_PID


class HidBridge:
    def __init__(
        self,
        options: HidBridgeOptions | None = None,
        *,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self.options = options or HidBridgeOptions()
        self._serial = None
        self._sequence = 1
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._write_lock = threading.Lock()
        self._command_lock = threading.Lock()
        self._sequence_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._closing = False
        self._receive_buffer = bytearray()
        self._batch_duration_seconds: float | None = None

    def __enter__(self) -> "HidBridge":
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def open(self) -> None:
        with self._lifecycle_lock:
            if self._serial is not None:
                return

            serial_mod = _serial_module()
            port = self.options.port or find_port(self.options.vid, self.options.pid)
            if not port:
                raise RuntimeError("RP2350 HID bridge serial port not found")
            serial_obj = serial_mod.Serial(
                port=port,
                baudrate=self.options.baudrate,
                timeout=self.options.timeout,
                write_timeout=self.options.timeout,
            )
            try:
                _set_dtr(serial_obj, True)
            except Exception:
                serial_obj.close()
                raise

            self._serial = serial_obj
            self._closing = False
            self._receive_buffer.clear()
            self._heartbeat_stop.clear()
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                name="rp2350-hid-heartbeat",
                daemon=True,
            )
            self._heartbeat_thread.start()

    def close(self) -> None:
        with self._lifecycle_lock:
            serial_obj = self._serial
            if serial_obj is None:
                return
            if self._closing:
                return
            self._closing = True

            try:
                try:
                    self._write_stop_all_best_effort(serial_obj)
                except Exception:
                    pass
            finally:
                self._stop_heartbeat()
                try:
                    _set_dtr(serial_obj, False)
                except Exception:
                    pass
                try:
                    serial_obj.close()
                finally:
                    self._serial = None
                    self._receive_buffer.clear()
                    self._batch_duration_seconds = None
                    self._closing = False

    def send_command(self, command_type: CommandType, payload: bytes = b"") -> Response:
        return self._send_command(command_type, payload)

    def _send_command(
        self,
        command_type: CommandType,
        payload: bytes = b"",
        *,
        allow_closing: bool = False,
    ) -> Response:
        with self._command_lock:
            serial_obj = self._require_open(allow_closing=allow_closing)
            sequence = self._next_sequence()
            frame = encode_frame(sequence, command_type, payload)
            response_timeout = self._response_timeout(command_type, payload)

            for attempt in range(self.options.retries + 1):
                self._write_frame(serial_obj, frame)
                try:
                    response = self._read_response(serial_obj, sequence, response_timeout)
                except TimeoutError:
                    if attempt >= self.options.retries:
                        raise
                    continue

                if response.command_type == CommandType.BUSY:
                    reason, retry_seconds = _decode_busy(response.payload)
                    if attempt >= self.options.retries:
                        reason_name = _BUSY_REASON_NAMES.get(reason, "unknown")
                        raise RuntimeError(
                            f"device remained BUSY ({reason_name}, reason {reason})"
                        )
                    self._sleep(retry_seconds)
                    continue

                if response.command_type == CommandType.NACK:
                    raise _nack_error(response.payload)

                expected = expected_response_type(command_type)
                if response.command_type != expected:
                    raise RuntimeError(
                        f"unexpected response {response.command_type}, expected {expected}"
                    )

                self._record_completed_command(command_type, payload)
                return response

        raise RuntimeError("command failed")

    def ping(self) -> None:
        self.send_command(CommandType.PING)

    def info(self) -> bytes:
        return self.send_command(CommandType.GET_INFO).payload

    def caps(self) -> bytes:
        return self.send_command(CommandType.GET_CAPS).payload

    def type_text(self, text: str) -> None:
        self.send_command(CommandType.TYPE_ASCII, ascii_payload(text))

    def key_tap(self, combo: str) -> None:
        self._send_key(CommandType.KEY_TAP, combo)

    def key_down(self, combo: str) -> None:
        self._send_key(CommandType.KEY_DOWN, combo)

    def key_up(self, combo: str) -> None:
        self._send_key(CommandType.KEY_UP, combo)

    def mouse_move(self, dx: int, dy: int) -> None:
        self.send_command(CommandType.MOUSE_MOVE_REL, i16_pair_payload(dx, dy))

    def mouse_click(self, button: str = "left") -> None:
        self.send_command(CommandType.MOUSE_CLICK, byte_payload(mouse_button_mask(button)))

    def mouse_down(self, button: str = "left") -> None:
        self.send_command(CommandType.MOUSE_BUTTON_DOWN, byte_payload(mouse_button_mask(button)))

    def mouse_up(self, button: str = "left") -> None:
        self.send_command(CommandType.MOUSE_BUTTON_UP, byte_payload(mouse_button_mask(button)))

    def mouse_wheel(self, delta: int) -> None:
        self.send_command(CommandType.MOUSE_WHEEL, byte_payload(delta))

    def wait_ms(self, ms: int) -> None:
        self.send_command(CommandType.WAIT_MS, u32_payload(ms))

    def stop_all(self) -> None:
        self.send_command(CommandType.STOP_ALL)

    def run_script(self, text: str) -> None:
        commands = parse_script(text)
        try:
            segment: list[ScriptCommand] = []
            for command in commands:
                if command.kind == "stop":
                    self._execute_script_batch(segment)
                    segment.clear()
                    self.stop_all()
                else:
                    segment.append(command)
            self._execute_script_batch(segment)
        except Exception:
            try:
                self.stop_all()
            finally:
                raise

    def _execute_script_batch(self, commands: list[ScriptCommand]) -> None:
        if not commands:
            return
        self.send_command(CommandType.BATCH_BEGIN)
        for command in commands:
            self._execute_script_command(command)
        self.send_command(CommandType.BATCH_END)

    def _send_key(self, command_type: CommandType, combo: str) -> None:
        modifier, keycode = parse_combo(combo)
        self.send_command(command_type, bytes([modifier, keycode]))

    def _execute_script_command(self, command: ScriptCommand) -> None:
        if command.kind == "type" and command.text is not None:
            self.type_text(command.text)
        elif command.kind == "key" and command.action and command.combo:
            combo = bytes(command.combo)
            command_type = {
                "tap": CommandType.KEY_TAP,
                "down": CommandType.KEY_DOWN,
                "up": CommandType.KEY_UP,
            }[command.action]
            self.send_command(command_type, combo)
        elif command.kind == "mouse" and command.action == "move":
            self.mouse_move(command.dx or 0, command.dy or 0)
        elif command.kind == "mouse" and command.action == "click":
            self.send_command(CommandType.MOUSE_CLICK, byte_payload(command.button or 0))
        elif command.kind == "mouse" and command.action == "down":
            self.send_command(CommandType.MOUSE_BUTTON_DOWN, byte_payload(command.button or 0))
        elif command.kind == "mouse" and command.action == "up":
            self.send_command(CommandType.MOUSE_BUTTON_UP, byte_payload(command.button or 0))
        elif command.kind == "mouse" and command.action == "wheel":
            self.mouse_wheel(command.delta or 0)
        elif command.kind == "wait" and command.ms is not None:
            self.wait_ms(command.ms)
        elif command.kind == "stop":
            self.stop_all()
        else:
            raise ValueError(f"unsupported script command {command}")

    def _write_frame(self, serial_obj, frame: bytes) -> None:
        with self._write_lock:
            serial_obj.write(frame)
            serial_obj.flush()

    def _write_stop_all_best_effort(self, serial_obj) -> None:
        sequence = self._next_sequence()
        frame = encode_frame(sequence, CommandType.STOP_ALL)
        self._write_frame(serial_obj, frame)

    def _read_response(
        self,
        serial_obj,
        expected_sequence: int,
        timeout_seconds: float,
    ) -> Response:
        deadline = self._monotonic() + timeout_seconds
        while True:
            response = _try_decode_response(self._receive_buffer, expected_sequence)
            if response is not None:
                return response

            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for response")
            serial_obj.timeout = remaining
            chunk = serial_obj.read(64)
            if chunk:
                self._receive_buffer.extend(chunk)

    def _response_timeout(self, command_type: CommandType, payload: bytes) -> float:
        configured = max(0.0, float(self.options.timeout))
        if command_type == CommandType.BATCH_END and self._batch_duration_seconds is not None:
            return max(configured, self._batch_duration_seconds + 1.0)

        duration = _known_command_duration(command_type, payload)
        if duration is None:
            return max(configured, 1.0)
        return max(configured, duration + TRANSPORT_MARGIN_SECONDS)

    def _record_completed_command(self, command_type: CommandType, payload: bytes) -> None:
        if command_type == CommandType.BATCH_BEGIN:
            self._batch_duration_seconds = 0.0
            return
        if command_type in (CommandType.BATCH_END, CommandType.STOP_ALL):
            self._batch_duration_seconds = None
            return
        if self._batch_duration_seconds is not None:
            duration = _known_command_duration(command_type, payload)
            if duration is not None:
                self._batch_duration_seconds += duration

    def _heartbeat_loop(self) -> None:
        frame = encode_frame(
            0,
            CommandType.HEARTBEAT,
            flags=FLAG_NO_RESPONSE,
        )
        while not self._heartbeat_stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            serial_obj = self._serial
            if serial_obj is None:
                return
            try:
                self._write_frame(serial_obj, frame)
            except Exception:
                if self._heartbeat_stop.is_set():
                    return

    def _stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        self._heartbeat_thread = None

    def _next_sequence(self) -> int:
        with self._sequence_lock:
            sequence = self._sequence
            self._sequence = (self._sequence + 1) & 0xFFFF
            if self._sequence == 0:
                self._sequence = 1
            return sequence

    def _require_open(self, *, allow_closing: bool = False):
        if self._serial is None or (self._closing and not allow_closing):
            raise RuntimeError("serial port is not open")
        return self._serial


def list_ports():
    ports_mod = _list_ports_module()
    return list(ports_mod.comports())


def find_port(vid: int = DEFAULT_VID, pid: int = DEFAULT_PID) -> str | None:
    for port in list_ports():
        if getattr(port, "vid", None) == vid and getattr(port, "pid", None) == pid:
            return port.device
    return None


def _try_decode_response(buffer: bytearray, expected_sequence: int) -> Response | None:
    while len(buffer) >= 2:
        if bytes(buffer[:2]) != MAGIC:
            del buffer[0]
            continue
        if len(buffer) < 9:
            return None
        payload_len = int.from_bytes(buffer[7:9], "big")
        if payload_len > MAX_PAYLOAD_SIZE:
            del buffer[0]
            continue
        frame_len = 11 + payload_len
        if len(buffer) < frame_len:
            return None
        frame_bytes = bytes(buffer[:frame_len])
        del buffer[:frame_len]
        try:
            frame = decode_frame(frame_bytes)
        except DecodeError:
            continue
        if frame.sequence != expected_sequence:
            continue
        return Response(frame.command_type, frame.payload, frame.sequence)
    return None


def _known_command_duration(command_type: CommandType, payload: bytes) -> float | None:
    if command_type == CommandType.WAIT_MS and len(payload) == 4:
        return int.from_bytes(payload, "big") / 1000.0
    if command_type == CommandType.TYPE_ASCII:
        return len(payload) * KEY_TAP_DELAY_SECONDS
    if command_type == CommandType.MOUSE_MOVE_REL and len(payload) == 4:
        dx = int.from_bytes(payload[:2], "big", signed=True)
        dy = int.from_bytes(payload[2:], "big", signed=True)
        steps = math.ceil(max(abs(dx), abs(dy)) / 127) if dx or dy else 0
        return steps * MOUSE_REPORT_ESTIMATE_SECONDS
    if command_type == CommandType.KEY_TAP:
        return KEY_TAP_DELAY_SECONDS
    if command_type == CommandType.MOUSE_CLICK:
        return MOUSE_CLICK_DELAY_SECONDS
    return None


def _decode_busy(payload: bytes) -> tuple[int, float]:
    if len(payload) != 3:
        raise RuntimeError(f"malformed BUSY payload ({len(payload)} bytes)")
    reason = payload[0]
    retry_ms = int.from_bytes(payload[1:3], "big")
    return reason, retry_ms / 1000.0


def _nack_error(payload: bytes) -> RuntimeError:
    code = payload[0] if payload else 0
    name = _NACK_ERROR_NAMES.get(code, "unknown error")
    return RuntimeError(f"device returned NACK {name} (error code {code})")


def _set_dtr(serial_obj, asserted: bool) -> None:
    if hasattr(type(serial_obj), "dtr") or hasattr(serial_obj, "dtr"):
        serial_obj.dtr = asserted
        return
    setter = getattr(serial_obj, "setDTR", None)
    if setter is None:
        raise RuntimeError("serial transport does not expose DTR control")
    setter(asserted)


def _serial_module():
    try:
        import serial
    except ImportError as exc:
        raise RuntimeError("install pyserial to use the serial client: pip install pyserial") from exc
    return serial


def _list_ports_module():
    try:
        from serial.tools import list_ports
    except ImportError as exc:
        raise RuntimeError("install pyserial to enumerate serial ports: pip install pyserial") from exc
    return list_ports
