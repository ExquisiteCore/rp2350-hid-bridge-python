# RP2350 HID Bridge Python SDK

Python SDK for the ExquisiteCore RP2350 KeyMouse Bridge.

The SDK talks to the board through the CDC serial command endpoint. The board
then emits standard USB HID keyboard and mouse reports.

## Requirements

```text
Python 3.10+
pyserial 3.5+
Windows COM port for real device control
```

The package can be installed independently or through the firmware repository
submodule.

## Install

From this SDK repository:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\python -m pip install -e .
```

From the firmware repository:

```powershell
cd sdk\python
python -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\python -m pip install -e .
```

Run tests:

```powershell
.\.venv\Scripts\python -m unittest discover -s tests
```

## Find The Device

The firmware uses VID/PID `CAFE:2350`. Passing `port=None` enables automatic
discovery:

```python
from rp2350_hid_bridge import HidBridge, HidBridgeOptions

with HidBridge(HidBridgeOptions(port=None)) as hid:
    hid.ping()
```

List serial ports:

```powershell
.\.venv\Scripts\python examples\list_ports.py
```

Run a basic protocol check:

```powershell
.\.venv\Scripts\python examples\basic.py --port COM3
```

## Direct Control API

```python
from rp2350_hid_bridge import HidBridge, HidBridgeOptions

with HidBridge(HidBridgeOptions(port="COM3")) as hid:
    hid.ping()
    print(hid.info().hex(" "))
    print(hid.caps().hex(" "))

    hid.type_text("hello")
    hid.key_tap("ENTER")
    hid.key_down("CTRL")
    hid.key_up("CTRL")

    hid.mouse_move(10, -5)
    hid.mouse_click("left")
    hid.mouse_down("right")
    hid.mouse_up("right")
    hid.mouse_wheel(-1)

    hid.wait_ms(100)
    hid.stop_all()
```

Common key names include letters, digits, `ENTER`, `ESC`, `TAB`, `SPACE`,
`F1`-`F12`, arrows, `HOME`, `END`, `PAGEUP`, `PAGEDOWN`, `DELETE`, `INSERT`,
and punctuation names such as `SLASH`, `DOT`, `COMMA`, `BACKSLASH`.

Modifiers are combined with `+`:

```text
CTRL+C
SHIFT+F5
ALT+TAB
WIN+R
```

## Script API

```python
script = '''
type "hello from script"
key tap ENTER
mouse move 20 0
mouse click left
wait 100
stop
'''

with HidBridge(HidBridgeOptions(port="COM3")) as hid:
    hid.run_script(script)
```

Supported commands:

```text
type "ASCII text"
key tap|down|up COMBO
mouse move DX DY
mouse click|down|up left|right|middle
mouse wheel DELTA
wait MILLISECONDS
stop
```

Preview the bundled script without sending input:

```powershell
.\.venv\Scripts\python examples\script_demo.py
```

Send it intentionally:

```powershell
.\.venv\Scripts\python examples\script_demo.py --run --port COM3
```

## Error Handling

The client retries `BUSY` responses, raises `RuntimeError` for `NACK`, and
raises `TimeoutError` if no matching response frame arrives before the configured
timeout.

```python
from rp2350_hid_bridge import HidBridge, HidBridgeOptions

try:
    with HidBridge(HidBridgeOptions(port=None, timeout=1.0, retries=2)) as hid:
        hid.type_text("safe text")
except TimeoutError:
    print("device did not respond")
except RuntimeError as exc:
    print(f"device/client error: {exc}")
```

## Notes

The examples produce real keyboard and mouse input only when explicitly run
against a device. Run them only when the active window is expected.
