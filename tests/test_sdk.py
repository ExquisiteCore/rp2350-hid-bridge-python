import inspect
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from rp2350_hid_bridge import client as client_module
from rp2350_hid_bridge.client import HidBridge, HidBridgeOptions, _try_decode_response
from rp2350_hid_bridge.keys import parse_combo
from rp2350_hid_bridge.protocol import (
    FLAG_NO_RESPONSE,
    PROTOCOL_VERSION,
    CommandType,
    DecodeError,
    decode_frame,
    encode_frame,
)
from rp2350_hid_bridge.script import parse_script

HEARTBEAT = CommandType.HEARTBEAT


class FakeClock:
    def __init__(self):
        self.now = 10.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ControlledSleeper:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.entered.set()
        self.release.wait(seconds)


class ObservableRLock:
    def __init__(self):
        self._lock = threading.RLock()
        self.notify_waiter = False
        self.waiter_entered = threading.Event()

    def acquire(self) -> None:
        if self.notify_waiter:
            self.waiter_entered.set()
        self._lock.acquire()

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


class FakeSerial:
    def __init__(
        self,
        responses_on_command_write=None,
        *,
        clock: FakeClock | None = None,
        auto_ack: bool = False,
        write_delay: float = 0.0,
    ):
        self.responses_on_command_write = list(responses_on_command_write or [])
        self.clock = clock
        self.auto_ack = auto_ack
        self.write_delay = write_delay
        self.timeout = 1.0
        self.write_timeout = 1.0
        self.writes: list[bytes] = []
        self.read_timeouts: list[float] = []
        self.read_calls = 0
        self.flush_calls = 0
        self.reset_input_buffer_calls = 0
        self.dtr_history: list[bool] = []
        self.closed = False
        self.max_active_writes = 0
        self.heartbeat_write_entered = threading.Event()
        self._active_writes = 0
        self._dtr = False
        self._read_buffer = bytearray()
        self._lock = threading.Lock()

    @property
    def dtr(self) -> bool:
        return self._dtr

    @dtr.setter
    def dtr(self, value: bool) -> None:
        self._dtr = bool(value)
        self.dtr_history.append(self._dtr)

    def write(self, data: bytes) -> int:
        frame_bytes = bytes(data)
        frame = decode_frame(frame_bytes)
        with self._lock:
            self._active_writes += 1
            self.max_active_writes = max(self.max_active_writes, self._active_writes)
            self.writes.append(frame_bytes)
        try:
            if frame.command_type == HEARTBEAT:
                self.heartbeat_write_entered.set()
            if self.write_delay:
                time.sleep(self.write_delay)
            if frame.command_type != HEARTBEAT:
                response = None
                if self.responses_on_command_write:
                    response = self.responses_on_command_write.pop(0)
                elif self.auto_ack:
                    response = response_frame(frame.sequence, CommandType.ACK)
                if response:
                    with self._lock:
                        self._read_buffer.extend(response)
        finally:
            with self._lock:
                self._active_writes -= 1
        return len(frame_bytes)

    def flush(self) -> None:
        self.flush_calls += 1

    def read(self, size: int) -> bytes:
        self.read_calls += 1
        self.read_timeouts.append(self.timeout)
        with self._lock:
            if self._read_buffer:
                chunk = bytes(self._read_buffer[:size])
                del self._read_buffer[:size]
                return chunk
        if self.clock is not None:
            self.clock.advance(self.timeout)
        return b""

    def reset_input_buffer(self) -> None:
        self.reset_input_buffer_calls += 1
        with self._lock:
            self._read_buffer.clear()

    def close(self) -> None:
        self.closed = True


class BlockingSerial(FakeSerial):
    def __init__(self):
        super().__init__()
        self.read_started = threading.Event()
        self.release_read = threading.Event()

    def read(self, size: int) -> bytes:
        self.read_started.set()
        self.release_read.wait(2.0)
        if self.closed:
            raise OSError("serial port closed")
        return super().read(size)

    def close(self) -> None:
        super().close()
        self.release_read.set()

    def force_unblock(self) -> None:
        self.closed = True
        self.release_read.set()


