# AutoWSGR 架构评审报告

> 评审日期：2026-10-11
> 对标基准：Alas (LmeSzinc/AzurLaneAutoScript) @ master
> 评审范围：`C:\Users\13930\Desktop\AutoWSGR`（188 个 .py / 43,606 行）
> **本次评审为只读，未修改任何代码。**

---

## 摘要

AutoWSGR 的**数据建模质量高于同类项目**（ROI/Template/Signature/PixelRule 全部 frozen dataclass + 坐标序校验，依赖注入方向基本正确，调度器的"触发器自产任务 + 看门狗收尾"设计甚至比 Alas 更精巧）。

但它存在一个**结构性的判断**：目前是一个「能跑通单条链路的调优型脚本」，而不是「能长期无人值守运行的自动化系统」。核心短板集中在**故障恢复**和**传输层固化**两处——两者都不是 bug，是架构选择带来的必然结果。

**最关键的三件事**（详见 P0）：
1. 战斗引擎的恢复分支没有次数上限，识别卡死会**死循环**
2. 调度器从不调用已存在的故障恢复函数，异常被吞成 `ACTION_FAILED` 后**无限重试**
3. 弹药检测、MaaTouch、NemuIpc 三个核心模块**从未纳入 git**，一次误操作即永久丢失

---

## P0 — 会导致挂机永久失效的问题

### P0-1 战斗恢复分支可无限循环（无次数上限）

`autowsgr/combat/engine.py:160-172`

```python
while True:
    try:
        decision = self._step()
    except CombatRecognitionTimeoutError as e:
        _log.warning('[Combat] 状态识别超时: {}', e)
        if self._try_recovery():
            continue          # ← 无计数器、无 backoff
        result.flag = ConditionFlag.SL
```

`_try_recovery()` 仅 `sleep(3s)` + 重新识别，无重试计数、无退避、无「已恢复 N 次则放弃」条件。

**已验证**：`grep -nE "retry|attempt|max_recover|recover_count" autowsgr/combat/engine.py` → **零命中**。

**失效路径**：若页面稳定停在某个后继态（典型如 `PROCEED` 模板误命中但点击无效），`_update_state` 再次超时 → 再次恢复 → `continue` → 永久死循环。对 7×24 挂机而言，单次死循环 = 整个脚本报废到手动干预为止。

### P0-2 调度器无健康巡检、无熔断，恢复能力形同虚设

`recover_to_main_or_restart()` 定义在 `ops/startup.py:304`，功能完整（含重启游戏），但：

**已验证调用点**：
- `ops/startup.py:380`（启动流程内）
- `ops/expedition.py:36`（远征任务内）
- `testing/ops/test_startup_unit.py:121`（测试）

**`scheduler.run_daily()` 主循环从不调用它。**

而失败处理是（`scheduler/scheduler.py:258-268`）：
```python
except Exception as exc:
    _log.opt(exception=True).error('[Scheduler] {} 第 {} 次异常...', task.name, rounds+1, exc)
    results = [CombatResult(flag=ConditionFlag.ACTION_FAILED)]
```

**失效路径**：夜间模拟器掉线 / 游戏闪退 / 网络断 → `goto_page` 抛 `NavigationError` → 被吞成 `ACTION_FAILED` → 触发器 `_on_done` 只把 `_idle=True` → 下一轮继续产出任务 → 再失败 → **无限空转，日志持续写盘**。

`logs/` 下已有 18+ 个时间戳目录印证了这一点。缺的是 Alas 式的 `failure_record` 计数 + ≥3 次熔断。

### P0-3 三个核心模块从未纳入版本控制

**已验证 `git status --porcelain`**：

```
?? autowsgr/emulator/controller/maatouch.py    ← 触控后端实现
?? autowsgr/emulator/controller/nemu_ipc.py     ← 截图后端实现
?? autowsgr/ui/battle/ammo.py                   ← 弹药检测（memory 中 10-08 反复调试的核心）
?? bin/                                          ← MaaTouch / DroidCast 二进制
?? benchmark.py, diag_*.py, verify_supply_gate.py, run_h5_acd.py, plans/
```

`ammo.py` 是项目记忆中投入最多（2026-10-08 专门迭代：RGB/BGR 通道、饱和度策略、坐标分段）的模块，**却没有任何版本历史**。一次 `git clean -fd` 或误删即永久损失。同时工作区还有大量 `M`（已修改未提交）状态。

