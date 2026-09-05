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

## `_load_candidate` 按类型列举异常：深嵌套 YAML 抛 RecursionError、`port: .inf` 抛 OverflowError

- **日期**：2026-09-04（fix round 5）
- **症状**：round 4 给 `_scan` 兜了 `Exception`，单文件候选这条分支却还按类型列举。
  `models/vllm/deep.yaml` 写 3000 层 `[` → `yaml.safe_load` 抛 `RecursionError`（实测），
  `port: .inf` → `int()` 抛 `OverflowError`（实测），两者都冒到 Task 5 的 `goal set` → 500。
- **根因**：两处列举都低估了被调用方的失败面——`yaml.safe_load` 不只抛 `YAMLError`
  （递归下降会击穿递归上限）；`int(value)` 不只抛 `TypeError/ValueError`（float('inf') 转
  整数是 `OverflowError`）。更危险的是**回退寻址要全量扫描逐个候选过 `_load_candidate`**，
  一处未兜的异常会连带同批所有正常候选一起失效——models 是多人共写的仓库，一份手滑的
  坏 YAML 即可让整个集群的 goal set 全挂（一坏俱坏）。
- **解决**：`_load_candidate` 的读取段与解析段各自 `except Exception` 兜底（`UnicodeDecodeError`
  分支必须排在泛兜**之前**，否则被吞掉后 reason 丢失"不是 UTF-8"这一关键提示）；
  `_port_reason` 的 `int()` 同样兜 `Exception` 并把异常类型名带进 reason。
- **要点**：
  - round 4 的教训要在**同一函数的每条分支**上都补齐："按继承树之上兜"不是某处的补丁，
    而是整模块的策略；改完 `_scan` 别漏 `_load_candidate`/`_port_reason`。
  - 兜底顺序即语义：特定异常分支在前、泛兜在后，否则专有的、可行动的提示会退化成
    `读取失败: UnicodeDecodeError: ...` 这类需要二次翻译的措辞。
  - 触发条件是**内容而非编码**：一份合法 UTF-8、语义合法的 YAML 也能靠深嵌套把解析器
    打穿，"文件看起来没问题"不构成免疫证据。

## 文件名寻址的"根目录优先"只有实现没有测试：round 3 只钉了展示名回退那条

- **日期**：2026-09-04（fix round 5）
- **症状**：无。`_resolve()` 的文件名分支一直走 `_root_first`，行为正确；缺的是把它钉住的
  用例——round 3 的 `test_display_name_fallback_prefers_root_file` 只覆盖展示名回退分支。
- **根因**：顺序类规则**每新增一条寻址路径都要重新确认优先级**，而测试同理：实现里两处
  共用 `_root_first` 不等于两处都被覆盖。临时把文件名分支的 `_root_first(...)` 退化成
  纯路径序 `[p for p in paths if p.stem == name]`，既有 43 条**全绿**，可见该分支是盲区。
- **解决**：新增 `test_filename_addressing_prefers_root_file`——根目录 `qwen.yaml`(port 9)
  + `aaa/qwen.yaml`(port 1)（子目录名首字符排在 stem 前，纯路径序会让子目录那份抢先），
  断言中心取根目录那份，并 `load_profile("qwen")` 做本地口径双证，同时覆盖 `engine=`
  显式选边那条独立取首个的分支。反向验证：去掉 `_root_first` → 该条失败并实际返回
  `aaa\qwen.yaml`（port 1）；恢复后 48 条全绿。
- **要点**：
  - "实现已正确"与"有测试保护"是两件事。补对称测试的成本极低，收益是后续任何一次
    重构（如把 `_root_first` 挪走或改成 set 迭代）都能立刻暴露。
  - 钉顺序规则必须用**反例数据**：子目录名首字符要排在 stem 之前（`aaa/` < `qwen.yaml`），
    用 `zzz/` 之类的巧合数据会假绿。

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

## 中心 gate 的"引擎→GPU 数字段名"表漏一个引擎，8 卡 profile 被当 1 卡放行

