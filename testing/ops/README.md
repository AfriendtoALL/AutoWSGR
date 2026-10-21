# ops 实机验证脚本

本目录下的 `*.py`（**不带 `test_` 前缀**的那些）是**需要真实模拟器的交互式
验证脚本**，不是 pytest 测试。

## 为什么它们不是 pytest 测试

这些脚本会：

1. 连接真实模拟器（`launch_for_test()` → `ScrcpyController`）
2. 在真实游戏里点击、出征、收取
3. 阻塞直到操作完成

因此它们**不能**被 pytest 收集——一旦被收集，CI 或本地跑全量测试时会尝试
连接不存在的设备，产生大量无意义的失败。

`testing/ops/` 下真正的 pytest 测试是 `test_*.py`（如
`test_normal_fight_unit.py`），那些是**无设备**的单元测试。

## 正确用法

手动运行，需要模拟器已启动：

```bash
# 自动检测设备
python testing/ops/build.py

# 指定设备
python testing/ops/build.py 127.0.0.1:16416
```

各脚本的具体参数见其 `--help` 或文件顶部 docstring。

## 收录的脚本

| 脚本 | 验证内容 |
| --- | --- |
| `build.py` | 建造收取 (`collect_built_ships`) |
| `campaign.py` | 战役 (`CampaignRunner`) |
| `cook.py` | 食堂做菜 (`cook`) |
| `decisive_battle.py` | 决战 (`DecisiveController`) |
| `destroy.py` | 舰船解装 (`destroy_ships`) |
| `event_fight.py` | 活动战 |
| `exercise.py` | 演习 (`run_exercise`) |
| `expedition.py` | 远征收取 (`collect_expedition`) |
| `normal_fight.py` | 常规战 7-4 |
| `repair.py` | 浴室修理 (`repair_in_bath`) |
| `reward.py` | 领取奖励 |
| `scheduler.py` | 调度器 |
| `startup.py` | 启动流程 |

## 维护约定

**不要把这些文件重命名为 `test_*.py`**。历史上曾有「测试文件命名不规范导致
pytest 未收集」的误判，实际这些文件本就不该被收集。

若要为某个 ops 补**真正的**单元测试，请新建 `test_<功能>_unit.py`，
用 mock 替代真机依赖（可参考 `test_normal_fight_unit.py` 的写法）。

`_framework.py` 提供共享的 `launch_for_test()`，供上述脚本复用。