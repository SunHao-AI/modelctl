# 已知问题索引（渐进式披露）

> 摘要层：仅记录标题、分类、日期与一句话描述。需要完整根因/方案时按需读取详情文件。

| 日期 | 分类 | 标题 | 一句话描述 | 详情 |
|---|---|---|---|---|
| 2026-09-03 | 构建 / 依赖 | uv 的 `default = true` 会让镜像源变成最低优先级 | 给 `[[index]]` 加 `default = true` 是降到兜底位而非设为主源，解析流量全落官方源。 | [build/uv-index-and-download.md](build/uv-index-and-download.md) |
| 2026-09-03 | 后端 / 运行时 | 进程未设置时区，日志与审计时间比预期早 8 小时 | 全项目隐式本地时间继承宿主 OS 时区；用标准 `TZ` 覆盖进程/子进程/容器三条路径（默认 Asia/Shanghai），含 Windows 写 `TZ` 污染子进程成 +0100 的坑。 | [backend/timezone.md](backend/timezone.md) |

## 目录约定

- `frontend/` —— 前端（Vue 3 + Element Plus）常见问题
- `backend/` —— 后端（FastAPI / Python）常见问题
- `database/` —— 数据库（MySQL）常见问题
- `build/` —— 构建与依赖（uv / 打包）常见问题

每个分类下按语义主题聚合为少量 `<主题>.md`；单主题超过约 40 条即按更细粒度拆分。
