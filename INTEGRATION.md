# RP2350 HID 桥接器 Python SDK 详细接入指南

本文说明如何在 Windows Python 应用中接入 ExquisiteCore RP2350 KeyMouse Bridge。目标是让应用开发者只使用 SDK 的公开接口完成设备发现、连接、键鼠控制、脚本执行、异常恢复和安全退出，不需要理解串口帧、CRC 或固件内部实现。

## 1. 适用场景与安全提示

本 SDK 适用于由 Windows 应用通过 USB CDC 串口控制 RP2350 板卡，再由板卡向操作系统发送标准 USB HID 键盘和鼠标报告的场景。

数据流如下：

```text
应用程序 → Python SDK → CDC 串口 → RP2350 固件 → USB HID 键盘/鼠标 → 操作系统
```

板卡产生的输入会发送给当前获得焦点的窗口。SDK 不知道哪个程序处于前台，也不能替应用判断一次点击或按键是否安全。因此：

- 首次测试只运行第 5 节的连通测试；它只执行 `ping()`、`info()` 和 `caps()`，不会产生键鼠输入。
- 完整示例默认不连接设备，必须显式传入 `--run` 才会启用真实输入。
- 测试真实输入前，先打开一个允许接收测试字符和点击的空白窗口。
- 所有真实输入路径都应在退出前尽力调用 `stop_all()`，并使用上下文管理器关闭连接。
- 不要把 SDK 用于绕过授权、访问控制或第三方软件规则。

## 2. 工作原理与前置条件

### 硬件与固件

- Raspberry Pi Pico 2 或其他采用 RP2350、且与本固件引脚和 USB 配置兼容的板卡。
- 板卡已刷入与当前 SDK 匹配的 RP2350 KeyMouse Bridge 协议 v2 固件。
- 一根支持数据传输的 USB 线；只支持充电的线无法枚举设备。
- Windows 应同时看到固件提供的 CDC 串口和标准 HID 键盘/鼠标接口。

本文不介绍固件编译、刷写和 USB 描述符开发。相关操作请查阅固件仓库的 `README.md` 与 `docs/BUILD.md`。

### 主机环境

```text
Windows 10/11
Python 3.10+
uv
pyserial 3.5+
```

SDK 的协议和解析器本身不依赖 Windows，但本文的真实设备流程以 Windows COM 口为准。

## 3. 安装 SDK

### 独立克隆 SDK

在 Python SDK 根目录执行：

```powershell
uv sync
uv run python -m unittest discover -s tests -v
```

`uv sync` 会创建或更新项目虚拟环境并安装 `pyserial`。测试不需要连接板卡。

### 从固件仓库使用

固件仓把 Python SDK 放在 `sdk/python` 子模块中。从固件仓根目录执行：

```powershell
git submodule update --init --recursive
uv sync --project sdk/python
uv run --project sdk/python python -m unittest discover -s sdk/python/tests -v
```

### 加入自己的 uv 项目

如果 SDK 作为应用仓库中的本地依赖，例如放在 `third_party/rp2350-hid-bridge-python`：

```powershell
uv add --editable .\third_party\rp2350-hid-bridge-python
```

也可以直接使用 Git 依赖：

```powershell
uv add "rp2350-hid-bridge @ git+https://github.com/ExquisiteCore/rp2350-hid-bridge-python.git"
```

应用代码使用公开入口导入：

```python
from rp2350_hid_bridge import HidBridge, HidBridgeOptions
```

### `HidBridgeOptions`

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---:|---|
| `port` | `str \| None` | `None` | `None` 时按 VID/PID 自动发现；也可显式填写 `COM3` |
| `baudrate` | `int` | `115200` | CDC 串口波特率；通常保持默认值 |
| `timeout` | `float` | `1.0` | 基础串口读写超时，单位为秒 |
| `retries` | `int` | `2` | 响应超时或固件 `BUSY` 后的重试次数；应为非负数 |
| `vid` | `int` | `0xCAFE` | 自动发现使用的 USB Vendor ID |
| `pid` | `int` | `0x2350` | 自动发现使用的 USB Product ID |

普通命令的实际响应截止时间至少为一秒。长等待、长文本、大幅鼠标移动和脚本批处理会根据预计执行时间自动延长截止时间，所以不要为了这些正常操作随意设置很大的基础超时。

## 4. 查找并确认串口

### 自动发现

`port=None` 时，`open()` 会调用 `find_port(0xCAFE, 0x2350)`，使用默认 VID/PID 查找桥接器：

```python
from rp2350_hid_bridge import HidBridge, HidBridgeOptions

with HidBridge(HidBridgeOptions(port=None)) as hid:
    hid.ping()
```

