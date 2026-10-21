"""MaaTouch 触摸注入通道 (协议兼容 minitouch, 改造自 ALAS)。

设备端: ``CLASSPATH=/data/local/tmp/maatouch app_process / com.shxyke.MaaTouch.App``
以持久 adb shell 流运行注入服务; PC 端通过 **subprocess 管道**写入
minitouch 协议命令, 写入即注入 (无 Android ``input`` 命令的 100ms+ 延迟)。

对比 PersistentAdbShell (adb input + 管道): **服务进程死亡时 stdout 立即
EOF (读线程转队列哨兵), 点击前即可发现并重建** —— 静默失效从机制上
不可能。

协议 (https://github.com/MaaAssistantArknights/MaaTouch):
- 握手: ``^ <max_contacts> <max_x> <max_y> <max_pressure>`` + ``$ <pid>``
- 触摸: ``d <contact> <x> <y> <pressure>\\n`` ``c\\n`` (commit)
        ``u <contact>\\n`` ``c\\n``
- 未安装时输出 ``Aborted``
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time

from autowsgr.infra.logger import get_logger

_log = get_logger('emulator')


class MaaTouchUnavailable(RuntimeError):
    """MaaTouch 通道不可用 (未安装/启动失败/持续报错), 应降级。"""


class MaaTouchController:
    """MaaTouch 触摸注入控制器 (subprocess 管道 + 行队列)。

    握手行 (``^ ...``) 即活性回执: 读到 = 服务存活。服务进程死亡时
    stdout EOF 由读线程转为哨兵, 每次点击前窥探, 静默失效不可能。
    """

    def __init__(
        self,
        adb_path: str,
        serial: str,
        remote_path: str = '/data/local/tmp/maatouch',
        local_path: str | None = None,
        screen_size: tuple[int, int] = (960, 540),
    ) -> None:
        self._adb_path = adb_path
        self._serial = serial
        self._remote = remote_path
        self._local = local_path
        self._screen_size = screen_size
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._pending: list[str] = []
        self._proc = None
        self.max_x = 0
        self.max_y = 0
        self._coord_mode = 'direct'

        if self._local and os.path.exists(self._local):
            self._push_binary()
        self.start()

    # ── 启动 / 重建 ──────────────────────────────────────────────

    def _push_binary(self) -> None:
        subprocess.run(
            [self._adb_path, '-s', self._serial, 'push', self._local, self._remote],
            capture_output=True, timeout=60,
        )
        subprocess.run(
            [self._adb_path, '-s', self._serial, 'shell', 'chmod', '0777', self._remote],
            capture_output=True, timeout=15,
        )
        _log.info('[Emulator] MaaTouch 已推送: {}', self._remote)

    def start(self) -> None:
        """启动注入服务流并完成握手 (超时 30s)。"""
        self._close_stream()
        self._proc = subprocess.Popen(
            [
                self._adb_path, '-s', self._serial, 'shell',
                f'CLASSPATH={self._remote} app_process / com.shxyke.MaaTouch.App',
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        threading.Thread(target=self._pump, daemon=True).start()

        out = self._qget(30)
        if out is None:
            raise MaaTouchUnavailable('MaaTouch 进程退出 (无握手输出)')
        if 'Aborted' in out:
            raise MaaTouchUnavailable('MaaTouch 输出 Aborted (未安装或启动失败)')
        try:
            parts = out.split(' ')
            if parts[0] != '^':
                raise ValueError(out)
            self.max_x = int(parts[2])
            self.max_y = int(parts[3])
        except (ValueError, IndexError) as exc:
            raise MaaTouchUnavailable(f'MaaTouch 握手异常: {out!r}') from exc
        self._qget(5)  # '$ <pid>'

        sw, sh = self._screen_size
        if (self.max_x, self.max_y) == (sw, sh):
            self._coord_mode = 'direct'
        elif (self.max_x, self.max_y) == (sh, sw):
            self._coord_mode = 'transposed'
        else:
            self._coord_mode = 'scaled'
        _log.info(
            '[Emulator] MaaTouch 已连接: max=({}x{}) 坐标模式={}',
            self.max_x, self.max_y, self._coord_mode,
        )

    def _pump(self) -> None:
        """后台线程: 持续读服务输出入队列; 进程死 → None 哨兵。"""
        try:
            for raw in iter(self._proc.stdout.readline, b''):
                self._queue.put(raw.decode('utf-8', errors='replace'))
        except Exception:
            pass
        self._queue.put(None)

    def _qget(self, timeout: float) -> str | None:
        """取一行 (优先预读缓冲)。空串 = 超时; None = 进程退出。"""
        deadline = time.time() + timeout
        while True:
            if self._pending:
                return self._pending.pop(0)
            try:
                line = self._queue.get(timeout=0.2)
            except queue.Empty:
                if time.time() >= deadline:
                    return ''
                continue
            if line is None:
                return None
            line = line.rstrip('\r\n')
            if line:
                return line

    # ── 公共接口 ─────────────────────────────────────────────────

    def _ensure_alive(self) -> None:
        """非阻塞窥探: 服务进程退出则立即重建。"""
        if self._pending:
            return
        try:
            line = self._queue.get_nowait()
        except queue.Empty:
            return
        if line is None:
            _log.warning('[Emulator] MaaTouch 进程已退出, 重建流')
            self.start()
        elif line.strip():
            self._pending.append(line)

    def click(self, px: int, py: int) -> None:
        """注入一次点击 (down + commit + up + commit)。失败重建流重试一次。"""
        with self._lock:
            self._ensure_alive()
            try:
                self._send_tap(px, py)
            except (OSError, BrokenPipeError, ValueError) as exc:
                _log.warning('[Emulator] MaaTouch 写入异常 ({}), 重建流重试', exc)
                self.start()
                self._ensure_alive()
                self._send_tap(px, py)

    def _send_tap(self, px: int, py: int) -> None:
        tx, ty = self._convert(px, py)
        payload = f'd 0 {tx} {ty} 50\nc\nu 0\nc\n'.encode('utf-8')
        self._proc.stdin.write(payload)
        self._proc.stdin.flush()

    def _convert(self, px: int, py: int) -> tuple[int, int]:
        sw, sh = self._screen_size
        if self._coord_mode == 'direct':
            return px, py
        if self._coord_mode == 'transposed':
            return py, px
        return int(px / sw * self.max_x), int(py / sh * self.max_y)

    def _close_stream(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None
        self._pending = []

    def close(self) -> None:
        with self._lock:
            self._close_stream()
