# Unsloth WebUI 访问指南

记录 Unsloth 无头推理服务 + Web 管理控制台（Studio UI）在 B 机（node1，nginx 网关）与 A 机（公网客户端）之间的部署、nginx 转发与访问方式。适用于 `models/unsloth/deepseek-v4-flash.yaml` 等 unsloth 引擎 profile。

架构：

```
A 机浏览器 ──公网──> B 机 nginx :5000 ──局域网──> .210 unsloth studio :8888 (WebUI)
                                             └──> .210 unsloth studio run :8001 (推理 API)
```

## 一、Unsloth 引擎关键机制（与常见认知不同）

- **无头推理命令**是 `unsloth studio run --api-only`（`studio` 是命令组，`run` 子命令承载模型/网络 flag），不是 `unsloth studio --api-only`。
- **API key 由 unsloth 运行时自动生成**，每次加载模型生成一把 `sk-unsloth-…`，打印在启动日志的 `API Key:` 行（模型加载完成后才出现）。`run` 命令拒绝 `--api-key` 参数，profile 无需（也不应）配置。
- modelctl 已自动处理：健康检查 / 预热 / 网关转发均从启动日志解析运行时 key；`modelctl start` 成功后会打印「上游 API Key（本次启动自动生成）」。
- 运行时依赖官方安装器：`curl -fsSL https://unsloth.ai/install.sh | sh`（仅 `pip install unsloth` 不够，缺少 Studio 运行时）。

## 二、启动 WebUI（modelctl 管理）

```bash
# 启动 Web 控制台（等价 unsloth studio -H 0.0.0.0 -p 8888）
# 并自动添加 ufw 白名单：ufw allow from <IP> to any port 8888 proto tcp
bash script/modelctl.sh ui start deepseek-v4-flash-unsloth --allow-from 192.168.77.202

# 停止（按 PID/端口，不会误杀推理实例）
bash script/modelctl.sh ui stop deepseek-v4-flash-unsloth

# 临时覆盖端口/来源
bash script/modelctl.sh ui start deepseek-v4-flash-unsloth --port 9999 --allow-from <IP>
```

- 默认配置见 `models/unsloth/deepseek-v4-flash.yaml` 的 `unsloth.ui` 段（`port: 8888`、`allow_from: [192.168.77.202]`），CLI 参数优先。
- `--allow-from` 只接受纯 IP，不能带 `http://`；ufw 未安装或失败会告警并提示手动命令。
- UI 实例记为 `ui-<name>`，与推理实例（`:8001`）的 PID/日志相互独立。
- 首次登录：用户名 `unsloth`，密码在 `.210` 的 `/root/.unsloth/studio/auth/.bootstrap_password`，登录后建议修改。

## 三、nginx 转发（B 机仅开放 5000 端口）

**关键约束**：Studio 前端为 Vite 构建，页面入口可挂子路径 `/unsloth-ui/`，但页面内资源走根绝对路径（`/@vite/`、`/src/`、`/node_modules/`、`/assets/`、`/api/`、`/ws/`、`/theme-boot.js`），必须逐类转发，否则落进 `location /`（→ 5001）导致 502。

在 `sites-enabled/myflaskapp` 的 `listen 5000` server 块内追加：

```nginx
    # ================= Unsloth Studio WebUI (node210) =================
    location ^~ /unsloth-ui/ {
        auth_basic "Unsloth Studio";
        auth_basic_user_file /etc/nginx/.htpasswd_unsloth;
        proxy_pass http://192.168.77.210:8888/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        client_max_body_size 0;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    # Vite 根路径资源 + Studio API（Basic Auth 通过后浏览器自动携带凭据）
    location ~ ^/(@vite/|@fs/|@id/|src/|node_modules/|assets/|api/|ws/|theme-boot\.js) {
        auth_basic "Unsloth Studio";
        auth_basic_user_file /etc/nginx/.htpasswd_unsloth;
        proxy_pass http://192.168.77.210:8888;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    # 消除 favicon 噪音（否则根路径落进 location / → 5001 → 502）
    location = /favicon.png {
        return 204;
        access_log off;
        log_not_found off;
    }
```

Basic Auth 凭据文件（注意 nginx worker 以 www-data 运行，不能用 600 root 专属权限）：

```bash
htpasswd -c /etc/nginx/.htpasswd_unsloth unsloth   # 交互输入两次密码
chown root:www-data /etc/nginx/.htpasswd_unsloth
chmod 640 /etc/nginx/.htpasswd_unsloth
nginx -t && nginx -s reload
```

## 四、访问步骤（A 机）

1. 浏览器打开 `http://36.156.121.146:5000/unsloth-ui/`（无痕窗口，避免凭据缓存混淆）。
2. Basic Auth：`unsloth` / 上面设置的密码。
3. Studio 登录页：`unsloth` + bootstrap 密码（见 `.210` 的 `/root/.unsloth/studio/auth/.bootstrap_password`）。

## 五、验证命令（B 机 node1）

```bash
curl -I http://127.0.0.1:5000/unsloth-ui/                          # 期望 401
curl -s -u 'unsloth:<密码>' http://127.0.0.1:5000/api/health       # 期望 {"status":"healthy",...}
nginx -t && nginx -s reload
```

## 六、排障记录

| 现象 | 根因 | 解决 |
| --- | --- | --- |
| `No such option: --model` | CLI 是 `unsloth studio run`，旧命令 `unsloth studio --api-only --model` 参数不合法 | 命令改为 `studio run`（已修复进代码） |
| `Unsloth Studio not set up` | 未执行官方安装器 | `curl -fsSL https://unsloth.ai/install.sh \| sh` |
| `ModuleNotFoundError: uvicorn` | Studio venv 依赖不完整 | 重跑安装器修复 |
| 健康检查超时 / 401 | API key 由运行时自动生成，profile 配的 key 无效 | 从启动日志解析 `API Key:`（已修复进代码） |
| Basic Auth 500 | htpasswd 文件 600 root 专属，nginx worker 读不了 | `chown root:www-data` + `chmod 640` |
| 页面 502 | Studio 前端为 Vite 构建，子路径资源走根绝对路径 | 补 `/assets/` 等根路径转发（见第三节） |
| 卡「加载中...」且无 /api 请求 | 浏览器工具经 userinfo URL 加载，preemptive 凭据不缓存，`/api/` 被 401 | 真实浏览器手动输凭据即可；自动化工具不支持 Basic Auth 弹窗 |
| `curl -I` 返回 405 | HEAD 请求被拒（`allow: GET`），属正常 | 用 GET 验证 |
| `/favicon.png` 502 | 根路径资源未转发 | `location = /favicon.png { return 204; }` |

## 七、安全提醒

- 5000 端口对公网开放时，`/unsloth-ui/` 必须保留 Basic Auth；密码不要太弱（勿用 123456）。
- Studio 控制台可查看 API key、加载模型，泄露等于放开服务器执行权限。
- `.210` 的 8888 端口由 ufw 白名单收窄到 node1 与白名单来源；公网不直连 8888。
- 若 A 机为动态公网 IP，IP 白名单不可靠，优先 Basic Auth（本方案已采用）。
