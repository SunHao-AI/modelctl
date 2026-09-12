# start_profile 启动失败不归还 GPU 锁 → 卡位对其它 profile 永久不可用

> **日期**：2026-09-12
> **涉及模块**：`core/all_service.py`（`start_profile`）、`core/gpu_lock.py`、`engines/*.py`（`check_requirements`）
> **级别**：P1（GPU 互斥静默失效 + 卡位永久占死；与 README:377「停止时自动释放」承诺相反）

## 一句话概述

`acquire_gpu_lock` 在 9 个引擎的 `check_requirements()` 末步落锁，owner 写成**当前常驻进程**的 pid（CLI / webui worker）；此后 `start_profile` 里 `pre_start` / `build_command` / `start_detached` 之前的任何失败、以及"健康检查失败且后端确认已死"这两类出口，**都不归还锁**。失败后没有任何东西持卡，但锁还在，卡位对所有其它 profile 永久不可用。

## 为什么 stale 清理救不了

`_read_lock`（`gpu_lock.py:49-53`）只在 `is_pid_alive(pid)` 为假时才 `unlink`。而 owner pid 是：

- **venv 路径**：`acquire` 写 `os.getpid()` = CLI/worker 进程。webui worker 是常驻的，进程不死 → `is_pid_alive` 恒真 → 永不 stale。
- **docker 路径**：按设计**不**把 owner 重绑到容器（`all_service.py:211-214`、`vllm.py:519` 注释——绑到秒退的 `docker run` 客户端反而会让锁被误删），所以 owner 恒为常驻 worker → 容器秒退后锁既不 stale 也没人清。

即"失败即泄漏、且不可自愈"。

## 与 stop 路径的口径差

`stop_instance` / `stop_docker_instance`（`process.py:419-424 / 340-344`）**无条件** `release_gpu_lock`。所以"起成功后再停"能回收，唯独"起失败"这条路是漏的——README:377 说"停止时自动释放"是对的，缺的是"启动失败也要释放"。

## 修复（按"后端是否在持卡"分流，三处出口）

`start_profile` 内新增 `_release_gpu_lock_if_idle()`（吞异常、只 warning，不掩盖原始启动错误），在三处调用：

1. `pre_start` 抛 `RequirementError`（拉镜像 / 下载失败）→ 后端从未拉起，归还。
2. `launch` 段在 `start_detached` **之前**抛（`build_command` 出错）→ 用 `launched` 标志区分，未拉起才归还；`start_detached` 之后抛（tee / 锁重绑段）进程可能已起并持卡，**保守保留**交给 stop。
3. 健康检查失败且 `adapter.backend_dead()` 为真（docker 容器秒退主场景）→ 归还。

**反向不变式**（同样重要）：健康检查"超时但进程/容器仍活"时**不得**归还——卡还被引擎占着，还了会让另一模型撞进同一张卡，正是本项目最怕的"互斥静默失效"。`backend_dead()` 的保守语义（`docker_container_alive` 只有 inspect 明确 not-found/Exited 才判死）保证只在"确证空卡"时归还。

## 测试

`tests/test_all_service.py` 5 条回归钉，正反两向都钉：
- 归还：`test_start_pre_start_failure_releases_gpu_lock` / `test_start_build_command_failure_releases_gpu_lock` / `test_start_dead_backend_releases_gpu_lock`
- 保留：`test_start_health_timeout_alive_backend_keeps_gpu_lock` / `test_start_ok_keeps_gpu_lock`

夹具 `_Locking(_FakeAdapter)` 在 `check_requirements` 里真调 `acquire_gpu_lock`（配 `CACHE_DIR` 隔离），断言 `list_gpu_locks()` 是否为空。**negative control 已验**：stash 掉修复后恰好 3 条"应归还"用例红、2 条"应保留"用例绿。
