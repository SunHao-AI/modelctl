#!/usr/bin/env python3
# ===============================================================================
# @File   : src/modelctl/core/webui/server.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/3 10:00
# @Desc   : Web UI 服务入口与前端静态挂载
# ===============================================================================

"""core/webui/server.py — Web UI 进程入口 + 前端 SPA 静态挂载。

单进程复用网关应用：`create_app(admin=True)` 在 /v1/* 之外挂上 /admin/api/* 与
前端构建产物（`dist/`），因此 `modelctl webui` 与 `modelctl gateway start` 是同一
份 FastAPI 的两个端口，管理面与数据面互不影响。

端口优先级：命令行 --port > 环境变量 WEBUI_PORT > WEBUI_DEFAULT_PORT。
`.env` 中的 WEBUI_PORT 由 CLI 的 load_env() 注入进程环境，再透传给 webui 子进程；
前端 vite dev server 的 proxy target 读同一份 .env，两端只有一处真值来源。

模块顶部不导入 fastapi/uvicorn（gateway extra 可选依赖），与包内其余模块一致。
独立运行：    python -m modelctl.core.webui.server
"""

import os
from pathlib import Path

WEBUI_DEFAULT_PORT = 4173
WEBUI_DEFAULT_HOST = "127.0.0.1"
# 管理面实例名（PID 文件 / 启动日志 / 进程匹配用），与网关的 "llm-gateway" 区分
WEBUI_INSTANCE = "modelctl-webui"


def webui_port() -> int:
    """Web UI 监听端口（WEBUI_PORT 非法值回退默认，与 gateway 端口互不干扰）。"""
    raw = os.environ.get("WEBUI_PORT", "")
    try:
        return int(raw) if raw else WEBUI_DEFAULT_PORT
    except ValueError:
        return WEBUI_DEFAULT_PORT


def webui_host() -> str:
    """Web UI 绑定地址。默认只监听回环：管理 API 无细粒度鉴权，不默认对公网开放。"""
    return os.environ.get("WEBUI_HOST") or WEBUI_DEFAULT_HOST


def dist_dir() -> Path:
    """前端构建产物目录（项目根 dist/）。"""
    # server.py 位于 src/modelctl/core/webui/，项目根 = parents[4]
    return Path(__file__).resolve().parents[4] / "dist"


def dist_ready(path: Path | None = None) -> bool:
    """前端产物是否可用（存在 index.html）。"""
    return (path or dist_dir()).joinpath("index.html").is_file()


def mount_static(app) -> bool:
    """把前端 SPA 挂到 `/`，返回是否实际挂载。

    必须在全部 API 路由注册完成后调用：兜底路由 `/{full_path:path}` 只应接住
    未被 API 命中的 GET，因此依赖注册顺序（后注册者优先级更低）。

    Vue Router 用 history 模式，深链（/models/qwen3.8）刷新时须回 index.html。
    dist/ 缺失时返回 False，由调用方给出提示（不影响 /admin/api 与 /v1 可用）。
    """
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    root = dist_dir()
    if not dist_ready(root):
        return False
    index = root / "index.html"
    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="webui-assets")

    # 这些前缀属 API/文档：未命中具体路由时必须回 404 JSON，绝不能回 SPA 页面，
    # 否则 nginx 转发的 /v1/* 请求会拿到 HTML 而表现为诡异的解析失败。
    api_prefixes = ("v1", "admin", "docs", "openapi.json", "redoc", "health")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_fallback(full_path: str):
        if full_path.split("/", 1)[0] in api_prefixes:
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        return FileResponse(index)

    return True


def main() -> None:
    """独立运行入口：python -m modelctl.core.webui.server。

    `modelctl webui start` 即以此形态（gateway 子环境解释器 -m ...）后台拉起。
    """
    from modelctl.core.envfile import load_env
    from modelctl.core.timezone import apply_timezone

    load_env()
    apply_timezone()

    import uvicorn

    from modelctl.core.gateway import create_app

    host, port = webui_host(), webui_port()
    app = create_app(admin=True)
    hint = "（未找到 dist/，仅暴露 /admin/api；先执行 npm run build）" if not dist_ready() else ""
    print(f"modelctl Web UI 运行于 http://{host}:{port}/ {hint}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
