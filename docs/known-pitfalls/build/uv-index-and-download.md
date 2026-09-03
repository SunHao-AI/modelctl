# 构建 / 依赖

> 原始单文件已并入本文件归档，保留溯源信息。

## uv 的 `default = true` 会让镜像源变成最低优先级（镜像形同未配置）

**日期**：2026-09-03
**分类**：构建 / 依赖
**一句话描述**：给 `[[index]]` 加 `default = true` 不是"设为主源"，而是把它移到优先级列表末尾，解析流量会全部落到 `files.pythonhosted.org`。

### 症状

- `modelctl env setup vllm` 极慢，几十个大 wheel 进度条速度**整齐划一**（均约 150–250 KiB/s）。
- 偶发直接失败：

```
× Failed to download `z3-solver==4.15.4.0`
├─▶ Request failed after 4 retries in 237.6s
├─▶ Failed to fetch:
│   `https://files.pythonhosted.org/packages/21/c9/.../z3_solver-4.15.4.0-...whl`
╰─▶ operation timed out
```

URL 主机是 `files.pythonhosted.org` —— 说明**根本没走国内镜像**。

### 根因

uv 的 index 优先级规则与直觉相反：

1. **先声明的 index 优先级最高**（first-match：命中第一个含该包的 index 即停止）。
2. `default = true` 的含义是"该 index 取代 PyPI 成为**兜底**源"，官方文档原文：
   > If an index is marked as `default = true`, it will be **moved to the end of the prioritized list**, such that it is given the **lowest priority**.

因此下面这份"看起来设了阿里源"的配置，实际优先级是 `pypi.org > aliyun`：

```toml
# 错误：aliyun 被 default=true 降到最后，pypi.org 才是第一优先级
[[index]]
url = "https://mirrors.aliyun.com/pypi/simple/"
default = true

[[index]]
url = "https://pypi.org/simple/"
```

叠加第二个坑 —— **uv 一旦发现 `pyproject.toml` 里有 `[tool.uv]` 表就停止向上查找配置**。所以 `envs/vllm/pyproject.toml` 为声明 `pytorch-cu13` 而写了 `[tool.uv]`，导致根 `uv.toml` 整份失效：

```toml
[tool.uv]
[[tool.uv.index]]          # 有这一段 → 根 uv.toml 被完全忽略
name = "pytorch-cu13"
explicit = true
```

两个 bug 相乘：既丢了根配置，子项目自己的 `default = true` 又排到最后 → 全量走官方源。

### 判别方法（比读文档可靠）

`uv.lock` 里每个包都记录实际来源，直接统计：

```powershell
Select-String -Path envs\vllm\uv.lock -Pattern 'registry = "([^"]+)"' -AllMatches |
  ForEach-Object { $_.Matches } | ForEach-Object { $_.Groups[1].Value } |
  Group-Object | Sort-Object Count -Descending
```

修复前：`https://pypi.org/simple/` × 17；修复后：`https://pypi.tuna.tsinghua.edu.cn/simple` × 252。

### 解决方案

**一、根 `uv.toml` 按优先级从高到低声明，且不给镜像加 `default = true`**：

```toml
[[index]]
name = "tuna"
url = "https://pypi.tuna.tsinghua.edu.cn/simple"   # 第一优先级

[[index]]
name = "aliyun"
url = "https://mirrors.aliyun.com/pypi/simple"     # 兜底 1

[[index]]
url = "https://pypi.org/simple/"                   # 兜底 2

[[index]]
name = "pytorch-cu13"
url = "https://download.pytorch.org/whl/cu130"
explicit = true
```

**二、`envs/*` 子项目一律不写 `[tool.uv]`，从而继承根 `uv.toml`**（同时消除 6 份重复配置的漂移风险）：

```toml
[project]
name = "modelctl-venv-vllm"
dependencies = ["vllm>=0.27,<0.28"]
# 刻意不写 [tool.uv]：写了就丢失根配置的镜像优先级
```

### 衍生陷阱 1：`explicit = true` 的 index 若无人引用就是死配置

`pytorch-cu13` 声明为 `explicit = true`，但全仓库没有任何 `[tool.uv.sources]` 指向它：