- **日期**：2026-09-05（M1 Task 4）
- **症状**：无显式失败。中心 placement gate（`cluster/gate.py`）对 tokenspeed/lmdeploy
  profile 做容量粗筛时恒按 1 卡判定，`gpu_count=1` 的节点被判 `ok`；错误要到 worker
  侧 `check_requirements` 抛 `tensor_parallel_size=8 超过实际 GPU 数` 才暴露。
- **根因**：gate 用一张 `_GPU_COUNT_KEYS` 表把引擎映射到"用几张卡"的字段名，取不到键
  就回落 `gpu_count`。tp 系 profile 里**根本没有 `gpu_count` 这个键**，回落的结果是
  `ec.get("gpu_count", 1)` → 1 而非"不可判"。初稿表里照抄了 vllm/sglang/aphrodite/
  tensorrt_llm，漏了同样读 `tensor_parallel_size` 的 lmdeploy 与 tokenspeed
  （`engines/lmdeploy.py`、`engines/tokenspeed.py` 实测；`models/tokenspeed/qwen3.5-397b.yaml`
  就是 `tensor_parallel_size: 8`）。**"缺字段"与"该引擎不这么配"两种语义被同一个回落吞掉**。
- **解决**：表按 `KNOWN_ENGINES` 全集补全（含 unsloth 的 `tensor_parallel`，注意不是
  `_size` 后缀），并在用例里对每个引擎各钉一条 `declared_gpu_count` 断言，表头注释写明
  "KNOWN_ENGINES 增删引擎必须同步补表"。
- **要点**：
  - 中心/worker 两侧的**同一份 profile 事实必须同口径解析**（与本文"坏 port"那条同族）：
    中心多算或少算一卡，要么误拦正常下发、要么放行必失败的下发，都比直接报错更难排查。
  - 引擎字段的权威来源是 `engines/*.py` 的 `check_requirements`/`build_command` 实际取值，
    不是文档也不是 `vram_estimator`；补表时逐个 grep 引擎适配器确认。
  - 白名单映射表的默认回落值必须**保守且可解释**。回落成"最小的 1"等于把漏配的引擎
    判成最宽松，方向反了；漏项应显式暴露（用例逐引擎钉死）而不是靠默认值兜。

## 测试工厂用 `None` 当哨兵，与"显式传 None"的用例互斥到计划里的 PASS 根本达不到

- **日期**：2026-09-05（M1 Task 4）
- **症状**：M1 计划 Task 4 的 `test_runtime_unknown_is_treated_as_unavailable` 写
  `_node("w-1", runtimes=None)` 断言 `result == "skip"`（节点从未上报 runtimes → 保守 skip），
  而计划给的工厂是 `def _node(..., runtimes=None)` +
  `{"vllm": {"ok": True}} if runtimes is None else runtimes`。显式传的 `None` 与"省略参数"
  撞成同一个哨兵，构造出的节点 runtimes 仍是**健康的** `{"vllm": {"ok": True}}`。
  实测：配**正确**的 gate 实现，该用例以 `AssertionError: assert 'ok' == 'skip'` 失败 ——
  计划正文"Expected: PASS"与它自带的测试**互斥**，那条 PASS 永远达不到。
- **根因**：`None` 既当"缺省"又当"业务值"。本模块里 `runtimes=None`（未上报）是**有语义的
  业务输入**，恰好是最容易被工厂哨兵吞掉的那类值。这类缺陷不一定表现为"静默假绿"，
  更表现为**测试与夹具互相矛盾**：照着计划实现必然红，容易被误读成"实现写错了"而去
  改坏正确的生产代码。
- **解决**：引入模块级哨兵 `_UNSET = object()`，工厂默认值改 `runtimes=_UNSET`、
  判断改 `if runtimes is _UNSET`，用例保持原样即通过。同类问题在 `estimate_vram_mb`
  的用例上还有两处：`assert got is None or isinstance(got, int)` 两侧可同真（int 路径
  零验证），改成确定值 `assert got == 256`；而 `256.0 == 256` 为真（`vram_estimator` 返回
  `round(kv, 1)` 的 float），漏掉 `int()` 转换照样绿，必须再钉 `isinstance(got, int)`。
