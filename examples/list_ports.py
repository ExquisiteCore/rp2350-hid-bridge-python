import argparse
from pathlib import Path

from rp2350_hid_bridge import find_port, list_ports


def main():
    parser = argparse.ArgumentParser(description="Find the RP2350 HID bridge COM port.")
    parser.add_argument(
        "--app-dir",
        type=Path,
        default=Path.cwd(),
        help="directory containing rp2350_hid_bridge.dll",
    )
    args = parser.parse_args()

    print("matching ports:", list_ports(app_dir=args.app_dir))
    detected = find_port(app_dir=args.app_dir)
    print(f"RP2350 bridge: {detected or 'not found'}")


if __name__ == "__main__":
    main()
