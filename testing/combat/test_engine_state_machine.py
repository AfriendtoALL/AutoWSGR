"""``CombatEngine.fight()`` 状态机端到端测试（T3-2 验收第 3 条）。

为什么需要这一层
----------------
``test_engine_circuit_breaker_unit.py`` 的 17 项全部monkeypatch 掉了
``engine._step`` —— 它们测的是 ``fight()`` 的**外壳**（步数护栏、恢复护栏、
SL 回调、终止态分发），而 ``_step()`` 以下的**真实状态机**
（``_update_state`` → ``resolve_successors`` → ``_make_decision`` → 各handler）
**一次都没跑过**。转移表被改坏、handler 派发出错、节点计数错乱，
现有测试全部无感。

测试策略
--------
只 mock **截图**（纯 IO），保留全部逻辑：

- ``CombatRecognizer`` 换成 :class:`ScriptedRecognizer`，按预设序列返回 phase，
  同时记录每一步收到的候选列表 —— 于是「转移是否合法」可被断言
- ``resolve_successors`` / ``_make_decision`` / 所有 handler **真跑**
- 设备是 ``MagicMock``（handlers 只用到 ``screenshot()``，见源码核实）
- 地图数据加载被跳过（SINGLE 模式不需要 map 数据，天然绕开文件系统）

不锁的东西
----------
截图是否真能认出某个 phase —— 那是``test_combat_fixtures.py`` 的职责，
用真机截图验证。本文件只管「给定识别结果，状态机怎么走」。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np

import pytest

from autowsgr.combat.engine import CombatEngine
from autowsgr.combat.history import EventType
from autowsgr.combat.plan import CombatMode, CombatPlan
from autowsgr.combat.state import CombatPhase
from autowsgr.types import ConditionFlag


# ══════════════════════════════════════════════
# 脚手架
# ══════════════════════════════════════════════


class ScriptedRecognizer:
    """按预设序列返回 phase，并记录每步的候选列表与门控参数。

    **只替代截图**。``candidates`` / ``allow_event_overlay_anchor`` / ``poll_action``
    都如实传给本对象，使测试能断言「引擎在正确阶段做了正确的事」。

    ``identify_current`` 是结算穿行用的「当前页是否已到落点」探测 ——
    它不吃脚本序列(那是主循环用的), 而是直接报告已抵达 ``settled_at``,
    因为盲点穿行的正确行为就是「点到落点就停」, 真实设备上这一步需要
    反复点击若干次, 脚本化时直接判定成功即可(点击次数本身不是本文件关注点)。
    """

    def __init__(self, script: list[CombatPhase], *, settled_at: CombatPhase | None = None) -> None:
        self.script = list(script)
        self.calls: list[dict] = []
        self.settled_at = settled_at
        self.current_probes = 0

    def wait_for_phase(self, candidates, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(
            {
                'candidates': list(candidates),
                'allow_event_overlay_anchor': kwargs.get('allow_event_overlay_anchor'),
                'poll_action': kwargs.get('poll_action'),
            }
        )
        if not self.script:
            raise AssertionError(f'脚本已耗尽但引擎仍在等待状态: 候选={[c.name for c in candidates]}')
        return self.script.pop(0)

    def identify_current(self, screen, stops):  # noqa: ANN001, ANN003
        """结算穿行的落点探测: 已配置则立即报告抵达, 否则永不抵达(模拟点穿)。"""
        self.current_probes += 1
        return self.settled_at


def make_event_plan(**overrides) -> CombatPlan:
    """活动战计划（终态 EVENT_MAP_PAGE）。"""
    plan = CombatPlan.from_dict({'name': 't', 'chapter': 'H', 'map': 5, 'event': '20260930'})
    plan.mode = CombatMode.EVENT
    for k, v in overrides.items():
        setattr(plan, k, v)
    return plan


def make_engine(recognizer: ScriptedRecognizer | None = None) -> CombatEngine:
    """构造不接真机的引擎。

    ``SimpleNamespace`` ctx 足够: ``_get_poll_action`` 只读 ``self._plan.mode``,
    ``_update_state`` 只读 ``self._plan.transitions``, handlers 只用到
    ``self._device.screenshot()``(已核实)。

    刻意**每次新建**而非复用 —— ``CombatEngine`` 内部有 ``_history`` /
    ``_phase`` / ``_steps`` 等状态, 复用会让测试互相污染(实测踩过:
    前一个用例残留的 phase 让后一个用例少走几步, 断言时好时坏)。

    ``screenshot()`` 必须返回**合法 numpy 图像** —— 索敌处理器会对它跑
    ``cv2.cvtColor`` 做敌舰编成识别(实测踩过: 返回 MagicMock 直接
    ``cv2.error``)。给一张纯黑 960x540 图即可: 识别不出敌舰 → enemies=None,
    规则引擎按 None 处理, 不影响「规则 → 撤退」链路本身。
    """
    blank = np.zeros((540, 960, 3), dtype=np.uint8)
    ctrl = MagicMock()
    ctrl.screenshot.return_value = blank
    ctx = SimpleNamespace(
        ctrl=ctrl,
        ocr=None,
        stop_event=threading.Event(),
    )
    return CombatEngine(ctx)


def run_fight(
    engine: CombatEngine,
    script: list[CombatPhase],
    monkeypatch,
    *,
    plan=None,
    settled_at: CombatPhase | None = None,
):
    """用脚本驱动 ``fight()`` 跑完, 返回 (result, recognizer)。

    打桩范围严格限定在「看不见画面」的地方 ——
    ``CombatRecognizer`` / ``ImageChecker`` / ``detect_result_grade`` /
    ``wait_until_leaving``。**状态机、转移表、handler 分派、节点计数全部真跑。**
    """
    plan = plan or make_event_plan()
    # 结算穿行的落点默认就是该计划的终态 —— 盲点穿行的正确行为是
    # 「一直点到离开结算页」, 脚本化时用「已抵达」表达
    recognizer = ScriptedRecognizer(
        script, settled_at=settled_at if settled_at is not None else plan.end_phase
    )

    # ── 视觉层打桩(纯 IO, 不含业务逻辑) ──
    monkeypatch.setattr(
        'autowsgr.combat.engine.CombatRecognizer', lambda *_a, **_k: recognizer
    )
    # handlers 里直接调用 ImageChecker.find_any (索敌成功判定), 需给合法返回
    monkeypatch.setattr(
        'autowsgr.combat.handlers.ImageChecker.find_any',
        lambda *_a, **_k: MagicMock(),
    )
    # 战果页评级识别: 战���期采集才走, 但慢速路径默认关闭也要防意外
    monkeypatch.setattr(
        'autowsgr.combat.handlers.detect_result_grade', lambda *_a, **_k: None
    )
    # 到达验证: 等待页面离开, 无真设备时立即「已离开」
    monkeypatch.setattr(
        'autowsgr.combat.handlers.wait_until_leaving', lambda *_a, **_k: True
    )

    monkeypatch.setattr('autowsgr.combat.engine.time.sleep', lambda _s: None)
    monkeypatch.setattr('autowsgr.combat.handlers.time.sleep', lambda _s: None)
    # 地图数据加载: EVENT 模式会读 activity YAML(真实文件), 打桩避免 IO
    monkeypatch.setattr(
        'autowsgr.combat.engine.MapNodeData.load_event', lambda *_a, **_k: None
    )

    result = engine.fight(plan, initial_ship_stats=None)
    return result, recognizer


# 一条最小可用的活动战路径:
# 出征 → 战况选择 → 索敌成功(打) → 阵型选择 → 战斗动画 → 战果 → 前进 → 终态
SIMPLE_EVENT_PATH = [
    CombatPhase.FIGHT_CONDITION,
    CombatPhase.SPOT_ENEMY_SUCCESS,
    CombatPhase.FORMATION,
    CombatPhase.FIGHT_PERIOD,
    CombatPhase.RESULT,
    CombatPhase.PROCEED,
    CombatPhase.EVENT_MAP_PAGE,  # end_phase, 战斗结束
]


def _history_types(result) -> list[EventType]:  # noqa: ANN001
    """``CombatResult.history`` 是 :class:`CombatHistory`, 取其 ``.events``。"""
    return [e.event_type for e in result.history.events]


@pytest.fixture
def engine() -> CombatEngine:
    """每用例一个全新引擎 (状态不复用, 见 make_engine 的说明)。"""
    return make_engine()


# ══════════════════════════════════════════════
# 1. 完整路径跑通 (核心回归)
# ══════════════════════════════════════════════


class TestFullPathRuns:
    def test_simple_event_path_reaches_success(self, engine, monkeypatch) -> None:
        """活动战单节点路径应跑通并返回 OPERATION_SUCCESS。"""
        result, _ = run_fight(engine, SIMPLE_EVENT_PATH, monkeypatch)
        assert result.flag is ConditionFlag.OPERATION_SUCCESS

    def test_all_steps_consumed_exactly_once(
        self, engine, monkeypatch
    ) -> None:
        """每一步都应恰好调用识别器一次 (不重复轮询、不跳过)。"""
        _, recognizer = run_fight(engine, SIMPLE_EVENT_PATH, monkeypatch)
        assert len(recognizer.calls) == len(SIMPLE_EVENT_PATH)
        assert not recognizer.script, '有脚本未被消费, 说明引擎提前退出了'


# ══════════════════════════════════════════════
# 2. 转移合法性: 每一步的候选必须包含下一个 phase
# ══════════════════════════════════════════════


class TestTransitionLegality:
    def test_every_next_phase_was_a_candidate(self, engine, monkeypatch) -> None:
        """**核心不变量**: 引擎只能走到转移表声明过的后继状态。

        若转移表被改坏(删边/改指向), 脚本给的下一个 phase 不在candidates 里,
        这里立刻失败 —— 而现有17项熔断测试对此完全无感。

        注意下标对应关系: 第 k 次调用**认出** ``script[k]``, 而它拿到的
        candidates 是**出发状态** ``script[k-1]`` 的后继
        (见 ``engine._update_state``: candidates 由 last_phase 解析)。
        所以校验 ``script[k] ∈ calls[k].candidates``, k 从 1 开始。
        """
        _, recognizer = run_fight(engine, SIMPLE_EVENT_PATH, monkeypatch)
        assert len(recognizer.calls) == len(SIMPLE_EVENT_PATH)
        for k in range(1, len(SIMPLE_EVENT_PATH)):
            from_phase = SIMPLE_EVENT_PATH[k - 1]
            to_phase = SIMPLE_EVENT_PATH[k]
            cands = {c.name for c in recognizer.calls[k]['candidates']}
            assert to_phase.name in cands, (
                f'从 {from_phase.name} 到 {to_phase.name} 不是合法转移; '
                f'实际候选={sorted(cands)}'
            )


# ══════════════════════════════════════════════
# 3. 门控参数: allow_event_overlay_anchor (2026-10-08 事故防线)
# ══════════════════════════════════════════════


class TestOverlayAnchorGate:
    def test_disabled_during_start_fight(self, engine, monkeypatch) -> None:
        """出征阶段必须关闭按钮锚点(否则误判回港直接结束)。

        这是 ``combat/engine.py`` 里 ``allow_event_overlay_anchor=
        last_phase is not CombatPhase.START_FIGHT`` 那行的行为。
        现有测试只锁了 recognizer 的签名, 没锁引擎**实际传了什么**。
        """
        _, recognizer = run_fight(engine, SIMPLE_EVENT_PATH, monkeypatch)
        # 第一步从 START_FIGHT 出发 → 必须关闭
        assert recognizer.calls[0]['allow_event_overlay_anchor'] is False

    def test_enabled_after_start_fight(self, engine, monkeypatch) -> None:
        """出征之后必须开启(战斗后段弹层只可能真实出现)。"""
        _, recognizer = run_fight(engine, SIMPLE_EVENT_PATH, monkeypatch)
        for i, call in enumerate(recognizer.calls[1:], start=1):
            assert call['allow_event_overlay_anchor'] is True, (
                f'第 {i} 步(从 {SIMPLE_EVENT_PATH[i-1].name} 出发)应开启锚点门控'
            )


# ══════════════════════════════════════════════
# 4. 战斗动画期绝不盲点 (2026-10-08 实机事故)
# ══════════════════════════════════════════════


class TestFightPeriodAndSettleSkip:
    """战斗动画期不点击, 与结算期的安全位盲点穿行。

    更正 (2026-10-12 用户指出)
    --------------------------
    初版断言写的是「等待期盲点会把夜战整段点穿」—— **说法有误**,
    把两个不同机制混为一谈了。实际分工是:

    - ``FIGHT_PERIOD`` (战斗动画期): poll_action 是纯 ``time.sleep``,
      **不点击**。此时结算三层还没出现, 安全位点击没有意义; 而动画中的
      加速点击另有 :func:`click_speed_up` 的专门场景。
    - 结算三层 (战果/经验/掉落): 由处理器层的 ``_blind_skip_settlement``
      用 :func:`click_settlement_skip` 连点**安全位**推进 —— 结算页
      全屏点击有效(点掉当前层), 而该坐标在全部相关页面上无可交互元素,
      **误触无害**。这是**主动**的推进手段, 不是防止点穿的护栏。

    真正的护栏是**坐标选得对**: 必须取左下象限 x≈0.26。取 x=0.5 会点中
    阵型选择界面的「单横阵」(该按钮一直贴到屏幕底缘)—— 2026-10-10 修正。
    """

    def test_fight_period_poll_action_does_not_click(self, engine, monkeypatch) -> None:
        """"当前 phase 为 FIGHT_PERIOD 时"的轮询不得点击 (只sleep)。

        下标对应关系 (``_update_state`` 先算poll_action 再更新 ``self._phase``):
        认出 ``FIGHT_PERIOD`` 的那次调用, 其 poll_action 是在**当前 phase
        仍为 FORMATION** 时构造的; 真正命中 ``FIGHT_PERIOD`` 分支的是
        **下一次**调用 (认出 RESULT, 但出发态是 FIGHT_PERIOD)。
        """
        _, recognizer = run_fight(engine, SIMPLE_EVENT_PATH, monkeypatch)
        idx = SIMPLE_EVENT_PATH.index(CombatPhase.FIGHT_PERIOD) + 1
        poll = recognizer.calls[idx]['poll_action']
        assert poll is not None, 'FIGHT_PERIOD 等待期应有 poll_action'

        ctrl = MagicMock()
        engine._device = ctrl
        poll(MagicMock())
        assert ctrl.click.call_count == 0, (
            'FIGHT_PERIOD 等待期不应点击 —— 结算安全位是结算期的事, '
            '此处盲点无意义(引擎该分支就是 sleep)'
        )

    def test_safe_zone_avoids_formation_button_row(self) -> None:
        """「结算安全位的 x 必须避开阵型按钮列 (x 0.45~0.97)。

        阵型选择界面的「单横阵」贴到屏幕底缘, 底缘中部点击会误触它 ——
        2026-10-10 用户指出后把 ``SETTLE_SKIP`` 的 x 从 0.5 改到 0.26。
        """
        from autowsgr.combat.actions import Coords

        x, y = Coords.SETTLE_SKIP
        assert x < 0.45, f'SETTLE_SKIP.x={x} 落在阵型按钮列区间内, 会误触「单横阵」'
        assert 0.9 < y <= 1.0, f'SETTLE_SKIP.y={y} 应在屏幕底缘安全带内'

    def test_settle_skip_only_used_by_settlement_handler(self) -> None:
        """「安全位点击只允许出现在结算处理器里。

        它是「点掉当前层」的推进手段; 若被挪到战斗动画期使用, 语义就错了
        (动画期根本没有可点掉的层)。
        """
        import ast
        import pathlib

        engine_src = pathlib.Path('autowsgr/combat/engine.py').read_text(encoding='utf-8')
        assert 'click_settlement_skip' not in engine_src, (
            '结算安全位点击不应出现在 engine.py —— 它属于处理器层'
        )


# ══════════════════════════════════════════════
# 5. 节点计数与事件历史
# ══════════════════════════════════════════════


class TestNodeCountingAndHistory:
    def test_single_node_counts_one(self, engine, monkeypatch) -> None:
        """单节点路径 → node_count == 1 (在 PROCEED 时累加)。"""
        result, _ = run_fight(engine, SIMPLE_EVENT_PATH, monkeypatch)
        assert result.node_count == 1

    def test_history_records_auto_return_on_end_phase(
        self, engine, monkeypatch
    ) -> None:
        """终态应记录 AUTO_RETURN 事件(供触发器判定成功)。"""
        result, _ = run_fight(engine, SIMPLE_EVENT_PATH, monkeypatch)
        events = _history_types(result)
        assert EventType.AUTO_RETURN in events, f'历史里应有 AUTO_RETURN, 实际={events}'

    def test_history_records_proceed_decision(self, engine, monkeypatch) -> None:
        """PROCEED 处理器应记录一条 PROCEED 事件。"""
        result, _ = run_fight(engine, SIMPLE_EVENT_PATH, monkeypatch)
        events = _history_types(result)
        assert EventType.PROCEED in events, f'历史里应有 PROCEED, 实际={events}'

    def test_multi_node_counts_correctly(self, monkeypatch) -> None:
        """三节点路径(连续打两个节点再回港)→ node_count == 2。"""
        # 构造 A→C→D→终态: 第二个 PROCEED 选择回港(直连 EVENT_MAP_PAGE)
        multi = [
            CombatPhase.FIGHT_CONDITION,
            CombatPhase.SPOT_ENEMY_SUCCESS,
            CombatPhase.FORMATION,
            CombatPhase.FIGHT_PERIOD,
            CombatPhase.RESULT,
            CombatPhase.PROCEED,      # 节点A 前进 -> node_count=1
            CombatPhase.FIGHT_CONDITION,
            CombatPhase.SPOT_ENEMY_SUCCESS,
            CombatPhase.FORMATION,
            CombatPhase.FIGHT_PERIOD,
            CombatPhase.RESULT,
            CombatPhase.PROCEED,      # 节点C 回港 -> node_count=2
            CombatPhase.EVENT_MAP_PAGE,
        ]
        engine = make_engine()
        result, _ = run_fight(engine, multi, monkeypatch)
        assert result.flag is ConditionFlag.OPERATION_SUCCESS
        assert result.node_count == 2, f'两节点应累计为 2, 实际={result.node_count}'


# ══════════════════════════════════════════════
# 6. 终止态分发 (不经 _step mock)
# ══════════════════════════════════════════════


def make_retreat_plan() -> CombatPlan:
    """配置了「索敌失败即撤退」的活动战计划。

    直接用 YAML 形态的 ``enemy_rules``(与 ``2-1捞胖次.yaml`` 等真实计划同构),
    而非手工构造 :class:`RuleEngine` —— 这样也顺带验证了
    ``plan.from_dict → NodeDecision.from_node_config → RuleEngine`` 这条
    配置解析链路本身。

    规则选``ALL <= 6`` : 无条件成立。**不能**写 ``CV >= 1`` 之类依赖敌舰
    编成的条件 —— 脚本化环境下截图是纯黑图, 敌舰识别结果为 ``{'ALL': 0}``,
    条件永远不成立(实测踩过, 表现为「规则没触发」而非报错, 很隐蔽)。
    本文件关注的是「规则判定 → handler 撤退 → fight() 返回 SKIP_FIGHT」
    这条链路, 不重复验证条件求值本身(那是 ``rules`` 模块的单元测试职责)。
    """
    plan = CombatPlan.from_dict(
        {
            'name': 't',
            'chapter': 'H',
            'map': 5,
            'event': '20260930',
            'node_defaults': {'enemy_rules': [['ALL <= 6', 'retreat']]},
        }
    )
    plan.mode = CombatMode.EVENT
    return plan


class TestTerminalStates:
    def test_rule_retreat_ends_with_skip_fight(self, monkeypatch) -> None:
        """规则判定撤退 → SKIP_FIGHT (不是 success)。

        实机 2026-10-08: 撤退轮曾冒充 success, 导致触发器误判达标、
        空转到永远打不完。这里用**真实规则引擎**驱动, 而非 mock 掉 handler
        —— 顺带验证「YAML 规则 → RuleEngine → 处理器 → fight() 终止态」
        整条链路。
        """
        retreat = [
            CombatPhase.FIGHT_CONDITION,
            CombatPhase.SPOT_ENEMY_SUCCESS,   # handler 按 enemy_rules 决定撤退
            CombatPhase.EVENT_MAP_PAGE,
        ]
        engine = make_engine()
        result, _ = run_fight(engine, retreat, monkeypatch, plan=make_retreat_plan())
        assert result.flag is ConditionFlag.SKIP_FIGHT

    def test_retreat_does_not_record_auto_return(self, monkeypatch) -> None:
        """撤退轮不应记 AUTO_RETURN (那是「打完回港」的标志)。"""
        retreat = [
            CombatPhase.FIGHT_CONDITION,
            CombatPhase.SPOT_ENEMY_SUCCESS,
            CombatPhase.EVENT_MAP_PAGE,
        ]
        engine = make_engine()
        result, _ = run_fight(engine, retreat, monkeypatch, plan=make_retreat_plan())
        assert EventType.AUTO_RETURN not in _history_types(result)

    def test_retreat_records_spot_enemy_action(self, monkeypatch) -> None:
        """撤退应在历史里留痕(便于实机排查为什么撤)。"""
        retreat = [
            CombatPhase.FIGHT_CONDITION,
            CombatPhase.SPOT_ENEMY_SUCCESS,
            CombatPhase.EVENT_MAP_PAGE,
        ]
        engine = make_engine()
        result, _ = run_fight(engine, retreat, monkeypatch, plan=make_retreat_plan())
        spot = [e for e in result.history.events if e.event_type is EventType.SPOT_ENEMY]
        assert spot, '撤退应记录 SPOT_ENEMY 事件'
        assert spot[-1].action == '撤退', f'动作应为撤退, 实际={spot[-1].action}'

    def test_dock_full_ends_with_dock_full_flag(self, monkeypatch) -> None:
        """船坞已满 → DOCK_FULL, 不记 success。"""
        dock = [
            CombatPhase.FIGHT_CONDITION,
            CombatPhase.DOCK_FULL,
        ]
        engine = make_engine()
        result, _ = run_fight(engine, dock, monkeypatch)
        assert result.flag is ConditionFlag.DOCK_FULL


# ══════════════════════════════════════════════
# 7. 脚本耗尽时必须报错 (而非静默通过)
# ══════════════════════════════════════════════


class TestScriptExhaustion:
    def test_engine_asking_beyond_script_raises(self, engine, monkeypatch) -> None:
        """脚本耗尽而引擎仍在等待 → 明确失败。

        防止「测试其实没跑到某段代码却绿了」的假阳性。
        """
        with pytest.raises(AssertionError, match='脚本已耗尽'):
            run_fight(engine, [CombatPhase.FIGHT_CONDITION], monkeypatch)
