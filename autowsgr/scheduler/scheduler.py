"""基础任务调度器 — 按顺序执行提交的战斗任务，定时插入远征检查。

使用方式::

    from autowsgr.scheduler import launch, TaskScheduler, FightTask

    ctx = launch("user_settings.yaml")

    scheduler = TaskScheduler(ctx, expedition_interval=15 * 60)
    scheduler.add(FightTask(runner=my_event_runner, times=30))
    scheduler.add(FightTask(runner=my_normal_runner, times=5))
    scheduler.run()

调度逻辑:

1. 按提交顺序依次执行每个 ``FightTask``
2. 每个 task 内循环执行 ``runner.run()``，直到达到指定次数或船坞满
3. 每次战斗完成后检查距上次远征收取是否超过 ``expedition_interval``，
   若超过则插入一次 ``collect_expedition``
4. 所有 task 执行完毕后调度器退出
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from autowsgr.combat import CombatResult
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
    from autowsgr.scheduler.triggers import Trigger

_log = get_logger('scheduler')


# ═══════════════════════════════════════════════════════════════════════════════
# 故障恢复熔断参数
#
# 背景: recover_to_main_or_restart() 定义在 ops/startup.py, 功能完整(含重启
# 游戏), 但此前 run_daily() 主循环从不调用它 —— 设备掉线 / 游戏闪退 / 网络
# 断时, 子任务异常被 _run_task 吞成 ACTION_FAILED, 触发器 on_done 只翻
# _idle=True, 下一轮继续产出任务 → 再失败 → **无限空转, 日志持续写盘**。
# logs/ 下已有 18+ 个时间戳目录即为此现象留痕。
#
# 对标 Alas 的 failure_record 计数熔断: 同一任务失败 >= N 次即升级处置。
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_MAX_CONSECUTIVE_FAILURES = 5
"""连续失败多少次后重启游戏。

取 5 (用户 2026-10-11 指定, 原方案为 3) 的理由: 单次战斗周期约 100s,
5 次连续失败 ≈ 8~10 分钟才判定「不是偶发抖动」。夜间偶发一次网络抖动
或一次船坞满处理失败不应触发重启; 而真正的掉线/闪退会持续失败, 8 分钟
内必然累积到阈值, 仍远快于「空烧一夜」。
"""

DEFAULT_RECOVERY_COOLDOWN = 60.0
"""两次重启之间的最小间隔 (秒)。

设备真的掉线时, 每轮都重启会加剧问题 (adb server 被反复拉起、模拟器
CPU 打满)。给 adb 重连留出窗口。
"""

_FAILURE_FLAGS: frozenset[ConditionFlag] = frozenset({
    ConditionFlag.ACTION_FAILED,
    ConditionFlag.SL,
})
"""判定「本轮算失败」的 CombatResult.flag。

- ``ACTION_FAILED``: 子任务异常/空结果, 由 _run_task 捕获后置位
- ``SL``: 战斗状态机反复无法推进, 引擎已重启过游戏但仍失败

注意 ``DOCK_FULL`` 不算失败 —— 那是正常的资源耗尽信号, 触发器会走解装
或改派其他任务, 把它计入失败会导致正常挂机被误判为故障并重启游戏。
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Runner 协议
# ═══════════════════════════════════════════════════════════════════════════════


@runtime_checkable
class FightRunnerProtocol(Protocol):
    """所有战斗执行器的公共协议。

    要求实现 ``run() → CombatResult``。
    :class:`EventFightRunner`, :class:`NormalFightRunner` 天然满足。
    :class:`CampaignRunner`, :class:`ExerciseRunner` 返回 ``list[CombatResult]``，
    需要通过 :class:`BatchRunnerAdapter` 适配。
    """

    def run(self) -> CombatResult: ...


class BatchRunnerAdapter:
    """将 runner 输出规范化为完整的 ``list[CombatResult]``。

    适用于 :class:`CampaignRunner` (内部自带循环，每次 ``run()`` 已执行多场)
    和 :class:`ExerciseRunner`。
    """

    def __init__(self, inner: object) -> None:
        if not hasattr(inner, 'run'):
            raise TypeError(f'{type(inner).__name__} 没有 run() 方法')
        self._inner = inner

    def run(self) -> list[CombatResult]:
        results = self._inner.run()  # type: ignore[union-attr]
        if isinstance(results, list):
            return results
        return [results]  # type: ignore[list-item]


