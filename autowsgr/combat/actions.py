"""战斗操作函数 — 封装所有战斗中的点击与识别操作。

包含:
  - 坐标常量 (Coords)
  - UI 点击操作 (click_*)
  - 敌方编成/阵型识别 (get_enemy_info, get_enemy_formation)
  - 战果评级识别 (detect_result_grade)
  - 图像检查 (image_exist, click_image)

所有函数为无状态的纯操作，接收必要的对象参数后直接作用。

注: 血量 (HP) 检测、舰船掉落识别已在重构中移除；血量统一视作正常，
不再做像素级血条探测，也不再按掉落触发每日舰船上限。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from autowsgr.image_resources import TemplateKey, Templates
from autowsgr.image_resources.keys import RESULT_GRADE_KEYS
from autowsgr.infra import get_logger
from autowsgr.types import FightCondition, Formation
from autowsgr.vision import ImageChecker

from .recognition import (
    recognize_enemy_formation,
    recognize_enemy_ships,
)
from .recognizer import CombatRecognitionTimeoutError


if TYPE_CHECKING:
    from numpy import ndarray

    from autowsgr.emulator import AndroidController
    from autowsgr.vision.ocr import OCREngine


_log = get_logger('combat')


class Coords:
    """战斗中使用的所有坐标常量（相对值）。"""

    # ── 索敌阶段 ──
    RETREAT = (0.705, 0.911)
    """撤退按钮（索敌成功界面）。"""

    ENTER_FIGHT = (0.891, 0.928)
    """进入战斗按钮（索敌成功界面）。"""

    # ── 前进 / 回港 ──
    PROCEED_YES = (0.339, 0.648)
    """前进按钮。"""

    PROCEED_NO = (0.641, 0.648)
    """回港按钮。"""

    # ── 夜战 ──
    NIGHT_YES = (0.339, 0.648)
    """追击（进入夜战）。"""

    NIGHT_NO = (0.641, 0.648)
    """撤退（不进入夜战）。"""

    # ── 结算 ──
    CLICK_RESULT = (0.953, 0.954)
    """点击战果页面继续。"""

    # ── 结算盲点穿行 ──
    SETTLE_SKIP = (0.260, 0.980)
    """结算三层 (战果/MVP/掉落) 盲点穿行安全位。

    位置依据 (用户指定 2026-10-08, 2026-10-10 修正 x): 底部安全带必须取
    **左下象限** (x≈0.26) —— 阵型选择界面的 5 个阵型按钮列 (x 0.45~0.97)
    中「单横阵」一直贴到屏幕底缘, 底缘中部 (x=0.5) 会点中它 (用户
    2026-10-10 指出)。x=0.26 在全部相关页面 (结算三层/Ex-11 弹层/准备页/
    行军图/阵型界面) 上均无可交互元素。结算页全屏点击有效 (点掉当前层);
    对话框遮罩点击无效 —— 但对话框本身是识别候选, 识别到即由处理器接管。
    """

    # ── 加速点击 ──
    SPEED_UP_NORMAL = (0.260, 0.963)
    """常规战移动加速点击 (左下安全带)。

    2026-10-10 修正 x: 底缘中部 (x=0.5) 会点中阵型选择界面贴底的
    「单横阵」按钮 (按钮列 x 0.45~0.97), x 移至 0.26 完全避开右侧
    功能按钮列 (用户指出)。"""

    SPEED_UP_BATTLE = (0.260, 0.963)
    """战役加速点击 / 跳过导弹动画 (左下安全带, 同上)。"""


# ═══════════════════════════════════════════════════════════════════════════════
# 操作函数
# ═══════════════════════════════════════════════════════════════════════════════


def click_retreat(device: AndroidController) -> None:
    """点击撤退按钮（索敌成功界面）。"""
    device.click(*Coords.RETREAT)
    time.sleep(0.2)


def click_enter_fight(device: AndroidController) -> None:
    """点击进入战斗（索敌成功界面）。"""
    time.sleep(0.5)
    device.click(*Coords.ENTER_FIGHT)
    time.sleep(0.2)


def click_formation(device: AndroidController, formation: Formation) -> None:
    """选择阵型。

    Parameters
    ----------
    device:
        设备控制器。
    formation:
        目标阵型。
    """
    _log.debug('[Action] 选择阵型: {} ({})', formation.name, formation.value)
    x, y = formation.relative_position
    device.click(x, y)
    time.sleep(2.0)


def click_fight_condition(device: AndroidController, condition: FightCondition) -> None:
    """选择战况。
    Parameters
    ----------
    device:
        设备控制器。
    condition:
        目标战况。
    """
    x, y = condition.relative_click_position

    # 选择战况后，舰队会在地图上移动，不能添加延迟，否则会出现识别不到下一个点的问题
    device.click(x, y, delay=False)


def click_night_battle(device: AndroidController, pursue: bool) -> None:
    """夜战选择。

    Parameters
    ----------
    device:
        设备控制器。
    pursue:
        ``True`` = 追击（进入夜战），``False`` = 撤退。
    """
    _log.debug('[Action] 夜战选择: {}', '追击' if pursue else '撤退')
    if pursue:
        device.click(*Coords.NIGHT_YES)
    else:
        device.click(*Coords.NIGHT_NO)


def click_proceed(device: AndroidController, go_forward: bool) -> None:
    """继续前进 / 回港选择。

    Parameters
    ----------
    device:
        设备控制器。
    go_forward:
        ``True`` = 前进，``False`` = 回港。
    """
    _log.debug('[Action] 继续前进: {}', '前进' if go_forward else '回港')
    if go_forward:
        device.click(*Coords.PROCEED_YES)
    else:
        device.click(*Coords.PROCEED_NO)


def click_result(device: AndroidController) -> None:
    """点击战果页面继续。"""
    device.click(*Coords.CLICK_RESULT)


def click_settlement_skip(device: AndroidController) -> None:
    """结算盲点穿行: 点击结算安全位跳过 战果/MVP/掉落 三层。

    由引擎在快速结算模式的等待轮询中持续调用 (见
    :meth:`CombatEngine._get_poll_action`), 每次轮询截图后、匹配前执行,
    误触无害 (见 :attr:`Coords.SETTLE_SKIP`)。
    """
    device.click(*Coords.SETTLE_SKIP, delay=False)


def click_speed_up(device: AndroidController, *, battle_mode: bool = False) -> None:
    """点击加速（移动中或战役中）。

    点击位于底部安全带 (见 :attr:`Coords.SPEED_UP_NORMAL`) —— 行军/战役
    画面点击任意空白处均加速动画, 该带无可交互元素。

    Parameters
    ----------
    device:
        设备控制器。
    battle_mode:
        ``True`` 使用战役加速坐标，``False`` 使用常规战坐标。
    """
    coords = Coords.SPEED_UP_BATTLE if battle_mode else Coords.SPEED_UP_NORMAL

    # 由于加速过快可能导致卡顿, 增加极短延迟以确保点击生效
    time.sleep(0.05)
    device.click(*coords, delay=False)


def click_skip_missile_animation(device: AndroidController) -> None:
    """跳过导弹支援动画。"""

    # 快速点击不需要增加操作后的 delay
    device.click(*Coords.SPEED_UP_BATTLE, delay=False)
    time.sleep(1)
    device.click(*Coords.SPEED_UP_BATTLE, delay=False)


# ═══════════════════════════════════════════════════════════════════════════════
# 图像检查与识别函数（接收 device 和 template_key，内部完成匹配）
# ═══════════════════════════════════════════════════════════════════════════════


def image_exist(device: AndroidController, template_key: TemplateKey, confidence: float) -> bool:
    """检查模板是否存在于当前截图中。

    Parameters
    ----------
    device:
        设备控制器。
    template_key:
        模板标识符。
    confidence:
        匹配置信度 (0.0-1.0)。

    Returns
    -------
    bool
        ``True`` = 模板存在，``False`` = 不存在。
    """
    screen = device.screenshot()
    if isinstance(template_key, str):
        template_key = TemplateKey(template_key)
    templates = template_key.templates
    return ImageChecker.find_any(screen, templates, confidence=confidence) is not None


def click_image(device: AndroidController, template_key: TemplateKey, timeout: float) -> bool:
    """等待并点击模板图像中心。

    Parameters
    ----------
    device:
        设备控制器。
    template_key:
        模板标识符。
    timeout:
        最大等待时间（秒）。

    Returns
    -------
    bool
        ``True`` = 成功点击，``False`` = 超时未找到。
    """

    if isinstance(template_key, str):
        template_key = TemplateKey(template_key)
    deadline = time.time() + timeout
    while time.time() < deadline:
        screen = device.screenshot()
        templates = template_key.templates
        detail = ImageChecker.find_any(screen, templates, confidence=0.8)
        if detail is not None:
            device.click(*detail.center)
            return True
        time.sleep(0.3)
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# 敌方识别函数
# ═══════════════════════════════════════════════════════════════════════════════


def get_enemy_info(device: AndroidController, mode: str = 'fight') -> dict[str, int]:
    """识别敌方舰类编成。

    Parameters
    ----------
    device:
        设备控制器。
    mode:
        战斗模式 (``"fight"`` 或 ``"exercise"``)。

    Returns
    -------
    dict[str, int]
        敌方编成信息，如 ``{"BB": 2, "CV": 1, ...}``。
    """
    screen = device.screenshot()
    return recognize_enemy_ships(screen, mode=mode)


def get_enemy_formation(device: AndroidController, ocr_engine: OCREngine) -> str:
    """OCR 识别敌方阵型。
    Returns
    -------
    str
        敌方阵型名称，如 ``"单纵阵"``；若无 OCR 引擎则返回空字符串。
    """
    if ocr_engine is None:
        return ''
    screen = device.screenshot()
    return recognize_enemy_formation(screen, ocr_engine)


def detect_result_grade(device: AndroidController) -> str:
    """从战果结算截图识别评级 (SS/S/A/B/C/D)。
    Returns
    -------
    str
        战果等级。
    """
    retry = 0
    while retry < 5:
        screen = device.screenshot()
        for grade, key in RESULT_GRADE_KEYS.items():
            templates = key.templates
            if ImageChecker.find_any(screen, templates, confidence=0.8) is not None:
                return grade
        time.sleep(0.25)
        retry += 1
    raise CombatRecognitionTimeoutError('战果等级识别超时: 5 次尝试未识别到有效等级')


def dismiss_resource_confirm(device: AndroidController, screen: ndarray) -> None:
    """检测并关闭地图移动中弹出的资源获取/失去确认弹窗。

    仅对当前帧做一次快速检测（不阻塞等待），找到任意确认按钮模板就点击。
    """
    detail = ImageChecker.find_any(
        screen,
        [Templates.Confirm.CONFIRM_1],
        confidence=0.75,
    )
    if detail is not None:
        time.sleep(0.25)

        # 由于在地图移动的过程中，点击后必须立刻获取当前画面，因此不能加延迟
        device.click(*detail.center, delay=False)
        _log.info(
            "[Combat] 点掉资源确认弹窗: '{}' ({:.4f}, {:.4f})",
            detail.template_name,
            *detail.center,
        )
