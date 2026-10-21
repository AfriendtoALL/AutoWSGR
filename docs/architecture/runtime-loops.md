# AutoWSGR 运行循环总览

> 返回 [架构概述](README.md)
>
> 本文按「启动 → 调度 → 单场战斗 → 补给 → 日常任务 → 故障恢复」的顺序，
> 梳理代码里真实存在的循环。和旧文档不一致之处以本文和源码为准。

---

## 1. 分层与目录

```
scheduler / server      调度：什么时候做什么（TaskScheduler / Trigger / TaskManager）
        │
        ▼
ops                     编排：一次完整流程（NormalFightRunner / CampaignRunner / ...）
        │
        ▼
combat                  战斗状态机（CombatEngine / PhaseHandlers / CombatPlan）
        │
        ▼
ui                      页面识别 + 页面内原子操作 + BFS 导航
        │
        ▼
vision                  模板匹配 / 像素签名 / OCR
        │
        ▼
emulator                截图 + 触控（ScrcpyController）
        │
        ▼
infra                   配置 / 日志 / 异常 / 类型
```

跨层共享状态统一放在 `GameContext`（`autowsgr/context/game_context.py`），
不依赖全局变量。

| 层 | 目录 | 关键文件 |
|---|---|---|
| 调度 | `autowsgr/scheduler/` | `launcher.py`、`scheduler.py`、`triggers.py`、`daily_plan.py`、`plan_runner.py` |
| 操作 | `autowsgr/ops/` | `normal_fight.py`、`campaign.py`、`exercise.py`、`expedition.py`、`repair.py`、`reward.py`、`startup.py` |
| 战斗 | `autowsgr/combat/` | `engine.py`、`state.py`、`handlers.py`、`recognizer.py`、`plan.py`、`node_tracker.py` |
| 页面 | `autowsgr/ui/` | `navigation.py`、`page.py`、`map/`、`battle/`、`mission_page/`、`bath_page/` |
| 上下文 | `autowsgr/context/` | `game_context.py`、`bathroom.py`、`expedition.py`、`fleet.py` |
| 配置 | `autowsgr/infra/config.py` | `UserConfig` / `DailyAutomationConfig` |

---

## 2. 启动链

入口：`autowsgr/scheduler/launcher.py::launch()` / `Launcher`

```
ConfigManager.load()          YAML → Pydantic UserConfig
        ↓
setup_logger() + 卡死看门狗
        ↓
ScrcpyController.connect()    ADB + scrcpy
        ↓
EasyOCREngine.create()        通用 OCR（可选增强船只 OCR）
        ↓
GameContext(ctrl, config, ocr, ship_ocr)
        ↓
ensure_game_ready(ctx)        游戏未运行则冷启动；已在运行则回主页
        ↓
handle_daily_overlays()       每日首次处理新闻/签到/活动预约浮层
```

`launch()` 返回后，游戏位于主页面，后续所有循环都围绕这个 `ctx` 运行。

---

## 3. 三种运行模式

### 3.1 一次性脚本（examples 常用）

```python
ctx = launch('usersettings.yaml')
results = run_yaml_plan(ctx, '9-2速刷胖次', times=10, fleet_id=3)
```

`run_yaml_plan()`（`scheduler/plan_runner.py`）并不是直接循环调用 Runner，
而是：

1. 加载 YAML 计划 → `CombatPlan`
2. 构造 `NormalFightPlan` + `NormalFightTrigger`
3. 创建 `TaskScheduler(expedition_interval=0)` 并注册触发器
4. 启动一个看门狗线程：触发器耗尽（达标/船坞满）后置 `ctx.stop_event`
5. 调用 `scheduler.run_daily()`，由常驻调度循环执行，达标后自动退出

好处：一次性脚本也复用了日常挂机的计数、战果条件、熔断与恢复逻辑。

### 3.2 全天挂机（auto_daily）

```python
ctx = launch('usersettings.yaml')
scheduler = TaskScheduler(ctx, expedition_interval=0)  # 远征交给触发器
build_daily_plan(scheduler, ctx)                        # 读 daily_automation
scheduler.run_daily()                                   # 常驻循环
```

### 3.3 HTTP 服务

`autowsgr/server/task_manager.py` 的 `TaskManager`：

- 同一时刻只允许一个运行中任务
- 战斗在后台线程执行（`threading.Thread`），不阻塞 FastAPI 事件循环
- 通过 `ws_manager` 推送进度/完成状态
- 执行器内部仍然调用同一批 `ops` Runner