class BatchAwareFakeSerial(FakeSerial):
    def __init__(self):
        super().__init__()
        self.collecting = False
        self.response_types: list[CommandType] = []

    def write(self, data: bytes) -> int:
        written = super().write(data)
        frame = decode_frame(data)
        response_type = CommandType.ACK
        payload = b""

        if frame.command_type == CommandType.BATCH_BEGIN:
            if self.collecting:
                response_type = CommandType.NACK
                payload = b"\x0b"
            else:
                self.collecting = True
        elif frame.command_type == CommandType.BATCH_END:
            if self.collecting:
                self.collecting = False
            else:
                response_type = CommandType.NACK
                payload = b"\x0b"
        elif frame.command_type == CommandType.STOP_ALL:
            self.collecting = False

        response = response_frame(frame.sequence, response_type, payload)
        self.response_types.append(response_type)
        with self._lock:
            self._read_buffer.extend(response)
        return written


class WriteFailingSerial(FakeSerial):
    def __init__(self):
        super().__init__()
        self.write_attempts = 0

    def write(self, data: bytes) -> int:
        self.write_attempts += 1
        raise OSError("serial write failed")


class FailingOnWriteSerial(FakeSerial):
    def __init__(self, fail_on_write: int, responses, *, clock: FakeClock):
        super().__init__(responses, clock=clock)
        self.fail_on_write = fail_on_write
        self.write_attempts = 0

    def write(self, data: bytes) -> int:
        self.write_attempts += 1
        if self.write_attempts == self.fail_on_write:
            raise OSError("cleanup write failed")
        return super().write(data)


class CloseRaceSerial(FakeSerial):
    def __init__(self):
        super().__init__(auto_ack=True)
        self.close_phase_started = threading.Event()
        self.allow_close = threading.Event()
        self.in_close_phase = False
        self.write_during_close = False

    @property
    def dtr(self) -> bool:
        return self._dtr

    @dtr.setter
    def dtr(self, value: bool) -> None:
        value = bool(value)
        if not value:
            self.in_close_phase = True
            self.close_phase_started.set()
            self.allow_close.wait(1.0)
        self._dtr = value
        self.dtr_history.append(value)
        if not value:
            self.in_close_phase = False

    def write(self, data: bytes) -> int:
        if self.in_close_phase:
            self.write_during_close = True
        return super().write(data)


class LateReadSerial(FakeSerial):
    def __init__(self):
        super().__init__()
        self.read_started = threading.Event()
        self.release_read = threading.Event()
        self._late_chunk_sent = False

    def read(self, _size: int) -> bytes:
        if not self._late_chunk_sent:
            self.read_started.set()
            self.release_read.wait(1.0)
            self._late_chunk_sent = True
            return b"\xA5"
        if self.closed:
            raise OSError("old serial port closed")
        return b""


def response_frame(sequence: int, command_type: CommandType, payload: bytes = b"") -> bytes:
    return encode_frame(sequence, command_type, payload)


def bridge_with_fake_serial(
    serial_obj: FakeSerial,
    *,
    retries: int = 2,
    timeout: float = 0.1,
    clock: FakeClock | None = None,
) -> HidBridge:
    clock = clock or FakeClock()
    bridge = HidBridge(HidBridgeOptions(port="FAKE", retries=retries, timeout=timeout))
    bridge._monotonic = clock.monotonic
    bridge._sleep = clock.sleep
    bridge._serial = serial_obj
    return bridge


