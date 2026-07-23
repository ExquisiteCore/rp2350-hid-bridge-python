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
uv sync
```

From the firmware repository:

```powershell
uv sync --project sdk/python
```

Run tests:

```powershell
uv run --project sdk/python python -m unittest discover -s sdk/python/tests -v
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

Modifiers may also be sent without an ordinary key. For example,
`key_down("SHIFT")` and `key_down("CTRL+SHIFT")` encode a zero keycode with
the requested modifier mask. A combo may contain at most one ordinary key;
use multiple `key_down` calls for simultaneous ordinary keys.

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

Protocol v2 retries only timeouts and `BUSY` responses. Every retry reuses the
exact original sequence and encoded frame so firmware replay protection can
distinguish a retry from a new action. A `BUSY` payload contains a one-byte
reason followed by a two-byte big-endian retry delay in milliseconds; the SDK
uses that advertised delay. `NACK`, serial errors, and unexpected response
types are terminal. Malformed frames are discarded while the stream parser
resynchronizes; if no valid matching response arrives, normal timeout retry
policy applies. `NACK` errors include the decoded firmware error name and
number.

Matching responses are read without clearing the serial input buffer. Complete
responses for stale sequences are ignored, so an old response cannot satisfy
the current request. A timeout raises `TimeoutError` after all configured retry
attempts.

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

Read deadlines account for device-side execution: ordinary commands allow at
least one second, waits include the requested duration plus a transport margin,
typing uses the character count and firmware tap delay, large mouse movement
uses its split HID report count, and `BATCH_END` includes the accumulated known
duration of the collected script.

## Protocol v2 Safety Lease

While the port is open, the SDK sends a serialized sequence-zero `HEARTBEAT`
frame with the `NO_RESPONSE` flag every 500 ms. Heartbeats share the command
write lock and never read a response. They maintain the firmware's two-second
control lease; loss of the process, serial connection, or heartbeats causes the
firmware to release held input.

Opening explicitly asserts DTR. Orderly close sends `STOP_ALL` best-effort while
the transport is still usable, stops and joins the heartbeat worker, deasserts
DTR, and then closes the serial port.

## Notes

`TYPE_ASCII` and the script `type` command support US-keyboard ASCII only; they
do not provide Unicode or layout-independent text entry. The examples produce
real keyboard and mouse input only when explicitly run against a device. Run
them only when the active window is expected.