---

## 4. 调度主循环（run_daily）

文件：`autowsgr/scheduler/scheduler.py`

```text
while not ctx.stop_event.is_set():
    1. _check_daily_reset()
       跨日（本地 0 点）→ 所有 trigger.reset()，清零每日计数器

    2. for trigger in triggers:
           task = trigger.should_fire(ctx)
           命中则 _enqueue(task)          # 按 priority 插入优先队列

    3. task = _dequeue()
       if task:
           results = _run_task(task)      # 内部按 task.times 循环
           连续失败统计 → _handle_failure()
       else:
           _ensure_main_page_clean()      # 每日浮层检查
           time.sleep(idle_sleep)         # 挂机等待

退出：stop_event 被置位（Ctrl+C / 看门狗 / Server stop_task）
```

要点：

- **优先级队列**：`priority` 数值越小越先执行；同优先级 FIFO。
- **触发器一次只产出一个任务**：`_idle` 标志防止队列堆积。
- **`_run_task` 统一包装 Runner**：`BatchRunnerAdapter` 把
  `run() -> CombatResult` 和 `run() -> list[CombatResult]` 统一成列表。
- **轮次计数**：`DOCK_FULL + dock_full_destroyed`（船坞满但已解装，未真正开打）
  不占次数，立即重试；船坞满且未解装则跳过该任务剩余次数。
- **`on_done` 回调**：每场结果回传给触发器，用于计数、耗尽判断、空转检测。

### 旧接口 `run()`

`TaskScheduler.run()` 是顺序执行预提交的 `FightTask` 后退出，保留给简单脚本；
`run_daily()` 才是触发器驱动的常驻循环。`run_yaml_plan` 和 `auto_daily`
都走 `run_daily()`。

---

## 5. 触发器模型

文件：`autowsgr/scheduler/triggers.py`、`daily_plan.py`

### 5.1 基类

```text
Trigger
├── should_fire(ctx) -> FightTask | None   是否产出任务
├── reset()                                跨日/跨时段重置
└── _build_task(ctx, on_done)              用 task_factory 构造 FightTask

ExhaustibleTrigger(Trigger)                 返回标志驱动的可耗尽触发器
├── CampaignTrigger      BATTLE_TIMES_EXCEED → 本日战役打满
└── ExerciseTrigger      SKIP_FIGHT         → 本时段演习打满

TimerTrigger(Trigger)                       到间隔产出一次
├── ExpeditionTrigger    远征（永不耗尽）
└── 任务奖励 / 浴室修理（daily_plan 中直接用 TimerTrigger）

NormalFightTrigger(Trigger)                 多 plan + 全局上限 + 空闲填充
```

### 5.2 优先级（daily_plan.py）

| 优先级 | 任务 | 触发方式 | 停止/耗尽条件 |
|---:|---|---|---|
| 0 | 远征 | 定时（默认 600s） | 永不耗尽 |
| 1 | 任务奖励 | 定时（同远征周期） | 永不耗尽 |
| 5 | 战役 | 条件 | `BATTLE_TIMES_EXCEED` |
| 10 | 演习 | 条件 | `SKIP_FIGHT`（无可挑战对手） |
| 100 | 常规战 / 活动战 | 条件 + 空闲填充 | 所有 plan 达标 / 船坞满 / 快修上限 |
| 200 | 浴室修理 | 定时 | 只在没有战斗任务时执行（空闲填充） |

### 5.3 常规战触发器细节

`NormalFightTrigger` 持有 `list[NormalFightPlan]`：

- `target is not None`：优先打未完成的有限计划。
- `target is None`：无限计划轮询（round-robin），作为空闲填充。
- 每场结束 `_on_done`：
  - 记录结果到 `plan.results`
  - `_DONE_FLAGS`（`OPERATION_SUCCESS` / `FIGHT_END` / `SL`）才计数
  - 配了 `grade` 条件的计划还要求战果全部达标才计数
  - 连续 `SKIP_FIGHT`（白名单撤退）达到 `IDLE_LOOP_THRESHOLD`（10）→
    停用该 plan，并给出「疑似编队不满足带路条件」的诊断
  - `DOCK_FULL` 且未解装 → 会话级停止常规战

### 5.4 跨日 / 跨时段

