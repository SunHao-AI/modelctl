# CLAUDE.md

## 遵守规范

### 数据库操作限制

- 严禁 Agent 对任何线上生产数据库执行 DDL 操作（含 DROP、TRUNCATE、ALTER、CREATE 等）。
- 针对本地或 Docker 容器内的开发环境，DDL 操作同样被禁止自动执行。
- 业务相关的 DML 操作（INSERT、UPDATE、DELETE）不受此限，允许 Agent 直接操作。
- 若确需执行 DDL，Agent 仅负责生成对应的 SQL 语句并展示给用户，最终由用户手动完成或明确授权后执行。

### 前端网页设计

- 网页中显示均为虚拟 ID，不显示真实 ID。

### 时间格式

- 所有时间均采用 `YYYY-MM-DD HH:mm:ss` 格式。

### 问题总结机制（渐进式披露）

每次完成 UI/样式调整或修复 Bug 后，自动将问题沉淀到 `docs/known-pitfalls/` 目录，采用**渐进式披露**策略：

- **摘要层**：`docs/known-pitfalls/README.md` 维护总览索引，仅记录问题标题、分类、日期和一句话描述。
- **详情层**：每个分类下按语义主题聚合为少量文件（命名 `<主题>.md`），每个问题在文件内以 `## <问题标题>` 条目呈现，各文件头部记录"原始单文件已并入本文件归档"的说明以保留溯源信息，完整记录根因、解决方案与代码示例；单主题超过约 40 条即按更细粒度拆分，避免单文件过载。
- **按需加载**：后续对话仅加载摘要层，需要具体方案时按需读取详情文件。

## 代码风格

### 前端（Vue 3 + Element Plus）

- 使用 ES6+ 语法，优先使用函数式组件 + `<script setup>` 组合式 API。
- 组件命名采用 PascalCase，`.vue` 组件内用 `<script setup name="Xxx">` 显式声明 name，且与路由 `route.name` 一致以配合 `keep-alive`。
- 文件/目录命名采用 kebab-case（约定俗成模块入口 `views/<module>/index.vue` 除外）。
- CSS 类名采用 BEM 规范；**修改子组件内部元素样式时必须用 `:deep()`，否则 scoped 选择器失效**。
- 网络请求统一走 `src/utils/request.js`；并发/大文件上传需显式 `repeatSubmit: false` 绕过防重复提交校验。
- 组件库 JS API（`ElMessage`、`ElMessageBox` 等）不在 unplugin-auto-import 范围，必须显式 import。
- 长文本尽量展示完整，配合 el-tooltip；表格列宽可自适应用 `min-width` 而非 `width`。
- 后端已格式化的字段（版本号、时间、文件大小）前端直接展示，不再二次格式化/拼接，统一由一端负责。

### 后端（FastAPI / Python 3.10+）

- 遵循 PEP 8；接口命名采用 RESTful 规范；统一用 try-except 处理异常并记录日志（logger）。
- Pydantic 模型：处理"未显式提供时取默认/继承"的字段时，用 `model_dump(exclude_unset=True)`，并显式补全嵌套模型缺省字段。
- SQLAlchemy JSON 列嵌套修改必须重建对象链，不能原地改，否则不触发 UPDATE。
- PyJWT 的 `iat/exp` 用 `int(time.time())` 秒数；pyotp 周期参数名是 `interval` 而非 `period`。
- 提供"列表 + 单条 CRUD"的资源，GET 列表 / GET 单条 / POST / PUT / DELETE 五方法必须在同一路由组同时定义，避免 405/404。
- 双通道鉴权（Bearer + 客户端签名）的依赖需嗅探请求头，不能强要求 Bearer。

### 数据库（MySQL）

