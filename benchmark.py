"""AutoWSGR benchmark — 截图/点击方式性能对比 (移植自 ALAS benchmark)。

用法:
    python benchmark.py [--samples 15] [--skip-install]

截图方式:
    ADB            adb exec-out screencap -p (PNG)
    ADB_nc         adb reverse + 设备端 nc 回传
    DroidCast      DroidCast_raw.apk /preview (PNG over HTTP)
    DroidCast_raw  DroidCast_raw.apk /screenshot (RGB565 over HTTP)
    aScreenCap     Android <= 9 才可用 (MuMu12 为 Android 12, 会自动跳过)
    aScreenCap_nc  同上 (nc 流式)
    uiautomator2   需 pip install uiautomator2
    nemu_ipc       MuMu12 专属, 读渲染共享内存 (需 MuMu >= 3.8.13)
    scrcpy         当前引擎方式 (H.264 流取帧), 作基线参考

点击方式:
    ADB            adb shell input tap (每次起进程, 当前引擎方式)
    PersistentADB  常驻 adb shell 会话逐行发命令 (零进程开销)
    MaaTouch       CLASSPATH app_process 注入, minitouch 协议
    minitouch      二进制已从 ALAS 移除, 自动跳过
    uiautomator2   需 pip install uiautomator2
"""

from __future__ import annotations

import argparse
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools.diagnostics._paths import PROJECT_ROOT, default_config, ensure_project_on_path


ensure_project_on_path()

from autowsgr.infra import ConfigManager


def _resolve_adb_and_serial() -> tuple[str, str]:
    """从 usersettings.yaml 解析 adb 路径与 serial, 环境变量/配置优先。

    原先硬编码 ``C:\\Program Files\\Netease\\MuMu\\nx_main\\adb.exe`` +
    ``127.0.0.1:16416``, 换机/换模拟器实例即失效, 且与 usersettings.yaml
    的 ``emulator.serial`` 配置重复且来源不一致 —— 现统一以配置文件为准。

    优先级: 环境变量 ``AUTOWSGR_ADB`` / ``AUTOWSGR_SERIAL`` > usersettings.yaml
    > 常见的 MuMu 安装路径探测。
    """
    adb = os.environ.get('AUTOWSGR_ADB')
    serial = os.environ.get('AUTOWSGR_SERIAL')

    if not (adb and serial):
        try:
            cfg = ConfigManager.load(default_config())
            serial = serial or cfg.emulator.serial
            emulator_cfg = cfg.emulator
        except Exception as exc:  # noqa: BLE001 - 探测失败不应阻断 benchmark
            print(f'[warn] 读取 {default_config()} 失败 ({exc}), 退回 MuMu 默认路径探测')
            emulator_cfg = None

        if not adb:
            candidates = []
            for folder in (
                r'C:\Program Files\Netease\MuMu',
                r'D:\Program Files\Netease\MuMu',
                r'C:\Program Files\Netease\MuMuPlayer',
            ):
                candidates.append(Path(folder) / 'nx_main' / 'adb.exe')
            candidates.append(Path('adb.exe'))
            adb = next((str(c) for c in candidates if c.exists()), 'adb.exe')

        del emulator_cfg

    return adb, serial or '127.0.0.1:16416'


ADB, SERIAL = _resolve_adb_and_serial()
MUMU_FOLDER = r'C:\Program Files\Netease\MuMu'  # 仅 nemu_ipc 基准用, 见 --mumu-folder
INSTANCE_ID = 1  # serial 端口 16416 = 16384 + 32*1

if Path(MUMU_FOLDER).exists():
    MUMU_FOLDER = str(Path(MUMU_FOLDER))
else:  # 从 adb 路径反推 (…/MuMu/nx_main/adb.exe → …/MuMu)
    _guessed = Path(ADB).resolve().parent.parent
    MUMU_FOLDER = str(_guessed) if _guessed.exists() else MUMU_FOLDER
    if not Path(MUMU_FOLDER).exists():
        print(f'[warn] MuMu 目录不存在: {MUMU_FOLDER}, nemu_ipc 基准将跳过')

SAFE_X, SAFE_Y = 480, 529  # 屏幕底部空白 (540p), 点击零误触

BENCH_N = 15
BENCH_BEST = 12


