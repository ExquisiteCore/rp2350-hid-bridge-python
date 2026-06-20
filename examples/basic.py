import argparse

from rp2350_hid_bridge import HidBridge, HidBridgeOptions


def main():
    parser = argparse.ArgumentParser(description="Run a basic RP2350 HID bridge protocol check.")
    parser.add_argument("--port", default=None, help="serial port such as COM3; auto-detect when omitted")
    args = parser.parse_args()

    with HidBridge(HidBridgeOptions(port=args.port)) as hid:
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
