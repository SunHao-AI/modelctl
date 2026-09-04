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

## 同名 profile 散落在多个引擎子目录，中心按排序首个"猜"引擎会把模型下发到错误引擎

- **日期**：2026-09-04
- **症状**：`cluster/profiles.py`（goal 下发源）对 `models/<engine>/<name>.yaml` 取
  `rglob` 排序后的**第一个**命中即返回 engine。本仓 `models/*/qwen3.8.yaml` 有 8 份
  （aphrodite/llamacpp/lmdeploy/ollama/sglang/tensorrt_llm/unsloth/vllm），
  `read_profile_source("qwen3.8")` 静默返回 `aphrodite`。
- **根因**：engine 决定 worker 侧写进哪个引擎子目录、由哪个引擎启动，是下发内容的组成部分，
  却按"查找顺序"而非"唯一性"决定。`find_profile_path` 与 `read_profile_source` 各写一份
  查找/校验逻辑，任一侧改规则即静默漂移（Task 5 用 read、worker 侧用 find 会各判一次）。
- **解决**：抽出 `_resolve()` 作为唯一定位入口，`find_profile_path` / `read_profile_source`
  共用；可判定 engine 的命中若落在**多个不同引擎** → 返回 `存在歧义：<相对路径列表>` 拒发，
  调用方用新增的 `engine=` 参数（缺省 None，签名向后兼容）显式选边；同引擎重名仍根目录优先，
  与 `core.profile.load_profile` 一致。engine 不可判定的同名文件不参与歧义判定（本就该忽略）。
- **要点**：
  - 下发/写盘路径的任何成分都不许"猜"，宁拒勿猜：猜错的模型会在错误引擎上启动，比报错难查一个量级。
  - "根目录优先 + 递归排序首个"这套顺序只在**单机本地读**时安全（读谁都行）；跨机下发语义变了。
  - `core.webui.admin_models` 的 `/{name}/yaml` 同样是"首个命中"，但它只回显文本、不决定引擎，可保留。

## cluster 寻址只认文件名，用户拿 UI 展示名下发必 404

- **日期**：2026-09-04
- **症状**：UI/网关展示的是 `core.profile` 自动推导的展示名（`{group}-{engine}[-{variant}]`，
  如 `qwen3.8-vllm`），而 `cluster/profiles.py` 只按**文件名**（`qwen3.8`）定位。
  用户照着 UI 抄名字执行 `goal set qwen3.8-vllm` → "profile 不存在"。
- **根因**：一个 profile 存在两种"名字"，展示名不是路径成分。若反过来让 goal 直接以
  展示名寻址并写盘，中心 `models/vllm/qwen3.8.yaml` 与 worker `models/vllm/qwen3.8-vllm.yaml`
  路径不同 → 同内容不同镜像，`load_profile` 的 group 推导也随之漂移，sha 漂移检测失真。
- **解决**（用户裁决 2026-09-04 条款④）：寻址双兼容但**归一化到文件名**——文件名候选
  全部未命中才做展示名回退（全目录扫描 `*.yaml` 过同套 `_load_candidate` 校验，按
  `display_name_of()` 匹配，该函数复用 `_resolve_group` 保证与 `_to_profile` 同口径）；
  文件名命中优先；展示名多引擎命中沿用歧义规则。成功返回 `name` = 文件 stem（goal/写盘
  唯一用名），`display_name` 仅回显且与 name 相同时省略。
- **要点**：
  - 双名字体系下，**外部可寻址、内部必须归一**：下发/写盘/漂移检测只认规范名，展示名只进不回。
  - 展示名推导逻辑禁止复制第二份，import `_resolve_group` 单点维护，否则 CLI 可寻址名与
    UI 展示名再次分裂。
  - 回退扫描对未通过校验的文件静默跳过（其报错与本次寻址无关），但**绝不放宽校验**——
    展示名指向坏文件按"未命中"处理。

## 中心只校验"有没有 port"，坏 port 一路下发到引擎启动才炸

- **日期**：2026-09-04
- **症状**：`port: notanumber` / `port: 0` / `port: 99999` 都能通过 `read_profile_source`
  返回 `ok=True`，goal 正常下发；worker 侧 `sync` 也只判 `raw.get("port") in (None, "")`，
  坏值原样落盘，直到引擎启动或 `load_profile` 才报 `port 必须在 1-65535`。
- **根因**：中心用了比 `core.profile._to_profile`（`int()` 可转 + 1-65535）宽松得多的口径，
  而双侧唯一的公共校验是"存在性"——错误被推到链路最末端，且已污染 worker 磁盘。
- **解决**：新增 `_port_reason()`，与 `core.profile` 同口径（int 可转、1-65535），
  在中心就返回 `ok=False` 并把原因带进 gate 报告。
- **要点**：
  - 双侧纵深防御必须是**同一口径**，只在下游客校验等于把校验形同虚设。
  - `port: "8101"`（YAML 字符串）`int()` 可转，`core.profile` 放行，中心也不能误拒。

