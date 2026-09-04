# 构建/部署环境 · Docker 安装与国内镜像源

> 原始单文件已并入本文件归档：`docker-install-mirror.md`（2026-09-04 建档，即本主题首个条目）。
> 本主题第二个问题（daemon.json 残留停服 Hub 加速域名）直接建档于本文件，未产生独立单文件。

## download.docker.com 国内 TLS 握手失败，docker-ce 无安装候选

### 现象

部署机（Ubuntu 22.04，系统源已走清华镜像）按 docker 官方文档添加 `download.docker.com` apt 源后：

```
Ign/Err:1 https://download.docker.com/linux/ubuntu jammy InRelease
  Could not handshake: Error in the pull function. [IP: 18.65.14.45 443]
E: Package 'docker-ce' has no installation candidate
```

`apt update` 对该域名稳定失败（TLS 层被干扰，非超时），而同一机器访问清华源、
`nvidia.github.io` 均正常。**换 IP 无效**（CDN 边缘 IP 均被干扰），不是偶发网络抖动。

### 根因

`download.docker.com` 走 CloudFront，在国内部分网络被 TLS 中间干扰；而 apt 的
`Ign → Err` 序列会静默忽略该源继续跑，只有到 `apt install docker-ce` 时才暴露为
"no installation candidate"，容易误判为"源没加上/密钥问题"。

### 解决方案

换清华 `docker-ce` 镜像（与官方源同构、同签名体系，`$(VERSION_CODENAME)` 通用）：

```bash
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://mirrors.tuna.tsinghua.edu.cn/docker-ce/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
> /etc/apt/sources.list.d/docker.list
apt update && apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

modelctl 已把该脚本内置为 `modelctl env setup docker`（默认打印指引，`--run` 实际执行，
见 `src/modelctl/core/docker_setup.py`）。

### 关联陷阱

1. **`apt install docker.io docker-compose` 是过时指引**：`docker-compose`（v1）与
   `docker run --gpus` 所需的 docker-ce + containerd + nvidia-container-toolkit 组合
   不一致；`docker compose`（v2）由 `docker-compose-plugin` 提供。旧提示文案会误导用户。
2. **nvidia-ctk 在 docker 之前装会留下"半就绪"状态**：`nvidia-ctk runtime configure`
   在 docker 未装时提示 `Config file does not exist; using empty config` 并照常写
   `/etc/docker/daemon.json`，随后 `systemctl restart docker` 报
   `Unit docker.service not found`——无害，装完 docker 后再 configure + restart 即可。
3. **`registry-mirrors` 与 nvidia runtime 同存 `daemon.json`**：`nvidia-ctk runtime
   configure` 与手工写 `registry-mirrors` 都是整文件读写，互相覆盖会静默丢配置。
   `docker_setup._merge_daemon_json()` 采用"读-合并-写"保序去重，避免覆盖 `runtimes`。
4. **vLLM Day-0 大镜像（约 21.8GB）走 Docker Hub 同样受阻**：装完 docker 只是第一步，
   还需 registry-mirrors 加速。注意 **清华 TUNA / 中科大 / 网易的 Docker Hub 加速均已
   停服**（勿与仍可用的 TUNA `docker-ce` apt 镜像混淆——那是 deb 包仓库，不是 Hub 加速）。
   modelctl 内置 2026-09 实测可用多源（`docker.1ms.run` / `docker.xuanyuan.me` /
   `docker.m.daocloud.io`），`env setup docker` 默认写入；`--registry-mirror <URL>`
   可重复传以显式覆盖。

## daemon.json 残留停服 Hub 加速域名，`modelctl start` 健康检查超时

### 现象

`modelctl start qwen3.8-flash-next-vllm` 健康检查空等到超时，日志尾部只有一行
`[Errno 111] Connection refused`，真正的报错在被截断的 launch 日志里：

```
Unable to find image 'vllm/vllm-openai:qwen38-flash-next' locally
docker: Error response from daemon: failed to resolve reference
  "docker.io/vllm/vllm-openai:qwen38-flash-next":
  Head "https://docker.mirrors.tuna.tsinghua.edu.cn/v2/vllm/vllm-openai/manifests/..."
  dial tcp: lookup docker.mirrors.tuna.tsinghua.edu.cn on 127.0.0.53:53: no such host
