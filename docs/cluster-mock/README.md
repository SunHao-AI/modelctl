# modelctl 集群 · 单机 mock 两节点

> 目标：在本机（D:\WorkPlace\Pycharm\modelctl）同时模拟**两个 modelctl 节点**
> （center + worker），跑通 M0–M2 分布式管理面：节点注册/心跳/lease、目标
> goal 下发/推进/删除/重试、profile 下发同步/机型校验/远程启停、节点治理、
> 故障注入与退役。
>
> **为什么必须两份独立目录**：`envfile.PROJECT_ROOT` 由
> `Path(__file__).parents[3]` 推导（不可用 `MODELCTL_PROJECT_ROOT` env 覆盖），
> 同一份装在两个 venv 会指向同一份 `data/cache/cluster-meta.db`，
> 两个 webui 进程会踩踏同一份 SQLite 台账。**所以**：center = 开目录，
> worker = mock 副本目录。

## 一步到位（人工执行一次即可）

```powershell
cd D:\WorkPlace\Pycharm\modelctl
powershell -ExecutionPolicy Bypass -File docs\cluster-mock\worker_copy.ps1
```

## 详细步骤与断言

见 [../cluster-mock-test-steps.md](../cluster-mock-test-steps.md)（T0–T9 全链路，
每步都标了"代码/人工"验证点 + 手动诊断表 + 跨机复刻方法）。

## 回退

```powershell
# center 改回 solo
Set-Content D:\WorkPlace\Pycharm\modelctl\.env -Value ((Get-Content D:\WorkPlace\Pycharm\modelctl\.env) -replace "^CLUSTER_ROLE=both","CLUSTER_ROLE=solo" -replace "^CLUSTER_LAN=lan-a","") -Encoding UTF8

# 删 worker mock
Remove-Item -Recurse -Force D:\WorkPlace\Pycharm\modelctl-mock-worker
```
