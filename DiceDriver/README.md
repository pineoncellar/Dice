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
| `<工作区>\data\` | Dice 数据目录（`Dice<QQ>\`），最后发言表与禁言到期表在其中 `Dice<QQ>\user\` |
| `<工作区>\*.bat` | `build-dicedriver.bat`（构建本目录）、`start-dicedriver.bat`（启动） |
| `<工作区>\build-portable.ps1` | 组装便携包：把本目录 + 两份 DLL + 一份 embeddable CPython 打成一个能拷到**没有开发环境**的机器上直接跑的目录（详见下节） |

路径关系写在 `dicedriver.toml` 里（都是相对本文件的路径）：`dice_dll` 与 `shim_dll` 指向
`../output`，`root_dir` 指向 `../data`，日志仍留在本目录的 `logs/`。状态文件的位置不用配，
固定跟着 `root_dir` 走：`<root_dir>\Dice<QQ>\user\`（`Dice<QQ>` 由 Dice 自己创建，驱动不会替它建）。

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

**唯一的过滤条件是"消息收发"**：某个聊天窗口处于**非默认态**时——Dice 正在跑团（`.log` 记录中）、
或该群的开关失效（`.bot off` / `.reply off`）——该窗口的**群消息事件不下发**、并且麦bot 发往该窗口的
**消息被拒绝**（返回 `status=failed` + 非 0 retcode）。
`get_*` 查询、群管理、`delete_msg` 等**一律照常透传**——中转不限制麦bot 的能力，只掐消息。

三个开关与默认态：

| 开关 | 默认 | 失效（拦截） | Dice 侧依据 |
|---|---|---|---|
| bot | 开 | `.bot off`（群管） | 群记录 `停用指令` |
| reply | 开 | `.reply off`（群管/信任） | 群记录 `禁用回复` |
| log | 关 | `.log new` / `.log on` | `isLogging` |

窗口开关状态来自 Dice 的 `@dice.logstate` 上报（见 `DiceSrc/docs/DiceDriver-interface-duties.md` §4.4），
报文里三个开关同一条：`on=`（跑团）、`bot=`、`reply=`。

- Dice 启动时先发一份**全量快照**（`action=snapshot` 若干条 + `action=snapshot-end` 一条终止行），
  驱动据此重建状态；之后 `.log` 变化、`.bot`/`.reply`/`.group` 改动开关、群记录回收都会逐条增量上报。
- **快照之前状态未知**，此时按 `on_unknown = "closed"` 一律拦截（更安全）。若始终收不到任何上报
  （例如 Dice DLL 版本过旧），超过 `snapshot_grace_s` 后会放弃该默认并打一条 warning——
  避免麦bot 被永久静音。
- 上报在 Dice 侧是**同步**调用，且排在 mod 脚本分发之前，所以"开启记录/静音的那条消息本身"就已经被拦住，
  不存在错位一条消息的窗口。
- 老版本 DLL 的报文没有 `bot=`/`reply=`，驱动按"两者皆 on"处理，即退化为只按 log 门控；
  但 **`.bot off` / `.reply off` 的门控需要新的 DLL**，因此 **Dice DLL 与驱动建议成对升级**。
- 插件绕过命令入口直写群记录（如 JS/Python 直接写上下文对象）时，宿主要到下一次启动快照才会看到。

排查门控是否已就绪：

- Dice 侧：`.admin bridge this state` 查看当前窗口的三个开关与门控结论（`g[群号]` 换群聊、省略 g 按私聊 QQ 号），
  `.admin bridge list` 列出上报集合（正在记录的窗口 + 开关失效的群窗口）及各自结论。输出里 `上报通道` 显示
  `未注册` 或 `启动快照` 显示 `未发出`，就说明宿主还没收到任何上报，此时驱动按 `on_unknown` 一律拦截。
- 驱动侧：`[log] level = "trace"` 时每条报文都会记一行 `logstate <action> -> <snapshot|unknown>, N not-default: [...]`，
  重建完成（出现 `snapshot`）之后门控才按真实状态行事。

其余可调项：`gate_bot` / `gate_reply`（是否把这两个开关计入门控，默认 true；置 false 即可退回"只按 log 门控"，
不必改 Dice）、`gate_private`（是否也拦截绑到跑团会话的私聊，默认只拦群消息）、
`access_token`（与 `[onebot]` 各自独立）、`queue_limit`（单客户端积压上限，超出即丢，防止慢客户端拖垮驱动）。

## 便携包：搬到没有开发环境的机器

目标机不需要 Python、venv、VS2022、编译器或 VC++ 运行库。在开发机上执行（要求 `output\` 下两份 DLL 与本目录的 `.venv` 都已就绪，即先跑过 `build-dicesrc.bat` 和 `build-dicedriver.bat`）：

```powershell
powershell -ExecutionPolicy Bypass -File build-portable.ps1        :: -> <工作区>\portable\Dice\
powershell -ExecutionPolicy Bypass -File build-portable.ps1 -Zip   :: 另出 portable\Dice-portable.zip
```

产出目录由 `runtime\`（embeddable CPython 3.11 + 随包的 websockets）、`DiceDriver\`（本目录的 `dicedriver` 包）、`Diceki\`（两份 DLL 与 `dicedriver.toml` 放在一起）、`data\`（空数据目录，可放入旧的 `Dice<QQ>\`）以及 `start-dicedriver-portable.bat`、`README-portable.txt` 组成；整个目录可任意挪动、整个拷走。

配置被放到 DLL 旁边，所以在生成时脚本会把配置里 `dice_dll`/`shim_dll` 的 `../output` 前缀改写成同目录的 `.`（`[log]` 的 `logs/…` 也以配置为基准，日志因此落在 `Diceki\logs\`），并检查这两项确实指向包内的文件。这一步同样不需要改源码：`config.py` 本来就是把相对路径解析到配置文件自身所在目录，自动重启也传的是绝对 `--config` 路径。两个启动脚本都会带上 `--supervise`，因此退出码、崩溃报告与自动重启在目标机上默认就有（见「崩溃取证与自动恢复」）。

不用"连 `.venv` 一起拷"：venv 不可重定位，`pyvenv.cfg` 里记着创建时解释器的绝对路径。可选参数见脚本头部注释与 `DiceSrc/docs/使用文档.md` §4.2。

## 崩溃取证与自动恢复：让"什么都没留下"不可能发生

Dice 是**进程内**加载的 C++ DLL（`ctypes.CDLL`）。它自己的线程里一旦出现访问冲突、堆损坏或
`abort`，整个宿主进程会在毫秒内消失，Python 侧没有任何机会执行——这就是"日志停在半句话上、
窗口像卡住"的那种死法。2026-10-15 那次就是这样：驱动日志最后一行是 13:03:10.578 的
`meta_event heartbeat`，而 Dice 自己每 5 分钟一次的「自动保存」本该在 13:03:28 出现，也没有出现，
两者合起来把死亡窗口夹在 13:03:10–13:03:28 这 18 秒里。

现在有三层兜底，外加一个进程外的观察者。

### 进程内：`dicedriver/crashguard.py`

| 文件 | 内容 | 为什么不会丢 |
| --- | --- | --- |
| `logs/crash.log` | 原生崩溃的全部线程栈、未捕获异常全文、卡死时的栈、每次启动/退出的分界行 | `faulthandler` 直接写文件描述符，不经过任何队列 |
| `logs/stderr.log` | `sys.stdout`/`sys.stderr` 的副本（traceback、原生 abort 文本） | 先写文件再写控制台：窗口卡住不再连带丢失 |
| `logs/journal.log` | `info` 及以上的同步副本（生命周期、warning/error/异常全文） | 调用线程直接落盘并 flush，不经过异步队列 |
| `logs/alive.json` | pid、运行时长、各 tick 源年龄、日志队列深度、线程名、最近 32 个操作 | 每 `liveness_s` 覆盖一次，是 supervisor 判断"卡死"的依据 |

- `faulthandler.enable(file=crash.log, all_threads=True)`：Windows 上它挂的是
  unhandled-exception 过滤器，所以 **Dice DLL 里的访问冲突也会把所有线程的 Python 栈写下来**
  （要拿到 C++ 帧另见下面的 WER dump）。
- `sys.excepthook` / `threading.excepthook` / `sys.unraisablehook` 都接到同一个文件，同时往主日志
  写一行 `critical` 摘要。
- 看门狗线程：`[log] stall_s`（默认 180 秒）内**没有任何 tick**（事件循环与维护循环各自上报 tick）
  就判定卡死，把全部线程栈 dump 进 `crash.log` 并刷新 `alive.json`。GIL 被死循环占住时看门狗
  自己也会停，那种情况由下面的 supervisor 收尸。
- 每个 run 在 `crash.log` 里都有 `===== driver started =====` 与 `===== driver exiting =====`
  两条分界行：**只有开头没有结尾的那一段，就是硬死的那一次**。

### 进程外：`--supervise`（两个启动器都已默认带上）

```powershell
python -m dicedriver --config Diceki\dicedriver.toml --supervise
```

父进程只做一件事：盯着子进程，所以它一定活得比子进程久，也就一定看得见退出码。

- `logs/supervisor.log`：每次启动/退出/重启/击杀一行，带 PID、运行时长、退出码与含义。
- `logs/incidents/incident-<时间>-pid<PID>.txt`：**每次异常退出**（含被击杀的卡死）生成一份报告：
  退出码（十进制 + 十六进制 + 含义）、运行时长、`alive.json` 最后状态、`crash.log`/`journal.log`/
  `stderr.log`/`dicedriver.log` 尾部，以及"下次怎么拿到原生栈"的现成命令。
- 退出码先分类，再决定要不要重启：
  - `0` 正常停止 → 不重启；
  - `75` Dice 的 Reload/Remake（监督模式下驱动不再自己 fork 替代进程，而是要求 supervisor 拉起）；
  - `2` 配置或 DLL 有问题 → 不重启（重启只会空转）；
  - `0xC0000005` 访问冲突、`0xC0000374` 堆损坏、`0xC0000409` fast-fail 等 → 记报告并重启。
- 重启上限：`--max-restarts`（默认 5）与 `--restart-window`（默认 600 秒）内超限就打印
  `CRASH LOOP` 并停下，避免疯狂重启把日志刷爆。
- 卡死击杀：`alive.json` 超过 `stall_s` 没更新（默认取配置，`--stall-timeout 0` 可关），supervisor
  打印 `STALL`、写报告、杀掉子进程并按异常退出处理——这是 GIL 被死循环占住时唯一的出路。
- Ctrl+C：supervisor 与子进程在同一个控制台组里，都能收到；驱动自己优雅停止（退出码 0），
  supervisor 不重启。启动器末尾那句 `pause` 也换成了明确提示，不会再让"回车把程序搞崩了"
  这种误判发生（那次真正的时间点是进程早已死掉，回车只是关掉了窗口）。

其余开关见 `python -m dicedriver --help`：`--no-restart`、`--restart-delay`、`--max-restarts`、
`--restart-window`、`--stall-timeout`。

### 出事了先看哪

1. `logs/supervisor.log` 最后几行：退出码 + 含义 + 报告文件名。
2. `logs/incidents/incident-*.txt`：退出码、运行时长、所有取证文件尾部。
3. `logs/crash.log`：出现 `Windows fatal exception` 就是原生崩溃；出现 `STALL` 就是卡死。
4. 要定位到 C++ 帧，得有一份原生 dump：按报告里的命令注册 WER LocalDumps，崩后取
   `C:\dumps\python.exe.*.dmp` 用 WinDbg 打开；同一时刻 `Application` 事件日志里的 Event ID 1000
   还会记下出错模块与偏移。

### 一个已经存在的好消息

`ctypes` 会把**在一次 ctypes 调用内部**发生的访问冲突翻译成 `OSError`
（`exception: access violation reading 0x…`）。事件分发路径上的这类故障因此不会带走进程，
而是被 `EventRouter` 记成 `handler for X event failed`（带完整 traceback）后继续跑。真正会静默
带走进程的，是 Dice 自己的线程里、或 ctypes 调用之外发生的崩溃——那正是上面这些文件覆盖的部分。

## 与文档契约的偏差/实现选择（供审阅）

1. **`Reload` 用"整进程重启"实现**，而不是文档 §8 的 `eventDisable → FreeLibrary → LoadLibrary`。
   原因：Dice 带后台线程与静态全局，进程内卸载不安全。流程：先 `eventDisable/eventExit`，再拉起新驱动进程
   （`--wait-pid` 等旧进程退出以释放端口/日志），旧进程 `os._exit`。`Remake` 同理（Dice 自己已停并备份）。
   在 `--supervise` 下不再自己 fork：驱动以退出码 `75` 收尾，由 supervisor 拉起替代进程（这样任何时刻
   只有一个"负责起进程的人"，替代进程也不会落在没人看的地方）。
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