- `TaskScheduler._check_daily_reset()`：本地日期变化 → 所有 trigger `reset()`。
- `ExerciseTrigger` 额外在 `should_fire` 内检查 0/12/18 三个时段，跨时段
  自动清空 `_exhausted`。
- `reset()` **不会**清除会话级状态：`NormalFightPlan.disabled_reason`、
  `NormalFightTrigger._dock_full_stopped`。

---

## 6. 单场战斗循环

### 6.1 Ops 层：一次出征

以常规战为例（`ops/normal_fight.py::NormalFightRunner.run`）：

```text
run()
 ├─ 1. _enter_fight()
 │     常规：goto_page(MAP) → 读出征面板今日获取数 → enter_sortie(chapter, map)
 │     活动：已在准备页 → 直接打；关卡详情弹层在屏 → 点一次出击准备；
 │           否则 goto_page(EVENT_MAP) → 选难度/节点 → 出击
 │
 ├─ 2. _prepare_for_battle()
 │     select_fleet(fleet_id)
 │     change_fleet(...)        # 仅配置了 fleet / fleet_rules 时
 │     apply_supply()           # → 见第 7 节
 │     start_battle()
 │
 ├─ 3. ctx.sync_before_combat()
 │
 ├─ 4. _do_combat() → CombatEngine.fight(plan)
 │
 ├─ 5. result.fleet / loot_count 回填
 │     ctx.sync_after_combat()
 │
 └─ 6. _handle_result()
       DOCK_FULL → 按 dock_full_mode 解装/强化
       其他 flag → 记录
```

`CampaignRunner` / `ExerciseRunner` 结构相同，但：

- 战役：`MapPage.enter_campaign()`，**不做补给**，只处理战役支援开关。
- 演习：`MapPage` 演习面板 → 选对手 → 准备页 `select_fleet` → 出征，
  **不做补给**。
- 活动战：`EventFightRunner` 是 `NormalFightRunner` 的兼容薄包装，
  `chapter` 为 E/H 时自动走活动地图。

### 6.2 Combat 层：状态机主循环

文件：`combat/engine.py`

```text
fight(plan, ship_stats)
 ├─ 选择节点追踪器（NORMAL/EVENT 加载地图数据；其他模式无）
 ├─ _reset()
 └─ while True:
        _steps += 1
        try:
            phase = _update_state()
                ├─ resolve_successors(transitions, 当前 phase, 上一动作)
                ├─ 构造 poll_action（加速点击 / 节点追踪 / 资源弹窗）
                └─ recognizer.wait_for_phase(candidates, poll_action)
            flag = _make_decision(phase)
                ├─ 按 _PHASE_HANDLERS 派发 _handle_xxx()
                └─ 到达 plan.end_phase → 记 AUTO_RETURN → FIGHT_END
        except 识别超时:
            恢复次数 +1
            超过 max_recovery_attempts → SL
            否则 _try_recovery()：识别终态/后继/结算页，成功则继续
        except 停止请求:
            FIGHT_END

        FIGHT_CONTINUE → 继续
        DOCK_FULL / SL / SKIP_FIGHT / FIGHT_END → 退出循环
```

注意：

- **步数上限已移除**。战斗长度由节点数决定，`_steps` 只用于日志/诊断。
- 防止死循环靠三处：
  1. 识别超时 + 恢复次数上限 → `SL`
  2. 各阶段独立超时（战斗 30s、战果 90s、夜战 150s 等）
  3. 跨场「连续白名单撤退」检测（`NormalFightTrigger._track_idle_loop`）

### 6.3 状态与转移

文件：`combat/state.py`

两类模式：

| 大类 | 模式 | 结束页 |
|---|---|---|
| `MAP` | 常规战 `normal` | `MAP_PAGE` |
| `MAP` | 活动战 `event` | `EVENT_MAP_PAGE` |
| `SINGLE` | 战役 `battle` | `RESULT`（`end_page=None`） |
| `SINGLE` | 演习 `exercise` | `EXERCISE_PAGE` |
| `SINGLE` | 决战 `decisive` | `RESULT` |

MAP 类典型流程：

```text
START_FIGHT → FIGHT_CONDITION → SPOT_ENEMY_SUCCESS → FORMATION
    → FIGHT_PERIOD → NIGHT_PROMPT → RESULT → [EXP_SETTLEMENT] → GET_SHIP
    → PROCEED → 下一节点 / 回港
```

- **快速模式**（默认）：不采集战果，`RESULT` 识别后
  `_blind_skip_settlement()` 在安全位连续点击，穿行战果/经验/掉落三层。
