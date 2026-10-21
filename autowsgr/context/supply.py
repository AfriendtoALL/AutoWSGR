"""补给运行时状态 — 游戏内自动补给复选框的会话级缓存。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SupplyState:
    """补给复选框的会话级缓存。

    游戏内「自动补给」复选框是持久状态，不随出征 / 页面切换重置。
    ``'auto'`` 模式下首次出征校正一次后，同一会话后续战斗无需再截图识别；
    ``'script'`` 模式仍每场校正，以确保没有残留勾选态。
    """

    checkbox_mode: str | None = None
    """最后一次成功校正的补给方式；``None`` 表示尚未校正。"""

    def checkbox_correct_for(self, mode: str) -> bool:
        """当前缓存的复选框状态是否已匹配指定补给方式。"""
        return self.checkbox_mode == mode

    def mark_checkbox_correct(self, mode: str) -> None:
        """记录复选框已校正到指定补给方式。"""
        self.checkbox_mode = mode

    def invalidate(self) -> None:
        """使缓存失效，下次调用会重新识别复选框。"""
        self.checkbox_mode = None
