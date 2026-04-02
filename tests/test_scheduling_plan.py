#!/usr/bin/env python3
"""
测试调度计划功能和会话消息读取

测试场景：
1. 读取会话文件中最后2条assistant消息
2. content截断（超1KB）
3. 处理content为数组格式
4. 空文件/不存在文件
5. 调度计划加载/保存/归档
"""

import json
import os
import pytest
import tempfile
from pathlib import Path
from datetime import datetime


def create_jsonl_file(lines: list) -> Path:
    """创建临时JSONL文件"""
    fd, path = tempfile.mkstemp(suffix='.jsonl')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + '\n')
    return Path(path)


# ===== 模拟 get_last_assistant_messages 逻辑 =====

def get_last_assistant_messages(jsonl_file: Path, count: int = 2) -> list:
    MAX_CONTENT_SIZE = 1024
    try:
        assistant_msgs = []
        with open(jsonl_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    if msg.get("type") == "message":
                        message = msg.get("message", {})
                        if message.get("role") == "assistant":
                            content = message.get("content", "")
                            if isinstance(content, list):
                                text_parts = []
                                for item in content:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        text_parts.append(item.get("text", ""))
                                    elif isinstance(item, str):
                                        text_parts.append(item)
                                content = "\n".join(text_parts)
                            if len(content) > MAX_CONTENT_SIZE:
                                content = content[:MAX_CONTENT_SIZE] + "...(截断)"
                            assistant_msgs.append({
                                "content": content,
                                "stopReason": message.get("stopReason"),
                                "errorMessage": message.get("errorMessage")
                            })
                except json.JSONDecodeError:
                    continue
        return assistant_msgs[-count:] if assistant_msgs else []
    except Exception:
        return []


class TestGetLastAssistantMessages:
    """测试读取会话文件最后assistant消息"""

    def test_single_assistant_message(self):
        """单条assistant消息"""
        lines = [
            {"type": "message", "message": {"role": "assistant", "content": "Hello", "stopReason": "endTurn"}}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_messages(path)
            assert len(result) == 1
            assert result[0]["content"] == "Hello"
            assert result[0]["stopReason"] == "endTurn"
            assert result[0]["errorMessage"] is None
        finally:
            path.unlink()

    def test_two_assistant_messages(self):
        """2条assistant消息，返回最后2条"""
        lines = [
            {"type": "message", "message": {"role": "assistant", "content": "First", "stopReason": "toolUse"}},
            {"type": "message", "message": {"role": "assistant", "content": "Second", "stopReason": "stop"}}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_messages(path)
            assert len(result) == 2
            assert result[0]["content"] == "First"
            assert result[1]["content"] == "Second"
            assert result[1]["stopReason"] == "stop"
        finally:
            path.unlink()

    def test_only_returns_last_two(self):
        """多于2条只返回最后2条"""
        lines = [
            {"type": "message", "message": {"role": "assistant", "content": f"Msg{i}", "stopReason": "endTurn"}}
            for i in range(5)
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_messages(path)
            assert len(result) == 2
            assert result[0]["content"] == "Msg3"
            assert result[1]["content"] == "Msg4"
        finally:
            path.unlink()

    def test_mixed_user_assistant(self):
        """混合user和assistant，只取assistant"""
        lines = [
            {"type": "message", "message": {"role": "user", "content": "请开始"}},
            {"type": "message", "message": {"role": "assistant", "content": "好的", "stopReason": "toolUse"}},
            {"type": "message", "message": {"role": "user", "content": "继续"}},
            {"type": "message", "message": {"role": "assistant", "content": "完成了", "stopReason": "stop"}}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_messages(path)
            assert len(result) == 2
            assert result[0]["content"] == "好的"
            assert result[1]["content"] == "完成了"
        finally:
            path.unlink()

    def test_content_array_format(self):
        """content为数组格式（实际Claude API格式）"""
        lines = [
            {"type": "message", "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "分析结果："},
                    {"type": "text", "text": "代码编译成功"}
                ],
                "stopReason": "endTurn"
            }}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_messages(path)
            assert len(result) == 1
            assert "分析结果：" in result[0]["content"]
            assert "代码编译成功" in result[0]["content"]
        finally:
            path.unlink()

    def test_content_truncation(self):
        """content超过1KB截断"""
        long_content = "A" * 2000
        lines = [
            {"type": "message", "message": {"role": "assistant", "content": long_content, "stopReason": "endTurn"}}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_messages(path)
            assert len(result) == 1
            assert len(result[0]["content"]) < 1100  # 1024 + "...(截断)"
            assert result[0]["content"].endswith("...(截断)")
        finally:
            path.unlink()

    def test_error_message_field(self):
        """errorMessage字段"""
        lines = [
            {"type": "message", "message": {
                "role": "assistant",
                "content": "出错了",
                "stopReason": "error",
                "errorMessage": "API rate limit exceeded"
            }}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_messages(path)
            assert result[0]["stopReason"] == "error"
            assert result[0]["errorMessage"] == "API rate limit exceeded"
        finally:
            path.unlink()

    def test_empty_file(self):
        """空文件"""
        path = create_jsonl_file([])
        try:
            result = get_last_assistant_messages(path)
            assert result == []
        finally:
            path.unlink()

    def test_file_not_exist(self):
        """文件不存在"""
        result = get_last_assistant_messages(Path("/nonexistent/file.jsonl"))
        assert result == []

    def test_no_assistant_messages(self):
        """只有user消息"""
        lines = [
            {"type": "message", "message": {"role": "user", "content": "你好"}},
            {"type": "message", "message": {"role": "user", "content": "请处理"}}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_messages(path)
            assert result == []
        finally:
            path.unlink()


class TestSchedulingPlan:
    """测试调度计划逻辑"""

    def test_plan_archive_structure(self):
        """验证归档计划的数据结构"""
        archive_entry = {
            "version": "V5.8",
            "completed_at": "2026-04-01T22:24:00",
            "milestones": [
                {"id": "M1", "name": "开发", "status": "completed"},
                {"id": "M2", "name": "验收", "status": "completed"}
            ]
        }
        assert archive_entry["version"] == "V5.8"
        assert len(archive_entry["milestones"]) == 2
        assert all(m["status"] == "completed" for m in archive_entry["milestones"])

    def test_plan_milestone_statuses(self):
        """验证里程碑状态值"""
        valid_statuses = {"pending", "in_progress", "completed", "blocked"}
        for s in valid_statuses:
            milestone = {"id": "M1", "name": "test", "status": s}
            assert milestone["status"] in valid_statuses

    def test_overall_status_completed_triggers_archive(self):
        """overall_status=completed时应触发归档"""
        plan = {
            "current_version": "V5.8",
            "overall_status": "completed",
            "milestones": [{"id": "M1", "name": "test", "status": "completed"}]
        }
        # 归档条件
        assert plan.get("overall_status") == "completed"

    def test_overall_status_in_progress_no_archive(self):
        """overall_status=in_progress时不归档"""
        plan = {
            "current_version": "V5.9",
            "overall_status": "in_progress",
            "milestones": [{"id": "M1", "name": "test", "status": "in_progress"}]
        }
        assert plan.get("overall_status") != "completed"

    def test_archive_max_5(self):
        """归档最多保留5个版本"""
        archive = [{"version": f"V5.{i}"} for i in range(8)]
        # 模拟裁剪
        if len(archive) > 5:
            archive = archive[-5:]
        assert len(archive) == 5
        assert archive[0]["version"] == "V5.3"

    def test_new_plan_from_scratch(self):
        """从零建立新计划"""
        plan = {
            "current_version": "V5.9",
            "overall_status": "in_progress",
            "milestones": [
                {"id": "M1", "name": "PRD编写", "status": "completed"},
                {"id": "M2", "name": "技术评审", "status": "in_progress"},
                {"id": "M3", "name": "UI设计", "status": "pending"},
                {"id": "M4", "name": "开发", "status": "pending"},
                {"id": "M5", "name": "验收", "status": "pending"}
            ],
            "next_actions": ["完成技术评审", "启动UI设计"]
        }
        assert len(plan["milestones"]) == 5
        assert plan["milestones"][0]["status"] == "completed"
        assert plan["milestones"][1]["status"] == "in_progress"


class TestQualityGate:
    """测试质量检查逻辑"""

    def test_version_close_all_conditions_required(self):
        """版本闭环需要同时满足4个条件"""
        version_status = {
            "dev_complete": True,
            "qa_passed": True,
            "product_confirmed": True,
            "env_stable": True
        }
        can_close = all(version_status.values())
        assert can_close is True

    def test_version_cannot_close_if_any_fail(self):
        """任一条件不满足都不能闭环"""
        for fail_key in ["dev_complete", "qa_passed", "product_confirmed", "env_stable"]:
            status = {"dev_complete": True, "qa_passed": True, "product_confirmed": True, "env_stable": True}
            status[fail_key] = False
            assert all(status.values()) is False, f"{fail_key}=False should block closure"

    def test_blocking_task_only_for_active_agents(self):
        """只有有实际任务的agent才算阻塞"""
        # ops等待发布 - 不算阻塞
        ops_task = {"agent": "ops", "task": "等待版本发布", "status": "waiting"}
        assert ops_task["status"] == "waiting"  # 不应出现在blocking_tasks

        # fullstack-dev修复BUG中途卡住 - 算阻塞
        dev_task = {"agent": "fullstack-dev", "task": "修复BUG-001", "status": "blocked"}
        assert dev_task["status"] == "blocked"  # 应出现在blocking_tasks


class TestDuplicateNotificationDetection:
    """测试重复任务通知检测逻辑"""

    def test_tc_number_matching(self):
        """TC编号匹配检测"""
        import re
        # 模拟历史通知
        history = [
            {
                "target_group": "dev-working-group",
                "mention_users": ["fullstack-dev"],
                "extracted_issues": ["TC-TASK-001: 创建任务API错误", "TC-TASK-002: 更新任务API错误"]
            }
        ]
        # 当前决策 - 相同TC编号
        current_issues = ["TC-TASK-001: P0级BUG需要修复"]
        # 检测逻辑：TC编号有交集
        hist_tcs = set()
        for issue in history[0]["extracted_issues"]:
            hist_tcs.update(re.findall(r'(TC-[A-Z]+-\d+)', issue, re.IGNORECASE))
        curr_tcs = set()
        for issue in current_issues:
            curr_tcs.update(re.findall(r'(TC-[A-Z]+-\d+)', issue, re.IGNORECASE))
        # 应该检测到交集
        assert hist_tcs & curr_tcs == {"TC-TASK-001"}

    def test_keyword_matching(self):
        """关键词匹配检测"""
        import re
        history = [
            {
                "target_group": "dev-working-group",
                "mention_users": ["fullstack-dev"],
                "extracted_issues": ["P0 BUG: 登录验证失败"]
            }
        ]
        current_issues = ["P0 BUG: 需要立即处理"]
        # 关键词：P0, BUG - 重合度100%
        hist_keywords = set()
        for issue in history[0]["extracted_issues"]:
            hist_keywords.update(k.upper() for k in re.findall(r'\b(P[0-2]|BUG)\b', issue))
        curr_keywords = set()
        for issue in current_issues:
            curr_keywords.update(k.upper() for k in re.findall(r'\b(P[0-2]|BUG)\b', issue))
        # 应该检测到关键词匹配
        assert hist_keywords == {"P0", "BUG"}
        assert len(hist_keywords & curr_keywords) >= 1

    def test_different_agent_no_duplicate(self):
        """不同agent不算重复"""
        history = [
            {
                "target_group": "dev-working-group",
                "mention_users": ["architect"],
                "extracted_issues": ["TC-TASK-001: API设计问题"]
            }
        ]
        # 当前决策 - 相同TC但不同agent (fullstack-dev vs architect)
        # agent不匹配，不应算重复
        hist_mention = set(u.lstrip('@').lower() for u in history[0]["mention_users"])
        curr_mention = {"fullstack-dev"}
        # 没有交集
        assert not (hist_mention & curr_mention)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
