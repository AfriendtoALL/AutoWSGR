# AutoWSGR 第二 / 三梯队改进方案

> 编写日期：2026-10-11  
> 最近校正：2026-10-12（按**代码**核对完成状态，非按记忆）  
> 前置：第一梯队已落地（见文末「已完成」）  
> 配套：[Alas 对标评审报告](alas-comparison-review-2026-10-11.md)

---

## ⚠️ 状态总览（2026-10-12 校正）

**以代码为准，不以本文件的勾选框为准** —— 下表是逐项核实过代码证据的结论：

| 项                           | 状态              | 代码证据                                                                             |
| --------------------------- | --------------- | -------------------------------------------------------------------------------- |
| T2-1 调度器熔断                  | ✅ 完成            | `scheduler/recovery.py` 指数退避 + `DEFAULT_MAX_CONSECUTIVE_FAILURES`                |
| T2-2 `_overlay_ready` ctx 化 | ✅ 完成            | `context/game_context.py:149` `overlay_ready_maps`                               |
| T2-3 活动期号参数化                | ✅ 完成            | `autowsgr/data/event/{20260212,20260730,20260930}.yaml` + `data/event_config.py` |
| T3-1 拆分 `ScrcpyController`  | ⚠️ **部分完成**（见下） | 939→565 行，拆出 `adb_conn`/`video_stream`/`touch` 三模块                               |
| T3-2 消除逆向依赖                 | ✅ 完成 | `combat` 不再 import `ops`/`ui`/`cv2`/`PIL`；`fight()` 状态机 15 项 |
| T3-3① ops 脚本标注+保护           | ✅ 完成            | `testing/ops/README.md` + `conftest.py::pytest_collect_file` + 57 项回归            |
| T3-3② 实机截图 fixture          | 🟡 **部分完成**（见下） | 5 张入库 + 19 项断言；缺昼战、旗舰大破                                                          |
| P0-1 战斗恢复死循环                | ✅ 已封            | `combat/engine.py:105` `max_recovery_attempts`                                   |
| P0-2 调度器无熔断                 | ✅ 已封            | `scheduler/recovery.py`                                                          |
| P0-3 版本控制                   | ✅ 已封            | 889 文件已追踪，`.gitignore` 干净                                                        |

### 两项「部分完成」的具体差距

**T3-1 未做的部分**：本方案原定新增 `TouchInjectorSelector`（多后端探测 + 自愈）。  
实际落地的是 `TouchChannelChain` —— 一个**固定顺序的三级降级链**  
（MaaTouch → 常驻 shell → 单次 adb input），失败即永久降级。  
二者区别：前者能「标记失败后重新探测」（自愈），后者不能。  
当前选择降级链是合理的（MaaTouch 静默失效从机制上不可能，见  
`maatouch.py` 的 EOF 哨兵设计），但 `mark_failed` 自愈语义确实未实现。

**T3-2 已全部完成**（2026-10-12 补测）：三条验收标准现已全部满足 ——
静态检查、子进程导入检查、以及 `fight()` 主循环覆盖率
（`testing/combat/test_engine_state_machine.py` 15 项，只 mock 截图、
保留全部状态机逻辑）。详见 T3-2 章节的「补测说明」。

### 仍完全未做的两项（Alas 六项差距）

1. **卡死检测** —— `grep stuck_timer` 零命中。Alas 用 60s/180s 双计时器 +  
   `show_function_call()` 打印完整调用栈。**熔断只在「失败返回」时触发，  
   而真卡死是永不返回，熔断根本不会被触发** —— 这是当前 7×24 挂机  
   最大的盲区。注意 `scheduler/plan_runner.py:100` 的 `_watchdog` 是  
   **停止信号监听**，不是 Alas 意义上的卡死检测。
2. **异常→恢复映射** —— 95 处 `except Exception`，仅 12 处直接吞。  
   没有集中的「异常类型 → 恢复动作」映射表，无法回答「这次失败会自愈吗」。

---

## 阅读指南

第二梯队是**结构性改进**，收益大但需要设计，涉及跨模块改动，建议逐项独立提交、独立验收。  
第三梯队是**重构**，改动面大、回归风险高，建议排期到有整块时间时做。

每项都给出：问题证据 → 方案 → 代码骨架 → 风险 → 验收标准。**代码骨架是示意，不是可直接粘贴的补丁**，落地时需按实际上下文调整。

**共同原则**：所有改动都必须先补测试再改实现。第一梯队的经验是——熔断护栏的 11 项测试在移除护栏后会挂死（exit 124），这才是真测试；只断言「返回值等于 SL」的测试是假测试，防不住回归。

---

# 第二梯队：结构性改进

## T2-1【最高优先】调度器故障恢复与熔断 ✅ 已完成 2026-10-11

### 问题证据

`recover_to_main_or_restart()` 定义在 `ops/startup.py:304`，功能完整（含重启游戏），但实测调用点只有三处：

```
ops/startup.py:380          启动流程内
ops/expedition.py:36        远征任务内
testing/ops/test_startup_unit.py:121   单元测试
```

**`scheduler.run_daily()` 主循环从不调用它。**

而失败处理是（`scheduler/scheduler.py:255-268`）：

```python
try:
    self._run_task(task)
except Exception as exc:
    _log.opt(exception=True).error('[Scheduler] 子任务 {} 执行崩溃, 已跳过: {}', ...)
    if task.on_done is not None:
        try:
            task.on_done(CombatResult(flag=ConditionFlag.ACTION_FAILED))
        except Exception:
            pass
```

