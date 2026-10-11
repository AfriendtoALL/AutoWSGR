"""最强之舟 H5 三连夜战练级 — 活动作战示例。

跑困难 H5 的 A → C → D 三连夜战练级。策略文件自动从
``autowsgr/data/plan/event/`` 解析 (文件名去掉 ``.yaml`` 后缀即可)。

与 :mod:`examples.event_fight` 的区别: 这里默认走三连路线 (A/C/D), 且带
命令行参数 (轮数 / 舰队 / 配置路径)。底层走融合后的
:func:`~autowsgr.ops.event_fight.run_event_fight_from_yaml`, 由 ``chapter: H``
自动切到活动战模式。

用法::

    # 打 10 轮 (每轮 = A+C+D 三场)，打完自动退出
    python examples/run_h5acd.py --times 10 --fleet 1

    # 直接 import 使用
    from autowsgr.ops import run_event_fight_from_yaml
    from autowsgr.scheduler import launch
    ctx = launch('usersettings.yaml')
    run_event_fight_from_yaml(ctx, 'H5ACD夜战', times=10, fleet_id=2)
"""

import argparse

from autowsgr.ops import run_event_fight_from_yaml
from autowsgr.scheduler import launch


def main() -> None:
    ap = argparse.ArgumentParser(description='最强之舟 H5 三连夜战练级')
    ap.add_argument(
        '--times',
        type=int,
        default=10,
        help='目标轮数 (每轮 = A+C+D 三场)',
    )
    ap.add_argument('--fleet', type=int, default=1, help='出击舰队编号 1~4')
    ap.add_argument(
        '--config',
        default='usersettings.yaml',
        help='usersettings.yaml 路径 (默认当前目录)',
    )
    args = ap.parse_args()

    ctx = launch(args.config)

    results = run_event_fight_from_yaml(
        ctx,
        'H5ACD夜战',
        times=args.times,
        fleet_id=args.fleet,
    )

    rounds = len(results) // 3
    print(f'完成 {len(results)} 场 (约 {rounds} 轮三连)，退出。')


if __name__ == '__main__':
    main()
