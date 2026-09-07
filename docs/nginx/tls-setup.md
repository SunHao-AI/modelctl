# 数据面 :5000 启用 HTTPS（自建 CA）操作文档

> 配套配置：[llm-routing.example.conf](llm-routing.example.conf)
> 背景设计：[2026-09-07-gateway-client-auth-design.md](../superpowers/specs/2026-09-07-gateway-client-auth-design.md) §4.4 / §9 裁决 3

> ⚠️ **本文所有 CA / 证书生成与分发命令均为一次性人工操作，由运维在维护窗口亲手执行，Agent 不代跑**（spec §9 裁决 3）。
> 本文只提供命令与步骤，不落可执行脚本；`ca.key`、`server.key` 属私密材料，严禁提交入库、严禁上传到除 B 机以外的任何机器。

## 1. 为什么自建 CA（而不是 Let's Encrypt）

B 机数据面入口是**裸 IP**（如 `36.156.121.146:5000`）。Let's Encrypt 只对**域名**签发证书（HTTP-01 / DNS-01 验证都以域名为单位），不支持给裸 IP 签发。在没有可解析域名的前提下，唯一能得到"客户端可校验信任链"的方案就是**自建根 CA + 自签服务端证书**：证书带 SAN（IP + 内网域名），客户端导入根 CA 后即获得与公网 CA 同等的校验体验（防中间人，而非仅加密）。

## 2. 生成 CA 与服务端证书（B 机 / 运维维护机执行）

在**空的临时目录**中执行（bash；第 3 步用了 `<(...)` 进程替换，Windows 的 PowerShell/cmd 不支持，请在 Git Bash / WSL / Linux 上跑）：

```bash
# 1) 根 CA（10 年；ca.key 离线保管，绝不上传任何机器）
openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
  -keyout ca.key -out ca.crt -subj "/CN=modelctl-internal-CA"

# 2) 服务端 CSR
openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr \
  -subj "/CN=llm.modelctl.internal"

# 3) 签发服务端证书（SAN 必须含实际访问的 IP/域名，否则客户端校验失败）
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days 365 -sha256 \
  -extfile <(printf "subjectAltName=IP:36.156.121.146,DNS:llm.modelctl.internal") \
  -out server.crt

# 4) 部署到 B 机
scp server.crt server.key root@B机IP:/etc/nginx/tls/
ssh root@B机IP 'chmod 600 /etc/nginx/tls/server.key && nginx -t && systemctl reload nginx'
```

要点：

- **SAN 决定客户端能否校验通过**：客户端用什么地址访问 `base_url`，SAN 里就必须有什么。用 IP 访问写 `IP:...`，用 hosts 映射的域名访问写 `DNS:...`；两者都用就都写（如上例）。事后改 SAN = 重签服务端证书（第 2、3 步），根 CA 不动。
- **ca.key 离线保管**：它是整个信任链的根，丢失 = 所有客户端重导 CA；泄露 = 信任链作废。建议加密后存入离线介质，不留存在任何联网机器。
- **服务端证书 1 年到期**：到期前重复第 2、3 步续签（根 CA 10 年有效不用重发），替换 B 机 `/etc/nginx/tls/server.crt` 后 `systemctl reload nginx` 即可，客户端无感。
- 证书文件落 B 机后：`server.key` 必须 `chmod 600`（第 4 步已带）；`server.crt`、`ca.crt` 是公开材料，无敏感。

## 3. 客户端信任根 CA（分发 `ca.crt`）

把 `ca.crt`（仅公钥证书，可安全分发）发给每个使用数据面的终端，按平台导入系统信任库：

### Windows

管理员 PowerShell / CMD：

```bat
certutil -addstore -f ROOT ca.crt
```

