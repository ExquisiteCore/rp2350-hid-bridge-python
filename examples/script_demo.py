import argparse
from pathlib import Path

from rp2350_hid_bridge import HidSession, parse_script


SCRIPT = '''
type "hello from ExquisiteCore"
key tap ENTER
mouse move 20 0
wait 100
stop
'''


def main():
    parser = argparse.ArgumentParser(description="Parse or run an RP2350 HID bridge script.")
    parser.add_argument("--run", action="store_true", help="send the script to the device")
    parser.add_argument("--port", default="COM4", help="serial port, for example COM4")
    parser.add_argument(
        "--app-dir",
        type=Path,
        default=Path.cwd(),
        help="directory containing rp2350_hid_bridge.dll",
    )
    args = parser.parse_args()

    if not args.run:
        for command in parse_script(SCRIPT):
            print(command)
        print("\nUse --run --port COMx to send real HID input.")
        return

    with HidSession(args.port, app_dir=args.app_dir) as hid:
        hid.run_script(SCRIPT)


if __name__ == "__main__":
    main()