**失效路径**：夜间模拟器掉线 / 游戏闪退 / 网络断 → `goto_page` 抛 `NavigationError` → 吞成 `ACTION_FAILED` → 触发器 `_on_done` 只把 `_idle=True` → 下一轮继续产出任务 → 再失败 → **无限空转，日志持续写盘**（`logs/` 下已有 18+ 个时间戳目录）。

Alas 的对应机制：`alas.py` 的 `failure_record` 字典计数，同一任务失败 ≥3 次 → `RequestHumanTakeover` + 推送 + `exit(1)`。

### 方案

**在 `run_daily()` 里加两层**：单轮失败 → 立即尝试恢复；连续多轮失败 → 熔断重启游戏。

```python
# scheduler.py 新增常量
MAX_CONSECUTIVE_FAILURES = 3   # 连续失败多少次后重启游戏
RECOVERY_COOLDOWN = 30.0       # 两次恢复之间的最小间隔(秒), 避免连续重启


class DailyScheduler:
    def __init__(self, ...):
        ...
        self._consecutive_failures = 0
        self._last_recovery_at = 0.0

    def run_daily(self) -> None:
        while not self._ctx.stop_event.is_set():
            self._check_daily_reset()
            # ... 收集触发器、取队首不变 ...

            if task is not None:
                try:
                    results = self._run_task(task)
                    # 关键: 有结果就算成功, 重置失败计数
                    self._consecutive_failures = 0
                except Exception as exc:
                    self._consecutive_failures += 1
                    _log.opt(exception=True).error(
                        '[Scheduler] 任务 {} 失败 (连续第 {} 次)',
                        task.name, self._consecutive_failures,
                    )
                    self._handle_failure(task, exc)
            else:
                # 队列空: 挂机等待
                self._ensure_main_page_clean()
                time.sleep(self._idle_sleep)

    def _handle_failure(self, task: FightTask, exc: Exception) -> None:
        """失败处置: 先轻量恢复, 连续失败则重启游戏。"""
        # 让触发器复位, 否则该触发器可能卡死不再产出
        if task.on_done is not None:
            try:
                task.on_done(CombatResult(flag=ConditionFlag.ACTION_FAILED))
            except Exception:
                pass

        if self._consecutive_failures < MAX_CONSECUTIVE_FAILURES:
            self._try_recover_lightweight()
            return

        # 连续失败 → 熔断: 强制重启游戏
        if time.monotonic() - self._last_recovery_at < RECOVERY_COOLDOWN:
            _log.error('[Scheduler] 连续失败 {} 次且仍在冷却期, 本轮不重启',
                       self._consecutive_failures)
            return
        _log.error(
            '[Scheduler] 连续失败 {} 次 (超过阈值 {}), 重启游戏',
            self._consecutive_failures, MAX_CONSECUTIVE_FAILURES,
        )
        self._restart_game_safely()
        self._consecutive_failures = 0   # 重启后重新计数
        self._last_recovery_at = time.monotonic()

    def _try_recover_lightweight(self) -> None:
        """轻量恢复: 回主页面, 失败不致命。"""
        try:
            recover_to_main_or_restart(self._ctx, self._ctx.config.account.game_app)
        except Exception as exc:
            _log.opt(exception=True).warning('[Scheduler] 轻量恢复失败: {}', exc)

    def _restart_game_safely(self) -> None:
        """重启游戏 (最重手段), 自身异常不得打断主循环。"""
        try:
            restart_game(self._ctx.ctrl)
        except Exception as exc:
            _log.opt(exception=True).error('[Scheduler] 重启游戏失败: {}', exc)
```

### 需要注意

1. **导入位置**：`recover_to_main_or_restart` 在 `autowsgr.ops.startup`，`restart_game` 在 `autowsgr.ops`。注意 `scheduler` ← `ops` 的依赖方向（若已有反向依赖，需函数内 import 打破循环，参照 `engine.py:183` 的既有做法）。
2. **不要在 `_run_task` 内部恢复**：那里已经 `except` 过，再嵌一层会让异常语义混乱。恢复逻辑放在 `run_daily` 的调用点更清晰。
3. **冷却期是必要的**：若设备真的掉线，每轮都重启游戏会加剧问题。30 秒给 adb 重连留时间。

### 风险

| 风险                        | 缓解                           |
| ------------------------- | ---------------------------- |
| 误重启正在正常游戏的实例              | 只在**连续**失败 ≥3 次后触发，且有 30s 冷却 |
| `restart_game` 自身抛异常打断主循环 | `_restart_game_safely` 内部兜底  |
| 恢复逻辑与触发器 `_on_done` 重复复位  | 明确由 `_handle_failure` 统一负责复位 |

### 实施记录（2026-10-11，实际落地）

**关键发现（改变了方案设计）**：原方案设想「异常传播到 `run_daily` 的 except」，  
但实际读代码发现 **`_run_task` 内部已把异常吞掉并转成 `ACTION_FAILED`**  
（`scheduler.py:323-334`），异常**永远不会传播到调用方**。所以失败判定必须  
基于 `_run_task` 的**返回值**：

1. `_run_task` 返回类型 `None` → `list[CombatResult]`（新增 `all_results` 累积）
2. `run_daily` 用 `_is_failed_round(results)` 判定，而不是 `except`
3. 判定口径：**无结果** 或 **全部 flag 在 `_FAILURE_FLAGS` 中**。  
   混合结果（A/C 成功 D 点 SL）**不算失败** —— 该轮仍有产出。  
   `DOCK_FULL` / `SKIP_FIGHT` **不算失败** —— 前者是正常资源耗尽信号  
   （计入会导致正常挂机被误判为故障并重启游戏），后者是策略行为。