---

## P1 — 结构性技术债

### P1-1 传输层是上帝类，触控后端硬编码 if-else 链

`emulator/controller/scrcpy.py`（939 行）同时承担：传输实现、H264 编解码、三套触控后端降级决策、ADB 生命周期、重连策略。

`_adb_input()`（`scrcpy.py:748-781`）：
```python
if self._maatouch is not None and args[0] == 'tap':   # 只有 tap 走 MaaTouch
if self._persistent_shell is not None:                # 常驻 shell
dev.shell(['input', *args], timeout=timeout)          # 单次 adb
```
且初始化失败**永久置 None**（`:712/:765/:776`），运行期无法恢复。新增一种触控方式要改 `_adb_input` + `connect` + ABC 三处。

`scroll`/`key_event` 走 scrcpy 控制流、`click`/`swipe`/`long_tap` 走 adb —— **同一抽象内两套传输语义**，注释坦承是 MuMu 12 冲突的临时方案。

对比 Alas：`Device` 聚合 10 种截图方法（`nemu_ipc`/`scrcpy`/`DroidCast`/`minitouch`/`MaaTouch`…），靠 `screenshot_methods()` 返回 dict + `@method_wrapper` 反射查表，**启动时跑基准测试自动选最优**。这是纯粹的「接口 + 多实现 + 策略选择」。

### P1-2 分层倒置：四个方向的逆向依赖

**已验证**：

| 依赖 | 位置 | 问题 |
|---|---|---|
| `context` → `ui` | `context/game_context.py:12` | context 层位于 ui 之下，却 import 上层 |
| `context` → `ops` | `game_context.py:257-263` | 函数内 `from autowsgr.ops import goto_page` |
| `combat` → `ops` | `combat/engine.py:183` | 作者自标 `TODO: 轻微的抽象泄露` |
| `combat` → `ui` | `combat/recognizer.py:99` | import `event_page` 的**两个下划线私有函数** |
| `combat` → `ui` | `combat/handlers.py:34` | `from autowsgr.ui.utils import wait_leave_page` |
| `combat` → `cv2` | `combat/node_tracker.py:22` | 绕过 `autowsgr.vision` 抽象层 |

后果：**combat 无法脱离 ui 独立运行和测试**。`fight()` 主循环零测试覆盖。

`docs/architecture/README.md:29` 明确写着"每一层只依赖其下方的层"——**文档承诺与代码不符**。

### P1-3 全局可变状态 4 处 + 类级运行时状态 3 处

**已验证 `global` 语句**：
- `infra/config.py:560` — Pydantic validator 里写 `global OPERATION_DELAY_MIN/MAX`，单测互相污染
- `infra/logger.py:290` — `global _image_dir, _channel_levels`
- `ops/intensify.py:158` — `global _session_target_inventory_baseline`
- `vision/ocr.py:53` — `global _ship_name_match_confidence`

**类级运行时状态**（最严重）：
```python
# ops/normal_fight.py:121
_overlay_ready: ClassVar[set[tuple[str | None, str, str | None]]] = set()
```
运行时状态存进类变量 → 同进程跑不同计划会串扰、进程重启即丢失、**永不清理**。注释 `:106-111` 自认这是为修「触发器每轮新建实例」而引入的补丁。

同类：`scheduler/triggers.py:120/198/207` 的 `exhaust_flags: ClassVar[set[...]]`。

### P1-4 异常处理失控：95 处 `except Exception`，12 处直接吞掉

最危险的（`emulator/controller/scrcpy.py:780-781`）：
```python
except Exception as exc:
    _log.warning('[Emulator] adb input {} 失败: {}', args, exc)
```
**仅记日志、不重抛** —— 点击静默失败，上层以为成功继续跑，错误延迟到业务层才暴露，且无法区分「设备断连」与「参数错」。`disconnect()` 的三连 `except Exception: pass` 同理。

项目有完整的异常树（`infra/exceptions.py`，20 个类），但**框架层几乎不抛**。Alas 的做法是每个自定义异常对应一个明确恢复动作，由 `alas.py::run()` 统一映射，不写 `except Exception` 通吃。

### P1-5 活动期号硬编码渗透到通用页面代码