如果存在多个相同 VID/PID 的设备，自动发现会使用枚举结果中的第一个。生产应用连接多块板卡时应让用户明确选择端口，而不是依赖枚举顺序。

### 列出并检查端口

```python
from rp2350_hid_bridge import find_port, list_ports

for port in list_ports():
    vid = f"{port.vid:04X}" if port.vid is not None else "----"
    pid = f"{port.pid:04X}" if port.pid is not None else "----"
    print(port.device, port.description, f"VID:PID={vid}:{pid}")

print("自动发现：", find_port())
```

也可以运行仓库示例：

```powershell
uv run python examples\list_ports.py
```

### 显式指定端口

显式指定端口会跳过 VID/PID 自动发现：

```python
options = HidBridgeOptions(port="COM3")
```

如果没有发现设备：

- 使用设备管理器确认 CDC COM 口是否存在。
- 更换确认支持数据传输的 USB 线和 USB 端口。
- 检查板卡是否确实运行桥接器固件，而不是仍处于 BOOTSEL 模式。
- 确认固件使用的 VID/PID 与 `HidBridgeOptions` 一致。
- 关闭可能独占端口的串口监视器和其他客户端。

## 5. 最小连通测试

下面的程序只查询设备，不发送键盘或鼠标报告。将其保存为 `bridge_probe.py`：

```python
import argparse

from rp2350_hid_bridge import HidBridge, HidBridgeOptions


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 RP2350 HID 桥接器连接")
    parser.add_argument("--port", default=None, help="例如 COM3；省略时按 VID/PID 自动查找")
    args = parser.parse_args()

    try:
        options = HidBridgeOptions(port=args.port, timeout=1.0, retries=2)
        with HidBridge(options) as hid:
            hid.ping()
            print("info:", hid.info().hex(" "))
            print("caps:", hid.caps().hex(" "))
        return 0
    except TimeoutError as exc:
        print(f"设备响应超时：{exc}")
    except ValueError as exc:
        print(f"参数错误：{exc}")
    except RuntimeError as exc:
        print(f"桥接器错误：{exc}")
    except OSError as exc:
        print(f"串口错误：{exc}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

自动发现设备：

```powershell
uv run python bridge_probe.py
```

或者显式指定端口：

```powershell
uv run python bridge_probe.py --port COM3
```

成功时会打印 `info:` 和 `caps:` 后的十六进制字节。`ping()`、`info()` 和 `caps()` 只查询桥接器状态，不产生 HID 输入；它们适合作为安装后的首次检查和应用启动时的健康检查。

## 6. 直接控制 API

推荐通过上下文管理器管理连接：

```python
with HidBridge(HidBridgeOptions(port="COM3")) as hid:
    hid.ping()
```

进入 `with` 时调用 `open()`，退出时调用 `close()`。公开的应用层方法如下：

| 方法 | 作用 | 关键约束 |
|---|---|---|
| `open()` | 打开串口、置位 DTR 并启动心跳 | 上下文管理器会自动调用 |
| `ping()` | 检查协议端点是否响应 | 不产生 HID 输入 |
| `info()` | 读取固件信息载荷 | 返回 `bytes` |
| `caps()` | 读取能力载荷 | 返回 `bytes` |
| `type_text(text)` | 输入 ASCII 文本 | 仅支持美式键盘 ASCII，不支持 Unicode |
| `key_tap(combo)` | 按下并释放组合键 | 例如 `CTRL+C`、`ENTER` |
| `key_down(combo)` | 保持组合键 | 退出前必须释放或调用 `stop_all()` |
| `key_up(combo)` | 释放组合键 | 组合键格式与 `key_down()` 相同 |
| `mouse_move(dx, dy)` | 相对移动鼠标 | 两个参数均须位于有符号 16 位范围 |
| `mouse_click(button)` | 点击鼠标按钮 | `left`、`right` 或 `middle` |
| `mouse_down(button)` | 保持鼠标按钮 | 退出前必须释放或调用 `stop_all()` |
| `mouse_up(button)` | 释放鼠标按钮 | 与 `mouse_down()` 配对 |
| `mouse_wheel(delta)` | 滚动滚轮 | 整数范围为 `-128..127` |
| `wait_ms(ms)` | 让设备等待 | 非负 32 位毫秒数 |
| `stop_all()` | 释放所有保持中的键和鼠标按钮 | 成功、失败和退出路径都应尝试调用 |
| `run_script(text)` | 串行执行脚本 | 详见第 7 节 |
| `close()` | 停止会话并关闭端口 | 上下文管理器会自动调用并尽力停止输入 |

按键名不区分大小写，常用名称包括：

```text
A-Z  0-9  ENTER  ESC  TAB  SPACE  BACKSPACE
F1-F12  LEFT  RIGHT  UP  DOWN
HOME  END  PAGEUP  PAGEDOWN  DELETE  INSERT
SLASH  DOT  COMMA  BACKSLASH
```

组合键用 `+` 连接修饰键：

```text
CTRL+C
SHIFT+F5
ALT+TAB
WIN+R
```

一个组合键最多包含一个普通按键，但可以包含多个修饰键。`SHIFT`、`CTRL+SHIFT` 这类只有修饰键的组合也有效。要同时保持多个普通按键，分别调用多次 `key_down()`，结束时分别 `key_up()`，并始终保留 `stop_all()` 兜底。

下面的调用会产生真实输入：

```python
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

