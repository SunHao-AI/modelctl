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

## 展示名归一只做在读侧：GoalService 用调用方原词查幂等集，重跑把 worker 状态机清零

- **日期**：2026-09-05（M1 Task 5）
- **症状**：profile 文件 `models/vllm/qwen-fast.yaml` 内写 `name: qwen-display`，用户按 UI 展示名下发的第一次成功（`read_profile_source` 条款④已把归一后的 stem 放进 `source["name"]`，落库是 `qwen-fast@@w-1`）。第二次同样的展示名重跑，本应 gate 幂等 skip，实际 `created == 1` → `upsert_goal` 把 `stage` 从 READY 重置回 `PENDING_PROFILE_SYNC`，等于"幂等重跑把 worker 状态机清零"。
- **根因**：Task 3 的归一裁决只覆盖**读取侧的返回值**，Task 5 的 `set_goals` 却拿**调用方原词**（展示名）去查 `list_goals(profile=profile)` 组装 `existing_goal_ids`、并比对 `local_profiles`——goal_id/落库/worker 写盘文件名三处只认 stem，于是"查库键"与"写库键"用了两套标识，第二次必然查不到已有 goal。计划正文的参考实现正是这么写的（`existing = {… list_goals(profile=profile)}`、`has_profile = {…: profile in …}`、`_write_goal(profile=profile)`）。
- **解决**：`source["name"]` 取一次 `name`，`existing` / `has_profile` / `_write_goal(profile=name)` / 事件 payload 全部改用 stem；新增端到端用例 `test_set_goals_addressed_by_display_name_normalizes_to_stem`（真写一个带 `name:` 的文件，断言落库 profile 是 stem + 第二次 `created == 0`）。该用例在改实现前是**真实 RED**（`1 == 0`）。
- **要点**：
  - "标识归一"是**跨任务契约**：上游归一出的规范名，下游每个消费点（查库、写库、比对节点上报清单、事件 payload、写盘文件名）都必须换成它。只在读侧归一 = 写侧用原词 = 同一个实体在系统里有两个主键。
  - 自检口诀：一个函数里凡出现"调用方传入的字符串"直接参与 `where`/`get`/`key in list`，先问一句"它归一了吗"。参数名同名（`profile`）掩盖了两种语义（寻址名 vs 规范名），必要时像本次一样立刻落到局部变量 `name` 上，让原词不再出现在后续逻辑里。
  - 幂等类缺陷的测试必须**跑两次**并断言第二次的计数与状态（不只是"库里只有一条"）——只断言条数的话，upsert 覆盖 stage 这类破坏完全隐形。

## 计划正文与自身测试/下游契约互斥：dry-run created、sha in YAML、撤除报告文案

- **日期**：2026-09-05（M1 Task 5）
- **症状**：实现期发现计划 Task 5 内部/跨任务三处互斥。① 测试断言 `g["sha"] in YAML`——`sha` 是 `sha256:`+十六进制摘要，按定义**不是**原文子串，该断言恒假。② `test_set_goals_dry_run_writes_nothing` 断言 `created == 0`，而 Task 11 的 REST 用例断言 `body["created"] == 1`、Task 12 的退出码判定依赖 `created == 0 且 errors > 0 → 2`（把 dry-run 的"将创建 1 个"报成 0，CLI 会输出"无变更"并退 0，用户以为没生效）。③ `remove_goals` 参考实现文案 `f"已删除 {n} 个 goal"` 不含 profile 名，同任务测试却断言 `"qwen" in out["report"]`。另有 ④ `_write_goal(source=source)` 让 `placement.min_vram_mb` 恒 0（估算值只挂在 `enriched` 上，写的是没合并过的 `source`）——死字段，无断言即无人发现。
- **根因**：计划的参考实现与参考测试由不同轮次写出，缺少"实现/测试/下游消费者"三方交叉核对；`sha`、`created` 这类**语义被别处定义**的量，在计划里按直觉写。dry-run 的 `created` 更是"预计数 vs 实际数"的经典歧义，`format_gate_report` 早已按"将创建/已创建"消费它。
- **解决**：以测试/下游契约为裁决基准（Task 11/12 是要编译进 CLI 与 REST 的对外契约）——dry-run `created=1` 且台账不写、report 前缀 `[dry-run]` 显式标注；sha 改为重算 `hashlib.sha256(YAML).hexdigest()` 比对；report 改 `f"profile {profile}：已删除 N 个 goal"`；`_write_goal` 传 `enriched`。六处订正连同裁决理由写回计划 Step 4，避免后续 Task 抄回旧稿。
- **要点**：
  - 计划的**测试代码 = 契约**、**实现代码 = 建议**。二者冲突时改实现；实现与**下游任务**的断言冲突时，以下游对外契约为准并回改上游测试——写代码前先把该字段在整个计划里的所有出现点 grep 一遍。
  - 哈希/摘要字段不能与原文互相断言包含关系。钉"原文逐字节下发"用 `g["yaml"] == YAML` 整串比对（`"${API_KEY}" in yaml` 只能证明没插值，证明不了没丢段/没改写），钉 sha 用**重算比对**。
  - 落库的派生字段（placement/估算值/统计列）必须有至少一条断言其**非默认值**的用例，否则"取错来源对象"会静默退化成死字段。
  - 计数语义有歧义（dry-run/部分成功/幂等重跑）时，把"预计数/实际写入数/受影响数"写进 docstring 并各配一条用例——这三个数在 CLI 退出码、REST body、报告文案三处必须同值。

