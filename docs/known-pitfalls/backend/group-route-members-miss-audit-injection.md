# create_app 注入循环漏掉 groups 成员 → 家族路由的成功请求整条不落审计

> **日期**：2026-09-12
> **涉及模块**：`core/gateway.py`（`create_app` / `build_registry` / `build_groups` / `resolve_model`）
> **级别**：P1（真实引擎冒烟 S6 发现：`data/audit/*.jsonl` 只有 401 短包，3 条 200 成功请求静默丢失）

## 一句话概述

生产入口 `main()`（以及 `modelctl webui`）调 `create_app()` **不传** registry/groups，走自动构建：`build_registry()` 与 `build_groups()` 会为**同一 profile 各 new 一份 GatewayModel**。而 `create_app` 末尾的注入循环只遍历 `registry.values()` 给实例挂 `audit_log` / `collector`——家族路由（`resolve_model` 的 group_route）命中的是 **groups 里的那份实例**，其 `audit_log` 恒为 `None`，成功路径的审计写入条件 `if self_audit_log is not None` 直接跳过，整条请求无审计。

## 为什么 401 有审计、200 没有（症状不对称的根源）

- 401/429 短路审计用的是 `create_app` 的**闭包变量 `audit_log`**（`proxy` 拒绝分支直接 `audit_log.record(...)`）——与 target 实例无关，所以永远有。
- 成功路径审计取的是 **`target.audit_log`**（解析出的 GatewayModel 实例字段）——groups 成员没被注入，字段是 None，静默跳过。

日志侧完全看不出异常：`data/logs/launch-llm-gateway.log` 正常打印 `OpenAI 上游路由 model='m' -> xxx（group_route）` 与响应摘要，只有审计文件行数对不上。**这类"按实例注入依赖"的模式，凡是存在同名对象多实例，注入点必须覆盖所有可达集合。**

## 同时受漏的还有 collector

同一循环里 `model.collector = get_collector(...)` 也只覆盖 registry。groups 成员 `collector=None` → 网关侧 token 累计（vLLM 这类 metrics 恒 0 的引擎）对家族路由流量不生效。冒烟场景 llamacpp 走引擎原生 `/metrics` 轮询（stats 服务）才被掩盖。

## 修复

`create_app` 注入段改为对 `registry.values() + groups 全部成员` 的并集（按 `id()` 去重，同一实例不重复建 collector）逐一注入。注入面收敛于 `create_app` 一处：`build_groups` 生产上仅此一个调用点，webui 走 `create_app(admin=True)` 同一路径。

## 测试

`tests/test_audit.py::test_group_route_success_writes_audit`：patch `build_registry` / `build_groups` 返回**不同实例**（复现生产自动构建），`create_app()` 无参 + MockTransport，打 `/v1/chat/completions`（model=组名）断言 200 且审计落盘 `status_code=200 / model=成员名 / auth=ok`。**negative control 已验**：stash 掉修复后恰好该 1 条红（"网关未产生审计文件"）。

## 通用法则

- "创建后注入（setter injection）"的依赖，注入循环的遍历集合必须等于**运行时全部可达对象集合**；构建器多（registry/groups 各 new 一份）时尤其容易漏。
- 审计/计量这类旁路写入若"目标对象上没有依赖就静默跳过"，故障形态是**数据缺失而非报错**——观测上要靠"成功请求数 == 审计行数"这类对账断言兜底，日志帮不上忙。