```

### 根因

`/etc/docker/daemon.json` 的 `registry-mirrors` 仍写着**已停服的 TUNA Docker Hub
加速域名**。Docker 解析 reference 时对 mirror 域名的 DNS 失败是**硬失败**，不会静默
回落到 `registry-1.docker.io`，于是 `docker run` 直接退出，容器从未启动 —— 8110 端口
`Connection refused` 只是连带症状，与 modelctl 逻辑无关。

两个易混淆点：

1. **TUNA 的 `docker-ce` apt 镜像仍可用**，停服的只是 `docker.mirrors.tuna.*` 这个
   Hub 加速域名。同前缀 ≠ 同服务，看到 `tuna` 别急着全删。
2. **`no such host`（域名字符串都没了）≠ 超时/403**。前者说明镜像源已永久下线，
   换源是唯一解；后者才需要排查网络策略与账号鉴权。

**代码修好 ≠ 环境修好**：`docker_setup.DEFAULT_REGISTRY_MIRRORS` 早已剔除停服源，
但对**已存在**的 daemon.json 不会自动生效，必须重跑 `--run` 触发"读-合并-写"。
（早期实现的合并是**只追加不清理**，重跑 `--run` 也清不掉坏源 —— 见下一条目。）

### 解决方案

```bash
modelctl env setup docker --run      # 自动剔除停服源 + 写入内置默认多源

systemctl daemon-reload && systemctl restart docker   # 手工改文件时必须显式重启
docker info | grep -A3 "Registry Mirrors"             # 验证 daemon 真的加载了
docker pull vllm/vllm-openai:qwen38-flash-next        # 约 21.8GB，先手动拉再 start
modelctl start qwen3.8-flash-next-vllm
```

`--registry-mirror <URL>`（可重复）可显式覆盖要写入的源；命中停服域名的传参会被
忽略并告警，全部无效时回落内置默认多源。

不想跑 modelctl（例如未安装或版本较旧）时手工清理，注意保留 `runtimes`：

```bash
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("/etc/docker/daemon.json")
d = json.loads(p.read_text() or "{}")
dead = [m for m in d.get("registry-mirrors", []) if "docker.mirrors." in m]
d["registry-mirrors"] = [m for m in d["registry-mirrors"] if m not in dead] + \
    [m for m in ("https://docker.1ms.run", "https://docker.m.daocloud.io")
     if m not in d["registry-mirrors"]]
p.write_text(json.dumps(d, indent=2) + "\n")
print("removed:", dead)
PY
```

排障顺序固定为：`docker info` 看 mirror 是否生效 → `docker pull` 看能否落地 → 才看
modelctl 的健康检查与端口，避免在应用层反复兜圈。

### 关联陷阱

1. **Day-0 专用 tag 镜像站内可能没有**：`qwen38-flash-next` 是模型专用 tag，不在常规
   同步白名单内，换源后若报 `manifest unknown`，走反代显式拉取再回打 tag：

   ```bash
   docker pull docker.m.daocloud.io/vllm/vllm-openai:qwen38-flash-next
   docker tag docker.m.daocloud.io/vllm/vllm-openai:qwen38-flash-next \
              vllm/vllm-openai:qwen38-flash-next
   ```

2. **镜像拉通后紧接着核对模型目录**：vLLM 走 HF 格式权重，与 GGUF 目录不通用。若
   profile 的 `model:` 指向不存在的目录，会在容器起来后触发上百 GB 的全量重下，表现
   为"换了源还是起不来"，容易被误判成镜像问题。

## `--run` 的 daemon.json 合并只追加不清理，坏源永久残留

### 现象

按上一条目重跑 `modelctl env setup docker --run --registry-mirror https://docker.1ms.run`，
命令退出码 0、日志提示"registry-mirrors 已合并写入"，但 `docker info` 里那个停服的
`docker.mirrors.tuna.tsinghua.edu.cn` **依然在列**，`docker run` 照旧 DNS 硬失败。

### 根因

早期 `_merge_daemon_json()` 的语义是"读-合并-写 + 保序去重"，对既有 mirror 一律
**原样保留**，只做追加。这套语义对"新增加速源"是对的，但对**已下线**的源就失效了：
坏域名排在列表首位，Docker 逐个尝试 mirror 时第一个就 DNS 失败退出，后面新追加的
好源根本没机会生效 —— 看起来"改了没用"。

### 解决方案

`docker_setup` 增加停服源清单与剔除逻辑，让 `--run` 成为真正**幂等收敛**的操作：

```python
DEAD_REGISTRY_MIRRORS = (  # 子串匹配，勿写裸 "tuna"/"ustc"（apt 镜像仍可用）
    "docker.mirrors.tuna.tsinghua.edu.cn",
    "docker.mirrors.ustc.edu.cn",
    "hub-mirror.c.163.com",
    "mirror.baidubce.com",
)

existing, dead = split_dead_mirrors(list(data.get("registry-mirrors") or []))
changed = bool(dead)          # 仅"删除坏源"也算有变更，必须写盘
```

`resolve_registry_mirrors()` 同步过滤：用户显式传停服源时告警并忽略，全部无效则回落
内置默认，避免把坏源又写回去。

### 关联陷阱

1. **停服清单必须用完整域名子串**：`tuna` / `ustc` 这类裸前缀会连带命中
   `mirrors.tuna.tsinghua.edu.cn/docker-ce`（仍可用的 apt 仓库），误删代价远大于漏删。
