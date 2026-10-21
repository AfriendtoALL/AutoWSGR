"""scrcpy 视频流解码线程的异常兜底测试（2026-10-12）。

发现的问题
----------
``_stream_loop`` 原本只捕 ``(ConnectionError, OSError)``。但实测
**``av.FFmpegError`` 不是 OSError 的子类** —— 码流损坏 / 解码器内部错误
会穿透该except，让解码线程**静默死亡**，而 ``_alive`` 仍是 ``True``。

后果比崩溃更糟：
- :meth:`ensure_alive` 看到 ``_alive=True`` 直接返回，**不做任何重连**
- :meth:`latest_frame` 一直返回最后一帧 → :meth:`screenshot` 正常返回
- 上层看到的是一张**过期画面**，表现为「界面卡住不动」而非任何报错

也就是说：模拟器/游戏出了任何解码侧问题，脚本会安静地拿着一张旧图
继续做模板匹配，然后一路失败到底，且日志里没有任何线索。

修复：增加 ``except BaseException`` 兜底，任何未预料的异常都必须
把 ``_alive`` 置否，让上层走重连路径。
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import MagicMock

import pytest


SOURCE = pathlib.Path('autowsgr/emulator/controller/video_stream.py')


def _stream_loop_body() -> str:
    """取出 ``_stream_loop`` 的源码，供静态断言。"""
    return ast.get_source_segment(SOURCE.read_text(encoding='utf-8'), _find_node('_stream_loop'))  # type: ignore[arg-type]


def _find_node(name: str):
    import ast

    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f'未找到 {name}')


# ══════════════════════════════════════════════
# 1. 静态结构：必须有兜底 except
# ══════════════════════════════════════════════


class TestStreamLoopHasCatchAll:
    def test_stream_loop_catches_base_exception(self) -> None:
        """解码循环必须有 ``except BaseException`` 兜底。

        用 AST 而非正则 —— 正则会漏掉嵌套结构，也认不出 except 的类型。
        """
        func = _find_node('_stream_loop')
        handlers = [
            h
            for n in ast.walk(func)
            if isinstance(n, ast.Try)
            for h in n.handlers
        ]
        caught = set()
        for h in handlers:
            if h.type is None:
                caught.add('bare')
            elif isinstance(h.type, ast.Name):
                caught.add(h.type.id)
            elif isinstance(h.type, ast.Tuple):
                caught.update(e.id for e in h.type.elts if isinstance(e, ast.Name))

        assert 'BaseException' in caught, (
            f'_stream_loop 必须有 except BaseException 兜底, 当前捕获: {sorted(caught)}'
        )

    def test_catch_all_sets_alive_false(self) -> None:
        """兜底分支必须把 ``_alive`` 置否，否则等于没兜。"""
        func = _find_node('_stream_loop')
        bodies = [
            h.body
            for n in ast.walk(func)
            if isinstance(n, ast.Try)
            for h in n.handlers
            if isinstance(h.type, ast.Name) and h.type.id == 'BaseException'
        ]
        assert bodies, '未找到 except BaseException 分支'
        # 手写遍历: ast.dump 只接受单个节点, 而这里拿到的是语句列表
        touches_alive = any(
            '_alive' in ast.dump(node)
            for stmt in bodies[0]
            for node in ast.walk(stmt)
        )
        assert touches_alive, '兜底分支必须操作 self._alive'


# ══════════════════════════════════════════════
# 2. 行为验证：真的把 _alive 置否吗
# ══════════════════════════════════════════════


def _make_stream():
    """构造一个只保留必要字段的 ScrcpyVideoStream（不建真实流）。"""
    from autowsgr.emulator.controller.video_stream import ScrcpyVideoStream

    s = ScrcpyVideoStream.__new__(ScrcpyVideoStream)
    s._alive = True
    s._video_socket = MagicMock()
    s._frame_lock = __import__('threading').Lock()
    s._last_frame = None
    s._resolution = (0, 0)
    s._frame_ready = __import__('threading').Event()
    return s


class TestStreamLoopRuntime:
    def test_non_oserror_exception_marks_stream_dead(self) -> None:
        """🔴 核心回归：非 OSError 的异常也必须把 _alive 置否。

        这里用 ``RuntimeError`` 代替 ``av.FFmpegError``（后者需要真实
        码流才能触发，且它同样不是 OSError 子类）。
        """
        stream = _make_stream()
        stream._video_socket.recv.side_effect = RuntimeError('模拟解码库内部错误')

        stream._stream_loop()

        assert stream._alive is False, (
            '解码线程遇到未预料异常后必须把 _alive 置否, '
            '否则 ensure_alive() 会误判健康、screenshot() 永远返回过期帧'
        )

    def test_ensure_alive_would_reconnect_after_catch_all(self) -> None:
        """兜底生效后, ensure_alive 的重连路径才可能被触发。"""
        stream = _make_stream()
        stream._video_socket.recv.side_effect = RuntimeError('boom')
        stream._stream_loop()
        # _alive 已为 False → 上层 screenshot() 会走 ensure_alive → reopen
        assert stream.alive is False

    def test_normal_disconnect_still_handled(self) -> None:
        """原有的正常断流处理不受影响。"""
        stream = _make_stream()
        stream._video_socket.recv.return_value = b''  # 对端关闭
        stream._stream_loop()
        assert stream._alive is False

    def test_blocking_io_error_keeps_running(self) -> None:
        """BlockingIOError 是正常背压, 不应置 _alive=False。"""
        import time as _time

        stream = _make_stream()
        calls = {'n': 0}

        def _recv(_size):
            calls['n'] += 1
            if calls['n'] < 3:
                raise BlockingIOError()
            stream._alive = False  # 主动收尾, 避免死循环
            return b''

        stream._video_socket.recv.side_effect = _recv
        orig_sleep = _time.sleep
        try:
            stream._stream_loop()
        finally:
            pass
        assert calls['n'] >= 3, 'BlockingIOError 后应继续重试 recv'
        assert stream._alive is False


def test_exception_class_hierarchy_is_as_documented() -> None:
    """记录前提：解码库异常不在 OSError 家族内。

    若将来 av 改了继承关系，本测试会提醒同步更新 ``_stream_loop`` 的
    except 列表（那时兜底虽冗余但无害，但说明前提变了）。
    """
    av = pytest.importorskip('av')
    err = getattr(av, 'FFmpegError', None)
    if err is None:  # pragma: no cover - 老版本 av 无此异常
        pytest.skip('当前 av 版本无 FFmpegError')
    assert not issubclass(err, OSError), (
        '若 av.FFmpegError 已成为 OSError 子类, _stream_loop 的兜底 except '
        '不再必需, 可考虑简化'
    )
