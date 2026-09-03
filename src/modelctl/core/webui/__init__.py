#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ===============================================================================
# @File   : src/modelctl/core/webui/__init__.py
# @IDE    : VSCode
# @Author : SunHao
# @Email  : 2865467769@qq.com
# @Date   : 2026/9/2 10:00
# @Desc   : Web UI 管理 API 子包
# ===============================================================================

"""modelctl Web UI 管理 API 路由。

本包提供 /admin/api 后端：FastAPI 管理路由子包（鉴权、任务流、模型/服务/环境/
体检/审计/配置端点）。FastAPI 等可选依赖在 create_admin_router() 内部延迟导入，
未安装 gateway extra 时导入本包不会报错。

公共 API：
- admin_auth（require_auth / optional_auth / is_valid_key / mask_key）
- admin_tasks（Task / TaskManager）
- create_admin_router() 聚合全部子路由
"""
