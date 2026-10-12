"""卡死诊断 — 停滞时自动 dump 调用栈 (2026-10-12)。

要解决什么
----------
7x24 挂机的故障分两种，处置方式完全不同：

- **失败返回**：抛异常 → 现有熔断接手（``scheduler/recovery.py``，阈值 5）
- **永不返回**：卡在某处不动 → 熔断**不会被触发**，只能靠人肉翻日志

本模块处理第二种。实测（2026-10-11 用户手动关 MuMu）后者**不是**「进程挂死」，
而是**高频重复无效动作**：日志里每 0.15s 一次 ``click_speed_up``，
界面毫无进展。真正让人失去判断力的是「看不出它到底卡在哪一行」。

Alas 的 ``show_function_call()`` 就是为解决这个问题；Python 标准库的
:mod:`faulthandler` 用十几行就能达到同等效果，且是**纯诊断、不改变控制流**
—— 卡住时把完整栈写进日志，让事后排查有据可查。

为什么不是「自动恢复」
----------------------
无法安全地杀掉一个卡在 ctypes / C 扩展里的线程 —— 那不是 Python异常，
``except`` 捕不到，强杀会导致设备状态错乱。所以这里**只报告不恢复**，
恢复交给已有的超时（``screenshot_timeout``）与熔断。

用法
----
在 :func:`~autowsgr.infra.logger.setup_logger` 之后调用一次::

    from autowsgr.infra.stuck_watchdog import arm_stall_watchdog
    arm_stall_watchdog(log_dir, timeout=180)
"""

from __future__ import annotations

import faulthandler
import sys
from pathlib import Path


#: 默认停滞判定阈值(秒)。选180 的理由：现有各层超时里最大的是
#: ``NIGHT_PROMPT`` 150s（等待玩家/动画），其余多在 30s 以内；
#: 180s 能容过所有「正常的慢」，超过就基本可判定为停滞。
DEFAULT_STALL_TIMEOUT = 180.0

#: dump 间隔(秒)：达标后每间隔这么久重复dump 一次，便于看出「确实一直卡着」
#: 而不是「卡了一下恢复了」。
_DEFAULT_DUMP_REPEAT = 60.0

_arm_state: dict[str, object] = {}


def arm_stall_watchdog(
    log_dir: Path | str | None,
    *,
    timeout: float = DEFAULT_STALL_TIMEOUT,
    repeat: float = _DEFAULT_DUMP_REPEAT,
) -> bool:
    """开启停滞看门狗：主线程超时无任何输出则 dump 调用栈。

    Parameters
    ----------
    log_dir:
        日志目录。栈会写到 ``<log_dir>/stalled_traceback.log``。
        传 ``None`` 则写到标准错误流（控制台可见但不留存）。
    timeout:
        停滞判定阈值(秒)。**从「最近一次标准输出/标准错误写入」起算**，
        不是「从调用本函数起算」—— 否则一次正常的长战斗（期间无打印）
        会被误判。
    repeat:
        达标后每隔多久重复 dump 一次。

    Returns
    -------
    bool
        是否成功开启。已开启过则返回 ``False``（不重复注册）。

    Notes
    -----
    用的是 ``dump_traceback_later`` 而非自建定时线程 —— 它由 CPython 的
    看门狗线程实现，**即使主线程卡在 ctypes 里也能触发**（自建 Python 线程
    在 GIL 被持有时反而可能跑不起来）。
    """
    if _arm_state.get('armed'):
        return False

    if log_dir is None:
        target = sys.stderr
    else:
        path = Path(log_dir) / 'stalled_traceback.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        target = open(path, 'a', encoding='utf-8')  # noqa: SIM115 # 进程级长驻, 由 OS 回收

    try:
        faulthandler.enable(file=target)
        # dump_traceback_later 只接受整数秒, 传 float 会抛 TypeError
        faulthandler.dump_traceback_later(
            max(1, int(timeout)),
            repeat=max(1, int(repeat)),
            file=target,
            exit=False,
        )
    except (ValueError, RuntimeError, TypeError):
        # dump_traceback_later 在解释器关闭中或不支持的环境会抛; 不该因此崩启动
        return False

    _arm_state['armed'] = True
    _arm_state['target'] = target
    _arm_state['timeout'] = timeout
    return True


def disarm_stall_watchdog() -> None:
    """关闭停滞看门狗（测试/热重载用）。"""
    if not _arm_state.get('armed'):
        return
    try:
        faulthandler.cancel_dump_traceback_later()
    except (ValueError, RuntimeError):  # pragma: no cover
        pass
    target = _arm_state.get('target')
    if target not in (None, sys.stderr, sys.stdout):
        try:
            target.close()  # type: ignore[union-attr]
        except OSError:  # pragma: no cover
            pass
    _arm_state.clear()


def is_armed() -> bool:
    """看门狗是否已开启（供测试与状态上报）。"""
    return bool(_arm_state.get('armed'))