**已验证**：9 处、跨 3 个文件
```python
# ui/event/event_page.py:315-316  —— 通用页面控制器里的单活动分支
if self._event_name == '20260930' and entrance is not None:
    raise ValueError('20260930 活动 map 请使用纯数字，不支持 α/β 入口')

# :147  节点布局坐标按期号硬编码
'20260930': { ... }
```
模板文件名亦含期号：`event_title_20260930_540p.png`、`fight_button_20260930_540p.png`、`event_icon_20260930_540p.png`、`quiz_prompt_20260930_540p.png`。

**风险**：`_get_event_title_templates` 的 `find_any` 语义是「任一命中即算活动页」。新一期活动页面若与旧期相似，旧模板仍会命中 → `is_current_page` 返回 True 但 `is_fight_ready` 全 False → 循环 10 次后抛 `ActionFailedError`，日志只有一行 warning。**新活动上线即无声失效。**

对比 Alas：`Button.area = {'cn':(...), 'en':(...), 'jp':(...), 'tw':(...)}` 按服取值，节点布局用 ASCII art + `spawn_data` 声明。

---

## P2 — 可维护性与质量

### P2-1 阈值无中央注册表（已验证 7 处重复）

`0.85` 在 `vision/image_matcher.py` 的 7 个方法签名逐个重复声明（`:143,318,332,350,369,395,429`），`image_template.py:256` 又独立写了一次。调参要逐调用点覆盖。

战斗层同样是内联 magic number：`confidence=0.8` 出现 3 次、`0.85` 两次、`0.75` 一次，**无常量、无出处注释**。

### P2-2 242 处 `time.sleep`，无统一等待原语（已验证）

密集区：`ui/decisive/map_controller.py` 30 处、`ui/build_page.py` 21 处、`ops/intensify.py` 16 处。`ops/normal_fight.py:412-449` 一路裸 sleep 串起出征准备，全程无页面断言。

Alas 用 `ModuleBase.loop(timeout=Timer)` 生成器 + `for/else` 表达超时，把「截图-判断」写成线性代码。

### P2-3 重试/超时四套并存

| 实现 | 位置 | 风格 |
|---|---|---|
| 导航重试 | `ui/utils/navigation.py:367` | `NavConfig(max_retries=2, retry_delay=1.0, timeout=5.0)` |
| 设备连接 | `scrcpy.py:267-285` | 硬编码 `max_attempts=3` + `sleep(2.0)` |
| 视频流重连 | `scrcpy.py:539-549` | 单次 `_reopen_stream()` |
| 各级降级 | `_adb_input` / nemu / MaaTouch | 各自独立，无退避 |

超时值 `5.0`/`10.0`/`30.0`/`120` 全是字面量。

### P2-4 死代码（作者自认的仍在）

`docs/features/dead-code-analysis.md` 列出 4 条，**逐一核实，4 条全部仍在**（文档自己写的「本次提交不同时清理」确实没做）：

| 声明 | 核实 |
|---|---|
| `combat/actions.py: FLAGSHIP_CONFIRM` | 仍在，零引用 |
| `combat/actions.py: click_start_march` | 仍在，零调用 |
| `combat/fleet.py: NATIVE_FLEET_VESSEL_TYPES` | 仍在，零引用 |
| `combat/rules.py: _SHIP_TYPE_PATTERN` | 仍在，零引用 |
| `combat/recognition.py: _SHIP_TYPE_DISPLAY_MAP` | ✅ 已删（唯一清理掉的） |

**新增发现**：`recognizer.py:213-223` 的 `RESULT_GRADE_TEMPLATES`（自称"向后兼容别名"但零消费）、`node_tracker.py:582-585` 的 `NodeTracker.track()`（零调用）、`plan.py:87` 的 `proceed_stop`（`usage_combat.md:131` 仍在文档化，但引擎全域零消费）。

其他：`scheduler.py:468 _disable_normal_fight()` 全仓零调用；`scheduler.py:458 _sync_initial_counts()` 是空 `return`；`navigation.py:92,154` 的 `handle_overlays` 参数标了 `noqa: ARG001` 从不使用，文档却列为能力。

### P2-5 根目录 9 个脚本散落，含本机绝对路径