2. **"只追加"类合并函数要区分"保留用户意图"与"保留已知错误"**：环境收敛类工具里，
   对已证实下线的配置项应主动清理并告警，否则用户重跑命令也无法脱离坏状态。
3. **变更判定要把删除计入 `changed`**：只在有新增时置真的话，"仅需清理坏源"的场景会
   直接 `return False` 不落盘。

## `docker pull` 大镜像中途 `short read … unexpected EOF`，加镜像源无效

### 现象

清掉停服源、`docker info` 已正确列出 mirror 之后，拉 21.8GB 的 Day-0 镜像仍反复失败：

```
docker pull vllm/vllm-openai:qwen38-flash-next
qwen38-flash-next: Pulling from vllm/vllm-openai
short read: expected 35254 bytes but got 0: unexpected EOF

docker pull vllm/vllm-openai:qwen38-flash-next
c18ed7025646: Download complete
...（十几个 Download complete）
a5881cf32fc4: Downloading [>] 1.049MB/61.57MB
short read: expected 201 bytes but got 0: unexpected EOF
```

直觉反应是"再加几个镜像源提高成功率"，于是往 `registry-mirrors` 里塞
`dockerproxy.com` / `docker.nju.edu.cn` / `registry.docker-cn.com` —— 加完照旧失败。

### 根因

两个独立误判叠在一起：

1. **失败性质是传输中断，不是找不到源**。manifest 与多个 layer 已经下载完成，说明
   mirror 可达、repo 存在，是跨境链路在传大 layer（本例最大层 **4.32 GB**）时被掐断。
2. **`registry-mirrors` 是 failover 列表，不是负载均衡**。Docker 按顺序取第一个可用
   mirror 走完整流程，2 个源和 5 个源在带宽上没有区别，加源对中断毫无帮助。

更糟的是提议的几个源本身已死：`dockerproxy.com` / `registry.docker-cn.com` TCP+TLS
全不通；`docker.nju.edu.cn` 的 `/v2/` 返 403、manifest 404（南大仅剩 gcr/ghcr/quay/nvcr，
Docker Hub 加速已下架）。写进去等于把上一条目的 DNS 硬失败重新制造一遍。

### 解决方案

**利用 Docker 复用已完成 layer 的特性做重试**：每次 `docker pull` 都从上次断点继续
推进（日志里第二次的十几个 `Download complete` 就是证据），是单调可重入过程。

`core.docker_setup.ensure_image()` 显式 pull + 按错误类型决定是否重试：

```python
PULL_ATTEMPTS = 5          # 中断类：重试（layer 复用，每次都在推进）
DEAD_MIRROR_MARKERS = ("no such host", "dial tcp: lookup")   # 硬失败：立即退出
MISSING_TAG_MARKERS = ("manifest unknown", "manifest invalid")  # 换反代，重试无意义
```

三类错误必须分开处置 —— 对 DNS 失败重试 5 次只是浪费 5 倍时间。三个 docker 型引擎
（vllm / tokenspeed / tensorrt_llm）在 `pre_start()` 里调用它，失败抛 `RequirementError`。

**降低并发数缓解中断**：`env setup docker --run` 会把 daemon 级
`max-concurrent-downloads`（出厂默认 3）收敛到 2 —— 3 路 GB 级 layer 互抢同一条跨境
链路会拉长单层耗时，放大被按空闲/时长掐断的概率；调小让每层更快传完。
`--max-concurrent-downloads N` 覆盖，`0` 表示保留机器现值。

顺带解决可观测性：走 `docker run` 隐式 pull 时，报错会被 launch 日志截断规则吃掉，
只剩健康检查超时的 `Connection refused`；显式 pull 让真实原因直接进 modelctl 日志。

### 关联陷阱

1. **别把 `registry-mirrors` 当带宽池**。它是有序 failover，只有"这个源不可用"才会
   轮到下一个；源在可用但慢/不稳时，后面的源永远轮不到。
2. **判断 mirror 死活要探 `/v2/<ns>/<repo>/manifests/<tag>`，而不是首页**。很多站首页
   返回 200 却已不代理 Docker Hub。`curl /v2/` 返回 **401 是健康信号**（正常要 token），
   `000` 才是 TCP/TLS 不通。
3. **本机探测有环境偏差**。办公网/家宽可达 ≠ 部署机房可达，反之亦然。改默认源前先
   `docker pull` 实测，别只信网上"2026 最新可用列表"——那些清单更新滞后且互相抄。
4. **`max-concurrent-downloads` 是 daemon 配置，不是 `docker pull` 参数**。`pull` 只有
   `--all-tags/--platform/--quiet`；写成 `docker pull --max-concurrent-downloads 2` 会报
   `unknown flag`。必须写 `/etc/docker/daemon.json` 并 `systemctl restart docker` 才生效。
   **`docker info` 不打印这个键**（2026-09 实测无 `Concurrent` 行），别按验证
   `Registry Mirrors` 那样去 grep 它，直接核对 daemon.json 文件内容。
