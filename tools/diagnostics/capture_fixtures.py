"""实机页面采集器 (T3-3② fixture 数据源)。

目的
----
给识别层建**真实截图 fixture 集**, 让「改阈值 / 换模板 / 调状态机」不必
每次都实机跑一轮验证。当前 `fight()` 主循环零测试, 识别改动只能靠实机
回归, 周期以天计。

它做什么
--------
挂上引擎跑真实战斗, 在**每个状态被识别到的瞬间**自动截图, 并同时记录
识别结果与置信度。于是得到:

    logs/fixtures/screenshots/
    ├── normal_day_<phase>_<n>.png         常规战·昼战
    ├── event_night_<phase>_<n>.png        活动战·夜战
    └── manifest.jsonl每行: 文件名 / 模式 / phase / confidence / 匹配位置

有了 manifest, 就能写断言「这张图必须识别为 X」—— 即真正的回归测试。

为什么区分「模式」和「昼夜」
--------------------------
你指出的差异是真实的, 且在代码里确实走不同分支:

- **常规战 vs 活动战**: 终态不同 (``MAP_PAGE`` vs ``EVENT_MAP_PAGE``),
  活动战回港会落关卡浮层态 (2026-10-08 实机事故)
- **夜战点 vs 昼战点**: ``NIGHT_PROMPT`` (夜战选择) 只在能追击时出现;
  H5-ACD 三点**进入即夜战无追击选项** (代码注释明确写了), 因此
  ``NIGHT_PROMPT`` 与 ``FIGHT_PERIOD`` 在两种点上表现不同

同一张模板在不同模式下可能匹配度不同, 所以 fixture 必须带模式标签。

用法
----
::

    # 采集常规战(昼战)
    python tools/diagnostics/capture_fixtures.py --mode normal

    # 采集活动战夜战
    python tools/diagnostics/capture_fixtures.py --mode event

    # 采集战役(SINGLE 模式, 终态不同)
    python tools/diagnostics/capture_fixtures.py --mode campaign

    # 只采集不实际操作(游戏需自己手动打)
    python tools/diagnostics/capture_fixtures.py --mode manual

⚠️ 会**真实出击并消耗资源**, 请确认舰队/油弹充足。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

OUTPUT_ROOT = PROJECT_ROOT / 'logs' / 'fixtures' / 'screenshots'


@dataclass
class CaptureRecord:
    """一次截图的记录 (写入 manifest.jsonl)。"""

    filename: str
    mode: str
    phase: str
    timestamp: str
    resolution: tuple[int, int]
    confidence: float | None = None
    matched_at: tuple[int, int] | None = None
    note: str = ''

    def to_dict(self) -> dict:
        return {
            'filename': self.filename,
            'mode': self.mode,
            'phase': self.phase,
            'timestamp': self.timestamp,
            'resolution': list(self.resolution),
            'confidence': self.confidence,
            'matched_at': list(self.matched_at) if self.matched_at else None,
            'note': self.note,
        }


class FixtureCollector:
    """在真实战斗过程中收集各状态截图。

    做法: 包装 :class:`CombatRecognizer` 的 ``wait_for_phase``,
    在它返回新phase **之前**截一张图 —— 这样抓到的是「该状态刚出现」
    的画面, 也就是识别刚成功的那一刻。

    为什么截在匹配成功时而不是失败时
    --------------------------------
    fixture 的用途是**回归验证**: 「这张图必须识别为 X」。
    若截的是随机时刻的图, 可能本身就处于模糊/动画中间态, 断言会不稳定。
    匹配成功瞬间的截图是确定的、正样本。
    """

    def __init__(self, mode: str, *, min_interval: float = 0.5) -> None:
        self.mode = mode
        self.min_interval = min_interval
        self._last_shot: dict[str, float] = {}
        self.records: list[CaptureRecord] = []
        self._counter: dict[str, int] = {}

        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.out_dir = OUTPUT_ROOT / f'{mode}_{stamp}'
        self.out_dir.mkdir(parents=True, exist_ok=True)

    # ── 采集 ────────────────────────────────────────────────────────────

    def on_phase_identified(
        self,
        phase_name: str,
        screen,
        *,
        confidence: float | None = None,
        matched_at: tuple[int, int] | None = None,
        note: str = '',
    ) -> Path | None:
        """某状态被识别到时截图。同一状态最短间隔 ``min_interval``。"""
        now = time.monotonic()
        if now - self._last_shot.get(phase_name, 0.0) < self.min_interval:
            return None
        self._last_shot[phase_name] = now

        seq = self._counter.get(phase_name, 0) + 1
        self._counter[phase_name] = seq

        safe_phase = phase_name.lower()
        filename = f'{self.mode}_{safe_phase}_{seq:02d}.png'
        path = self.out_dir / filename

        try:
            import cv2

            arr = screen[:, :, ::-1] if _looks_rgb(screen) else screen
            cv2.imwrite(str(path), arr)
        except Exception as exc:  # noqa: BLE001
            print(f'  [warn] 截图失败 {filename}: {exc}')
            return None

        h, w = screen.shape[:2]
        record = CaptureRecord(
            filename=filename,
            mode=self.mode,
            phase=phase_name,
            timestamp=datetime.now(timezone.utc).isoformat(timespec='seconds'),
            resolution=(int(w), int(h)),
            confidence=confidence,
            matched_at=matched_at,
            note=note,
        )
        self.records.append(record)
        print(f'  [采集] {filename}  ({phase_name}, conf={confidence})')
        return path

    # ── 输出 ────────────────────────────────────────────────────────────

    def finalize(self) -> Path:
        """写manifest 并返回目录。"""
        manifest = self.out_dir / 'manifest.jsonl'
        with manifest.open('w', encoding='utf-8') as fh:
            for record in self.records:
                fh.write(json.dumps(record.to_dict(), ensure_ascii=False) + '\n')

        readme = self.out_dir / 'README.md'
        readme.write_text(
            f'# 采集自{self.mode}\n\n'
            f'- 时间: {datetime.now().isoformat(timespec="seconds")}\n'
            f'- 张数: {len(self.records)}\n'
            f'- manifest: `manifest.jsonl` (每行一条记录)\n\n'
            '## 说明\n\n'
            '这些是识别成功瞬间的截图 (正样本), 用于回归验证:\n'
            '「这张图必须被识别为 <phase>」。\n\n'
            '采集后建议:\n\n'
            '1. 人工浏览确认每张图确实处于标注的 phase (采集器只记录引擎\n'
            '   的判断, 若引擎本身误判, 这里会跟着错)\n'
            '2. 移入 `testing/fixtures/screenshots/<mode>/` 成为长期 fixture\n'
            '3. 补一条 manifest 断言, 纳入 pytest\n',
            encoding='utf-8',
        )

        print(f'\n共采集 {len(self.records)} 张 -> {self.out_dir}')
        print(f'manifest: {manifest.name}')
        return self.out_dir


def _looks_rgb(screen) -> bool:
    """截图协议是 RGB (见项目记忆: ammo.py 曾因按 BGR 处理而全错)。"""
    return screen is not None and getattr(screen, 'ndim', 0) == 3


# ═══════════════════════════════════════════════════════════════════════════════
# 实机采集: 挂引擎跑真实战斗
# ═══════════════════════════════════════════════════════════════════════════════


def collect_by_engine(mode: str, *, rounds: int) -> Path | None:
    """挂到战斗引擎上自动采集 (推荐)。

    走真实战斗, 在每个状态被识别的瞬间截图。消耗资源, 但得到的是
    「引擎自己认为的状态」与对应画面, 正是回归测试需要的。
    """
    from autowsgr.combat.recognizer import CombatRecognizer
    from autowsgr.scheduler import launch
    from autowsgr.scheduler.plan_runner import run_yaml_plan

    collector = FixtureCollector(mode)

    # 包装识别器的 wait_for_phase: 返回前先截一张
    original = CombatRecognizer.wait_for_phase

    def _patched(self, candidates, *args, **kwargs):
        screen = self._ctx.ctrl.screenshot()
        result = original(self, candidates, *args, **kwargs)
        if result is not None:
            collector.on_phase_identified(
                result.name,
                screen,
                note=f'candidates={[c.name for c in candidates]}',
            )
        return result

    CombatRecognizer.wait_for_phase = _patched

    print(f'启动采集: mode={mode}, rounds={rounds}')
    print('⚠将真实出击, 请确认舰队/油弹充足。\n')

    plan = _pick_plan(mode)
    if plan is None:
        print(f'[!]找不到 {mode} 对应的作战计划')
        CombatRecognizer.wait_for_phase = original
        return None

    try:
        ctx = launch('usersettings.yaml')
        run_yaml_plan(ctx, str(plan), times=rounds, fleet_id=1)
    finally:
        CombatRecognizer.wait_for_phase = original

    return collector.finalize()


def _pick_plan(mode: str) -> Path | None:
    """按模式挑一份现成的作战计划。

    注意 ``normal`` 指**常规战** (非活动), 走 ``data/plan/normal_fight/``;
    ``event`` 指活动战, 走 ``data/plan/event/``。两者终态不同 ——
    分别是 ``MAP_PAGE`` 与 ``EVENT_MAP_PAGE``, 见 ``plan.py`` 的
    ``MODE_END_PHASES``。
    """
    plan_dir = PROJECT_ROOT / 'autowsgr' / 'data' / 'plan'
    candidates: list[Path] = {
        # 常规战: 5-2 炸鱼 (非夜战, 覆盖昼战路径)
        'normal': [plan_dir / 'normal_fight' / '5-2炸鱼.yaml'],
        # 活动战: H5 三连夜战 (进入即夜战无追击选项)
        'event': [plan_dir / 'event' / 'H5ACD夜战.yaml'],
        # 战役 (SINGLE 模式, 终态与 MAP 不同)
        'campaign': [plan_dir / 'normal_fight' / '5-2炸鱼.yaml'],
    }.get(mode, [])

    for path in candidates:
        if path.exists():
            return path

    # 兜底: 目录里随便挑一份, 并提示用户确认场景是否符合
    fallback_dir = (
        plan_dir / 'event' if mode == 'event' else plan_dir / 'normal_fight'
    )
    if fallback_dir.exists():
        first = sorted(fallback_dir.glob('*.yaml'))
        if first:
            print(f'[warn] 指定计划不存在, 回退到 {first[0].name} (请确认场景符合)')
            return first[0]
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# 手动采集: 你自己打, 脚本只监听
# ═══════════════════════════════════════════════════════════════════════════════


def collect_manually(mode: str, *, duration: float) -> Path:
    """不驱动战斗, 只按间隔连拍。用于「临时缺某个页面的图」。

    你在模拟器里手动操作, 脚本持续截图。**注意**: 这种是随机时刻的图,
    可能处于动画中间态, 不适合直接当断言 fixture, 只适合当素材。
    """
    from autowsgr.scheduler import launch
    from autowsgr.ops.navigate import identify_current_page

    collector = FixtureCollector(mode, min_interval=0.0)
    ctx = launch('usersettings.yaml', ensure_game=False)

    print(f'开始连拍{duration:.0f}s, 请在模拟器里手动操作。')
    print('按 Ctrl+C 结束。\n')

    deadline = time.monotonic() + duration
    try:
        while time.monotonic() < deadline:
            screen = ctx.ctrl.screenshot()
            page = identify_current_page(ctx)
            collector.on_phase_identified(
                page.name if page else 'UNKNOWN',
                screen,
                note='手动连拍 (随机时刻, 不保证是断言级 fixture)',
            )
            time.sleep(0.4)
    except KeyboardInterrupt:
        print('\n已中断。')

    return collector.finalize()


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> int:
    ap = argparse.ArgumentParser(
        description='采集实机页面截图 (T3-3② fixture 数据源)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        '--mode',
        required=True,
        choices=['normal', 'event', 'campaign', 'manual'],
        help='采集场景: normal=常规战 event=活动战 campaign=战役 manual=手动连拍',
    )
    ap.add_argument('--rounds', type=int, default=1, help='采集轮数 (仅引擎模式)')
    ap.add_argument('--duration', type=float, default=300.0, help='连拍时长秒 (仅 manual)')
    args = ap.parse_args()

    print(f'输出目录: {OUTPUT_ROOT}\n')

    if args.mode == 'manual':
        out = collect_manually('manual', duration=args.duration)
    else:
        out = collect_by_engine(args.mode, rounds=args.rounds)

    if out is None:
        return 1

    print('\n下一步:')
    print('  1. 人工浏览截图, 确认每张确实处于 manifest 标注的 phase')
    print('  2. 把满意的移入 testing/fixtures/screenshots/<mode>/')
    print('  3. 补断言纳入 pytest (可参考 docs/architecture/tier2-3-improvement-plan.md 的 T3-3②)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())