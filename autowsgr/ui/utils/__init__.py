"""UI 工具函数包。

汇总导航工具与选船列表 OCR 工具，对外保持与原 ``autowsgr.ui.utils`` 完全一致的公共 API。
"""

from .navigation import (
    DEFAULT_NAV_CONFIG,
    NavConfig,
    NavigationError,
    click_and_wait_for_page,
    click_and_wait_leave_page,
    confirm_operation,
    wait_for_page,
    wait_leave_page,
)
from .ship_list import (
    LEGACY_HEIGHT,
    LEGACY_LIST_WIDTH,
    LEGACY_WIDTH,
    locate_ship_rows,
    recognize_ships_in_list,
    to_legacy_format,
)


__all__ = [
    'DEFAULT_NAV_CONFIG',
    'LEGACY_HEIGHT',
    'LEGACY_LIST_WIDTH',
    'LEGACY_WIDTH',
    'NavConfig',
    'NavigationError',
    'click_and_wait_for_page',
    'click_and_wait_leave_page',
    'confirm_operation',
    'locate_ship_rows',
    'recognize_ships_in_list',
    'to_legacy_format',
    'wait_for_page',
    'wait_leave_page',
]
