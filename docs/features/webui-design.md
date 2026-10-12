# AutoWSGR WebUI 设计文档（ALAS 模式调度前端）

> 状态：**待用户确认**（superpowers 头脑风暴产物，确认后进入实施计划 → worktree → TDD）
> 日期：2026-10-12 ｜ 作者：WorkBuddy + 用户
> 关联：`docs/architecture/scheduler-and-server.md`（既有 HTTP 服务）、
> `autowsgr/scheduler/timed_scheduler.py`（2026-10-12 交付的定时调度器）

---

## 1. 目标与非目标

### 目标（一句话）

**启动调度器后，在网页上勾选需要的任务，调度器按优先级自动安排执行**——
即 ALAS 的核心交互模式，落到 AutoWSGR 现有的 FastAPI 服务之上。

### 第一期范围（三件套，已与用户确认）

| # | 能力 | 前端表现 |
|---|---|
| 1 | 连接管理 | 设备/游戏状态展示，启动/停止系统 |
| 2 | 任务勾选与调度控制 | 勾选表格（启用哪些日常任务+参数）、「启动/停止调度器」按钮、任务运行态表格 |
| 3 | 实时日志 | WS 日志流滚动终端 |

### 非目标（本期不做，列入二期）

- 配置全量可视化编辑（emulator/ocr/log 段）——一期只编辑 `daily_automation` 段
- 远程访问与令牌鉴权（一期仅监听 127.0.0.1）
- 作战计划（YAML）可视化编辑器——一期只做「从 plan 目录下拉选择」
- WS 推送运行态（一期用 1s 轮询）
- 多配置实例并行（ALAS 的多 alas 实例管理）

---

## 2. 现状盘点

### 已有资产（直接消费，不改）

- `autowsgr/server/`：FastAPI 应用，路由 `system`(启停/状态) / `task`(5 类任务) /
  `game` / `ops`(远征/建造/修理/强化/奖励/料理/解装) / `health`
- WS：`/ws/logs`（实时日志流）、`/ws/task`（任务状态）
- `task_manager`：executor 线程模型（`is_running` / `stop_event` /
  `should_stop()` / 进度回调 / 设备租约），`testing/server/` 12 个测试文件
- `ConfigManager.load()` + `inffra/file_utils.save_yaml()`：配置读写基础件
- `TimedScheduler` + `wsgr_daily.run_daily_automation()`：昨天交付，等一个用户入口
- `DailyAutomationConfig`：勾选语义已全部建模（`auto_expedition` /
  `auto_gain_bonus` / `auto_bath_repair` / `auto_battle` + `battle_type` /
  `auto_exercise` + `exercise_fleet_id` / `auto_normal_fight` +
  `normal_fight_tasks` + `quick_repair_limit`）

### 缺口（本期增量）

1. 无前端页面消费上述 API
2. 无「调度器」任务类型——`TimedScheduler` 只能从 Python 代码启动
3. 无配置读写 API——勾选状态无处落盘
4. `TimedScheduler` 无运行态外暴露（next_run/上次结局/失败计数只在日志里）

---

## 3. 总体架构与数据流

```
┌───────────────────────────┐        ┌────────────────────────────────────┐
│  webui/（Vite 工程，新增）  │        │  autowsgr.server（已有 + 增量）      │
│                           │        │                                    │
│  连接面板 ────────────────┼────────▶│ POST /api/system/start|stop  (已有) │
│  勾选表格 + 参数表单       │        │                                    │
│    └ GET/PUT ─────────────┼────────▶│ /api/scheduler/config      (新增)  │
│  启动/停止调度器 ──────────┼────────▶│ /api/scheduler/start|stop  (新增)  │
│  运行态表格 ◀─────────────┼────────▶│ GET  /api/scheduler/status  (新增)  │
│  日志终端 ◀───────────────┼──WS────│ WS /ws/logs                  (已有) │
└───────────────────────────┘        └────────────────────────────────────┘
                                                    │
                                                    ▼
                              task_manager executor 线程（已有机制）
                              run_daily_automation(ctx, state_path=...)
                              → TimedScheduler.run()（常驻，直到 stop_event）
```

**核心语义**：勾选状态落盘到 `usersettings.yaml` 的 `daily_automation` 段
（与 CLI、桌面 bat 共用同一份配置，不搞第二份真相源）；「启动调度器」在
executor 线程里跑 `TimedScheduler`，按 远征0<奖励1<战役5<演习10<常规战100<浴室200
自动调度，直到用户点停止。

---

## 4. 后端设计（3 处增量，全部 TDD）

### 4.1 新增 `autowsgr/server/routes/scheduler.py`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/scheduler/config` | 返回 `daily_automation` 当前值 + 可用 plan 列表（扫 `autowsgr/data/plan/`） |
| PUT | `/api/scheduler/config` | 写回 `usersettings.yaml`，**仅允许改 `daily_automation` 段**；写前自动 `.bak` 备份 |
| POST | `/api/scheduler/start` | 以「scheduler 模式」启动 task_manager（无参数 = 用落盘配置；可传临时覆盖） |
| POST | `/api/scheduler/stop` | 停止（`task_manager.stop_task()` → `ctx.stop_event`） |
| GET | `/api/scheduler/status` | 运行态快照，见 4.3 |

约束：调度器运行中拒绝重复 start（409，与 task 路由一致）；config 写入用
`load_yaml` → 改段 → `save_yaml` 的读写-修改-写回，保持其余段原样。

### 4.2 `task_manager` 增加「scheduler」任务类型

executor 内调用 `run_daily_automation(ctx, state_path=<log 目录>/timed_state.json)`，
复用现有的线程/停止/租约机制，无新机制。`TaskInfo.type='scheduler'`。

