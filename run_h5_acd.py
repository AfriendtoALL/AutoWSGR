"""run_h5_acd.py — 跑「最强之舟 H5-A-C-D 三连夜战」。

三连夜战 = 困难 H5 走 A → C → D 连续打三个夜战点，
社区俗称"最强之舟"（H5 三点均为轻母 Ξ级IV型×3，无门神、免驻防、免带路）。

用法::

    # 打 10 轮（每轮 3 场），打完自动退出
    python run_h5_acd.py --times 10 --fleet 1

    # 挂机一直刷，按 Ctrl+C 优雅停止（等当前战斗结束）
    python run_h5_acd.py --times 0 --fleet 1

停止：第一次 Ctrl+C 优雅退出（等当前战斗结束），第二次 Ctrl+C 强制退出。
"""

import argparse
import signal
import sys
from pathlib import Path

from autowsgr.scheduler import TaskScheduler, build_daily_plan, launch
from autowsgr.scheduler.plan_runner import run_yaml_plan
from autowsgr.types import ConditionFlag

BASE = Path(__file__).parent
PLAN = BASE / 'plans' / 'H5-ACD.yaml'
SETTINGS = BASE / 'usersettings.yaml'


def main() -> None:
    ap = argparse.ArgumentParser(description='最强之舟 H5 ACD 三连夜战')
    ap.add_argument('--times', type=int, default=10, help='目标轮数，每轮 3 场；0 = 无限挂机')
    ap.add_argument('--fleet', type=int, default=1, help='出击舰队编号 1~4')
    ap.add_argument('--config', default=str(SETTINGS), help='usersettings.yaml 路径')
    args = ap.parse_args()

    ctx = launch(args.config)

    def _on_stop(signum, frame):
        if ctx.stop_event.is_set():
            print('\n收到第二次停止信号，强制退出。', file=sys.stderr)
            raise KeyboardInterrupt
        print('\n收到停止信号，等当前战斗结束后退出...', file=sys.stderr)
        ctx.stop_event.set()

    signal.signal(signal.SIGINT, _on_stop)

    if args.times <= 0:
        # 无限挂机：交给常驻调度器，按同一份计划反复出击
        scheduler = TaskScheduler(ctx, expedition_interval=0)
        build_daily_plan(scheduler, ctx)
        scheduler.run_daily()
        return

    # 一次性打满 N 轮：达不成自动收尾退出
    # results 元素 = 一次完整出征 (一轮 A-C-D 三连节点战) 的 CombatResult
    results = run_yaml_plan(ctx, str(PLAN), times=args.times, fleet_id=args.fleet)

    ok = sum(1 for r in results if r.flag == ConditionFlag.OPERATION_SUCCESS)
    print(f'\n共出击 {len(results)} 轮三连 (约 {len(results) * 3} 场节点战)，成功 {ok} 轮，退出。')


if __name__ == '__main__':
    main()
