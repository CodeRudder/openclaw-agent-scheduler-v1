#!/usr/bin/env python3
"""
check_and_recover_agent_sessions.py 单元测试

测试覆盖：
- _get_text_length: 提取text内容长度
- get_last_assistant_messages: 解析assistant消息
- get_last_non_user_messages: 解析非user消息
- check_agent_session: 会话异常检测（正向和反向）

运行：
  python3 -m pytest tests/test_check_and_recover_agent_sessions.py -v
"""

import json
import pytest
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

from check_and_recover_agent_sessions import (
    _get_text_length,
    is_content_empty,
    get_last_assistant_messages,
    get_last_non_user_messages,
    check_agent_session,
    DEFAULT_MIN_VALID_LENGTH,
    DEFAULT_RECENT_MSG_COUNT,
)


# ===== 辅助函数 =====

def make_assistant_msg(text="", stop_reason="stop", timestamp=None):
    """构造assistant消息的JSONL行"""
    if text:
        content = [{"type": "text", "text": text}]
    else:
        content = []
    msg = {
        "type": "message",
        "message": {
            "role": "assistant",
            "content": content,
            "stopReason": stop_reason,
        },
        "timestamp": timestamp or "2026-04-04T10:00:00.000Z"
    }
    return json.dumps(msg, ensure_ascii=False)


def make_user_msg(text="hello", timestamp=None):
    """构造user消息的JSONL行"""
    msg = {
        "type": "message",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
        "timestamp": timestamp or "2026-04-04T10:00:00.000Z"
    }
    return json.dumps(msg, ensure_ascii=False)


def make_tool_use_msg(tool_name="bash", timestamp=None):
    """构造toolUse类型的assistant消息"""
    msg = {
        "type": "message",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": tool_name, "input": {}}],
            "stopReason": "toolUse",
        },
        "timestamp": timestamp or "2026-04-04T10:00:00.000Z"
    }
    return json.dumps(msg, ensure_ascii=False)


def write_session(lines: list) -> Path:
    """写入临时JSONL会话文件，返回路径"""
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False, encoding='utf-8')
    for line in lines:
        f.write(line + '\n')
    f.close()
    return Path(f.name)


# ===== _get_text_length 测试 =====

class TestGetTextLength:
    def test_string_content(self):
        assert _get_text_length("hello world") == 11

    def test_empty_string(self):
        assert _get_text_length("") == 0

    def test_list_with_text(self):
        content = [{"type": "text", "text": "hello"}]
        assert _get_text_length(content) == 5

    def test_list_with_text_and_thinking(self):
        """thinking类型不计入长度"""
        content = [
            {"type": "thinking", "thinking": "some thinking"},
            {"type": "text", "text": "hello"},
        ]
        assert _get_text_length(content) == 5

    def test_list_with_text_signature(self):
        """textSignature字段不计入长度"""
        content = [{"type": "text", "text": "", "textSignature": "abc123"}]
        assert _get_text_length(content) == 0

    def test_list_with_tool_use(self):
        """tool_use类型不计入长度"""
        content = [{"type": "tool_use", "id": "t1", "name": "bash", "input": {}}]
        assert _get_text_length(content) == 0

    def test_empty_list(self):
        assert _get_text_length([]) == 0

    def test_none(self):
        assert _get_text_length(None) == 0

    def test_multiple_text_items(self):
        content = [
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
        ]
        # "hello ".strip() = "hello" (5), "world".strip() = "world" (5) → total = 10
        assert _get_text_length(content) == 10

    def test_multiple_text_items_correct(self):
        content = [
            {"type": "text", "text": "hello"},
            {"type": "text", "text": " world"},
        ]
        # "hello".strip() = "hello" = 5, " world".strip() = "world" = 5 → total = 10
        assert _get_text_length(content) == 10


# ===== get_last_assistant_messages 测试 =====