```
diag_ammo.py  diag_capture.py  diag_enter.py  diag_grid.py  diag_grid2.py  diag_ocr.py
benchmark.py  verify_supply_gate.py  run_h5_acd.py
```
全部未入 git。其中：
```python
# diag_capture.py:6
sys.path.insert(0, r'C:/Users/13930/Desktop/AutoWSGR')
# benchmark.py:38-41
ADB = r'C:\Program Files\Netease\MuMu\nx_main\adb.exe'
SERIAL = '127.0.0.1:16416'
```
硬编码路径**换机即废**；`benchmark.py` 的 MuMu 路径与 `usersettings.yaml:9-10` 配置**重复且来源不一致**。`tools/` 已有 `benchmark_emulator.py`，两者应合并。

### P2-6 私有 API 跨层引用

```
ui/bath_page/recognition.py:24   from autowsgr.vision.ocr import _fuzzy_match
ui/choose_ship_page.py:33         from autowsgr.vision.ocr import _fuzzy_match
ui/utils/ship_list.py:19          from autowsgr.vision.ocr import _fuzzy_match
vision/named_portrait_matcher.py  from ...ship_card_recognizer import _SHIP_TYPE_CODES
```
`_fuzzy_match` 是模块私有函数（`ocr.py:677`），被 3 个 UI 模块跨层调用 —— 重构必崩三处。

### P2-7 本机绝对路径泄漏进源码

`vision/ship_card_recognizer.py:353,359`：
```python
default_ncc = Path(r'E:\wsgrgui\resource\wsg-ncc')
default_lib = Path(r'E:\wsgrgui\resource\ship-library')
```
非该机用户必须设 `AUTOWSGR_WSG_NCC_DATA`，否则直接抛错。

### P2-8 文档与代码系统性脱节

- `docs/architecture/emulator.md:34` 写 `class AndroidController(Protocol)`，实为 `ABC`
- `docs/architecture/combat-engine.md:346-353` 详述 `recognize_ship_drop` + Levenshtein 模糊匹配 —— **该函数已删除**
- 同文档 phase 枚举**漏了 `EXP_SETTLEMENT`**；L253 仍写 `_handle_fight_period` 做血量分类（已移除）
- `infra.md` 完全未提 280 行的 `config_compat.py`

### P2-9 测试覆盖存在结构性缺口

**已验证** `testing/ops/` 下 11 个文件不以 `test_` 开头（`build.py`/`campaign.py`/`normal_fight.py`/`scheduler.py`…）→ **pytest 默认不收集，实际零覆盖**。

其他缺口：
- `combat/engine.py::fight()` 主循环零覆盖；`_try_recovery()` 零覆盖
- `conftest.py` 全文只有 2 个 fixture，**零 mock**
- `testing/ui/_framework.py:258` 必须连真机
- **没有一张真实截图 fixture** 做识别回归 —— 而识别恰恰是最脆弱、实机反馈周期以天计的部分
- `combat/README.md:46-61` 整节「特别测试（未做）」「补充测试（未做）」：旗舰大破回港、远程打击、中破规则回港、敌方阵容不匹配、SL 路径 **全部未做**

### P2-10 抽象泄漏（作者自认）

`docs/features/dead-code-analysis.md:108-111` 自认 `scheduler.py:50 FightRunnerProtocol`「不参与运行时判断」。根因是**缺统一 Task 契约**：`runner.run()` 返回类型在代码库里有两种（`CombatResult` vs `list[CombatResult]`），靠 `BatchRunnerAdapter` 事后适配。

`scheduler.py:227-232` 注释坦承踩坑经过：
> 不能靠 `isinstance(FightRunnerProtocol)` 判断：`@runtime_checkable` 不检查返回类型，CampaignRunner 有 `run()` 方法即被误判满足协议而跳过适配（曾致 `'list' object has no attribute 'flag'` 崩溃）

**协议签名骗了运行时检查** —— 这是缺 Task 抽象的直接代价。

---

## 与 Alas 的六项关键差距

