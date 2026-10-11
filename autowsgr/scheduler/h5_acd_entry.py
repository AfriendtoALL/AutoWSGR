"""最强之舟 H5-ACD 三连夜战入口 (供 run_h5_acd.py / 桌面启动器复用)。

从 :mod:`run_h5_acd` 抽出的可复用实现, 目的是让「H5-ACD 挂机」这件事的
路径解析与熔断参数只存在于一处 —— 原脚本把它们写死在模块级常量里,
外部无法在不改文件的情况下调整。

两条执行路径 (语义不同, 不要混淆):

- ``--times N`` (N>0): **一次性打满N 轮**, 内部走
  :func:`~autowsgr.scheduler.plan_runner.run_yaml_plan`, 达标即退出。
  这条路径**不受调度器熔断保护** (它不是常驻循环, 失败会直接抛出)。
- ``--times 0``: **无限挂机**, 交给 :class:`~autowsgr.scheduler.TaskScheduler`
  常驻循环, 享受故障恢复与熔断保护 (连续失败 5次 → 重启游戏, 见
  :data:`~autowsgr.scheduler.scheduler.DEFAULT_MAX_CONSECUTIVE_FAILURES`)。
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from autowsgr.combat import CombatResult
from autowsgr.scheduler import TaskScheduler, build_daily_plan, launch
from autowsgr.scheduler.plan_runner import run_yaml_plan
from autowsgr.scheduler.scheduler import (
    DEFAULT_MAX_CONSECUTIVE_FAILURES,
    DEFAULT_RECOVERY_COOLDOWN,
)
from autowsgr.types import ConditionFlag


PROJECT_ROOT = Path(__file__).resolve().parents[2]
"""项目根目录 (本文件位于 ``<root>/autowsgr/scheduler/``)。"""

DEFAULT_PLAN = PROJECT_ROOT / 'plans' / 'H5-ACD.yaml'
"""默认作战计划。

项目内另有一份用途相同的计划
``autowsgr/data/plan/event/H5ACD夜战.yaml`` (供 ``examples/run_h5acd.py``
使用)。两者曾分叉 (那份用 formation: 4 梯形阵且缺 ``fight_condition``),
用户 2026-10-11 确认统一为**单纵阵** 后已统一。

一致性由 ``testing/combat/test_plan_consistency.py`` 锁定 —— 改动其中
一份时必须同步另一份, 否则测试会失败。
"""

DEFAULT_SETTINGS = PROJECT_ROOT / 'usersettings.yaml'
"""默认用户配置。"""


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    ap = argparse.ArgumentParser(
        description='最强之舟 H5 ACD 三连夜战',
        epilog=(
            '--times 0 为无限挂机 (带故障熔断保护, 连续失败 '
            f'{DEFAULT_MAX_CONSECUTIVE_FAILURES} 次自动重启游戏)'
        ),
    )
    ap.add_argument(
        '--times',
        type=int,
        default=10,
        help='目标轮数，每轮 3 场；0 = 无限挂机',
    )
    ap.add_argument('--fleet', type=int, default=1, help='出击舰队编号 1~4')
    ap.add_argument(
        '--config',
        default=str(DEFAULT_SETTINGS),
        help='usersettings.yaml 路径',
    )
    ap.add_argument(
        '--plan',
        default=str(DEFAULT_PLAN),
        help=f'作战计划 YAML 路径 (默认 {DEFAULT_PLAN.name})',
    )
    ap.add_argument(
        '--max-failures',
        type=int,
        default=DEFAULT_MAX_CONSECUTIVE_FAILURES,
        help=(
            '无限挂机模式下, 连续失败多少次后重启游戏;'
            ' 0 = 禁用熔断 (仅轻量恢复)'
        ),
    )
    ap.add_argument(
        '--recovery-cooldown',
        type=float,
        default=DEFAULT_RECOVERY_COOLDOWN,
        help='两次重启游戏之间的最小间隔 (秒)',
    )
    ap.add_argument(
        '--auto-launch-emulator',
        action='store_true',
        help=(
            '设备不可用时允许脚本拉起模拟器实例 (默认关闭)。'
            '注意: 该操作会占用内存/显存, 且模拟器可能是用户主动关闭的'
        ),
    )
    return ap


def _install_stop_handler(ctx) -> None:
    """安装 SIGINT 处理器: 第一次优雅停, 第二次强退。

    优雅停 = 置 ``stop_event``, 让当前战斗打完再退出 (避免半途丢弃战果)。
    """

    def _on_stop(_signum, _frame):
        if ctx.stop_event.is_set():
            print('\n收到第二次停止信号，强制退出。', file=sys.stderr)
            raise KeyboardInterrupt
        print('\n收到停止信号，等当前战斗结束后退出...', file=sys.stderr)
        ctx.stop_event.set()

    signal.signal(signal.SIGINT, _on_stop)


def _print_summary(results: list[CombatResult]) -> None:
    """打印一次性模式的结果统计。"""
    ok = sum(1 for r in results if r.flag == ConditionFlag.OPERATION_SUCCESS)
    print(
        f'\n共出击 {len(results)} 轮三连 (约 {len(results) * 3} 场节点战)，'
        f'成功 {ok} 轮，退出。',
    )


def run_once(ctx, *, times: int, fleet_id: int, plan_path: str) -> int:
    """一次性打满 N 轮。返回进程退出码。"""
    # results 元素 = 一次完整出征 (一轮 A-C-D 三连节点战) 的 CombatResult
    results = run_yaml_plan(ctx, plan_path, times=times, fleet_id=fleet_id)
    _print_summary(results)
    return 0


def run_forever(
    ctx,
    *,
    max_failures: int,
    recovery_cooldown: float,
    auto_launch_emulator: bool = False,
) -> int:
    """无限挂机 (常驻调度器, 带故障熔断)。返回进程退出码。"""
    scheduler = TaskScheduler(
        ctx,
        expedition_interval=0,  # 远征由触发器接管
        max_consecutive_failures=max_failures,
        recovery_cooldown=recovery_cooldown,
        auto_launch_emulator=auto_launch_emulator,
    )
    build_daily_plan(scheduler, ctx)
    scheduler.run_daily()
    return 0


def main(argv: list[str] | None = None) -> int:
    """命令行入口。返回进程退出码。"""
    args = build_parser().parse_args(argv)

    plan_path = Path(args.plan)
    if not plan_path.exists():
        print(f'错误: 作战计划不存在: {plan_path}', file=sys.stderr)
        return 2

    ctx = launch(args.config)
    _install_stop_handler(ctx)

    if args.times <= 0:
        # 无限挂机: 交给常驻调度器, 按同一份计划反复出击
        return run_forever(
            ctx,
            max_failures=args.max_failures,
            recovery_cooldown=args.recovery_cooldown,
            auto_launch_emulator=args.auto_launch_emulator,
        )

    return run_once(ctx, times=args.times, fleet_id=args.fleet, plan_path=str(plan_path))