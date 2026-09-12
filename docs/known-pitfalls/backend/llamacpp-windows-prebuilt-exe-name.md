# llama.cpp Windows 预编译包的 llama-server.exe 找不到 → 误报"缺少 cmake"要求编译

> **日期**：2026-09-12
> **涉及模块**：`engines/llamacpp.py`（`find_server` / `pre_start`）
> **级别**：P1（真实引擎冒烟 S1 发现：Windows 上 prebuilt 路径整条不可用，报错误导为缺编译工具链）

## 一句话概述

`find_server()` 只找**无扩展名**的 `llama-server`，而 llama.cpp 官方 Windows Release 包里是 `llama-server.exe`、且解压后位于 source **根目录**（不在 `build/bin`）。找不到产物 → `pre_start` 落入"需要编译"分支 → 报 `RequirementError：缺少 cmake`。用户机器上产物其实早已就绪，报错完全误导；而 Windows 上从源码编译 llama.cpp 实际近乎不可行（依赖 vcpkg/Ninja/MSVC 工具链），**prebuilt 是 Windows 唯一可用路径**，这个缺陷等于 Windows 引擎直接不可用。

## 与设计注释的矛盾

`pre_start` 原注释写明"产物已就绪则不校验 cmake"，但判据硬编码 `source/build/bin/llama-server`，与 `find_server` 的多布局搜索脱节——注释说的能力和代码做的不是一件事。

## 修复

1. `_server_names()`：`os.name == "nt"` 时返回 `["llama-server.exe", "llama-server"]`，POSIX 保持 `["llama-server"]`。
2. `find_server()`：按 `build/bin` → source 根 × 候选名逐一搜索（兼容"编译产物在 build/bin"与"Release 包扁平解压在根"两种布局）。
3. `pre_start` 编译判据改为 `if not find_server(source).is_file():`——判定与定位共用同一函数，注释与实现对齐。

## 测试

`tests/test_engines_llamacpp.py`：
- `test_find_server_returns_platform_exe_name`：monkeypatch `os.name`，钉 build/bin exe、根目录 exe、POSIX 无后缀三种布局；
- `test_pre_start_windows_prebuilt_root_skips_cmake`：根目录 `llama-server.exe` + `which` 全 None，`pre_start` 不得抛缺 cmake，且启动命令指向 `.exe`。

## 通用法则

跨平台的"可执行文件定位"必须把**平台后缀**与**发行包布局**当作两维独立变量枚举；"产物存在性判定"与"产物路径解析"必须是同一个函数，否则判据分支与真实能力脱节、报错方向全错。
