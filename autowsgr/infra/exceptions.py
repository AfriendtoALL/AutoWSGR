"""AutoWSGR 异常层级体系。

层级树::

    AutoWSGRError
    ├── ConfigError
    ├── EmulatorError
    │   ├── EmulatorConnectionError
    │   └── EmulatorNotFoundError
    ├── VisionError
    │   ├── ImageNotFoundError
    │   └── OCRError
    ├── UIError
    │   ├── PageNotFoundError
    │   ├── NavigationError
    │   └── ActionFailedError
    ├── GameError
    │   ├── DockFullError
    │   └── ResourceError
    ├── CombatError
    │   ├── CombatRecognitionTimeoutError
    │   └── CombatDecisionError
    └── CriticalError
"""

from __future__ import annotations


# ── 基类 ──


class AutoWSGRError(Exception):
    """所有 AutoWSGR 异常的基类。"""


# ── 基础设施异常 ──


class ConfigError(AutoWSGRError):
    """配置错误（文件缺失、字段非法等）。"""


# ── 模拟器异常 ──


class EmulatorError(AutoWSGRError):
    """模拟器操作失败。"""


class EmulatorConnectionError(EmulatorError):
    """模拟器连接失败。"""


class EmulatorNotFoundError(EmulatorError):
    """未检测到模拟器。"""


class RepeatedActionError(EmulatorError):
    """短时间内重复同一无效动作过多，判定界面无进展（疑似卡死）。

    典型场景（实机 2026-10-11 手动关闭 MuMu）：画面已停滞在某页，
    而脚本仍在按既定节奏反复点同一坐标 —— 每 0.15s 一次，永不放弃。

    与「卡住不动」的区别：进程仍在跑、日志仍在写，但**没有实际进展**。
    这类故障逃过了所有超时与熔断（它们只处理「异常返回」），
    故单独用异常显式暴露，交由调度器熔断接手。
    """


# ── 视觉层异常 ──


class VisionError(AutoWSGRError):
    """视觉识别相关错误。"""


class OCRError(VisionError):
    """OCR 识别失败。"""


class ImageNotFoundError(VisionError):
    """模板图像在屏幕上未找到。"""

    def __init__(self, template_name: str = '', timeout: float = 0) -> None:
        self.template_name = template_name
        self.timeout = timeout
        msg = f'未找到图像: {template_name}'
        if timeout:
            msg += f' (超时 {timeout:.1f}s)'
        super().__init__(msg)


# ── UI 层异常 ──


class UIError(AutoWSGRError):
    """UI 操作相关错误。"""


class PageNotFoundError(UIError):
    """无法识别当前页面。"""


class NavigationError(UIError):
    """页面导航失败（从 A 到 B 的转移失败）。"""

    def __init__(self, source: str, target: str, reason: str = '') -> None:
        self.source = source
        self.target = target
        msg = f'导航失败: {source} -> {target}'
        if reason:
            msg += f' ({reason})'
        super().__init__(msg)


class ActionFailedError(UIError):
    """UIAction 执行失败。"""

    def __init__(self, action_name: str, reason: str = '') -> None:
        self.action_name = action_name
        msg = f'操作失败: {action_name}'
        if reason:
            msg += f' ({reason})'
        super().__init__(msg)


# ── 游戏逻辑异常 ──


class GameError(AutoWSGRError):
    """游戏逻辑错误。"""


class NetworkError(GameError):
    """游戏网络错误（断线、卡顿）。"""


class DockFullError(GameError):
    """船坞已满。"""


class ResourceError(GameError):
    """资源不足。"""


# ── 战斗系统异常 ──


class CombatError(AutoWSGRError):
    """战斗系统错误。"""


class CombatRecognitionTimeoutError(CombatError):
    """战斗状态识别超时。"""

    def __init__(self, candidates: list[str] | None = None, timeout: float = 0) -> None:
        self.candidates = candidates or []
        self.timeout = timeout
        names = ', '.join(self.candidates)
        super().__init__(f'战斗状态识别超时 ({timeout:.1f}s): [{names}]')


class CombatDecisionError(CombatError):
    """战斗决策错误（规则配置问题等）。"""


# ── 不可恢复错误 ──


class CriticalError(AutoWSGRError):
    """不可恢复的严重错误，需要终止。"""