- **慢速模式**：计划配置了 `grade` 条件时自动启用，停留采集评级/MVP，
  经验页进入状态机逐页推进。

### 6.4 阶段处理器

文件：`combat/handlers.py`

| 阶段 | 决策 |
|---|---|
| `FIGHT_CONDITION` | 按计划点击战况 |
| `SPOT_ENEMY_SUCCESS` | 白名单检查 → 规则（迂回/撤退/阵型）→ 导弹支援 → 开战 |
| `FORMATION` | 规则阵型 / 默认阵型；索敌失败或迂回失败按配置 SL |
| `FIGHT_PERIOD` | 需要卡点时 SL，否则等待 |
| `NIGHT_PROMPT` | 按 `night` 决定追击/撤退 |
| `RESULT` | 采集或盲点穿行 |
| `GET_SHIP` | 记录掉落（当前不识别舰名），点击继续 |
| `PROCEED` | 按节点 `proceed` 决定前进/回港 |
| `FLAGSHIP_SEVERE_DAMAGE` | 强制回港 |
| `DOCK_FULL` | 返回 `DOCK_FULL` 交给 ops 处理 |

节点决策来自 YAML（`combat/plan.py`）：

```yaml
chapter: 9
map: 2
selected_nodes: [A, E, D, G, H, M, O]
repair_mode: 2
node_defaults:
  formation: 4
  night: false
  proceed: true
  SL_when_detour_fails: true
node_args:
  O:
    enemy_rules:
      - [AP != 1, retreat]
    proceed: false
```

---

## 7. 补给循环

补给不是独立调度任务，而是**每次出征准备时执行一次**，位于：

- `ops/normal_fight.py::_prepare_for_battle()` → `page.apply_supply()`
- 实现：`ui/battle/supply.py::SupplyMixin`

### 7.1 配置

`UserConfig.supply_mode`：

- `'auto'`：勾选游戏内「自动补给」复选框，由游戏在出击时补给。
- `'script'`（默认）：先取消「自动补给」勾选，再由脚本按需补给。

### 7.2 每轮流程

```text
apply_supply()
 ├─ 读取 supply_mode
 ├─ 校正复选框状态（幂等）
 │    auto   → ensure_auto_supply()
 │    script → disable_auto_supply()
 │
 ├─ has_empty_ammo_ship()
 │    对准备页截图做弹药格颜色检测，最多重试 3 次
 │
 ├─ 结果三分支：
 │    False（所有船尚有弹药） → 跳过补给
 │    None （检测不到子弹行） → 跳过补给（宁可漏补不可误补）
 │    True （至少一艘船 5 格全空）→
 │         auto   → 交给游戏自动补给
 │         script → 打开「快速补给」→ 点击「全部补给」
```

关键设计：

- 触发条件是「**任一舰船弹药全空**」，不是全队全空。
- 检测失败时**不补**，避免误补浪费燃料。
- 两种模式每轮都校正复选框，因为它是游戏内持久状态，不随页面切换重置。
- 只在常规战/活动战准备流程调用；战役和演习当前不调用 `apply_supply()`。

---

## 8. 日常任务循环

`build_daily_plan()` 读取 `ctx.config.daily_automation`，按开关注册触发器。

| 任务 | 调度方式 | 一次执行内容 | 结束条件 |
|---|---|---|---|
| 远征收取 | `ExpeditionTrigger`，定时 | 回主页 → 检测远征通知 → 地图远征面板 → 逐个收取 | 无就绪槽位 |
| 任务奖励 | `TimerTrigger` | 回主页 → 检测任务红点 → 任务页 → 领取 | 无奖励可领 |
| 战役 | `CampaignTrigger`，条件 | `CampaignRunner(times=1)` 打一场 | `BATTLE_TIMES_EXCEED` |
| 演习 | `ExerciseTrigger`，条件 | `ExerciseOnceRunner` 挑战一个对手 | 无对手 `SKIP_FIGHT` |
| 常规战/活动 | `NormalFightTrigger`，空闲填充 | 每个 plan 执行一次 `NormalFightRunner.run()` | 达标 / 船坞满 / 快修上限 |
| 浴室修理 | `TimerTrigger` + 优先级 200 | 有空槽则导航浴室，循环派修直到无空槽/无候选 | 无空槽或无候选 |

### 8.1 远征收取

`ops/expedition.py::collect_expedition()`：

