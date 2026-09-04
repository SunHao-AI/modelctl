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

### 解决方案

```bash
modelctl env setup docker --run \
  --registry-mirror https://docker.1ms.run \
  --registry-mirror https://docker.m.daocloud.io

systemctl daemon-reload && systemctl restart docker   # 手工改文件时必须显式重启
docker info | grep -A3 "Registry Mirrors"             # 验证 daemon 真的加载了
docker pull vllm/vllm-openai:qwen38-flash-next        # 约 21.8GB，先手动拉再 start
modelctl start qwen3.8-flash-next-vllm
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