**阈值按用户要求改为 5**（原方案 3）：单次战斗周期约 100s，5 次连续失败  
≈ 8~10 分钟才判定「不是偶发抖动」。夜间一次网络抖动不应触发重启；  
而真掉线会持续失败，8 分钟内必然累积到阈值，仍远快于「空烧一夜」。

**实际落地参数**：

- `DEFAULT_MAX_CONSECUTIVE_FAILURES = 5`（可用 `--max-failures` 覆盖，0 = 禁用熔断）
- `DEFAULT_RECOVERY_COOLDOWN = 60.0`（可用 `--recovery-cooldown` 覆盖）
- 新增 `_try_recover_to_main()` / `_restart_game_safely()`，均**吞掉自身异常**  
  （恢复是尽力而为，不该打断主循环）

**测试**：21 项，含端到端跑真实 `run_daily` 循环验证计数归零。  
变异测试验证：把 `_is_failed_round` 改成恒 `return False` + 短路熔断后  
**8 项失败**，还原后全过。

**验证标准达成情况**：

- [x] 连续 5 次失败 → `restart_game` 调用 1 次
- [x] 失败后成功 → 计数归零（端到端跑真实循环验证）
- [x] 冷却期内 → 不重复重启
- [x] 恢复/重启自身抛异常 → 主循环继续
- [ ] 实机：手动杀游戏进程观察重启（**待做**）

---

## T2-2 `_overlay_ready` 从类变量改为上下文状态 ✅ 已完成 2026-10-11

> **落地方案与本节原设计略有出入**：最终没有新增独立的状态容器类，  
> 而是在 `GameContext` 上加了一个字段  
> `overlay_ready_maps: set[tuple[str | None, str, str | None]]`（键控关卡身份）。  
> 选择理由：ctx 在触发器模式下跨轮存活，生命周期恰好匹配「弹层可复用」的语义；  
> 而类变量是进程级的（同进程跑不同计划会共享、重启即丢、永不清理）。  
> 测试替身可能没有该字段，故读取处用 `getattr(ctx, 'overlay_ready_maps', None)`  
> 兜底（见 `ops/normal_fight.py:56`）。

### 问题证据

`ops/normal_fight.py:121`

```python
_overlay_ready: ClassVar[set[tuple[str | None, str, str | None]]] = set()
```

运行时状态存进**类变量**。这是为修「触发器每轮新建实例导致 `_skip_check` 归零」而引入的补丁（注释 `:106-111` 自认）。

**三个问题**：

1. 同进程跑不同计划会**串扰**（共享同一个 set）
2. 进程重启状态丢失
3. **永不清理**——set 只增不减，长期挂机内存缓慢增长

同类：`scheduler/triggers.py:120/198/207` 的 `exhaust_flags: ClassVar[set[...]]`（这批是常量语义，可保留）。

### 方案

状态应挂在 `GameContext` 上——它在触发器模式下**跨轮存活**，正好匹配这个状态的语义。

```python
# context/game_context.py
@dataclass
class GameContext:
    ...
    # 运行时缓存 (跨轮存活, 进程内唯一)
    _overlay_ready: set[tuple[str | None, str, str | None]] = field(
        default_factory=set, repr=False
    )
```

```python
# ops/normal_fight.py
# 删除类变量, 改为访问 ctx
def _is_overlay_ready(self, ctx: GameContext, key: tuple) -> bool:
    return key in ctx._overlay_ready

def _mark_overlay_ready(self, ctx: GameContext, key: tuple) -> None:
    ctx._overlay_ready.add(key)
```

**更彻底的做法**：这个 set 本质是「本次运行中已确认过某活动浮层已处理」的幂等标记。如果活动浮层的判据可靠（模板匹配而非「见过就没」），可以**直接去掉这个缓存**，每次重新识别。代价是每轮多几次模板匹配（~5ms，可忽略），收益是消除一整类状态 bug。

**建议**：先做 ctx 化（低风险），观察一个活动周期后再评估是否彻底移除。

### 风险

低。纯状态归属变更，不改逻辑。

### 验收标准

- [x] 单测：连续两次 `NormalFightRunner` 共享同一 ctx，第二次能命中缓存  
  → `test_normal_fight_unit.py::test_skip_check_survives_new_runner_instance`
- [x] 单测：不同 ctx 之间互不影响  
  → `test_normal_fight_unit.py::test_different_ctx_does_not_share_state`
- [x] 实机：日志不再每轮「已关闭残留关卡浮层」（2026-10-11 反馈驱动）

---

## T2-3 活动期号参数化 ✅ 已完成 2026-10-11

### 问题证据

实测 **9 处硬编码，跨 3 个文件**：

```python
# ui/event/event_page.py:315-316 —— 通用页面控制器里的单活动分支
if self._event_name == '20260930' and entrance is not None:
    raise ValueError('20260930 活动 map 请使用纯数字，不支持 α/β 入口')

# ui/event/event_page.py:147 —— 节点布局坐标按期号硬编码
'20260930': { ... }

# 模板文件名亦含期号
event_title_20260930_540p.png / fight_button_20260930_540p.png
event_icon_20260930_540p.png  / quiz_prompt_20260930_540p.png
```

**失效路径**：`_get_event_title_templates` 的 `find_any` 语义是「任一命中即算活动页」。新一期活动页面若与旧期相似，旧模板仍会命中 → `is_current_page` 返回 True 但 `is_fight_ready` 全 False → `_enter_node` 循环 10 次（2.5s）后抛 `ActionFailedError`，**日志只有一行 warning**。新活动上线即无声失效。

### 方案

**把活动相关的一切移到数据文件**，代码只读数据。

```
autowsgr/data/event/
├── 20260930.yaml        # 节点坐标、模板路径、入口支持
└── 20261115.yaml        # 下一期只需加文件
```