## 恒真断言新变体：键名出现在错误文案里、幂等重跑让"隔离性"断言失去判别力

- **日期**：2026-09-05（M1 Task 5 变异验证）
- **症状**：7 项变异验证首轮 1 项存活——把 `validate_env_overlay` 的凭据前置检查整段删掉，`test_env_overlay_rejects_secret_keys_even_inside_allowlist_shape`（原稿断言 `"API_KEY" in err`）仍绿：`MODELSCOPE_API_KEY` 不在白名单，走的是通用文案 `f"env_overlay 键 {key!r} 不在白名单 …"`，**键名被原样带出**。同类问题在同文件的 revision 用例：原稿用 `set_goals(w-2, create=True)` 制造"他节点变更"，而 goal 已存在 → gate 幂等 skip → 什么都没变，"w-1 的 revision 不变"即使 `snapshot_for` 忘了按 node_id 过滤也照样绿。
- **根因**：断言选的是"必然随错误一起出现的字符"（键名、节点 id、通用前缀），而不是**该分支独有的措辞**；隔离性断言的"变更"没有真正改变被隔离侧的数据（幂等设计使重复下发是 no-op）。两者都属"实现删掉一半也全绿"的假绿。
- **解决**：凭据类拒绝统一断言专用措辞 `"凭据" in err`（Task 11 REST 层同口径，形成两侧共用的措辞契约）；隔离性用例改用 `store.update_goal("qwen@@w-2", intent="stop")` 真改数据，并加 `assert store.get_goal("qwen@@w-2")["intent"] == "stop"` 自证前提成立。复跑变异 7/7 KILLED。
- **要点**：
  - 拒绝/校验类断言必须锁**该分支专属词汇**，绝不锁键名/ID/通用前缀——这些在多条失败路径里都出现，等于没断言。挑词标准：把这条检查整段删掉，错误文案里还剩不剩这个词？
  - "A 的变更不影响 B"类断言，写完要自问"B 侧数据真的变了吗"。幂等 API 的重跑天然是 no-op，不能用来制造变更；直接写库或调用能真正改状态的接口，并显式断言前提（`intent == "stop"`）。
  - 变异验证的价值恰在首轮有存活项：全绿的变异报告要先怀疑"断言恒真"，再怀疑"实现有别处兜底"。

## --gpus 只进 params/placement 不落地进下发 YAML，中心按 requested 判、worker 按 declared 锁卡

- **日期**：2026-09-05（M1 Task 5 fix round 1，用户裁决 A）
- **症状**：无显式失败。`goal set --gpus 0,1` 下发 profile 内 `tensor_parallel_size: 4`（或未写
  gpu_list）的 vllm 模型：中心 gate 用 `effective = requested or declared` 按 `[0,1]`（2 卡）判
  容量与 tp 一致性并放行落库，但**下发/写盘的 YAML 保持 profile 原文逐字节透传**（`sha` 是原文
  哈希），`params["gpu_list"]` 与 `placement.gpu_count` 只是台账侧记账，worker 侧 `apply_snapshot`
  原样落 YAML、Task 9 reconciler 均不消费 params。worker `selected_gpus()`
  （`engines/base.py:68-71` = `resolve_gpu_list(engine_config.gpu_list, None, MODELCTL_GPUS)`，
  优先级 **profile.gpu_list > CLI > env**）读的是**下发 YAML 引擎段的 gpu_list**，于是 worker 按
  profile 声明的卡位锁卡、中心按 requested 判定 → 中心与 worker 对"这个 goal 用哪几张卡"给出两个
  答案，双源不一致的毒 goal。`--gpus` 在 M1 里实际"无落地路径"，是个只写不读的字段。
- **根因**：`resolve_gpu_list` 的优先级是 **profile 声明 > 中心显式点卡**，与直觉相反——中心以为
  "我显式传了 --gpus 就覆盖 profile"，但 worker 读的是下发内容里的 `engine_config.gpu_list`，中心
  从没把 requested 写回下发内容。这是"中心/worker 同口径"族（坏 port、不安全 stem、大小写、
  gpu_list 生效卡位）的又一变体：**中心掌握并据以判定的事实，没有随下发传递到执行侧**，两侧各自
  按不同来源解析同一份 profile，`requested or declared` 的语义在中心兑现、在 worker 落空。
- **解决**（裁决 A）：`GoalService.set_goals` 生成 goal 时，**仅当 `gpu_list` 非空**，把生效卡位
  合并进下发 YAML 引擎段——新增 `_merge_gpu_list_into_yaml()`：`safe_load` →
  `data[engine]["gpu_list"] = list(gpu_list)` → `safe_dump(allow_unicode, sort_keys=False,
  width=4096)`，`sha` 对**合并后文本**用 `profiles.profile_sha()` 重算，经 `enriched` 覆盖
  `yaml`/`sha` 传给 `_write_goal` 落库。合并后 worker `declared == requested`，双源不一致从源头
  消除，**gate 零逻辑改动**。无 `--gpus` 时保持 `source["yaml"]`/`source["sha"]` 纯原文透传——
  "原文逐字节"铁律不破（避免键序/引号往返差异被 worker 漂移检测误判成篡改）。合并**绝不插值**
  （`${VAR}` 原样保留），"密钥不出中心"不破。