应用应在调用这些方法前自行确认运行模式和活动窗口。`type_text()` 的非 ASCII 文本、未知组合键或按钮会产生 `ValueError`；超出整数编码范围的移动量或等待时间还可能产生 `OverflowError`，因此业务层应先验证数值。

## 7. 脚本批处理接入

`run_script()` 适合把一组必须按顺序执行的操作一次性交给 SDK：

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

支持的语法：

| 命令 | 参数 | 示例 |
|---|---|---|
| `type` | 双引号包围的 ASCII 文本 | `type "hello"` |
| `key tap` | 一个组合键 | `key tap CTRL+C` |
| `key down` | 一个组合键 | `key down SHIFT` |
| `key up` | 一个组合键 | `key up SHIFT` |
| `mouse move` | `DX DY` | `mouse move 20 -10` |
| `mouse click` | 按钮名 | `mouse click left` |
| `mouse down` | 按钮名 | `mouse down right` |
| `mouse up` | 按钮名 | `mouse up right` |
| `mouse wheel` | `-128..127` | `mouse wheel -1` |
| `wait` | 非负毫秒数 | `wait 100` |
| `stop` | 无参数 | `stop` |

空行会被忽略。可以先用公开的 `parse_script()` 预览解析结果，不连接板卡：

```python
from rp2350_hid_bridge import parse_script

for command in parse_script(script):
    print(command)
```

脚本事务与普通命令共用命令锁。一个脚本片段执行期间，其他线程的普通命令不能插入批处理。`stop` 会先执行它前面的非空片段，再发送 `STOP_ALL`；后续命令会进入新的片段。只包含 `stop` 的脚本不会发送空批处理。

如果脚本执行失败，SDK 会尽力发送一次 `STOP_ALL`，同时保留原始异常。上层仍应在自己的清理路径中再次尽力调用 `stop_all()`，因为故障可能发生在串口已断开的时刻。

不要在响应是否已到达不明确时自行无限重放整段脚本。SDK 已对协议允许的超时和 `BUSY` 做有限重试；最终失败后应退出旧上下文、检查设备状态，再由明确的业务策略决定是否从头提交一项新任务。

## 8. 完整应用接入模板

以下程序默认不连接设备，也不发送输入。只有显式传入 `--run` 时才会启用真实 HID 控制；`--port` 省略时使用 VID/PID 自动发现：