```yaml
# autowsgr/data/event/20260930.yaml
event_name: '20260930'
title: 最强之舟

# 模板 (相对 image_resources/templates/event/)
templates:
  title:      event_title_540p.png
  fight_button: fight_button_540p.png
  icon:       event_icon_540p.png
  quiz_prompt:  quiz_prompt_540p.png

# 入口支持: 该活动不提供 α/β 分入口
supports_entrance: false

# 节点布局坐标 (原 NODE_POSITIONS_BY_EVENT)
node_positions:
  A: [0.50, 0.62]
  B: [0.35, 0.48]
  C: [0.65, 0.48]
  D: [0.50, 0.35]
  E: [0.20, 0.30]
```


```python
# ui/event/event_page.py
from autowsgr.infra.file_utils import load_yaml

@lru_cache(maxsize=4)
def load_event_config(event_name: str) -> dict:
    """加载活动配置。未找到时抛出可读错误, 而非静默用旧模板。"""
    path = EVENT_CONFIG_DIR / f'{event_name}.yaml'
    if not path.exists():
        raise ActionFailedError(
            f'活动 {event_name} 无配置文件 ({path.name})。'
            f'新活动需先在 autowsgr/data/event/ 下新增 YAML。'
        )
    return load_yaml(path)

# 原 if 分支 → 配置查询
if entrance is not None and not cfg['supports_entrance']:
    raise ValueError(f'活动 {self._event_name} 不支持 α/β 入口, 请使用纯数字 map')
```

**关键改进**：把「找不到配置」变成**显式报错**，而不是「拿旧模板凑合」。这直接消灭无声失效。

### 风险

| 风险                    | 缓解                                                               |
| --------------------- | ---------------------------------------------------------------- |
| 改数据格式时弄错坐标            | 迁移时加一个测试：加载 20260930.yaml 后断言坐标与原 `NODE_POSITIONS_BY_EVENT` 完全一致 |
| `lru_cache` 在活动切换后不刷新 | 以 `event_name` 为 key，切换活动自然换缓存条目                                 |

### 验收标准

- [x] 单测：加载 20260930.yaml，断言 `node_positions` 与迁移前一致  
  → `testing/data/test_event_config.py::test_coordinates_match_migration`
- [x] 单测：请求不存在的活动，断言抛 `EventConfigError`  
  → `test_unknown_event_raises`
- [x] 实机：H5-ACD 计划跑 3 轮成功（2026-10-12 复验：`success (节点数=2, 恢复=0)`）
- [x] **额外加固**：模板名必须是 `<kind>_<期号>` 格式（`recognizer._is_event_overlay_button`  
  依赖 `startswith('fight_button')`），缺配置**显式报错不静默放行**  
  → `test_template_names_keep_kind_prefix`

---

# 第三梯队：重构

## T3-1 拆分 `ScrcpyController`（939 行上帝类）⚠️ 部分完成 2026-10-12

### 问题证据

`emulator/controller/scrcpy.py` 同时承担五件事：传输实现、H264 编解码、三套触控后端降级决策、ADB 生命周期、重连策略。

`_adb_input()`（`:748-781`）硬编码优先级链：

```python
if self._maatouch is not None and args[0] == 'tap':   # 只有 tap 走 MaaTouch
if self._persistent_shell is not None:                # 常驻 shell
dev.shell(['input', *args], timeout=timeout)          # 单次 adb
```

且初始化失败**永久置 None**（`:712/:765/:776`），运行期无法恢复。新增一种触控方式要改 `_adb_input` + `connect` + ABC 三处。

另外 `scroll`/`key_event` 走 scrcpy 控制流、`click`/`swipe`/`long_tap` 走 adb —— **同一抽象内两套传输语义**（`:741-746` 注释坦承是 MuMu 12 冲突的临时方案）。

### 方案：接口化 + 策略选择

```
emulator/controller/
├── protocol.py          AndroidController (ABC, 保持不变)
├── scrcpy_video.py      ScrcpyVideoSource   —— 只管截图流
├── touch.py             TouchInjector (ABC) + MaaTouchInjector + AdbInputInjector + PersistentShellInjector
├── selector.py          TouchInjectorSelector —— 按优先级选一个，带降级与自愈
└── scrcpy.py            ScrcpyController —— 组合上述部件，保持对外接口不变
```

```python
# touch.py
class TouchInjector(ABC):
    """触控注入后端。只负责把一次点击/滑动变成设备动作。"""
    @abstractmethod
    def available(self) -> bool:
        """当前是否可用 (二进制存在 / shell 存活)。"""
    @abstractmethod
    def tap(self, x: int, y: int) -> None: ...
    @abstractmethod
    def swipe(self, x1, y1, x2, y2, duration_ms) -> None: ...
    @abstractmethod
    def key(self, key_code: int) -> None: ...


class TouchInjectorSelector:
    """按优先级选择注入后端, 失败时降级并允许运行期自愈。

    替代原先内联在 ScrcpyController._adb_input 里的 if-else 链。
    关键改进: 后端失败是**可恢复**的 (mark_failed 后下次重新探测)，
    而原实现是「初始化失败永久置 None」。
    """
    def __init__(self, injectors: list[TouchInjector]) -> None:
        self._candidates = injectors
        self._active: TouchInjector | None = None
        self._failed: set[type] = set()

    def _pick(self) -> TouchInjector | None:
        if self._active is not None and self._active.available():
            return self._active
        for inj in self._candidates:
            if type(inj) in self._failed:
                continue
            if inj.available():
                self._active = inj
                return inj
        return None

    def tap(self, x: int, y: int) -> None:
        for attempt in self._candidates:
            if type(attempt) in self._failed or not attempt.available():
                continue
            try:
                attempt.tap(x, y)
                self._active = attempt
                return
            except Exception as exc:
                _log.warning('[Touch] {} 注入失败, 降级: {}', type(attempt).__name__, exc)
                self._failed.add(type(attempt))   # 本轮标记失败, 下轮重试
        raise DeviceUnavailableError('所有触控注入后端均失败')
```