- **要点**：
  - 中心据以判定的每个事实，若 worker 执行时也要用，**必须随下发内容传递**，不能只在台账侧记账。
    "记进 params/placement" ≠ "落地到生效路径"——params 是审计/回显通道，worker 消费的是下发 YAML。
  - 改写下发 YAML 要守两条铁律的边界：**只在必要时改写**（有 --gpus 才动，否则纯原文），**只动必要
    的键**（safe_load/dump 仅改一个数字字段，绝不触碰占位符/其他键），且 sha 必须跟着改成合并后文本
    的哈希（否则 worker 拿原文哈希比对合并后落盘内容，一写盘就误判"本地被篡改"）。
  - "放行下游必拒内容"族的镜像：这次不是中心放行坏值，而是**中心与 worker 各按不同来源解析导致
    期望状态不收敛**——双侧纵深防御不仅要求"同口径校验"，还要求"同口径的生效输入"，即中心算 effective
    卡位、worker 也必须拿到同一个 effective 卡位。
  - 测试互为镜像钉边界：带 --gpus 用例断言下发 yaml 引擎段 `gpu_list==传入值` + `sha==profile_sha(合并后)`
    + `!=原文哈希`；无 --gpus 用例断言 `profile_yaml==YAML`（逐字节）+ `sha==profile_sha(原文)` +
    引擎段不凭空多出 gpu_list。只测带 --gpus 会漏掉"不该改写时改写了"的回归。

## 中心"在用 GPU"白名单与 Task 9 实际写入值零交集——"夹具与实现同错"家族二次引爆，gate 容量检查生产静默空转

- **日期**：2026-09-05（M1 Task 5 fix round 2，评审裁决 1）
- **症状**：无显式失败，单测全绿。`GoalService._in_use_gpus` 过滤
  `row["state"] in ("READY","STARTING","UP")`，而 `model_states.state` 全仓唯一写入口是
  `record_model_states`（透传 worker 心跳），Task 9 计划 `_STATE_OF_STAGE` 实际写入的是
  **小写** `running/starting/degraded/...`。两套词表**零交集** → Task 7 接线后中心"在用
  GPU"恒为空集，gate 的"卡位冲突求交"与"节点 GPU 已占满"两项检查在生产静默空转——
  与在用模型抢同一张卡的 goal 被放行，冲突推迟到 worker `gpu_lock` 才爆。附带：
  `_in_use_gpus` docstring 声称"回退 worker 侧 gpu_lock 上报"，代码根本没有该分支
  （gpu_lock 真值在 worker 本地，中心拿不到），注释许诺了一条不存在的自愈路径。
- **根因**：Task 5 与 Task 9 各写各的状态字面量，中间没有共享常量；测试夹具
  `{"qwen": {"state": "READY"}}` 与实现**同用**这套生产从不产生的虚构值——与 Task 4
  `engine_config` 假键同族："夹具与实现同错 → 单测体系对缺陷整体免疫"。词表这类
  **跨任务枚举契约**没有任何单一权威定义点，两侧凭想象各抄一份，抄错也不报错，只
  让下游判定恒空/恒真。
- **解决**（裁决 1）：goals.py 定义模块级 `GPU_OCCUPYING_STATES: frozenset[str] =
  frozenset({"running","starting","degraded","READY","STARTING","UP"})`（不带下划线，
  供 Task 7/9 与计划文本引用同一来源；小写=Task 9 实际写入值为未来主词表，大写=M0
  心跳透传/历史值过渡兼容），`_in_use_gpus` 判 `state in GPU_OCCUPYING_STATES`；删除
  docstring 的 gpu_lock 虚假承诺。夹具与用例改用真实词表，running/starting/degraded
  各钉一条占卡断言 + stopped/failed/未知态不占卡；端到端用例 `record_model_states
  (state="running") → set_goals 同卡位 → skip "占用"` 复现最小失效路径。变异验证：
  白名单退回大写三值 → 恰红 3 条。计划 Task 5 条款 + Task 9 `_STATE_OF_STAGE` 注释
  同步注明"占卡词表以 goals.GPU_OCCUPYING_STATES 为唯一来源"。
- **要点**：
  - **跨任务共享的枚举值（状态词表、错误类、事件 kind）必须落成一个命名常量并被两侧
    import**，禁止各处抄字面量；抄写的那一刻就注入了"零交集也不报错"的潜伏雷。没有
    共享模块可 import 时（如 worker 侧刻意不 import 中心的 store 链），至少在计划与
    双侧注释里互相点名唯一权威 + 用一条测试钉住全集。
  - 判定"某集合为空是否正常"的测试要问：**夹具值的出处是生产写入口吗**？凡状态字段
    参与业务判定（过滤/求交/计数），夹具值必须从写入口的映射表（这里是
    `_STATE_OF_STAGE`）反查得到，不能凭 dashboard 观感或旧版记忆编造。
  - docstring 里"回退 X 来源"的承诺必须能在代码里指出对应分支；写不出的承诺就是
    给未来排障者画的饼，评审时按缺陷处理（本轮直接删除该句）。
  - 白名单是**放行面**：少一项 = 静默漏判（本例：不占卡），多一项 = 静默误判（终态
    被当占卡 → 节点被僵尸行占满永不放行）。两侧各钉用例：词表内每个值都占卡、
    词表外每个终态/未知态都不占卡。