```python
import argparse

from rp2350_hid_bridge import HidBridge, HidBridgeOptions


def run_input(port: str | None) -> None:
    options = HidBridgeOptions(port=port, timeout=1.0, retries=2)
    with HidBridge(options) as hid:
        try:
            print("警告：真实 HID 输入已启用，请先确认当前活动窗口。")
            hid.mouse_move(20, 0)
            hid.mouse_click("left")
            hid.key_tap("ENTER")
            hid.type_text("hello from rp2350")
            hid.wait_ms(100)
        finally:
            try:
                hid.stop_all()
            except Exception as stop_exc:
                print(f"STOP_ALL 发送失败：{stop_exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="RP2350 HID 桥接器接入模板")
    parser.add_argument("--run", action="store_true", help="明确允许发送真实 HID 输入")
    parser.add_argument("--port", default=None, help="例如 COM3；省略时自动查找")
    args = parser.parse_args()

    if not args.run:
        print("未启用真实输入。确认活动窗口安全后，添加 --run。")
        return 0

    try:
        run_input(args.port)
        return 0
    except TimeoutError as exc:
        print(f"设备响应超时：{exc}")
    except (ValueError, OverflowError) as exc:
        print(f"参数错误：{exc}")
    except RuntimeError as exc:
        print(f"桥接器错误：{exc}")
    except OSError as exc:
        print(f"串口错误：{exc}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

默认安全路径：

```powershell
uv run python app.py
```

程序会直接退出，不构造 `HidBridge`。确认活动窗口安全后才运行：

```powershell
uv run python app.py --run --port COM3
```

或者使用自动发现：

```powershell
uv run python app.py --run
```

内层 `finally` 会在串口仍打开时请求 `STOP_ALL`，随后上下文管理器的 `__exit__()` 再执行一次尽力停止并关闭端口。`--run` 是显式授权开关，不是“试运行”参数。业务应用可以换成配置项、管理员确认或 UI 二次确认，但默认状态必须保持为不发送输入。

## 9. 错误处理与恢复

| 场景 | 异常 | 建议处理 |
|---|---|---|
| VID/PID 自动发现失败 | `RuntimeError` | 检查固件、USB 枚举、VID/PID，或显式指定 COM 口 |
| COM 口不存在、被占用或打开失败 | pyserial 异常、`OSError` 或 `RuntimeError` | 检查端口号，关闭串口工具或另一个客户端后重新打开 |
| 设备最终没有匹配响应 | `TimeoutError` | 记录操作和端口，退出旧上下文，检查 USB/固件后再决定是否重连 |
| 固件持续返回 `BUSY` | `RuntimeError` | 等待当前操作结束；确认没有第二个应用同时控制设备 |
| 固件返回 `NACK` | `RuntimeError` | 根据异常中的错误名和编号修正命令、参数或 SDK/固件版本 |
| 组合键、按钮、文本或脚本无效 | `ValueError` | 修正输入；这类错误通常在写串口前产生 |
| 移动量或等待时间超出编码范围 | `OverflowError` | 在业务层限制为协议支持的整数范围 |
| 串口读取、写入或 DTR 操作失败 | pyserial 异常、`OSError` 或 `RuntimeError` | 视为当前会话不可用，关闭后检查连接 |
| 对象未打开、正在关闭或会话已更换 | `RuntimeError` | 停止旧任务，只向新会话重新提交明确的新任务 |

推荐恢复顺序：

1. 记录失败的方法、参数范围、端口和异常文本，不记录敏感业务文本。
2. 如果仍在 `with` 块内，尽力调用 `stop_all()`；清理失败时不要覆盖原始异常。
3. 退出上下文，让 `close()` 使旧会话失效。
4. 检查 USB 枚举、COM 口占用、固件版本和设备电源状态。
5. 重新运行只读连通测试。
6. 只有业务逻辑能确认重复执行安全时，才建立新会话并重新提交操作。

SDK 只对响应超时和 `BUSY` 进行配置次数内的重试。`NACK`、串口 I/O 错误、格式错误的响应和意外响应类型会立即终止。不要在上层增加无上限循环，否则可能在不知道上一项操作是否已执行时重复产生输入。

## 10. 心跳、DTR、并发与会话生命周期

### 心跳和控制租约

`open()` 成功后，SDK 会启动后台心跳线程，默认每 500 毫秒发送一次心跳。固件要求持续心跳来维持两秒控制租约；进程停止、串口断开或心跳中断后，租约到期会释放保持中的键和鼠标按钮。

打开连接时 SDK 置位 DTR，正常关闭时取消 DTR。DTR 或 USB 连接丢失也会触发固件侧的安全重置。Python SDK 当前不通过 `HidBridgeOptions` 暴露心跳间隔，应用无需自行发送心跳。

### 并发规则

- 同一个 `HidBridge` 的普通命令和脚本事务通过递归命令锁串行执行。
- 心跳与命令共享写入锁，不会与命令字节交叉写入。
- 不要为同一个 COM 口创建两个客户端；Windows 串口独占和固件 `BUSY` 都可能导致失败。
- 可以从多个线程提交命令，但需要由应用定义顺序。对顺序敏感的一组操作应使用单一工作队列或 `run_script()`。
- 不要在另一个线程仍调用对象方法时丢弃最后一个对象引用；先停止工作线程，再退出上下文。

### 会话代次

每次成功 `open()` 都创建新的生命周期代次。`close()` 或重新打开会使旧代次失效。排队中的旧脚本和 `BUSY` 等待会检查代次并中止，剩余命令不会通过新连接继续发送；旧响应也不能满足新会话中的请求。

`close()` 的主要顺序是：

1. 标记对象正在关闭，并使当前会话代次失效。
2. 通知心跳线程停止。
3. 在串行写入锁内尽力发送 `STOP_ALL`。
4. 取消 DTR 并关闭串口对象。
5. 等待心跳线程退出并清理会话状态。

如果旧心跳线程仍在停止，`open()` 会拒绝开始新会话。应用应等待当前 `close()` 完成，不要用高频循环同时关闭和重新打开同一个对象。

## 11. 正常退出与异常退出

推荐结构是上下文管理器加内层清理：

```python
with HidBridge(options) as hid:
    try:
        # 真实输入操作
        pass
    finally:
        try:
            hid.stop_all()
        except Exception as stop_exc:
            print(f"STOP_ALL 发送失败：{stop_exc}")
