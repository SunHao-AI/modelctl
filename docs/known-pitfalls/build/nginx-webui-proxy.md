# nginx 反代 Web UI（管理面）

> 原始单文件已并入本文件归档：`webui-proxy-placeholder-breaks-nginx-t.md`、
> `webui-localhost-bind-502.md`（2026-09-04 聚合）

## 模板占位符原样上传导致 `nginx -t` 直接 emerg

**现象**

```
nginx: [emerg] invalid parameter "<办公网段>" in /etc/nginx/conf.d/modelctl-webui.conf:46
nginx: configuration file /etc/nginx/nginx.conf test failed
```

**根因**

参考配置里用尖括号占位（`allow <办公网段>;`）提示"此处需替换"，但 nginx 解析器不认约定——
`<` 直接当指令参数解析，`nginx -t` 硬失败。更糟的是 reload 被 `&&` 短路，旧配置仍在跑，
容易误判"配置没生效"。

**解决方案**

- 仓库内 example 一律**不留尖括号占位**：给出「改后能通过 `nginx -t` 的合法默认值」+ 紧邻
  `# 【必改】` 注释说明怎么改。已在 `docs/nginx/webui.example.conf` 落实。
- 剩余必改项只有证书路径（文件不存在报 `cannot load certificate`），已附自签命令。

**易混淆点：`allow` / `deny` 命中的是客户端 IP**

`$remote_addr` 是**访问 node1 的浏览器/VPN 出口 IP**，不是后端 `192.168.77.x`。
只写 `allow 192.168.77.0/24;` 时，外部网络访问会被 `deny all` 拦成 **403**——
与 502（后端不可达）是两类故障，排查方向不同。

## 证书路径未替换：`cannot load certificate ... BIO_new_file() failed`

**现象**

```
nginx: [emerg] cannot load certificate "/etc/nginx/ssl/modelctl.crt": BIO_new_file() failed
       (SSL: error:80000002:system library::No such file or directory:calling fopen(...))
```

**根因**

example 里的 `ssl_certificate` 路径是模板值，文件根本不存在。`listen 443 ssl` 的 server 块
在 **config 解析阶段**就要加载证书，因此 `nginx -t` 直接 emerg——属"必改项"而非配置写错。

**解决方案**

- 优先复用同机已有证书：`grep -rn ssl_certificate /etc/nginx/sites-enabled/ /etc/nginx/conf.d/`
  （数据面 :5000 若已是 https，直接抄路径，三个 server 块可共用一份）。
- 自签必须带 **SAN**：`-addext "subjectAltName=IP:<node1_ip>,DNS:localhost"`。现代浏览器
  （Chrome 58+ / Safari 自 iOS 13）**忽略 CN 只认 SAN**，只写 `-subj "/CN=..."` 的证书
  能过 `nginx -t` 却在浏览器报 `NET::ERR_CERT_COMMON_NAME_INVALID`，属"nginx 通过、浏览器失败"
  的二段式踩坑。
- 只想快速验证反代链路（SSE / SPA 深链 / WS）时，可先把三个 server 块降级为明文：
  `listen 8443;` 并删掉两行 `ssl_certificate*`，跑通后再上 TLS，把两类故障分开排。

## 管理面不能挂子路径；公网单端口时只能靠 server_name 或顶掉根路径

**约束场景**：B 机只向公网暴露一个端口（如 :5000），管理面无法另开 8443/8444。

**为什么不能挂子路径**：前端三处硬编码根路径——axios `baseURL: '/admin/api'`、
Vite 产物引用 `/assets/`、`createWebHistory()` 无 base。挂 `/208/webui/` 同时坏三处，
除非 `vite --base` + `createWebHistory('/webui/208/')` + axios 相对化三处同改，
且**每个节点的构建产物不再相同**。

**单端口下只剩两条路**

1. **按 `server_name` 加 vhost**（有域名时，零代码改动）：`listen 5000 ssl` 可重复声明，
   Host 命中新 vhost 走 Web UI，不命中落回原 `myflaskapp`。同端口同 IP → 直接复用原证书。
2. **并入原 server 块的 `location /`**（仅 IP 访问时）：一个 server 只有一个 `location /`，
   谁拿到根路径谁独占。若原块根路径已被业务占用则不可行。

**多节点 UI 共存的硬约束**：根路径唯一 ⇒ 单端口下**只能承载一个节点的 UI**。
用 `upstream modelctl_center { server 192.168.77.208:4173; }` 把中心收敛成一行，
换中心只改这一处；非中心节点保持 `WEBUI_HOST=127.0.0.1` 不暴露，攻击面更小。
真正"一个 UI 管多节点"要靠中心侧 `/admin/api` 按节点透传（集群 P2，当前未实现）。

**改 `location /` 的隐性回归**：原本兜底 502 的 `location /` 变 SPA 反代后，LLM 路由
若哪天正则配漏，不再暴露为 502 而是回一坨 HTML，OpenAI SDK 表现为莫名的 JSON 解析失败。
改完必须回归数据面 curl 矩阵（`docs/nginx/测试指南.md` §3.1），确认状态码不变。

## `/webui` 子路径访问 Web UI：nginx 单独做不到

**诉求**：`http://<公网>:5000/webui` 这种子路径入口。

**为什么不能只改 nginx**：Web UI 对外依赖三个**根级命名空间**，必须同时可达——
入口 HTML、`/assets/*`（Vite 产物绝对引用）、`/admin/api/*`（axios baseURL）。

