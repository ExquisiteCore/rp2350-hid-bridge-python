import ctypes
import dataclasses
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rp2350_hid_bridge import (
    HidBridge,
    HidBridgeOptions,
    HidSession,
    find_port,
    list_ports,
)
from rp2350_hid_bridge import native as native_module
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


class FakeNativeApi:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.abi_info = SimpleNamespace(abi_major=1, abi_minor=0, feature_flags=3)
        self.calls = []
        self.opened = False
        self.discovered_port = None
        self.fail_next_open = False
        self.next_handle = 123

    def find_port(self, vid, pid):
        self.calls.append(("find_port", vid, pid))
        return self.discovered_port

    def create(self, options):
        handle = self.next_handle
        self.next_handle += 1
        self.calls.append(("create", options))
        return handle

    def open(self, handle):
        self.calls.append(("open", handle))
        if self.fail_next_open:
            self.fail_next_open = False
            raise RuntimeError("open failed")
        self.opened = True

    def is_open(self, handle):
        self.calls.append(("is_open", handle))
        return self.opened

    def release(self, handle):
        self.calls.append(("release", handle))
        self.opened = False

    def _command(self, name, handle, *args):
        self.calls.append((name, handle, *args))

    def ping(self, handle):
        self._command("ping", handle)

    def info(self, handle):
        self._command("info", handle)
        return b"info"

    def caps(self, handle):
        self._command("caps", handle)
        return b"caps"

    def type_text(self, handle, text):
        self._command("type_text", handle, text)

    def key_tap(self, handle, combo):
        self._command("key_tap", handle, combo)

    def key_down(self, handle, combo):
        self._command("key_down", handle, combo)

    def key_up(self, handle, combo):
        self._command("key_up", handle, combo)

    def mouse_move(self, handle, dx, dy):
        self._command("mouse_move", handle, dx, dy)

    def mouse_click(self, handle, button):
        self._command("mouse_click", handle, button)

    def mouse_down(self, handle, button):
        self._command("mouse_down", handle, button)

    def mouse_up(self, handle, button):
        self._command("mouse_up", handle, button)

    def mouse_wheel(self, handle, delta):
        self._command("mouse_wheel", handle, delta)

    def wait_ms(self, handle, milliseconds):
        self._command("wait_ms", handle, milliseconds)

    def stop_all(self, handle):
        self._command("stop_all", handle)

    def run_script(self, handle, script):
        self._command("run_script", handle, script)


class HidSessionTests(unittest.TestCase):
    def test_context_shares_one_handle_and_releases_once(self):
        api = FakeNativeApi(Path("rp2350_hid_bridge.dll"))
        hid = HidSession("COM4", _api=api)

        with hid:
            hid.open()
            hid.key_down("W")
            hid.mouse_move(12, -4)
            self.assertTrue(hasattr(type(hid), "native_handle"))
            self.assertTrue(hasattr(type(hid), "dll_path"))
            self.assertEqual(hid.native_handle, 123)
            self.assertEqual(hid.dll_path, api.path)
            hid.stop_all()

        hid.close()
        self.assertEqual(
            [call[0] for call in api.calls],
            [
                "create",
                "open",
                "key_down",
                "mouse_move",
                "is_open",
                "stop_all",
                "release",
            ],
        )
        created = api.calls[0][1]
        self.assertEqual(created.port, "COM4")
        self.assertEqual(created.timeout_ms, 1000)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            _ = hid.native_handle
        self.assertEqual(hid.dll_path, api.path)

    def test_all_commands_forward_to_one_handle(self):
        api = FakeNativeApi(Path("rp2350_hid_bridge.dll"))
        hid = HidSession("COM4", _api=api)
        hid.open()
        api.calls.clear()

        hid.ping()
        self.assertEqual(hid.info(), b"info")
        self.assertEqual(hid.caps(), b"caps")
        hid.type_text("abc")
        hid.key_tap("ENTER")
        hid.key_down("W")
        hid.key_up("W")
        hid.mouse_move(1, -2)
        hid.mouse_click()
        hid.mouse_down("right")
        hid.mouse_up("right")
        hid.mouse_wheel(-1)
        hid.wait_ms(25)
        hid.run_script("stop\n")
        hid.stop_all()

        self.assertEqual(
            api.calls,
            [
                ("ping", 123),
                ("info", 123),
                ("caps", 123),
                ("type_text", 123, "abc"),
                ("key_tap", 123, "ENTER"),
                ("key_down", 123, "W"),
                ("key_up", 123, "W"),
                ("mouse_move", 123, 1, -2),
                ("mouse_click", 123, "left"),
                ("mouse_down", 123, "right"),
                ("mouse_up", 123, "right"),
                ("mouse_wheel", 123, -1),
                ("wait_ms", 123, 25),
                ("run_script", 123, "stop\n"),
                ("stop_all", 123),
            ],
        )
        hid.close()

    def test_open_failure_releases_candidate_and_next_open_is_fresh(self):
        api = FakeNativeApi(Path("rp2350_hid_bridge.dll"))
        api.fail_next_open = True
        hid = HidSession("COM4", _api=api)

        with self.assertRaisesRegex(RuntimeError, "open failed"):
            hid.open()
        self.assertEqual(
            [call[0] for call in api.calls],
            ["create", "open", "release"],
        )
        with self.assertRaisesRegex(RuntimeError, "closed"):
            hid.key_down("W")

        hid.open()
        self.assertEqual(api.calls[-2][0], "create")
        self.assertEqual(api.calls[-1], ("open", 124))
        hid.close()

    def test_missing_port_uses_native_discovery(self):
        api = FakeNativeApi(Path("rp2350_hid_bridge.dll"))
        api.discovered_port = "COM9"
        hid = HidSession(_api=api)
        hid.open()

        self.assertEqual(api.calls[0], ("find_port", 0xCAFE, 0x2350))
        self.assertEqual(api.calls[1][1].port, "COM9")
        hid.close()

    def test_compatibility_constructor_and_discovery_helpers(self):
        api = FakeNativeApi(Path("rp2350_hid_bridge.dll"))
        options = HidBridgeOptions(
            port="COM7",
            baudrate=230400,
            timeout=0.25,
            retries=4,
            vid=1,
            pid=2,
        )
        bridge = HidBridge(options, _api=api)
        bridge.open()
        created = api.calls[0][1]
        self.assertEqual(created.port, "COM7")
        self.assertEqual(created.baudrate, 230400)
        self.assertEqual(created.timeout_ms, 250)
        self.assertEqual(created.retries, 4)
        bridge.close()

        api.discovered_port = "COM8"
        self.assertEqual(find_port(1, 2, _api=api), "COM8")
        self.assertEqual(list_ports(1, 2, _api=api), ["COM8"])

    def test_argument_ranges_are_checked_before_native_call(self):
        api = FakeNativeApi(Path("rp2350_hid_bridge.dll"))
        hid = HidSession("COM4", _api=api)
        hid.open()
        api.calls.clear()

        for dx, dy in ((-32769, 0), (32768, 0), (0, -32769), (0, 32768)):
            with self.subTest(dx=dx, dy=dy):
                with self.assertRaisesRegex(ValueError, "signed 16-bit"):
                    hid.mouse_move(dx, dy)
        for delta in (-129, 128):
            with self.subTest(delta=delta):
                with self.assertRaisesRegex(ValueError, "mouse wheel delta"):
                    hid.mouse_wheel(delta)
        for duration in (-1, 0x1_0000_0000):
            with self.subTest(duration=duration):
                with self.assertRaisesRegex(ValueError, "unsigned 32-bit"):
                    hid.wait_ms(duration)
        self.assertEqual(api.calls, [])
        hid.close()


