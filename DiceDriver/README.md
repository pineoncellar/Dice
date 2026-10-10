# DiceDriver (Python)

把 x64 的 `w4123.Dice` DLL 接到 **OneBot 11**（正向 / 反向 WebSocket）的宿主程序。
接口取舍与行为约定见 [`DiceSrc/docs/DiceDriver-interface-duties.md`](../DiceSrc/docs/DiceDriver-interface-duties.md)（v3，已确认）；
整个工作区的构建与启动方式见 [`DiceSrc/docs/使用文档.md`](../DiceSrc/docs/使用文档.md)。

## 结构

```
shim/dd_shim.cpp        原生垫片(x64, MSVC /MT)：持有 Dice 要求的 C++ ABI API 表，
                        39 个 thunk 统一转发给 Python 的一个回调
build-shim.ps1          编译垫片 -> <工作区>\output\dd_shim.dll
dicedriver/
  config.py             dicedriver.toml 读取与校验
  logs.py               主日志(含 TRACE) + Dice DebugLog 单独文件，异步写盘
  cq.py                 CQ 码 <-> OneBot 消息段
  onebot.py             正向/反向 WS、echo 配对、断线重连、access_token
  bridge.py             OneBot 中转：给外部 bot（麦bot）的第二个 OB11 端点 + 跑团消息门控
  state.py              缓存与持久状态（成员/好友/昵称/最后发言/禁言到期/请求 flag）
  hostapi.py            39 个宿主接口的实现（纯 Python，可单测）
  native.py             ctypes：加载垫片与 Dice DLL，派发回调、投递事件
  events.py             OneBot 事件 -> Dice 的 eventXxx（按会话保序、工作线程池）
  notify.py             DebugMsg 合并限流投递 / DiceHeartbeat 异步 POST
  app.py, __main__.py   装配、生命周期、Reload/Remake/Killme
  __init__.py           包版本号 __version__：.bot 回执里的驱动版本与打包版本都取自这里
tests/                  单元测试 + 端到端（真 Dice DLL + 脚本化的假 OneBot）
logs/                   运行日志（dicedriver.log 与 dice-<QQ>.log）
```

工作区里与本目录相关的其他位置：

| 位置 | 内容 |
|---|---|
| `<工作区>\output\` | 编译产物：`w4123.Dice.windows.amd64.dll`、`dd_shim.dll` |
| `<工作区>\build\` | 中间产物（CMake、对象文件、vcpkg 缓存与日志） |
| `<工作区>\data\` | Dice 数据目录（`Dice<QQ>\`）与驱动状态（最后发言表、禁言到期表） |
| `<工作区>\*.bat` | `build-dicedriver.bat`（构建本目录）、`start-dicedriver.bat`（启动） |
| `<工作区>\build-portable.ps1` | 组装便携包：把本目录 + 两份 DLL + 一份 embeddable CPython 打成一个能拷到**没有开发环境**的机器上直接跑的目录（详见下节） |

路径关系写在 `dicedriver.toml` 里（都是相对本文件的路径）：`dice_dll` 与 `shim_dll` 指向
`../output`，`root_dir` 与 `state_dir` 指向 `../data`，日志仍留在本目录的 `logs/`。

为什么需要垫片：Dice 的宿主接口是 C++ ABI（`const std::string&`、`const std::set<long long>&`、
`unordered_map<string, void*>`），Python/ctypes 造不出这些对象，所以由一个与 Dice 同工具链同 CRT 的
小 DLL 承担 ABI，再用 `(api, ints, text, out, cap)` 这种纯 C 形式转给 Python。

## 使用

最简单的方式是用工作区根目录的批处理（它会把路径都算好）：

```bat
build-dicedriver.bat     :: 建虚拟环境、装依赖、编译垫片 DLL
start-dicedriver.bat     :: 启动驱动（读 dicedriver.toml）
```

等价的手动步骤：

```powershell
# 1) 编译垫片（需要 VS2022 的 x64 工具链；会自动找 vswhere，也可设 DD_VCVARS64）
powershell -ExecutionPolicy Bypass -File build-shim.ps1