## 标识归一漏掉删除侧；归一依赖可被删除的外部源时，失败必须显式降级为"原词单候选"

- **日期**：2026-09-05（M1 Task 5 fix round 2，评审裁决 2）
- **症状**：两个分裂。① `set_goals` 已把展示名归一成 stem 落库，`remove_goals`/
  `_targets_for_removal` 却拿调用方原词拼 `goal_id_of(profile, node)`、按原词查
  `list_goals(profile=)` → Task 12 CLI `goal remove --profile <展示名>` 全进 missing，
  `--all` 查库查不到，"set 能用展示名、remove 报不存在"。② 连带清 model_states 用
  原词：`delete_model_state(node_id, profile)`，而 model_states 是 worker 回流（stem
  建键）→ 展示名撤 goal 成功、运行态却残留，dashboard 继续显示"在跑"且被当占卡。
- **根因**：上一轮"归一是跨任务契约"只修了 set 路径，remove 路径是同一契约的第二个
  消费点却漏改（查库键/写库键/清理键三处，修了两处）。且归一实现有个天然陷阱：
  `read_profile_source` 是**唯一**的"寻址名→stem"映射源，而 profile 文件在 remove 时
  **可能已被删除**（撤模型 = 删 YAML 与撤 goal 是两个可任意先后的操作）——归一失败若
  报错或返回空候选，"文件已删"就连带"goal 撤不掉"，台账里留下永久撤除失败的僵尸。
- **解决**（裁决 2）：`_resolve_names(profile)`：读到源 → `[stem, 原词]`（dict.fromkeys
  去重，stem==原词时单元素）；**读取失败 → `[原词]`**（硬要求，绝不返回空表）。
  `_targets_for_removal` 对候选名 × 节点（--node 路径）/ 候选名 × list_goals（--all
  路径）两路展开去重。`remove_goals` 连带清运行态改用**被删 goal 行的 profile 字段**
  （`gone["profile"]`，恒为 stem），不再用调用方原词。钉 4 条用例：展示名 remove 命中
  stem goal、**profile 文件已删仍能 remove**、--all 展示名可撤、展示名 remove 连带清
  model_state。变异验证：`names = [profile]`（归一退化）→ 恰红 3 条展示名用例。
- **要点**：
  - 归一裁决落地时把**同一实体的全部键位**列成清单逐个过：查库、写库、连带清理、
    事件 payload、下游文件名——本轮 set 侧三处上轮已修，remove 侧三处原样漏网。
  - 归一函数依赖外部源（文件/DB/RPC）时，必须显式设计**源不可用**分支：撤除类操作
    的语义是"按台账现状删"，归一只是**扩大命中面的优化**，优化失败必须退化为最小
    可用（原词单候选），绝不允许"归一失败 = 操作整体失败"。
  - 候选名 × 目标展开用 dict 保序去重（`dict.fromkeys`），别用 set——missing/removed
    清单的顺序会进 CLI 输出与测试断言，set 迭代序漂移是隐性 flaky 源。
  - 连带清理永远按**被删行的字段**取键，不按调用方输入：行内字段是系统写入口径
    （stem），调用方输入是用户口径（可能是展示名），两者等价的前提（归一）并不总成立。

## 演练/派生量与实跑分叉：上限检查排在 dry-run 计数之后；version 从原文 sha 而非最终落库 sha 派生

- **日期**：2026-09-05（M1 Task 5 fix round 2，实现者自提、控制器裁决 4 收编）
- **症状**：两处"演练与实跑/哈希链不同源"。① `set_goals` 的写库循环里
  `MAX_GOALS_PER_NODE` 检查在 `if dry_run: created += 1` **之后**——节点已达上限时
  dry-run 报"预计 created=1"，实跑却 skip（created=0）。Task 11/12 按 dry-run 结论给
  退出码/提示，运维看到"演练通过"，实跑 goal 却被静默跳过。② `--gpus` 合并改写
  YAML 后 `effective_sha` 变了，但 `enriched = {**source, ...}` 未覆盖 `version`，
  落库 `profile_version` 仍是**原文 sha** 派生的日期+13 位前缀——版本号的哈希段指向
  一份不存在于任何台账行的内容，"version ↔ sha ↔ yaml"单一哈希链断裂，version 的
  可追溯语义（Triton version policy）名存实亡。
- **根因**：① dry-run 分支是"提前 return/continue 的捷径"，捷径之后的校验对演练不可见
  ——凡影响"能不能创建"结论的检查（容量、上限、配额）都必须在捷径**之前**，捷径之后
  只剩真正的写库动作。② 覆盖式补丁（`{**source, "yaml": ..., "sha": ...}`）新增字段时
  漏了**由被覆盖字段派生的下游字段**：sha 换了，从 sha 派生的 version 不会自动跟着换。
- **解决**：上限检查整体移到 dry_run 计数之前（dry-run 下 `_count_for_node` 读当前台账，
  演练不写库，与紧随的实跑起点一致，结论必然相同）；`enriched` 显式
  `"version": profiles.default_profile_version(effective_sha)`。各钉一条用例：dry-run 与
  实跑对超限节点 created/skipped/reason 全同（monkeypatch 上限=1，避免插 512 行）；
  带 --gpus 时 `profile_version == default_profile_version(profile_sha)` 且哈希后缀互钉。
  变异验证：顺序换回 → 恰红 1 条（dry created=1≠0）；version 退回 source 值 → 恰红 1 条。