class NativeLoaderTests(unittest.TestCase):
    def test_native_loader_reports_all_missing_exports(self):
        fake = SimpleNamespace(rp2350_hid_get_abi_info=lambda _value: 0)
        with self.assertRaisesRegex(RuntimeError, "rp2350_hid_session_create"):
            native_module._require_exports(fake, Path("old-hid.dll"))

    def test_hid_abi_requires_1_0_and_required_features(self):
        valid = native_module.HidAbiInfo(
            abi_major=1,
            abi_minor=0,
            options_size=ctypes.sizeof(native_module._CHidOptions),
            feature_flags=native_module.REQUIRED_FEATURES,
        )
        native_module._validate_abi(valid, Path("rp2350_hid_bridge.dll"))
        with self.assertRaisesRegex(RuntimeError, "ABI major"):
            native_module._validate_abi(
                dataclasses.replace(valid, abi_major=2),
                Path("bad.dll"),
            )
        with self.assertRaisesRegex(RuntimeError, "options size"):
            native_module._validate_abi(
                dataclasses.replace(valid, options_size=1),
                Path("bad-options.dll"),
            )
        with self.assertRaisesRegex(RuntimeError, "required features"):
            native_module._validate_abi(
                dataclasses.replace(
                    valid,
                    feature_flags=native_module.FEATURE_SHARED_SESSION,
                ),
                Path("missing-discovery.dll"),
            )

    def test_formal_app_dir_never_falls_back_to_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment_dll = root / "environment.dll"
            environment_dll.write_bytes(b"environment")
            missing_app = root / "app"
            with patch.dict(
                os.environ,
                {"RP2350_HID_BRIDGE_DLL": str(environment_dll)},
            ):
                with self.assertRaisesRegex(
                    FileNotFoundError,
                    "app.*rp2350_hid_bridge.dll",
                ):
                    native_module.find_hid_dll(app_dir=missing_app)

    def test_explicit_dll_path_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            dll = Path(directory) / "custom-hid.dll"
            dll.write_bytes(b"fake")
            self.assertEqual(
                native_module.find_hid_dll(
                    app_dir=Path(directory) / "ignored",
                    dll_path=dll,
                ),
                dll.resolve(),
            )


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
        self.assertEqual(parse_combo("SHIFT"), (0x02, 0x00))
        self.assertEqual(parse_combo("CTRL+SHIFT"), (0x03, 0x00))
        with self.assertRaises(ValueError):
            parse_combo("CTRL+A+B")

    def test_script_parser(self):
        commands = parse_script(
            'type "abc"\n'
            "key tap ENTER\n"
            "key down CTRL+SHIFT\n"
            "mouse move 10 -5\n"
            "wait 100\n"
            "stop\n"
        )
        self.assertEqual(len(commands), 6)
        self.assertEqual(commands[0].text, "abc")
        self.assertEqual(commands[1].action, "tap")
        self.assertEqual(commands[2].combo, (0x03, 0x00))
        self.assertEqual((commands[3].dx, commands[3].dy), (10, -5))
        self.assertEqual(commands[4].ms, 100)
        self.assertEqual(commands[5].kind, "stop")

    def test_script_parser_rejects_out_of_range_mouse_wheel_delta(self):
        for delta in (-129, 128):
            with self.subTest(delta=delta):
                with self.assertRaisesRegex(ValueError, "mouse wheel delta"):
                    parse_script(f"mouse wheel {delta}")


if __name__ == "__main__":
    unittest.main()