**迁移策略（关键）**：**保持 `ScrcpyController` 的对外接口完全不变**，只换内部实现。这样上层 8012 行的 `ui/` 一行都不用改，风险降到最低。

Alas 的对应做法：`Device` 聚合 10 种截图方法，靠 `screenshot_methods()` 返回 dict + `@method_wrapper` 反射查表，启动时跑基准测试自动选最优。

### 风险

| 风险             | 缓解                                              |
| -------------- | ----------------------------------------------- |
| 触控时序变化导致实机行为改变 | MaaTouch 与 adb input 的点击间隔语义不同，需实机回归 H5-ACD 3 轮 |
| 大规模重导入         | 对外接口不变，只改内部；建议分两个 commit（抽类 / 换实现）              |

### 验收标准

> ⚠️ **原定验收标准已按实际落地重写**。原方案要求新增 `TouchInjectorSelector`  
> （多后端列表 + `mark_failed` 后重新探测的自愈机制），实际实现的是  
> `TouchChannelChain` —— 固定顺序三级降级链，**不具备自愈能力**。  
> 详见文首「两项部分完成的具体差距」。

- [x] 单测：MaaTouch 抛异常时正确降级到常驻 shell，且**永久**降级  
  → `test_touch_channel.py::test_maatouch_failure_drops_to_shell_and_closes_maatouch`
- [x] 单测：全部通道失败时兜底到单次 adb input  
  → `test_both_fail_reaches_adb`
- [x] 单测：MaaTouch 只处理 tap，swipe 必须绕过（它只实现了 click）  
  → `test_non_tap_never_uses_maatouch`
- [x] 变异验证（12 个变异全被捕获）：降级不置空 / MaaTouch 接管 swipe /  
  移除转发 / 并发重连去锁 / 控制流无限重试（挂死）等
- [x] 实机对照：`tools/diagnostics/probe_controller.py` 采before/after 基线 diff
- [x] 实机：H5-ACD 3 轮成功，日志显示实际使用的注入通道
- [ ] 单测：后端在 `mark_failed` 后下一轮会被重新探测（**自愈 —— 未实现**）
- [ ] 单测：全部后端不可用时抛 `DeviceUnavailableError`（**未实现**，当前逐级兜底不抛）

**顺带修的既有缺陷**（2026-10-12 实机发现）：`_resolve_adb_path()` 原只用  
`shutil.which('adb')`，Windows 上 adb 不在 PATH 故恒None，且 `_adb_path` 只在  
`detect_mumu_folder()` 成功分支里被赋值 → 常驻 shell 通道长期不可用。  
修复后 `persistent_shell` false→true，**swipe 延迟 250ms → 0ms**  
（MaaTouch 只实现 click，滑动全靠此通道）。

---

## T3-2 消除四个方向的逆向依赖 ✅ 已完成 2026-10-12

### 问题证据

实测反向依赖：

| 依赖                | 位置                           | 问题                                              |
| ----------------- | ---------------------------- | ----------------------------------------------- |
| `context` → `ui`  | `context/game_context.py:12` | context 位于 ui 之下却 import 上层                     |
| `context` → `ops` | `game_context.py:257-263`    | 函数内 `from autowsgr.ops import goto_page`        |
| `combat` → `ops`  | `combat/engine.py:183`       | 作者自标 `TODO: 轻微的抽象泄露`                            |
| `combat` → `ui`   | `combat/recognizer.py:99`    | import `event_page` 的**两个下划线私有函数**              |
| `combat` → `ui`   | `combat/handlers.py:34`      | `from autowsgr.ui.utils import wait_leave_page` |
| `combat` → `cv2`  | `combat/node_tracker.py:22`  | 绕过 `autowsgr.vision` 抽象层                        |

`docs/architecture/README.md:29` 明确写「每一层只依赖其下方的层」——**文档承诺与代码不符**。

后果：**combat 无法脱离 ui 独立测试**。`fight()` 主循环零覆盖。

### 方案：依赖注入 + 能力下沉

**① `restart_game` 改为注入回调**（消除 combat → ops）

```python
# combat/engine.py
class CombatEngine(PhaseHandlersMixin):
    def __init__(
        self,
        ctx: GameContext,
        *,
        on_sl: Callable[[], None] | None = None,   # SL 时的恢复动作
    ) -> None:
        self._on_sl = on_sl

# 原先
#   from autowsgr.ops import restart_game
#   restart_game(self._device)

# 改为
elif decision == ConditionFlag.SL:
    result.flag = ConditionFlag.SL
    if self._on_sl is not None:
        self._on_sl()
    break
```

```python
# ops/normal_fight.py 调用处
engine = CombatEngine(ctx, on_sl=lambda: restart_game(ctx.ctrl))
```

**② 模板获取提升为 `image_resources` 正式接口**（消除 combat → ui 私有函数）

```python
# 现状（recognizer.py:99，导入私有函数）
from autowsgr.ui.event.event_page import (
    _get_event_title_templates,
    _get_fight_button_templates,
)

# 改为：image_resources 是模板的唯一权威来源
from autowsgr.image_resources import event_templates
templates = event_templates.title_for(event_name)
```

把模板解析逻辑从 `ui/event/event_page.py` 移到 `image_resources/`，UI 层改为调用它——**方向反转：ui → image_resources，而不是 combat → ui**。

