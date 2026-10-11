"""战斗引擎熔断护栏的无设备单元测试。

背景 (P0-1): ``engine.fight()`` 的 ``while True`` 原先只在 ``_step()`` 抛
异常或返回终止 flag 时退出。其中「识别超时 → 恢复成功 → 继续 → 又超时」
这条路径是**裸 ``continue``**：若页面稳定停在某个后继态 (典型如 PROCEED
模板误命中但点击无效), 每次都能被恢复判据认出来, 于是无限自我循环,
永不退出 —— 对 7×24 挂机而言等于脚本报废到人工干预为止。

实测 ``grep -nE "retry|attempt|max_recover" engine.py`` 在修复前零命中。

本次加入两道护栏 (对标 Alas device.py 的 stuck_timer 双计时器):
- ``max_recovery_attempts`` 封住恢复死循环
- ``max_steps`` 封住「识别成功但页面不推进」的死循环

覆盖:
- 恢复次数上限: 超限判 SL, 不再无限 continue
- 恢复计数在成功推进后归零 (不会跨正常步累积误伤)
- 步数上限: 超限判 SL
- 两个上限均可通过构造函数覆盖
- 计数器在每次 fight() 开头重置 (复用于下一场战斗)
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from autowsgr.combat.engine import (
    DEFAULT_MAX_RECOVERY_ATTEMPTS,
    DEFAULT_MAX_STEPS,
    CombatEngine,
)
from autowsgr.combat.history import CombatResult
from autowsgr.combat.plan import CombatMode, CombatPlan
from autowsgr.combat.recognizer import CombatRecognitionTimeoutError
from autowsgr.combat.state import CombatPhase
from autowsgr.types import ConditionFlag


# ═══════════════════════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════════════════════


def _make_plan(mode: CombatMode = CombatMode.EVENT) -> CombatPlan:
    plan = CombatPlan.from_dict(
        {'name': 't', 'chapter': 'H', 'map': 5, 'event': '20260930'}
    )
    plan.mode = mode
    return plan


def _make_engine(**kwargs) -> CombatEngine:
    """构造一个不接真机的引擎 (device 全 MagicMock)。"""
    ctx = SimpleNamespace(
        ctrl=MagicMock(),
        ocr=None,
        stop_event=threading.Event(),
    )
    return CombatEngine(ctx, **kwargs)


def _patch_no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """消除 _try_recovery 里的 3秒 sleep, 避免测试真实等待。"""
    monkeypatch.setattr('autowsgr.combat.engine.time.sleep', lambda _s: None)


def _stub_fight_setup(monkeypatch: pytest.MonkeyPatch, engine: CombatEngine) -> None:
    """跳过 fight() 中与设备/地图数据耦合的初始化。

    保留 _reset() (它重置熔断计数器, 正是被测对象), 跳过节点追踪器加载
    与 CombatRecognizer 构造 (两者都需要真实上下文或文件系统)。
    """

    def _fake_reset() -> None:
        engine._history.reset()
        engine._node = '0'
        engine._node_count = 0
        engine._formation_by_rule = None
        engine._recovery_attempts = 0
        engine._steps = 0
        engine._phase = CombatPhase.START_FIGHT
        engine._last_action = ''

    monkeypatch.setattr(engine, '_reset', _fake_reset)
    monkeypatch.setattr(
        'autowsgr.combat.engine.CombatRecognizer',
        lambda *_a, **_k: MagicMock(),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 护栏 1: 恢复次数上限
# ═══════════════════════════════════════════════════════════════════════════════


class TestRecoveryAttemptCap:
    """连续恢复达到上限后必须判 SL, 而不是无限 continue。"""

    def test_recovery_loop_terminates_with_sl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """核心回归: 每次超时都能恢复成功, 但达到上限后必须退出。

        构造最恶劣情形 —— ``_try_recovery()`` **永远返回 True** (页面稳定
        停在一个后继态)。修复前这里是无界 while 循环, 测试会挂死;
        现在应在 ``max_recovery_attempts + 1`` 次后判 SL 返回。
        """
        _patch_no_sleep(monkeypatch)

        engine = _make_engine(max_recovery_attempts=3)
        _stub_fight_setup(monkeypatch, engine)

        # _step 永远超时 → 触发恢复分支
        monkeypatch.setattr(
            engine,
            '_step',
            MagicMock(side_effect=CombatRecognitionTimeoutError('卡在某页')),
        )
        # _try_recovery 永远「成功」—— 这正是死循环的成因
        monkeypatch.setattr(engine, '_try_recovery', MagicMock(return_value=True))

        result = engine.fight(_make_plan(), initial_ship_stats=None)

        assert result.flag is ConditionFlag.SL
        # 上限 3 → 第 4 次超时即放弃
        assert engine._recovery_attempts == 4
        # 恢复被调用 3 次 (第 4 次在检查上限后未调用)
        assert engine._try_recovery.call_count == 3  # type: ignore[attr-defined]

    def test_recovery_within_cap_still_continues(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未达上限时恢复仍然生效 —— 护栏不能误伤正常恢复路径。"""
        _patch_no_sleep(monkeypatch)

        engine = _make_engine(max_recovery_attempts=5)
        _stub_fight_setup(monkeypatch, engine)

        calls = {'n': 0}

        def _fake_step():
            calls['n'] += 1
            if calls['n'] <= 3:
                # 前 3 次超时, 每次都恢复成功
                raise CombatRecognitionTimeoutError('暂时超时')
            # 第 4 次正常结束
            return ConditionFlag.FIGHT_END

        monkeypatch.setattr(engine, '_step', _fake_step)
        monkeypatch.setattr(engine, '_try_recovery', MagicMock(return_value=True))

        result = engine.fight(_make_plan(), initial_ship_stats=None)

        assert result.flag is ConditionFlag.OPERATION_SUCCESS
        assert engine._try_recovery.call_count == 3  # type: ignore[attr-defined]

    def test_recovery_counter_resets_on_progress(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """成功推进状态机后, 恢复计数必须归零。

        否则「超时→恢复→成功一步→再超时」的交替模式下, 计数会跨正常步
        累积, 最终误触发上限导致正常战斗被判 SL。
        """
        _patch_no_sleep(monkeypatch)

        engine = _make_engine(max_recovery_attempts=2)
        _stub_fight_setup(monkeypatch, engine)

        sequence = [
            CombatRecognitionTimeoutError('t1'),
            ConditionFlag.FIGHT_CONTINUE,  # 成功推进 → 计数归零
            CombatRecognitionTimeoutError('t2'),
            ConditionFlag.FIGHT_CONTINUE,  # 再次归零
            ConditionFlag.FIGHT_END,
        ]
        monkeypatch.setattr(
            engine,
            '_step',
            MagicMock(side_effect=sequence),
        )
        monkeypatch.setattr(engine, '_try_recovery', MagicMock(return_value=True))

        result = engine.fight(_make_plan(), initial_ship_stats=None)

        assert result.flag is ConditionFlag.OPERATION_SUCCESS
        # 两次超时各只用掉一次额度, 互相未累积
        assert engine._recovery_attempts == 0

    def test_failed_recovery_ends_immediately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """恢复失败 (认不出页面) 时立即判 SL, 不再重试。"""
        _patch_no_sleep(monkeypatch)

        engine = _make_engine(max_recovery_attempts=3)
        _stub_fight_setup(monkeypatch, engine)

        monkeypatch.setattr(
            engine,
            '_step',
            MagicMock(side_effect=CombatRecognitionTimeoutError('认不出')),
        )
        monkeypatch.setattr(engine, '_try_recovery', MagicMock(return_value=False))

        result = engine.fight(_make_plan(), initial_ship_stats=None)

        assert result.flag is ConditionFlag.SL
        assert engine._try_recovery.call_count == 1  # type: ignore[attr-defined]


class TestSLCallbackInjection:
    """SL 的恢复动作必须走**注入的回调**, 而不是引擎自己 import ops。

    迁移前是 ``from autowsgr.ops import restart_game`` (函数内 import), 使
    ``combat`` 反向依赖 ``ops``, 且 SL 路径无法脱离 ops 环境测试
    (``fight()`` 此前零覆盖)。改为注入后, 本层可独立构造与测试。
    """

    def test_on_sl_is_called_on_sl_decision(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """战斗判 SL → 调用注入的 on_sl。"""
        calls: list[int] = []

        engine = _make_engine(on_sl=lambda: calls.append(1))
        _stub_fight_setup(monkeypatch, engine)
        monkeypatch.setattr(
            engine, '_step', MagicMock(return_value=ConditionFlag.SL)
        )

        result = engine.fight(_make_plan(), initial_ship_stats=None)

        assert result.flag is ConditionFlag.SL
        assert calls == [1], '注入的 on_sl 应被调用恰好一次'

    def test_on_sl_not_called_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """正常结束不应触发 on_sl。"""
        calls: list[int] = []

        engine = _make_engine(on_sl=lambda: calls.append(1))
        _stub_fight_setup(monkeypatch, engine)
        monkeypatch.setattr(
            engine, '_step', MagicMock(return_value=ConditionFlag.FIGHT_END)
        )

        engine.fight(_make_plan(), initial_ship_stats=None)

        assert calls == []

    def test_sl_without_callback_still_reports(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """未注入 on_sl 时 SL 仍要正常记结果 (只是不重启游戏)。

        ``_make_engine`` 默认不注入回调 —— 这条保证引擎可独立测试。
        """
        engine = _make_engine()
        _stub_fight_setup(monkeypatch, engine)
        monkeypatch.setattr(
            engine, '_step', MagicMock(return_value=ConditionFlag.SL)
        )

        result = engine.fight(_make_plan(), initial_ship_stats=None)

        assert result.flag is ConditionFlag.SL

    def test_combat_layer_does_not_import_ops(self) -> None:
        """静态检查: ``combat`` 包不得 import ``ops`` (排除注释/文档)。

        这是分层铁律 —— ``combat`` 位于 ``ops`` 之下, 反向依赖会让战斗域
        无法脱离编排层独立运行与测试。
        """
        import re
        from pathlib import Path

        import autowsgr.combat as combat_pkg

        offenders: list[str] = []
        pattern = re.compile(r'^\s*(?:from|import)\s+autowsgr\.ops')
        for py in Path(combat_pkg.__file__).parent.glob('*.py'):
            for lineno, line in enumerate(
                py.read_text(encoding='utf-8').splitlines(), 1
            ):
                if pattern.match(line):
                    offenders.append(f'{py.name}:{lineno}: {line.strip()}')

        assert not offenders, (
            'combat 层不应依赖 ops (应改用回调注入):\n' + '\n'.join(offenders)
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 护栏 2: 步数上限
# ═══════════════════════════════════════════════════════════════════════════════


class TestStepCap:
    """识别始终成功但页面不推进时, 步数上限必须兜底退出。"""

    def test_step_loop_terminates_with_sl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """核心回归: _step 永远返回 FIGHT_CONTINUE (状态机原地打转)。

        修复前这是另一个无界循环 (与恢复死循环不同, 这条根本不经过
        ``except`` 分支)。现在应在 ``max_steps`` 步后判 SL。
        """
        _patch_no_sleep(monkeypatch)

        engine = _make_engine(max_steps=25)
        _stub_fight_setup(monkeypatch, engine)

        monkeypatch.setattr(
            engine,
            '_step',
            MagicMock(return_value=ConditionFlag.FIGHT_CONTINUE),
        )

        result = engine.fight(_make_plan(), initial_ship_stats=None)

        assert result.flag is ConditionFlag.SL
        assert engine._steps == 26  # 上限 25, 第 26 步触发

    def test_step_cap_not_tripped_by_normal_battle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """正常战斗 (12 步) 远低于默认上限, 护栏不得误触发。"""
        _patch_no_sleep(monkeypatch)

        engine = _make_engine()  # 默认 200 步上限
        _stub_fight_setup(monkeypatch, engine)

        sequence = [ConditionFlag.FIGHT_CONTINUE] * 11 + [ConditionFlag.FIGHT_END]
        monkeypatch.setattr(engine, '_step', MagicMock(side_effect=sequence))

        result = engine.fight(_make_plan(), initial_ship_stats=None)

        assert result.flag is ConditionFlag.OPERATION_SUCCESS
        assert engine._steps == 12

    def test_step_counter_resets_between_fights(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """计数器在每次 fight() 开头重置 —— 引擎实例可复用于多场战斗。

        调度器复用同一引擎打多轮时, 若不重置, 第二场会带着第一场的步数
        开跑, 累积到上限后被误判 SL。
        """
        _patch_no_sleep(monkeypatch)

        engine = _make_engine(max_steps=100)
        _stub_fight_setup(monkeypatch, engine)

        # 第一场: 走 10 步后结束
        first = [ConditionFlag.FIGHT_CONTINUE] * 9 + [ConditionFlag.FIGHT_END]
        monkeypatch.setattr(engine, '_step', MagicMock(side_effect=first))
        r1 = engine.fight(_make_plan(), initial_ship_stats=None)
        assert r1.flag is ConditionFlag.OPERATION_SUCCESS
        assert engine._steps == 10

        # 第二场: 同样的 10 步应同样成功 (而非因累计 20 步而异常)
        monkeypatch.setattr(
            engine,
            '_step',
            MagicMock(side_effect=[ConditionFlag.FIGHT_CONTINUE] * 9 + [ConditionFlag.FIGHT_END]),
        )
        r2 = engine.fight(_make_plan(), initial_ship_stats=None)
        assert r2.flag is ConditionFlag.OPERATION_SUCCESS
        assert engine._steps == 10


# ═══════════════════════════════════════════════════════════════════════════════
# 默认值与可配置性
# ═══════════════════════════════════════════════════════════════════════════════


class TestCircuitBreakerDefaults:
    """护栏默认值与构造参数覆盖。"""

    def test_defaults_are_sane(self) -> None:
        """默认值需覆盖正常战斗 (8~14 步/节点) 且留足余量。"""
        assert DEFAULT_MAX_STEPS >= 50
        assert 1 <= DEFAULT_MAX_RECOVERY_ATTEMPTS <= 5

    def test_engine_uses_defaults(self) -> None:
        engine = _make_engine()
        assert engine._max_steps == DEFAULT_MAX_STEPS
        assert engine._max_recovery_attempts == DEFAULT_MAX_RECOVERY_ATTEMPTS

    def test_overrides_take_effect(self) -> None:
        engine = _make_engine(max_steps=7, max_recovery_attempts=2)
        assert engine._max_steps == 7
        assert engine._max_recovery_attempts == 2

    def test_stop_request_still_exits_immediately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """停止请求必须立刻退出, 不受步数上限影响 (用户按 Ctrl+C 要能马上停)。

        注意此��� FIGHT_END 而非 SL —— 战斗引擎把「主动停止」当作正常结束,
        由上层 ops 决定如何计入统计。
        """
        from autowsgr.combat.recognizer import CombatStopRequestedError

        engine = _make_engine(max_steps=1)  # 上限极低, 用于验证不被触发
        _stub_fight_setup(monkeypatch, engine)

        monkeypatch.setattr(
            engine,
            '_step',
            MagicMock(side_effect=CombatStopRequestedError('用户停止')),
        )

        result = engine.fight(_make_plan(), initial_ship_stats=None)

        assert result.flag is ConditionFlag.FIGHT_END