- `-f` 覆盖旧版本（续签根 CA 后重导用）；弹窗确认"是否安装证书"选"是"。
- 图形界面等价：双击 `ca.crt` → 安装证书 → 本地计算机 → 将所有的证书都放入下列存储 → 浏览选"受信任的根证书颁发机构"。
- 删除旧条目：`certutil -delstore ROOT "modelctl-internal-CA"`。
- 仅当前用户信任（不需管理员）：把 `ROOT` 换成 `Root` 前先确认策略允许；受管控的企业终端通常只允许机器级存储。

### Linux（Debian / Ubuntu 系）

```bash
sudo cp ca.crt /usr/local/share/ca-certificates/modelctl-internal-ca.crt
sudo update-ca-certificates
```

RHEL / CentOS 系：

```bash
sudo cp ca.crt /etc/pki/ca-trust/source/anchors/
sudo update-ca-trust
```

### macOS

```bash
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain ca.crt
```

或"钥匙串访问"打开系统钥匙串 → 拖入 `ca.crt` → 双击 → 信任 → 当使用此证书时：始终信任。

### 无法安装 CA 的客户端（降级方案，需明示接受风险）

不便导入系统信任库的第三方工具 / SDK，可关闭服务端证书校验：

```bash
curl -k https://36.156.121.146:5000/210/llm/v1/models ...
```

```python
# OpenAI SDK
OpenAI(base_url="https://...", http_client=httpx.Client(verify=False))
# 或 requests
requests.post(url, verify=False, ...)
```

> ⚠️ `-k` / `verify=False` 只保证传输加密，**不校验对方身份，中间人可冒充 B 机截取 API Key**，属安全降级。优先导入 `ca.crt`；确需降级时应在变更记录中登记是哪台客户端（记录时间用 `YYYY-MM-DD HH:mm:ss` 格式）。折中方案：不导入系统库，仅在 SDK 里指定 CA 文件（`httpx.Client(verify="/path/ca.crt")` / `requests.post(verify="/path/ca.crt")`），信任范围最小化。

## 4. 5000 端口从 http 直接切 https（无过渡期）

**裁决**：不做 http/https 双监听，5000 一刀切 https。理由：继续保留明文监听等于凭据与推理内容继续裸奔，本次改造白做。

### 停机说明

- 切换瞬间起，所有仍用 `http://<B机>:5000/...` 的客户端**立即失败**（TLS 握手错误 / 连接重置），属预期行为，不是故障。
- 选择维护窗口执行：更新 B 机 server 块（参考 conf 的 `listen 5000 ssl` + 四行 ssl_*）→ `nginx -t && systemctl reload nginx`。`reload` 不断存量连接，但存量 http keepalive 会话后续请求会失败。
- 回滚预案：还原旧 server 块 + reload，1 分钟内可回到明文态（仅限紧急止血，回滚后应尽快再切回）。

### 客户端 `base_url` 迁移清单

逐端检查以下位置，把 `http://` 改为 `https://`（端口仍是 5000，路径不变）：

| 客户端 | 改造点 |
|---|---|
| cc-switch 卡片 | 每个供应商卡片的 `base_url` 改为 `https://<B机>:5000/<node>/llm/v1` |
| Claude Code | `ANTHROPIC_BASE_URL` 环境变量改 `https://` |
| OpenAI / Anthropic SDK | 代码或 .env 里的 `base_url` / `api_base` 改 `https://` |
| curl / 脚本 / cron | 全量搜索 `http://` + 5000 端口，改 `https://`（或按 §3 降级加 `-k`） |
| 用量统计等内部调用 | 指向 `https://<B机>:5000/.../v1/api/usage` 的调用方同上 |

迁移完成判据：B 机执行下列命令，**不信任 CA 且不加 `-k` 的旧 http 客户端应全部失败**；信任 CA 的 https 客户端返回 401（缺凭据）即为 TLS 正常：

```bash
# 期望 401（TLS 通、凭据未带）；用 http:// 访问则应握手失败
curl -s -o /dev/null -w '%{http_code}\n' https://36.156.121.146:5000/210/llm/v1/models
```