class ProtocolTests(unittest.TestCase):
    def test_frame_round_trip(self):
        frame = encode_frame(0x1234, CommandType.PING, b"")
        decoded = decode_frame(frame)

        self.assertEqual(decoded.version, PROTOCOL_VERSION)
        self.assertEqual(decoded.sequence, 0x1234)
        self.assertEqual(decoded.command_type, CommandType.PING)
        self.assertEqual(decoded.payload, b"")

    def test_v2_heartbeat_frame_carries_no_response_flag(self):
        self.assertEqual(PROTOCOL_VERSION, 2)
        self.assertEqual(CommandType.HEARTBEAT, 0x04)
        self.assertIn("flags", inspect.signature(encode_frame).parameters)
        frame = encode_frame(0, HEARTBEAT, flags=FLAG_NO_RESPONSE)
        decoded = decode_frame(frame)

        self.assertEqual(decoded.version, 2)
        self.assertEqual(decoded.flags, FLAG_NO_RESPONSE)
        self.assertEqual(decoded.sequence, 0)
        self.assertEqual(decoded.command_type, HEARTBEAT)

    def test_flags_are_covered_by_crc(self):
        self.assertIn("flags", inspect.signature(encode_frame).parameters)
        frame = bytearray(encode_frame(0, HEARTBEAT, flags=FLAG_NO_RESPONSE))
        frame[3] = 0

        with self.assertRaises(DecodeError):
            decode_frame(frame)

    def test_legacy_version_frame_still_decodes(self):
        frame = encode_frame(7, CommandType.PING, version=1)

        self.assertEqual(decode_frame(frame).version, 1)

    def test_bad_crc_is_rejected(self):
        frame = bytearray(encode_frame(7, CommandType.PING, b""))
        frame[-1] ^= 0x55

        with self.assertRaises(DecodeError):
            decode_frame(bytes(frame))

    def test_key_combo_parser(self):
        self.assertEqual(parse_combo("CTRL+C"), (0x01, 0x06))
        self.assertEqual(parse_combo("SHIFT+R"), (0x02, 0x15))
        self.assertEqual(parse_combo("ENTER"), (0x00, 0x28))
        self.assertEqual(parse_combo("F5"), (0x00, 0x3E))

    def test_modifier_only_combos_use_zero_keycode(self):
        self.assertEqual(parse_combo("SHIFT"), (0x02, 0x00))
        self.assertEqual(parse_combo("CTRL+SHIFT"), (0x03, 0x00))

    def test_more_than_one_non_modifier_key_remains_invalid(self):
        with self.assertRaises(ValueError):
            parse_combo("CTRL+A+B")

    def test_script_parser_accepts_modifier_only_combo(self):
        commands = parse_script("key down CTRL+SHIFT\nkey up CTRL+SHIFT")

        self.assertEqual(commands[0].combo, (0x03, 0x00))
        self.assertEqual(commands[1].combo, (0x03, 0x00))

    def test_script_parser(self):
        commands = parse_script(
            '''
type "abc"
key tap ENTER
mouse move 10 -5
wait 100
stop
'''
        )

        self.assertEqual(len(commands), 5)
        self.assertEqual(commands[0].kind, "type")
        self.assertEqual(commands[0].text, "abc")
        self.assertEqual(commands[1].kind, "key")
        self.assertEqual(commands[1].action, "tap")
        self.assertEqual(commands[2].kind, "mouse")
        self.assertEqual(commands[2].action, "move")
        self.assertEqual(commands[2].dx, 10)
        self.assertEqual(commands[2].dy, -5)
        self.assertEqual(commands[3].kind, "wait")
        self.assertEqual(commands[3].ms, 100)
        self.assertEqual(commands[4].kind, "stop")


