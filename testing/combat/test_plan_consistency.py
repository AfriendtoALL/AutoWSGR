"""H5-ACD 作战计划的关键配置断言。

背景 (2026-10-11)
-----------------
项目里曾有**两份**用途相同的 H5-ACD 三连计划:

- ``plans/H5-ACD.yaml``                         (根目录, 桌面启动器使用)
- ``autowsgr/data/plan/event/H5ACD夜战.yaml``   (包内, examples 使用)

两者长期分叉: 根目录那份用 ``formation: 1`` (单纵阵), 包内那份用
``formation: 4`` (梯形阵) 且缺 ``name`` 与 ``fight_condition``。分叉是
**静默的** —— 两份都能正常加载, 跑起来才发现行为不同。

用户确认统一为**单纵阵**后, 根目录那份已删除, 计划统一到包内
``autowsgr/data/plan/event/H5ACD夜战.yaml``。

分叉既然发生过一次, 就该锁住: 本测试显式断言关键配置, 任何人把阵型
改成梯形阵 / 漏掉 fight_condition 时立即失败, 而不必等实机跑一轮才发现。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autowsgr.combat.plan import CombatPlan
from autowsgr.types import FightCondition, Formation


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLAN_PATH = PROJECT_ROOT / 'autowsgr' / 'data' / 'plan' / 'event' / 'H5ACD夜战.yaml'


@pytest.fixture(scope='module')
def plan() -> CombatPlan:
    return CombatPlan.from_yaml(PLAN_PATH)


# ═══════════════════════════════════════════════════════════════════════════════
# 文件位置
# ═══════════════════════════════════════════════════════════════════════════════


def test_plan_file_exists() -> None:
    assert PLAN_PATH.exists(), f'计划文件缺失: {PLAN_PATH}'


def test_legacy_root_plan_is_gone() -> None:
    """根目录 ``plans/`` 下的旧副本必须已删除。

    它与包内计划曾长期分叉(阵型/战况不同)。若哪天它又回来了, 脚本可能
    指向错误的那份 —— 显式断言它不存在, 把「单一事实来源」锁死。
    """
    legacy = PROJECT_ROOT / 'plans' / 'H5-ACD.yaml'
    assert not legacy.exists(), (
        f'根目录副本 {legacy.name} 已重新出现。请删除它, 计划只保留包内一份。'
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 关键标量配置
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ('field', 'expected'),
    [
        ('chapter', 'H'),
        ('map_id', 5),
        ('fleet_id', 1),
        ('event_name', '20260930'),
    ],
)
def test_map_level_config(plan: CombatPlan, field: str, expected: object) -> None:
    """关卡级配置锁定: 困难 H5、第1 舰队、活动 20260930。"""
    assert getattr(plan, field) == expected


def test_fight_condition_is_steady_advance(plan: CombatPlan) -> None:
    """战况必须显式为「稳步前进」。

    这项曾**整体缺失**于包内那份 (未指定时走默认值), 现已显式写出并锁定,
    避免「补齐字段」时不小心引入行为变化。
    """
    assert plan.fight_condition is FightCondition.steady_advance


def test_selected_nodes_are_acd(plan: CombatPlan) -> None:
    """节点白名单必须是 A / C / D 三连。"""
    assert sorted(plan.selected_nodes) == ['A', 'C', 'D']


# ═══════════════════════════════════════════════════════════════════════════════
# 逐节点决策 (核心: 阵型)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize('node', ['A', 'C', 'D'])
def test_node_formation_is_single_column(plan: CombatPlan, node: str) -> None:
    """三个节点都必须是**单纵阵** (用户 2026-10-11 确认)。

    这是历史上两份计划分叉的那一项, 故逐节点显式断言。
    """
    decision = plan.get_node_decision(node)
    assert decision.formation is Formation.single_column, (
        f'节点 {node} 阵型应为单纵阵, 实为 {decision.formation}'
    )


@pytest.mark.parametrize('node', ['A', 'C', 'D'])
def test_node_nights_and_proceeds(plan: CombatPlan, node: str) -> None:
    """三个点均追击夜战; D 点回港收工, A/C 继续前进。"""
    decision = plan.get_node_decision(node)
    assert decision.night is True, f'节点 {node} 应追击夜战'
    assert decision.proceed is (node != 'D'), (
        f'节点 {node} 的 proceed 应为 {node != "D"} (D 点回港收工)'
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 入口默认指向
# ═══════════════════════════════════════════════════════════════════════════════


def test_entry_default_plan_points_to_package_copy() -> None:
    """H5-ACD 入口的默认计划必须指向包内那份。

    防止 ``DEFAULT_PLAN`` 指向已删除的根目录副本导致启动即报错。
    """
    from autowsgr.scheduler.h5_acd_entry import DEFAULT_PLAN

    assert DEFAULT_PLAN == PLAN_PATH, (
        f'入口默认计划指向 {DEFAULT_PLAN}, 应为 {PLAN_PATH}'
    )