## 非 UTF-8 profile 让"绝不抛异常"契约破功，且超尺寸文件先整份读进内存

- **日期**：2026-09-04
- **症状**：UTF-16 / 二进制残留的 YAML 使 `read_profile_source` 抛 `UnicodeDecodeError`
  （实测 0xff 开头即触发），沿调用链冒到 Task 5 的 `goal set` → REST 500。
- **根因**：`UnicodeDecodeError` 是 `ValueError` 子类，不在 `except OSError` / `except yaml.YAMLError`
  范围内；尺寸上限又用 `len(text.encode())` 判定，等于先把整份文件读进内存才拒绝。
- **解决**：`_load_candidate()` 显式 `except UnicodeDecodeError`；上限改用 `path.stat().st_size`
  在**读盘前**判定（测试用 monkeypatch 把 `Path.read_text` 换成 `raise` 钉死"不读盘"）。
- **要点**：
  - 声明"绝不抛异常"的函数，兜底要按异常**继承树**核对，不能按"想到的那几种"列举。
  - 任何"超限即拒"都要在 IO 之前用元数据判定，读进来再拒只挡住语义、挡不住资源。

## 展示名回退命中的文件 stem 未过写盘白名单，中心放行 worker 必拒

- **日期**：2026-09-04（fix round 2）
- **症状**：`models/vllm/千问.yaml` 写 `name: ascii-name` 后，`read_profile_source("ascii-name")`
  返回 `ok=True, name="千问"`；`.hidden.yaml` 同理。goal 下发到 worker 后被 Task 8 的
  `_validate`（`is_safe_name(profile)`）拒绝落盘，goal 永远停在拒收态且不报错回中心。
- **根因**：条款④的展示名回退只保证候选 YAML 过 `_load_candidate` 校验，归一出的
  `path.stem` 却没重新过白名单——展示名（YAML 显式 `name:`）允许任意字符串，文件名却
  是 worker 写盘路径成分。与 round 1 的坏 port 同属"中心放行、worker 必拒"。
- **解决**：回退路径命中后对 `path.stem` 再过一次 `is_safe_name`；不合格候选出局并在
  reason 点名文件与 stem；同展示名的安全候选不受拖累（否则又造一种 404）。
- **要点**：
  - 双名字体系中"入口宽、出口严"：外部寻址名可以宽松，但凡是**路径成分**的归一结果
    必须回到出口侧的同一白名单复核。
  - 拒收 reason 必须点名是哪个文件/哪个 stem，"不存在"会把用户引向错误排查方向。

## rglob 的三重坑：重复命中根目录、匹配目录、重复遍历

- **日期**：2026-09-04（fix round 2）
- **症状**：根目录 `models/qwen.yaml` + `models/sglang/qwen.yaml` 触发歧义拒发时，
  reason 里候选清单为 `qwen.yaml, qwen.yaml, sglang\qwen.yaml`——同一文件列两遍，
  误导用户以为还有第二个候选要选边；且文件名与展示名两次寻址各遍历一遍全目录。
- **根因**：`Path.rglob("*.yaml")` 的 `**` 匹配**零层目录**，根目录那份同时出现在
  "根候选"与递归结果里；`rglob` 还匹配**目录**（手建 `vllm/x.yaml/` 即命中）；
  条款④引入回退后同一请求遍历两次目录。
- **解决**：抽出 `_scan(root)` 单一遍历点——`sorted({p for p in root.rglob("*.yaml") if p.is_file()})`，
  集合去重 + 目录过滤，文件名/展示名两种寻址共用；文件名候选用**稳定二次排序**恢复
  "根目录优先 + 路径序"（`sort(key=lambda p: p.parent != root)`，不得依赖 set 迭代序）。
- **要点**：
  - 测试钉死手段：monkeypatch `Path.rglob` 记录 pattern 调用序列（钉"一次读取一次遍历"），
    断言歧义 reason 括号内候选计数（钉"去重"）。
  - "根候选 + rglob"这种手工拼接候选的写法必然踩零层目录重复，优先"一次全扫 + 过滤"。

## 中心对显式 `engine: VLLM` 替它 lower，本地 load_profile 却按未知引擎硬失败

- **日期**：2026-09-04（fix round 3）
- **症状**：`models/vllm/a.yaml` 写 `engine: VLLM`，中心 `read_profile_source("a")` 返回
  `ok=True, engine="vllm"` 并正常下发；worker 落盘后 `load_profile("a")` 报
  `a.yaml：未知引擎 'VLLM'（支持：[...]）`，goal 永不收敛。
- **根因**：中心写了 `explicit.strip().lower()`，而 `core.profile._resolve_engine` 对
  **显式值不做 lower**（只对父目录名 lower）。这是 round 1 坏 port、round 2 不安全 stem
  的同族问题：中心比 worker 的权威口径更宽松，错误被推到落盘之后才暴露。
- **解决**：`_load_candidate()` 里显式值只 `strip()` 不 `lower()`（目录名推断仍 lower，
  与 `_resolve_engine` 完全一致）；`engine.lower() in KNOWN_ENGINES` 时在 reason 追加
  "engine 值大小写敏感，请用其小写形式"。反向口径同样要防：`models/VLLM/` 这类大写目录
  本地能跑，中心必须照样放行。
