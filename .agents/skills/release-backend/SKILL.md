---
name: release-backend
description: 'Release AutoWSGR Python backend to PyPI and GitHub. USE WHEN: bumping backend version, publishing to PyPI, creating backend release, tagging backend build. Includes mandatory local CI check before push.'
---

# Release AutoWSGR Backend

发布 AutoWSGR Python 后端包到 PyPI + 创建 GitHub Release 的完整流程。

## 前置条件

- Python 3.12+ 虚拟环境已激活
- `pre-commit install` 已执行
- 所有待发布改动已合并到 `main`

## 发布步骤

### 1. 更新版本号

编辑 `autowsgr/__init__.py`：

```python
__version__ = 'x.y.z'
```

`pyproject.toml` 通过 `{attr = "autowsgr.__version__"}` 动态读取，无需手动编辑。

版本号遵循语义化版本：
- **patch** (x.y.Z): bug 修复、小调整
- **minor** (x.Y.0): 新功能、新 API
- **major** (X.0.0): 破坏性变更

### 2. 本地跑 CI（必须！）

**远端 CI 经常因环境差异失败，必须先在本地确认通过后再推送。**

```bash
# 跑全部 pre-commit 检查（等同 CI lint job）
pre-commit run --all-files

# 确认包能正常构建
python -m build
```

常见本地 CI 失败原因及修复：

| 检查项 | 失败原因 | 修复方式 |
|--------|----------|----------|
| `check-shebang-scripts-are-executable` | 有 shebang 的脚本未标记可执行 | `git add --chmod=+x <file>` |
| `ruff-check` | 代码风格/lint 错误 | `ruff check --fix .` 自动修复；无法自动修复的需手动改 |
| `ruff-format` | 格式不规范 | `ruff format .` |
| `trailing-whitespace` | 行尾空格 | pre-commit 自动修复后 re-add |
| `end-of-file-fixer` | 文件末尾缺换行 | pre-commit 自动修复后 re-add |
| `codespell` | 拼写错误 | 修正拼写或加入 `docs/spelling_wordlist.txt` |

#### ruff 常见错误处理策略

- **E402** (import not at top): 把 `_log = get_logger(...)` 移到 import 之后
- **UP042** (str+Enum): 改为继承 `StrEnum`
- **RUF015** (list(...)[0]): 改用 `next(iter(...))`
- **FURB110** (ternary if/else): 改用 `or` 运算符
- **SIM401** (if/else → dict.get): 改用 `.get(key, default)`
- **不适合修复的规则**: 已在 `pyproject.toml [tool.ruff.lint] ignore` 中配置忽略（如 N818, RUF022, RUF012）

**如果 pre-commit 自动修了文件，需要重新 `git add` 再跑一遍（跑两遍是正常的）。**

### 3. 提交并推送

```bash
git add -A
git commit -m "release: vX.Y.Z"
git push origin main
```

**注意**：backend 不需要手动打标签。`release.yml` workflow 会自动从 `__init__.py` 提取版本号并创建 `vX.Y.Z` 标签。

### 4. CI 自动执行

推送到 `main` 且 `autowsgr/__init__.py` 有变更时，两个 workflow 并行触发：

**python-publish.yml**（PyPI 发布）：
1. `python -m build` 构建 sdist + wheel
2. `pypa/gh-action-pypi-publish` 上传到 PyPI

**release.yml**（GitHub Release）：
1. **lint job**: `pre-commit run --all-files` + `python -m build`（质量门禁）
2. **release job**（依赖 lint 通过）：
   - 从 `__init__.py` 提取版本号
   - 创建 git tag `vX.Y.Z`
   - 创建 GitHub Release（自动生成 release notes）

### 5. 验证发布

- [ ] [PyPI 包页面](https://pypi.org/project/autowsgr/) 版本已更新
- [ ] GitHub Release 页面有新版本
- [ ] `pip install autowsgr==X.Y.Z` 能正常安装

## 关键文件

| 文件 | 说明 |
|------|------|
| `autowsgr/__init__.py` | 版本号定义（唯一版本源） |
| `pyproject.toml` | 包配置、依赖声明 |
| `.github/workflows/python-publish.yml` | PyPI 发布 workflow |
| `.github/workflows/release.yml` | GitHub Release workflow（含 lint 门禁） |
| `.pre-commit-config.yaml` | 本地 + CI lint 规则 |

## 回滚

如果发布了有问题的版本：
1. PyPI: 在 pypi.org 项目管理页面 yank 该版本（无法删除，只能 yank）
2. GitHub Release: 删除对应 Release 和 tag
3. 修复后发一个新的 patch 版本