# ─────────────────────────────────────────────
# adb 基础封装
# ─────────────────────────────────────────────


def adb(*args: str, timeout: float = 30, binary: bool = False) -> bytes:
    """执行 adb 命令, 返回 stdout (bytes)。"""
    proc = subprocess.run(
        [ADB, '-s', SERIAL, *args],
        capture_output=True,
        timeout=timeout,
    )
    if binary:
        return proc.stdout
    return proc.stdout


def adb_shell(*args: str, timeout: float = 30) -> str:
    out = adb('shell', *args, timeout=timeout)
    return out.decode('utf-8', errors='replace').strip()


def adb_push(local: str, remote: str) -> None:
    adb('push', local, remote, timeout=60)


def get_sdk() -> int:
    return int(adb_shell('getprop', 'ro.build.version.sdk') or 0)


def get_abi() -> str:
    return adb_shell('getprop', 'ro.product.cpu.abi') or 'unknown'


def load_screencap_png(data: bytes) -> np.ndarray:
    """screencap PNG → RGB ndarray。"""
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f'screencap PNG decode failed, len={len(data)}')
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


# ─────────────────────────────────────────────
# 截图方式
# ─────────────────────────────────────────────


class ShotADB:
    name = 'ADB'

    def setup(self) -> None:
        pass

    def shot(self) -> np.ndarray:
        data = adb('exec-out', 'screencap', '-p', binary=True)
        return load_screencap_png(data)