- **要点**：
  - "同口径"要逐字段核对到**被 import 的权威实现**，别顺手加一次归一化；`lower()` 这种
    看似无害的宽容，在"中心校验 + 下游按原文执行"的链路里就是放行下游必拒的内容。
  - 用例用 `pytest.raises(ProfileError)` + `load_profile()` 做**双证**：中心与本地对同一
    文件的结论必须一致，单向断言挡不住口径再次分叉。

## 展示名回退只按路径序取首个，根目录那份被子目录抢先 → 中心与 worker 指向不同文件

- **日期**：2026-09-04（fix round 3）
- **症状**：`models/misc.yaml`（`name: shared`, port 9）+ `models/aaa/other.yaml`
  （`name: shared`, port 1）时，中心 `read_profile_source("shared")` 返回 `name="other"`
  （子目录那份），而 worker/本地 `load_profile("shared")` 走 `list_profiles` 的**根目录优先**，
  解析到 `misc.yaml`。两侧"同一个 profile"其实是两个文件，sha 漂移检测彻底失真。
- **根因**：文件名寻址做了"根目录优先"的稳定二次排序，展示名回退却直接用 `_scan` 的
  纯路径序取首个命中。路径序比的是分隔符后的首个字符（`aaa/other.yaml` < `misc.yaml`），
  **根目录那份不保证排在前面**。
- **解决**：抽出 `_root_first(paths, root)`（`sorted(paths, key=lambda p: p.parent != root)`，
  稳定排序），文件名与展示名两条寻址路径共用，与 `core.profile.load_profile` /
  `list_profiles` 的"根目录优先"三点一致。
- **要点**：
  - 同一份候选集**每新增一条寻址路径，都要重新确认优先级规则**；"根目录优先"是
    core.profile 的既有语义，中心侧任何新路径漏掉它都会造成中心/worker 指向分裂。
  - 该 bug 是否显形取决于文件名首字符（`aaa/...` < `misc.yaml` 时子目录抢先，
    `zzz/...` > `misc.yaml` 时根目录恰好先命中），**跨目录名才暴露**——顺序类规则必须
    用反例文件名钉住，不能靠当前数据巧合通过。

## `rglob` 自身抛异常没兜：破"绝不抛异常"契约，半份清单还会把歧义降级成静默选引擎

- **日期**：2026-09-04（fix round 4）
- **症状**：`_scan()` 只兜了"读单个文件"的异常（round 1 的 `UnicodeDecodeError`），遍历
  本身裸奔 `sorted({p for p in root.rglob("*.yaml") if p.is_file()})`。models 目录下某个
  子目录被设 ACL 拒绝 / 符号链接成环时，`read_profile_source` 直接抛 `PermissionError`
  冒到 Task 5 的 `goal set` → REST 500（与 round 1 同一破口，只是换了抛点）。
- **根因**：三点叠加——
  1. `rglob` 是**惰性生成器**，异常在 `sorted()` 消费时才抛。把 try 写成只包住
     `root.rglob(...)` 这次调用，等于一行没兜；
  2. `Path.walk` 只吞**它自己那次 scandir** 的 OSError，而 `is_file()` 是消费方**另一次
     `stat()`**，其 `PermissionError`/ELOOP 不在 pathlib 的忽略清单内，会原样上抛；
  3. 消费期异常**不止 OSError**：路径含 NUL/无法编码抛 `ValueError`，超深目录树抛
     `RecursionError`。按"想到的几种"列举必然漏（round 1 的 `UnicodeDecodeError` 同族教训）。
- **解决**：`_scan()` 用 `try` 包住**整个遍历表达式**，按继承树之上兜 `Exception`；返回
  `(paths, reason)` 二元组，`_resolve()` 见 reason 立即原样返回。reason 带异常类型名
  （`PermissionError: ...`）便于运维定位是哪类 IO 故障。
- **要点**：
  - **必须 fail-closed**：异常时返回**空清单 + reason**，绝不能把已 yield 的半份结果当
    完整清单。半份清单会漏掉另一个引擎下的同名文件，把"歧义拒发"降级成"静默下发到排序
    靠前的引擎"——正是本模块第一条硬约束禁止的"猜"。实测该 fail-open 变体下
    `read_profile_source("qwen")` 返回 `ok=True, engine="vllm"` 而非拒发，比 500 更难查。
  - 兜"惰性求值"的异常，try 的范围要按**消费点**画，不是按**调用点**画。
  - 测试手段：monkeypatch `Path.rglob` 为 `yield from itertools.islice(real, n); raise exc`
    ——`n=0` 模拟"首个 next 就抛"，`n=1` 模拟"吐一条再抛"（同时钉住惰性语义与 fail-closed）。
    反向验证：去掉兜底 → 5 条全红；改成 fail-open → 同样 5 条全红（且能看到它静默选中 vllm）。

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