**③ `connectedComponentsWithStats` 封装进 vision**（消除 combat → cv2）

```python
# vision/ 提供 (或复用既有 ImageChecker)
def connected_components(image: np.ndarray, **kwargs) -> list[Component]: ...
# combat/node_tracker.py 改为
from autowsgr.vision import connected_components
```

**④ `context` 不依赖 `ui`/`ops`**

`game_context.py:257-263` 的 `sync_daily_drop_counts()` 通过 `goto_page` 回主页面。改为**回调注入**：由 `ops` 在启动时注册，context 只持引用、不 import。

### 风险

| 风险               | 缓解                                       |
| ---------------- | ---------------------------------------- |
| 循环依赖             | 用「函数内 import」或 `TYPE_CHECKING` 打破，避免顶层互引 |
| `fight()` 现有行为变化 | 依赖注入是纯结构调整，行为等价                          |

### 验收标准

- [x] 静态检查：`combat` 不再 import `ops`/`ui`/`cv2`/`PIL`  
  → `test_engine_circuit_breaker_unit.py::test_combat_layer_does_not_import_ops`  
  （AST 静态检查，非正则—— 正则会漏掉函数体内的局部 import）
- [x] 单测：`CombatEngine` 能在**不 import ops** 的前提下构造并跑完 `fight()`  
  → `test_waiting.py::TestCombatLayerIsIndependent`（子进程导入检查，  
  隔离已加载模块以避免全量测试下的误报）
- [x] 单测：`fight()` 主循环获得覆盖率 →
  `testing/combat/test_engine_state_machine.py` 15 项（2026-10-12）

### T3-2 补测说明（2026-10-12）

原「`fight()` 覆盖率为零」的说法需修正：`test_engine_circuit_breaker_unit.py`
的 17 项**全部 monkeypatch 了 `engine._step`**，测的是 `fight()` 的**外壳**
（步数护栏、恢复护栏、SL 回调、终止态分发）；`_step()` 以下的真实状态机
（`_update_state` → `resolve_successors` → `_make_decision` → 各 handler）
确实一次都没跑过。

补测策略：**只 mock 截图**，保留全部逻辑。`CombatRecognizer` 换成按脚本
返回 phase 的 `ScriptedRecognizer`，同时记录每步收到的候选列表与门控参数；
转移表解析、handler 派发、节点计数、事件历史全部真跑。

锁住三个此前无人守护的不变量：

1. **转移合法性** —— 第 k 步认出的 phase 必须在第 k 步的候选集里。
   转移表删边/改指向立即失败。
2. **`allow_event_overlay_anchor` 传值** —— `START_FIGHT` 阶段必须传 `False`
   （2026-08 出征瞬间误判回港事故的防线）。此前只锁了 recognizer 的**签名**，
   没锁引擎**实际传了什么**。
3. **`FIGHT_PERIOD` 等待期不盲点** —— 该阶段 poll_action 必须是纯 sleep。
   战斗动画期盲点曾把夜战整段点穿（2026-08）。

变异验证 3 个全被捕获：删 `FIGHT_PERIOD→RESULT` 转移边 / 移除
`allow_event_overlay_anchor` 门控 / 等待期改为盲点。

**踩过的坑**（写这类测试时容易再踩）：

- `CombatResult.history` 是 `CombatHistory`，取 `.events`；它**不可迭代**
- `ctx.ctrl.screenshot()` 必须返回**合法 numpy 图像** —— 索敌处理器会对其跑
  `cv2.cvtColor`，返回 MagicMock 直接 `cv2.error`
- `CombatEngine` 状态（`_history`/`_phase`/`_steps`）**不可跨用例复用**，
  复用会污染后续用例（表现为断言时好时坏）
- 规则引擎的 `enemy_rules` 条件不能依赖敌舰编成：脚本化环境下截图是纯黑图，
  识别结果为 `{'ALL': 0}`，`CV >= 1` 之类条件永远不成立且**不报错**，
  只表现为「规则没触发」—— 很隐蔽

---

## T3-3 真实截图 fixture 回归体系

### 问题证据

`testing/` 有 14 个子目录、1335 项测试，但识别层**没有一张真实截图 fixture**：

```
testing/ops/ 下 11 个文件不以 test_ 开头 → pytest 不收集，实际零覆盖
conftest.py 全文只有 2 个 fixture，零 mock
testing/ui/_framework.py:258 必须连真机
combat/engine.py::fight() 主循环零覆盖
```

而**识别恰恰是最脆弱、实机反馈周期以天计**的部分。当前每次改模板/阈值都只能靠实机跑一轮验证，回归全靠记忆。

`combat/README.md:46-61` 整节「特别测试（未做）」：旗舰大破回港、远程打击、中破规则回港、敌方阵容不匹配、SL 路径——**全部未做**。

### 方案

**① 先修 pytest 收集问题** ✅ 已完成 2026-10-11（**原结论有误，已修正**）

~~把 `testing/ops/` 下非 `test_` 开头的 11 个文件重命名，或加 `conftest.py` 的  
`python_files` 配置。~~

> **实施时实测发现原结论是错的：**
>
> - 这些文件里 `test_` 定义数为 **0**，全部带 `if __name__ == '__main__'`
> - 它们是需要**真实模拟器**的交互式脚本（连接设备 → 真实点击 → 阻塞）
> - `pytest testing/ops/` 收集到 148 项，**全部来自 10 个 `test_*.py`**，  
>   与这些脚本无关
>
> 若按原计划重命名，pytest 会收集它们 → 跑全量时尝试连接不存在的设备  
> → 一片失败。**是把问题引入，不是修复。**

