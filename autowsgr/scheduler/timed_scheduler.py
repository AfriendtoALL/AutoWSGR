"""ALAS 风格定时任务调度器 — 每个任务自带「下次运行时间」的挂机调度核心。

与 :class:`~autowsgr.scheduler.scheduler.TaskScheduler` (触发器模式) 的关系
------------------------------------------------------------------------
两者**并存不替换**: 触发器模式回答「现在有没有该做的事」 (pull), 本模块回答
「每件事下一次该在什么时候做」 (push)。触发器适合「条件未知、依赖游戏画面
返回值」的任务 (战役次数、演习对手); 定时模式适合「周期确定、需要跨重启
记忆」的任务 (每日重置、固定时段)。

本模块从 ALAS (AzurLaneAutoScript) 调度器移植的核心机制:

==============================  ============================================
ALAS 机制                      本模块对应实现
==============================  ============================================
``Scheduler.NextRun`` 每任务   :attr:`TaskState.next_run` — 每个任务各自
                               记住下次运行时间, 可选持久化 (见
                               ``state_path``), 脚本重启后不错过周期
pending / waiting 双队列       :meth:`TimedScheduler._pick_task` — 到期任务
                               按 (priority, next_run) 排序取队首
``SCHEDULER_PRIORITY``         :attr:`TaskSpec.priority` — 数值越小越先执行
``task_call('Restart')``       :meth:`TimedScheduler.task_call` — 强制把某
                               任务的下次运行时间改到 now (故障注入口)
``task_delay(success=...)``    :attr:`TaskSpec.success_schedule` /
                               :attr:`TaskSpec.failure_interval`
``failure_record`` (每任务     :attr:`TaskSpec.max_failures` — 同一任务连续
失败 3 次人工接管)             失败达阈值即人工接管, 与全局熔断分离
``TaskHoardingDuration``       (未移植) 见下方「有意偏差」第 5 条
异常→恢复映射                  :data:`EXCEPTION_RECOVERY_MAP` — 按异常类型
                               派发到设备重连/重启游戏/回主页/退避/接管
``Optimization_WhenTaskQueueEmpty``  ``when_queue_empty`` — 空闲期行为
                               (``goto_main`` / ``stay_there``)
服务器维护等待                 (不移植) 见下方「已知不移植项」
==============================  ============================================

与 ALAS 的有意偏差 (基于 AutoWSGR 现状):

- **不 ``exit(1)``**: ALAS 是独立进程, 人工接管即杀进程; AutoWSGR 是库,
  杀掉进程会连带杀死调用方。默认行为是记录 critical 日志 + 置停止信号
  结束调度 (``human_takeover_mode='stop'``), 需要抛异常时可设 ``'raise'``。
- **不保存 60 张错误截图**: ALAS 依赖 ``device.screenshot_deque``,
  ``ScrcpyController`` 无此结构。改为可注入的 ``on_error_evidence`` 回调
  (调用方自行落盘), 卡死现场已有
  :mod:`autowsgr.infra.stuck_watchdog` 的 faulthandler dump 覆盖。
- **服务器状态检查不移植**: 碧蓝航线有公开服务器状态 API, 战舰少女R 没有;
  「服务器维护」在 WSGR 中表现为页面识别失败, 由异常映射与熔断兜底。
- **不移植 TaskHoardingDuration (囤积)**: ALAS 默认值本身就是 0 (关闭);
  其语义是「队列排空时把批量起点延迟一小段时间以容纳更多任务」, 而 WSGR
  的各刷新时刻 (战役 00:05 / 演习 00:05/12:05/18:05 / 常规战顺延 00:05)
  已由 :class:`DailySchedule` 天然对齐, 同期任务无需额外延迟即同批入队;
  按任务类型改造的变体也无法让到期时刻不同的任务真正批量。故不引入该参数。

WSGR 机制事实 (zjsnrwiki, 构建日程计划时据此设定重置点):

- 每日任务刷新时间为**凌晨 3 点**; 周常为**周一凌晨 3 点** (wiki/任务)。
- 掉落计数 / 战役次数 / 演习对手在**0 点**刷新; 演习另有 **12 点、18 点**
  两个刷新时段 (wiki/海域资料、wiki/新手入门)。
- 常规图每日掉落上限 500 艘、战利品上限 50 个 (0 点刷新)。
- 战役每日 8 次 (建造养成活动期间 12 次), C 败及以下不消耗次数。
- 船坞满 (DOCK_FULL) 是**正常资源耗尽信号**, 不算失败 —— 与
  :data:`~autowsgr.scheduler.scheduler._FAILURE_FLAGS` 的口径一致。

使用方式::

    from autowsgr.scheduler import launch
    from autowsgr.scheduler.timed_scheduler import (
        DailySchedule, IntervalSchedule, TaskSpec, TimedScheduler,
    )

    ctx = launch('usersettings.yaml')
    scheduler = TimedScheduler(ctx)
    scheduler.register(TaskSpec(
        name='战役',
        runner_factory=lambda c: CampaignRunner(c, '困难潜艇', times=1),
        success_schedule=IntervalSchedule(60),
        exhausted_schedule=DailySchedule(('00:05',)),  # 0 点刷新, 留 5 分钟余量
        priority=5,
    ))
    scheduler.run()
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from autowsgr.combat import CombatResult
from autowsgr.infra.exceptions import (
    ActionFailedError,
    CombatDecisionError,
    CombatRecognitionTimeoutError,
    ConfigError,
    CriticalError,
    DockFullError,
    EmulatorConnectionError,
    EmulatorNotFoundError,
    ImageNotFoundError,
    NavigationError,
    NetworkError,
    OCRError,
    PageNotFoundError,
    RepeatedActionError,
    ResourceError,
)
from autowsgr.infra.logger import get_logger
from autowsgr.scheduler.recovery import (
    RecoveryOutcome,
    RecoveryPolicy,
    maybe_launch_emulator,
    recover_device,
)
from autowsgr.types import ConditionFlag


if TYPE_CHECKING:
    from collections.abc import Callable

    from autowsgr.context import GameContext


_log = get_logger('scheduler.timed')


# ═══════════════════════════════════════════════════════════════════════════════
# 调度规格 (Schedule) — 「下一次什么时候跑」
# ═══════════════════════════════════════════════════════════════════════════════


@runtime_checkable
class Schedule(Protocol):
    """调度规格协议: 给定当前时间, 计算下一次运行时间 (严格晚于 now)。"""

    def next_after(self, now: datetime) -> datetime: ...


@dataclass(frozen=True)
class IntervalSchedule:
    """间隔调度: 每 ``seconds`` 秒一次。"""

    seconds: float

    def next_after(self, now: datetime) -> datetime:
        return now + timedelta(seconds=self.seconds)


@dataclass(frozen=True)
class DailySchedule:
    """每日调度: 每天在 ``times`` 给定的时刻 (``"HH:MM"``) 运行。

    当天时刻已过则顺延到次日。多个时刻按先后各自计算, 取最近者 —— 例如
    演习的三个刷新时段 ``("00:05", "12:05", "18:05")``。
    """

    times: tuple[str, ...]

    def next_after(self, now: datetime) -> datetime:
        return next_daily_time(self.times, now)


@dataclass(frozen=True)
class WeeklySchedule:
    """每周调度: 每周 ``weekday`` (周一=0) 的 ``time`` (``"HH:MM"``) 运行。"""

    weekday: int
    time: str

    def next_after(self, now: datetime) -> datetime:
        return next_weekday_time(self.weekday, self.time, now)


def _parse_hhmm(value: str) -> tuple[int, int]:
    """解析 ``"HH:MM"``; 非法输入抛 :class:`ValueError` (配置错误应尽早暴露)。"""
    parts = value.strip().split(':')
    if len(parts) != 2:
        raise ValueError(f'时间格式应为 "HH:MM", 收到: {value!r}')
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f'时间超出范围: {value!r}')
    return hour, minute


def next_daily_time(times: tuple[str, ...], now: datetime) -> datetime:
    """``times`` 中下一个严格晚于 ``now`` 的时刻; 当天全部已过则取次日最早者。"""
    if not times:
        raise ValueError('DailySchedule.times 不能为空')
    best: datetime | None = None
    for value in times:
        hour, minute = _parse_hhmm(value)
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        if best is None or candidate < best:
            best = candidate
    assert best is not None  # times 非空时必然赋值
    return best


def next_weekday_time(weekday: int, value: str, now: datetime) -> datetime:
    """下一个指定星期几的时刻 (严格晚于 ``now``; 今天已过则顺延一周)。"""
    if not 0 <= weekday <= 6:
        raise ValueError(f'weekday 应在 0~6 (周一=0), 收到: {weekday}')
    hour, minute = _parse_hhmm(value)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    candidate += timedelta(days=(weekday - candidate.weekday()) % 7)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


# ═══════════════════════════════════════════════════════════════════════════════
# 任务规格 / 运行时状态
# ═══════════════════════════════════════════════════════════════════════════════


class TaskOutcome(Enum):
    """一次任务执行的结局分类 (调度器据此更新 next_run)。"""

    SUCCESS = 'success'
    """有进展: 正常打完 / 混合结果 (部分成功)。"""

    EXHAUSTED = 'exhausted'
    """本周期已打满 (战役次数用尽 / 演习无对手 / 常规战目标完成)。"""

    DOCK_FULL = 'dock_full'
    """船坞满。不算失败 (正常资源信号), 只做退避。"""

    FAILED = 'failed'
    """失败: 异常 / 空结果 / 全部结果为失败标志。"""


@dataclass
class TaskSpec:
    """任务声明 — 注册给 :class:`TimedScheduler` 的静态描述。

    Parameters
    ----------
    name:
        任务显示名 (日志用)。
    runner_factory:
        接收 ``ctx``、返回带 ``run()`` 方法的 runner。每次执行都会重新
        调用工厂 (与触发器模式一致, 保证 runner 状态不跨轮残留)。
    success_schedule:
        **成功**后的下次运行时间规格。战役/演习这类「每日分多次打完」的任务
        应给短间隔 (打完一场尽快打下一场)。
    exhausted_schedule:
        **本周期打满**后的下次运行时间规格。``None`` 表示沿用
        ``success_schedule``。日课任务应给每日重置时刻 —— 例如战役打完 8 次
        后回到次日 0 点, 而不是 60 秒后重试。
    command:
        任务唯一标识 (``task_call`` / 失败记录 / 持久化的键)。默认取 ``name``。
    priority:
        优先级, 数值越小越先执行 (ALAS ``SCHEDULER_PRIORITY`` 语义)。
    failure_interval:
        单次失败后的退避秒数。
    dock_full_interval:
        船坞满且未解装时的退避秒数 (解装完毕的轮只做 :attr:`DOCK_RETRY_DELAY`
        的短退避, 不占失败计数)。
    max_failures:
        同一任务连续失败达此次数即人工接管 (ALAS ``failure_record`` 阈值 3)。
    enable:
        是否启用。运行期可用 :meth:`TimedScheduler.task_disable` 覆盖。
    """

    name: str
    runner_factory: Callable[[GameContext], object]
    success_schedule: Schedule
    exhausted_schedule: Schedule | None = None
    command: str = ''
    priority: int = 50
    failure_interval: float = 300.0
    dock_full_interval: float = 1800.0
    max_failures: int = 3
    enable: bool = True

    def __post_init__(self) -> None:
        if not self.command:
            self.command = self.name


@dataclass
class TaskState:
    """任务运行时状态 — 可持久化的部分只有 ``next_run`` / ``failure_count``。"""

    spec: TaskSpec
    next_run: datetime
    failure_count: int = 0
    enabled: bool = True
    running: bool = False
    last_outcome: TaskOutcome | None = None
    last_run_at: datetime | None = None
    last_dock_destroyed: bool = False
    #: 本轮是否已由异常派发做过针对性处置 (重连/重启/回主页/纯退避)。
    #: 为 ``True`` 时通用失败路径只做退避, 不重复同一恢复动作。
    targeted: bool = False
    results: deque[CombatResult] = field(default_factory=lambda: deque(maxlen=4096))

    @property
    def command(self) -> str:
        return self.spec.command

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def priority(self) -> int:
        return self.spec.priority


# ═══════════════════════════════════════════════════════════════════════════════
# 异常 → 恢复动作映射 (ALAS run() 的 except 分派表)
# ═══════════════════════════════════════════════════════════════════════════════


class RecoveryAction(Enum):
    """异常对应的恢复动作。"""

    RECONNECT_DEVICE = 'reconnect_device'
    """设备层: adb 重连 + 重建视频流 (L2, 无副作用)。"""

    RESTART_GAME = 'restart_game'
    """游戏层: 重启游戏 (最重手段, 受全局熔断与冷却保护)。"""

    GOTO_MAIN = 'goto_main'
    """页面层: 回主页面 (认不出页面时由被调方决定是否重启游戏)。"""

    RETRY_LATER = 'retry_later'
    """稍后重试: 仅计失败并退避, 不做任何页面/设备操作。"""

    DOCK_FULL = 'dock_full'
    """按船坞满处理 (退避, 不计失败)。"""

    HUMAN_TAKEOVER = 'human_takeover'
    """不可恢复: 人工接管。"""

    FAILED = 'failed'
    """未识别的异常: 走通用失败路径 (与既有 TaskScheduler 口径一致)。"""


#: 异常类型 → 恢复动作。查表顺序为先本表后默认值, 子类异常按 MRO 匹配
#: (例如 ``EmulatorConnectionError`` 命中设备重连, 其父类 ``EmulatorError``
#: 不在表内也不影响)。
EXCEPTION_RECOVERY_MAP: dict[type[BaseException], RecoveryAction] = {
    # 设备层 — 先修连接, 绝不在 adb 不通时重启游戏 (实机 2026-10-11 教训)
    EmulatorConnectionError: RecoveryAction.RECONNECT_DEVICE,
    EmulatorNotFoundError: RecoveryAction.RECONNECT_DEVICE,
    # 无进展的高频重复点击 — ALAS GameStuckError/GameTooManyClickError 的对应物
    RepeatedActionError: RecoveryAction.RESTART_GAME,
    # 页面层 — 页面不认识/导航失败, 回主页重来
    PageNotFoundError: RecoveryAction.GOTO_MAIN,
    NavigationError: RecoveryAction.GOTO_MAIN,
    ActionFailedError: RecoveryAction.GOTO_MAIN,
    # 游戏逻辑 — 退避重试, 不碰页面
    NetworkError: RecoveryAction.RETRY_LATER,
    ResourceError: RecoveryAction.RETRY_LATER,
    OCRError: RecoveryAction.RETRY_LATER,
    ImageNotFoundError: RecoveryAction.RETRY_LATER,
    CombatRecognitionTimeoutError: RecoveryAction.RETRY_LATER,
    CombatDecisionError: RecoveryAction.RETRY_LATER,
    # 船坞满 — 资源信号, 不是故障
    DockFullError: RecoveryAction.DOCK_FULL,
    # 配置/严重错误 — 重试无意义, 直接人工接管
    ConfigError: RecoveryAction.HUMAN_TAKEOVER,
    CriticalError: RecoveryAction.HUMAN_TAKEOVER,
}

#: 默认动作: 未识别的异常按通用失败处理 (计入全局熔断, 由设备/页面分层恢复兜底)
DEFAULT_RECOVERY_ACTION = RecoveryAction.FAILED


def map_exception(exc: BaseException) -> RecoveryAction:
    """按异常类型查恢复动作; 未识别返回 :data:`DEFAULT_RECOVERY_ACTION`。

    匹配按 MRO 从具体到抽象: 先查异常类本身, 再查其基类, 保证新增子类
    不查表也能落到合理的父类动作上。
    """
    for klass in type(exc).__mro__:
        action = EXCEPTION_RECOVERY_MAP.get(klass)
        if action is not None:
            return action
    return DEFAULT_RECOVERY_ACTION


class HumanTakeoverError(Exception):
    """人工接管异常 (``human_takeover_mode='raise'`` 时由调度器抛出)。"""


class FunctionRunner:
    """把任意 ``fn()`` 包成 ``run() -> CombatResult`` 的 runner。

    ``fn`` 是闭包 (通常已捕获 ``ctx``, 如 ``lambda: collect_expedition(ctx)``),
    与 :mod:`autowsgr.scheduler.daily_plan` 的 ``_FunctionRunner`` 同语义。
    远征/任务奖励/浴室修理等非战斗任务没有 ``CombatResult`` 语义, 统一返回
    ``OPERATION_SUCCESS``; 异常被吞掉并记录 —— 单次辅助任务失败不应中断整日
    挂机。
    """

    def __init__(self, fn: Callable[[], object], name: str = '任务') -> None:
        self._fn = fn
        self._name = name

    def run(self) -> CombatResult:
        try:
            self._fn()
        except Exception as exc:
            _log.opt(exception=True).warning('[Timed] {} 执行异常: {}', self._name, exc)
        return CombatResult(flag=ConditionFlag.OPERATION_SUCCESS)


# ═══════════════════════════════════════════════════════════════════════════════
# 调度器
# ═══════════════════════════════════════════════════════════════════════════════

#: 船坞满但已解装完毕的轮次 (本轮未开打) 的短重试间隔 (秒)
DOCK_RETRY_DELAY = 5.0

#: 判定「本轮算失败」的 CombatResult.flag — 与 scheduler.py 的口径保持一致:
#: DOCK_FULL / SKIP_FIGHT / 混合结果都不算失败
_FAILURE_FLAGS: frozenset[ConditionFlag] = frozenset(
    {
        ConditionFlag.ACTION_FAILED,
        ConditionFlag.SL,
    }
)

#: 判定「本周期打满」的 flag
_EXHAUSTED_FLAGS: frozenset[ConditionFlag] = frozenset(
    {
        ConditionFlag.BATTLE_TIMES_EXCEED,
        ConditionFlag.SKIP_FIGHT,
    }
)


class TimedScheduler:
    """ALAS 风格定时任务调度器。

    Parameters
    ----------
    ctx:
        游戏上下文 (提供 ``stop_event`` / ``ctrl`` / 每日计数器)。
    idle_sleep:
        空闲轮询间隔 (秒)。
    when_queue_empty:
        空闲期行为: ``'goto_main'`` (默认, 回主页面并顺带做每日浮层处理)
        或 ``'stay_there'`` (原地等待)。
    max_consecutive_failures:
        **全局**连续失败熔断阈值: 跨任务累计的失败轮达此次数即重启游戏
        (用户 2026-10-11 指定 5)。与 :attr:`TaskSpec.max_failures` (单任务)
        是两层独立保护。
    recovery_cooldown:
        两次重启游戏之间的最小间隔 (秒)。
    auto_launch_emulator:
        设备不可用时是否允许**拉起模拟器实例** (默认 ``False``, 有副作用)。
    recovery_policy:
        设备恢复策略; ``None`` 时用默认 :class:`RecoveryPolicy`。
    state_path:
        调度状态持久化文件 (JSON)。跨脚本重启恢复各任务 ``next_run`` /
        ``failure_count``; ``None`` 则不持久化 (纯内存)。
    on_human_takeover:
        人工接管回调 ``(TaskState, reason) -> None`` (通知 / 落盘用)。
    on_error_evidence:
        严重失败时的现场回调 ``(TaskState, Exception | None) -> None``
        (ALAS ``save_error_log`` 的可注入替代)。
    human_takeover_mode:
        ``'stop'`` (默认) 置停止信号结束调度; ``'raise'`` 抛
        :class:`HumanTakeoverError`。
    clock / sleeper / monotonic:
        时间源注入 (测试用); 默认 ``datetime.now`` / ``time.sleep`` /
        ``time.monotonic``。
    """

    def __init__(
        self,
        ctx: GameContext,
        *,
        idle_sleep: float = 5.0,
        when_queue_empty: str = 'goto_main',
        max_consecutive_failures: int = 5,
        recovery_cooldown: float = 60.0,
        auto_launch_emulator: bool = False,
        recovery_policy: RecoveryPolicy | None = None,
        state_path: str | None = None,
        on_human_takeover: Callable[[TaskState, str], None] | None = None,
        on_error_evidence: Callable[[TaskState, BaseException | None], None] | None = None,
        human_takeover_mode: str = 'stop',
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if when_queue_empty not in ('goto_main', 'stay_there'):
            raise ValueError(f'未知的 when_queue_empty: {when_queue_empty!r}')
        if human_takeover_mode not in ('stop', 'raise'):
            raise ValueError(f'未知的 human_takeover_mode: {human_takeover_mode!r}')
        self._ctx = ctx
        self._idle_sleep = idle_sleep
        self._when_queue_empty = when_queue_empty
        self._max_consecutive_failures = max_consecutive_failures
        self._recovery_cooldown = recovery_cooldown
        self._auto_launch_emulator = auto_launch_emulator
        self._recovery_policy = recovery_policy or RecoveryPolicy(
            auto_launch_emulator=auto_launch_emulator,
        )
        self._state_path = state_path
        self._on_human_takeover = on_human_takeover
        self._on_error_evidence = on_error_evidence
        self._human_takeover_mode = human_takeover_mode
        self._now = clock or datetime.now  # 可注入, 默认墙上时钟
        self._sleep = sleeper or time.sleep
        self._monotonic = monotonic or time.monotonic

        self._states: list[TaskState] = []
        self._consecutive_failures = 0
        self._last_restart_at = 0.0
        self._last_date = self._now().date()  # 跨日按本地墙上时钟
        self._idle_goto_main_done = False

    # ── 任务管理 ──

    def register(self, spec: TaskSpec) -> TimedScheduler:
        """注册一个任务 (启动时立即到期, 与 ALAS ``NextRun`` 默认过去一致)。"""
        if any(state.command == spec.command for state in self._states):
            raise ValueError(f'任务 command 重复: {spec.command!r}')
        state = TaskState(spec=spec, next_run=self._now(), enabled=spec.enable)
        self._states.append(state)
        _log.info(
            '[Timed] 注册任务: {} (prio={}, 首次立即执行)',
            spec.name,
            spec.priority,
        )
        return self

    def task_call(self, command: str) -> bool:
        """强制某任务立即执行 (ALAS ``task_call``)。

        典型用途: 故障处置时注入口 (重启游戏 / 回主页)。返回是否命中。
        """
        for state in self._states:
            if state.command == command:
                state.next_run = self._now()
                state.enabled = True  # task_call 隐式启用 (ALAS 同)
                _log.info('[Timed] task_call: {} (强制立即执行)', command)
                return True
        _log.warning('[Timed] task_call 未命中: {}', command)
        return False

    def task_delay(self, command: str, seconds: float) -> bool:
        """把某任务的下次运行推迟 ``seconds`` 秒 (ALAS ``task_delay(minute=)``)。"""
        for state in self._states:
            if state.command == command:
                state.next_run = self._now() + timedelta(seconds=seconds)
                _log.info('[Timed] task_delay: {} (+{:.0f}s)', command, seconds)
                return True
        _log.warning('[Timed] task_delay 未命中: {}', command)
        return False

    def task_disable(self, command: str) -> bool:
        """禁用某任务 (运行期覆盖 :attr:`TaskSpec.enable`)。"""
        for state in self._states:
            if state.command == command:
                state.enabled = False
                _log.info('[Timed] 任务已禁用: {}', command)
                return True
        return False

    @property
    def tasks(self) -> list[TaskState]:
        """全部任务状态 (只读副本)。"""
        return list(self._states)

    # ── 主循环 ──

    def run(self) -> None:
        """常驻调度循环, 直到 ``ctx.stop_event`` 被置位。"""
        if not self._states:
            _log.warning('[Timed] 未注册任何任务, 直接退出')
            return
        self._load_state()
        _log.info('[Timed] 开始定时调度: {} 个任务', len(self._states))

        while not self._ctx.stop_event.is_set():
            self._maybe_reset_daily_counters()
            state = self._pick_task()
            if state is not None:
                self._idle_goto_main_done = False
                self._run_and_apply(state)
            else:
                self._idle_tick()
                self._sleep(self._idle_sleep)

        self._save_state()
        self._print_summary()
        _log.info('[Timed] 收到停止信号, 调度结束')

    def _pick_task(self) -> TaskState | None:
        """选出下一个应执行的任务: 到期者按 (priority, next_run) 取队首。"""
        now = self._now()
        pending = [s for s in self._states if s.enabled and s.next_run <= now]
        if not pending:
            return None
        pending.sort(key=lambda s: (s.priority, s.next_run, s.command))
        return pending[0]

    def _run_and_apply(self, state: TaskState) -> None:
        """执行一个任务并根据结局更新其调度状态。

        ``outcome is None`` 表示该轮已被接管流程处置 (人工接管), 不再分类、
        不再记失败 —— 否则空结果会被误判为又一轮失败, 污染 failure_count。
        """
        outcome = self._run_task(state)
        state.last_run_at = self._now()
        if outcome is None:
            return
        state.last_outcome = outcome
        self._apply_outcome(state, outcome)
        self._save_state()

    def _run_task(self, state: TaskState) -> TaskOutcome | None:
        """执行任务 (含异常映射), 返回结局分类; ``None`` = 已被接管处置。"""
        state.running = True
        state.targeted = False
        self._ctx.active_fight_tasks += 1
        try:
            results = self._execute_runner(state)
        finally:
            self._ctx.active_fight_tasks -= 1
            state.running = False
        if results is None:
            return None
        return self._classify(state, results)

    def _execute_runner(self, state: TaskState) -> list[CombatResult] | None:
        """构造并运行 runner; 异常按 :data:`EXCEPTION_RECOVERY_MAP` 派发。

        返回 ``None`` 表示该轮已被人工接管处置, 调用方不应再分类。
        """
        try:
            runner = state.spec.runner_factory(self._ctx)
            raw = runner.run()  # type: ignore[union-attr]
        except Exception as exc:
            return self._dispatch_exception(state, exc)
        if isinstance(raw, list):
            return raw
        return [raw]

    def _dispatch_exception(
        self,
        state: TaskState,
        exc: BaseException,
    ) -> list[CombatResult] | None:
        """按异常类型执行恢复动作。

        返回用于分类的结果列表 (空 = 失败); 人工接管时返回 ``None``
        (不再分类/计失败)。除船坞满与人工接管外, 其余动作统一按「无产出
        的失败轮」处理, 由 :meth:`_apply_failure` 计入熔断。
        """
        action = map_exception(exc)
        _log.opt(exception=True).warning(
            '[Timed] 任务 {} 抛出 {} → {}',
            state.name,
            type(exc).__name__,
            action.value,
        )

        if action is RecoveryAction.DOCK_FULL:
            return [CombatResult(flag=ConditionFlag.DOCK_FULL)]
        if action is RecoveryAction.HUMAN_TAKEOVER:
            self._human_takeover(state, f'不可恢复的异常: {type(exc).__name__}: {exc}')
            return None

        # 其余动作: 执行针对性处置 (或无操作), 均按无产出的失败轮处理
        if action is RecoveryAction.RECONNECT_DEVICE:
            self._ensure_device_usable()
            state.targeted = True
        elif action is RecoveryAction.RESTART_GAME:
            self._restart_game_safely()
            state.targeted = True
        elif action is RecoveryAction.GOTO_MAIN:
            self._try_recover_to_main()
            state.targeted = True
        else:
            # RETRY_LATER: 干预级别=无操作, 通用路径只退避不碰页面/设备;
            # FAILED (未识别异常): 无针对性处置, 返回空结果走通用失败路径
            state.targeted = action is RecoveryAction.RETRY_LATER
        return []

    def _classify(self, state: TaskState, results: list[CombatResult]) -> TaskOutcome:
        """把结果列表分类为 :class:`TaskOutcome` (口径与 TaskScheduler 一致)。"""
        if not results:
            return TaskOutcome.FAILED
        if any(r.flag == ConditionFlag.DOCK_FULL for r in results):
            # 解装完毕的轮 (dock_full_destroyed) 只做短重试; 未解装做长退避
            state.last_dock_destroyed = all(
                r.flag != ConditionFlag.DOCK_FULL or r.dock_full_destroyed for r in results
            )
            return TaskOutcome.DOCK_FULL
        state.last_dock_destroyed = False
        if all(r.flag in _FAILURE_FLAGS for r in results):
            return TaskOutcome.FAILED
        if all(r.flag in _EXHAUSTED_FLAGS for r in results):
            return TaskOutcome.EXHAUSTED
        return TaskOutcome.SUCCESS

    def _apply_outcome(self, state: TaskState, outcome: TaskOutcome) -> None:
        """按结局更新 ``next_run`` / 失败计数, 并触发对应处置。"""
        now = self._now()
        spec = state.spec

        if outcome is TaskOutcome.SUCCESS:
            state.failure_count = 0
            self._consecutive_failures = 0
            state.next_run = spec.success_schedule.next_after(now)
            _log.info(
                '[Timed] {} 成功, 下次运行 {}',
                state.name,
                _fmt_time(state.next_run),
            )
            return

        if outcome is TaskOutcome.EXHAUSTED:
            # 打满不计失败; 日课回到下一个重置点, 间隔任务按成功间隔重试
            state.failure_count = 0
            self._consecutive_failures = 0
            schedule = spec.exhausted_schedule or spec.success_schedule
            state.next_run = schedule.next_after(now)
            _log.info(
                '[Timed] {} 本周期已打满, 下次运行 {}',
                state.name,
                _fmt_time(state.next_run),
            )
            return

        if outcome is TaskOutcome.DOCK_FULL:
            # 船坞满是正常资源信号: 不计失败, 不触发熔断
            delay = DOCK_RETRY_DELAY if state.last_dock_destroyed else spec.dock_full_interval
            state.next_run = now + timedelta(seconds=delay)
            _log.warning(
                '[Timed] {} 船坞已满 ({}), {:.0f}s 后重试',
                state.name,
                '已解装' if state.last_dock_destroyed else '未解装',
                delay,
            )
            return

        self._apply_failure(state)

    def _apply_failure(self, state: TaskState) -> None:
        """失败结局: 双轨计数 (单任务 → 人工接管; 全局 → 熔断重启)。"""
        state.failure_count += 1
        self._consecutive_failures += 1
        state.next_run = self._now() + timedelta(seconds=state.spec.failure_interval)
        _log.warning(
            '[Timed] {} 失败 (本任务第 {}/{} 次, 全局连续第 {} 次)',
            state.name,
            state.failure_count,
            state.spec.max_failures,
            self._consecutive_failures,
        )
        if state.failure_count >= state.spec.max_failures:
            self._human_takeover(
                state,
                f'任务 {state.name} 连续失败 {state.failure_count} 次 '
                f'(阈值 {state.spec.max_failures})',
            )
            return
        self._handle_failure(state)

    # ═══════════════════════════════════════════════════════════════════════════
    # 故障恢复 (与 TaskScheduler 同口径: 先设备后游戏, 退避保证不空转)
    # ═══════════════════════════════════════════════════════════════════════════

    def _handle_failure(self, state: TaskState) -> None:
        """全局熔断处置: 设备层优先, 未达阈值只轻量恢复, 达阈值重启游戏。

        本轮已由异常派发做过针对性处置时 (``state.targeted``), 只做退避 —
        避免同一轮里重复同一恢复动作 (派发已重连过又去重连、已回主页又回
        一遍); 轮次仍计入熔断, 持续失败最终仍会升级到重启游戏。
        """
        if state.targeted:
            _log.info(
                '[Timed] {} 本轮已由异常派发处置, 通用路径只退避',
                state.name,
            )
            self._apply_failure_backoff()
            return
        if not self._ensure_device_usable():
            return
        if self._consecutive_failures < self._max_consecutive_failures:
            self._try_recover_to_main()
            self._apply_failure_backoff()
            return
        now = self._monotonic()
        if now - self._last_restart_at < self._recovery_cooldown:
            _log.error(
                '[Timed] 全局连续失败 {} 次, 但距上次重启仅 {:.0f}s (冷却 {:.0f}s), 本轮不再重启',
                self._consecutive_failures,
                now - self._last_restart_at,
                self._recovery_cooldown,
            )
            self._apply_failure_backoff()
            return
        _log.error(
            '[Timed] 全局连续失败 {} 次达到阈值, 重启游戏 (任务 {})',
            self._consecutive_failures,
            state.name,
        )
        self._restart_game_safely()
        self._consecutive_failures = 0
        self._last_restart_at = now

    def _apply_failure_backoff(self) -> None:
        """失败轮之间退避 (指数增长, 防设备不可用时高速空转)。"""
        delay = self._recovery_policy.backoff_seconds(self._consecutive_failures)
        if delay > 0:
            _log.info('[Timed] 失败退避 {:.1f}s', delay)
            self._sleep(delay)

    def _ensure_device_usable(self) -> bool:
        """确保设备可用 (L2 重连 → L2.5 被动等待 → L3 拉起)。

        返回 ``True`` 表示可继续走游戏层恢复; ``False`` 表示连 adb 都不通,
        任何「重启游戏」的尝试都必然失败, 已在设备层处置并退避。
        """
        try:
            outcome = recover_device(self._ctx, self._recovery_policy)
        except Exception as exc:
            _log.opt(exception=True).warning('[Timed] 设备诊断异常: {}', exc)
            return True  # 诊断失败 ≠ 设备故障, 保守按原路径走

        if outcome is RecoveryOutcome.RECOVERED:
            self._consecutive_failures = 0
            _log.info('[Timed] 设备已恢复, 继续执行')
            return True
        if outcome is RecoveryOutcome.NEEDS_GAME_RESTART:
            return True

        outcome = maybe_launch_emulator(self._ctx, self._recovery_policy)
        if outcome is RecoveryOutcome.RECOVERED:
            self._consecutive_failures = 0
            return True
        if outcome is RecoveryOutcome.NEEDS_GAME_RESTART:
            return True

        _log.error(
            '[Timed] 设备不可用且无法自动恢复 (auto_launch_emulator={}), 本轮放弃。'
            '不重启游戏 (adb 不通, 重启必然失败)。',
            self._auto_launch_emulator,
        )
        self._consecutive_failures = 0
        self._last_restart_at = self._monotonic()
        self._apply_failure_backoff()
        return False

    def _try_recover_to_main(self) -> None:
        """轻量恢复: 回主页面 (认不出页面时由被调方重启游戏)。"""
        try:
            # 函数内 import: ops 依赖 scheduler, 顶层 import 会成环
            from autowsgr.ops.startup import recover_to_main_or_restart

            recover_to_main_or_restart(self._ctx, self._ctx.config.account.game_app)
        except Exception as exc:
            _log.opt(exception=True).warning(
                '[Timed] 轻量恢复失败 (不影响主循环): {}',
                exc,
            )

    def _restart_game_safely(self) -> None:
        """重启游戏 (最重手段), 自身异常不得打断主循环。"""
        try:
            # 函数内 import: 同 _try_recover_to_main
            from autowsgr.ops import restart_game

            restart_game(self._ctx.ctrl)
            _log.info('[Timed] 游戏已重启')
        except Exception as exc:
            _log.opt(exception=True).error('[Timed] 重启游戏失败: {}', exc)

    def _human_takeover(self, state: TaskState, reason: str) -> None:
        """人工接管: 记录现场 → 回调 → 按模式停止或抛异常。

        不 ``exit(1)``: AutoWSGR 是库, 杀进程会连带杀死调用方 (见模块 docstring)。
        """
        _log.critical('[Timed] 请求人工接管: {} — {}', state.name, reason)
        self._save_state()
        if self._on_error_evidence is not None:
            try:
                self._on_error_evidence(state, None)
            except Exception as exc:
                _log.opt(exception=True).warning('[Timed] on_error_evidence 异常: {}', exc)
        if self._on_human_takeover is not None:
            try:
                self._on_human_takeover(state, reason)
            except Exception as exc:
                _log.opt(exception=True).warning('[Timed] on_human_takeover 异常: {}', exc)
        if self._human_takeover_mode == 'raise':
            raise HumanTakeoverError(f'{state.name}: {reason}')
        self._ctx.stop_event.set()

    # ═══════════════════════════════════════════════════════════════════════════
    # 空闲期行为
    # ═══════════════════════════════════════════════════════════════════════════

    def _idle_tick(self) -> None:
        """队列排空时的空闲行为 (每个空闲轮调用一次)。"""
        if self._when_queue_empty == 'goto_main' and not self._idle_goto_main_done:
            # 每次进入空闲只做一次: 回主页 + 每日浮层处理 (后者有 0 点时间戳 gate,
            # 当天已处理过则直接返回, 不会每个空闲轮都截图)
            self._idle_goto_main_done = True
            self._ensure_main_page_clean()
            self._try_recover_to_main()

    def _ensure_main_page_clean(self) -> None:
        """常驻弹窗检查: 每日首次消除主页面浮层 (新闻公告 / 签到 / 活动预约)。"""
        try:
            # 函数内 import: 同 _try_recover_to_main
            from autowsgr.ops.startup import handle_daily_overlays

            handle_daily_overlays(self._ctx)
        except Exception as exc:
            _log.opt(exception=True).warning(
                '[Timed] 主页面浮层检查失败: {}',
                exc,
            )

    # ═══════════════════════════════════════════════════════════════════════════
    # 跨日重置与状态持久化
    # ═══════════════════════════════════════════════════════════════════════════

    def _maybe_reset_daily_counters(self) -> None:
        """检测跨日 (0 点) → 清零 ctx 每日计数器 (掉落 / 快修累计)。

        任务自身的「今日次数」由各任务的 exhausted_schedule 在打满时顺延到
        次日重置点, 无需在此统一 reset (ALAS 同思路: NextRun 即唯一事实源)。
        """
        today = self._now().date()  # 本地墙上时钟 (跨日检测)
        if today == self._last_date:
            return
        _log.info('[Timed] 检测到跨日 ({} → {}), 清零每日计数器', self._last_date, today)
        self._ctx.dropped_ship_count = 0
        self._ctx.dropped_loot_count = 0
        self._ctx.quick_repair_used = 0
        self._last_date = today

    def _load_state(self) -> None:
        """从 ``state_path`` 恢复各任务 next_run / failure_count (尽力而为)。"""
        if not self._state_path:
            return
        try:
            with open(self._state_path, encoding='utf-8') as fh:
                data = json.load(fh)
            saved = data.get('tasks', {})
        except (OSError, ValueError) as exc:
            _log.warning('[Timed] 读取调度状态失败 (忽略, 按新会话处理): {}', exc)
            return
        restored = 0
        for state in self._states:
            entry = saved.get(state.command)
            if not isinstance(entry, dict):
                continue
            next_run = _parse_dt(entry.get('next_run'))
            if next_run is not None:
                state.next_run = next_run
                restored += 1
            failure_count = entry.get('failure_count')
            if isinstance(failure_count, int) and failure_count >= 0:
                state.failure_count = failure_count
        if restored:
            _log.info('[Timed] 已从状态文件恢复 {} 个任务的调度进度', restored)

    def _save_state(self) -> None:
        """把各任务 next_run / failure_count 写入 ``state_path`` (尽力而为)。"""
        if not self._state_path:
            return
        payload = {
            'version': 1,
            'saved_at': self._now().isoformat(),
            'tasks': {
                state.command: {
                    'next_run': state.next_run.isoformat(),
                    'failure_count': state.failure_count,
                }
                for state in self._states
            },
        }
        try:
            with open(self._state_path, 'w', encoding='utf-8') as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            _log.warning('[Timed] 写入调度状态失败 (不影响运行): {}', exc)

    # ── 汇总 ──

    def _print_summary(self) -> None:
        """打印调度汇总 (各任务本次会话的执行情况)。"""
        _log.info('[Timed] ' + '=' * 50)
        _log.info('[Timed] 调度结束, 各任务状态:')
        for state in self._states:
            outcome = state.last_outcome.value if state.last_outcome else '未执行'
            _log.info(
                '[Timed]   {} (prio={}): 结局={}, 连续失败={}, 下次运行={}',
                state.name,
                state.priority,
                outcome,
                state.failure_count,
                _fmt_time(state.next_run),
            )
        _log.info('[Timed] ' + '=' * 50)


def _fmt_time(value: datetime) -> str:
    """格式化时间 (日志用)。"""
    return value.strftime('%m-%d %H:%M:%S')


def _parse_dt(value: object) -> datetime | None:
    """解析 ISO 时间字符串; 失败返回 ``None`` (坏数据忽略而非崩溃)。"""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