| 维度 | Alas | AutoWSGR | 差距影响 |
|---|---|---|---|
| **异常→恢复映射** | 每个自定义异常对应明确恢复动作，由 `alas.py::run()` 统一映射 | 95 处 `except Exception`，12 处直接吞 | 无法判断失败是否会自愈 |
| **卡死检测** | `stuck_timer` + `stuck_timer_long` 双计时器 + `show_function_call()` 打印完整调用栈 | 无卡死检测，无调用栈输出 | 卡死时无法定位 |
| **失败熔断** | `failure_record` 计数，≥3 次 → `RequestHumanTakeover` + 推送 + exit | 无 | 无限重试 |
| **设备抽象** | 10 种截图方法反射查表 + 基准测试自动选优 | 单一实现 + 内联 if-else 降级 | 加设备要改上帝类 |
| **识别抽象** | `ButtonGrid(origin, delta, shape)` 参数化网格 + 四层识别栈（颜色→亮度→二值→复合） | 阈值散落 7 处，坐标散落 3 处 | 加分辨率/活动影响面大 |
| **调度契约** | 命名三合一契约（JSON key = 优先级 token = Python 方法名），零注册表 | 两种 runner 返回类型靠事后适配 | 协议骗过运行时检查 |

---

## 客观地说：AutoWSGR 做对了什么

不是全盘否定，有几处设计**比 Alas 更精巧**：

1. **调度器的「触发器自产任务 + 看门狗收尾」**（`scheduler/triggers.py` + `plan_runner.py:113`）比 Alas 的固定优先级串更灵活
2. **识别签名的三种手法**：否决键（`exclude_template_key` 区分战果/经验页）、多锚点 OR（`find_any`）、上下文开关（`allow_event_overlay_anchor` 解决出征瞬间误判）—— 都是实机事故换来的，Alas 反而没有
3. **到达验证 `_click_result_until_closed`**（`handlers.py:483-553`）把"动画吞点击""点击穿透中间页""未知页"三种实机故障分别建模，**认不出就只等待不点击**——这是本次评审中最扎实的设计
4. **坐标系统全项目统一归一化 0.0–1.0**，无绝对像素泄漏
5. **数据模型质量高**：ROI/Template/Signature/PixelRule 全部 frozen dataclass + `__post_init__` 校验（`roi.py:58-66` 校验坐标序）
6. **每次代码修复都在注释里留下实机事故记录**（日期 + 现象），这是很好的工程习惯

---

## 改进优先级建议

### 第一梯队（改动小、收益最大，建议立刻做）
1. **P0-3**：`git add` 那三个核心模块 + `bin/`，提交一次。零风险。
2. **P0-1**：给 `engine.py:160` 的 `while True` 加 `max_steps` 硬上限；给 `_try_recovery` 加 `recovery_attempts` 计数器 + 指数退避（2 行改动）。
3. **P2-4**：删掉作者已自认的 4 条死代码（文档已列清单）。
4. **P2-5**：根目录 9 个脚本 → 合并进 `tools/`，删 5 个 `diag_*`，`benchmark.py` 的 MuMu 路径改读 `usersettings.yaml`。

### 第二梯队（结构性，建议排期）
5. **P0-2**：把 `recover_to_main_or_restart` 接进 `scheduler.run_daily` 失败分支，加「连续 N 轮全失败 → 重启游戏」熔断。**这是 7×24 挂机的必要条件。**
6. **P1-3**：`NormalFightRunner._overlay_ready` 从 `ClassVar` 改为 ctx 级状态。
7. **P1-5**：`event_name` 参数化 — 节点坐标、模板路径、α/β 支持全部走数据（YAML）而非代码分支。

### 第三梯队（需要重构，适合有大块时间时）
8. **P1-1**：把 `ScrcpyController`（939 行）拆成 `ScrcpyVideoSource` / `TouchInjector`（各自接口化）+ 一个 `TransportSelector`，一次性消解硬编码降级链、LSP 签名不一致、设备切换非插件式三个问题。
9. **P1-2**：消除四个方向的逆向依赖，把 `restart_game` 作为回调注入 `CombatEngine.__init__`。
10. **P2-9**：建真实截图 fixture 集，把识别签名纳入回归测试。

---

## 一句话总结

AutoWSGR 的**识别与时序调优水平已经接近 Alas**，甚至在几个局部做得更细。但 Alas 用「异常体系 + 卡死检测 + 熔断 + 调用栈留证」这四层运维机制换来的 7×24 可靠性，AutoWSGR 目前**一层都没有**。

**当前状态下它适合「人盯着跑」，不适合「放着不管跑一整夜」。** 而补齐 P0 三条的改动量，可能不到 50 行代码。