- **要点**：
  - 工厂/fixture 的默认参数**不能占用业务上有意义的值**（`None` / `""` / `0` / `[]` 都危险），
    用 `object()` 哨兵显式区分"没传"与"传了这个值"。
  - 计划的"Expected: PASS N 条"不是事实。发现**计划自带测试与计划自带夹具矛盾**时，
    先按实测结论修订计划（与 `parse_ack` 丢弃畸形条目那次同处理），再落实现。
  - 写完测试做**变异验证**：把被测实现改坏（回落成恒 1 卡、`nid_width` 改 `len()`、
    去掉 `int()` 转换），确认恰好对应用例转红；三条补强用例都这样验过。
  - `x is None or isinstance(x, int)` 这类"或"断言几乎总是恒真；要验的就确定地断言。
  - 浮点返回值别只断言 `== 期望整数`：`256.0 == 256` 为真，类型漂移看不见。

## gate/CLI 报告按 `len()` 取列宽，中文 node_id 让整表右移错位

- **日期**：2026-09-05（M1 Task 4）
- **症状**：`format_gate_report` 逐节点一行输出，node_id 含中文时该行的 reason 比别的行
  右移若干列（实测 `算力节点-2` 那行右移 4 列）。
- **根因**：列宽用 `max(len(v.node_id) ...)` 算，`pad_width` 却按**显示宽度**补空格 ——
  一个按字符数、一个按 CJK 双宽，两者不匹配。集群视图按 CLAUDE.md 例外条款直接显示
  `node_id`，而它是运维在 `cluster join --node-id` 时自定义的，完全可能是中文。
- **解决**：宽度改 `max(display_width(...) ...)`（标记列同理由）。用例钉的是不变量
  "reason 起始显示列逐行一致"，而不是具体空格数：
  `display_width(line[:line.index(v.reason)])` 取集合，`len(...) == 1`。
- **要点**：
  - 仓库规则"凡字段可能含 CJK 就用 `display_width`/`pad_width`"同样适用于**新写的
    报告/表格函数**；`pad_width` 补、`len()` 算 = 只修了一半，看起来还在用封装但依旧错位。
  - 对齐类断言别数空格（改文案/改列宽就得改用例），钉"各列起始位置一致"这个不变量，
    且数据里必须真的含双宽字符，纯 ASCII 数据会假绿。

## gate 按字面键 `engine_config` 读引擎配置段，真实 YAML 的段键是引擎名——夹具与实现同形状导致全绿生产全错

- **日期**：2026-09-05（M1 Task 4 fix round 1）
- **症状**：无显式失败。`cluster/gate.py` 的 `declared_gpu_count`/`estimate_vram_mb`/
  `_tp_conflict` 都用 `raw.get("engine_config")` 取引擎配置段，而 `read_profile_source`
  返回的 `raw` 是 YAML safe_load **原文**——引擎段挂在**引擎名键**下
  （`models/tokenspeed/qwen3.5-397b.yaml` 是 `tokenspeed: tensor_parallel_size: 8`，
  权威口径 = `core/profile.py::_to_profile` 的 `engine_config = raw.get(engine) or {}`）。
  对一切真实 profile：`raw.get("engine_config")` 恒 None → 卡数恒判 1、显存估算恒
  None、tp 一致性恒不触发。上一轮补的"引擎→卡数字段表"在真实数据上整体空转。
- **根因**：`engine_config` 是 **Profile dataclass 的字段名**，不是 YAML 里的键；
  计划 Task 4 的合成夹具 `SRC["raw"] = {"port": ..., "engine_config": {...}}` 恰好
  也用了这个假键，实现照抄夹具形状 → 22 条测试全绿，而夹具形状与 `read_profile_source`
  的真实返回不符。这是"中心/worker 同口径"族的**最恶性变体**：不是实现偏离权威口径，
  而是**实现与测试夹具一起偏离**，单测体系对该缺陷整体免疫。
