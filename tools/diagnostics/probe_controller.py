"""实机基线探针 — T3-1 步骤2 重构前后对照用。

在真实 MuMu 上量一组**不可从代码推导**的运行时事实，作为重构的行为契约：

1. 截图通道：nemu_ipc 是否启用、分辨率、单帧耗时（多次采样）
2. 触控链：实际挂上了哪几级（mapatouch / 常驻 shell）
3. 端到端点击延迟（click 含 operation_delay）
4. 断连后能否干净重连

用法::

    # 重构前
    python tools/diagnostics/probe_controller.py --out logs/diagnostics/baseline_before.json
    # 重构后
    python tools/diagnostics/probe_controller.py --out logs/diagnostics/baseline_after.json
    # 对比
    python tools/diagnostics/probe_controller.py --diff a.json b.json

⚠️ 本脚本会**真的点击屏幕**（点击的是屏幕中心 0.5/0.5）。
运行前请确认游戏当前处于安全页面（挂机中即可，不要停在弹窗上）。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _paths import DIAG_DIR  # noqa: E402

from autowsgr.emulator.controller.scrcpy import ScrcpyController  # noqa: E402


def _probe_once(serial: str, *, do_click: bool) -> dict:
    ctrl = ScrcpyController(serial=serial)
    result: dict = {'serial': serial}

    t0 = time.monotonic()
    info = ctrl.connect()
    result['connect_seconds'] = round(time.monotonic() - t0, 3)
    result['resolution'] = list(info.resolution)

    # 通道装配情况
    result['nemu_ipc_active'] = ctrl._nemu is not None
    result['touch_chain'] = {
        'maatouch': ctrl._touch.maatouch is not None,
        'persistent_shell': ctrl._touch.persistent_shell is not None,
    }

    # 截图耗时（多次采样，看抖动而非单值）
    samples = []
    frames = []
    for _ in range(12):
        t = time.monotonic()
        frame = ctrl.screenshot()
        samples.append(time.monotonic() - t)
        frames.append(frame)
    result['screenshot_ms'] = {
        'min': round(min(samples) * 1000, 2),
        'median': round(statistics.median(samples) * 1000, 2),
        'max': round(max(samples) * 1000, 2),
    }
    result['frame_shape'] = list(frames[-1].shape)
    result['frame_dtype'] = str(frames[-1].dtype)
    # 帧是否在动（连续两帧不同 = 画面在刷新，非卡死）
    result['frames_differ'] = bool((frames[-1] != frames[-2]).any())

    if do_click:
        times = []
        for _ in range(6):
            t = time.monotonic()
            ctrl.click(0.5, 0.5, delay=False)
            times.append(time.monotonic() - t)
        result['click_ms'] = {
            'min': round(min(times) * 1000, 2),
            'median': round(statistics.median(times) * 1000, 2),
        }

    ctrl.disconnect()
    result['after_disconnect'] = {
        'resolution': list(ctrl.resolution),
        'touch_closed': ctrl._touch.maatouch is None and ctrl._touch.persistent_shell is None,
    }

    # 重连
    t0 = time.monotonic()
    info2 = ctrl.connect()
    result['reconnect_seconds'] = round(time.monotonic() - t0, 3)
    result['reconnect_resolution'] = list(info2.resolution)
    ctrl.screenshot()
    result['reconnect_screenshot_ok'] = True
    ctrl.disconnect()
    return result


def _diff(a: dict, b: dict) -> int:
    """对比两份基线；有差异返回 1。"""
    import difflib

    pa = json.dumps(a, indent=2, sort_keys=True, ensure_ascii=False).splitlines()
    pb = json.dumps(b, indent=2, sort_keys=True, ensure_ascii=False).splitlines()
    # 耗时类字段天然抖动，忽略
    noisy = ('seconds', '_ms')
    def scrub(lines):
        return [ln for ln in lines if not any(n in ln for n in noisy)]

    diff = list(difflib.unified_diff(scrub(pa), scrub(pb), 'before', 'after', lineterm=''))
    if diff:
        print('\n'.join(diff))
        print('\n[FAIL] 存在非耗时差异（见上）')
        return 1
    print('[OK] 非耗时字段完全一致（耗时字段抖动已忽略）')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--serial', default='127.0.0.1:16416')
    ap.add_argument('--out', default=None, help='把基线写入该JSON 文件')
    ap.add_argument('--diff', nargs=2, metavar=('A', 'B'))
    ap.add_argument('--no-click', action='store_true', help='跳过点击（不碰屏幕）')
    args = ap.parse_args()

    if args.diff:
        with open(args.diff[0], encoding='utf-8') as f:
            a = json.load(f)
        with open(args.diff[1], encoding='utf-8') as f:
            b = json.load(f)
        return _diff(a, b)

    if args.no_click:
        print('[!] 跳过点击，不会碰屏幕')
    data = _probe_once(args.serial, do_click=not args.no_click)
    text = json.dumps(data, indent=2, ensure_ascii=False)
    print(text)
    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = DIAG_DIR / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding='utf-8')
        print(f'\n已写入 {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())