**实际处置：标注 + 保护，而非改名**

| 项                              | 内容                                         |
| ------------------------------ | ------------------------------------------ |
| `testing/ops/README.md`        | 说明脚本性质、用法、维护约定                             |
| 13 个脚本 docstring               | 首行统一标注「实机验证脚本 — 非 pytest 测试」               |
| `testing/conftest.py`          | 新增 `pytest_collect_file` 钩子，误改名立即报错并说明正确做法 |
| `test_manual_scripts_guard.py` | 57 项，锁住上述机制                                |

变异测试：把保护条件改成 `if False` → `test_guard_rejects_renamed_script` 失败。

**教训**：「文件未被 pytest 收集」≠「测试有问题」。判断依据是**内容**  
（有无 `test_` 函数、是否需要真机、能否 mock），不是文件名。  
只看命名就下结论会把好代码改坏。

**② 建截图 fixture 集** ⏳ **待实机采集（2026-10-12）**

采集脚本已就绪：`tools/diagnostics/capture_fixtures.py`。挂到  
`CombatRecognizer.wait_for_phase` 上，在**每个状态被识别成功的瞬间**截图，  
并写 `manifest.jsonl`（文件名/模式/phase/置信度/分辨率）。

> **2026-10-11 未能采集**：当日掉落已达游戏上限 **500 艘舰船**，  
> 不再有舰船掉落 → `GET_SHIP` 页不出现。
>
> **已实测确认这不会导致卡死**：`GET_SHIP` 在转移表里只是**候选之一**  
> （`state.py:234` → `t[RESULT] = list(after_result)`，含 `PROCEED` /  
> `MAP_PAGE` / `GET_SHIP` / `FLAGSHIP_SEVERE_DAMAGE`），掉落满时页面跳过，  
> 引擎认 `PROCEED` 继续。满 500 时关卡选择页会弹一条横幅  
> （「今日已打捞获得500次舰船…」）但**消失很快，无需处理**。

**三个场景必须分开采**（用户指出普通战/活动战、夜战点/普通点确有差异，  
代码核实属实）：

| mode       | 计划                           | 覆盖路径         | 终态               |
| ---------- | ---------------------------- | ------------ | ---------------- |
| `normal`   | `5-2炸鱼.yaml`（`night: False`） | 常规战·**昼战**   | `MAP_PAGE`       |
| `event`    | `H5ACD夜战.yaml`               | 活动战·**夜战**   | `EVENT_MAP_PAGE` |
| `campaign` | `5-2炸鱼.yaml`                 | 战役 SINGLE 模式 | 与 MAP 不同         |


特别注意：H5 三点**进入即夜战、无追击选项**，故 `NIGHT_PROMPT` 在活动战  
场景下**不会出现**，不能指望从 `event` 采集里拿到。

```bash
cd C:\Users\13930\Desktop\AutoWSGR
.venv\Scripts\python.exe tools\diagnostics\capture_fixtures.py --mode normal   --rounds 2
.venv\Scripts\python.exe tools\diagnostics\capture_fixtures.py --mode event    --rounds 3
.venv\Scripts\python.exe tools\diagnostics\capture_fixtures.py --mode campaign --rounds 2
```

采完后的三步：① 人工过图确认 phase 标注（采集器只记录**引擎的判断**，  
引擎误判会跟着错）② 移入 `testing/fixtures/screenshots/<mode>/`  
③ 补断言纳入 pytest。

**存放位置**：

```
testing/fixtures/screenshots/
├── normal/                # 常规战·昼战
│   ├── normal_proceed_01.png
│   ├── normal_result_01.png
│   └── manifest.jsonl
├── event/                 # 活动战·夜战
└── campaign/              # 战役
```

采集器直接输出 `manifest.jsonl`（每行一条记录），形如：

```json
{"filename": "event_result_01.png", "mode": "event", "phase": "RESULT",
 "resolution": [960, 540], "confidence": 0.91,
 "note": "candidates=['EXP_SETTLEMENT','PROCEED',...]"}
```

人工确认后可转成断言：

```python
@pytest.mark.parametrize('case', load_manifest(), ids=lambda c: c['filename'])
def test_recognizer(case, screen):
    """识别签名回归: 每张 fixture 图必须得到 manifest 里声明的结果。"""
    result = CombatRecognizer.identify(screen, [case['phase']])
    assert result is not None, f"{case['filename']} 应识别为 {case['phase']}"
```

**③ 补 `fight()` 主循环测试**

复用本次熔断测试的模式（mock `_step`），覆盖正常流程的各终态。

### 风险

低。fixture 是纯新增，不影响现有代码。

### 验收标准

- [x] `pytest testing/` 计数上升至**1638 passed / 25 skipped**  
  （原1335 → T2-1 1356 → 今日 1638）
- [x] 每个 fixture 图都有 manifest 条目，测试全绿  
  → `testing/fixtures/screenshots/combat/` + `test_combat_fixtures.py` 19 项
- [x] 故意改坏一个阈值，fixture 测试必须失败（变异验证 3 个全被捕获）  
  → RESULT 置信度 0.85→0.999 挂 / PROCEED 签名张冠李戴挂 /  
  移除 `allow_event_overlay_anchor` 开关挂

### 仍缺（未做）

- [ ] `day_battle` 昼战 fixture —— 用户反馈流程复杂未截；下次跑常规战时补  
  （H5 活动战是夜战，没有昼战场景）
- [ ] 旗舰大破 fixture —— 触发概率低
- [ ] `get_ship` 舰船掉落页 —— **用户已魔改为快速点击跳过，流程不再经过，  
  该 fixture 无意义，不要再尝试采**
- [ ] `fight()` 主循环端到端测试 —— 见 T3-2 验收第 3 条