- **解决**：新增 `_engine_section(raw, engine)` 单点封装（镜像 `_to_profile` 的
  `raw.get(engine)`），三处消费点统一改走它；测试夹具 SRC 及全部 raw 断言改成真实
  形状 `{"port": 8101, "vllm": {"tensor_parallel_size": 2}}`；另加两条钉：
  `test_declared_gpu_count_ignores_literal_engine_config_key`（字面 engine_config 键
  必须视同空段）与 `test_declared_gpu_count_reads_real_multicard_profiles`
  （用 `read_profile_source` 读**仓库真 profile** 钉 tokenspeed=8/unsloth=2/llamacpp=1）。
- **要点**：
  - 消费上游解析结果的模块，**夹具必须复刻上游的真实形状**，或干脆用
    `read_profile_source` 真读一份仓库 profile 做回归；合成夹具的每个键都该能
    在上游真实返回值里找到出处。
  - 变异验证夹具形状是否"活"：把实现改回 `raw.get("engine_config")`，真 profile
    回归用例应转红（实测 6 条红，含 8 卡→1 卡、显存估算→None）。
  - dataclass 字段名 ≠ 序列化文档的键名；跨模块传 dict 时，键的权威定义在**产出方**
    （这里是 `read_profile_source`/`_to_profile`），消费方凭印象造键名必错。

## unsloth 的 `tensor_parallel` 是布尔开关却按卡数 int()，llamacpp 卡数缺省与适配器的 8 脱节

- **日期**：2026-09-05（M1 Task 4 fix round 1）
- **症状**：补全引擎表后仍判错。`{"unsloth": {"tensor_parallel": true}}` 被判 **1 卡**
  放行（`int(True)==1`），而 worker 侧 `engines/unsloth.py:74` 对 `tensor_parallel`
  硬要求 ≥2 卡；`{"llamacpp": {}}`（未写 `gpu_count`）被判 1 卡，而
  `engines/llamacpp.py:239` 是 `cfg.get("gpu_count", 8)`——真实启动按 8 卡要资源，
  单卡节点上 worker 报 "gpu_count=8 超过实际 GPU 数 1" 拒启。中心放行 = 一条永不
  收敛的 goal。