- **要点**：
  - dry-run 的实现纪律：**演练 = 实跑减去写库副作用**。所有改变"会不会创建"判定的
    校验必须位于 dry-run 捷径之前；dry-run 期间不得推进任何循环内累加器/写任何状态，
    使"演练 + 实跑"与"直接实跑"结论一致。CLI/REST 拿 dry-run 报告给退出码时，两侧
    同值是靠这条纪律保证的，不是靠文案。
  - 哈希链上任一环节被替换，其**全部派生环节**必须同步重算。写 `**source` 覆盖补丁时
    grep 被覆盖字段名在整个函数里的派生用途（sha→version、yaml→sha、name→goal_id 同族）。
  - 上限类常量进测试用 monkeypatch 压低（512→1），不要真造 512 行数据；被测的是
    **分支顺序**而非阈值本身。
  - 自提缺陷的诚实申报换来了本轮收编：实现者报告里"version 与 profile_sha 非同一哈希链，
    故未动"的待确认点，被评审升格为裁决——**"当前无消费者"不是放任派生链断裂的理由**，
    M1 内无人读 ≠ Task 11+ 的 dashboard 不读。

## 候选名归一的幻影组合进 missing：撤除成功却同时报"不存在"，假警报按 goal_id 而非节点计数

- **日期**：2026-09-05（M1 Task 5 fix round 3）
- **症状**：`remove_goals(profile=<展示名>, node_ids=["w-1"])` 撤除成功（`removed=
  ["qwen-fast@@w-1"]`），返回里却同时 `missing=["qwen-display@@w-1"]`，report 输出
  "已删除 1 个 goal；不存在 1 个"——removed 与 missing 自相矛盾，运维无法判断这次
  `goal remove` 到底成没成。round 2 落地时该行为曾被注释钉住为"missing 语义本就属实"，
  本轮评审推翻该裁决。
- **根因**：round 2 的 `_targets_for_removal` 按"候选名 × 节点"**全组合**展开目标集
  （stem 那份 + 展示名那份），而 set 侧只认 stem，展示名 goal_id 是**恒不落库的幻影
  键**；`missing = targets - removed` 的集合差不区分"真实目标未命中"与"归一算法自身
  产生的幻影"，幻影必然进 missing。计数维度也错了：goal 主键是 (profile,node)，一个
  节点对一次撤除请求只有"撤到/没撤到"两种结局，运维感知的成败单位是**节点**，不是
  候选名展开出的 goal_id 行数——按 goal_id 计数会让归一算法的内部实现细节泄漏进对外
  契约（Task 11 REST 原样透传 missing、Task 12 CLI 据此给提示/退出码）。
- **解决**：missing **按节点聚合**。删除循环记 `hit_nodes`（被删行的 node_id，来自
  `delete_goal` 返回值，天然归一）；--node 路径 `missing = 未命中节点 × names[0]`
  （stem 优先的规范键，每节点至多一条，重复 --node 经 `dict.fromkeys` 去重不重复计数）；
  --all 路径 targets 来自 `list_goals` 查库、无幻影组合，维持集合差（仅防 list/delete
  竞态）。`_targets_for_removal` 签名 `profile` → `names: list[str]`，候选名解析上移到
  `remove_goals` 供两处共用。改写 round 2 钉住旧行为的断言为 `missing == []` +
  report 不含"不存在"，新增 `test_remove_missing_aggregated_per_node` 钉"缺失节点按
  stem 各报一条 + 去重"。变异验证：聚合改回集合差 → 恰红 2 条；missing 键改用调用方
  原词（`goal_id_of(profile, n)`）→ 恰红 1 条。
- **要点**：
  - **归一/展开算法的副产物不能进对外契约**：为"扩大命中面"而做的候选展开（候选名 ×
    节点、别名 × 资源），其未命中组合是算法内部噪声，聚合回业务主键（本例 (profile,
    node)）再对外报告；否则每个归一优化都会自带一种新假警报。
  - 上一轮"钉住而非回避"的注释不是终审：被钉住的行为若让对外输出自相矛盾
    （removed 与 missing 同时非空且指向同一意图），下轮评审应重裁而非继承。判别标准：
    **missing 的粒度必须与操作对象的唯一键一致**，"查无此目标"里的"目标"是节点级 goal，
    不是候选名字符串拼出来的行。
  - 聚合修复要交代清楚**两条目标来源路径**：--node（组合展开）与 --all（查库）的
    missing 语义不同，前者必须聚合、后者本无幻影只需说明"保留集合差是为防 list/delete
    竞态"——这个理由必须写进注释，防止后人"统一两处实现"时把幻影差集重新引回 --node
    路径。
  - 命中判定用**被删行自带的主键**（`gone["node_id"]`），不用从 goal_id 反解或拿调用方
    node_ids 对照——反解要处理分隔符、对照要处理候选名错位，行内字段是零成本且必然
    正确的口径（同 round 2"连带清理按被删行字段"要点，本例是 node 维复用同一原则）。