**已知的采集器陷阱**（2026-10-12 实测踩坑）：

1. **验证 fixture 必须用 `PHASE_SIGNATURES`**（`combat/recognizer.py`），  
   **不是** `build_transitions()` 的返回 —— 后者只有后继列表没有签名，  
   拿它验证会得到「取到签名: []」的空结果而误判为全 MISS。
2. **`battle_prep`（出征准备页）会命中 `EVENT_MAP_PAGE`** —— 这不是 bug，  
   它正是 2026-10-08「出征瞬间误判回港」事故的根因，  
   `allow_event_overlay_anchor` 门控是唯一防线。已有断言锁死。
3. **`START_FIGHT` 匹配失败不是缺陷** —— 签名 `template_key=None`，  
   注释明写「过渡态，不直接用模板匹配」，靠转移表时序推进。
4. **采集器必须在同一帧上打标签** —— 原来用 `identify_current_page(ctx)`  
   会**自己另截一张图**，导致落盘的图与标签不是同一帧，动画页标签必然错位。  
   已改用 `get_current_page(screen)`。
5. **连拍必须去重** —— 0.4s × 480s 会产出上千张近乎相同的图。  
   按缩略图（64x36）**平均绝对差**判重（不要用 maxdiff，单像素变化会被  
   `INTER_AREA` 量化抹平），静止页只留 2 张。

---

## 优先级与排期建议

> 2026-10-12 更新：下表为**原始排期建议**，实际执行顺序有调整。  
> 已完成：T2-1 → T3-3① → T2-2 → T2-3 → T3-2 → T3-1（比建议顺序更快，  
> 因为 T3-2 的部分内容与 T2-3 解耦）。T3-3② 卡在需要用户手动截昼战。

| 阶段     | 项目                          | 预估工作量 | 风险 | 依赖             | 实际状态                         |
| ------ | --------------------------- | ----- | -- | -------------- | ---------------------------- |
| **立即** | T3-3 ① 标注+保护 ops 实机脚本       | ✅ 完成  | 无  | 无              | ✅ 2026-10-11                 |
| **立即** | T2-1 调度器熔断                  | 2~3h  | 中  | 无              | ✅ 2026-10-11                 |
| **本周** | T2-2 `_overlay_ready` ctx 化 | 1h    | 低  | 无              | ✅ 2026-10-11                 |
| **本周** | T3-3 ② 截图 fixture           | 4~6h  | 低  | 需采集截图          | 🟡 5/7 张                     |
| **下周** | T2-3 活动期号参数化                | 4~6h  | 中  | 无              | ✅ 2026-10-11                 |
| **下周** | T3-2 依赖注入                   | 1~2天  | 中  | T2-3           | ⚠️ 依赖已解，仅差fight() 覆盖率        |
| **排期** | T3-1 拆分 ScrcpyController    | 2~3天  | 高  | 需整块时间 + 实机回归窗口 | ⚠️ 3/4 完成，差 `mark_failed` 自愈 |

**剩余工作的建议顺序**（2026-10-12）：

1. **卡死检测（`stuck_timer`）** —— 最该做。熔断只在「失败返回」时触发，  
   真卡死是永不返回，熔断不会被触发。这是 7×24 挂机最大的盲区。
2. 昼战 fixture 补采（低优先，等常规战时顺手做）
3. 异常→恢复映射表（收益明确但改动面大）

---

## 已完成

### 第一梯队（2026-10-11）

| 项        | 内容                                                            | 提交        |
| -------- | ------------------------------------------------------------- | --------- |
| P0-3     | 三个核心模块 + `bin/` + `plans/` + `run_h5_acd.py` 纳入 git           | `b7205eb` |
| P0-1     | 战斗引擎熔断护栏（`max_recovery_attempts` + `max_steps`），11 项单测，变异测试验证 | `7019087` |
| P2-4     | 清理 7 处死代码                                                     | `11cfe36` |
| P2-5     | 7 个诊断脚本收进 `tools/diagnostics/`，消除硬编码路径，修复无效验证                 | `8a0e0c8` |
| **T2-1** | **调度器故障恢复与熔断**（阈值 5，`--max-failures` 可调），21 项单测               | 见 git log |

### 第二 / 三梯队（2026-10-11 ~ 2026-10-12）

| 项         | 内容                                                                                | 提交                            |
| --------- | --------------------------------------------------------------------------------- | ----------------------------- |
| **T3-3①** | ops 实机脚本标注 + `pytest_collect_file` 防误改保护，57 项回归                                   | 见 git log                     |
| **T2-2**  | `_overlay_ready` 类变量 → `GameContext.overlay_ready_maps`                           | 见 git log                     |
| **T2-3**  | 活动期号参数化，新增 `data/event/*.yaml` + `data/event_config.py`，23 项单测                    | 见 git log                     |
| **T3-2**  | 消除 `combat → ops/ui/cv2/PIL` 逆向依赖（下沉到 `vision/` 与 `data/`），AST 静态 + 子进程运行时双重检查    | 见 git log                     |
| **T3-1**  | `ScrcpyController` 939→565 行，拆出 `adb_conn` / `video_stream` / `touch` 三模块，59 项新测试 | `1af9ecf` `89a4259` `8906a13` |
| **缺陷修复**  | 常驻 adb shell 通道恒不可用 → swipe 延迟 250ms→0ms                                          | `ec44166`                     |
| **T3-3②** | 5 张实机战斗页 fixture + 19 项断言 + 采集脚本时序修复                                              | `ff22a69`                     |

全量测试：**1638 passed / 25 skipped**（第一梯队前1335→ T2-1 后 1356 → 今日 1638）。