class TestGetLastAssistantMessages:
    def test_basic(self):
        session = write_session([
            make_assistant_msg("hello world", "stop"),
            make_assistant_msg("", "toolUse"),
        ])
        msgs = get_last_assistant_messages(session, count=5)
        assert len(msgs) == 2
        assert msgs[0]["content_length"] == 11
        assert msgs[0]["stop_reason"] == "stop"
        assert msgs[1]["content_length"] == 0
        assert msgs[1]["stop_reason"] == "toolUse"
        session.unlink()

    def test_count_limit(self):
        session = write_session([make_assistant_msg(f"msg {i}", "stop") for i in range(10)])
        msgs = get_last_assistant_messages(session, count=5)
        assert len(msgs) == 5
        session.unlink()

    def test_ignores_user_messages(self):
        session = write_session([
            make_user_msg("user message"),
            make_assistant_msg("assistant reply", "stop"),
        ])
        msgs = get_last_assistant_messages(session, count=5)
        assert len(msgs) == 1
        assert msgs[0]["content_length"] == len("assistant reply")
        session.unlink()

    def test_text_signature_ignored(self):
        """textSignature字段不计入content_length"""
        msg = {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "", "textSignature": "abc123"}],
                "stopReason": "stop",
            },
            "timestamp": "2026-04-04T10:00:00.000Z"
        }
        session = write_session([json.dumps(msg)])
        msgs = get_last_assistant_messages(session, count=5)
        assert len(msgs) == 1
        assert msgs[0]["content_length"] == 0
        session.unlink()

    def test_stop_reason_underscore_compat(self):
        """兼容stop_reason（下划线）命名"""
        msg = {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [],
                "stop_reason": "end_turn",
            },
            "timestamp": "2026-04-04T10:00:00.000Z"
        }
        session = write_session([json.dumps(msg)])
        msgs = get_last_assistant_messages(session, count=5)
        assert msgs[0]["stop_reason"] == "end_turn"
        session.unlink()


# ===== get_last_non_user_messages 测试 =====

class TestGetLastNonUserMessages:
    def test_only_assistant(self):
        session = write_session([
            make_assistant_msg("hello", "stop"),
            make_assistant_msg("", "stop"),
        ])
        msgs = get_last_non_user_messages(session, count=5)
        assert len(msgs) == 2
        assert all(m["role"] == "assistant" for m in msgs)
        session.unlink()

    def test_excludes_user(self):
        session = write_session([
            make_user_msg("user"),
            make_assistant_msg("reply", "stop"),
            make_user_msg("user2"),
        ])
        msgs = get_last_non_user_messages(session, count=5)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "assistant"
        session.unlink()

    def test_mixed_stop_reasons(self):
        session = write_session([
            make_tool_use_msg(),
            make_user_msg("tool result"),
            make_assistant_msg("done", "stop"),
        ])
        msgs = get_last_non_user_messages(session, count=5)
        # Should have 2 non-user messages: toolUse assistant + stop assistant
        assert len(msgs) == 2
        assert msgs[0]["stop_reason"] == "toolUse"
        assert msgs[1]["stop_reason"] == "stop"
        session.unlink()

    def test_count_limit(self):
        lines = []
        for i in range(10):
            lines.append(make_assistant_msg(f"msg {i}", "stop"))
        session = write_session(lines)
        msgs = get_last_non_user_messages(session, count=5)
        assert len(msgs) == 5
        session.unlink()


# ===== check_agent_session 测试 =====

