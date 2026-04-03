#!/usr/bin/env python3
"""测试OpenClaw会话Edit diff行号功能"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from file_line_cache import parse_read_result, find_edit_start_line
from check_agent_sessions import _compute_diff_with_lines, _backfill_file_cache_from_session


def test_openclaw_edit_without_prior_read():
    """测试OpenClaw会话中没有先Read就Edit的情况（使用相对行号）"""
    old_str = """import { RedisService } from '../../modules/redis/redis.service';

/**
 * 权限守卫 - 基于RBAC模型的权限检查
 *
 * 功能：
 * 1. 从JWT token中提取用户角色
 * 2. 从数据库查询角色的权限列表
 * 3. 检查用户是否有访问资源所需的权限
 * 4. 使用Redis缓存优化性能
 */"""

    new_str = """import { RolePermission } from '../../modules/user/entities/role-permission.entity';

/**
 * 权限守卫 - 基于RBAC模型的权限检查
 *
 * 功能：
 * 1. 从JWT token中提取用户角色
 * 2. 从数据库查询角色的权限列表
 * 3. 检查用户是否有访问资源所需的权限
 *
 * TODO: 后续添加Redis缓存优化
 */"""

    file_cache = {}  # 空缓存，模拟没有Read的情况
    file_path = '/path/to/permissions.guard.ts'

    # 查找起始行号（应该返回0，因为没有缓存）
    start_line = find_edit_start_line(file_cache, file_path, old_str)
    assert start_line == 0, f"Expected start_line=0, got {start_line}"

    # 计算diff（使用相对行号）
    old_lines = old_str.split('\n')
    new_lines = new_str.split('\n')
    diff_ops = _compute_diff_with_lines(old_lines, new_lines, start_line)

    # 验证行号是相对的（从1开始）
    assert diff_ops[0] == ('del', 1, None, "import { RedisService } from '../../modules/redis/redis.service';")
    assert diff_ops[1] == ('add', None, 1, "import { RolePermission } from '../../modules/user/entities/role-permission.entity';")

    print("✓ test_openclaw_edit_without_prior_read passed")


def test_openclaw_edit_with_prior_read():
    """测试OpenClaw会话中先Read再Edit的情况（使用真实行号）"""
    # 模拟Read结果（纯文本，无行号前缀）
    read_result = """import {
  Injectable,
  CanActivate,
  ExecutionContext,
  ForbiddenException,
  Logger,
} from '@nestjs/common';
import { Reflector } from '@nestjs/core';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { PERMISSIONS_KEY } from '../decorators/permissions.decorator';
import { RedisService } from '../../modules/redis/redis.service';

/**
 * 权限守卫 - 基于RBAC模型的权限检查
 *
 * 功能：
 * 1. 从JWT token中提取用户角色
 * 2. 从数据库查询角色的权限列表
 * 3. 检查用户是否有访问资源所需的权限
 * 4. 使用Redis缓存优化性能
 */"""

    file_path = '/path/to/permissions.guard.ts'

    # 解析Read结果并缓存
    file_cache = parse_read_result(read_result, file_path=file_path)
    assert file_path in file_cache, "File should be cached"
    assert len(file_cache[file_path]) > 0, "Cache should have lines"

    # Edit的old_string（从第12行开始）
    old_str = """import { RedisService } from '../../modules/redis/redis.service';

/**
 * 权限守卫 - 基于RBAC模型的权限检查
 *
 * 功能：
 * 1. 从JWT token中提取用户角色
 * 2. 从数据库查询角色的权限列表
 * 3. 检查用户是否有访问资源所需的权限
 * 4. 使用Redis缓存优化性能
 */"""

    new_str = """import { RolePermission } from '../../modules/user/entities/role-permission.entity';

/**
 * 权限守卫 - 基于RBAC模型的权限检查
 *
 * 功能：
 * 1. 从JWT token中提取用户角色
 * 2. 从数据库查询角色的权限列表
 * 3. 检查用户是否有访问资源所需的权限
 *
 * TODO: 后续添加Redis缓存优化
 */"""

    # 查找起始行号（应该找到第12行）
    start_line = find_edit_start_line(file_cache, file_path, old_str)
    assert start_line == 12, f"Expected start_line=12, got {start_line}"

    # 计算diff（使用真实行号）
    old_lines = old_str.split('\n')
    new_lines = new_str.split('\n')
    diff_ops = _compute_diff_with_lines(old_lines, new_lines, start_line)

    # 验证行号是真实的（从12开始）
    assert diff_ops[0] == ('del', 12, None, "import { RedisService } from '../../modules/redis/redis.service';")
    assert diff_ops[1] == ('add', None, 12, "import { RolePermission } from '../../modules/user/entities/role-permission.entity';")

    print("✓ test_openclaw_edit_with_prior_read passed")


def test_openclaw_backfill_from_session():
    """测试从会话历史回溯查找Read结果"""
    import check_agent_sessions

    # 模拟会话消息
    check_agent_sessions._session_messages_cache = [
        # 消息1: Read工具调用
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "name": "read",
                        "arguments": {
                            "path": "/path/to/test.py"
                        }
                    }
                ]
            }
        },
        # 消息2: Read工具结果
        {
            "type": "message",
            "message": {
                "role": "toolResult",
                "toolName": "read",
                "content": [
                    {
                        "type": "text",
                        "text": "def hello():\n    print('Hello, World!')\n\ndef goodbye():\n    print('Goodbye!')"
                    }
                ]
            },
            "toolUseResult": {
                "file": {
                    "filePath": "/path/to/test.py",
                    "content": "def hello():\n    print('Hello, World!')\n\ndef goodbye():\n    print('Goodbye!')"
                }
            }
        },
        # 消息3: Edit工具调用
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "name": "edit",
                        "arguments": {
                            "path": "/path/to/test.py",
                            "oldText": "def hello():\n    print('Hello, World!')",
                            "newText": "def hello(name):\n    print(f'Hello, {name}!')"
                        }
                    }
                ]
            }
        }
    ]

    # 模拟Edit时file_cache为空
    file_cache = {}
    current_msg = check_agent_sessions._session_messages_cache[2]
    target_file_path = "/path/to/test.py"

    # 回溯查找Read结果
    _backfill_file_cache_from_session(current_msg, file_cache, target_file_path)

    # 验证file_cache被填充
    assert target_file_path in file_cache, "File should be in cache after backfill"
    assert len(file_cache[target_file_path]) == 5, f"Expected 5 lines, got {len(file_cache[target_file_path])}"

    print("✓ test_openclaw_backfill_from_session passed")


if __name__ == '__main__':
    test_openclaw_edit_without_prior_read()
    test_openclaw_edit_with_prior_read()
    test_openclaw_backfill_from_session()
    print("\n✅ All OpenClaw Edit tests passed!")