class RetryTests(unittest.TestCase):
    def test_nack_is_terminal_and_is_written_once(self):
        clock = FakeClock()
        serial_obj = FakeSerial(
            [response_frame(1, CommandType.NACK, b"\x0f")], clock=clock
        )
        bridge = bridge_with_fake_serial(serial_obj, retries=3, clock=clock)

        with self.assertRaisesRegex(RuntimeError, "15"):
            bridge.key_down("W")

        self.assertEqual(len(serial_obj.writes), 1)

    def test_timeout_retry_reuses_exact_preencoded_frame_and_sequence(self):
        clock = FakeClock()
        serial_obj = FakeSerial(
            [None, response_frame(1, CommandType.ACK)], clock=clock
        )
        bridge = bridge_with_fake_serial(serial_obj, retries=1, clock=clock)

        bridge.key_down("W")

        self.assertEqual(len(serial_obj.writes), 2)
        self.assertEqual(serial_obj.writes[0], serial_obj.writes[1])
        self.assertEqual(decode_frame(serial_obj.writes[0]).sequence, 1)
        self.assertEqual(serial_obj.reset_input_buffer_calls, 0)

    def test_busy_uses_reason_delay_and_retries_exact_frame(self):
        clock = FakeClock()
        serial_obj = FakeSerial(
            [
                response_frame(1, CommandType.BUSY, b"\x03\x00\x19"),
                response_frame(1, CommandType.ACK),
            ],
            clock=clock,
        )
        bridge = bridge_with_fake_serial(serial_obj, retries=1, clock=clock)

        bridge.key_down("W")

        self.assertEqual(clock.sleeps, [0.025])
        self.assertEqual(serial_obj.writes[0], serial_obj.writes[1])
        self.assertEqual(serial_obj.reset_input_buffer_calls, 0)

    def test_close_interrupts_long_busy_delay_without_retry_write(self):
        sleeper = ControlledSleeper()
        serial_obj = FakeSerial(
            [response_frame(1, CommandType.BUSY, b"\x01\xff\xff")]
        )
        bridge = HidBridge(
            HidBridgeOptions(port="FAKE", retries=1, timeout=0.1),
            sleep=sleeper,
        )
        bridge._serial = serial_obj
        command_done = threading.Event()
        command_errors: list[Exception] = []

        def send_key() -> None:
            try:
                bridge.key_down("W")
            except Exception as exc:
                command_errors.append(exc)
            finally:
                command_done.set()

        command_thread = threading.Thread(target=send_key, daemon=True)
        command_thread.start()
        self.assertTrue(sleeper.entered.wait(1.0))

        close_started = time.monotonic()
        bridge.close()
        close_elapsed = time.monotonic() - close_started
        try:
            exited_promptly = command_done.wait(0.3)
        finally:
            sleeper.release.set()
            command_thread.join(1.0)

        commands = [decode_frame(frame).command_type for frame in serial_obj.writes]
        self.assertLess(close_elapsed, 0.2)
        self.assertTrue(exited_promptly, "BUSY retry delay ignored close")
        self.assertEqual(commands, [CommandType.KEY_DOWN, CommandType.STOP_ALL])
        self.assertTrue(command_errors)

    def test_close_reopen_still_interrupts_old_busy_waiter(self):
        sleeper = ControlledSleeper()
        old_serial = FakeSerial(
            [response_frame(1, CommandType.BUSY, b"\x01\xff\xff")]
        )
        new_serial = FakeSerial(auto_ack=True)
        serial_module = SimpleNamespace(Serial=lambda **_kwargs: new_serial)
        bridge = HidBridge(
            HidBridgeOptions(port="FAKE", retries=1, timeout=0.1),
            sleep=sleeper,
        )
        bridge._serial = old_serial
        command_done = threading.Event()
        command_errors: list[Exception] = []

        def send_key() -> None:
            try:
                bridge.key_down("W")
            except Exception as exc:
                command_errors.append(exc)
            finally:
                command_done.set()

        command_thread = threading.Thread(target=send_key, daemon=True)
        command_thread.start()
        self.assertTrue(sleeper.entered.wait(1.0))

        with (
            patch("rp2350_hid_bridge.client._serial_module", return_value=serial_module),
            patch("rp2350_hid_bridge.client.HEARTBEAT_INTERVAL_SECONDS", 10.0),
        ):
            bridge.close()
            bridge.open()
            exited_promptly = command_done.wait(0.3)
            new_commands = [
                decode_frame(frame).command_type for frame in new_serial.writes
            ]
            bridge.close()

        sleeper.release.set()
        command_thread.join(1.0)
        self.assertTrue(exited_promptly, "reopen erased BUSY cancellation")
        self.assertNotIn(CommandType.KEY_DOWN, new_commands)
        self.assertTrue(command_errors)

    def test_stale_response_is_ignored_without_clearing_input(self):
        clock = FakeClock()
        responses = response_frame(99, CommandType.ACK) + response_frame(1, CommandType.ACK)
        serial_obj = FakeSerial([responses], clock=clock)
        bridge = bridge_with_fake_serial(serial_obj, retries=0, clock=clock)

        bridge.ping()

        self.assertEqual(len(serial_obj.writes), 1)
        self.assertEqual(serial_obj.reset_input_buffer_calls, 0)

    def test_bad_length_candidate_resyncs_to_following_valid_response(self):
        corrupt = bytearray(response_frame(99, CommandType.ACK))
        corrupt[7:9] = (1).to_bytes(2, "big")
        valid = response_frame(1, CommandType.ACK)
        buffer = bytearray(corrupt + valid)

        response = _try_decode_response(buffer, 1)

        self.assertEqual(response.sequence if response else None, 1)
        self.assertEqual(response.command_type if response else None, CommandType.ACK)
        self.assertEqual(buffer, b"")