```text
recover_to_main_or_restart()
goto_page(MAIN)
if 主页无远征通知: return False
goto_page(MAP)
if 地图无远征通知: 回主页 return False
switch_panel(EXPEDITION)
collect_expedition()   # 逐个点击就绪槽位 → 跳过动画 → 确认 → 回地图
goto_page(MAIN)
```

注意：当前实现只**收取已完成远征**，不会重新派遣新远征。
配置名 `auto_expedition` / 示例注释里的「自动重新派遣」与实现不完全一致。

### 8.2 任务奖励

`ops/reward.py::collect_rewards()`：

```text
goto_page(MAIN)
MainPage.has_task_ready() → False 则返回
goto_page(MISSION)
MissionPage.collect_rewards()
goto_page(MAIN)
```

### 8.3 浴室修理

`ops/repair.py::repair_one_available()`：

```text
if ctx.bathroom 无空闲槽: return False
goto_page(BATH)
while bath.is_available():
    打开「选择修理」overlay
    repair_longest(blacklist)
    secs > 0  → bath.occupy(secs) 记录释放时间，继续
    secs == -2 → 浴场满，mark_unknown，退出
    secs <= 0 → 无可修候选，退出
goto_page(MAIN)
```

`BathRoom.available_time` 只跟踪**本脚本派出的修理**，不逐槽扫描游戏画面。

### 8.4 战役 / 演习

- 战役：`CampaignRunner.run()` 循环 `times` 次；准备页处理战役支援开关，
  出征后等待离开准备页，两次未离开判定次数用尽。
- 演习：`ExerciseOnceRunner.run()` 每次只挑战一个可用对手；
  无对手返回 `SKIP_FIGHT`，由 `ExerciseTrigger` 判定本时段打满。

---

## 9. 结果处理与故障恢复

### 9.1 战斗结果标志

`autowsgr/types.py::ConditionFlag`

| 标志 | 含义 | 上层处理 |
|---|---|---|
| `OPERATION_SUCCESS` | 正常打完 | 触发器计数 |
| `FIGHT_END` | 收到停止请求等 | 触发器按完成计 |
| `DOCK_FULL` | 船坞满 | `dock_full_mode` 解装/强化 |
| `BATTLE_TIMES_EXCEED` | 战役次数用尽 | 战役触发器耗尽 |
| `SKIP_FIGHT` | 主动撤退/跳过 | 常规战不计数；演习视为打满 |
| `SL` | 需要/执行了 SL | 触发器按完成计，但引擎侧算失败标志 |
| `ACTION_FAILED` | 子任务异常 | 不计数，交给熔断 |

### 9.2 船坞满

`NormalFightRunner._handle_result()` → `_handle_dock_full()`：

- `dock_full_mode`：0 关闭 / 1 解装 / 2 强化 / 3 先强化后解装。
- 解装成功：保持 `DOCK_FULL`，但置 `dock_full_destroyed=True`，
  调度器识别为「未开打、可重试」，不占次数。
- 解装失败：`dock_full_destroyed=False`，调度器跳过该任务剩余次数；
  常规战触发器则会话级停止。

### 9.3 战斗内恢复

- 识别超时 → `_try_recovery()`：
  - 识别终态页
  - 识别当前状态的后继
  - MAP 模式额外尝试结算三层页面
- 连续恢复超过 `max_recovery_attempts`（默认 3）→ `SL`
- `SL` 的恢复动作由 ops 注入：`restart_game(ctx.ctrl)`

### 9.4 调度层熔断

`TaskScheduler.run_daily()`：

```text
本轮失败？
 ├─ crashed（_run_task 自身异常）
 └─ _is_failed_round(results)：
      无结果，或全部结果为 ACTION_FAILED / SL
      （DOCK_FULL 不算失败；部分成功不算失败）

连续失败计数 +1
 ├─ 设备可用？
 │    recover_device() 分级：页面 → adb/视频流 → 模拟器
 ├─ 未达阈值 → 回主页 + 指数退避
 └─ 达阈值（默认 5）→ 冷却 60s 后 restart_game()
```

设计要点：

- **先判设备再判游戏**：adb 不通时重启游戏必然失败。
- 失败轮之间指数退避，避免「0.1 秒跑完 5 次失败」的高速空转。
- 重启后连续失败计数归零。

### 9.5 空转检测

`NormalFightTrigger._track_idle_loop()`：

