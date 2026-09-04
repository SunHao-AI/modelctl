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
| 2026-09-04 | 后端 / 引擎启动 | 缺 cmake 报错只说"请安装"，不给可执行安装命令 | `require()` 新增 `install_hint()`，按系统包管理器（apt/dnf/yum/zypper/pacman/apk）拼出安装命令并按需加 sudo。 | [backend/engine-launch-args.md](backend/engine-launch-args.md) |
| 2026-09-04 | 构建 / 部署环境 | nginx 模板里的 `<办公网段>` 占位符让 `nginx -t` 直接 emerg | nginx 不认尖括号约定，`invalid parameter` 硬失败且 `&&` 短路使旧配置继续跑；`allow` 命中的是客户端 IP 而非后端 IP（403 与 502 分属两类故障）。 | [build/nginx-webui-proxy.md](build/nginx-webui-proxy.md) |
| 2026-09-04 | 构建 / 部署环境 | 模板证书路径未替换，`nginx -t` 报 `cannot load certificate` | `listen ssl` 在解析阶段就加载证书；优先抄同机已有证书路径，自签必须带 SAN（浏览器已忽略 CN，只写 CN 会"nginx 通过、浏览器报错"）。 | [build/nginx-webui-proxy.md](build/nginx-webui-proxy.md) |
| 2026-09-04 | 构建 / 部署环境 | 公网只暴露单端口时，管理面不能多节点共存 | 单端口下根路径唯一 ⇒ 只能承载一个节点，靠 `server_name` 加 vhost 或顶掉原 `location /`，后者会让 LLM 路由配漏从 502 变成"返回 HTML"。 | [build/nginx-webui-proxy.md](build/nginx-webui-proxy.md) |
| 2026-09-04 | 前端 / 部署路径 | `/webui` 子路径访问 SPA 白屏，nginx 单独做不到 | 入口 HTML、`/assets/`、`/admin/api/` 三个根级命名空间必须同时可达且 Router base 要一致；补 location 只解决资源不解决路由，改动面是 6 个前端文件。 | [build/nginx-webui-proxy.md](build/nginx-webui-proxy.md) |
| 2026-09-04 | 前端 / SSE 寻址 | 后端返回的 `stream_url` 其实没人消费 | 前端 `openTaskStream()` 用 `taskId` 自行拼绝对路径，改子路径时后端 6 处 `stream_url` 是无关项，真正要改的是前端 3 处 EventSource 拼接。 | [build/nginx-webui-proxy.md](build/nginx-webui-proxy.md) |
| 2026-09-04 | 构建 / 部署环境 | Web UI 默认绑 127.0.0.1，跨机 nginx 反代必 502 | `webui_host()` 的安全默认所致；作为反代中心的节点需 `WEBUI_HOST=0.0.0.0`，其余节点保持回环不暴露。 | [build/nginx-webui-proxy.md](build/nginx-webui-proxy.md) |
| 2026-09-04 | 前端 / 部署路径 | 想用 Cookie 定桩在一个端口跑多节点 UI，会停错模型 | 同域 Cookie 不分标签页，`/208/` 页面的 3s 轮询与启停按钮会打到后来打开的 209；Web UI 能停模型删 venv，属真实破坏。三端口 + localStorage 按 origin 隔离才安全。 | [build/nginx-webui-proxy.md](build/nginx-webui-proxy.md) |
| 2026-09-04 | 构建 / 部署环境 | `--run` 的 daemon.json 合并只追加不清理，坏源永久残留 | 停服 mirror 排在首位时 Docker 第一个就 DNS 失败退出，新追加的好源没机会生效；新增 `DEAD_REGISTRY_MIRRORS` 让 `--run` 幂等收敛，删除也要计入 `changed`。 | [build/docker-install-mirror.md](build/docker-install-mirror.md) |
| 2026-09-04 | 构建 / 部署环境 | 大镜像 `docker pull` 中途 `short read … EOF`，加镜像源无效 | mirrors 是有序 failover 不是带宽池，加源对传输中断无帮助；`ensure_image()` 利用 layer 复用做重试并按错误三类处置，daemon 级 `max-concurrent-downloads=2` 降低大 layer 互抢（注意它不是 pull 参数、`docker info` 也不显示）。 | [build/docker-install-mirror.md](build/docker-install-mirror.md) |
| 2026-09-04 | 后端 / 配置管理 | 运行时数据目录默认值散在各调用点，长成三套口径 | logs 落项目根上级、usage 与 cache 撞目录、audit 相对 CWD；统一到 `core/paths.py` 单点解析（相对值按 PROJECT_ROOT）。 | [backend/runtime-data-dir.md](backend/runtime-data-dir.md) |
| 2026-09-04 | 后端 / 配置管理 | gpu_lock 模块级常量绕过 CACHE_DIR，GPU 互斥静默失效 | 锁目录常量导入时求值，设了 CACHE_DIR 也不生效 → PID 与 .gpu-lock 分家，`list_gpu_locks()` 恒报无冲突；改函数即时解析。 | [backend/runtime-data-dir.md](backend/runtime-data-dir.md) |
| 2026-09-04 | 后端 / 配置管理 | profile 的 alias 与自动推导 name 同名，整个 profile 被静默跳过 | `{group}-{engine}` 恰好等于 alias 时 `_parse_aliases` 抛错，而 `list_profiles` 只 warning+continue → 配置凭空消失；新增 profile 必须 `load_profile()` 实加载确认。 | [backend/profile-config-drift.md](backend/profile-config-drift.md) |
| 2026-09-04 | 后端 / 引擎启动 | profile 默认值全按 8×48GB 设计，小显存单卡照抄必失败 | `gpu_count` 缺省 8、`dspark` 缺省 on、`ctx_size` 缺省 **1M**；6GB 单卡须逐个显式覆盖，且 vllm/sglang 托管 venv **仅 Linux** 可建。 | [backend/profile-config-drift.md](backend/profile-config-drift.md) |

## 目录约定

- `frontend/` —— 前端（Vue 3 + Element Plus）常见问题
- `backend/` —— 后端（FastAPI / Python）常见问题
- `database/` —— 数据库（MySQL）常见问题
- `build/` —— 构建与依赖（uv / 打包）常见问题

每个分类下按语义主题聚合为少量 `<主题>.md`；单主题超过约 40 条即按更细粒度拆分。