class DeadlineTests(unittest.TestCase):
    def _bridge(self):
        clock = FakeClock()
        serial_obj = FakeSerial(clock=clock, auto_ack=True)
        bridge = bridge_with_fake_serial(serial_obj, retries=0, timeout=0.1, clock=clock)
        return bridge, serial_obj

    def test_ordinary_command_has_one_second_minimum_deadline(self):
        bridge, serial_obj = self._bridge()

        bridge.ping()

        self.assertAlmostEqual(serial_obj.read_timeouts[-1], 1.0)

    def test_wait_deadline_includes_requested_duration_and_margin(self):
        bridge, serial_obj = self._bridge()

        bridge.wait_ms(2500)

        self.assertAlmostEqual(serial_obj.read_timeouts[-1], 3.0)

    def test_text_deadline_uses_character_count_and_tap_delay(self):
        bridge, serial_obj = self._bridge()

        bridge.type_text("abcdefghij")

        self.assertAlmostEqual(serial_obj.read_timeouts[-1], 0.58)

    def test_mouse_deadline_uses_split_report_step_estimate(self):
        bridge, serial_obj = self._bridge()

        bridge.mouse_move(300, -300)

        self.assertAlmostEqual(serial_obj.read_timeouts[-1], 0.503)

    def test_batch_end_deadline_uses_accumulated_known_duration(self):
        bridge, serial_obj = self._bridge()

        bridge.run_script('type "abcdefghij"\nmouse move 300 -300\nwait 2500')

        commands = [decode_frame(frame).command_type for frame in serial_obj.writes]
        batch_end_index = commands.index(CommandType.BATCH_END)
        self.assertEqual(
            commands,
            [
                CommandType.BATCH_BEGIN,
                CommandType.TYPE_ASCII,
                CommandType.MOUSE_MOVE_REL,
                CommandType.WAIT_MS,
                CommandType.BATCH_END,
            ],
        )
        self.assertAlmostEqual(serial_obj.read_timeouts[batch_end_index], 3.583)