class TestCheckAgentSession:
    """测试会话异常检测逻辑（使用临时目录模拟agent会话）"""

    def _make_agent_dir(self, tmp_path, agent_name, session_lines):
        """创建临时agent会话目录"""
        session_dir = tmp_path / agent_name / "sessions"
        session_dir.mkdir(parents=True)
        session_file = session_dir / "test_session.jsonl"
        session_file.write_text('\n'.join(session_lines) + '\n', encoding='utf-8')
        return session_dir

    def test_normal_session_with_tool_use(self, tmp_path, monkeypatch):
        """正常会话：有toolUse消息，不应重置（条件2不满足）"""
        lines = [
            make_tool_use_msg(),
            make_user_msg("tool result"),
            make_assistant_msg("这是有效内容超过10字符", "stop"),
            make_tool_use_msg(),
            make_user_msg("tool result"),
            make_assistant_msg("这是有效内容超过10字符", "stop"),
            make_tool_use_msg(),
            make_user_msg("tool result"),
            make_assistant_msg("这是有效内容超过10字符", "stop"),
        ]
        self._make_agent_dir(tmp_path, "test-agent", lines)

        import check_and_recover_agent_sessions as m
        monkeypatch.setattr(m, "AGENTS_BASE", tmp_path)

        result = check_agent_session("test-agent")
        assert result["should_reset"] is False, f"不应重置: {result['reason']}"

    def test_all_invalid_content_triggers_reset(self, tmp_path, monkeypatch):
        """条件1：最近N条消息全部无效（内容过短），应触发重置"""
        lines = [
            make_assistant_msg("", "stop"),
            make_assistant_msg("", "stop"),
            make_assistant_msg("", "stop"),
            make_assistant_msg("", "stop"),
            make_assistant_msg("", "stop"),
        ]
        self._make_agent_dir(tmp_path, "test-agent", lines)

        import check_and_recover_agent_sessions as m
        monkeypatch.setattr(m, "AGENTS_BASE", tmp_path)

        result = check_agent_session("test-agent")
        assert result["should_reset"] is True
        assert "全部无效" in result["reason"]

    def test_all_stop_no_tool_triggers_reset(self, tmp_path, monkeypatch):
        """条件2：最近N条非user消息全部是assistant+stop（无toolUse），应触发重置"""
        lines = [
            make_assistant_msg("这是有效内容超过10字符", "stop"),
            make_assistant_msg("这是有效内容超过10字符", "stop"),
            make_assistant_msg("这是有效内容超过10字符", "stop"),
            make_assistant_msg("这是有效内容超过10字符", "stop"),
            make_assistant_msg("这是有效内容超过10字符", "stop"),
        ]
        self._make_agent_dir(tmp_path, "test-agent", lines)

        import check_and_recover_agent_sessions as m
        monkeypatch.setattr(m, "AGENTS_BASE", tmp_path)

        result = check_agent_session("test-agent")
        assert result["should_reset"] is True
        assert "stop" in result["reason"]

    def test_active_session_with_tool_use_not_reset(self, tmp_path, monkeypatch):
        """正常会话：有toolUse消息，不应重置（条件2不满足）"""
        lines = [
            make_tool_use_msg(),           # assistant, toolUse
            make_user_msg("tool result"),  # user (tool result)
            make_assistant_msg("这是有效内容超过10字符", "stop"),
            make_tool_use_msg(),           # assistant, toolUse
            make_user_msg("tool result"),  # user
            make_assistant_msg("这是有效内容超过10字符", "stop"),
        ]
        self._make_agent_dir(tmp_path, "test-agent", lines)

        import check_and_recover_agent_sessions as m
        monkeypatch.setattr(m, "AGENTS_BASE", tmp_path)

        result = check_agent_session("test-agent")
        assert result["should_reset"] is False, f"不应重置: {result['reason']}"

    def test_mixed_valid_invalid_not_reset(self, tmp_path, monkeypatch):
        """混合会话：部分有效消息，不应重置（条件1不满足）"""
        lines = [
            make_assistant_msg("", "stop"),
            make_assistant_msg("", "stop"),
            make_assistant_msg("这是有效内容超过10字符", "stop"),
            make_assistant_msg("", "stop"),
            make_assistant_msg("", "stop"),
        ]
        self._make_agent_dir(tmp_path, "test-agent", lines)

        import check_and_recover_agent_sessions as m
        monkeypatch.setattr(m, "AGENTS_BASE", tmp_path)

        result = check_agent_session("test-agent")
        # 条件1不满足（有1条有效），条件2满足（全部stop）→ 触发重置
        # 注意：条件2会触发，因为所有非user消息都是assistant+stop
        assert result["should_reset"] is True

    def test_new_session_too_few_messages(self, tmp_path, monkeypatch):
        """新会话：消息数不足N条，不应重置"""
        lines = [
            make_assistant_msg("", "stop"),
            make_assistant_msg("", "stop"),
        ]
        self._make_agent_dir(tmp_path, "test-agent", lines)

        import check_and_recover_agent_sessions as m
        monkeypatch.setattr(m, "AGENTS_BASE", tmp_path)

        result = check_agent_session("test-agent", recent_msg_count=5)
        assert result["should_reset"] is False
        assert "不足" in result["reason"]

    def test_no_session_files(self, tmp_path, monkeypatch):
        """无会话文件，不应重置"""
        session_dir = tmp_path / "test-agent" / "sessions"
        session_dir.mkdir(parents=True)

        import check_and_recover_agent_sessions as m
        monkeypatch.setattr(m, "AGENTS_BASE", tmp_path)

        result = check_agent_session("test-agent")
        assert result["should_reset"] is False
        assert "无会话文件" in result["reason"]

    def test_condition2_requires_all_stop(self, tmp_path, monkeypatch):
        """条件2：只要有一条toolUse，就不触发重置"""
        lines = [
            make_assistant_msg("这是有效内容超过10字符", "stop"),
            make_assistant_msg("这是有效内容超过10字符", "stop"),
            make_assistant_msg("这是有效内容超过10字符", "stop"),
            make_assistant_msg("这是有效内容超过10字符", "stop"),
            make_tool_use_msg(),  # 最后一条是toolUse
        ]
        self._make_agent_dir(tmp_path, "test-agent", lines)

        import check_and_recover_agent_sessions as m
        monkeypatch.setattr(m, "AGENTS_BASE", tmp_path)

        result = check_agent_session("test-agent")
        assert result["should_reset"] is False, f"不应重置: {result['reason']}"

    def test_condition2_with_user_messages_interspersed(self, tmp_path, monkeypatch):
        """条件2：user消息（含tool_result）被排除，只看非user消息"""
        lines = [
            make_assistant_msg("这是有效内容超过10字符", "stop"),
            make_user_msg("user message 1"),
            make_assistant_msg("这是有效内容超过10字符", "stop"),
            make_user_msg("user message 2"),
            make_assistant_msg("这是有效内容超过10字符", "stop"),
            make_user_msg("user message 3"),
            make_assistant_msg("这是有效内容超过10字符", "stop"),
            make_user_msg("user message 4"),
            make_assistant_msg("这是有效内容超过10字符", "stop"),
        ]
        self._make_agent_dir(tmp_path, "test-agent", lines)

        import check_and_recover_agent_sessions as m
        monkeypatch.setattr(m, "AGENTS_BASE", tmp_path)

        result = check_agent_session("test-agent")
        # 最近5条非user消息全部是assistant+stop → 触发重置
        assert result["should_reset"] is True
        assert "stop" in result["reason"]

    def test_verbose_output(self, tmp_path, monkeypatch, caplog):
        """verbose模式输出详细信息"""
        import logging
        lines = [make_assistant_msg("", "stop") for _ in range(5)]
        self._make_agent_dir(tmp_path, "test-agent", lines)

        import check_and_recover_agent_sessions as m
        monkeypatch.setattr(m, "AGENTS_BASE", tmp_path)

        with caplog.at_level(logging.INFO):
            result = check_agent_session("test-agent", verbose=True)

        assert result["should_reset"] is True
        assert any("条件1" in r.message for r in caplog.records)
        assert any("条件2" in r.message for r in caplog.records)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
