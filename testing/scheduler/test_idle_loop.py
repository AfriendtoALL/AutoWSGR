"""常规战「空转」检测单元测试（无设备）—— 2026-10-13 用户实机反馈。

问题现场
--------
用户的 5-2 计划要求白名单节点 ``[C]``，但默认编队不满足带路条件，
舰队实际进入 ``B`` 点 → 白名单未命中 → 撤退 → ``SKIP_FIGHT`` 结束本场
→ 触发器再产出下一场 → 再次进 ``B`` → 再撤退 → **无限循环**。

为什么现有三道防线全都漏掉（已逐一核实）
-----------------------------------------
1. ``max_steps``（单场步数上限 200）：``CombatEngine._reset()`` **每场清零**，
   而本循环每场只走约 5 步就结束 —— 计数器永远攒不到 200。
2. 熔断（连续失败 5 次）：``SKIP_FIGHT`` **不在** ``_DONE_FLAGS`` 也不算失败，
   是刻意排除的正常资源信号 —— 不计入 ``_consecutive_failures``。
3. 停滞检测（同坐标连续点击 180s）：每场点的是不同坐标（出征/撤退按钮轮换），
   不满足「同一坐标」前提。

于是 ``completed`` 永不增长 → 触发器无限产出。本模块补的就是这一类
**「无异常、只是无进展」**的循环。

判据选择
--------
「连续 N 场都是 ``SKIP_FIGHT`` 且期间零成功」—— 语义上就是「一直在打但
一次没打成」，是个明确的资源浪费信号，与熔断精神一致但覆盖它漏掉的类别。
"""

from __future__ import annotations

import types

import pytest

from autowsgr.combat import CombatResult
from autowsgr.combat.history import CombatEvent, CombatHistory, EventType
from autowsgr.scheduler.triggers import (
    IDLE_LOOP_THRESHOLD,
    NormalFightPlan,
    NormalFightTrigger,
)
from autowsgr.types import ConditionFlag


# ══════════════════════════════════════════════
# 辅助
# ══════════════════════════════════════════════


def _ctx() -> types.SimpleNamespace:
    """最小 ctx 替身（不触碰未用字段）。"""
    return types.SimpleNamespace(
        dropped_ship_count=0,
        dropped_loot_count=0,
        quick_repair_used=0,
    )


def _make_trigger(*, target: int | None = None) -> tuple[NormalFightTrigger, NormalFightPlan]:
    plan = NormalFightPlan(
        factory=lambda _c: object(),
        name='x',
        fleet_id=1,
        target=target,
    )
    trigger = NormalFightTrigger(priority=100, name='常规战', plans=[plan])
    return trigger, plan


def _retreat_result(node: str = 'B') -> CombatResult:
    """构造一场「白名单未命中 → 撤退」的战斗结果。

    与 ``handlers.py`` 的白名单撤退分支同构：记一条 ``SPOT_ENEMY`` 事件、
    ``action='撤退'``，并以 ``SKIP_FIGHT`` 结束。
    """
    history = CombatHistory()
    history.add(
        CombatEvent(event_type=EventType.SPOT_ENEMY, node=node, action='撤退'),
    )
    return CombatResult(flag=ConditionFlag.SKIP_FIGHT, history=history)


def _success_result() -> CombatResult:
    return CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)


# ══════════════════════════════════════════════
# 1. 核心：连续 SKIP_FIGHT 触发停用
# ══════════════════════════════════════════════


