# profile 配置漂移（写回 / 锁文件）

> 原始单文件已并入本文件归档：`profile-auto-persist-breaks-git-pull.md`

## 下载后写回 model 路径导致 git 脏区，服务器 git pull 被挡

- **日期**：2026-09-04
- **症状**：服务器上 `modelctl start` 跑过之后 `models/<engine>/*.yaml` 出现本地改动
  （`model:` 被改成 `/raid5/...` 绝对路径 + 多出 `*.yaml.bak`），`git pull` 报
  "Your local changes to the following files would be overwritten by merge"。
- **根因**：各引擎 `pre_start` 下载完成后调用 `persist_model_path` 把**机器相关的绝对路径**
  写回被 git 跟踪的 profile YAML。写回值换台机器根本不成立，多机部署还会互相污染。
- **解决**：删除 `_persist.py` 写回机制。下载落地路径本就确定性可推导：
  `repo_local_dir = $MODEL_ROOT/<modelscope_id 最后一段>`；`download_repo` 增加
  `_is_populated` 检查（目录含 config.json/safetensors 即复用，不重复下载），
  下载后仅更新内存 `cfg["model"]`。YAML 永远保持可提交状态，多机各按自己的 MODEL_ROOT 落地。
- **要点**：
  - 运行时可推导的状态不要持久化进版本库跟踪的配置文件；"记住路径"是伪需求。
  - 仓库中历史提交（b7c62de 等）已混入 `/raid5` 绝对路径，需还原为 HF ID 或 `""`。
  - 同类漂移：`gateway/uv.lock` 被 `uv sync --project gateway` 按本机重解析改写，
    已比照 `envs/*/uv.lock` 加入 `.gitignore`（需 `git rm --cached` 一次解除跟踪）。
  - 存量脏区机器升级时需先 `git checkout -- models/ gateway/uv.lock` 再 pull 一次。

## profile 的 alias 与自动推导 name 同名，整个 profile 被静默跳过

- **日期**：2026-09-04
- **症状**：新建 `models/ollama/qwen2.5-1.5b.yaml`（`group: qwen2.5-1.5b` +
  `alias: qwen2.5-1.5b-ollama`），`modelctl list` 里查不到，`load_profile` 报
  "profile 不存在"，只在日志里躺着一行 `跳过 profile 文件 …`。
- **根因**：`name` 缺省自动推导为 `{group}-{engine}[-{variant}]`，本例恰好等于
  `qwen2.5-1.5b-ollama`；`_parse_aliases` 对 `alias == name` 抛 `ProfileError`，
  而 `list_profiles` 对单个 profile 的 ProfileError 是 **warning + continue**，
  于是整条配置消失而非报错退出。
- **解决**：ollama 家族 profile 一律不写 `alias`（现有 `ollama/*.yaml` 均如此，
  name 已足够可寻址）。需要短名路由时只给家族内的**非默认引擎成员**写 alias。
- **要点**：
  - 写 alias 前先算一遍 `{group}-{engine}[-{variant}]`，两者相同就别写。
  - 新增 profile 后必须 `load_profile(name)` 实际加载一次确认在列，不能只看文件写好了；
    `list_profiles` 只 warning 不抛错，静默跳过的排查成本极高。
  - 同家族跨引擎成员的 alias 必须**全局唯一**（网关别名表按 alias 索引），
    与另一个成员的 name 撞车同样会出问题。

## profile 默认值全按 8×48GB 数据中心卡设计，小显存单卡照抄必失败

- **日期**：2026-09-04
- **症状**：在单卡 6GB（GTX 1660 Ti / CC 7.5）机器上复用现有 profile 结构新建小模型配置，
  启动即报 `profile gpu_count=8 超过实际 GPU 数 1`；改小后又出现 KV cache OOM
  或无意义的 DSpark warning。
- **根因**：三处缺省值都以多卡大显存为前提 ——
  `llamacpp.gpu_count` 缺省 **8**（`engines/llamacpp.py`）、
  `llamacpp.dspark` 缺省 **on**、`llamacpp.ctx_size` 缺省 **1,048,576（1M）**
  （`CTX_PER_SLOT`，未显式配置时 `--ctx-size` 直接给 1M）。
  另 vllm/sglang 的 `kv_cache_dtype: fp8` 需要 CC ≥ 8.9，Turing（7.5）会被
  `fp8_quant_cc` 规则 block。
- **解决**：新增 `models/llamacpp/qwen2.5-1.5b.yaml` 与 `models/ollama/qwen2.5-1.5b.yaml`
  作为 6GB 单卡冒烟基线，显式写死 `gpu_count: 1` / `dspark: off` / `ctx_size: 8192` /
  `reasoning: off` + `reasoning_format: none`（Qwen2.5 非思考模型）/ `vision: off`（无 mmproj）。
- **要点**：
  - 小显存 profile 必须**显式覆盖每一个多卡缺省值**，别依赖"引擎会自动降级"——
    DSpark 会因剩余显存 <11GB 自动关，但会留下 warning；ctx_size 根本不会自动降。
  - `check_requirements` 里的显存预检是 `GGUF 文件大小 × 1.1 > 剩余显存` 硬失败，
    选量化时按此上界估算，别只看权重标称大小。
  - 平台边界优先于模型大小：`envs.MANAGED_ENGINES`（vllm/sglang/aphrodite/lmdeploy/
    tokenspeed/tensorrt_llm）的托管 venv **仅 Linux 可建**，Windows 上写任何 profile
    都跑不起来；Windows 只有 llamacpp / ollama / unsloth 三个非托管引擎可试。
  - 验证手段：`get_adapter(p.engine)(p, caps).check_requirements()` 配合真机 `probe()`，
    能在不下载权重、不启动引擎的前提下确认 GPU 数/显存/兼容性规则全部通过。