- 只转 `/webui/` → 后端 SPA fallback 正常回 `index.html`，但页面里 `/assets/index-x.js`
  跳出前缀去根路径找；且 `createWebHistory()` 无 base，Router 拿到 `/webui/` 匹配不到
  任何路由 → **白屏**。
- 再补 `/assets/`、`/admin/api/` 两个根级 location → 资源和 API 通了，但 Router 仍按
  根部署改写地址栏为 `/models`，刷新落回原站点 → **依然不可用**。

结论：根路径三处 + Router base 必须一致，`sub_filter` 改写 HTML/JS 里的 JS 字符串字面量
要跟 gzip 打架、升级即碎，生产不可取。

**改动面（6 文件，同一概念 BASE_URL）**

| 位置 | 改动 |
|---|---|
| `web/vite.config.ts` | `base: '/webui/'`（env 注入，默认 `/` 保持兼容） |
| `web/src/router/index.ts` | `createWebHistory(import.meta.env.BASE_URL)` |
| `web/src/api/client.ts` | `baseURL` 带 base 拼接 |
| `web/src/api/tasks.ts` / `sse.ts` / `models.ts` | 三处 EventSource 绝对地址带 base |

代价：产物与前缀绑定，换前缀要重新 `npm run build`。

**易踩的细节：SSE 地址有两条独立来源**

后端多处返回 `stream_url: f"/admin/api/tasks/{id}/stream"`（admin_models/services/envs/config
共 6 处），但前端**并不消费该字段**——`openTaskStream()` 用 `taskId` 自行拼绝对路径。
所以做子路径时"后端返回的 stream_url 带不带前缀"是无关项，真正要改的是前端 3 处拼接。
反之若将来改成消费 `stream_url`，就得回头处理后端这 6 处。

## 多节点 Web UI 的两种可行形态（最终采纳三端口）

根路径唯一 ⇒ 一个 server 块只能承载一个节点的 UI。要同时管 208/209/210：

**形态 1：三端口（采纳）** `listen 5001/5002/5003` 三个 server 块各指一个节点 :4173。

- 前端与后端零改动，与数据面 :5000 完全隔离（不碰 myflaskapp 的 `location /`，
  无"LLM 配漏从 502 变返回 HTML"的回归风险）。
- 登录态天然隔离：token 存 `localStorage`（`stores/auth.ts`），按 origin（含端口）分域，
  三个标签页各管一个节点互不干扰。
- 代价：三个节点都要 `WEBUI_HOST=0.0.0.0`，公网多开两个端口，攻击面更大。

**形态 2：单中心 upstream** `upstream modelctl_center { server 192.168.77.208:4173; }`，
只有中心节点放开绑定，其余保持回环，攻击面最小；换中心改一行。想"一个 UI 管多节点"
得等中心侧 `/admin/api` 按节点透传（集群 P2，当前 `admin_cluster.py` 未实现）。

**反面形态：Cookie 定桩**（入口 `/208/` 写 `mc_node` Cookie，后续根级请求按 Cookie 分流）
—— 同域 Cookie 不区分标签页，同时开 208/209 会让 208 页面的 3s 轮询与**启停按钮打到 209**。
Web UI 能停模型、删 venv，这不是显示错乱而是真实破坏，不可用于生产。

**通用要点**

- 明文 http 上线时登录 API_KEY 明文过网：优先 `allow` 固定出口 IP，或后续 `listen ssl`
  复用 myflaskapp 证书（同机可共用，见上文 SAN 要求）。
- 新端口"连不上（超时）"与"403"要分开排：超时多为云安全组/ufw 未放行，与 nginx 无关。
- 多 server 块的 SSE/超时参数无 include 时是复制粘贴关系，调参必须逐块同步。

## Web UI 默认绑 127.0.0.1，跨机反代必 502

**根因**

`webui_host()` 默认 `127.0.0.1`（`WEBUI_HOST` 未设置时），是刻意的安全默认：管理面只有
API_KEY 一道鉴权，不默认对网开放。启动日志会明写 `Web UI：http://127.0.0.1:4173`。

**解决方案**

跨机反代前在**被反代的节点**上设 `.env`：`WEBUI_HOST=0.0.0.0` → `modelctl webui restart`，
防火墙只放行 node1 访问 4173。

**配套要点（同源问题）**

- 管理面**不能**并入数据面 `:5000` 的子路径：axios `baseURL: '/admin/api'`、产物引用
  `/assets/`、`createWebHistory()` 三处均无 base，挂 `/208/webui/` 同时坏三处。
  可用形态只有「独立 server 块 + 根路径」，而根路径在单端口下唯一（见上一节）。
- `/admin/api/` 必须 `proxy_buffering off` + 大 `proxy_read_timeout`：任务进度与模型日志是
  SSE，且 `EventSource` 无法带 header，令牌走 `?key=<API_KEY>` → 该 location 建议 `access_log off`。
- `/admin/api/ws/cluster` 需 `Upgrade` + `$connection_upgrade`；`$connection_upgrade` 若
  nginx.conf 已定义，conf.d 内重复 `map` 会报 duplicate。
- 只有中心节点需要 `WEBUI_HOST=0.0.0.0`；若后续放开更多公网端口，可改为每节点一个
  独立 server 块（独立端口）+ 根路径反代，届时各节点都要放开绑定地址。
