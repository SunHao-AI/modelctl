# 已知问题索引（渐进式披露）

> 摘要层：仅记录标题、分类、日期与一句话描述。需要完整根因/方案时按需读取详情文件。

| 日期 | 分类 | 标题 | 一句话描述 | 详情 |
|---|---|---|---|---|
| 2026-09-03 | 构建 / 依赖 | uv 的 `default = true` 会让镜像源变成最低优先级 | 给 `[[index]]` 加 `default = true` 是降到兜底位而非设为主源，解析流量全落官方源。 | [build/uv-index-and-download.md](build/uv-index-and-download.md) |
| 2026-09-03 | 后端 / 运行时 | 进程未设置时区，日志与审计时间比预期早 8 小时 | 全项目隐式本地时间继承宿主 OS 时区；用标准 `TZ` 覆盖进程/子进程/容器三条路径（默认 Asia/Shanghai），含 Windows 写 `TZ` 污染子进程成 +0100 的坑。 | [backend/timezone.md](backend/timezone.md) |
| 2026-09-03 | 后端 / 引擎启动 | vLLM venv 分支漏传 `--port`，健康检查永远 Connection refused | `vllm serve` 回退默认 8000 与 `profile.port` 脱节，表现为端口秒退或健康检查空等超时；`--port` 须置于 `extra_args` 之前。 | [backend/engine-launch-args.md](backend/engine-launch-args.md) |
| 2026-09-03 | 后端 / 引擎启动 | `vllm --version` 探测 5s 超时，版本门控长期静默失效 | 跑 CLI 问版本会触发完整 import 链；改读 dist-info 元数据，且版本正则绝不能碰 stderr（会把解释器版本当成包版本误放行）。 | [backend/engine-launch-args.md](backend/engine-launch-args.md) |
| 2026-09-03 | 测试 / 隔离 | 生产代码 `load_env()` 把本地 .env 泄漏进测试，用例结论随机器漂移 | `os.environ.setdefault` 不受 monkeypatch 管辖；`GATEWAY_DEFAULT_MODEL` 泄漏让未知 model 不再 404（全量红、单跑绿）；conftest 需对改变控制流的 env 做 delenv 白名单。 | [backend/test-isolation.md](backend/test-isolation.md) |
| 2026-09-03 | 测试 / 收集 | `tests/` 里调试脚本模块级 `sys.exit` 掀翻整个 pytest session | `test_*.py` 顶层在 collection 阶段执行；改造成 fixture + 断言，路由枚举改用 `app.openapi()["paths"]`。 | [backend/test-isolation.md](backend/test-isolation.md) |
| 2026-09-03 | 后端 / 启动预检 | 端口占用无预检，引擎秒退后靠翻日志反推 EADDRINUSE | 新增 `port_in_use` 前置拦截并点名占用者；ollama 共享 serve 端口是设计语义，必须豁免。 | [backend/engine-launch-args.md](backend/engine-launch-args.md) |
| 2026-09-04 | 构建 / 部署环境 | download.docker.com 国内 TLS 握手失败，docker-ce 无安装候选 | 换清华 docker-ce 镜像源；已内置 `modelctl env setup docker [--run]`，含 daemon.json 合并与过时 docker.io 提示的纠正。 | [build/docker-install-mirror.md](build/docker-install-mirror.md) |
| 2026-09-04 | 构建 / 部署环境 | daemon.json 残留停服 Hub 加速域名，`modelctl start` 健康检查超时 | mirror 域名 DNS 失败是硬失败、不回落官方源，容器从未起；代码里的默认源修好了也要重跑 `--run` 才落到既有 daemon.json。 | [build/docker-install-mirror.md](build/docker-install-mirror.md) |
| 2026-09-04 | 后端 / 配置管理 | 下载后写回 model 路径导致 git 脏区，服务器 git pull 被挡 | 删除 `_persist` 写回机制；落地路径由 MODEL_ROOT+modelscope_id 确定性推导，目录已就位即复用，YAML 永不改写；gateway/uv.lock 同步 gitignore。 | [backend/profile-config-drift.md](backend/profile-config-drift.md) |

## 目录约定

- `frontend/` —— 前端（Vue 3 + Element Plus）常见问题
- `backend/` —— 后端（FastAPI / Python）常见问题
- `database/` —— 数据库（MySQL）常见问题
- `build/` —— 构建与依赖（uv / 打包）常见问题

每个分类下按语义主题聚合为少量 `<主题>.md`；单主题超过约 40 条即按更细粒度拆分。
