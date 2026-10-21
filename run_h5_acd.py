"""run_h5_acd.py — 跑「最强之舟 H5-A-C-D 三连夜战」。

三连夜战 = 困难 H5 走 A → C → D 连续打三个夜战点,
社区俗称"最强之舟"(H5 三点均为轻母 Ξ级IV型×3,无门神、免驻防、免带路)。

用法::

    # 打 10 轮(每轮 3 场),打完自动退出
    python run_h5_acd.py --times 10 --fleet 1

    # 挂机一直刷,按 Ctrl+C 优雅停止(等当前战斗结束)
    python run_h5_acd.py --times 0 --fleet 1

    # 换一份作战计划(如打 E5 简单图)
    python run_h5_acd.py --times 10 --plan autowsgr/data/plan/event/E5ACD夜战.yaml

停止:第一次 Ctrl+C 优雅退出(等当前战斗结束),第二次 Ctrl+C 强制退出。

注意: 本脚本留在**项目根目录**, 因为桌面启动器 ``最强之舟挂机.bat`` 按相对
路径调用它 (``python run_h5_acd.py``)。移动位置需同步改启动器。

本文件只是薄壳 —— 路径解析、熔断参数等统一走
:mod:`autowsgr.scheduler.h5_acd_entry`, 便于被启动器/其他入口复用。
"""

from __future__ import annotations

import sys

from autowsgr.scheduler.h5_acd_entry import main


if __name__ == '__main__':
    sys.exit(main())