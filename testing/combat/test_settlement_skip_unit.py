"""结算盲点穿行 (处理器层) + 索敌按需采集 的无设备单元测试 (2026-10-10 提速优化)。

设计边界 (实机 2026-10-10 回归教训): 战斗动画等待期 (FIGHT_PERIOD/
NIGHT_PROMPT) **绝不盲点** —— H5-ACD 三点进入即夜战 (无追击选项), 等待期
盲点会把战斗/结算整段点穿, 且 PROCEED 对话框按钮 (y≈0.648) 与夜战提示
同位, 可被 NIGHT_PROMPT 模板误认。结算三层 (战果/经验/掉落) 的快速跳过
由处理器层 handlers._blind_skip_settlement 在 RESULT 识别触发后完成。

覆盖:
- engine 等待期 poll: 任何模式 FIGHT_PERIOD 均不点击 (回归保护)。
- state.py 转移图: 慢速/SINGLE 模式 RESULT 路径不变, 快速模式 RESULT
  条目保留 (错误恢复 resume 用)。
- handlers._blind_skip_settlement: 安全位持续点击, 命中合法落点即停,
  超时交回引擎。
- handlers._handle_spot_enemy 按需采集: 无规则计划跳过敌编成/阵型 OCR。
- recognizer.wait_for_phase timeout 覆盖参数。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from autowsgr.combat.actions import Coords
from autowsgr.combat.engine import CombatEngine
from autowsgr.combat.plan import CombatMode, CombatPlan, NodeDecision
from autowsgr.combat.recognizer import CombatRecognitionTimeoutError, CombatRecognizer
from autowsgr.combat.state import (
    CombatPhase,
    ModeCategory,
    build_transitions,
    resolve_successors,
)
from autowsgr.types import ConditionFlag


_W, _H = 1280, 720
_SETTLE_SKIP = Coords.SETTLE_SKIP


def _make_plan(mode: CombatMode = CombatMode.EVENT) -> CombatPlan:
    plan = CombatPlan.from_dict(
        {'name': 't', 'chapter': 'H', 'map': 5, 'event': '20260930'}
    )
    plan.mode = mode
    return plan


def _make_engine(plan: CombatPlan, clicks: list | None = None) -> CombatEngine:
    device = MagicMock()
    if clicks is not None:
        device.click.side_effect = lambda x, y, **k: clicks.append((x, y))
    ctx = SimpleNamespace(
        ctrl=device,
        ocr=None,
        stop_event=threading.Event(),
    )
    engine = CombatEngine(ctx)
    engine._plan = plan
    return engine


# ═══════════════════════════════════════════════════════════════════════════════
# 等待期零点击 (engine.py) — 回归保护
# ═══════════════════════════════════════════════════════════════════════════════


class TestFightPeriodPollNoClick:
    """战斗动画等待期绝不盲点 — 任何模式 FIGHT_PERIOD 均 sleep 轮询。

    实机 2026-10-10: 等待期盲点以 ~6.5 次/s 轰击夜战动画, 把战斗/结算
    整段点穿; PROCEED 对话框 (按钮 y=0.648 与夜战提示同位) 被误认成
    夜战提示后点「追击」实为「前进」。结算穿行只允许在 RESULT 触发后
    的处理器层 (handlers._blind_skip_settlement) 发生。
    """

    def test_fast_map_poll_does_not_click(self, monkeypatch: pytest.MonkeyPatch):
        """快速 MAP 模式 FIGHT_PERIOD 等待期 sleep 轮询, 零点击。"""
        monkeypatch.setattr('autowsgr.combat.engine.time.sleep', lambda _s: None)
        clicks: list[tuple[float, float]] = []
        engine = _make_engine(_make_plan(), clicks)
        engine._phase = CombatPhase.FIGHT_PERIOD

        poll = engine._get_poll_action(CombatPhase.FIGHT_PERIOD)
        assert poll is not None
        poll(np.zeros((_H, _W, 3), dtype=np.uint8))

        assert clicks == []

    def test_slow_map_poll_does_not_click(self, monkeypatch: pytest.MonkeyPatch):
        """慢速采集模式等待期同样零点击。"""
        monkeypatch.setattr('autowsgr.combat.engine.time.sleep', lambda _s: None)
        clicks: list[tuple[float, float]] = []
        plan = _make_plan()
        plan.collect_result_info = True
        engine = _make_engine(plan, clicks)
        engine._phase = CombatPhase.FIGHT_PERIOD

        poll = engine._get_poll_action(CombatPhase.FIGHT_PERIOD)
        assert poll is not None
        poll(np.zeros((_H, _W, 3), dtype=np.uint8))

        assert clicks == []

    def test_single_mode_poll_does_not_click(self, monkeypatch: pytest.MonkeyPatch):
        """SINGLE 模式 (战役/演习) 等待期零点击, 保持原 sleep 轮询。"""
        monkeypatch.setattr('autowsgr.combat.engine.time.sleep', lambda _s: None)
        clicks: list[tuple[float, float]] = []
        engine = _make_engine(_make_plan(mode=CombatMode.EXERCISE), clicks)
        engine._phase = CombatPhase.FIGHT_PERIOD

        poll = engine._get_poll_action(CombatPhase.FIGHT_PERIOD)
        assert poll is not None
        poll(np.zeros((_H, _W, 3), dtype=np.uint8))

        assert clicks == []

    def test_night_prompt_has_no_poll_action(self):
        """NIGHT_PROMPT (夜战提示/同位的 PROCEED 对话框) 无 poll 动作。

        该阶段与 PROCEED 对话框按钮同位, 任何自动点击都有误触风险,
        必须纯等待识别。
        """
        engine = _make_engine(_make_plan())
        engine._phase = CombatPhase.NIGHT_PROMPT

        poll = engine._get_poll_action(CombatPhase.NIGHT_PROMPT)

        assert poll is None

    def test_routing_phase_does_not_click_safe_spot(self):
        """地图行军阶段走加速点击, 不得命中结算安全位 (干扰节点追踪)。"""
        clicks: list[tuple[float, float]] = []
        engine = _make_engine(_make_plan(), clicks)
        engine._phase = CombatPhase.PROCEED

        poll = engine._get_poll_action(CombatPhase.PROCEED)
        if poll is not None:
            poll(np.zeros((_H, _W, 3), dtype=np.uint8))
        assert _SETTLE_SKIP not in clicks


# ═══════════════════════════════════════════════════════════════════════════════
# 转移图 (state.py): 结算等待路径
# ═══════════════════════════════════════════════════════════════════════════════


class TestSettlementPhasePaths:
    """结算三层经状态机等待进入 RESULT; 快速模式 RESULT 条目保留供恢复。"""

    def test_slow_mode_keeps_result_path(self):
        """慢速 (collect_result_info=True): FIGHT_PERIOD/NIGHT_PROMPT 进 RESULT。"""
        t = build_transitions(
            ModeCategory.MAP, CombatPhase.EVENT_MAP_PAGE, collect_result_info=True
        )
        assert resolve_successors(t, CombatPhase.FIGHT_PERIOD, '') == [
            CombatPhase.NIGHT_PROMPT,
            CombatPhase.RESULT,
        ]
        assert resolve_successors(t, CombatPhase.NIGHT_PROMPT, 'yes') == [CombatPhase.RESULT]

    def test_single_mode_keeps_result_path(self):
        t = build_transitions(ModeCategory.SINGLE, CombatPhase.EXERCISE_PAGE)
        assert CombatPhase.RESULT in resolve_successors(t, CombatPhase.FIGHT_PERIOD, '')

    def test_result_entry_kept_for_recovery(self):
        """快速模式 RESULT 转移条目保留: 错误恢复落到结算页可从该状态继续。"""
        t = build_transitions(ModeCategory.MAP, CombatPhase.EVENT_MAP_PAGE)
        successors = resolve_successors(t, CombatPhase.RESULT, '')
        assert CombatPhase.PROCEED in successors
        assert CombatPhase.EVENT_MAP_PAGE in successors
        assert CombatPhase.GET_SHIP in successors


# ═══════════════════════════════════════════════════════════════════════════════
# 处理器层结算穿行 (handlers._blind_skip_settlement)
# ═══════════════════════════════════════════════════════════════════════════════


class TestBlindSkipSettlement:
    """RESULT 触发后的结算三层盲点穿行 (安全位持续点击)。"""

    def _make_host(self, clicks: list[tuple[float, float]]) -> CombatEngine:
        engine = _make_engine(_make_plan(), clicks)
        engine._recognizer = MagicMock()
        engine._device.screenshot.return_value = np.zeros((_H, _W, 3), dtype=np.uint8)
        return engine

    def test_first_click_hits_and_stops(self):
        """命中落点 (PROCEED) 即停: 首击即中则只点一次, 坐标为安全位。"""
        clicks: list[tuple[float, float]] = []
        engine = self._make_host(clicks)
        engine._recognizer.identify_current.return_value = CombatPhase.PROCEED

        engine._blind_skip_settlement()

        assert clicks == [_SETTLE_SKIP]
        engine._device.screenshot.assert_called_once()

    def test_clicks_through_gradient_frames(self, monkeypatch: pytest.MonkeyPatch):
        """页面渐变/动画帧识别不到时继续点击, 命中旗舰大破页停止。"""
        monkeypatch.setattr('autowsgr.combat.handlers.time.sleep', lambda _s: None)
        clicks: list[tuple[float, float]] = []
        engine = self._make_host(clicks)
        engine._recognizer.identify_current.side_effect = [
            None,
            None,
            CombatPhase.FLAGSHIP_SEVERE_DAMAGE,
        ]

        engine._blind_skip_settlement()

        assert len(clicks) == 3
        assert set(clicks) == {_SETTLE_SKIP}  # 恒定安全位, 无漂移

    def test_stops_configuration(self, monkeypatch: pytest.MonkeyPatch):
        """stops = 落点集 (PROCEED/旗舰大破/终态页); 结算层是点穿对象。

        GET_SHIP / EXP_SETTLEMENT / RESULT 不在停止集 —— 点击要穿过它们
        直到对话框或终态页, 交回引擎按当前页接管。
        """
        monkeypatch.setattr('autowsgr.combat.handlers.time.sleep', lambda _s: None)
        clicks: list[tuple[float, float]] = []
        engine = self._make_host(clicks)
        engine._recognizer.identify_current.side_effect = [None, engine._plan.end_phase]

        engine._blind_skip_settlement()

        assert len(clicks) == 2  # 第 2 轮命中终态页停止
        stops_used = engine._recognizer.identify_current.call_args[0][1]
        assert CombatPhase.PROCEED in stops_used
        assert CombatPhase.FLAGSHIP_SEVERE_DAMAGE in stops_used
        assert engine._plan.end_phase in stops_used
        assert CombatPhase.RESULT not in stops_used
        assert CombatPhase.EXP_SETTLEMENT not in stops_used
        assert CombatPhase.GET_SHIP not in stops_used

    def test_timeout_zero_returns_without_click(self):
        """超时上限为 0 时直接交回引擎, 不点击不抛异常。"""
        clicks: list[tuple[float, float]] = []
        engine = self._make_host(clicks)

        engine._blind_skip_settlement(timeout=0.0)

        assert clicks == []
        engine._device.screenshot.assert_not_called()

    def test_deadline_exits_loop(self, monkeypatch: pytest.MonkeyPatch):
        """循环内 deadline 到期退出: 不无限点击, warning 后交回引擎。"""
        # monotonic 调用序: deadline 计算 → while#1 (通过) → while#2 (超时退出)
        clock = iter([0.0, 0.0, 999.0])
        monkeypatch.setattr(
            'autowsgr.combat.handlers.time.monotonic', lambda: next(clock)
        )
        monkeypatch.setattr('autowsgr.combat.handlers.time.sleep', lambda _s: None)
        clicks: list[tuple[float, float]] = []
        engine = self._make_host(clicks)
        engine._recognizer.identify_current.return_value = None

        engine._blind_skip_settlement(timeout=20.0)

        assert len(clicks) == 1  # deadline 前恰好一次点击


# ═══════════════════════════════════════════════════════════════════════════════
# 错误恢复 (engine.py)
# ═══════════════════════════════════════════════════════════════════════════════


class TestRecoveryIncludesSettlement:
    """错误恢复候选含结算三层: 超时落在结算页时可续跑而非 SL 重开。"""

    def test_recovery_resumes_from_result_page(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr('autowsgr.combat.engine.time.sleep', lambda _s: None)
        engine = _make_engine(_make_plan())
        engine._phase = CombatPhase.NIGHT_PROMPT
        engine._last_action = 'yes'
        engine._device.screenshot.return_value = np.zeros((_H, _W, 3), dtype=np.uint8)
        engine._recognizer = MagicMock()
        engine._recognizer.identify_current.return_value = CombatPhase.RESULT

        assert engine._try_recovery() is True
        assert engine._phase is CombatPhase.RESULT


# ═══════════════════════════════════════════════════════════════════════════════
# recognizer.wait_for_phase 的 timeout 覆盖
# ═══════════════════════════════════════════════════════════════════════════════


class TestWaitForPhaseTimeoutOverride:
    def test_timeout_override_takes_effect(self):
        """候选默认超时 7.5s, 传 timeout=0.25 应在亚秒级超时抛错。"""
        device = MagicMock()
        device.screenshot.return_value = np.full((_H, _W, 3), 255, dtype=np.uint8)
        ctx = SimpleNamespace(ctrl=device, stop_event=threading.Event())
        recognizer = CombatRecognizer(ctx)

        with pytest.raises(CombatRecognitionTimeoutError) as exc_info:
            recognizer.wait_for_phase([CombatPhase.PROCEED], timeout=0.25)

        assert '0.2s' in str(exc_info.value)  # 错误信息携带覆盖后的超时


# ═══════════════════════════════════════════════════════════════════════════════
# 索敌按需采集 (handlers.py)
# ═══════════════════════════════════════════════════════════════════════════════


class TestSpotEnemyOnDemandScan:
    """无敌舰/阵型规则的计划跳过敌编成 OCR; 有规则才采集。"""

    @staticmethod
    def _patch_flow(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
        from autowsgr.combat import handlers as handlers_mod

        calls = {'info': 0, 'formation': 0, 'enter': 0}
        monkeypatch.setattr(
            handlers_mod,
            'get_enemy_info',
            lambda *_a, **_k: (calls.__setitem__('info', calls['info'] + 1), {})[1],
        )
        monkeypatch.setattr(
            handlers_mod,
            'get_enemy_formation',
            lambda *_a, **_k: (calls.__setitem__('formation', calls['formation'] + 1), '')[1],
        )
        monkeypatch.setattr(
            handlers_mod,
            'click_enter_fight',
            lambda *_a, **_k: calls.__setitem__('enter', calls['enter'] + 1),
        )
        monkeypatch.setattr(handlers_mod, 'image_exist', lambda *_a, **_k: False)
        return calls

    def test_ruleless_plan_skips_ocr(self, monkeypatch: pytest.MonkeyPatch):
        calls = self._patch_flow(monkeypatch)
        engine = _make_engine(_make_plan())
        engine._node = 'A'
        engine._last_action = 'yes'

        result = engine._handle_spot_enemy()

        assert result is ConditionFlag.FIGHT_CONTINUE
        assert calls['info'] == 0  # 敌编成识别被跳过
        assert calls['formation'] == 0  # 阵型 OCR 被跳过
        assert calls['enter'] == 1  # 直接进战斗

    def test_enemy_rules_trigger_scan(self, monkeypatch: pytest.MonkeyPatch):
        calls = self._patch_flow(monkeypatch)
        engine = _make_engine(_make_plan())
        engine._node = 'A'
        engine._last_action = 'yes'
        engine._plan.nodes['A'] = NodeDecision(
            enemy_rules=MagicMock(evaluate=MagicMock(return_value=None))
        )

        engine._handle_spot_enemy()

        assert calls['info'] == 1
        assert calls['formation'] == 0  # 无阵型规则 → 阵型 OCR 仍跳过

    def test_formation_rules_trigger_scan(self, monkeypatch: pytest.MonkeyPatch):
        calls = self._patch_flow(monkeypatch)
        engine = _make_engine(_make_plan())
        engine._node = 'A'
        engine._last_action = 'yes'
        engine._plan.nodes['A'] = NodeDecision(
            formation_rules=MagicMock(evaluate_formation=MagicMock(return_value=None))
        )

        engine._handle_spot_enemy()

        assert calls['formation'] == 1
        assert calls['info'] == 0


class TestRetreatFlag:
    """撤退 (白名单外/规则撤退) 返回 SKIP_FIGHT, 不得冒充成功打完。

    实机 2026-10-08: 出征后节点追踪失败 (节点=0) 触发撤退, 返回
    FIGHT_END 被引擎记成 OPERATION_SUCCESS, 撤退轮计入成功数。
    """

    def test_whitelist_retreat_returns_skip_fight(self, monkeypatch: pytest.MonkeyPatch):
        from autowsgr.combat import handlers as handlers_mod

        monkeypatch.setattr(handlers_mod, 'click_retreat', lambda *_a, **_k: None)
        monkeypatch.setattr(handlers_mod, 'image_exist', lambda *_a, **_k: False)
        monkeypatch.setattr(handlers_mod, 'click_enter_fight', lambda *_a, **_k: None)
        plan = _make_plan()
        plan.selected_nodes = ['A', 'C', 'D']
        engine = _make_engine(plan)
        engine._node = '0'  # 初始节点 (追踪未更新), 白名单外
        engine._last_action = 'yes'

        result = engine._handle_spot_enemy()

        assert result is ConditionFlag.SKIP_FIGHT
