"""H5-ACD 作战计划的配置一致性测试。

背景 (2026-10-11)
-----------------
项目里有两份同名同用途的 H5-ACD 三连计划:

- ``plans/H5-ACD.yaml``                                (桌面启动器实际使用)
- ``autowsgr/data/plan/event/H5ACD夜战.yaml``          (examples/run_h5acd.py 使用)

两份曾长期分叉: 后者用 ``formation: 4`` (梯形阵), 前者用 ``formation: 1``
(单纵阵), 且后者缺 ``name`` 与 ``fight_condition`` 两项。用户确认统一为
**单纵阵** 后已修正。

分叉的危害是静默的: 两份都能正常加载, 跑起来才发现行为不同。
本测试锁住关键字段, 任一份被改动而另一份没跟上时立即失败。

注意: ``CombatPlan.name`` 来自**文件名**(``Path(path).stem``, 见
``plan.py:417``), 不是 YAML 里的 ``name:`` 字段 —— 故两份的 name 必然
不同 (H5-ACD vs H5ACD夜战), 不纳入一致性断言。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autowsgr.combat.plan import CombatPlan


# ═══════════════════════════════════════════════════════════════════════════════
# 路径
# ═══════════════════════════════════════════════════════════════════════════════

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROOT_PLAN = PROJECT_ROOT / 'plans' / 'H5-ACD.yaml'
PACKAGE_PLAN = PROJECT_ROOT / 'autowsgr' / 'data' / 'plan' / 'event' / 'H5ACD夜战.yaml'


@pytest.fixture(scope='module')
def root_plan() -> CombatPlan:
    return CombatPlan.from_yaml(ROOT_PLAN)


@pytest.fixture(scope='module')
def package_plan() -> CombatPlan:
    return CombatPlan.from_yaml(PACKAGE_PLAN)


# ═══════════════════════════════════════════════════════════════════════════════
# 两份文件均存在
# ═══════════════════════════════════════════════════════════════════════════════


def test_both_plans_exist() -> None:
    """两份计划都必须存在 —— 缺一个说明文件被误删。"""
    assert ROOT_PLAN.exists(), f'缺少 {ROOT_PLAN}'
    assert PACKAGE_PLAN.exists(), f'缺少 {PACKAGE_PLAN}'


# ═══════════════════════════════════════════════════════════════════════════════
# 标量字段一致性
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    'field',
    ['chapter', 'map_id', 'fleet_id', 'fight_condition', 'event_name', 'mode', 'repair_mode'],
)
def test_scalar_fields_match(root_plan: CombatPlan, package_plan: CombatPlan, field: str) -> None:
    """活动代号/章节/地图/舰队/战况/模式等必须完全一致。"""
    assert getattr(root_plan, field) == getattr(package_plan, field), (
        f'{field} 不一致: {ROOT_PLAN.name}={getattr(root_plan, field)} '
        f'vs {PACKAGE_PLAN.name}={getattr(package_plan, field)}'
    )


def test_selected_nodes_match(root_plan: CombatPlan, package_plan: CombatPlan) -> None:
    """节点白名单必须一致 (A/C/D 三连)。"""
    assert root_plan.selected_nodes == package_plan.selected_nodes


# ═══════════════════════════════════════════════════════════════════════════════
# 逐节点决策一致性 (核心)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize('node', ['A', 'C', 'D'])
def test_node_decisions_match(
    root_plan: CombatPlan,
    package_plan: CombatPlan,
    node: str,
) -> None:
    """每个节点的阵型/夜战/前进决策必须一致。

    这是本次修正的核心 —— 历史上包内那份是 ``formation: 4``(梯形阵)。
    """
    assert root_plan.get_node_decision(node) == package_plan.get_node_decision(node), (
        f'节点 {node} 决策不一致: '
        f'{ROOT_PLAN.name}={root_plan.get_node_decision(node)} '
        f'vs {PACKAGE_PLAN.name}={package_plan.get_node_decision(node)}'
    )


def test_formation_is_single_column(
    root_plan: CombatPlan,
    package_plan: CombatPlan,
) -> None:
    """两份都必须是**单纵阵** (用户 2026-10-11 确认)。

    显式断言而非只比较两者相等 —— 若两份同时被改成梯形阵, 仅靠
    「相等」断言无法发现意图偏离。
    """
    from autowsgr.types import Formation

    for plan, path in ((root_plan, ROOT_PLAN), (package_plan, PACKAGE_PLAN)):
        decision = plan.get_node_decision('A')
        assert decision.formation is Formation.single_column, (
            f'{path.name} 阵型应为单纵阵, 实为 {decision.formation}'
        )