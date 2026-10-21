"""战斗状态处理器 — 各状态节点的决策逻辑。


每个 ``_handle_*`` 方法对应一个 :class:`~autowsgr.combat.state.CombatPhase`，
执行该阶段所需的决策和操作，并返回 :class:`~autowsgr.types.ConditionFlag`
指示引擎是否继续循环。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from autowsgr.combat.actions import (
    click_enter_fight,
    click_fight_condition,
    click_formation,
    click_image,
    click_night_battle,
    click_proceed,
    click_result,
    click_retreat,
    click_settlement_skip,
    click_skip_missile_animation,
    detect_result_grade,
    get_enemy_formation,
    get_enemy_info,
    image_exist,
)
from autowsgr.combat.recognition import detect_mvp
from autowsgr.image_resources import TemplateKey
from autowsgr.infra.logger import get_logger
from autowsgr.types import ConditionFlag, Formation, ShipDamageState
from autowsgr.vision import ImageChecker
from autowsgr.vision.waiting import wait_until_leaving

from .history import CombatEvent, CombatHistory, EventType, FightResult
from .plan import CombatMode, CombatPlan, NodeDecision
from .rules import RuleResult
from .state import CombatPhase


if TYPE_CHECKING:
    from autowsgr.combat.recognizer import CombatRecognizer
    from autowsgr.emulator import AndroidController
    from autowsgr.vision import OCREngine


_log = get_logger('combat')


# ── 状态 → 处理器方法名映射  ────────────────────────────────────────────────

_PHASE_HANDLERS: dict[CombatPhase, str] = {
    CombatPhase.FIGHT_CONDITION: '_handle_fight_condition',
    CombatPhase.SPOT_ENEMY_SUCCESS: '_handle_spot_enemy',
    CombatPhase.FORMATION: '_handle_formation',
    CombatPhase.MISSILE_ANIMATION: '_handle_missile_animation',
    CombatPhase.FIGHT_PERIOD: '_handle_fight_period',
    CombatPhase.NIGHT_PROMPT: '_handle_night_prompt',
    CombatPhase.RESULT: '_handle_result',
    CombatPhase.EXP_SETTLEMENT: '_handle_exp_settlement',
    CombatPhase.GET_SHIP: '_handle_get_ship',
    CombatPhase.PROCEED: '_handle_proceed',
    CombatPhase.FLAGSHIP_SEVERE_DAMAGE: '_handle_flagship_severe_damage',
    CombatPhase.DOCK_FULL: '_handle_dock_full',
}


class PhaseHandlersMixin:
    """战斗状态处理器 Mixin。

    为 ``CombatEngine`` 提供 ``_make_decision`` 及所有 ``_handle_*`` 方法。

    约定: 本 Mixin 假设宿主类具有以下属性::

        _device: AndroidController
        _plan: CombatPlan
        _ocr: OCREngine | None
        _node: str
        _last_action: str
        _ship_stats: list[ShipDamageState]
        _history: CombatHistory
        _node_count: int
        _formation_by_rule: Formation | None
        _recognizer: CombatRecognizer
    """

    # 类型提示 (供 IDE/mypy 在 Mixin 上下文使用)
    _device: AndroidController
    _plan: CombatPlan
    _ocr: OCREngine | None
    _node: str
    _last_action: str
    _ship_stats: list[ShipDamageState]
    _history: CombatHistory
    _recognizer: CombatRecognizer
    _node_count: int
    _formation_by_rule: Formation | None

    def _make_decision(self, phase: CombatPhase) -> ConditionFlag:
        """根据当前状态做出决策并执行操作。

        每个状态通过 ``_PHASE_HANDLERS`` 映射到对应的处理器方法；
        处理器执行完毕后，若当前状态是终止阶段则返回 ``FIGHT_END``。

        Parameters
        ----------
        phase:
            当前识别到的战斗状态。

        Returns
        -------
        ConditionFlag
        """
        # ── 派发处理器 ──
        handler_name = _PHASE_HANDLERS.get(phase)
        if handler_name is not None:
            result = getattr(self, handler_name)()
        else:
            result = ConditionFlag.FIGHT_CONTINUE

        # ── 终止态检查 ──
        if phase == self._plan.end_phase:
            self._history.add(
                CombatEvent(
                    event_type=EventType.AUTO_RETURN,
                    node=self._node,
                    action='正常',
                )
            )
            return ConditionFlag.FIGHT_END

        return result

    # ── 各状态处理器 ─────────────────────────────────────────────────────────

    def _handle_fight_condition(self) -> ConditionFlag:
        """处理战况选择。
        TODO: 需测试
        """
        condition = self._plan.fight_condition
        click_fight_condition(self._device, condition)
        self._last_action = str(condition.value)

        self._history.add(
            CombatEvent(
                event_type=EventType.FIGHT_CONDITION,
                node=self._node,
                action=str(condition.value),
            )
        )
        return ConditionFlag.FIGHT_CONTINUE

    def _handle_spot_enemy(self) -> ConditionFlag:  # noqa: PLR0912
        """处理索敌成功 — 核心决策节点。

        决策顺序:
        1. 采集敌方编成和阵型
        2. 检查节点是否在白名单中
        3. 检查阵型规则 (formation_rules)
        4. 检查敌舰规则 (enemy_rules)
        5. 根据结果执行: 撤退 / 迂回 / 设置阵型 / 进入战斗
        """
        # ── 信息采集 (按需) ──
        # 敌编成识别 + 阵型 OCR 在每个索敌点固定耗时 1~2s, 仅供
        # enemy_rules / formation_rules 消费; 纯挂机计划 (固定阵型 +
        # 节点白名单, 无规则) 完全用不上, 跳过采集让索敌决策直达。
        # (decision 查询是纯查找, 提前无副作用。)
        decision = self._get_current_decision()
        mode = 'exercise' if self._plan.mode == CombatMode.EXERCISE else 'fight'
        enemies: dict[str, int] | None = None
        enemy_formation = ''
        if decision.enemy_rules:
            enemies = get_enemy_info(self._device, mode=mode)
        if decision.formation_rules:
            enemy_formation = get_enemy_formation(self._device, self._ocr)
        if enemies is not None or enemy_formation:
            _log.info('[Combat] 敌方编成: {} 阵型: {}', enemies, enemy_formation)

        # 白名单检查
        if not self._plan.is_selected_node(self._node):
            click_retreat(self._device)
            self._last_action = 'retreat'
            self._history.add(
                CombatEvent(
                    event_type=EventType.SPOT_ENEMY,
                    node=self._node,
                    action='撤退',
                    extra={'reason': '不在预设点'},
                )
            )
            # SKIP_FIGHT 而非 FIGHT_END: 撤退不是成功打完, 不得冒充
            # OPERATION_SUCCESS (实机 2026-10-08: 撤退轮曾计入成功数)
            return ConditionFlag.SKIP_FIGHT

        # 检查迂回按钮是否可用
        can_detour = image_exist(self._device, TemplateKey.BYPASS, 0.8)
        want_detour = can_detour and decision.detour

        # 阵型规则优先
        rule_action = None
        if decision.formation_rules and enemy_formation:
            rule_action = decision.formation_rules.evaluate_formation(enemy_formation)

        # 敌舰规则
        if (
            rule_action is None or rule_action.result == RuleResult.NO_ACTION
        ) and decision.enemy_rules:
            rule_action = decision.enemy_rules.evaluate(enemies)

        # 应用规则结果
        if rule_action is not None:
            if rule_action.result == RuleResult.RETREAT:
                click_retreat(self._device)
                self._last_action = 'retreat'
                self._history.add(
                    CombatEvent(
                        event_type=EventType.SPOT_ENEMY,
                        node=self._node,
                        action='撤退',
                        enemies=enemies.copy() if enemies is not None else None,
                    )
                )
                # 同白名单撤退: 规则撤退也不得冒充成功打完
                return ConditionFlag.SKIP_FIGHT

            if rule_action.result == RuleResult.DETOUR:
                if not can_detour:
                    _log.error('[Combat] 规则指定迂回, 但该点无法迂回')
                    raise ValueError('该点无法迂回, 但在规则中指定了迂回')
                want_detour = True

            if rule_action.result == RuleResult.FORMATION and rule_action.formation:
                self._formation_by_rule = rule_action.formation

        # 执行迂回
        if want_detour:
            clicked = click_image(self._device, TemplateKey.BYPASS, 2.5)
            if clicked:
                _log.info('[Combat] 执行迂回')
                spot_templates = TemplateKey.SPOT_ENEMY.templates
                wait_until_leaving(
                    self._device,
                    checker=lambda screen: (
                        ImageChecker.find_any(screen, spot_templates, confidence=0.8) is not None
                    ),
                    timeout=10.0,
                    source='spot_enemy_success',
                    target='map_routing',
                )
            else:
                _log.warning('[Combat] 未找到迂回按钮')
            self._last_action = 'detour'
            self._history.add(
                CombatEvent(
                    event_type=EventType.SPOT_ENEMY,
                    node=self._node,
                    action='迂回',
                    enemies=enemies.copy() if enemies is not None else None,
                )
            )
            return ConditionFlag.FIGHT_CONTINUE

        # 远程导弹支援
        if decision.long_missile_support:
            clicked = click_image(self._device, TemplateKey.MISSILE_SUPPORT, 2.5)
            if clicked:
                _log.info('[Combat] 开启远程导弹支援')
            else:
                _log.warning('[Combat] 未找到远程支援按钮')

        # 进入战斗
        click_enter_fight(self._device)
        self._last_action = 'fight'
        self._history.add(
            CombatEvent(
                event_type=EventType.SPOT_ENEMY,
                node=self._node,
                action='战斗',
                enemies=enemies.copy() if enemies is not None else None,
            )
        )
        return ConditionFlag.FIGHT_CONTINUE

    def _handle_formation(self) -> ConditionFlag:
        """处理阵型选择。"""
        decision = self._get_current_decision()
        is_from_spot_enemy = self._last_action in ('fight', 'detour')

        # 白名单检查
        if not self._plan.is_selected_node(self._node):
            self._history.add(
                CombatEvent(
                    event_type=EventType.FORMATION,
                    node=self._node,
                    action='SL',
                    extra={'reason': '不在预设点'},
                )
            )
            return ConditionFlag.SL

        # 迂回失败 SL
        if is_from_spot_enemy and self._last_action == 'detour' and decision.SL_when_detour_fails:
            self._history.add(
                CombatEvent(
                    event_type=EventType.DETOUR,
                    node=self._node,
                    result='失败',
                )
            )
            self._history.add(
                CombatEvent(
                    event_type=EventType.FORMATION,
                    node=self._node,
                    action='SL',
                )
            )
            return ConditionFlag.SL

        # 确定阵型
        formation = decision.formation

        if is_from_spot_enemy and self._formation_by_rule is not None:
            formation = self._formation_by_rule
            self._formation_by_rule = None
            _log.debug('[Combat] 使用规则阵型: {}', formation.name)
        elif not is_from_spot_enemy:
            # 索敌失败
            if decision.SL_when_spot_enemy_fails:
                self._history.add(
                    CombatEvent(
                        event_type=EventType.FORMATION,
                        node=self._node,
                        action='SL',
                        extra={'reason': '索敌失败'},
                    )
                )
                return ConditionFlag.SL
            if decision.formation_when_spot_enemy_fails is not None:
                formation = decision.formation_when_spot_enemy_fails

        # 选择阵型
        _log.info('[Combat] 阵型选择: {}', formation.name)
        click_formation(self._device, formation)

        self._last_action = str(formation.value)
        self._history.add(
            CombatEvent(
                event_type=EventType.FORMATION,
                node=self._node,
                action=f'阵型{formation.value} ({formation.name})',
            )
        )
        return ConditionFlag.FIGHT_CONTINUE

    def _handle_missile_animation(self) -> ConditionFlag:
        """跳过导弹支援动画。"""
        _log.info('[Combat] 跳过导弹支援动画')
        click_skip_missile_animation(self._device)
        self._last_action = 'skip_animation'
        return ConditionFlag.FIGHT_CONTINUE

    def _handle_fight_period(self) -> ConditionFlag:
        """处理战斗进行中。"""
        decision = self._get_current_decision()
        if decision.SL_when_enter_fight:
            self._history.add(
                CombatEvent(
                    event_type=EventType.ENTER_FIGHT,
                    node=self._node,
                    action='SL',
                )
            )
            return ConditionFlag.SL
        return ConditionFlag.FIGHT_CONTINUE

    def _handle_night_prompt(self) -> ConditionFlag:
        """处理夜战选择。"""
        decision = self._get_current_decision()
        pursue = decision.night

        _log.info('[Combat] 夜战选择: {}', '追击' if pursue else '撤退')
        click_night_battle(self._device, pursue=pursue)
        self._last_action = 'yes' if pursue else 'no'

        self._history.add(
            CombatEvent(
                event_type=EventType.NIGHT_BATTLE,
                node=self._node,
                action='追击' if pursue else '撤退',
            )
        )
        return ConditionFlag.FIGHT_CONTINUE

    def _handle_result(self) -> ConditionFlag:
        """处理战果结算 -- 按 ``collect_result_info`` 决定采集与通过速度。

        重构后不再做像素级血量检测, 我方血量统一视作正常
        (:attr:`ShipDamageState.NORMAL`)。

        快速穿行 (默认): 结算三层 (战果/经验/掉落) 由
        :meth:`_blind_skip_settlement` 盲点穿行 —— **仅在 RESULT 已被
        识别触发后启动**, 战斗动画期绝不点击 (实机 2026-10-10: 等待期
        盲点把夜战点穿、PROCEED 对话框被误认成夜战提示)。

        慢速 (:attr:`CombatPlan.collect_result_info`): RESULT 页停留采集
        评级/MVP 并记录 :class:`FightResult` (供条件战斗按评级判定等),
        经验页入状态机逐页推进。
        """
        # 血量检测已移除: 我方血量统一视作正常, 保持引擎默认值 NORMAL*6
        self._ship_stats = [ShipDamageState.NORMAL] * 6

        if self._plan.collect_result_info:
            # ── 慢速: 完整采集 (评级/MVP) ──
            grade = detect_result_grade(self._device)
            screen = self._device.screenshot()
            mvp = detect_mvp(screen)

            fight_result = FightResult(
                node=self._node,
                mvp=mvp,
                grade=grade,
                ship_stats=self._ship_stats[:],
            )
            self._history.add(
                CombatEvent(
                    event_type=EventType.RESULT,
                    node=self._node,
                    result=grade,
                    ship_stats=self._ship_stats[:],
                    extra={'mvp': mvp},
                )
            )
            _log.info('[Combat] 战果: {} 节点: {}', fight_result, self._node)
            self._click_result_until_closed(CombatPhase.RESULT)
        else:
            # ── 快速: 结算三层盲点穿行 (RESULT 已确认在屏, 点击安全) ──
            self._blind_skip_settlement()
        return ConditionFlag.FIGHT_CONTINUE

    def _blind_skip_settlement(self, timeout: float = 20.0) -> None:
        """结算三层 (战果/经验/掉落) 盲点穿行 — 持续点击安全位直到命中落点。

        快速模式专用, **只在 RESULT 已被识别触发后调用**: 此时战斗动画
        已结束、结算页确认在屏, 全屏点击有效, 每次点击点掉当前层
        (战果 → 经验 → 掉落)。安全位 (0.5, 0.98) 位于准备页「快速修理」
        按钮下沿线以下的屏幕底缘带 (用户指定 2026-10-08, 见
        :attr:`Coords.SETTLE_SKIP`), 全部相关页面上无可交互元素, 连点
        恒无害; 对话框遮罩上的误触无效且无副作用 —— 命中任何合法落点
        (前进/回港对话框、旗舰大破、终态页) 即停止, 交回引擎主循环按
        当前页接管 (PROCEED 决策、终态收尾等)。

        与引擎等待期盲点的边界: FIGHT_PERIOD/NIGHT_PROMPT 等待期**绝不
        点击** —— 夜战点进入即夜战 (H5-ACD 三点无追击选项), 等待期盲点
        会把战斗/结算整段点穿; 且 PROCEED 对话框按钮与夜战提示同位
        (y≈0.648), NIGHT_PROMPT 模板可能误匹配 (实机 2026-10-10 教训)。

        Parameters
        ----------
        timeout:
            穿行总时长上限 (秒)。超时则 warning 后交回引擎主循环 ——
            RESULT 的错误恢复候选含结算三层, 引擎识别到后会重新调用
            本处理器接着穿行, 不会丢战斗战果。
        """
        stops = [CombatPhase.PROCEED, CombatPhase.FLAGSHIP_SEVERE_DAMAGE]
        end_phase = self._plan.end_phase
        if end_phase is not None and end_phase not in stops:
            stops.append(end_phase)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            click_settlement_skip(self._device)
            screen = self._device.screenshot()
            current = self._recognizer.identify_current(screen, stops)
            if current is not None:
                _log.debug('[Combat] 结算穿行命中 {}, 交回引擎', current.name)
                return
            # 识别不到 = 页面渐变/动画帧, 下一轮继续点 (结算页连点无害)
            time.sleep(0.2)
        _log.warning('[Combat] 结算穿行超时 ({:.0f}s), 交回引擎重试', timeout)

    def _click_result_until_closed(
        self,
        phase: CombatPhase,
        *,
        attempts: int = 6,
        interval: float = 0.3,
        polls: int = 4,
        pass_through: tuple[CombatPhase, ...] = (),
    ) -> None:
        """点击战果类页面继续，并验证已到达**已知后继状态**。

        验证判据是到达验证而非"原页面签名消失"——点击 RESULT 战果页后,
        游戏先进入**经验结算子页** (逐舰船经验, 对应 CombatPhase.EXP_SETTLEMENT):
        若以"RESULT 消失"为成功判据, 复检会在经验页误判成功提前返回,
        引擎随后等待 PROCEED/GET_SHIP 等状态全部落空
        (实机 2026-08-15: 状态识别超时 → 恢复失败 → 强制重启)。

        **快速点击 + 复检轮询**: 点击后每 *interval* 秒截图识别一次
        (最多 *polls* 次), 在 ``[phase] + pass_through + 后继状态`` 集合上判定:
          - 命中后继 → 成功返回;
          - 命中 *pass_through* 中的页面 (如快速穿行模式的经验页) →
            视为未到达, 继续点击跳过;
          - 命中 *phase* → 点击被动画吞掉, 立即重试点击 (重试即动画等待);
          - 识别不到 (过渡帧/页面渐变中) → **只等待不点击**: 对切换中的
            页面盲目连点会穿透中间页, 落点若是 PROCEED 对话框还可能误触
            按钮。轮询窗口耗尽仍认不出才继续点击推进 (真正的未知页)。

        Parameters
        ----------
        phase:
            待关闭页面的状态签名 (RESULT / GET_SHIP)。
        attempts:
            最大点击次数。
        interval:
            复检轮询间隔 (秒)。
        polls:
            每次点击后的复检轮询上限 (过渡帧最长等待 attempts x interval x polls)。
        pass_through:
            识别到即**继续点击跳过**的过渡页 (快速穿行模式的 EXP_SETTLEMENT)。
        """
        successors = self._result_successors(phase)
        candidates = [phase, *pass_through, *successors]
        for attempt in range(1, attempts + 1):
            click_result(self._device)
            for _ in range(polls):
                time.sleep(interval)
                screen = self._device.screenshot()
                current = self._recognizer.identify_current(screen, candidates)
                if current is None:
                    continue  # 过渡帧: 只等待, 不点击 (防穿透/误触)
                if current in pass_through:
                    _log.debug(
                        '[Combat] {} 到达过渡页 {} (第 {} 次点击), 继续点击跳过',
                        phase.name,
                        current.name,
                        attempt,
                    )
                    break  # 过渡页: 继续点击 (与"被吞"同路径)
                if current == phase:
                    _log.warning(
                        '[Combat] {} 页面未关闭 (第 {} 次点击), 延迟重试', phase.name, attempt
                    )
                    break  # 确认被吞 → 重试点击
                _log.debug('[Combat] {} 已推进到 {}', phase.name, current.name)
                return
            else:
                # 整个轮询窗口都无法识别 → 未知页, 继续点击推进
                _log.debug(
                    '[Combat] {} 点击后持续无法识别 (第 {} 次), 继续点击', phase.name, attempt
                )
        _log.error('[Combat] {} 页面点击 {} 次仍未推进, 继续执行', phase.name, attempts)

    def _result_successors(self, phase: CombatPhase) -> list[CombatPhase]:
        """返回战果类页面点击后的**合法落点集合** (到达验证候选)。

        比 state.py 转移图宽: 转移图按真实流转建模, 此集合额外包含穿透
        场景 — 快速点击下连点两击可能跳过中间页 (如 RESULT 点击穿透到
        GET_SHIP), 复检把它们都认出来即可提前停止点击, 交引擎主循环按
        当前页继续。

        慢速 (collect_result_info=True): RESULT 之后含经验结算页 (作为
        到达点逐页推进); 快速: 经验页是 pass_through 过渡页, 不在到达集。

        RESULT 之后: PROCEED / 终态页 / GET_SHIP / 旗舰大破 (+经验页, 慢速);
        EXP_SETTLEMENT 之后: 同上但去掉 EXP_SETTLEMENT 自身;
        GET_SHIP 之后: 同上但去掉 GET_SHIP 自身。
        """
        successors = [CombatPhase.PROCEED, CombatPhase.FLAGSHIP_SEVERE_DAMAGE]
        end_phase = self._plan.end_phase
        if end_phase is not None:
            successors.append(end_phase)
        if phase == CombatPhase.RESULT:
            successors.append(CombatPhase.GET_SHIP)
            if self._plan.collect_result_info:
                successors.append(CombatPhase.EXP_SETTLEMENT)
        elif phase == CombatPhase.EXP_SETTLEMENT:
            successors.append(CombatPhase.GET_SHIP)
        return successors

    def _handle_exp_settlement(self) -> ConditionFlag:
        """处理经验结算子页 — 点击继续推进到掉落/前进/终态页。"""
        self._click_result_until_closed(CombatPhase.EXP_SETTLEMENT)
        return ConditionFlag.FIGHT_CONTINUE

    def _handle_get_ship(self) -> ConditionFlag:
        """处理获取舰船。

        重构后不再做舰船掉落识别, 仅记录"获得舰船"事件 (记为空名称)。
        """
        ship_name = ''
        self._history.add(
            CombatEvent(
                event_type=EventType.GET_SHIP,
                node=self._node,
                result=ship_name,
            )
        )
        self._click_result_until_closed(CombatPhase.GET_SHIP)
        return ConditionFlag.FIGHT_CONTINUE

    def _handle_proceed(self) -> ConditionFlag:
        """处理继续前进 / 回港决策。

        决策依据: 当前节点的 ``proceed`` 配置。

        重构后不再依据血量做前进/回港判断 (血量检测已移除), 仅按节点规划决策。
        """
        self._node_count += 1
        decision = self._get_current_decision()

        should_proceed = decision.proceed

        _log.info('[Combat] 继续前进决策: {}', '前进' if should_proceed else '回港')
        click_proceed(self._device, go_forward=should_proceed)
        self._last_action = 'yes' if should_proceed else 'no'

        self._history.add(
            CombatEvent(
                event_type=EventType.PROCEED,
                node=self._node,
                action='前进' if should_proceed else '回港',
                ship_stats=self._ship_stats[:],
            )
        )

        if should_proceed:
            return ConditionFlag.FIGHT_CONTINUE
        return ConditionFlag.FIGHT_END

    def _handle_flagship_severe_damage(self) -> ConditionFlag:
        """处理旗舰大破。"""
        _log.info('[Combat] 旗舰大破, 强制回港')
        click_image(self._device, TemplateKey.FLAGSHIP_DAMAGE, 2.0)
        time.sleep(0.25)

        self._history.add(
            CombatEvent(
                event_type=EventType.FLAGSHIP_DAMAGE,
                node=self._node,
                action='回港',
            )
        )
        return ConditionFlag.FIGHT_END

    def _handle_dock_full(self) -> ConditionFlag:
        """处理船坞已满弹窗 — 返回 DOCK_FULL 标志交由上层处理。"""
        _log.warning('[Combat] 检测到船坞已满，战斗无法开始')
        self._history.add(
            CombatEvent(
                event_type=EventType.AUTO_RETURN,
                node=self._node,
                action='船坞已满',
            )
        )
        return ConditionFlag.DOCK_FULL

    # ── Mixin 所需的方法签名 (由宿主类提供) ──

    def _get_current_decision(self) -> NodeDecision:
        """获取当前节点的决策 (由 CombatEngine 实现)。"""
        raise NotImplementedError