## agent 心跳在 rt=None 时按 M0 形状带 `profiles: {}`，中心按"显式空集"全量覆盖 model_states 抹台账

- **日期**：2026-09-05（M1 终审 A-5 沉淀）
- **症状**：worker 侧 reconciler 尚未就绪（`rt=None`）的那一拍，`agent.collect_heartbeat`
  返回与 M0 **逐键一致**的基础形状，其中就含 `payload.profiles = {}`。中心
  `NodeRegistry.handle_heartbeat` 对 profiles 的三态口径是"**None=未上报（保留既有
  事实），{}=明确为空（照实覆盖）**"——`{}` 是合法业务值，`record_model_states(node,
  {})` 会把该节点全部在跑模型记录清空，dashboard 集体假 down，且 `_in_use_gpus`
  随即认为无卡占用，gate 放行冲突下发。
- **根因**：M0 逐键兼容的**刻意取舍**。v1 worker/老中心只认"payload 恒含 profiles 键"
  的形状，若 rt=None 时省略该键，等于对 M0 消费方改变协议形状；而"未知"语义在该
  协议里只能用 None/缺键表达，`{}` 已被"本机确实没有模型"占用。rt 存在但取数抛错的
  路径没有这个约束，所以 `collect_heartbeat` 用 `payload.pop("profiles")` 表达"未知"
  ——两条路径形状不同是设计而非疏漏。
- **缓解（常态窗口已消除）**：`webui/server.start_cluster_background` 的启动顺序刻意
  定为**先 reconciler 后 Agent**（见该函数 docstring）：reconciler 是"有状态可独立运行"
  的一方，Agent 是"尽力上报"的一方；顺序反了才会出现"Agent 已连上中心但本机还没有
  reconciler 实例"的稳态空窗，那一拍起每一拍心跳都带 `profiles:{}` 持续抹台账。顺序
  正确时 rt=None 至多出现在 reconciler 创建失败的极窄窗口，且该窗口内 Agent 同样
  启动失败（同一 try 块），不产生持续上报。
- **要点**：
  - **"空集合"与"未上报"必须是两个不同的线上传输值**，且协议升级期内老形状的
    缺省值只能占用其中之一。本例 M0 形状被迫把 `{}` 用作缺省，等于"缺省 = 明确的
    空"，于是"采集器没就绪"与"本机真的没模型"不可区分——新增"未知"语义时应扩展
    值域（None/缺键）而不是复用空集，v2 消毒层 `parse_heartbeat_v2` 已按此口径实现。
  - 中心侧对"全量覆盖"型字段必须核对**每一条生产写路径**能否产出覆盖性空值；
    只盯"None 分支有没有兜住"是不够的，`{}`/`[]` 走的是正常覆盖分支，任何
    "采集失败但返回空容器"的实现都会伪装成合法业务值。
  - 跨组件启动顺序本身就是数据正确性约束：当 A（Agent）的输出内容依赖 B（reconciler）
    的就绪状态时，先 B 后 A 的顺序要在启动函数注释里写明"反了会怎样"，否则
    重构时会被当作无害的语句调换掉。

## 隐式下界类测试常量（rounds=3）必须注释"为什么不能再低"，否则为提速缩减会静默失去判别力

- **日期**：2026-09-05（M1 终审 A-5/T10-② 沉淀）
- **症状**：`test_cluster_agent_v2.test_agent_reads_one_reply_per_result_frame` 用局部
  常量 `rounds = 3` 驱动"稳态循环收发"的观测轮数。该数值是**判别力下界**而非随意
  取值：读队列每轮净积压 1 帧的失衡形态，要到第 2~3 轮才会在 `(投递序号, 中心心跳数)`
  错位序列上显形；若后人觉得"3 轮太慢"改成 1，用例在缺陷实现下也照样全绿——红证
  静默退化成恒绿。
- **根因**：测试常量承担了"**最小可判别规模**"的隐式契约，但契约只存在于作者头脑中。
  循环轮数、重复次数、批量大小这类"取多少都行，但低于某值就测不出"的下界数值，
  与魔法数字同理：不解释来历，就必然被后人按"越小越快越好"的直觉改动。
- **解决**：终审裁决 T10-② 维持 rounds=3，并沉淀本条规范——凡取值为**刻意下界**的
  测试常量，注释必须交代三件事：① 低于该值为什么失去判别力（给出失衡形态最早
  在第几轮显形）；② 判据表达式在哪一轮开始有区分度；③ 该常量的增减必须与判据
  断言（如 `lag == [(k, k) for k in range(1, rounds + 1)]`）联动改写。
- **要点**：
  - 与"测试/覆盖"家族的其他教训同源：**断言的判别力是测试的全部价值**，凡削弱
    判别力的改动（缩轮数、放宽阈值、去掉反例数据）都不会让测试变红，只能靠注释
    与评审防御。
  - 下界数值优先写成"由判据推导"的形式（如判据本身就要求 ≥2 次观测才取 2+1），
    不要写裸字面量；无法推导时才写常量 + 完整注释。
  - 同类需要注释的隐式下界：等"至少 2 拍"的心跳轮数、验证"顺序稳定"所需的 ≥2 个
    反序元素、验证"去重生效"的重复次数 ≥2。单个元素/单轮循环证明不了任何
    "关系类"不变量。

## webui 读模型 YAML 按 name 拼路径，自动推导 name 与文件 stem 分叉必 404

- **日期**：2026-09-09
- **症状**：WebUI 模型详情页点「YAML」tab 显示 `Request failed with status code 404`。
  前端 `getModelYaml(name)` 打 `GET /admin/api/models/{name}/yaml`，后端点
  `get_model_yaml` 用 `models_dir / f"{name}.yaml"` + `rglob(f"{name}.yaml")` 找文件。
- **根因**：前端传的 `name` 是 **profile 自动推导名** `{group}-{engine}[-{variant}]`
  （如 `qwen2.5-0.5b-vllm`），而磁盘文件名是 **stem**（`qwen2.5-0.5b`）。二者分叉，
  `rglob("qwen2.5-0.5b-vllm.yaml")` 恒匹配不到任何文件 → 404。vllm/llamacpp 等
  非根目录成员（qwen3.8、deepseek-v4-flash 等）全部命中此坑；只有文件名恰好等于
  推导名的成员（如根目录 `models/xxx.yaml` 且 name==stem）才幸免。
- **根因（更深层）**：同一个 `/models` 路由组里，其它端点（详情/日志/状态）走
  `_find_profile(name)` → `profile.path`，而 yaml 端点却**手写了一套按 name 拼文件的
  glob**，等于同一资源两套定位逻辑——正是本文件"同名 profile 散落在多引擎子目录"
  那条警告的"写回显文本"半套逻辑。
- **解决**：`get_model_yaml` 也走 `_find_profile(name)` 拿 `profile.path`，直接
  `profile.path.read_text()`；`profile is None` 或 `path` 缺失才 404。与同组其它端点
  同口径，不再依赖"文件名 == 推导名"这个不成立的假设。
- **要点**：
  - 一个路由组内的同一资源，**定位逻辑必须单点复用**（`_find_profile` + `Profile.path`），
    不因"只是读个文本"就另写一份 glob——那份 glob 迟早与真实命名规则分叉。
  - "自动推导 name"与"文件 stem"是**两个体系**：推导名是 YAML 派生值（可含
    `-{engine}`），stem 是物理路径成分。凡拿一个去匹配另一个，都要先确认二者是否
    恒等；本文件的"cluster 寻址只认文件名""展示名归一只做在读侧"与本次 yaml 端点
    都是同族：**对外可寻址名 ≠ 内部物理名，去向内部体系时必须显式归一**。
  - 修复后不能再回头用 `PROJECT_ROOT / "models"` 手拼候选，否则下次新增子目录成员
    又 404（旧的"首个命中可保留"结论随之失效）。

## worker 每次心跳都刷 100+ 条"stem 冲突"WARNING 刷屏

- **日期**：2026-09-09
- **症状**：`launch-modelctl-webui.log` 里 `models/ 下 stem 冲突：...` 反复刷屏：每 30 秒
  （reconciler 循环）一轮，每轮 ~39 条（10+ 个 stem × 多个引擎），一小时内累积数千条
  同语义 WARNING；dashboard 与日志均被淹没，真实告警被噪声压到不可见。
- **根因**：`local_profile_paths` 在每个被忽略的候选文件上逐条 `logger.warning`，
  而 `Reconciler._collect`（826 行）每拍都调用它，全仓库 `models/` 下 9 个引擎目录
  各持 `qwen3.8.yaml`、`qwen2.5-0.5b.yaml` 等，是 M1 常态配置而非"病态"——
  "该告警一次"的告警器叠加 reconcile 的每拍节奏，把单条 WARNING 放大成"每 30s 打 39 次"。
- **解决**：
  1. 单条汇总 WARNING：`f"models/ 下 {n} 处 stem 冲突：{pair1}；{pair2}；……"`，
     列出全部冲突对（替代逐文件告警）。
  2. 进程级缓存：`_PROFILE_PATHS_CACHE[str(models_dir)] = (fp, {stem: Path})`，
     `fp` 是双层"目录树指纹"（顶层条目名 + 每个引擎目录内条目名，仅名称不含 mtime/size）。
  3. 目录未变动 → `dict(hit[1])` 直接返回，不重扫、不告警；目录变动 → 失效重扫，
     仅打一条 WARNING 并更新缓存。
  4. TDD：`_WarnCollector` 替身 `setattr(reconcile, "logger", ...)` 捕获 WARNING 次数；
     RED = 两个新用例（单条 + 缓存）失败，GREEN = 64/64（模块）+ 172/172（6 文件）。
- **要点**：
  - "每拍都调用"的循环契约，叠加"每命中都告警"的策略，会把"该告警一次"的商品
    放大成持续刷屏——告警频率 ≪ 业务频率才合理；高频调用路径上的告警必须有
    "已报告"去重窗口或进程级缓存。
  - 指纹用**名称**不用 mtime/size：Windows mtime 不可靠，原子重命名
    （`.tmp`→rename）可能改 size 但内容不变；名称是唯一稳定的"目录内容变化"信号，
    且对 reconcile 高频路径零额外 IO。
  - 同 stem 跨多引擎在此仓库是**合法常态**（9 个引擎目录 × 10+ 模型），不应视为
    病态；但告警仍保留（运维应能感知"哪些 stem 被忽略"），只是不再刷屏。
  - 监控功能轮询函数时用 per-call 例模式（`_WarnCollector` + `setattr`），避开
    loguru sink 配置与等级过滤差异，断言只数"调用次数 + 传入文本"。
- **SOP（运维触发条件）**：
  > **触发器**：若同一 worker 在 5 分钟内同时出现「『状态落盘失败』逐 30s 刷屏」
  > **叠加**「『stem 冲突』每排查一轮就被告警一次」（罕见，仅在中心频繁调整
  > models/ 目录结构时发生）→ 说明当前冷路径风险"不再冷"。
  >
  > **排查顺序**（自上而下）：
  > 1. `cache_dir` 磁盘/权限：`df -h <cache_dir>` 确认 <90% 占用且不在 FAT32；
  >    `whoami` + `icacls`（Windows）/`ls -ld`（Unix）确认可写。
  > 2. cache_dir 是否被挂到共享盘（NAS / IAM 挂载盘 / 网络映射盘），且该盘允许
  >    长时间只读（如配额被打满、网络中立性）→ 改回本机可写盘。
  > 3. models/ 目录是否出现了大量"非管理"改文件（比如手工编辑底层 profile yaml）
  >    → 确认是不是合法的视察意图；非预期改动应 `git status` + `git diff` 排查。
  > 4. 仍然无法定位 → 在本条下补一段"运维现场重现"附 worker passage 与
  >    `mtime` 序列，以便后续给出"是否改 `_save` /`_usable_overlay` 告警去重"的决策。
  >
  > **代码改造触发条件**：当以上 4 步都无法定位根因，或确认是"冷路径频繁变热"
  > 时，再把 `_save` / `_usable_overlay` 两处加入"5min 同 message 去重窗口"
  > （类似 `local_profile_paths` 的指纹门控），并附新增测试。在此之前，
  > 维持 monitor 状态即可，**不预防性修**。

## TUI Dashboard 列布局 adapter 只修 `len()` 没修 width 折叠

- **日期**：2026-09-10（T6 任务 2：80 列窄屏适配）
- **症状**：`main_dashboard.py` 在 80 列终端行末被截断，且不截断时整行右移；
  旧实现每行各自 `f"{x:<13}"` 按 ASCII 列宽补齐，CJK 双宽字符
  （如 profile 中文名 `Qwen3.5-397B-中文`）让列起始位置逐行漂移，
  叠加列宽常量未做窄屏折叠后，80 列宽度下 `total` 列直接砍出去。
- **根因**：两条规则同时缺位——
  1. **字符数 ≠ 显示宽度**：`f"{x:<13}"`/`ljust`/`len()` 把 CJK 当 1 列补空格，
     而 `pad_width` 按 2 列补齐——同一个"对齐"在两种算法下结果不一致，
     正好命中 T5 `gate/CLI 报告按 len() 取列宽，中文 node_id 让整表右移错位`
     的**同一病灶**在 TUI 行的复现。
  2. **列宽常量只按 full 档一次性取值**：`COL_RATE=13`、`COL_TOTAL=12`
     在 80 列下需要进一步压到 7/8（尤其是 VRAM 列动态宽度时，rate/total 再不
     压窄总宽必爆）。
- **解决**：单点封装（与 T5 闸宽同口径，集中在 `_layout` 区段）：
  - 列宽常量分档：`COL_RATE_FULL=13 / COL_RATE_NARROW=7`、
    `COL_TOTAL_FULL=80 / COL_TOTAL_MEDIUM=71 / COL_TOTAL_NARROW=65`。
  - 布局常量：`LAYOUT_FULL / MEDIUM / NARROW`（按显示宽锁定各列宽）。
  - `_layout_for_width(width) -> mode`：单一 adapter 决定用哪套列宽，
    行渲染侧不再各自 if/elif。
  - `_profile_row(state, idx, profile, width, theme, mode)`：所有 f-string
    改为 `pad_width(x, COL_*)`，vram 不 fold（`display_width(vram_str)`
    取实际长度），末尾 `rest = width - (total - VRAM + vram_width)` 补齐，
    超宽不截断（仓库规则）。
  - 测试钉的是**不变量**：`display_width(line) == width` 且 200/120/100/80
    四档都通过（80 档 fix 后也升级为 `==`），数据里含 CJK 行（不能让纯 ASCII 假绿）。
- **要点**：
  - 与 T5 `gate 按 len() 取列宽` 同主题不同侧：**列宽常量与列宽 adapter 都要分档**，
    只修一半依然错位。TUI 行的每个 f-string 对齐点都必须走
    `display_width`/`pad_width`，不允许多处各自取"最优"列宽。
  - 不变量断言 > 空格数断言：钉 `display_width(line) == width` 等价于钉
    "每行末列位置一致"，且数据必须含真实双宽字符；数空格会在改文案/
    改列宽时刻刻得改用例。
  - 折叠切换点**集中**：`_layout_for_width` 是唯一宽度阈值入口，dashboard
    行渲染侧不再自定义 mode 阈值——宽度阈值与 theme 切换解耦后可独立
    加档（如追加 `LAYOUT_ULTRA_NARROW`）而不需动行渲染。
  - 动态列（vram）不固定宽度：`display_width(vram)` 取实际值参与
    rest 补齐，让 VRAM 列在数据宽度变化时自适应不被截断；固定列才
    进 `COL_*` 常量分档。