```

`with` 块退出时会调用 `close()`。即使没有显式调用 `stop_all()`，`close()` 也会尽力发送停止命令、取消 DTR 并关闭端口；显式清理可以让应用记录停止失败，并让安全意图更清晰。

上下文管理器和 `close()` 不是进程崩溃、强制结束、系统掉电或 USB 瞬断时的绝对保证。在这些情况下，最终保护来自固件的两秒控制租约以及 DTR/USB 断开触发的安全重置。

对于服务或长期运行程序，还应：

- 在停止工作线程后再退出桥接器上下文。
- 在可控的服务停止、窗口关闭、`KeyboardInterrupt` 和取消操作路径中统一执行清理。
- 不在底层信号处理器中执行复杂的串口逻辑。
- 重连时创建清晰的新任务边界，不延续旧会话中尚未确认的脚本。

## 12. 常见问题

### 自动发现报告找不到端口

确认设备管理器中存在 CDC COM 口，并用 `list_ports()` 检查 `VID:PID` 是否为 `CAFE:2350`。固件使用自定义 VID/PID 时，在 `HidBridgeOptions` 中传入对应值，或者直接填写端口。

### 打开端口时报拒绝访问

串口通常正被串口监视器、另一个 SDK 客户端或旧进程独占。关闭其他客户端后重试；不要同时运行两个控制程序。

### `ping()` 成功，但没有键鼠动作

确认运行的是带 `--run` 的真实输入路径，当前窗口可以接收输入，并且 SDK 与固件版本匹配。先测试小幅鼠标移动或在空白文本编辑器中测试一个安全字符。

### 中文或其他 Unicode 文本无法输入

`type_text()` 和脚本 `type` 使用美式键盘 ASCII 映射，不是 Unicode 文本注入。非 ASCII 输入需要由应用根据目标键盘布局拆成受支持的按键操作；SDK 不提供布局无关的文本输入。

### 经常出现 `BUSY`

确认没有第二个客户端，同一应用也没有反复创建多个桥接器实例。长脚本执行期间的其他命令会被 SDK 串行化；跨进程竞争则无法由单个客户端锁解决。

### 超时后是否可以直接重发

SDK 已在协议允许的范围内重试。最终 `TimeoutError` 表示执行状态可能未知，不应盲目重发会产生副作用的命令。先退出旧上下文、检查设备并运行只读连通测试。

### 退出后按键仍像被按住

正常退出路径必须尽力调用 `stop_all()`。如果进程被强制终止，等待两秒控制租约到期；仍未恢复时断开板卡 USB，并检查固件是否为当前版本。

## 13. 生产接入检查清单

发布应用前逐项确认：

- [ ] 固件与 SDK 来自相互匹配的仓库版本。
- [ ] 安装文档明确支持的 Windows、Python 和 uv 版本。
- [ ] 启动时先用 `ping()`、`info()` 和 `caps()` 完成只读健康检查。
- [ ] 自动发现失败时允许用户显式选择 COM 口。
- [ ] 多设备环境不依赖第一个 VID/PID 匹配项。
- [ ] 默认运行模式不产生 HID 输入，真实输入需要显式授权。
- [ ] UI 或命令行在启用输入前提示活动窗口风险。
- [ ] 所有组合键、文本、移动量、滚轮量和等待时间在业务层验证范围。
- [ ] 顺序敏感操作使用单一队列或 `run_script()`。
- [ ] 同一设备同一时间只有一个客户端。
- [ ] 分别处理 `TimeoutError`、`ValueError`、`OverflowError`、`RuntimeError` 和串口异常。
- [ ] 最终失败后不进行无上限或无条件重试。
- [ ] 成功、失败、取消和正常退出路径都尽力调用 `stop_all()`。
- [ ] 工作线程退出后才离开 `with` 块或调用 `close()`。
- [ ] 已实际验证两秒租约、USB 拔出和应用异常退出后的输入释放行为。
- [ ] 日志足以定位端口、操作类别和异常，但不会泄露敏感输入文本。

完成以上检查后，再把真实输入功能交付给最终用户或上层业务模块。
