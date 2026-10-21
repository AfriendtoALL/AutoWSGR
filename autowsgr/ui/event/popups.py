"""活动答题浮层：识别完整提示后，在浮层内定位取消按钮。"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import TYPE_CHECKING

from autowsgr.infra.exceptions import ActionFailedError
from autowsgr.vision import ROI, ImageChecker


if TYPE_CHECKING:
    import numpy as np

    from autowsgr.emulator import AndroidController
    from autowsgr.vision import ImageMatchDetail, ImageTemplate


#: 活动答题浮层模板所属期号 (声明见 autowsgr/data/event/<期号>.yaml)。
#: 2026-10-11 T2-3: 模板路径由数据文件提供, 不再硬编码在 Python 里。
_QUIZ_EVENT = '20260930'


@lru_cache(maxsize=1)
def _quiz_templates() -> tuple[ImageTemplate, ImageTemplate]:
    from autowsgr.data.event_config import load_event_config

    cfg = load_event_config(_QUIZ_EVENT)
    prompt = cfg.templates('quiz_prompt')
    cancel = cfg.templates('quiz_cancel')
    return (prompt[0], cancel[0])


def quiz_prompt_detail(screen: np.ndarray) -> ImageMatchDetail | None:
    return ImageChecker.find_template(screen, _quiz_templates()[0], confidence=0.85)


def dismiss_quiz(ctrl: AndroidController) -> None:
    """最多取消三次，消失后才继续；不执行自动答题或点击其他取消按钮。"""
    for _ in range(3):
        screen = ctrl.screenshot()
        prompt = quiz_prompt_detail(screen)
        if prompt is None:
            return
        x1, y1 = prompt.top_left
        x2, y2 = prompt.bottom_right
        cancel = ImageChecker.find_template(
            screen,
            _quiz_templates()[1],
            roi=ROI(x1, y1, x2, y2),
            confidence=0.8,
        )
        if cancel is None:
            raise ActionFailedError('活动答题提示已出现，但未找到取消按钮')
        ctrl.click(*cancel.center)
        time.sleep(0.3)
    if quiz_prompt_detail(ctrl.screenshot()) is not None:
        raise ActionFailedError('活动答题提示取消三次后仍未关闭')