### 4.3 `TimedScheduler` 运行态外暴露

`TimedScheduler` 增加可选 `status_sink: Callable[[list[TaskState]], None]`，
每次 `_apply_outcome` 后调用；server 侧用模块级 holder 保存当前 scheduler 实例，
`/api/scheduler/status` 读它。响应结构：

```json
{
  "running": true,
  "current_task": "常规战/8-5AI",
  "tasks": [
    {
      "name": "远征", "priority": 0, "enabled": true,
      "next_run": "2026-10-12 18:30:00",
      "last_outcome": "success", "failure_count": 0
    }
  ]
}
```

---

## 5. 前端设计（Vue 3 + Element Plus + Vite）

### 5.1 技术选型（已确认）

Vue 3 + Element Plus + Vite + TypeScript。理由：表单/表格/开关类后台的最短
路径；dev 模式 vite proxy 转发 `/api` 与 `/ws` 到 `127.0.0.1:8000`；build 产物
输出到 `autowsgr/server/static/`，由 FastAPI `StaticFiles` 托管——生产环境
零 Node 依赖，`uvicorn autowsgr.server.main:app` 一条命令带前端。

### 5.2 目录结构（控制在 6 个源文件以内，YAGNI）

```
webui/
├── package.json / vite.config.ts / tsconfig.json / index.html
└── src/
    ├── main.ts                # createApp + ElementPlus
    ├── App.vue                # el-tabs 三页签：调度 / 日志 / 连接
    ├── api/client.ts          # fetch 封装（apiXxx）+ WS 封装（LogStream）
    └── views/
        ├── TaskBoard.vue      # 勾选表格 + 参数表单 + 启停按钮 + 运行态表格
        ├── LogPanel.vue       # 日志终端（el-scrollbar 自动到底）
        └── ConnectPanel.vue   # system status + start/stop
```

不做 vue-router（页签切换）、不做 pinia（跨组件状态只有「运行态」一处，
`TaskBoard` 内部持有即可）。

### 5.3 勾选表格 ↔ 配置映射

| UI 控件 | 配置字段 |
|---|---|
| 开关：自动远征 | `auto_expedition` |
| 开关：任务奖励 | `auto_gain_bonus` |
| 开关：浴室修理 | `auto_bath_repair`（+ 黑名单编辑，可后补） |
| 开关：自动战役 + 下拉 | `auto_battle` + `battle_type`（10 种） |
| 开关：自动演习 + 舰队号 | `auto_exercise` + `exercise_fleet_id` |
| 开关：自动常规战 + 多行表 | `auto_normal_fight` + `normal_fight_tasks`（plan 下拉 + 舰队 + 次数，次数空=无限） |
| 数字：快修上限 | `quick_repair_limit` |

plan 下拉的数据源 = `GET /api/scheduler/config` 返回的可用 plan 列表
（扫包内 `autowsgr/data/plan/`），用户不手输路径。

---

## 6. 测试策略

- **后端**（主力）：`testing/server/test_scheduler_routes.py`，沿用既有
  TestClient + 假 ctx 模式；配置写回用 tmp_path 断言「只动 daily_automation 段、
  其余段字节级不变、.bak 生成」；启停/重复 start 409/status 结构各若干项；
  `status_sink` 单测。**变异验证**（项目纪律：改坏逻辑必须有测试挂）。
- **前端**（冒烟）：vitest 测 `api/client.ts` 的响应解析与错误分支；
  `TaskBoard` 渲染一条假状态不报错。不上 E2E（无浏览器环境）。
- **联调**： dev 模式手点一遍（连接 → 勾选 → 启动 → 看日志 → 停止），
  记录到实施计划的验证步骤。

---

## 7. 部署与运行

| 场景 | 命令 |
|---|---|
| 开发 | 终端1 `uvicorn autowsgr.server.main:app --port 8000`；终端2 `cd webui && npm run dev`（proxy 到 8000） |
| 生产 | `cd webui && npm run build` → 产物进 `autowsgr/server/static/` → 只跑 uvicorn |

webui 工程放主仓 `webui/` 目录（不新建仓库）：与后端同版本发布，build 产物
随包分发；`.gitignore` 忽略 `node_modules/` 与 `dist/`。

---

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| `TimedScheduler` 是昨天新写的、未经实机，WebUI 是它的第一个消费者 | 联调问题按系统化调试流程修；后端测试先锁定契约 |
| `usersettings.yaml` 是实机配置，写坏 = 挂机脚本全坏 | PUT 仅允许 `daily_automation` 段；写前 `.bak`；测试断言其余段不变 |
| 停止非即时：一场战斗约 100s，`stop_event` 只在任务间隙检查 | UI 明示「停止将在当前任务结束后生效」；与既有 task 停止语义一致 |
| 前端引入 npm 构建链，脱离 Python 环境 | 一期控制在 6 个源文件；build 产物进包后运行期零 Node 依赖 |

---

## 9. 分期

- **本期（MVP）**：第 1 节三件套 + 调度器任务类型 + 配置读写（daily_automation 段）
- **二期候选**（按用户反馈排序）：配置全量编辑 / 远程访问+令牌 /
  运行态 WS 推送 / plan 可视化编辑 / 远征编队配置 UI

---

## 10. 确认后流程（superpowers）

设计文档签字 → git worktree 开隔离分支 → 写实施计划（2-5 分钟/任务）→
TDD 实现（后端先行：契约测试 → 路由 → task_manager → status_sink；前端随后）→
代码评审 → 全量测试 + 联调 → 收尾分支（合并/PR/保留/丢弃四选一）。
