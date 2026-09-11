# Lint / 类型检查工具链与 CI 口径对齐

> 原始单文件已并入本文件归档，保留溯源信息。

## ruff 满屏 `panic: ... wrong package cache for file` —— 本地缓存损坏，不是 ruff 版本 bug

**日期**：2026-09-11
**症状**：`uv run ruff check src tests` 输出里混着上百条

```
panic: wrong package cache for file ...
```

原始体检报告据此定性为"ruff 存在 bug，需升级版本 / 配 per-file-ignore"，并把真实告警
数记成 229。

**根因**：与 ruff 版本、被检文件内容**均无关**，是本地 `.ruff_cache` 目录损坏（跨版本
复用缓存所致）。证据链（全部留档于 `docs/health-checks/raw/`）：

| 步骤 | 命令 | 结果 |
|---|---|---|
| 1 | `uv run ruff check src tests` | 238 errors + 100 panic（首屏截断，误判全貌） |
| 2 | `uv tool run ruff check src tests` | **同样 panic** ⇒ 排除 uv 注入路径 |
| 3 | `uv run ruff check --no-cache src tests` | **0 panic** |
| 4 | `ruff clean` 后再跑 | **0 panic**，272 errors（真实数字） |

**解决方案**：本地见 panic 先 `uv run ruff clean`（或一律 `--no-cache` 取权威数字）。
本次**未改** `pyproject.toml`——无需升级 ruff，也无需 per-file-ignore。

**教训**：

- 工具报 panic 时先做**缓存变量排除**（`--no-cache` / `clean`）再谈版本假设；把缓存损坏
  误诊成版本 bug，会引出"升级 + 到处加 ignore"的错误修复面。
- panic 会让首屏输出被截断，**error 计数随之失真**（本次 229 → 真实 272）。任何以工具
  输出为基线的门禁口径，必须在 panic 归零后重新取数。
- 完整排查记录见 `docs/health-checks/raw/ruff-panic-triage.md`。

## CI 与本地的 ruff/mypy 结果必然有差异：口径钉在 Python 3.12

**日期**：2026-09-11
**症状**：本地（Python 3.13）跑 `ruff check` / `mypy src/modelctl` 的告警集合与 CI 不一致；
本地 `mypy` 还偶发 `INTERNAL ERROR`（2.3.1）。

**根因**：`.github/workflows/ci.yml` 是 `ubuntu-latest` + `astral-sh/setup-uv@v5` +
`python-version: "3.12"`，依赖版本浮动（`ruff>=0.4`、`mypy>=1.10`，`uv sync --extra dev`
解析到的版本随时间漂移），且 `pyproject.toml` 中 `[tool.ruff].target-version` 与
`[tool.mypy].python_version` 均为 `py312`。本地 3.13 + 另一套工具版本，产生差异属预期。

**结论 / 门禁口径**：

- 发布门的 lint/type 口径**以 CI 为准**；需要本地复现 CI 结论时用
  `uv run --python 3.12 ruff check src tests` 显式对齐解释器。
- 本地 3.13 不以 lint 数量作硬门禁，改以 **pytest 全量 0 failed + 隔离端口冒烟**为硬门禁；
  工具链告警只做定位、不做定量清零。
- CI 是全新 checkout，不复用 `.ruff_cache`，故上一节的 panic 现象在 CI 不会出现——
  "CI 也可能 panic"的疑点随缓存定性一并消解。
