import argparse
from pathlib import Path

from rp2350_hid_bridge import HidSession


def main():
    parser = argparse.ArgumentParser(description="Run a basic RP2350 HID bridge protocol check.")
    parser.add_argument("--port", default="COM4", help="serial port such as COM4")
    parser.add_argument(
        "--app-dir",
        type=Path,
        default=Path.cwd(),
        help="directory containing rp2350_hid_bridge.dll",
    )
    args = parser.parse_args()

    with HidSession(args.port, app_dir=args.app_dir) as hid:
        hid.ping()
        print("info:", hid.info().hex(" "))
        print("caps:", hid.caps().hex(" "))

        # 下面会产生真实 HID 输入，使用前确认当前焦点安全。
        hid.type_text("hello from python sdk")
        hid.key_tap("ENTER")
        hid.mouse_move(20, 0)
        hid.stop_all()


if __name__ == "__main__":
    main()
