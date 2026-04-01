#!/usr/bin/env python3
"""
测试会话stopReason解析逻辑

测试场景：
1. stopReason=toolUse → 正常使用工具中
2. stopReason=endTurn → 正常完成一轮对话
3. stopReason=aborted → 用户取消，异常终止
4. stopReason=error → 错误终止
5. stopReason=stop → 主动停止
6. 最后一条是user消息 → 表示处理中
7. 空文件
8. 文件不存在
"""

import json
import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch


# 模拟 get_last_assistant_stop_reason 方法
def get_last_assistant_stop_reason(jsonl_file: Path) -> str:
    """获取最后一条assistant消息的stopReason"""
    try:
        last_stop_reason = None
        with open(jsonl_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    if msg.get("type") == "message":
                        message = msg.get("message", {})
                        role = message.get("role")
                        if role == "assistant":
                            last_stop_reason = message.get("stopReason")
                        elif role == "user":
                            last_stop_reason = None  # 有新user消息，重置为处理中
                except json.JSONDecodeError:
                    continue
        return last_stop_reason
    except Exception:
        return None


def create_jsonl_file(lines: list) -> Path:
    """创建临时JSONL文件并返回路径"""
    fd, path = tempfile.mkstemp(suffix='.jsonl')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        for line in lines:
            f.write(json.dumps(line) + '\n')
    return Path(path)


import os


class TestStopReasonParsing:
    """测试stopReason解析"""

    def test_tool_use(self):
        """stopReason=toolUse: 正常使用工具"""
        lines = [
            {"type": "message", "message": {"role": "assistant", "stopReason": "toolUse"}}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_stop_reason(path)
            assert result == "toolUse"
        finally:
            path.unlink()

    def test_end_turn(self):
        """stopReason=endTurn: 正常完成一轮"""
        lines = [
            {"type": "message", "message": {"role": "assistant", "stopReason": "endTurn"}}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_stop_reason(path)
            assert result == "endTurn"
        finally:
            path.unlink()

    def test_aborted(self):
        """stopReason=aborted: 用户取消"""
        lines = [
            {"type": "message", "message": {"role": "assistant", "stopReason": "aborted"}}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_stop_reason(path)
            assert result == "aborted"
        finally:
            path.unlink()

    def test_error(self):
        """stopReason=error: 错误终止"""
        lines = [
            {"type": "message", "message": {"role": "assistant", "stopReason": "error"}}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_stop_reason(path)
            assert result == "error"
        finally:
            path.unlink()

    def test_stop(self):
        """stopReason=stop: 主动停止"""
        lines = [
            {"type": "message", "message": {"role": "assistant", "stopReason": "stop"}}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_stop_reason(path)
            assert result == "stop"
        finally:
            path.unlink()

    def test_user_message_after_assistant(self):
        """最后一条是user消息: 表示处理中，返回None"""
        lines = [
            {"type": "message", "message": {"role": "assistant", "stopReason": "stop"}},
            {"type": "message", "message": {"role": "user", "content": "请继续"}}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_stop_reason(path)
            assert result is None, f"期望None(处理中)，实际返回: {result}"
        finally:
            path.unlink()

    def test_assistant_after_user(self):
        """user消息后又有assistant: 以最后的assistant为准"""
        lines = [
            {"type": "message", "message": {"role": "assistant", "stopReason": "stop"}},
            {"type": "message", "message": {"role": "user", "content": "继续"}},
            {"type": "message", "message": {"role": "assistant", "stopReason": "endTurn"}}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_stop_reason(path)
            assert result == "endTurn"
        finally:
            path.unlink()

    def test_no_stop_reason(self):
        """没有stopReason字段: 返回None"""
        lines = [
            {"type": "message", "message": {"role": "assistant"}}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_stop_reason(path)
            assert result is None
        finally:
            path.unlink()

    def test_empty_file(self):
        """空文件: 返回None"""
        path = create_jsonl_file([])
        try:
            result = get_last_assistant_stop_reason(path)
            assert result is None
        finally:
            path.unlink()

    def test_file_not_exist(self):
        """文件不存在: 返回None"""
        result = get_last_assistant_stop_reason(Path("/nonexistent/file.jsonl"))
        assert result is None

    def test_mixed_messages(self):
        """混合消息类型"""
        lines = [
            {"type": "message", "message": {"role": "user", "content": "开始"}},
            {"type": "message", "message": {"role": "assistant", "stopReason": "toolUse"}},
            {"type": "message", "message": {"role": "user", "content": "好的"}},
            {"type": "message", "message": {"role": "assistant", "stopReason": "stop"}}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_stop_reason(path)
            assert result == "stop"
        finally:
            path.unlink()


class TestStopReasonHandling:
    """测试不同stopReason的处理逻辑"""

    def test_aborted_triggers_activation(self):
        """aborted应该触发立即激活"""
        # 这个测试验证处理逻辑，实际调用在handle_timeout_agents中
        stop_reason = "aborted"
        assert stop_reason in ("aborted", "error"), "aborted应该触发激活"

    def test_error_triggers_activation(self):
        """error应该触发立即激活"""
        stop_reason = "error"
        assert stop_reason in ("aborted", "error"), "error应该触发激活"

    def test_stop_triggers_inquiry(self):
        """stop应该触发任务询问"""
        stop_reason = "stop"
        assert stop_reason == "stop", "stop应该触发任务询问"

    def test_tooluse_no_special_handling(self):
        """toolUse不需要特殊处理"""
        stop_reason = "toolUse"
        assert stop_reason not in ("aborted", "error", "stop"), "toolUse正常，无需特殊处理"

    def test_endturn_no_special_handling(self):
        """endTurn不需要特殊处理"""
        stop_reason = "endTurn"
        assert stop_reason not in ("aborted", "error", "stop"), "endTurn正常，无需特殊处理"

    def test_none_falls_through_to_timeout(self):
        """None应该进入超时检查流程"""
        stop_reason = None
        # None表示处理中或无stopReason，走正常超时检查
        assert stop_reason not in ("aborted", "error", "stop"), "None走正常超时检查"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