class ShotADBnc:
    name = 'ADB_nc'
    PORT = 2048

    def setup(self) -> None:
        # adb reverse: 设备访问 127.0.0.1:PORT → PC 侧同端口
        adb('reverse', f'tcp:{self.PORT}', f'tcp:{self.PORT}')

    def shot(self) -> np.ndarray:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', self.PORT))
        server.listen(1)
        server.settimeout(10)
        try:
            proc = subprocess.Popen(
                [ADB, '-s', SERIAL, 'shell', f'screencap | nc 127.0.0.1 {self.PORT}'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            conn, _ = server.accept()
            conn.settimeout(10)
            chunks = []
            while True:
                data = conn.recv(262144)
                if not data:
                    break
                chunks.append(data)
            conn.close()
            proc.wait(timeout=10)
        finally:
            server.close()
        return load_screencap_png(b''.join(chunks))


class ShotDroidCast:
    """DroidCast_raw: CLASSPATH app_process 启动, HTTP 取 PNG (/preview)。"""

    name = 'DroidCast'
    REMOTE = '/data/local/tmp/DroidCast_raw.apk'
    PORT = 53516

    def setup(self) -> None:
        adb_shell('kill', '-s', '9', '$(pidof ink.mol.droidcast_raw.Main)') if adb_shell(
            'pidof', 'ink.mol.droidcast_raw.Main'
        ) else None
        adb_push(r'bin\DroidCast\DroidCast_raw-release-1.0.apk', self.REMOTE)
        # 后台启动 (nohup 语义: shell 立即返回, 进程常驻)
        adb_shell(
            'nohup', f'CLASSPATH={self.REMOTE}', 'app_process', '/',
            'ink.mol.droidcast_raw.Main', '>/dev/null', '2>&1', '&'
        )
        adb('forward', f'tcp:{self.PORT}', f'tcp:{self.PORT}')
        import requests
        self.session = requests.Session()
        self.session.trust_env = False
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                self.session.get(f'http://127.0.0.1:{self.PORT}/', timeout=2)
            except requests.exceptions.ConnectionError:
                time.sleep(0.5)
                continue
            break  # 404 也算启动完成
        else:
            raise RuntimeError('DroidCast startup timeout')

    def shot(self) -> np.ndarray:
        import requests
        try:
            resp = self.session.get(f'http://127.0.0.1:{self.PORT}/preview', timeout=5)
        except requests.exceptions.ConnectionError:
            self.setup()  # 服务死掉自动重启
            resp = self.session.get(f'http://127.0.0.1:{self.PORT}/preview', timeout=5)
        return load_screencap_png(resp.content)


class ShotDroidCastRaw(ShotDroidCast):
    name = 'DroidCast_raw'

    def shot(self) -> np.ndarray:
        import requests
        try:
            data = self.session.get(f'http://127.0.0.1:{self.PORT}/screenshot', timeout=5).content
        except requests.exceptions.ConnectionError:
            self.setup()
            data = self.session.get(f'http://127.0.0.1:{self.PORT}/screenshot', timeout=5).content
        # RGB565 → RGB888 (540x960)
        arr = np.frombuffer(data, dtype=np.uint16)
        h, w = 960, 540
        try:
            arr = arr.reshape(w, h)  # 竖屏存储
        except ValueError as e:
            raise ValueError(f'DroidCast_raw len={len(data)}') from e
        tmp = np.empty_like(arr)
        cv2.bitwise_and(arr, 0b1111100000000000, dst=tmp)
        r = cv2.convertScaleAbs(tmp, alpha=0.0040283203125)
        cv2.bitwise_and(arr, 0b0000011111100000, dst=tmp)
        g = cv2.convertScaleAbs(tmp, alpha=0.126953125)
        cv2.bitwise_and(arr, 0b0000011111100000, dst=tmp)
        b = cv2.convertScaleAbs(tmp, alpha=8.25)
        img = cv2.merge([r, g, b])
        return cv2.transpose(img)  # 转回横屏


class ShotAScreenCap:
    name = 'aScreenCap'

    def setup(self) -> None:
        raise RuntimeError('aScreenCap 仅支持 Android 5.x-9.x (MuMu12 为 Android 12)')

    def shot(self) -> np.ndarray:
        raise RuntimeError


class ShotAScreenCapNc(ShotAScreenCap):
    name = 'aScreenCap_nc'


class ShotU2:
    name = 'uiautomator2'

    def setup(self) -> None:
        try:
            import uiautomator2 as u2  # noqa: F401
        except ImportError:
            raise RuntimeError('需要 pip install uiautomator2')
        self.u2 = u2.connect(SERIAL)

    def shot(self) -> np.ndarray:
        pil = self.u2.screenshot()  # PIL Image (RGB)
        return np.array(pil)


class ShotNemuIpc:
    """MuMu12 专属: 读渲染共享内存 (无截图编码/传输开销)。"""

    name = 'nemu_ipc'

    def setup(self) -> None:
        import ctypes
        dll = ctypes.CDLL(rf'{MUMU_FOLDER}\nx_main\sdk\external_renderer_ipc.dll')
        self.lib = dll
        self.connect_id = 0
        self.width = 0
        self.height = 0
        # dll 期望宽字符路径 (ctypes 自动 str->wchar); instance 自动探测
        last_err = None
        for inst in (INSTANCE_ID, 1, 0, 2, 3):
            cid = self.lib.nemu_connect(MUMU_FOLDER, inst)
            if cid:
                self.connect_id = cid
                print(f'  nemu_ipc connected: instance={inst} cid={cid}')
                break
            last_err = f'instance {inst} connect_id=0'
        if self.connect_id == 0:
            raise RuntimeError(f'nemu_connect failed: {last_err}; 请确认模拟器正在运行')

    def _get_resolution(self) -> None:
        import ctypes
        w_ptr = ctypes.pointer(ctypes.c_int(0))
        h_ptr = ctypes.pointer(ctypes.c_int(0))
        nullptr = ctypes.POINTER(ctypes.c_int)()
        ret = self.lib.nemu_capture_display(self.connect_id, 0, 0, w_ptr, h_ptr, nullptr)
        if ret > 0:
            raise RuntimeError(f'nemu get_resolution failed ret={ret}')
        self.width, self.height = w_ptr[0], h_ptr[0]
        print(f'  nemu resolution: {self.width}x{self.height}')

    def shot(self) -> np.ndarray:
        import ctypes
        if self.connect_id == 0:
            self.setup()
        if self.width == 0:
            self._get_resolution()
        w_ptr = ctypes.pointer(ctypes.c_int(self.width))
        h_ptr = ctypes.pointer(ctypes.c_int(self.height))
        length = self.width * self.height * 4
        px_ptr = ctypes.pointer((ctypes.c_ubyte * length)())
        ret = self.lib.nemu_capture_display(
            self.connect_id, 0, length, w_ptr, h_ptr, px_ptr)
        if ret > 0:
            raise RuntimeError(f'nemu_capture_display failed ret={ret}')
        img = np.ctypeslib.as_array(px_ptr.contents).reshape((self.height, self.width, 4))
        img = np.flipud(img)  # nemu 输出上下颠倒
        return cv2.cvtColor(img, cv2.COLOR_RGBA2RGB)


class ShotScrcpyBaseline:
    name = 'scrcpy(当前)'

    def setup(self) -> None:
        raise RuntimeError('当前引擎方式, 见引擎日志轮询耗时 (不实测)')

    def shot(self) -> np.ndarray:
        raise RuntimeError


SCREENSHOT_METHODS = {
    'ADB': ShotADB,
    'ADB_nc': ShotADBnc,
    'DroidCast': ShotDroidCast,
    'DroidCast_raw': ShotDroidCastRaw,
    'aScreenCap': ShotAScreenCap,
    'aScreenCap_nc': ShotAScreenCapNc,
    'uiautomator2': ShotU2,
    'nemu_ipc': ShotNemuIpc,
    'scrcpy(当前)': ShotScrcpyBaseline,
}


# ─────────────────────────────────────────────
# 点击方式
# ─────────────────────────────────────────────


class ClickADB:
    name = 'ADB(当前)'

    def setup(self) -> None:
        pass

    def click(self) -> None:
        adb_shell('input', 'tap', str(SAFE_X), str(SAFE_Y), timeout=10)


class ClickPersistent:
    """常驻 adb shell 会话: 零进程开销, 逐行发命令。"""

    name = 'PersistentADB'

    def setup(self) -> None:
        self.proc = subprocess.Popen(
            [ADB, '-s', SERIAL, 'shell'],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def click(self) -> None:
        self.proc.stdin.write(f'input tap {SAFE_X} {SAFE_Y}\n'.encode())
        self.proc.stdin.flush()

    def close(self) -> None:
        self.proc.kill()


class ClickMaaTouch:
    """MaaTouch: CLASSPATH app_process 启动, minitouch 协议 (d/c/u/c)。"""

    name = 'MaaTouch'
    REMOTE = '/data/local/tmp/maatouch'

    def setup(self) -> None:
        adb_push(r'bin\MaaTouch\maatouch', self.REMOTE)
        adb_shell('chmod', '0777', self.REMOTE)
        # 持久 shell 流启动注入服务
        self.proc = subprocess.Popen(
            [ADB, '-s', SERIAL, 'shell',
             f'CLASSPATH={self.REMOTE} app_process / com.shxyke.MaaTouch.App'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        # 等握手行 (max_contacts/max_x/max_y/max_pressure)
        self.proc.stdout.readline()

    def click(self) -> None:
        w = self.proc.stdin
        w.write(f'd 0 {SAFE_X} {SAFE_Y} 50\nc\nu 0\nc\n'.encode())
        w.flush()

    def close(self) -> None:
        self.proc.kill()


class ClickMinitouch:
    name = 'minitouch'

    def setup(self) -> None:
        raise RuntimeError('minitouch 二进制已从 ALAS 移除 (MaaTouch 替代), 无可推送文件')

    def click(self) -> None:
        raise RuntimeError


class ClickU2:
    name = 'uiautomator2'

    def setup(self) -> None:
        try:
            import uiautomator2 as u2
        except ImportError:
            raise RuntimeError('需要 pip install uiautomator2')
        self.u2 = u2.connect(SERIAL)

    def click(self) -> None:
        self.u2.click(SAFE_X, SAFE_Y)


CLICK_METHODS = {
    'ADB(当前)': ClickADB,
    'PersistentADB': ClickPersistent,
    'MaaTouch': ClickMaaTouch,
    'minitouch': ClickMinitouch,
    'uiautomator2': ClickU2,
}


# ─────────────────────────────────────────────
# 计时与报表
# ─────────────────────────────────────────────


def run_bench(func, n: int = BENCH_N) -> float | str:
    """执行 n 次, 返回最好 BENCH_BEST 次的平均耗时; 失败返回 'Failed'。"""
    record: list[float] = []
    for i in range(1, n + 1):
        start = time.perf_counter()
        try:
            func()
        except Exception as exc:
            print(f'  [{i}/{n}] FAILED: {type(exc).__name__}: {exc}')
            return 'Failed'
        cost = time.perf_counter() - start
        print(f'  [{i:02d}/{n}] {cost * 1000:.1f} ms')
        record.append(cost)
    return float(np.mean(sorted(record)[:BENCH_BEST]))


def fmt(cost) -> str:
    if isinstance(cost, str):
        return cost
    return f'{cost * 1000:.1f} ms'


def evaluate_screenshot(cost) -> str:
    if not isinstance(cost, (int, float)):
        return 'Failed'
    if cost < 0.025:
        return 'Insane Fast'
    if cost < 0.100:
        return 'Ultra Fast'
    if cost < 0.200:
        return 'Very Fast'
    if cost < 0.300:
        return 'Fast'
    if cost < 0.500:
        return 'Medium'
    if cost < 0.750:
        return 'Slow'
    if cost < 1.000:
        return 'Very Slow'
    return 'Ultra Slow'


def evaluate_click(cost) -> str:
    if not isinstance(cost, (int, float)):
        return 'Failed'
    if cost < 0.100:
        return 'Fast'
    if cost < 0.200:
        return 'Medium'
    if cost < 0.400:
        return 'Slow'
    return 'Very Slow'


def show_table(title: str, data: list, evaluate) -> None:
    width = max((len(row[0]) for row in data), default=10) + 2
    print(f'\n┌{"─" * width}┬────────────┬───────────────┐')
    print(f'│ {title.center(width - 2)} │ {"Time".center(10)} │ {"Speed".center(13)} │')
    print(f'├{"─" * width}┼────────────┼───────────────┤')
    for name, cost in data:
        print(f'│ {name.ljust(width - 2)} │ {fmt(cost).rjust(10)} │ {evaluate(cost).center(13)} │')
    print(f'└{"─" * width}┴────────────┴───────────────┘')


def bench_group(methods: dict, attr: str, n: int) -> list:
    results = []
    for name, cls in methods.items():
        print(f'\n>>> Testing {name}')
        inst = cls()
        try:
            inst.setup()
        except Exception as exc:
            print(f'  SETUP FAILED: {exc}')
            results.append([name, 'Failed'])
            continue
        try:
            cost = run_bench(getattr(inst, attr), n)
            results.append([name, cost])
        finally:
            close = getattr(inst, 'close', None)
            if close:
                try:
                    close()
                except Exception:
                    pass
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description='AutoWSGR 截图/点击性能基准')
    parser.add_argument('--samples', type=int, default=15)
    parser.add_argument('--skip-install', action='store_true',
                        help='跳过设备端二进制推送 (不覆盖已装服务)')
    parser.add_argument('--no-click', action='store_true')
    args = parser.parse_args()

    BENCH_N = args.samples
    BENCH_BEST = max(1, int(BENCH_N * 0.8))

    print(f'== AutoWSGR benchmark ==')
    print(f'adb: {ADB}')
    print(f'serial: {SERIAL}')
    print(f'sdk: {get_sdk()}, abi: {get_abi()}')

    # ── 截图 ──
    print('\n========== SCREENSHOT BENCHMARK ==========')
    shot_results = bench_group(SCREENSHOT_METHODS, 'shot', BENCH_N)

    # ── 点击 ──
    click_results = []
    if not args.no_click:
        print('\n========== CLICK BENCHMARK ==========')
        click_results = bench_group(CLICK_METHODS, 'click', BENCH_N)

    # ── 汇总 ──
    print('\n========== RESULTS ==========')
    show_table('Screenshot', shot_results, evaluate_screenshot)
    if click_results:
        show_table('Click', click_results, evaluate_click)

    def key(row):
        return row[1] if isinstance(row[1], (int, float)) else 99

    if shot_results:
        best = sorted(shot_results, key=key)[0]
        print(f'\n推荐截图方式: {best[0]} ({fmt(best[1])})')
    if click_results:
        best = sorted(click_results, key=key)[0]
        print(f'推荐点击方式: {best[0]} ({fmt(best[1])})')
    print('\n注: scrcpy(当前) 为引擎现有方式基线参考, 其耗时来自引擎轮询日志 '
          '(截图取帧近零成本, H.264 链路延迟 50-150ms), 未在本脚本实测。')


if __name__ == '__main__':
    main()
