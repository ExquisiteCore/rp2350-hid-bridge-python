# RP2350 HID 桥接器 Python SDK

面向 ExquisiteCore RP2350 KeyMouse Bridge 的 Python SDK。

SDK 通过 CDC 串口命令端点与板卡通信，板卡随后生成标准 USB HID 键盘和鼠标报告。

## 环境要求

```text
Python 3.10+
pyserial 3.5+
Windows COM port for real device control
```

该软件包既可以独立安装，也可以通过固件仓库中的子模块安装。

## 安装

在本 SDK 仓库中执行：

```powershell
uv sync
```

在固件仓库中执行：

```powershell
uv sync --project sdk/python
```

在本 SDK 仓库中运行测试：

```powershell
uv run python -m unittest discover -s tests -v
```

## 查找设备

固件使用 VID/PID `CAFE:2350`。传入 `port=None` 可启用自动发现：

```python
from rp2350_hid_bridge import HidBridge, HidBridgeOptions

with HidBridge(HidBridgeOptions(port=None)) as hid:
    hid.ping()
```

列出串口：

```powershell
.\.venv\Scripts\python examples\list_ports.py
```

运行基础协议检查：

```powershell
.\.venv\Scripts\python examples\basic.py --port COM3
```

## 直接控制 API

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

常用按键名包括字母、数字、`ENTER`、`ESC`、`TAB`、`SPACE`、`F1`-`F12`、
方向键、`HOME`、`END`、`PAGEUP`、`PAGEDOWN`、`DELETE`、`INSERT`，以及
`SLASH`、`DOT`、`COMMA`、`BACKSLASH` 等标点符号名称。

组合键使用 `+` 连接修饰键：

```text
CTRL+C
SHIFT+F5
ALT+TAB
WIN+R
```

也可以只发送修饰键而不包含普通按键。例如，`key_down("SHIFT")` 和
`key_down("CTRL+SHIFT")` 会使用零键码和指定的修饰键掩码进行编码。一个组合键最多
只能包含一个普通按键；如需同时按住多个普通按键，请多次调用 `key_down`。

## 脚本 API

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

支持的命令：

```text
type "ASCII text"
key tap|down|up COMBO
mouse move DX DY
mouse click|down|up left|right|middle
mouse wheel DELTA
wait MILLISECONDS
stop
```

`stop` 会先完成前面非空的批处理，再发送 `STOP_ALL`。`stop` 后面的命令会进入新的
批处理。只包含 `stop` 的脚本会发送 `STOP_ALL`，但不会创建空批处理。

预览随附脚本但不发送输入：

```powershell
.\.venv\Scripts\python examples\script_demo.py
```

确认后实际发送：

```powershell
.\.venv\Scripts\python examples\script_demo.py --run --port COM3
```

## 错误处理

协议 v2 只会重试响应超时和 `BUSY`。每次重试都复用完全相同的原始序列号和编码帧，
使固件的重放保护能够区分重试和新操作。`BUSY` 载荷包含一个原因字节，后接采用大端序、
以毫秒为单位的双字节重试延迟；SDK 会使用设备给出的延迟。`NACK`、串口错误和意外
响应类型都会立即终止当前操作。流解析器重新同步时会丢弃格式错误的帧；如果没有收到
有效的匹配响应，则采用正常的超时重试策略。`NACK` 错误会包含解码后的固件错误名称
和编号。

读取匹配响应时不会清空串口输入缓冲区。属于过期序列号的完整响应会被忽略，因此旧响应
无法满足当前请求。用完所有配置的重试次数后仍超时，会抛出 `TimeoutError`。

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

读取截止时间会计入设备端执行时间：普通命令至少允许一秒；等待命令包含请求的等待时长
和传输余量；文本输入根据字符数和固件点击延迟计算；大幅鼠标移动根据拆分后的 HID
报告数量计算；`BATCH_END` 还包含已收集脚本累计的已知执行时长。

## 协议 v2 安全租约

端口打开期间，SDK 每 500 毫秒串行发送一个序列号为零且带 `NO_RESPONSE` 标志的
`HEARTBEAT` 帧。心跳与命令共用写入锁，并且不会读取响应。心跳用于维持固件的两秒
控制租约；进程、串口连接或心跳中断后，固件会释放所有保持中的输入。

打开连接时会明确置位 DTR。正常关闭时，会在传输层仍可用的情况下尽力发送
`STOP_ALL`，停止并等待心跳工作线程结束，取消 DTR，最后关闭串口。

## 并发与会话生命周期

命令和脚本共用一个命令锁，因此同一时间只会有一个请求/响应交换，普通命令也无法插入
脚本批处理。`run_script()` 会记录当前串口会话代次，并在每个批处理片段和命令中
检查。如果其他线程关闭或重新打开桥接器，旧脚本会抛出 `RuntimeError`，而不会通过
新连接继续发送剩余命令。

`close()` 首先将当前会话代次标记为失效并停止后续心跳写入。随后它会在持有串行写入锁
时尽力发送一次 `STOP_ALL`，取消 DTR，关闭串口对象，并等待心跳工作线程结束。活动的
读取和 `BUSY` 重试等待也会检查会话代次，因此会在关闭开始时退出。如果旧的心跳工作
线程仍在停止，`open()` 会拒绝启动新会话。

## 注意事项

`TYPE_ASCII` 和脚本中的 `type` 命令只支持美式键盘 ASCII，不能输入 Unicode 文本，
也不能提供与键盘布局无关的文本输入。只有明确连接设备运行示例时，示例才会产生真实
键盘和鼠标输入。请仅在确认活动窗口符合预期时运行。