# 2) 安装依赖（.venv 已建好）
.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 3) 配置
copy dicedriver.example.toml dicedriver.toml     # 修改 onebot 段 / notify 等

# 4) 检查并运行
.venv\Scripts\python.exe -m dicedriver --check
.venv\Scripts\python.exe -m dicedriver
```

Python 部分的改动不需要重新构建，直接重启驱动即可。

OneBot 端：
- 正向（`mode = "forward"`）：OneBot 实现开启正向 WS 服务，`url` 指向它；`access_token` 以 `Authorization: Bearer` 发送。
- 反向（`mode = "reverse"`）：OneBot 实现的反向 WS 地址填 `ws://<host>:<port><path>`；token 可走头或 `?access_token=`。

注意：
- 骰娘账号取自 OneBot 连接（`get_login_info` / `X-Self-ID`），`bot_qq` 非 0 时会校验。
- `root_dir` 必须能用本机 ANSI 代码页表示（Dice 用 `std::filesystem::path` 读它），请避免中文路径。
- Dice 自身配置（主人、`config:` 下的开关等）在 `<root_dir>/Dice<QQ>/conf/console.yaml`。
- 想用管理指令：先把主人 QQ 写进该文件的 `master:`；`.system reload|remake|die` 需主人权限。

## OneBot 中转：把外部 bot 接进来（可选）

目的是让一个**独立的 AI 应用**（下称麦bot）与骰子共用同一个 QQ：由麦bot 自己连 OneBot 协议端时，
谁也拦不住它在跑团中插话；改成让麦bot 连本驱动，就有一处可以掌控的地方。

```
QQ ── 协议端(SnowLuma 等) ──► [onebot] 上游链路 ──► Dice DLL
                                    │
                                    └──► [bridge] 中转服务 ──► 麦bot
```

开启方式（`dicedriver.toml`，全部字段见 `dicedriver.example.toml`）：

```toml
[bridge]
enabled = true
port    = 6701      # 可自定义；端口不与 [onebot] reverse 冲突即可
```

麦bot 侧把它当普通 OneBot 协议端配置即可（`ws://127.0.0.1:6701/`），**不需要改麦bot**。

工作机制：

| 方向 | 行为 |
|---|---|
| 上行（麦bot → 协议端） | 逐个 action 透传；驱动换成自己的 echo 发往上游，再把上游的响应帧（`status`/`retcode`/`data`/`wording`）**原样**用麦bot 的 echo 还回去 |
| 下行（协议端 → 麦bot） | 事件逐条转发；`message_sent` 默认不发（同号时无法分辨是谁发的，发下去会让两边互相回话） |

**唯一的过滤条件是"消息收发"**：某个聊天正在跑团（Dice 开了 `.log` 记录）时，该聊天的
**群消息事件不下发**、并且麦bot 发往该群的**消息被拒绝**（返回 `status=failed` + 非 0 retcode）。
`get_*` 查询、群管理、`delete_msg` 等**一律照常透传**——中转不限制麦bot 的能力，只掐消息。

跑团状态来自 Dice 的 `@dice.logstate` 上报（见 `DiceSrc/docs/DiceDriver-interface-duties.md` §4.4）：

- Dice 启动时先发一份**全量快照**（`action=snapshot` 若干条 + `action=snapshot-end` 一条终止行），
  驱动据此重建状态；之后 `.log new|on|off|end` 逐条增量上报。
- **快照之前状态未知**，此时按 `on_unknown = "closed"` 一律拦截（更安全）。若始终收不到任何上报
  （例如 Dice DLL 版本过旧），超过 `snapshot_grace_s` 后会放弃该默认并打一条 warning——
  避免麦bot 被永久静音。
- 上报在 Dice 侧是**同步**调用，且排在 mod 脚本分发之前，所以"开启记录的那条消息本身"就已经被拦住，
  不存在错位一条消息的窗口。