```toml
# 缺这一段，上面的 explicit index 永远不会被使用
[tool.uv.sources]
torch = { index = "pytorch-cu13" }
```

`explicit` 的语义是"只有被 `[tool.uv.sources]` 显式路由的依赖才走这个 index"。没有引用 → 该 index **一次都不会被访问**，且 uv **不会警告**。

**⚠ 但"死配置"不等于"装错了"——不要凭"index 没生效"就推断产物有误。**

本项目一度据此断言"torch 装成了 cu128"，实测 `uv.lock` 后证明是错的：

```
torch 2.13.0
  ├─ cuda-toolkit           13.0.3.0
  ├─ nvidia-cudnn-cu13      9.20.0.48
  ├─ nvidia-nccl-cu13       2.29.7
  ├─ nvidia-cusparselt-cu13
  └─ nvidia-nvshmem-cu13
```

torch 自 2.9 起 **PyPI 默认 Linux wheel 已是 CUDA 13**，"来自 PyPI 源"≠"cu12x"。正确结论是：该 index 是**无意义的死配置**，而非**错误的配置**。

**教训：判断"装了什么"必须读 `uv.lock` 的实际依赖树，不能从 index 配置反推。**

最终处理是删除该死配置（而非补 `[tool.uv.sources]`），因为 `download.pytorch.org` 无国内镜像同步，指向它会把 526 MB 的 torch wheel 变成跨境下载，与加速目标相悖。

CUDA 构建的正确性改由**启动前校验**保证（`compat_rules.torch_cuda_build`）：torch 的 CUDA 小版本取决于 vllm 钉死的 torch 版本 + 所选 index，且 PyPI 默认 wheel 的 CUDA 构建会随 torch 版本漂移，安装期无法保证，只能在启动前显式检查。

### 衍生陷阱 2：`UV_CACHE_DIR` 落在全盘导致每次重新下载

uv 缓存默认 `~/.cache/uv`。vLLM 引擎 wheel 约 3.7 GB，若 `~` 是空间紧张的系统盘，缓存写不下时 uv **不报错**、静默不缓存，于是每次 `env setup` 重新下载全部 wheel。已在 `.env.example` 固化：

```bash
UV_CACHE_DIR=/raid5/sh/cache/uv
UV_HTTP_TIMEOUT=120
```

`load_env()` 会把 `.env` 注入 `os.environ`，`envs.setup()` 再 `**os.environ` 传给 uv 子进程，uv 原生识别 `UV_*` 变量，无需额外代码。

### 关于"并发不够"的误判

现象容易误判为"没用多线程"。实际上 uv 默认 `concurrent-downloads = 50`，图中几十行进度条同时跳动**正是 50 路并发在工作**。

**每个包速度都几乎相同，恰恰说明它们在均分同一条总管道带宽**，而非缺少并发。单连接下载器会呈现快慢参差，不会这么整齐。

总吞吐 ≈ 250 KiB/s × 50 ≈ 12 MiB/s 才是真实瓶颈。调 `UV_CONCURRENT_DOWNLOADS` 无效（官方 issue #5073 实测 8/16/32/50 几乎无差别）。后期变慢是因为小包下完后并发数从 50 塌到 3–5 路，而剩余 cublas 403 MiB + torch 502 MiB + cudnn 349 MiB 约 3.5 GB 占了总量的 95%。

### 相关镜像实测（开发机侧，单连接 Range 拉 16 MiB）

| 镜像 | 速度 | 备注 |
|---|---|---|
| `pypi.tuna.tsinghua.edu.cn` | 2.0 MiB/s | 同步最全，选为主源 |
| `mirrors.aliyun.com` | 1.1 MiB/s | 作兜底 |
| `mirrors.ustc.edu.cn` | 0.4 MiB/s | 未采用 |

### 弱网/内网绕开下载

`modelctl env setup <target> --wheels <DIR> [--offline]`：透传 `uv sync --find-links <DIR>`，`--offline` 再加 `--offline` 完全禁网。在有网机器 `pip download -r <(uv export ...)` 备好目录后拷入即可。
