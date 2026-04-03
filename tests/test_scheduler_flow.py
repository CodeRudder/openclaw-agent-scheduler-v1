#!/usr/bin/env python3
"""
调度器流程Mock测试

使用mock_scheduler框架验证ClaudeDrivenScheduler的内部处理流程。
所有外部依赖（MM API、LLM API、飞书、文件I/O）均被mock。

运行：
  python3 -m pytest tests/test_scheduler_flow.py -v
"""

import json
import os
import pytest
import tempfile
from pathlib import Path
from datetime import datetime

import sys
PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from tests.mock_scheduler import MockScheduler, MockDataFactory


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def sched(tmp_dir):
    """创建MockScheduler实例"""
    with MockScheduler(tmp_dir) as m:
        yield m


def _make(ms):
    """创建调度器，返回 (scheduler, module)"""
    return ms.get_scheduler_with_mock()


# ===== 计划管理测试 =====

class TestPlanManagement:

    def test_load_plan(self, sched):
        """从文件加载计划"""
        sched.setup_active_plan()
        scheduler, module = _make(sched)
        try:
            assert scheduler.scheduling_plan.get("current_version") == "V5.9"
            assert len(scheduler.scheduling_plan.get("milestones", [])) > 0
        finally:
            sched.restore_module(module)

    def test_save_plan(self, sched):
        """保存计划更新文件"""
        sched.setup_active_plan()
        scheduler, module = _make(sched)
        try:
            scheduler.scheduling_plan["milestones"][0]["status"] = "completed"
            scheduler._save_scheduling_plan()
            plan = sched.get_plan()
            assert plan["milestones"][0]["status"] == "completed"
        finally:
            sched.restore_module(module)

    def test_archive_completed(self, sched):
        """归档已完成计划"""
        plan = MockDataFactory.plan(
            status="completed", version="V5.8",
            milestones=[{"id": "M1", "name": "V5.8开发", "status": "completed",
                         "progress": "完成", "assigned_to": "fullstack-dev"}]
        )
        sched._write_json(sched.data_dir / "scheduling_plan.json", plan)
        scheduler, module = _make(sched)
        try:
            assert scheduler.scheduling_plan == {}
        finally:
            sched.restore_module(module)

    def test_empty_plan(self, sched):
        """空计划"""
        sched.setup_empty_plan()
        scheduler, module = _make(sched)
        try:
            assert scheduler.scheduling_plan == {}
        finally:
            sched.restore_module(module)


# ===== 通知历史测试 =====

class TestNotificationHistory:

    def test_load_empty(self, sched):
        """加载空历史"""
        sched.setup_active_plan()
        scheduler, module = _make(sched)
        try:
            assert scheduler.notification_history.get("history") == []
        finally:
            sched.restore_module(module)

    def test_save_history(self, sched):
        """保存通知历史"""
        sched.setup_active_plan()
        scheduler, module = _make(sched)
        try:
            scheduler.notification_history.setdefault("history", []).append({
                "timestamp": datetime.now().isoformat(),
                "message_content": "测试消息"
            })
            scheduler._save_notification_history()
            history = sched.get_history()
            assert len(history.get("history", [])) == 1
        finally:
            sched.restore_module(module)


# ===== 重复通知检测测试 =====

class TestDuplicateNotification:

    def _decision(self, mention_users, extracted_issues,
                  target_group="dev-working-group"):
        from scripts.claude_driven_scheduler import SchedulingDecision
        return SchedulingDecision(
            action="notify",
            target_group=target_group,
            target_group_name="开发工作群",
            mention_users=mention_users,
            extracted_issues=extracted_issues,
            message_content="请处理",
            reasoning="测试",
            source_group="qa-acceptance-group"
        )

    def test_tc_duplicate(self, sched):
        """相同TC编号重复"""
        sched.setup_active_plan()
        sched.setup_notification_history(entries=[{
            "timestamp": datetime.now().isoformat(),
            "target_group": "dev-working-group",
            "mention_users": ["fullstack-dev"],
            "extracted_issues": ["TC-TASK-001: 创建任务API错误"]
        }])
        scheduler, module = _make(sched)
        try:
            d = self._decision(["fullstack-dev"], ["TC-TASK-001: API错误"])
            assert scheduler._is_duplicate_task_notification(d, lookback=5) is True
        finally:
            sched.restore_module(module)

    def test_different_tc(self, sched):
        """不同TC编号不重复"""
        sched.setup_active_plan()
        sched.setup_notification_history(entries=[{
            "timestamp": datetime.now().isoformat(),
            "target_group": "dev-working-group",
            "mention_users": ["fullstack-dev"],
            "extracted_issues": ["TC-TASK-001: 创建任务API错误"]
        }])
        scheduler, module = _make(sched)
        try:
            d = self._decision(["fullstack-dev"], ["TC-PROJ-002: 项目API错误"])
            assert scheduler._is_duplicate_task_notification(d, lookback=5) is False
        finally:
            sched.restore_module(module)

    def test_different_agent(self, sched):
        """不同agent不重复"""
        sched.setup_active_plan()
        sched.setup_notification_history(entries=[{
            "timestamp": datetime.now().isoformat(),
            "target_group": "dev-working-group",
            "mention_users": ["architect"],
            "extracted_issues": ["API设计问题"]
        }])
        scheduler, module = _make(sched)
        try:
            d = self._decision(["fullstack-dev"], ["API实现问题"])
            assert scheduler._is_duplicate_task_notification(d, lookback=5) is False
        finally:
            sched.restore_module(module)

    def test_empty_history(self, sched):
        """空历史不重复"""
        sched.setup_active_plan()
        scheduler, module = _make(sched)
        try:
            d = self._decision(["fullstack-dev"], ["TC-NEW-001"])
            assert scheduler._is_duplicate_task_notification(d, lookback=5) is False
        finally:
            sched.restore_module(module)