- 因此 **Dice DLL 与驱动必须成对升级**：只有新的 DLL 才会发这份上报。

其余可调项：`gate_private`（是否也拦截绑到跑团会话的私聊，默认只拦群消息）、
`access_token`（与 `[onebot]` 各自独立）、`queue_limit`（单客户端积压上限，超出即丢，防止慢客户端拖垮驱动）。

## 便携包：搬到没有开发环境的机器

目标机不需要 Python、venv、VS2022、编译器或 VC++ 运行库。在开发机上执行（要求 `output\` 下两份 DLL 与本目录的 `.venv` 都已就绪，即先跑过 `build-dicesrc.bat` 和 `build-dicedriver.bat`）：

```powershell
powershell -ExecutionPolicy Bypass -File build-portable.ps1        :: -> <工作区>\portable\Dice\
powershell -ExecutionPolicy Bypass -File build-portable.ps1 -Zip   :: 另出 portable\Dice-portable.zip
```

产出目录由 `runtime\`（embeddable CPython 3.11 + 随包的 websockets）、`DiceDriver\`（本目录的 `dicedriver` 包）、`Diceki\`（两份 DLL 与 `dicedriver.toml` 放在一起）、`data\`（空数据目录，可放入旧的 `Dice<QQ>\`）以及 `start-dicedriver-portable.bat`、`README-portable.txt` 组成；整个目录可任意挪动、整个拷走。

配置被放到 DLL 旁边，所以在生成时脚本会把配置里 `dice_dll`/`shim_dll` 的 `../output` 前缀改写成同目录的 `.`（`[log]` 的 `logs/…` 也以配置为基准，日志因此落在 `Diceki\logs\`），并检查这两项确实指向包内的文件。这一步同样不需要改源码：`config.py` 本来就是把相对路径解析到配置文件自身所在目录，自动重启也传的是绝对 `--config` 路径。

不用"连 `.venv` 一起拷"：venv 不可重定位，`pyvenv.cfg` 里记着创建时解释器的绝对路径。可选参数见脚本头部注释与 `DiceSrc/docs/使用文档.md` §4.2。

## 与文档契约的偏差/实现选择（供审阅）

1. **`Reload` 用"整进程重启"实现**，而不是文档 §8 的 `eventDisable → FreeLibrary → LoadLibrary`。
   原因：Dice 带后台线程与静态全局，进程内卸载不安全。流程：先 `eventDisable/eventExit`，再拉起新驱动进程
   （`--wait-pid` 等旧进程退出以释放端口/日志），旧进程 `os._exit`。`Remake` 同理（Dice 自己已停并备份）。
2. **`Killme`**：延迟 0.5 s 后 flush 状态并退出进程。
3. **文件上传**：`UploadGroupFile` 异步发出，文件不存在/发送失败只记日志并返回 `true`（决策 ⑭，抑制 Dice 的私聊回退）；
   `SendPrivateFile` 同步等待结果，返回真实成败。
4. **好友申请**：`AnswerFriendRequest` 的附言文字 OneBot 无对应；同意后会按 `friend_greeting` 以私聊发出 Dice 的欢迎语。
5. **`AnswerGroupInvited`**：`3`（忽略）按拒绝处理；只处理 `sub_type=invite`，主动入群申请(`add`)不交给 Dice。
6. **Dice 启动前收到的事件会被丢弃**（debug 日志可见），重连不会重启 Dice。
7. 失败的宿主接口调用一律返回文档约定的回退值（`IsXxx` → Dice 传入的 default、`GetGroupLastMsg` → -1、其余 0/空），异常不会越过 DLL 边界。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest tests -q          # 全部（端到端需 output 下的 Dice DLL 与 dd_shim.dll，缺则自动跳过）
.venv\Scripts\python.exe -m pytest tests -q -k "not e2e"   # 仅单元测试
```

端到端测试默认使用 `<工作区>\output\` 下的产物，可用环境变量 `DICE_DLL` 指定其他 DLL。