class ScriptExecutionTests(unittest.TestCase):
    def _run(self, script: str) -> BatchAwareFakeSerial:
        serial_obj = BatchAwareFakeSerial()
        bridge = bridge_with_fake_serial(serial_obj, retries=0, timeout=0.1)
        try:
            bridge.run_script(script)
        except RuntimeError as exc:
            self.fail(f"script produced a protocol error: {exc}")
        return serial_obj

    def test_stop_at_end_executes_batch_before_stop(self):
        serial_obj = self._run('type "abc"\nstop')

        commands = [decode_frame(frame).command_type for frame in serial_obj.writes]
        self.assertEqual(
            commands,
            [
                CommandType.BATCH_BEGIN,
                CommandType.TYPE_ASCII,
                CommandType.BATCH_END,
                CommandType.STOP_ALL,
            ],
        )
        self.assertEqual(serial_obj.response_types, [CommandType.ACK] * 4)

    def test_stop_in_middle_delimits_two_batches(self):
        serial_obj = self._run('type "abc"\nstop\nwait 25')

        commands = [decode_frame(frame).command_type for frame in serial_obj.writes]
        self.assertEqual(
            commands,
            [
                CommandType.BATCH_BEGIN,
                CommandType.TYPE_ASCII,
                CommandType.BATCH_END,
                CommandType.STOP_ALL,
                CommandType.BATCH_BEGIN,
                CommandType.WAIT_MS,
                CommandType.BATCH_END,
            ],
        )
        self.assertEqual(serial_obj.response_types, [CommandType.ACK] * 7)

    def test_stop_only_does_not_create_empty_batch(self):
        serial_obj = self._run("stop")

        commands = [decode_frame(frame).command_type for frame in serial_obj.writes]
        self.assertEqual(commands, [CommandType.STOP_ALL])
        self.assertEqual(serial_obj.response_types, [CommandType.ACK])

    def test_failed_collected_command_aborts_without_batch_end(self):
        clock = FakeClock()
        serial_obj = FakeSerial(
            [
                response_frame(1, CommandType.ACK),
                response_frame(2, CommandType.NACK, b"\x0f"),
                response_frame(3, CommandType.ACK),
            ],
            clock=clock,
        )
        bridge = bridge_with_fake_serial(serial_obj, retries=0, clock=clock)

        with self.assertRaisesRegex(RuntimeError, "keyboard busy"):
            bridge.run_script('type "abc"\nwait 25')

        commands = [decode_frame(frame).command_type for frame in serial_obj.writes]
        self.assertEqual(
            commands,
            [
                CommandType.BATCH_BEGIN,
                CommandType.TYPE_ASCII,
                CommandType.STOP_ALL,
            ],
        )
        self.assertNotIn(CommandType.BATCH_END, commands)

    def test_cleanup_failure_does_not_mask_original_command_error(self):
        clock = FakeClock()
        serial_obj = FailingOnWriteSerial(
            3,
            [
                response_frame(1, CommandType.ACK),
                response_frame(2, CommandType.NACK, b"\x0f"),
            ],
            clock=clock,
        )
        bridge = bridge_with_fake_serial(serial_obj, retries=0, clock=clock)

        caught: Exception | None = None
        try:
            bridge.run_script('type "abc"')
        except Exception as exc:
            caught = exc

        self.assertIsInstance(caught, RuntimeError)
        self.assertIn("keyboard busy", str(caught))
        self.assertEqual(serial_obj.write_attempts, 3)

    def test_ordinary_command_cannot_interleave_with_script_batch(self):
        serial_obj = BatchAwareFakeSerial()
        bridge = bridge_with_fake_serial(serial_obj, retries=0, timeout=0.1)
        script_at_first_command = threading.Event()
        release_script = threading.Event()
        thread_errors: list[Exception] = []
        original_execute = bridge._execute_script_command

        def delayed_execute(command) -> None:
            script_at_first_command.set()
            release_script.wait(1.0)
            original_execute(command)

        bridge._execute_script_command = delayed_execute

        def run_script() -> None:
            try:
                bridge.run_script('type "abc"')
            except Exception as exc:
                thread_errors.append(exc)

        def click_mouse() -> None:
            try:
                bridge.mouse_click()
            except Exception as exc:
                thread_errors.append(exc)

        script_thread = threading.Thread(target=run_script)
        command_thread = threading.Thread(target=click_mouse)
        script_thread.start()
        self.assertTrue(script_at_first_command.wait(1.0))
        command_thread.start()
        time.sleep(0.05)
        release_script.set()
        script_thread.join(1.0)
        command_thread.join(1.0)

        commands = [decode_frame(frame).command_type for frame in serial_obj.writes]
        self.assertEqual(
            commands,
            [
                CommandType.BATCH_BEGIN,
                CommandType.TYPE_ASCII,
                CommandType.BATCH_END,
                CommandType.MOUSE_CLICK,
            ],
        )
        self.assertEqual(thread_errors, [])


