# UI 页面与导航系统

> 返回 [架构概述](README.md)

本文档描述 AutoWSGR 的 UI 页面识别、导航图寻路和页面控制器设计。

---

## 设计概览

```
┌──────────────────────────┐
│  page.py                 │  页面注册中心: register_page / get_current_page
├──────────────────────────┤
│  navigation.py           │  导航图: NavEdge 有向图 + BFS find_path
├──────────────────────────┤
│  utils.py                │  等待工具: wait_for_page / click_and_wait_for_page
├──────────────────────────┤
│  pages/ (各页面控制器)    │  MainPage / MapPage / BathPage / ...
└──────────────────────────┘
```

---

## 页面注册中心

**文件**: `autowsgr/ui/page.py`

全局注册表 `_PAGE_REGISTRY: dict[str, Callable[[ndarray], bool]]`，每个页面注册一个 `checker` 函数（通常基于 `PixelSignature`）。

### API

```python
register_page(name: PageName, checker: Callable[[ndarray], bool])
    # 注册页面识别函数

get_current_page(screen: ndarray) -> str | None
    # 遍历注册表，返回首个匹配页面名；无匹配返回 None

get_registered_pages() -> list[str]
    # 列出所有已注册页面
```

### 识别流程

```
screenshot()
    │
    ▼
get_current_page(screen)
    │
    ├─ 遍历 _PAGE_REGISTRY
    │   ├─ MainPage.checker(screen) → True? → 返回 "MAIN"
    │   ├─ MapPage.checker(screen) → True? → 返回 "MAP"
    │   ├─ ...
    │   └─ 全部 False → 返回 None
    │
    └─ checker 抛异常 → 记录 warning，继续遍历
```

各页面在模块加载时自动调用 `register_page()` 完成注册。

---

## 页面控制器设计模式

每个页面类遵循 **Page-per-class** 模式：

```python
class SomePage:
    # ── 静态方法 (无需设备) ──
    @staticmethod
    def is_current_page(screen: ndarray) -> bool
        # 使用 PixelSignature / ImageChecker 检测

    @staticmethod
    def get_some_state(screen: ndarray) -> data
        # 从截图中读取状态 (如血量、按钮状态)

    # ── 实例方法 (需要设备) ──
    def __init__(self, ctx: GameContext)
        # 持有 ctx 引用

    def navigate_to(self, target) -> None
        # 点击并等待目标页面出现

    def go_back(self) -> None
        # 返回上一页面

    def click_button(self) -> None
        # 执行页面操作
```

**设计要点**: 静态方法仅依赖截图 numpy 数组，可独立测试；实例方法通过 `ctx.ctrl` 执行设备操作。

---

## 核心页面一览

| 页面                      | PageName            | 职责                        |
|---------------------------|---------------------|-----------------------------|
| `MainPage`                | `MAIN`              | 中央枢纽，导航到各功能模块    |
| `MapPage`                 | `MAP`               | 出击/演习/远征/战役/决战 面板 |
| `BattlePreparationPage`   | `BATTLE_PREP`       | 舰队选择、血量检测、出征      |
| `BathPage`                | `BATH`              | 浴室修理                     |
| `CanteenPage`             | `CANTEEN`           | 食堂烹饪                     |
| `BuildPage`               | `BUILD`             | 建造/解装/开发/废弃           |
| `DecisiveBattlePage`      | `DECISIVE_BATTLE`   | 决战概览 + 地图控制器         |
| `BackyardPage`            | `BACKYARD`          | 后院 (入口: 浴室/食堂)       |
| `SidebarPage`             | `SIDEBAR`           | 侧边栏 (入口: 建造/强化/好友) |
| `MissionPage`             | `MISSION`           | 任务奖励领取                  |
| `StartScreenPage`         | `START_SCREEN`      | 游戏启动画面                  |
| `IntensifyPage`           | `INTENSIFY`         | 强化界面                     |
| `FriendPage`              | `FRIEND`            | 好友界面                     |
| `ChooseShipPage`          | `CHOOSE_SHIP`       | 选船界面                     |
| `BaseEventPage`           | `EVENT_MAP`         | 活动地图页面                  |

---

## 导航图

**文件**: `autowsgr/ui/navigation.py`

页面间的导航关系用 **有向图** 表示，每条边存储一个动作函数。

### NavEdge

```python
@dataclass
class NavEdge:
    source: PageName              # 出发页面
    target: PageName              # 到达页面
    action: Callable[[GameContext], None]  # 导航动作
    description: str              # 人类可读描述
```

### 导航图拓扑

```
                    ┌─── MISSION
                    │
          SIDEBAR ──┼─── BUILD
          │   │     │
          │   │     ├─── INTENSIFY
          │   │     │
          │   │     └─── FRIEND
          │   │
  MAIN ───┤   ├─── EVENT_MAP
          │
          ├─── MAP ──── DECISIVE_BATTLE
          │     │
          │     └─── BATTLE_PREP
          │
          └─── BACKYARD ──┬─── BATH
                          │
                          └─── CANTEEN
```

完整边列表定义在 `NAV_GRAPH: list[NavEdge]` 中，共 20+ 条边。

### 动作函数

每条边的 `action` 是一个延迟导入的函数，内部调用对应页面控制器：

```python
def _main_to_map(ctx: GameContext) -> None:
    from autowsgr.ui.main_page import MainPage
    MainPage(ctx).navigate_to(MainPage.Target.SORTIE)

def _map_to_main(ctx: GameContext) -> None:
    from autowsgr.ui.map.page import MapPage
    MapPage(ctx).go_back()
```

延迟导入避免了页面模块间的循环依赖。

### BFS 寻路

```python
find_path(source: PageName, target: PageName) -> list[NavEdge] | None
```

对 `NAV_GRAPH` 构建的邻接表执行 BFS，返回最短路径。调用方逐条执行 `edge.action(ctx)` 即可完成导航。

---

## 等待工具

**文件**: `autowsgr/ui/utils.py`

| 函数                                                      | 说明                           |
|-----------------------------------------------------------|--------------------------------|
| `wait_for_page(ctx, page, timeout) -> bool`               | 轮询直到当前页面匹配            |
| `wait_leave_page(ctx, page, timeout) -> bool`             | 轮询直到离开当前页面            |
| `click_and_wait_for_page(ctx, xy, target, timeout)`       | 点击坐标后等待目标页面出现       |
| `click_and_wait_leave_page(ctx, xy, page, timeout)`       | 点击坐标后等待离开当前页面       |
| `confirm_operation(ctx, description)`                     | 确认操作 (通用弹窗)            |

### NavigationError

超时未到达目标页面时抛出：

```python
class NavigationError(UIError):
    # 继承自 UIError → AutoWSGRError
```

### NavConfig

```python
@dataclass
class NavConfig:
    timeout: float = 10.0        # 等待超时 (秒)
    interval: float = 0.5        # 轮询间隔 (秒)
    retry: int = 3               # 重试次数
```

---

## 与其他模块的关系

- **下游**: [vision](vision.md) 的 `PixelChecker` / `ImageChecker` 提供页面识别能力
- **上游**: [ops](ops.md) 的 `goto_page()` / `identify_current_page()` 封装了导航图调用
- **上游**: [combat-engine](combat-engine.md) 使用 `BattlePreparationPage` 进行出征前准备
- **类型**: [infra](infra.md) 的 `PageName` 枚举定义了所有页面名