# ===== 消息发送测试 =====

class TestMessageSending:

    def test_send_activation(self, sched):
        """发送激活消息"""
        sched.setup_active_plan()
        scheduler, module = _make(sched)
        try:
            result = scheduler.send_activation_message("dev-working-group", "fullstack-dev")
            assert result is True
            sent = sched.get_sent_mm_posts()
            assert len(sent) == 1
            assert "fullstack-dev" in sent[0]
        finally:
            sched.restore_module(module)

    def test_send_activation_unknown_group(self, sched):
        """未知群不发激活消息"""
        sched.setup_active_plan()
        scheduler, module = _make(sched)
        try:
            result = scheduler.send_activation_message("unknown-group", "agent")
            assert result is False
        finally:
            sched.restore_module(module)

    def test_send_activation_only(self, sched):
        """发送简短激活消息"""
        sched.setup_active_plan()
        from scripts.claude_driven_scheduler import SchedulingDecision
        decision = SchedulingDecision(
            action="notify",
            target_group="dev-working-group",
            target_group_name="开发工作群",
            mention_users=["fullstack-dev"],
            extracted_issues=["TC-TASK-001: API错误"],
            message_content="请修复",
            reasoning="测试"
        )
        scheduler, module = _make(sched)
        try:
            result = scheduler._send_activation_only(decision)
            assert result is True
            sent = sched.get_sent_mm_posts()
            assert len(sent) == 1
        finally:
            sched.restore_module(module)

    def test_send_task_inquiry(self, sched):
        """发送任务询问"""
        sched.setup_active_plan()
        scheduler, module = _make(sched)
        try:
            result = scheduler.send_task_inquiry_message(
                "dev-working-group", "fullstack-dev", "修复BUG-001")
            assert result is True
        finally:
            sched.restore_module(module)


# ===== 会话JSONL解析测试 =====

class TestSessionJsonlParsing:

    def test_parse_stop_reason(self, tmp_dir):
        """解析stopReason"""
        mock = MockScheduler(tmp_dir)
        mock.setup_active_plan()
        scheduler, module = _make(mock)
        try:
            jsonl = Path(tmp_dir) / "test.jsonl"
            lines = [
                {"type": "message", "message": {"role": "assistant", "stopReason": "stop"}},
                {"type": "message", "message": {"role": "user", "content": "继续"}},
                {"type": "message", "message": {"role": "assistant", "stopReason": "error",
                                                 "errorMessage": "API error"}}
            ]
            jsonl.write_text("\n".join(json.dumps(l) for l in lines), encoding='utf-8')

            result = scheduler.get_last_assistant_stop_reason(jsonl)
            assert result == "error"
        finally:
            mock.restore_module(module)

    def test_parse_last_messages(self, tmp_dir):
        """解析最后N条assistant消息"""
        mock = MockScheduler(tmp_dir)
        mock.setup_active_plan()
        scheduler, module = _make(mock)
        try:
            jsonl = Path(tmp_dir) / "test.jsonl"
            lines = [
                {"type": "message", "message": {"role": "user", "content": "开始"}},
                {"type": "message", "message": {"role": "assistant",
                                                 "content": "第一轮", "stopReason": "stop"}},
                {"type": "message", "message": {"role": "user", "content": "继续"}},
                {"type": "message", "message": {"role": "assistant",
                                                 "content": "第二轮", "stopReason": "endTurn"}},
                {"type": "message", "message": {"role": "assistant",
                                                 "content": "第三轮", "stopReason": "stop"}}
            ]
            jsonl.write_text("\n".join(json.dumps(l) for l in lines), encoding='utf-8')

            result = scheduler.get_last_assistant_messages(jsonl, count=2)
            assert len(result) == 2
            assert result[0]["content"] == "第二轮"
            assert result[1]["content"] == "第三轮"
            assert result[1]["stopReason"] == "stop"
        finally:
            mock.restore_module(module)


# ===== 边界情况测试 =====

class TestEdgeCases:

    def test_malformed_plan(self, sched):
        """格式错误的计划文件"""
        with open(sched.data_dir / "scheduling_plan.json", 'w') as f:
            f.write("not valid json{{{")

        data = sched._read_json(sched.data_dir / "scheduling_plan.json")
        assert data == {}

    def test_nonexistent_file(self, sched):
        """不存在的文件"""
        data = sched._read_json(sched.data_dir / "nonexistent.json")
        assert data == {}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