class TestIdleLoopDetection:
    def test_disabled_after_threshold_consecutive_skips(self) -> None:
        """🔴 核心回归：连续 N 次撤退后该 plan 必须被停用。"""
        trigger, plan = _make_trigger()
        trigger._current = plan

        for _ in range(IDLE_LOOP_THRESHOLD):
            trigger._on_done(_retreat_result())

        assert plan.disabled_reason, '达到阈值后 plan 应被标记停用'

    def test_not_disabled_below_threshold(self) -> None:
        """差一次不触发 —— 阈值边界必须精确。"""
        trigger, plan = _make_trigger()
        trigger._current = plan

        for _ in range(IDLE_LOOP_THRESHOLD - 1):
            trigger._on_done(_retreat_result())

        assert not plan.disabled_reason, (
            f'仅 {IDLE_LOOP_THRESHOLD - 1} 次不应停用（阈值 {IDLE_LOOP_THRESHOLD}）'
        )

    def test_success_resets_counter(self) -> None:
        """中途成功一场 → 计数归零（证明是真「连续」判定）。"""
        trigger, plan = _make_trigger()
        trigger._current = plan

        for _ in range(IDLE_LOOP_THRESHOLD - 1):
            trigger._on_done(_retreat_result())
        trigger._on_done(_success_result())          # 插一场成功
        for _ in range(IDLE_LOOP_THRESHOLD - 1):
            trigger._on_done(_retreat_result())

        assert not plan.disabled_reason, '中间有成功就不该判定为空转'

    def test_disabled_plan_not_picked(self) -> None:
        """停用后 ``should_fire`` 不得再产出该 plan。"""
        trigger, plan = _make_trigger()
        trigger._current = plan
        for _ in range(IDLE_LOOP_THRESHOLD):
            trigger._on_done(_retreat_result())

        assert trigger.should_fire(_ctx()) is None, '停用的 plan 不应再被选中'

    def test_pick_plan_skips_disabled(self) -> None:
        """直接验证 ``_pick_plan`` 的过滤 —— 变异测试发现的缺口（两处都要锁）。

        ``should_fire`` 会先走 ``_is_exhausted``，而 ``_has_plan`` 已能拦住
        停用的 plan。于是只测 ``should_fire`` 的话，``_pick_plan`` 内部的
        判断**被删掉也不会失败**（变异 D 曾存活）。

        注意 ``_pick_plan`` 有**两条**候选路径，必须分别覆盖：
        - 第一个循环：有 ``target`` 且未完成的 plan
        - ``unlimited`` 列表：``target is None`` 的 plan
        只测其中一条时，另一条的过滤删掉也发现不了（第一次补测就栽在这里）。
        """
        # —— 路径 1: 有 target 的 plan ——
        bad_t = NormalFightPlan(factory=lambda _c: object(), name='bad_t', fleet_id=1, target=3)
        good_t = NormalFightPlan(factory=lambda _c: object(), name='good_t', fleet_id=1, target=3)
        trigger = NormalFightTrigger(priority=100, name='常规战', plans=[bad_t, good_t])
        trigger._current = bad_t
        for _ in range(IDLE_LOOP_THRESHOLD):
            trigger._on_done(_retreat_result())
        picked = trigger._pick_plan()
        assert picked is not None and picked.name == 'good_t', (
            f'[有 target 路径] 应跳过停用 plan, 实际: {getattr(picked, "name", None)}'
        )

        # —— 路径 2: 无限 plan (target=None) ——
        bad_u = NormalFightPlan(factory=lambda _c: object(), name='bad_u', fleet_id=1)
        good_u = NormalFightPlan(factory=lambda _c: object(), name='good_u', fleet_id=1)
        trigger2 = NormalFightTrigger(priority=100, name='常规战', plans=[bad_u, good_u])
        trigger2._current = bad_u
        for _ in range(IDLE_LOOP_THRESHOLD):
            trigger2._on_done(_retreat_result())
        picked2 = trigger2._pick_plan()
        assert picked2 is not None and picked2.name == 'good_u', (
            f'[无限 plan 路径] 应跳过停用 plan, 实际: {getattr(picked2, "name", None)}'
        )

    def test_pick_plan_returns_none_when_all_disabled(self) -> None:
        """全部 plan 停用 → 无 plan 可选（而非返回停用者）。"""
        plan = NormalFightPlan(factory=lambda _c: object(), name='only', fleet_id=1)
        trigger = NormalFightTrigger(priority=100, name='常规战', plans=[plan])

        trigger._current = plan
        for _ in range(IDLE_LOOP_THRESHOLD):
            trigger._on_done(_retreat_result())

        assert trigger._pick_plan() is None, '全部停用时不应返回被停用的 plan'

    def test_other_plans_unaffected(self) -> None:
        """只停出问题的那一个 plan，其余照常产出。

        这是用户明确要求的行为（「停止该计划 + 报警」而非停整个调度器）：
        多计划挂机时，一个 plan 配置有误不应连带停掉其它正常任务。
        """
        bad = NormalFightPlan(factory=lambda _c: object(), name='bad', fleet_id=1)
        good = NormalFightPlan(factory=lambda _c: object(), name='good', fleet_id=1)
        trigger = NormalFightTrigger(priority=100, name='常规战', plans=[bad, good])

        trigger._current = bad
        for _ in range(IDLE_LOOP_THRESHOLD):
            trigger._on_done(_retreat_result())

        assert bad.disabled_reason, '有问题的 plan 应停用'
        assert not good.disabled_reason, '其它 plan 不应受影响'
        task = trigger.should_fire(_ctx())
        assert task is not None and 'good' in task.name, (
            f'应继续产出正常 plan, 实际: {getattr(task, "name", None)}'
        )

    def test_reset_does_not_revive_disabled_plan(self) -> None:
        """跨日 reset 不应复活 —— 根因是配置错误，不会自己好。

        与 ``_dock_full_stopped`` 的会话级语义一致：编队不满足带路条件
        是配置问题，跨日不会自动缓解，必须人工修正后重启脚本。
        """
        trigger, plan = _make_trigger(target=3)
        trigger._current = plan
        for _ in range(IDLE_LOOP_THRESHOLD):
            trigger._on_done(_retreat_result())

        trigger.reset()

        assert plan.disabled_reason, 'reset 后仍应保持停用'
        assert trigger.should_fire(_ctx()) is None


# ══════════════════════════════════════════════
# 2. 根因提示：告诉用户卡在哪个节点
# ══════════════════════════════════════════════


