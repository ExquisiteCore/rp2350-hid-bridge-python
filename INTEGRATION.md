# RP2350 HID 桥接器 Python SDK 接入指南

本文面向打包为 Windows EXE 的 Python 调用端。SDK 版本为 `0.2.0`，原生 DLL ABI
为 `1.0`。

## 1. 运行目录

推荐应用布局：

```text
MyClient.exe
rp2350_hid_bridge.dll
resources/
```

wheel 只包含 Python 代码，不内嵌 DLL。构建/安装流程分别交付：

- `rp2350-hid-bridge==0.2.0` wheel；
- ABI 1.0 的 `rp2350_hid_bridge.dll`；
- 调用端自己的 EXE 和资源。

SDK 加载顺序固定为：显式 `dll_path`、显式 `app_dir`、开发环境变量
`RP2350_HID_BRIDGE_DLL`、源码仓构建目录。生产调用必须传 `app_dir`；一旦传入，
缺失 DLL 会直接报错，不搜索 PATH。

## 2. 创建唯一会话

```python
from pathlib import Path
from rp2350_hid_bridge import HidSession

app_dir = Path(__file__).resolve().parent

with HidSession(
    "COM4",
    app_dir=app_dir,
    baudrate=115200,
    timeout=1.0,
    retries=2,
) as hid:
    hid.ping()
    print("info", hid.info().hex(" "))
    print("caps", hid.caps().hex(" "))
```

`with` 进入时创建并打开原生句柄，退出时 release 一次。重复 `open()` 和 `close()`
是幂等的。打开失败会立即 release 候选句柄；下一次 `open()` 创建全新的会话。

如果端口为 `None`，DLL 使用 SetupAPI 按 VID/PID 自动发现。辅助函数返回普通字符串：

```python
from rp2350_hid_bridge import find_port, list_ports

port: str | None = find_port(app_dir=app_dir)
ports: list[str] = list_ports(app_dir=app_dir)
```

## 3. 键盘、鼠标和脚本

```python
with HidSession("COM4", app_dir=app_dir) as hid:
    hid.type_text("hello")
    hid.key_tap("ENTER")
    hid.key_down("SHIFT")
    hid.key_up("SHIFT")

    hid.mouse_move(20, -5)
    hid.mouse_click("left")
    hid.mouse_down("right")
    hid.mouse_up("right")
    hid.mouse_wheel(-1)
    hid.wait_ms(100)

    hid.run_script('key tap ENTER\nmouse move 2 0\n')
    hid.stop_all()
```

移动量必须位于 signed 16-bit，滚轮位于 `-128..127`，等待时间位于 unsigned
32-bit 毫秒。超范围在进入 DLL 前抛出 `ValueError`。

## 4. 与视觉运行时共享

同一个 COM 口不得创建两个客户端。调用端创建并拥有一个 `HidSession`；视觉 Python
SDK 从该对象取得内部绑定，将同一个不透明句柄交给 `vision_runtime.dll` retain。

```text
Python HidSession ── owns ──┐
                            ├─ one native session ── COM4
vision_runtime.dll ─ retain ┘
```

视觉运行时 disarm、reset、标定结束或 close 只释放视觉自己的引用，不发送全局
`STOP_ALL`，因此调用端保持的 `W`、Shift 等键不会被误释放。全局释放只能由会话
所有者显式调用 `hid.stop_all()`，或在最后引用释放/端口断开/固件租约超时时发生。

不要自行调用 `_binding_for_runtime()`；它是两个第一方 SDK 之间的内部握手接口。

## 5. 同步调用与线程

- Python 方法是同步的：返回时该命令已收到 ACK 或抛出异常。
- `ctypes.CDLL` 在原生调用期间释放 GIL。
- 捕获和推理不持有 HID 命令锁。
- 键盘和鼠标仅在各自请求/ACK 往返期间串行。
- `run_script()` 是长事务，执行期间可能延迟视觉瞄准命令。
- `close()` 与命令使用同一个 Python 生命周期锁；先停止工作线程，再释放最后所有者。

## 6. 错误与恢复

| 场景 | Python 异常 | 处理 |
|---|---|---|
| DLL 缺失 | `FileNotFoundError` | 修复 EXE 同级部署，不搜索其他目录 |
| ABI/导出/特性不兼容 | `RuntimeError` | 使用配套 DLL 和 wheel |
| 自动发现无结果 | `RuntimeError` | 检查设备或显式填写 COM |
| 响应超时 | `TimeoutError` | 当前会话已故障，关闭后明确新建 |
| 串口断开/读写失败 | `RuntimeError` | 当前会话已故障，不自动重连 |
| NACK/业务参数错误 | `RuntimeError`/`ValueError` | 修正命令；健康会话仍可使用 |

故障会话拒绝后续命令。不要对可能产生副作用的操作做无限重放；记录错误、退出旧上下文、
检查 USB/固件/端口占用，再由业务层决定是否创建新会话。

## 7. 安全停止

业务上需要立即释放所有保持输入时：

```python
try:
    with HidSession("COM4", app_dir=app_dir) as hid:
        hid.key_down("W")
        # 业务与视觉处理
        hid.stop_all()
except (TimeoutError, RuntimeError) as exc:
    log_error(exc)
```

`close()` 不额外发送 Python 侧 `STOP_ALL`。最后一个原生引用释放时 DLL 会执行一次
best-effort 全局停止、停止心跳、取消 DTR 并关闭端口。若进程崩溃或 USB 瞬断，固件
两秒控制租约是最终兜底。

## 8. 发布检查

- [ ] EXE 同级存在经过哈希校验的 `rp2350_hid_bridge.dll`。
- [ ] Python wheel 为 `0.2.0`，DLL ABI 为 `1.0`，特性位为 `3`。
- [ ] 一个 COM 口只有一个调用端拥有的 `HidSession`。
- [ ] 视觉运行时 attach 同一会话，不另开串口。
- [ ] 控制线程在销毁会话前停止。
- [ ] 显式全局停止路径只调用 `hid.stop_all()`。
- [ ] 已验证最后引用释放、USB 拔出和固件两秒租约。
- [ ] 日志不记录敏感输入文本或裸句柄。
