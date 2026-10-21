# 调度器与 HTTP 服务

> 返回 [架构概述](README.md)

本文档描述 AutoWSGR 的两种运行模式：脚本直接调度 (Scheduler) 和 HTTP 服务 (Server)。

---

## 启动器 (Launcher)

**文件**: `autowsgr/scheduler/launcher.py`

Launcher 是系统的 "零到就绪" 引导器，将配置加载、设备连接、OCR 初始化和游戏启动串成一条流水线。

### 启动链

```
load_config()       → UserConfig (YAML → Pydantic)
    ↓
connect()           → ScrcpyController (ADB + scrcpy 连接)
    ↓
create_ocr()        → EasyOCREngine (模型加载)
    ↓
build_context()     → GameContext(ctrl, config, ocr)
    ↓
ensure_ready(ctx)   → 确保游戏运行并在主页面
```

### 配置查找顺序

1. 显式传入的 `config_path` → 直接加载
2. `config_path=None` → `ConfigManager` 自动检测当前目录下 `usersettings.yaml`
3. 以上均不存在 → 内置默认值

### 便捷函数

```python
from autowsgr.scheduler.launcher import launch

# 一站式启动: 加载配置 → 连接模拟器 → 启动游戏
ctx = launch('usersettings.yaml')

# 不启动游戏 (仅连接)
ctx = launch('usersettings.yaml', ensure_game=False)
```

### Launcher 分步使用

```python
from autowsgr.scheduler.launcher import Launcher

launcher = Launcher(config_path='usersettings.yaml')
launcher.load_config()
launcher.connect()
ctx = launcher.build_context()
launcher.ensure_ready(ctx)
```

分步模式适合单元测试和自定义流程。

---

## 任务调度器 (TaskScheduler)

**文件**: `autowsgr/scheduler/scheduler.py`

面向脚本使用场景的简单顺序调度器。

### 基本用法

```python
from autowsgr.scheduler import launch, TaskScheduler, FightTask

ctx = launch('usersettings.yaml')

scheduler = TaskScheduler(ctx, expedition_interval=15 * 60)
scheduler.add(FightTask(runner=my_event_runner, times=30))
scheduler.add(FightTask(runner=my_normal_runner, times=5))
results = scheduler.run()  # → list[FightTask] (含结果)
```

### 调度逻辑

```
for task in tasks (FIFO):
    for round in range(task.times):
        maybe_collect_expedition()  # 检查间隔
        result = runner.run()
        if result.flag == DOCK_FULL:
            break  # 跳过该任务剩余轮次
print_summary()
```

1. 按提交顺序 (FIFO) 依次执行每个 `FightTask`
2. 每轮战斗前检查远征间隔，超时则插入 `collect_expedition()`
3. 遇到船坞满 (`DOCK_FULL`) 跳过当前任务剩余轮次
4. 全部完成后打印汇总

### FightRunnerProtocol

调度器通过协议 (`Protocol`) 定义 Runner 接口：

```python
@runtime_checkable
class FightRunnerProtocol(Protocol):
    def run(self) -> CombatResult: ...
```

- `NormalFightRunner` / `EventFightRunner` → 天然满足
- `CampaignRunner` / `ExerciseRunner` → 返回 `list[CombatResult]`，调度器自动使用 `BatchRunnerAdapter` 包装

### BatchRunnerAdapter

```python
class BatchRunnerAdapter:
    def run(self) -> CombatResult:
        results = self._inner.run()  # → list
        return results[-1] if results else CombatResult(flag=OPERATION_SUCCESS)
```

将返回列表的 Runner 适配为单次协议。

### FightTask 数据类

```python
@dataclass
class FightTask:
    runner: object        # Runner 实例
    times: int = 1        # 执行次数
    name: str = ''        # 任务名称 (日志用)
    # 运行时状态
    completed: int        # 已完成轮次
    results: list[CombatResult]  # 每轮结果
```

### 远征自动检查

- `expedition_interval`: 检查间隔 (秒)，默认 900 (15 分钟)
- 设为 0 或负数禁用
- 使用 `time.monotonic()` 计时，不受系统时钟调整影响

---

## HTTP 服务 (Server)

**文件**: `autowsgr/server/main.py`

基于 FastAPI 的 HTTP/WebSocket 接口，供前端 UI 调用。

### 启动

```bash
uvicorn autowsgr.server.main:app --host 0.0.0.0 --port 8000
```

### 路由结构

```
POST /api/system/start    → 启动系统 (加载配置+连接模拟器)
POST /api/system/stop     → 停止系统

POST /api/task/*           → 任务执行 (战斗/战役/演习/活动)
GET  /api/game/*           → 游戏状态查询

POST /api/expedition/*     → 远征操作
POST /api/build/*          → 建造操作

GET  /api/health           → 健康检查

WS   /ws/logs              → 实时日志流
WS   /ws/task              → 任务状态更新
```