- SQL 关键字使用大写（SELECT、INSERT、UPDATE、DELETE）；表名与字段名使用 snake_case。
- 所有查询必须包含明确的字段列表，禁止使用 `SELECT *`；复杂查询需添加注释说明业务逻辑。
- 含中文的 SQL 文件顶部必须写 `SET NAMES utf8mb4;`，避免 latin1 客户端连接导致二次编码。
- 迁移脚本按"先加列（带默认值）→ 回填 → 后加约束"顺序执行，重复执行会失败，属预期，执行前备份目标表。
- `AUTO_INCREMENT` 列必须显式声明 `PRIMARY KEY (id)`（MySQL 强制，SQLite/ORM 隐式不暴露）。
- `docker/init/01-schema.sql` 必须与 ORM 模型同步演进，且必须合入顶层 init 脚本（entrypoint 忽略子目录）。

### CLI / 终端与日志输出对齐（CJK 双宽）

等宽终端中 ASCII 占 1 列、CJK（中日韩）字符占 **2 列**；Python 的 `f"{x:<N}"` / `str.ljust` / `len()` 按字符数计算（CJK 也计 1），导致中英混排**视觉错位**。**终端输出与日志文件输出同理**——凡字段可能含 CJK 的输出，无论是 CLI 指令、表格、KV 行，还是 loguru 等写文件的日志记录，**必须**用 `display_width` + `pad_width` 对齐，禁止直接 `f"{x:<N}"` / `str.ljust` / `len()`。

- 提供 `display_width(text) -> int`（按 Unicode codepoint 判宽：CJK/谚文/全角符号 → 2，其余 → 1）与 `pad_width(text, width, *, align="left")`（按**显示宽度**补齐，超宽**不截断**，只留 0 余量）。
- 同一区块所有行共用同一个 `width`，取整块所有键 `display_width` 的最大值（可再加余量），不要每行各自定义。
- 全角符号 `（）：，、·`（U+FF08 等）也是 2 列，别只当中文字符才双宽。
- 含 ANSI 码的文本：先算**纯文本宽度**再上色，再参与补齐；不要对带码文本调 `pad_width`（会把 ANSI 码当字符计）。
- **日志文件输出同样存在对齐问题，不只限于终端屏幕**。loguru 等写文件/写终端的记录，**不要**用模板 `{level:<20}`（按 `len()` 计算会错位），应在调用侧用 `align_row` 预先按 `display_width` 对齐 message，loguru 只负责时间/级别前缀；跨 task/跨进程（PID 切换）时 `LogCols` 会残留宽度峰值，需在切换时 `reset`。

## 已知陷阱

### 前置说明

以下文件采用**渐进式披露**结构，详细问题记录在各分类子目录下的主题聚合文件中，请按需查看：

- `docs/known-pitfalls/README.md` —— 总览索引（优先阅读）
- `docs/known-pitfalls/frontend/` —— 前端常见问题详情
- `docs/known-pitfalls/backend/` —— 后端常见问题详情
- `docs/known-pitfalls/database/` —— 数据库常见问题详情

### 快速检查清单

- □ 是否在 UI 中使用了真实 ID？
- □ 时间格式是否为 `YYYY-MM-DD HH:mm:ss`？
- □ 是否执行了未经授权的 DDL 操作？
- □ 接口是否做了幂等性处理？
- □ 是否处理了所有异常情况？
- □ CLI/日志/表格等可能含 CJK 的输出，是否用 `display_width`+`pad_width` 而非 `len()` 对齐？

## 成功模式

- **问题自动沉淀**：每次修复问题后，自动按渐进式披露策略写入 `docs/known-pitfalls/`，形成可持续积累的知识库。
- **Token 优化**：通过摘要索引 + 详情文件分离，降低长期 Token 占用，提升检索效率。
- **知识复用**：新问题优先检索摘要索引，快速判断是否已有解决方案，避免重复踩坑。
- **CJK 对齐防护**：凡 CLI/日志/表格字段可能含中文，一律走 `display_width`/`pad_width` 统一封装，避免 `len()`/f-string 对齐错位。