- 连续 10 场 `SKIP_FIGHT` 且零成功 → 停用该 plan。
- 典型场景：计划要求白名单 `[C]`，但编队不满足带路条件，
  实际总进 `B` 点 → 撤退 → 再出征 → 无限循环。
- 停用是会话级的，跨日不恢复，需要人工修正配置后重启。

---

## 10. 上下文与计数器

`GameContext` 中与循环相关的字段：

| 字段 | 作用 |
|---|---|
| `stop_event` | 停止信号（调度器、战斗识别、看门狗共用） |
| `ui_stack` | 页面来路栈，导航候选集与 `go_back` 用 |
| `overlay_ready_maps` | 活动关卡弹层可复用的身份集合 |
| `bathroom` | 浴室槽位释放时间 |
| `expeditions` | 远征槽位模型（当前主要由 UI 收取，不参与调度） |
| `dropped_ship_count` / `dropped_loot_count` | 每日掉落统计（信息用途） |
| `quick_repair_used` | 快修计数（当前代码没有自增点） |
| `active_fight_tasks` | 当前战斗任务数 |
| `daily_overlay_date` | 每日浮层处理时间戳 gate |

`sync_before_combat()` / `sync_after_combat()` 在每场战斗前后同步舰队、血量、
今日获取数量等信息。

> 注意：`quick_repair_limit` 的判断读 `ctx.quick_repair_used`，但当前代码库
> 没有给它自增的位置，因此该上限实际不生效（除非外部手动修改）。
> `dropped_ship_count` / `dropped_loot_count` 也已不再用于门控常规战。

---

## 11. 页面导航循环

`ops/navigate.py::goto_page()`：

```text
for step in range(20):
    current = identify_current_page(ctx, candidates)
    ctx.ui_stack.observe(current)
    if current == target: return
    path = find_path(current, target)      # BFS，图定义在 ui/navigation.py
    edge = path[0]
    ctx.ui_stack.push(edge.target)
    edge.action(ctx)                       # 页面控制器执行导航
    candidates = ctx.ui_stack.candidates(target)
失败 → ui_stack.resync() → 再试一次
```

导航图只包含一级页面之间的边；`BATTLE_PREP` / `CHOOSE_SHIP` 是战斗域中转页，
不入图，由各 Runner 显式进入。

---

## 12. 改代码时的定位表

| 想改什么 | 看哪里 |
|---|---|
| 启动流程 / 模拟器连接 | `scheduler/launcher.py` |
| 日常任务开关 / 顺序 | `infra/config.py::DailyAutomationConfig`、`scheduler/daily_plan.py` |
| 任务何时触发 / 何时停 | `scheduler/triggers.py` |
| 一次出征的页面流程 | `ops/normal_fight.py`、`ops/campaign.py`、`ops/exercise.py` |
| 战斗阶段的判断与点击 | `combat/handlers.py`、`combat/actions.py` |
| 战斗状态流转 | `combat/state.py`、`combat/plan.py` |
| 战斗画面识别 | `combat/recognizer.py`、`combat/recognition.py` |
| 节点追踪 / 白名单 | `combat/node_tracker.py`、YAML `selected_nodes` |
| 补给规则 | `ui/battle/supply.py`、`ui/battle/ammo.py` |
| 修理派单 | `ops/repair.py`、`context/bathroom.py`、`ui/bath_page/` |
| 远征 / 任务奖励 | `ops/expedition.py`、`ops/reward.py`、`ui/map/panels/expedition.py`、`ui/mission_page/` |
| 导航图 / BFS | `ui/navigation.py`、`ops/navigate.py` |
| 失败恢复 | `scheduler/recovery.py`、`scheduler/scheduler.py`、`ops/startup.py` |

---

## 13. 已知的文档/实现差异

1. `docs/architecture/README.md` 里的目录树仍写着 `ops/supply.py`、
   `ui/utils.py`、`data/missions.yaml` 等旧路径；实际补给在
   `ui/battle/supply.py`，`ui/utils/` 是包，任务数据在 `ui/mission_page/`。
2. `auto_expedition` 名为「自动远征」，实现只收取已完成远征，不重新派遣。
3. 战役、演习的准备流程不调用 `apply_supply()`，只有常规战/活动战会补给。
4. `quick_repair_limit` 依赖的 `ctx.quick_repair_used` 没有自增点，
   当前实际不生效。
5. `dropped_ship_count` / `dropped_loot_count` 仅作统计，不再作为停止上限。