class LifecycleTests(unittest.TestCase):
    def test_open_heartbeat_and_close_lifecycle(self):
        self.assertEqual(client_module.HEARTBEAT_INTERVAL_SECONDS, 0.5)
        serial_obj = FakeSerial(auto_ack=True)
        serial_module = SimpleNamespace(Serial=lambda **_kwargs: serial_obj)
        bridge = HidBridge(HidBridgeOptions(port="FAKE", timeout=0.05, retries=0))

        with (
            patch("rp2350_hid_bridge.client._serial_module", return_value=serial_module),
            patch("rp2350_hid_bridge.client.HEARTBEAT_INTERVAL_SECONDS", 0.01, create=True),
        ):
            bridge.open()
            self.assertTrue(serial_obj.heartbeat_write_entered.wait(1.0))
            heartbeat_thread = bridge._heartbeat_thread
            reads_before_close = serial_obj.read_calls

            heartbeat_frames = [
                decode_frame(frame)
                for frame in serial_obj.writes
                if decode_frame(frame).command_type == HEARTBEAT
            ]
            self.assertTrue(heartbeat_frames)
            self.assertTrue(all(frame.sequence == 0 for frame in heartbeat_frames))
            self.assertTrue(all(frame.flags == FLAG_NO_RESPONSE for frame in heartbeat_frames))
            self.assertEqual(reads_before_close, 0)

            bridge.close()

        commands = [decode_frame(frame).command_type for frame in serial_obj.writes]
        self.assertIn(CommandType.STOP_ALL, commands)
        self.assertEqual(serial_obj.dtr_history, [True, False])
        self.assertTrue(serial_obj.closed)
        self.assertIsNotNone(heartbeat_thread)
        self.assertFalse(heartbeat_thread.is_alive())
        writes_after_close = len(serial_obj.writes)
        time.sleep(0.03)
        self.assertEqual(len(serial_obj.writes), writes_after_close)

    def test_command_and_heartbeat_writes_are_serialized(self):
        serial_obj = FakeSerial(auto_ack=True, write_delay=0.03)
        serial_module = SimpleNamespace(Serial=lambda **_kwargs: serial_obj)
        bridge = HidBridge(HidBridgeOptions(port="FAKE", timeout=0.1, retries=0))

        with (
            patch("rp2350_hid_bridge.client._serial_module", return_value=serial_module),
            patch("rp2350_hid_bridge.client.HEARTBEAT_INTERVAL_SECONDS", 0.001, create=True),
        ):
            bridge.open()
            self.assertTrue(serial_obj.heartbeat_write_entered.wait(1.0))
            bridge.ping()
            bridge.close()

        self.assertEqual(serial_obj.max_active_writes, 1)

    def test_close_remains_best_effort_when_stop_all_fails(self):
        serial_obj = WriteFailingSerial()
        bridge = bridge_with_fake_serial(serial_obj, retries=0)
        bridge._heartbeat_thread = None

        bridge.close()

        self.assertEqual(serial_obj.write_attempts, 1)
        self.assertEqual(serial_obj.writes, [])
        self.assertEqual(serial_obj.dtr_history, [False])
        self.assertTrue(serial_obj.closed)

    def test_close_sends_stop_without_waiting_for_active_command(self):
        serial_obj = BlockingSerial()
        bridge = bridge_with_fake_serial(serial_obj, retries=0, timeout=0.1)
        command_errors: list[Exception] = []
        close_done = threading.Event()

        def run_wait() -> None:
            try:
                bridge.wait_ms(60_000)
            except Exception as exc:
                command_errors.append(exc)

        command_thread = threading.Thread(target=run_wait)
        close_thread = threading.Thread(
            target=lambda: (bridge.close(), close_done.set())
        )
        command_thread.start()
        self.assertTrue(serial_obj.read_started.wait(1.0))
        close_thread.start()

        try:
            self.assertTrue(
                close_done.wait(0.2),
                "close queued behind the active long-running command",
            )
        finally:
            serial_obj.force_unblock()
            command_thread.join(1.0)
            close_thread.join(1.0)

        commands = [decode_frame(frame).command_type for frame in serial_obj.writes]
        self.assertIn(CommandType.STOP_ALL, commands)
        self.assertTrue(command_errors)

    def test_close_rejects_writer_that_passed_open_check_before_stop(self):
        serial_obj = CloseRaceSerial()
        bridge = bridge_with_fake_serial(serial_obj, retries=0)
        writer_ready = threading.Event()
        release_writer = threading.Event()
        writer_errors: list[Exception] = []
        original_write_frame = bridge._write_frame

        def delayed_write(
            serial_transport,
            frame: bytes,
            generation: int,
        ) -> None:
            if decode_frame(frame).command_type != CommandType.STOP_ALL:
                writer_ready.set()
                release_writer.wait(1.0)
            original_write_frame(serial_transport, frame, generation)

        bridge._write_frame = delayed_write

        def send_ping() -> None:
            try:
                bridge.ping()
            except Exception as exc:
                writer_errors.append(exc)

        writer_thread = threading.Thread(target=send_ping)
        close_thread = threading.Thread(target=bridge.close)
        writer_thread.start()
        self.assertTrue(writer_ready.wait(1.0))
        close_thread.start()
        self.assertTrue(serial_obj.close_phase_started.wait(1.0))

        try:
            release_writer.set()
            time.sleep(0.05)
        finally:
            serial_obj.allow_close.set()
            writer_thread.join(1.0)
            close_thread.join(1.0)

        commands = [decode_frame(frame).command_type for frame in serial_obj.writes]
        self.assertEqual(commands, [CommandType.STOP_ALL])
        self.assertFalse(serial_obj.write_during_close)
        self.assertTrue(serial_obj.closed)
        self.assertTrue(writer_errors)

    def test_close_reopen_rejects_command_queued_in_old_session(self):
        old_serial = FakeSerial()
        new_serial = FakeSerial(auto_ack=True)
        serial_module = SimpleNamespace(Serial=lambda **_kwargs: new_serial)
        bridge = bridge_with_fake_serial(old_serial, retries=0)
        observed_lock = ObservableRLock()
        bridge._command_lock = observed_lock
        command_errors: list[Exception] = []

        def send_ping() -> None:
            try:
                bridge.ping()
            except Exception as exc:
                command_errors.append(exc)

        observed_lock.acquire()
        observed_lock.notify_waiter = True
        command_thread = threading.Thread(target=send_ping)
        command_thread.start()
        self.assertTrue(observed_lock.waiter_entered.wait(1.0))

        with (
            patch("rp2350_hid_bridge.client._serial_module", return_value=serial_module),
            patch("rp2350_hid_bridge.client.HEARTBEAT_INTERVAL_SECONDS", 10.0),
        ):
            bridge.close()
            bridge.open()
            observed_lock.release()
            command_thread.join(1.0)
            new_commands = [
                decode_frame(frame).command_type for frame in new_serial.writes
            ]
            bridge.close()

        self.assertEqual(new_commands, [])
        self.assertTrue(command_errors)

    def test_close_reopen_isolates_late_bytes_from_old_reader(self):
        old_serial = LateReadSerial()
        new_serial = FakeSerial(auto_ack=True)
        serial_module = SimpleNamespace(Serial=lambda **_kwargs: new_serial)
        bridge = bridge_with_fake_serial(old_serial, retries=0)
        command_errors: list[Exception] = []

        def send_ping() -> None:
            try:
                bridge.ping()
            except Exception as exc:
                command_errors.append(exc)

        command_thread = threading.Thread(target=send_ping)
        command_thread.start()
        self.assertTrue(old_serial.read_started.wait(1.0))

        with (
            patch("rp2350_hid_bridge.client._serial_module", return_value=serial_module),
            patch("rp2350_hid_bridge.client.HEARTBEAT_INTERVAL_SECONDS", 10.0),
        ):
            bridge.close()
            bridge.open()
            old_serial.release_read.set()
            command_thread.join(1.0)
            reopened_buffer = bytes(bridge._receive_buffer)
            bridge.close()

        self.assertEqual(reopened_buffer, b"")
        self.assertTrue(command_errors)


if __name__ == "__main__":
    unittest.main()