# ═══════════════════════════════════════════════════════════════════════════════
# 战斗任务
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class FightTask:
    """一个战斗任务。

    Parameters
    ----------
    runner:
        战斗执行器实例。需满足 ``run() → CombatResult`` 协议。
        对于 ``CampaignRunner`` / ``ExerciseRunner``（返回 list），
        可传原始 runner，调度器会自动包装。
    times:
        执行次数。``CampaignRunner`` 自带 times 时此处设 1 即可。
    name:
        任务名称（用于日志），留空则自动推导。
    priority:
        优先级（数值越小越先执行），用于 ``run_daily`` 触发器队列排序。默认 50。
    on_done:
        每场战斗结束后的回调（接收 ``CombatResult``）。触发器用它更新
        ``_idle`` / ``_exhausted`` 等内部状态。
    """

    runner: object
    times: int = 1
    name: str = ''
    priority: int = 50
    on_done: Callable[[CombatResult], None] | None = None

    # 运行时状态
    completed: int = field(default=0, init=False, repr=False)
    results: list[CombatResult] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = type(self.runner).__name__


# ═══════════════════════════════════════════════════════════════════════════════
# 调度器
# ═══════════════════════════════════════════════════════════════════════════════


class TaskScheduler:
    """基础任务调度器。

    Parameters
    ----------
    ctx:
        游戏上下文 (用于远征检查)。
    expedition_interval:
        远征检查间隔 (秒)。默认 ``900`` (15 分钟)。
        设为 ``0`` 或负数则禁用自动远征。
    max_consecutive_failures:
        连续多少次「失败轮次」后重启游戏。默认
        :data:`DEFAULT_MAX_CONSECUTIVE_FAILURES` (5)。设为 ``0`` 或负数
        则禁用该熔断 (仅保留轻量恢复)。
    recovery_cooldown:
        两次重启游戏之间的最小间隔 (秒)。默认
        :data:`DEFAULT_RECOVERY_COOLDOWN` (60)。
    auto_launch_emulator:
        设备不可用时是否允许脚本**拉起模拟器实例**。默认 ``False``。
        这是有副作用的操作 (占用内存/显存、可能与用户手动操作冲突),
        因此必须显式开启。见 :mod:`autowsgr.scheduler.recovery` 的分级策略。
    """

    def __init__(
        self,
        ctx: GameContext,
        *,
        expedition_interval: float = 900.0,
        idle_sleep: float = 5.0,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
        recovery_cooldown: float = DEFAULT_RECOVERY_COOLDOWN,
        auto_launch_emulator: bool = False,
    ) -> None:
        self._ctx = ctx
        self._expedition_interval = expedition_interval
        self._idle_sleep = idle_sleep
        self._max_consecutive_failures = max_consecutive_failures
        self._recovery_cooldown = recovery_cooldown
        self._auto_launch_emulator = auto_launch_emulator
        self._tasks: list[FightTask] = []
        self._last_expedition_time: float = 0.0
        # 触发器调度 (auto_daily 长期挂机)
        self._triggers: list[Trigger] = []
        self._queue: list[FightTask] = []
        self._last_date: date = date.today()  # noqa: DTZ011  # 跨日按本地墙上时钟 (游戏 0 点刷新)
        # 故障恢复熔断状态
        self._consecutive_failures: int = 0
        self._last_restart_at: float = 0.0
        self._recovery_policy = RecoveryPolicy(auto_launch_emulator=auto_launch_emulator)

    # ── 任务管理 ──

    def add(self, task: FightTask) -> TaskScheduler:
        """添加一个战斗任务。支持链式调用。"""
        self._tasks.append(task)
        _log.info(
            '[Scheduler] 添加任务: {} x{}',
            task.name,
            task.times,
        )
        return self

    @property
    def tasks(self) -> list[FightTask]:
        """当前任务列表 (只读副本)。"""
        return list(self._tasks)

    # ── 执行 ──

    def run(self) -> list[FightTask]:
        """按顺序执行所有任务。

        Returns
        -------
        list[FightTask]
            执行完毕的任务列表 (包含结果)。
        """
        if not self._tasks:
            _log.warning('[Scheduler] 无任务，直接退出')
            return []

        _log.info(
            '[Scheduler] 开始调度: {} 个任务',
            len(self._tasks),
        )
        self._last_expedition_time = time.monotonic()

        for i, task in enumerate(self._tasks):
            _log.info(
                '[Scheduler] ── 任务 {}/{}: {} x{} ──',
                i + 1,
                len(self._tasks),
                task.name,
                task.times,
            )
            self._ensure_main_page_clean()
            self._run_task(task)  # 返回值在此路径忽略 (顺序执行语义不变)

        self._print_summary()
        return list(self._tasks)

    def _ensure_main_page_clean(self) -> None:
        """常驻弹窗检查: 每日首次消除主页面浮层 (新闻公告 / 每日签到 / 活动预约)。

        在任务执行前、挂机等待时调用; 内部由
        :func:`autowsgr.ops.startup.handle_daily_overlays` 的时间戳 gate 短路:
        每天 0 点后第一次调用才真正截图检测并消除, 当天已处理过则直接返回,
        不干扰正常流程。
        """
        try:
            from autowsgr.ops.startup import handle_daily_overlays

            handle_daily_overlays(self._ctx)
        except Exception as exc:
            _log.opt(exception=True).warning(
                '[Scheduler] 主页面浮层检查失败: {}',
                exc,
            )

    def _run_task(self, task: FightTask) -> list[CombatResult]:
        """执行单个任务的全部轮次。

        Returns
        -------
        list[CombatResult]
            本子任务产生的全部结果 (含因异常/空结果补的 ACTION_FAILED)。
            :meth:`run_daily` 据此判定成败并驱动故障熔断 —— 注意不能只看
            是否抛异常, 因为子任务异常已在下方被捕获并转成 ACTION_FAILED,
            异常不会传播到调用方。
        """
        all_results: list[CombatResult] = []
        # 统一用 BatchRunnerAdapter 包装: 完整保留 run()→list[CombatResult],
        # 对 run()→单个 CombatResult 规范化为单元素列表, 兼容两类 runner
        # (CampaignRunner / ExerciseRunner 返回 list, 其余返回单个)。
        # 不能靠 isinstance(FightRunnerProtocol) 判断: @runtime_checkable 不检查
        # 返回类型, CampaignRunner 有 run() 方法即被误判满足协议而跳过适配
        # (曾致战役 on_done 回调 'list' object has no attribute 'flag' 崩溃)。
        runner = BatchRunnerAdapter(task.runner)

        self._ctx.active_fight_tasks += 1
        try:
            # 按「真实开打的轮次」计数: 解装完毕的船坞满轮 (DOCK_FULL +
            # dock_full_destroyed, 本轮未开打) 不占次数, 立即重试; 否则打满
            # task.times 轮为止
            rounds = 0
            while rounds < task.times:
                if self._ctx.stop_event.is_set():
                    _log.info('[Scheduler] {} 检测到停止信号, 中断', task.name)
                    break

                _log.info(
                    '[Scheduler] {} 第 {}/{} 次',
                    task.name,
                    rounds + 1,
                    task.times,
                )

                # 远征检查 (战斗前) — 仅旧 run() 路径有效;run_daily() 下由触发器接管
                self._maybe_collect_expedition()

                try:
                    results = runner.run()
                except Exception as exc:
                    # 子任务异常: 结束本子任务, 不崩溃主循环。ACTION_FAILED 不属
                    # 于任何触发器的成功/耗尽标志, 故 on_done 不会计入战斗次数、
                    # 不会误判耗尽 —— 远征出错等下次定时重试, 战斗出错不扣次数。
                    _log.opt(exception=True).error(
                        '[Scheduler] {} 第 {} 次异常, 结束本子任务: {}',
                        task.name,
                        rounds + 1,
                        exc,
                    )
                    results = [CombatResult(flag=ConditionFlag.ACTION_FAILED)]

                if not results:
                    _log.error(
                        '[Scheduler] {} 第 {} 次未执行任何战斗',
                        task.name,
                        rounds + 1,
                    )
                    results = [CombatResult(flag=ConditionFlag.ACTION_FAILED)]

                task.results.extend(results)
                all_results.extend(results)

                # 本轮全部为「解装完毕的船坞满」→ 未开打, 不占轮次,
                # on_done 后立即重试 (下轮 run 重新导航进图)
                dock_resolved = results and all(
                    r.flag == ConditionFlag.DOCK_FULL and r.dock_full_destroyed for r in results
                )
                if not dock_resolved:
                    rounds += 1
                    task.completed = rounds

                # 通知触发器更新状态 (auto_daily 触发器调度用)
                for result in results:
                    if task.on_done is not None:
                        try:
                            task.on_done(result)
                        except Exception as exc:
                            _log.opt(exception=True).warning(
                                '[Scheduler] {} on_done 回调异常: {}',
                                task.name,
                                exc,
                            )

                    _log.info(
                        '[Scheduler] {} [{}/{}] → {}{}',
                        task.name,
                        task.completed,
                        task.times,
                        result.flag.value if result.flag else 'N/A',
                        ' (解装完毕, 重试)' if dock_resolved else '',
                    )

                # 船坞满且未解装 → 停止当前任务 (解装完毕的轮不进此分支,
                # 循环自然进入下一轮重试)
                if any(
                    r.flag == ConditionFlag.DOCK_FULL and not r.dock_full_destroyed for r in results
                ):
                    _log.warning(
                        '[Scheduler] {} 船坞已满且无法解装, 跳过剩余 {} 次',
                        task.name,
                        task.times - task.completed,
                    )
                    break
        finally:
            self._ctx.active_fight_tasks -= 1

        return all_results

    # ═══════════════════════════════════════════════════════════════════════════════
    # 触发器调度 (auto_daily 长期挂机)
    # ═══════════════════════════════════════════════════════════════════════════════

    def register_trigger(self, trigger: Trigger) -> TaskScheduler:
        """注册一个触发器。支持链式调用。"""
        self._triggers.append(trigger)
        _log.info(
            '[Scheduler] 注册触发器: {} (prio={})',
            trigger.name,
            trigger.priority,
        )
        return self

    def run_daily(self) -> None:
        """触发器驱动的长期挂机主循环 (auto_daily)。

        循环逻辑::

            while not stop_event:
                1. 检测跨日 → reset 所有触发器 (战役 _exhausted / 常规战计数清零)
                2. 询问每个触发器 should_fire → 命中的任务按 priority 入队
                3. 队首有任务 → 执行;队列空 → idle_sleep 后继续 (挂机等待)

        与 :meth:`run` 的区别:后者顺序执行预提交任务后退出;
        本方法由触发器持续产出任务,适合全天 / 跨日挂机。
        """
        _log.info(
            '[Scheduler] 开始触发器调度: {} 个触发器',
            len(self._triggers),
        )
        self._last_date = date.today()  # noqa: DTZ011  # 本地墙上时钟
        # 启动时校准每日掉落计数器 (避免首次常规战误触发, 见 _sync_initial_counts)
        self._sync_initial_counts()

        while not self._ctx.stop_event.is_set():
            self._check_daily_reset()

            # 收集触发器产出的新任务
            for trigger in self._triggers:
                try:
                    task = trigger.should_fire(self._ctx)
                except Exception as exc:
                    _log.opt(exception=True).warning(
                        '[Scheduler] 触发器 {} 异常: {}',
                        trigger.name,
                        exc,
                    )
                    continue
                if task is not None:
                    self._enqueue(task)

            # 取队首执行
            task = self._dequeue()
            if task is not None:
                results: list[CombatResult] = []
                crashed = False
                try:
                    results = self._run_task(task)
                except Exception as exc:
                    # _run_task 自身异常 (非 runner.run, 极少见) 兜底: 复位触发器
                    # _idle 避免该触发器卡死不再产出, 继续主循环, 不崩溃脚本。
                    crashed = True
                    _log.opt(exception=True).error(
                        '[Scheduler] 子任务 {} 执行崩溃, 已跳过: {}',
                        task.name,
                        exc,
                    )
                    if task.on_done is not None:
                        try:
                            task.on_done(
                                CombatResult(flag=ConditionFlag.ACTION_FAILED),
                            )
                        except Exception:  # noqa: S110  # on_done 回调异常不应影响主循环
                            pass

                # 故障熔断 (T2-1): 「崩溃」与「跑完但结果全是失败」都要计入,
                # 否则子任务异常被 _run_task 吞成 ACTION_FAILED 后异常不传播,
                # 主循环会在设备掉线/游戏闪退时无限空转。详见 _handle_failure。
                if crashed or self._is_failed_round(results):
                    self._consecutive_failures += 1
                    self._handle_failure(task, results, crashed)
                elif self._consecutive_failures:
                    _log.info(
                        '[Scheduler] 任务 {} 成功, 连续失败计数归零 ({} → 0)',
                        task.name,
                        self._consecutive_failures,
                    )
                    self._consecutive_failures = 0
            else:
                # 队列空:所有触发器暂无任务 (常规战打满 / 只等远征定时) → 挂机等待
                # 挂机期间顺带做常驻弹窗检查, 保证回到主页面时浮层不残留
                self._ensure_main_page_clean()
                time.sleep(self._idle_sleep)

        _log.info('[Scheduler] 收到停止信号, 调度结束')
        self._print_summary()

    # ═══════════════════════════════════════════════════════════════════════════
    # 故障恢复与熔断 (T2-1)
    # ═══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _is_failed_round(results: list[CombatResult]) -> bool:
        """本轮是否应计为「失败」。

        判定口径: 无结果 (任务未真正执行) 或**全部**结果都在
        :data:`_FAILURE_FLAGS` 中。混合结果 (部分成功部分失败) 不计失败 ——
        例如三连里D 点 SL 但A/C 成功, 该轮仍有产出, 不应触发故障处置。
        """
        if not results:
            return True
        return all(r.flag in _FAILURE_FLAGS for r in results)

    def _handle_failure(
        self,
        task: FightTask,
        results: list[CombatResult],
        crashed: bool,
    ) -> None:
        """失败处置: 分层恢复 (页面 → 设备 → 游戏 → 模拟器)。

        两级判断的原因: 绝大多数失败是**单轮偶发** (一次船坞处理失败、一次
        页面抖动), 立即重启游戏代价太大; 而设备真掉线时会持续失败, 8~10 分钟
        内必然累积到阈值 (见 :data:`DEFAULT_MAX_CONSECUTIVE_FAILURES`)。

        **先判设备再判游戏** (实机 2026-10-11 教训): 设备离线时
        ``restart_game`` 必然失败 (adb 指令都发不出去), 此前把两者当同一
        层处理, 导致模拟器关闭后进入高速死循环 —— 5 次「失败轮」在 0.1 秒内
        跑完, 60 秒冷却期形同虚设, 8 分钟写出10MB 日志。

        处置顺序 (副作用从小到大):

        1. 设备正常 → 回主页面; 连续失败达阈值 → 重启游戏
        2. 设备异常 → L2 重连 adb/视频流 (无副作用)
        3. 仍不可用 → L3 拉起模拟器 (有副作用, 需 ``--auto-launch-emulator``)
        """
        flags = [r.flag.value if r.flag else 'N/A' for r in results]
        _log.warning(
            '[Scheduler] 任务 {} 本轮失败 (连续第 {} 次{}{}): {}',
            task.name,
            self._consecutive_failures,
            f'/{self._max_consecutive_failures}'
            if self._max_consecutive_failures > 0
            else '',
            ', 崩溃' if crashed else '',
            flags or '无结果',
        )

        # ── 先判断设备是否可用 (决定走哪条恢复路径) ──
        if not self._ensure_device_usable():
            return

        # ── 设备正常: 可能是游戏/页面层问题 ──
        if self._max_consecutive_failures <= 0:
            self._try_recover_to_main()
            return

        if self._consecutive_failures < self._max_consecutive_failures:
            self._try_recover_to_main()
            self._apply_failure_backoff()
            return

        # 达阈值 → 熔断重启游戏, 但受冷却期保护
        now = time.monotonic()
        if now - self._last_restart_at < self._recovery_cooldown:
            _log.error(
                '[Scheduler] 已连续失败 {} 次, 但距上次重启仅 {:.0f}s '
                '(冷却期 {:.0f}s), 本轮不再重启',
                self._consecutive_failures,
                now - self._last_restart_at,
                self._recovery_cooldown,
            )
            # 冷却期内也要退避, 否则会高速空转
            self._apply_failure_backoff()
            return

        _log.error(
            '[Scheduler] 连续失败 {} 次达到阈值, 重启游戏 (任务 {})',
            self._consecutive_failures,
            task.name,
        )
        self._restart_game_safely()
        # 重启后重新计数, 让新连接有机会从干净状态跑起
        self._consecutive_failures = 0
        self._last_restart_at = now

    def _apply_failure_backoff(self) -> None:
        """失败轮之间退避。

        修复前的致命问题: 设备不可用时截图瞬间抛异常, 5 次「失败轮」在
        **0.1 秒内**跑完, 60 秒冷却期完全失效 → 高速死循环 (8 分钟 10MB
        日志)。指数退避保证失败轮之间有真实等待。
        """
        delay = self._recovery_policy.backoff_seconds(self._consecutive_failures)
        if delay > 0:
            _log.info('[Scheduler] 失败退避 {:.1f}s', delay)
            time.sleep(delay)

    def _ensure_device_usable(self) -> bool:
        """确保设备可用。返回 ``True`` 表示可以继续走游戏层恢复。

        这是**设备与游戏的分水岭**: 返回 ``False`` 意味着连 adb 都不通,
        此时任何「重启游戏」的尝试都必然失败, 必须在设备层解决。
        """
        try:
            outcome = recover_device(self._ctx, self._recovery_policy)
        except Exception as exc:  # noqa: BLE001 - 诊断本身不应打断主循环
            _log.opt(exception=True).warning('[Scheduler] 设备诊断异常: {}', exc)
            return True  # 诊断失败 ≠ 设备故障, 保守按原路径走

        if outcome is RecoveryOutcome.RECOVERED:
            self._consecutive_failures = 0
            _log.info('[Scheduler] 设备已恢复, 继续执行')
            return True

        if outcome is RecoveryOutcome.NEEDS_GAME_RESTART:
            return True

        # 设备不可用 → 尝试 L3 拉起模拟器 (仅在显式允许时)
        outcome = maybe_launch_emulator(self._ctx, self._recovery_policy)

        if outcome is RecoveryOutcome.RECOVERED:
            self._consecutive_failures = 0
            return True
        if outcome is RecoveryOutcome.NEEDS_GAME_RESTART:
            return True

        # 彻底无法恢复
        _log.error(
            '[Scheduler] 设备不可用且无法自动恢复 (auto_launch_emulator={}), '
            '本轮放弃。不重启游戏 (adb 不通, 重启必然失败)。',
            self._auto_launch_emulator,
        )
        self._consecutive_failures = 0
        self._last_restart_at = time.monotonic()
        # 退避: 设备长期不可用时不能高速空转
        self._apply_failure_backoff()
        return False

    def _try_recover_to_main(self) -> None:
        """轻量恢复: 回到主页面 (认不出页面时由被调方重启游戏)。

        自身异常一律吞掉 —— 恢复是「尽力而为」, 失败不该打断主循环。
        """
        try:
            # 函数内 import: ops 依赖 scheduler, 顶层 import 会成环
            from autowsgr.ops.startup import recover_to_main_or_restart

            recover_to_main_or_restart(self._ctx, self._ctx.config.account.game_app)
        except Exception as exc:
            _log.opt(exception=True).warning(
                '[Scheduler] 轻量恢复失败 (不影响主循环): {}',
                exc,
            )

    def _restart_game_safely(self) -> None:
        """重启游戏 (最重手段), 自身异常不得打断主循环。"""
        try:
            from autowsgr.ops import restart_game

            restart_game(self._ctx.ctrl)
            _log.info('[Scheduler] 游戏已重启')
        except Exception as exc:
            _log.opt(exception=True).error('[Scheduler] 重启游戏失败: {}', exc)

    def _enqueue(self, task: FightTask) -> None:
        """按 priority 插入队列 (数值小先出;同 priority FIFO)。"""
        idx = len(self._queue)
        for i, existing in enumerate(self._queue):
            if existing.priority > task.priority:
                idx = i
                break
        self._queue.insert(idx, task)
        _log.debug(
            '[Scheduler] 入队: {} (prio={}, 队列长度={})',
            task.name,
            task.priority,
            len(self._queue),
        )

    def _dequeue(self) -> FightTask | None:
        """取出队首任务 (priority 最小者)。"""
        if not self._queue:
            return None
        return self._queue.pop(0)

    def _check_daily_reset(self) -> None:
        """检测跨日 (0 点) → 通知所有触发器 reset。

        游戏每日 0 点刷新战役次数、演习时段、掉落上限。
        大部分由游戏自身重置 (脚本每次读画面值);脚本只需 reset 自身状态
        (战役 _exhausted、常规战完成计数、ctx 每日计数器)。
        """
        today = date.today()  # noqa: DTZ011  # 本地墙上时钟 (跨日检测)
        if today == self._last_date:
            return
        _log.info(
            '[Scheduler] 检测到跨日 ({} → {}), 重置触发器',
            self._last_date,
            today,
        )
        for trigger in self._triggers:
            try:
                trigger.reset()
            except Exception as exc:
                _log.opt(exception=True).warning(
                    '[Scheduler] 触发器 {} reset 异常: {}',
                    trigger.name,
                    exc,
                )
        # 清零 ctx 每日计数器 (掉落 / 快修累计)
        self._ctx.dropped_ship_count = 0
        self._ctx.dropped_loot_count = 0
        self._ctx.quick_repair_used = 0
        self._last_date = today

    # ── 启动校准 ──

    def _sync_initial_counts(self) -> None:
        """启动时校准每日掉落计数器。

        .. note::
            重构后已移除 ``stop_max_ship`` / ``stop_max_loot`` 上限, 每日掉落/
            船计数器不再用于门控常规战触发器, 因此无需启动时校准。此方法保留
            为空操作, 仅作兼容占位 (避免调用方改动)。
        """
        return

    # ── 远征检查 ──

    def _maybe_collect_expedition(self) -> None:
        """若距上次远征检查超过 interval，执行一次收取。"""
        if self._expedition_interval <= 0:
            return

        elapsed = time.monotonic() - self._last_expedition_time
        if elapsed < self._expedition_interval:
            return

        _log.info(
            '[Scheduler] 远征检查 (距上次 {:.0f}s)',
            elapsed,
        )
        try:
            from autowsgr.ops.expedition import collect_expedition

            collect_expedition(self._ctx)
        except Exception as exc:
            _log.opt(exception=True).warning(
                '[Scheduler] 远征检查失败: {}',
                exc,
            )

        self._last_expedition_time = time.monotonic()

    # ── 汇总 ──

    def _print_summary(self) -> None:
        """打印执行汇总。"""
        _log.info('[Scheduler] ' + '=' * 50)
        _log.info('[Scheduler] 调度完成')

        total_fights = 0
        total_success = 0

        for task in self._tasks:
            success = sum(1 for r in task.results if r.flag == ConditionFlag.OPERATION_SUCCESS)
            total_fights += len(task.results)
            total_success += success
            _log.info(
                '[Scheduler]   {} : {}/{} 次执行, {} 场结果, {} 成功',
                task.name,
                task.completed,
                task.times,
                len(task.results),
                success,
            )

        _log.info(
            '[Scheduler] 总计: {} 场战斗, {} 成功',
            total_fights,
            total_success,
        )
        _log.info('[Scheduler] ' + '=' * 50)