- **根因**：上一轮沉淀的"漏表"教训只覆盖了**键名**维度，没覆盖**同一键名下的字段
  语义与缺省值**维度：`tensor_parallel`（unsloth，bool）与 `tensor_parallel_size`
  （tp 系，int）名字只差后缀、语义完全不同；models/unsloth/*.yaml 现实值只有
  true/false，`int(True)=1` 静默通过。缺省值同理——字段缺失时各引擎适配器的真实
  默认不同（llamacpp=8，其余=1），中心统一回落 1 与适配器缺省脱节。
- **解决**：gate 拆出 `_GPU_FLAG_KEYS = {"unsloth": ("tensor_parallel", 2)}`（布尔开关
  → 最小卡数下界）与 `_GPU_COUNT_DEFAULT = {"llamacpp": 8}`（缺省跟随适配器）；
  集合守卫用例 `set(KNOWN_ENGINES) <= set(_GPU_COUNT_KEYS) | set(_GPU_FLAG_KEYS)`
  把"增引擎必须落表"变成硬约束；真 profile 回归钉 unsloth=true → 2。
- **要点**：
  - 字段名映射表要钉到**语义三件套**：键名、值类型/语义（bool/int/list）、缺失时的
    真实缺省值——三者都要以 `engines/*.py` 实际取值为准，逐个打开适配器看。
  - `int(value)` 对 bool 静默成功（True→1）是最危险的类型错配：不抛异常、结果还
    "看着合理"。凡字段可能是布尔，先查适配器怎么用它，再决定解析方式。
  - "放行下游必拒内容"族的又一变体：这次不是校验缺失，而是**容量维度算小了**——
    粗筛表的每个数字都必须与执行侧同口径，包括缺省值。

## gate 放行 worker 必拒的组合：tp 系 gpu_list 卡数 ≠ tensor_parallel_size；节点 disabled 位漏查

- **日期**：2026-09-05（M1 Task 4 fix round 1）
- **症状**：两个独立漏拦。① `goal set --gpus 0,1,2` 下发 tp=2 的 vllm profile：
  中心 gate 判 ok 落库，worker `check_requirements` 六个 tp 系适配器同文案硬失败
  （`engines/vllm.py:83`：`len(gpu_list)` 必须 == `tensor_parallel_size`），goal
  永不收敛。② 运维 `cluster disable` 停用的节点被 `--node` 显式指定时照常下发——
  `_verdict_one` 上方注释承诺"offline/disabled 不发"，代码却只查了 `status`；
  store 的 `disabled` 是独立位（`_NODE_COLS` 含它），rejoin 会把 status 刷回
  online，只查 status 形同虚设。
- **根因**：① `need_gpus = len(requested)` 让显式卡位**覆盖**了 profile 声明，容量
  判定自洽了，但"覆盖后与 profile 内部字段矛盾"没人管——中心把"以谁为准"的选择
  做了一半。② 注释里的承诺清单没有对应的用例逐条钉，兑现情况无人核对。
- **解决**：新增 `_tp_conflict()`——requested 非空、引擎 ∈ 六个 tp 系、profile 写了
  tp 且 `tp != len(requested)` 时 skip 并给出与 worker 同文案的冲突提示（tp 键缺失
  时适配器按 `len(gpus)` 兜底、天然一致，不拦）；`_verdict_one` 顶部补
  `if node.get("disabled")` 拦截。各配一条用例（矛盾→skip、一致→ok、无 tp 键→ok；
  disabled=1 且 status=online → skip）。
- **要点**：
  - worker 侧存在"参数组合必拒"硬校验时，中心凡是**改写其中一侧**的逻辑（如
    gpu_list 覆盖卡数）必须同时校验组合一致性，否则放行即毒 goal——与"stem 未过
    写盘白名单""坏 port"同族。
  - 状态位是**多个独立列**（status/disabled）时，每个消费点都要核对全部分量：
    `disabled` 不改变 `status`，而 rejoin 又会把 `status` 刷回 online——只看
    `status` 的闸门对"人为停用"永远失明。
  - 注释承诺的拦截清单（"offline/disabled 不发"）应与用例一一对应；本次就是
    注释兑现检查顺带揪出的漏拦。

## 显存估算 int() 向下截断违背"下界"语义；整数夹具对截断缺陷不敏感

- **日期**：2026-09-05（M1 Task 4 fix round 1）
- **症状**：`estimate_vram_mb` 返回 `int(est["kv_total_mb"])`，255.4 → 255。该值是
  "节点至少要有这么多显存"的**下界**口径，截断让 255MB 的节点通过实际放不下的下发。
- **根因**：既有 int 用例的夹具恰好整除（估算恰好 256.0），`int(256.0) == ceil(256.0)`
  ——数据对截断不敏感，变异 `ceil→int` 时 29 条**全绿**，属实现缺陷与测试盲区同存。
- **解决**：改 `math.ceil(float(...))`；新增 `test_estimate_vram_rounds_up_fractional_estimate`
  ——monkeypatch `kv_estimate_for_profile` 喂 `{"kv_total_mb": 255.4}` 断言返回 256。
  变异验证：`ceil→int` 后该条精确转红（255 ≠ 256）。
- **要点**：
  - "下界/上界"型数值在边界处的取整方向就是语义本身：下界向上取整、上界向下取整，
    `int()` 恒向零截断，两个方向都错。
  - 取整类逻辑的用例数据必须**带小数**（或落在边界 ±1），整除夹具会让变异验证
    假阴性——变异没转红不一定是代码有别处兜底，先怀疑数据不敏感。
  - 直接构造能让被测值非整数的输入最稳的途径是 monkeypatch 估算器，不必为凑小数
    去反推模型架构参数。

## gate 三处裸 `int()` 重犯 `.inf` 的 OverflowError：只修被点名的那处 = 另外两处继续炸

- **日期**：2026-09-05（M1 Task 4 fix round 2）
- **症状**：round 1 声称已修 P1，但 `declared_gpu_count({'vllm': {'tensor_parallel_size': float('inf')}}, 'vllm')` 实测仍抛 `OverflowError`。控制器复核又发现同模块两处同型裸路径：`_tp_conflict` 的 `int(ec["tensor_parallel_size"])`、`evaluate_gate` 的 `int(source["gpu_count"])` / `int(source["min_vram_mb"])`——全部只 `except (TypeError, ValueError)`。端到端：profile YAML 写 `tensor_parallel_size: .inf` → `read_profile_source` 返回 ok=True（只校验 port）→ `evaluate_gate` 抛 `OverflowError`，冒到 Task 5 `set_goals`（契约"永不抛业务异常"）即 REST 500。
- **根因**：修"某个 `int()` 漏 OverflowError"时只改**评审点名的那一行**，没在同一模块内横向扫其余裸 `int()`。`int(float('inf'))` 抛 `OverflowError`（`float('nan')` 抛 ValueError）是 `int()` 的固定失败面，任何 `int(用户可控值)` 都会中招。`profiles._port_reason` 在 round 5 已因**完全相同**的 `.inf`→OverflowError 泛兜 `Exception` 并沉淀，但那条"按继承树之上兜"从未作为**模块级策略**推广到 gate——`declared_gpu_count` 的 docstring 白纸黑字写"任何异常一律取 1"，实现却按类型列举，契约与实现自相矛盾。
- **解决**：抽 `_safe_int(value, default)` 单点封装（内部 `except Exception`），`declared_gpu_count`、`_safe_port`、`evaluate_gate` 的 need_gpus/min_vram 三处改走它；`_tp_conflict` 坏值要返回 `""` 而非 int，用本地 `try/except Exception`（语义相同、出口不同）。用例参数化 `inf/-inf/nan` 覆盖 `declared_gpu_count` 与 `evaluate_gate` 两条端到端路径，`_tp_conflict`/`_safe_port` 各单独钉；变异验证 `except Exception`→`except (TypeError, ValueError)` 分两处做：`_safe_int` 变红 6 条、`_tp_conflict` 变红 3 条。
- **要点**：
  - 修一个 `int()`/`float()`/解析类漏点，必须 grep **同模块 + 同调用链**的所有同类裸转换一次性泛兜；评审只点名一处 ≠ 只有一处（gate 这次三处，profiles round 5 已是前车）。**契约写"任何异常"，实现就不能按类型列举**——docstring 的"任何"是硬承诺。
  - 泛兜逻辑出现第 2 次就抽封装（`_safe_int`/`_safe_port`），把"按继承树兜 + 永不冒泡"固化到一处；散落的 `try/except (TypeError, ValueError)` 是"下次新增又漏兜"的温床，单点封装让新增消费点自动继承正确兜底。
  - 坏值回落的 `default` 有业务语义：坏卡数回落**契约最小值 1**，而非引擎缺省（llamacpp=8）——坏配置无法推断意图，猜大数字会误拦健康节点并把 reason 写成误导性的"gpu_count 不足：需 8 卡"。缺省值只用于"字段缺失"（合法未配置），"存在但非法"走更保守的 1；且坏计数值不得连带吞掉同段内独立有效的 `gpu_list` 选卡数。
  - `_tp_conflict` 泛兜后仍须保持"坏值不作为拒判依据"的既有语义（转不出整数就返回 ""，由 worker check_requirements 报错），不能因为兜了异常就编一条 `tensor_parallel_size=inf 与卡位数不一致` 的误导性 reason——泛兜 ≠ 改变判定方向。
  - **同型漏点还包括 try 块之后的转换**：`estimate_vram_mb` 的 try 只包估算器调用，try 之后的 `math.ceil(float(est["kv_total_mb"]))` 在估算值本身为 inf（如 `max_model_len: .inf` 参与乘积）时同样抛 OverflowError——"泛兜一条链路"必须逐行核对到函数的**出口**，不止 try 覆盖的那段。非有限估算值按"不可估算"返回 None（无法参与容量比较）。
  - **端到端复现脚本**（`read_profile_source` 读带 `.inf` 的真 YAML → `evaluate_gate`）比纯单测更能证明"Task 5 永不抛 → 不会 500"这条跨任务契约真的成立；验证后删除，结论写入 review。

## 多行 `source.reason` 原样拼进报告，破坏"逐节点一行"不变量

- **日期**：2026-09-05（M1 Task 4 fix round 2）
- **症状**：`source.ok=False` 短路时 reason 直接取 `read_profile_source` 的失败原因，而它可能是整段 `yaml.YAMLError` 的 str（实测 4 行，含换行）。`format_gate_report` 原样 `f"...{v.reason}"` 拼接后，一个节点被输出成 4 行，`len(lines) == 节点数 + 1` 的行数不变量破裂，按行消费的 CLI/测试全部错位。
- **根因**：报告函数假设 reason 是单行短语（各分支自产的 skip/error 文案确实单行），但**短路分支的 reason 来自外部**（YAML 解析器、IO 异常的 str），这些字符串天然可能多行。列对齐用 `pad_width` 处理了 CJK 宽度，却没处理换行——两者都是"字段内容形态不受本报告函数控制"的表现。
- **解决**：拼行前 `reason = " ".join(v.reason.split())` 折叠所有空白（含换行→空格）。用例喂一段多行 YAML 错误串，断言"报告行数 == 节点数 + 1"（钉不变量而非具体文案）。变异验证去掉折叠 → 该条转红（实测 7 行 ≠ 3 行）。
- **要点**：
  - 逐行/逐列对齐的**报告/表格函数，对任何外部来源字段都要先归一化换行**（`\r\n`/`\n`/`\t` 折成空格），不能假设上游只给单行；本模块自产的短文案单行 ≠ 所有 reason 单行。
  - 折叠空白用 `" ".join(s.split())` 一行搞定，比 `s.replace("\n", " ")` 稳（顺带压掉连续空白/制表/`\r`）；这是"把不可控的多行输入塞进单行槽位"的通用收口。
  - 与"列宽按 `len()` 算"同族：对齐类函数要同时对**宽度**（CJK 双宽）和**行数**（换行）两个维度免疫，缺一个都会错位。

## gate 只把显式 --gpus 当生效卡位，profile 内 gpu_list 绕过 tp 一致性与在用卡位求交

- **日期**：2026-09-05（M1 Task 4 fix round 3）
- **症状**：两个独立漏拦。① tp=4 的 vllm profile 在 YAML 里写 `gpu_list: "0,1"`、下发时不传 `--gpus`：中心 gate 判 ok，worker `check_requirements`（engines/vllm.py:81-83）对 `len(gpu_list)=2 ≠ tensor_parallel_size=4` 硬失败，goal 永不收敛；`gpu_list: "2,3"` 撞上在用 GPU `[2,3]` 同样判 ok（clash 求交只对 requested 做，requested 为空 = 空集求交恒空）。② unsloth `tensor_parallel: true` 配 `--gpus 0`：need 取 `len(requested)=1`，中心判 ok，而 worker（engines/unsloth.py:82-83）对生效 gpu_list <2 硬失败。
- **根因**：round 1 修"requested 优先容量"时，把 `_tp_conflict` 与 clash 求交的输入也一并绑死在 `requested` 上——而 worker 侧 `selected_gpus()` 读的是 **gpu_list 优先、CLI 其次**，"生效卡位"在中心被窄化成了 `--gpus` 一个来源。计划裁决④"profile 声明的 gpu_list 与在用 GPU 求交"只做了一半（`declared_gpu_count` 里读了 gpu_list 的**卡数**，卡位**序号**却没喂给冲突判定）。unsloth 下界同理：round 1 只在 `declared_gpu_count`（无 requested 路径）实现布尔下界，requested 路径的 `len(requested)` 直接覆盖，下界对生效卡位失明。
- **解决**：`evaluate_gate` 归一 `effective = requested or _gpu_list_ids(profile 内 gpu_list)`，同喂 `_tp_conflict`、clash 求交与 need_gpus；unsloth 下界改成**冲突拦截**（`flag_conflict`，effective < floor 即 skip）而非抬高 need_gpus——抬高只会拦小节点，4 卡节点带 1 卡 gpu_list 照样放行，而 worker 拒的是"选中的卡位数"，与节点大小无关。`_gpu_list_len` 改 `_gpu_list_ids` 返回卡位列表（卡数 = len）。变异验证：`effective = requested` → 恰红 2 条（tp/clash）；下界分支禁用 → 恰红 1 条。
- **要点**：
  - "A 覆盖 B"类裁决必须核对**下游权威实现里覆盖的完整语义**：worker 的生效卡位 = gpu_list or CLI，中心只认其中一个来源 = 同一事实在两侧口径分裂，"放行 worker 必拒内容"族再次显形。
  - 卡位冲突求交的输入集合必须与 worker 实际锁卡的集合同源；空集合参与求交恒为空，"没传参数"不等于"没有生效卡位"。
  - **下界校验放错了机制就是没放**：容量不足（need > 节点卡数）与卡位组合非法（选中的卡数 < 开关要求）是两种判定——前者随节点大小变化、后者与节点大小无关，用抬高 need 实现下界只对部分节点生效。
  - 单测全绿的又一变体：夹具里 requested 与 profile gpu_list **从不同时出现**，两侧各测各的都能绿；跨来源归一类逻辑必须显式构造"另一来源非空"的组合数据。

## gate 缺省回落用 `or` 短路：显式 0 被当缺失，同值三果（0→8、"0"→1、""→8）

- **日期**：2026-09-05（M1 Task 4 fix round 3）
- **症状**：`declared_gpu_count({"llamacpp": {"gpu_count": 0}}, "llamacpp")` 返回 **8**（`0 or 8` 短路），而 worker `engines/llamacpp.py:239` 对显式 0 按 0 校验；字符串 `"0"` 却返回 1（`"0" or 8` → `"0"` → int 成功）。同一个语义值在中心有三种结果。`evaluate_gate` 的 `source.get("gpu_count") or declared_gpu_count(...)` 同型：显式 0 被 profile 事实（如 tp=2）推翻。round 2 刚裁决"缺省只用于缺失"，同一段代码里 `or` 短路就是违例。
- **根因**：Python 的 `or` 按**真值**回落而非**存在性**，0/""/False 都是 falsy——"缺省回落"与"零值/falsy 值"是两回事。round 2 修的是 `int()` 抛异常的泛兜面，没扫 `or` 短路这条"不抛异常但语义错"的姊妹路径；两处 `or`（declared 路径 + evaluate_gate 的 source 路径）互相背对，改一处漏一处。
- **解决**：两处都改显式判 `value is None or value == ""` 才落引擎缺省；存在但非法（非整数/inf）与显式 0 统一收拢契约下限 1（`max(1,·)`），与 round 2"坏值回落 1 而非引擎缺省"的既有裁决同向。用例钉住四象限：0→1、"0"→1、""→8、None→8。变异验证：改回 `or` → 恰红对应 2 条。
- **要点**：
  - 缺省回落的判据是**存在性**（`is None` / 显式空值），永远不是真值（`or` / `if x:`）——falsy 但合法的值（0、False、空列表）是这类缺陷的固定受害者，与"工厂默认值不能占用业务值"（`_UNSET` 哨兵）是同一课的两个方向。
  - 三态设计要显式写进契约：**缺失**（引擎缺省）/ **存在但非法**（保守下限）/ **显式值**（按值判），每个态各钉一条用例，否则任何一次 `or → if-else` 重构都可能悄悄合并其中两态。
  - YAML 语境下 `key:`（隐式 null）与 `key: ""` 都是"写了但等于没写"，归入缺失态与键不存在同路径，避免第四种结果。
