"""MuMu12 nemu_ipc 截图通道 + 常驻 adb shell 会话。

两个引擎加速通道 (benchmark 实测 2026-10-09):

- :class:`NemuIpcCapture`: 直读 MuMu 渲染共享内存 (external_renderer_ipc.dll),
  截图 ~3ms, 比当前 scrcpy H.264 链路 (50-150ms) 快一个数量级, 且画面
  无损 (无压缩噪声)。失败自动重连一次, 仍失败则由上层永久降级回 scrcpy 流。

- :class:`PersistentAdbShell`: 常驻 ``adb shell`` 会话, 点击命令逐行写入,
  免去每次起 adb 进程的开销 (实测提交 ~0ms vs 单次 70ms)。断连自动重开
  一次, 仍失败由上层回退旧路径。
"""

from __future__ import annotations

import subprocess
import threading
import time

import cv2
import numpy as np

from autowsgr.infra.logger import get_logger

_log = get_logger('emulator')


class NemuIpcUnavailable(RuntimeError):
    """nemu_ipc 通道不可用 (dll 缺失/连接失败/持续报错), 应永久降级。"""


class NemuIpcCapture:
    """MuMu12 nemu_ipc 截图通道。

    Parameters
    ----------
    mumu_folder:
        MuMu12 安装根目录 (含 ``nx_main/sdk/external_renderer_ipc.dll``)。
    serial:
        设备 serial (用于推算实例 id, 如 ``127.0.0.1:16416`` → 实例 1)。
    expected_size:
        期望的输出尺寸 ``(w, h)`` (引擎分辨率)。nemu 返回尺寸与之不符时
        视为不可用 (避免图像几何错乱)。
    """

    def __init__(self, mumu_folder: str, serial: str, expected_size: tuple[int, int]) -> None:
        import ctypes

        self._lock = threading.Lock()
        self._mumu_folder = mumu_folder
        self._instance_id = 0
        self.connect_id = 0
        self.width = 0
        self.height = 0
        self._expected = expected_size

        dll_path = rf'{mumu_folder}\nx_main\sdk\external_renderer_ipc.dll'
        try:
            self.lib = ctypes.CDLL(dll_path)
        except OSError as exc:
            raise NemuIpcUnavailable(f'dll 加载失败: {exc}') from exc

        # 探测实例 id: serial 端口推算 (16384 + 32*n) 优先, 再常见值
        candidates: list[int] = []
        if ':' in serial:
            try:
                port = int(serial.rsplit(':', 1)[1])
                if port >= 16384:
                    candidates.append((port - 16384) // 32)
            except ValueError:
                pass
        candidates += [1, 0, 2, 3]
        seen: set[int] = set()
        instance_id = None
        for inst in candidates:
            if inst in seen:
                continue
            seen.add(inst)
            # nemu_connect 期望宽字符路径 (ctypes 自动 str→wchar)
            cid = self.lib.nemu_connect(mumu_folder, inst)
            if cid:
                self._instance_id = inst
                self.connect_id = cid
                instance_id = inst
                break
        if instance_id is None:
            raise NemuIpcUnavailable('nemu_connect 全部实例失败 (模拟器未运行?)')

        self._get_resolution()
        if (self.width, self.height) != tuple(expected_size):
            raise NemuIpcUnavailable(
                f'nemu 分辨率 {(self.width, self.height)} 与引擎期望 {expected_size} 不符'
            )
        _log.info(
            '[Emulator] nemu_ipc 已启用: instance={} 分辨率={}x{}',
            instance_id,
            self.width,
            self.height,
        )

    def _get_resolution(self) -> None:
        """查询渲染分辨率 (length=0 + 空数据指针 = 纯查询模式)。"""
        import ctypes

        w_ptr = ctypes.pointer(ctypes.c_int(0))
        h_ptr = ctypes.pointer(ctypes.c_int(0))
        nullptr = ctypes.POINTER(ctypes.c_int)()
        ret = self.lib.nemu_capture_display(self.connect_id, 0, 0, w_ptr, h_ptr, nullptr)
        if ret > 0:
            raise NemuIpcUnavailable(f'nemu get_resolution failed ret={ret}')
        self.width, self.height = w_ptr[0], h_ptr[0]

    def capture(self) -> np.ndarray:
        """抓取一帧, 返回 RGB ndarray (engine 期望的朝向与色彩)。"""
        import ctypes

        with self._lock:
            try:
                return self._capture_locked()
            except NemuIpcUnavailable:
                raise
            except Exception as exc:
                # 瞬态错误 (rpc error 17xx 等): 重连一次再试
                _log.warning('[Emulator] nemu_ipc 异常 ({}), 尝试重连', exc)
                try:
                    self.lib.nemu_disconnect(self.connect_id)
                except Exception:
                    pass
                self.connect_id = 0
                cid = self.lib.nemu_connect(self._mumu_folder, self._instance_id)
                if not cid:
                    raise NemuIpcUnavailable('nemu 重连失败') from exc
                self.connect_id = cid
                self.width = 0
                return self._capture_locked()

    def _capture_locked(self) -> np.ndarray:
        import ctypes

        if self.connect_id == 0:
            raise NemuIpcUnavailable('not connected')
        if self.width == 0:
            self._get_resolution()
        w_ptr = ctypes.pointer(ctypes.c_int(self.width))
        h_ptr = ctypes.pointer(ctypes.c_int(self.height))
        length = self.width * self.height * 4
        px_ptr = ctypes.pointer((ctypes.c_ubyte * length)())
        ret = self.lib.nemu_capture_display(
            self.connect_id, 0, length, w_ptr, h_ptr, px_ptr)
        if ret > 0:
            raise NemuIpcUnavailable(f'nemu_capture_display ret={ret}')
        # 尺寸可能被 dll 更新 (分辨率切换), 重查一次以对齐
        if w_ptr[0] != self.width or h_ptr[0] != self.height:
            self.width, self.height = w_ptr[0], h_ptr[0]
            if (self.width, self.height) != tuple(self._expected):
                raise NemuIpcUnavailable(
                    f'nemu 分辨率变为 {(self.width, self.height)}')
        img = np.ctypeslib.as_array(px_ptr.contents).reshape((self.height, self.width, 4))
        img = np.flipud(img)  # nemu 输出上下颠倒
        return cv2.cvtColor(img, cv2.COLOR_RGBA2RGB)

    def close(self) -> None:
        with self._lock:
            if getattr(self, 'connect_id', 0):
                try:
                    self.lib.nemu_disconnect(self.connect_id)
                except Exception:
                    pass
                self.connect_id = 0


def detect_mumu_folder() -> str | None:
    """探测 MuMu12 安装根目录 (含 nx_main/sdk/external_renderer_ipc.dll)。"""
    import os

    candidates = [
        r'C:\Program Files\Netease\MuMu',
        r'C:\Program Files (x86)\Netease\MuMu',
        r'D:\Program Files\Netease\MuMu',
        r'D:\Program Files (x86)\Netease\MuMu',
    ]
    try:
        import adbutils
        # 从 adb 二进制位置反推 (adb.exe 通常在 <root>/nx_main/)
        adb_path = adbutils.adb_client.adb_path
        if adb_path:
            root = os.path.dirname(os.path.dirname(os.path.abspath(adb_path)))
            candidates.insert(0, root)
    except Exception:
        pass
    for folder in candidates:
        if os.path.exists(rf'{folder}\nx_main\sdk\external_renderer_ipc.dll'):
            return folder
    return None


class PersistentAdbShell:
    """常驻 ``adb shell`` 会话: 命令逐行写入, 免每次起进程。

    断连 (BrokenPipe) 自动重开会话重试一次; 再失败由上层回退旧路径。
    """

    def __init__(self, adb_path: str, serial: str) -> None:
        self._adb_path = adb_path
        self._serial = serial
        self._lock = threading.Lock()
        self._proc = self._spawn()

    def _spawn(self) -> subprocess.Popen:
        return subprocess.Popen(
            [self._adb_path, '-s', self._serial, 'shell'],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def run(self, *args: str) -> None:
        """在常驻会话内执行一条命令 (异步, 不等 Android 端回执)。"""
        with self._lock:
            line = ' '.join(args) + '\n'
            try:
                self._proc.stdin.write(line.encode())
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError):
                _log.warning('[Emulator] 持久 shell 断连, 重开会话重试')
                self._proc.kill()
                self._proc = self._spawn()
                self._proc.stdin.write(line.encode())
                self._proc.stdin.flush()

    def close(self) -> None:
        with self._lock:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()
