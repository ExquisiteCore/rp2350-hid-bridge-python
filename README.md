# RP2350 HID 桥接器 Python SDK

Python 3.11+ 调用层，包装 `rp2350_hid_bridge.dll` 的稳定 C ABI。Python 不再打开
pyserial、不维护心跳/序列号/ACK；这些状态全部由原生 DLL 中唯一的 `HidSession`
管理。

## 部署

Python wheel 不包含 DLL。调用端应把原生 DLL 放在 EXE 同级目录：

```text
MyClient.exe
rp2350_hid_bridge.dll
resources/
```

安装 wheel 后，将该目录显式传给 `app_dir`。正式 `app_dir` 模式只加载该目录下的
`rp2350_hid_bridge.dll`，不会回退到 PATH 或其他机器目录。

```powershell
uv pip install rp2350_hid_bridge-0.2.0-py3-none-any.whl
```

SDK 无第三方运行依赖，也不依赖 pyserial。

## 基本使用

```python
from pathlib import Path
from rp2350_hid_bridge import HidSession

app_dir = Path(__file__).resolve().parent

with HidSession("COM4", app_dir=app_dir) as hid:
    hid.ping()
    print(hid.info().hex(" "))
    print(hid.caps().hex(" "))

    hid.key_down("W")
    hid.mouse_move(10, -5)
    hid.key_up("W")
    hid.stop_all()
```

省略端口时，DLL 通过 SetupAPI 按默认 VID/PID `CAFE:2350` 查找第一个匹配 COM 口：

```python
with HidSession(app_dir=app_dir) as hid:
    hid.ping()
```

`HidBridge(HidBridgeOptions(...))` 仍可兼容旧调用代码；新代码使用 `HidSession`。
`list_ports()` 现在返回 `list[str]`，不再返回 pyserial 的 `ListPortInfo`。

## 一个 COM、一个会话

一个 COM 口只能由一个原生会话拥有。键盘控制和视觉鼠标输出必须 retain/attach 同一个
句柄，不能各自再打开 COM。主运行时 SDK 会从打开的 `HidSession` 取得内部绑定并让
视觉 DLL retain；调用端不需要操作裸指针。

`close()` 只释放当前 Python 对象拥有的一个引用，不隐式调用第二次 `STOP_ALL`。
仍有视觉运行时引用时，心跳和调用端保持的按键继续存在。需要立即全局释放所有输入时
显式调用 `hid.stop_all()`；最后一个引用释放时 DLL 才执行最终停止、DTR 复位和关端口。

## 并发与故障

`ctypes.CDLL` 调用期间释放 GIL。DLL 内部只在每次请求/ACK 往返期间持有命令锁，
因此 Python 的控制线程可以在其他线程执行视觉推理时提交键盘命令。一个长
`run_script()` 事务会占用同一个命令锁，并可能延迟视觉鼠标命令。

超时或串口传输失败会把当前会话标记为故障，不自动重连。关闭旧对象、检查设备后，
由调用端明确创建新会话。参数错误和设备 NACK 不会把健康会话标记为故障。

## 安全边界

板卡生成真实 USB HID 输入，调用前必须确认活动窗口。正常业务路径显式调用
`hid.stop_all()`；进程崩溃或 USB 断开时，固件两秒控制租约负责最终释放保持输入。

运行无需硬件的测试：

```powershell
uv run python -m unittest discover -s tests -v
```

示例：

```powershell
uv run python examples\list_ports.py --app-dir .
uv run python examples\basic.py --app-dir . --port COM4
uv run python examples\script_demo.py --app-dir .
uv run python examples\script_demo.py --app-dir . --run --port COM4
```
