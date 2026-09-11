# vLLM KV Offload / KV Connector 支持边界

> 2026-09-10：沉淀 `recipes.vllm.ai/Qwen/Qwen3.8-Flash-Next` 页面 "KV Offload → Mooncake (Distributed)" 选项在本仓硬件/模型上的适用性评估。结论是**不可用**，并顺带钉住"引擎参数能力不能靠 recipe 页面推断"这条通用教训。
>
> 原始单文件已并入本文件归档。

## 混合注意力（GDN/QSA/Mamba）模型上 KV connector 全线不可用

**背景**：vLLM 官方 recipe 页面为 Qwen3.8-Flash-Next 提供 `KV Offload` 下拉项（`Off / SimpleOffloading / LMCache / Mooncake`），URL 形如 `?kv_offload=kv_store_distributed_mooncake`；配套文档 [mooncake_store_connector_usage](https://docs.vllm.ai/en/stable/features/mooncake_store_connector_usage/) 详述了 CPU-DRAM 池 + 跨实例 prefix 共享。看起来是"显卡不够时用内存补 KV"的现成方案。

**症状（若照抄启用 `--kv-transfer-config`）**：引擎启动即崩，且报错点离真实原因很远：

- `MooncakeConnector` / `MooncakeStoreConnector`：connector 用 `get_current_attn_backend(vllm_config)` 从**第一层**取 attention backend。混合模型首层是 `linear_attention` → 拿到 `GDNAttentionBackend`，再被塞进 `TransferTopology(is_mamba=False, attn_backends=[GDNAttentionBackend])`；轮到 `FullAttentionSpec` 组时调 `attn_backend.get_kv_cache_shape(...)`——SSM backend 没有标准 KV cache shape，直接抛错。
- `NixlConnector`：能识别 `MambaSpec` 走进 SSM conv 传输分支，设 `VLLM_SSM_CONV_STATE_LAYOUT=DS` 后又撞
  `NotImplementedError: 3-read conv transfer only supports Mamba2 models, got mamba_type='gdn_attention'`。
- 关掉 hybrid KV cache manager 时另有前置报错：
  `ValueError: Hybrid KV cache manager is disabled but failed to convert the KV cache specs to one unified type.`

**根因**：connector 的 transfer topology 是**按单一 attention backend 建模**的，而混合模型有 N 个 cache group（`MambaSpec/gdn_attention` × 多组 + `FullAttentionSpec` × 1 组），需要逐组选 backend。上游尚未实现（vllm#43765 feature request、vllm#41860 bug、vllm-ascend#9246/#9057 的 KV pool 支持仅在 roadmap 阶段）。

**解决方案**：本仓模型（`qwen3.8-flash-next` = 36 层 GDN + 12 层 QSA）**不加** `--kv-transfer-config`；PD 分离（`MooncakeConnector` + `MultiConnector`）同样排除。已在 profile 里以硬约束注释形式落档。

**判据（可复用的通用规则）**：只要模型的 KV cache spec 不是**单一** `FullAttentionSpec`，就不要指望任何 KV connector 能用。快速自检：

```bash
# 看 cache group 划分；出现 MambaSpec / gdn_attention / linear_attention 即不可用
grep -E 'layer_types|linear_attention|full_attention' <model_dir>/config.json
# 启动日志里搜这几行也是同一信号
grep -E 'Mamba cache mode|Hybrid KV cache manager' logs/launch-*.log
```

## recipe 页面的可选项 ≠ 官方推荐配方

**症状**：recipe URL 上带了 `kv_offload=kv_store_distributed_mooncake`，容易被当成"这个模型的标准部署方式"。

**根因**：recipe 页面是**参数化配置生成器**，URL query（`variant` / `hardware` / `kv_offload` / `strategy`）只是把可选下拉项的当前选中值回显到 URL 上。该页正文给出的三条推荐命令（4×GB300、8×H200、4×H100）**均未启用 Mooncake**；Mooncake 一节明确写着面向 "2+ instances + cache-aware vllm-router"。

**解决方案**：以 recipe **正文的命令块**为准，不以 URL query 或下拉项为准；引入任何引擎参数前先确认它对应的前置条件（进程、网络、内存、实例数）本环境是否满足。

## Mooncake 的容量单位是 per GPU，不是 per node

**症状**：按"节点内存 − 若干 GB"估 `global_segment_size`，结果 8 rank 直接把宿主内存吃穿。

**根因**：`global_segment_size` 是**每个 rank（每 GPU）**贡献给分布式池的 CPU 内存，官方 recipe 在 B300 上给的值是 `100GB`。8 卡即 800GB DRAM 量级，与"整机内存"完全不是一回事。`local_buffer_size` 同为 per GPU。

**解决方案 / 本仓判据**：本机 128GB 系统内存（见 `docs/rtx5880-optimization-guide.md`），扣除 OS + modelctl + 网关 + 模型下载缓存后可用余量不足，8 rank 连 `8GB/rank`（64GB）都勉强 → 直接否决，无需再试。另注意 KV offload 的收益前提是跨节点 `protocol: rdma`；本仓只有 PCIe 4.0 无 NVLink、无 RDMA，只能走 `tcp`，搬运 KV 未必快过重算 prefill。

## 跨实例共享 prefix cache 必须固定 PYTHONHASHSEED（除 Mooncake 外同样适用）

**症状**：多实例指向同一 KV 池，同前缀请求永远不命中，池子里全是重复条目。

**根因**：block hash 链从 `NONE_HASH` 起算。默认算法下 `NONE_HASH` 由固定种子派生，跨进程可复现；但 `--prefix-caching-hash-algo` 取非加密的 `xxhash` / `xxhash_cbor` 时，`NONE_HASH` 是**每进程随机**的，跨进程块哈希必然不同。

**解决方案**：所有共享同一缓存池的 vLLM 进程（DP rank、独立的 prefill/decode 节点）显式设成同一个 `PYTHONHASHSEED`。本仓当前是"每 profile 单实例"，暂不受影响；但将来若在 group 内做多副本 + 共享缓存（无论 LMCache 还是 Mooncake），这就是必修项。

**相关**：同池不同部署还要用 `kv_connector_extra_config.cache_prefix`（或 Mooncake 的 `tenant_id`）做命名空间隔离，否则同 master 下的不同模型互相污染。

## VLLM_PLE_CPU_OFFLOAD 与 KV offload 是两回事，勿混为一谈

**症状**：看到"CPU offload"就以为同属 KV connector 体系，进而因为"GDN 不支持 connector"而错判 PLE offload 也不能用。

**根因**：两者卸载对象完全不同——

| | 卸载对象 | 载体 | 受 GDN 限制 |
|---|---|---|---|
| `--kv-transfer-config` + Mooncake | KV cache block | 需 `mooncake_master` 外部进程 + JSON 配置 + RDMA | **是** |
| `VLLM_PLE_CPU_OFFLOAD=1` | N-gram Embedding（PLE）查表权重 | 纯环境变量，宿主 RAM + 异步预取 | **否** |

**解决方案 / 落地**：Qwen3.8-Flash-Next 的 51B N-gram Embedding 表在 80GB 级及以下卡的 TP/TEP 部署上是**必须项**（官方在 4×H100 配方里自动开启，H200 以上才"可选"）。本仓 8×RTX 5880（48GB/卡）属于必须侧，它也是 profile 阶段固定显存开销的主要来源之一（实测每卡可用 KV 仅 2.54 GiB，逼得 `max_model_len` 从 262144 降到 131072）。docker 路径经 yaml `vllm.docker_env` 注入即可，前置是宿主 ≥51GB + 运行时余量。