路由按功能拆分到子模块：

| 模块               | 前缀               | 职责           |
|--------------------|---------------------|----------------|
| `routes/system.py` | `/api/system/*`     | 系统管理       |
| `routes/task.py`   | `/api/task/*`       | 任务执行       |
| `routes/game.py`   | `/api/game/*`       | 游戏状态查询   |
| `routes/ops.py`    | `/api/expedition/*` | 操作类接口     |
| `routes/health.py` | `/api/health`       | 健康检查       |

### 全局上下文管理

```python
_ctx: Any = None  # 全局 GameContext

def get_context() -> Any:
    if _ctx is None:
        raise RuntimeError('系统未启动')
    return _ctx
```

HTTP 服务使用全局 `_ctx` 保存 `GameContext`。通过 `POST /api/system/start` 初始化。

### 生命周期

```python
@asynccontextmanager
async def lifespan(app):
    loop = asyncio.get_running_loop()
    task_manager.set_loop(loop)
    yield
    # 清理资源
```

- 启动时: 注入事件循环引用到 `TaskManager`
- 关闭时: 断开模拟器连接

---

## 任务管理器 (TaskManager)

**文件**: `autowsgr/server/task_manager.py`

处理 HTTP 任务请求的异步执行层，独立于脚本模式的 `TaskScheduler`。

### 状态机

```
IDLE → RUNNING → COMPLETED
                → FAILED
                → STOPPED
```

`TaskStatus` 枚举定义五种状态。

### 执行模型

```python
task_manager.start_task(
    task_type='normal_fight',
    total_rounds=10,
    executor=my_executor_fn,  # Callable[[TaskInfo], list[dict]]
)
```

- 单任务串行: 同一时刻只允许一个运行中任务
- 后台线程: 战斗在独立线程执行，不阻塞 FastAPI 事件循环
- WebSocket 通知: 进度和完成状态通过 `ws_manager` 推送

### TaskInfo 数据类

```python
@dataclass
class TaskInfo:
    task_id: str              # UUID-based ID
    task_type: str            # 任务类型标识
    status: TaskStatus
    current_round: int        # 当前进度
    total_rounds: int         # 总轮次
    current_node: str | None  # 当前节点
    results: list[dict]       # 结果列表
    stop_requested: bool      # 停止请求标志
```

### 关键方法

| 方法                  | 调用方      | 说明                        |
|-----------------------|------------|-----------------------------|
| `start_task()`        | 路由层      | 创建任务并启动后台线程       |
| `stop_task()`         | 路由层      | 请求停止 (设置标志)          |
| `update_progress()`   | 执行线程    | 更新进度并推送 WebSocket     |
| `add_result()`        | 执行线程    | 添加一轮结果                 |
| `should_stop()`       | 执行线程    | 检查是否应停止               |

### 线程-事件循环桥接

```python
asyncio.run_coroutine_threadsafe(
    self._notify_completion(task),
    self._loop,
)
```

后台线程通过 `run_coroutine_threadsafe` 将 WebSocket 通知投递到 FastAPI 事件循环。

---

## WebSocket 管理器

**文件**: `autowsgr/server/ws_manager.py`

管理 WebSocket 连接的多播广播器：

- `connect(ws)` / `disconnect(ws)`: 连接生命周期管理
- `send_task_update()`: 推送任务进度
- `send_task_completed()`: 推送任务完成/失败通知
- 心跳: 客户端发送 `{"type": "ping"}`，服务端回复 `{"type": "pong"}`

---

## 脚本模式 vs 服务模式

| 对比项        | 脚本模式 (`TaskScheduler`)   | 服务模式 (`Server`)           |
|---------------|------------------------------|-------------------------------|
| 调用方式      | Python 脚本直接调用          | HTTP/WebSocket 请求           |
| 任务执行      | 主线程同步顺序执行           | 后台线程异步执行              |
| 远征检查      | 内置定时检查                 | 手动触发                      |
| 进度查看      | 日志输出                     | WebSocket 实时推送            |
| 多任务        | 顺序执行多任务               | 单任务串行                    |
| 停止控制      | 不支持 (Ctrl+C)             | `stop_task()` 优雅停止        |

---

## 与其他模块的关系

- **下游**: [ops](ops.md) 的各类 Runner 和操作函数
- **下游**: [context-and-config](context-and-config.md) 的 GameContext 与 ConfigManager
- **下游**: [infra](infra.md) 的日志与配置系统