class TestRetreatNodeDiagnostics:
    def test_records_retreat_nodes(self) -> None:
        """应累计各撤退节点的次数，供日志给出可操作提示。"""
        trigger, plan = _make_trigger()
        trigger._current = plan

        for _ in range(5):
            trigger._on_done(_retreat_result('B'))
        for _ in range(5):
            trigger._on_done(_retreat_result('A'))

        assert plan.skip_nodes.get('B') == 5, f'B 点次数不对: {plan.skip_nodes}'
        assert plan.skip_nodes.get('A') == 5, f'A 点次数不对: {plan.skip_nodes}'

    def test_diagnostics_cleared_on_success(self) -> None:
        """成功后节点统计应清空，避免把历史数据当成当前问题。"""
        trigger, plan = _make_trigger()
        trigger._current = plan

        trigger._on_done(_retreat_result('B'))
        trigger._on_done(_success_result())

        assert plan.skip_nodes == {}, f'成功后应清空统计, 实际: {plan.skip_nodes}'

    def test_log_mentions_node_and_possible_cause(self, caplog) -> None:
        """触发时日志必须含节点信息与「带路条件」提示。

        这条断言的价值：把「隐形循环」变成「一次可操作的配置提示」——
        用户看到日志就知道要去改编队，而不是面对一个不知为何在空转的脚本。
        """
        trigger, plan = _make_trigger()
        trigger._current = plan
        for _ in range(IDLE_LOOP_THRESHOLD):
            trigger._on_done(_retreat_result('B'))

        assert plan.disabled_reason is not None
        reason = plan.disabled_reason
        assert 'B' in reason, f'停用原因应含撤退节点, 实际: {reason}'
        assert '带路' in reason or '编队' in reason, (
            f'停用原因应给出可能的根因提示, 实际: {reason}'
        )


# ══════════════════════════════════════════════
# 3. 不误伤：其它终止态不应计入空转
# ══════════════════════════════════════════════


class TestNoFalsePositive:
    def test_dock_full_does_not_count_as_idle(self) -> None:
        """船坞满是正常资源信号，有自己的停止路径，不应算空转。"""
        trigger, plan = _make_trigger()
        trigger._current = plan
        for _ in range(IDLE_LOOP_THRESHOLD * 2):
            trigger._on_done(
                CombatResult(flag=ConditionFlag.DOCK_FULL, dock_full_destroyed=True)
            )
        assert not plan.disabled_reason, 'DOCK_FULL 不应触发空转判定'

    def test_action_failed_does_not_count_as_idle(self) -> None:
        """ACTION_FAILED 由熔断负责，不重复计空转（避免双重惩罚）。"""
        trigger, plan = _make_trigger()
        trigger._current = plan
        for _ in range(IDLE_LOOP_THRESHOLD * 2):
            trigger._on_done(CombatResult(flag=ConditionFlag.ACTION_FAILED))
        assert not plan.disabled_reason, 'ACTION_FAILED 应由熔断处理'

    def test_threshold_is_positive(self) -> None:
        assert IDLE_LOOP_THRESHOLD > 0


# ══════════════════════════════════════════════
# 4. 端到端：复现用户的实际循环（公开 API 路径）
# ══════════════════════════════════════════════


class TestUserScenarioEndToEnd:
    """按用户实机场景走完整循环，确认从「无限空转」变为「有限次后停止」。

    用户原话：「他只会不断的在地图页出征，从起始点出发然后进入不包含的
    节点，撤退，然后再次回到地图页从起始点出征进入死循环，但是我看并没有
    触发兜底」。

    这里用 ``should_fire`` → ``_on_done`` 交替驱动（与调度器主循环同构），
    统计停止前究竟产出了多少场任务。
    """

    def test_loop_terminates(self) -> None:
        trigger, plan = _make_trigger(target=50)   # 想要 50 场，但一场也打不成
        ctx = _ctx()

        produced = 0
        for _ in range(200):                       # 上限远大于阈值，防止测试挂死
            task = trigger.should_fire(ctx)
            if task is None:
                break
            produced += 1
            trigger._on_done(_retreat_result('B'))

        assert produced == IDLE_LOOP_THRESHOLD, (
            f'应在 {IDLE_LOOP_THRESHOLD} 场后停止产出, 实际产出 {produced} 场'
        )
        assert plan.completed == 0, '一场都没打成，完成数应保持 0'
        assert plan.disabled_reason and 'B' in plan.disabled_reason

    def test_normal_fight_still_completes_target(self) -> None:
        """对照组：正常战斗不受影响，仍能打满 target（防止误伤）。"""
        trigger, plan = _make_trigger(target=3)
        ctx = _ctx()

        produced = 0
        for _ in range(20):
            task = trigger.should_fire(ctx)
            if task is None:
                break
            produced += 1
            trigger._on_done(_success_result())

        assert produced == 3, f'应打满 3 场即停, 实际 {produced}'
        assert plan.completed == 3
        assert not plan.disabled_reason, '正常完成不应被判定为空转'
