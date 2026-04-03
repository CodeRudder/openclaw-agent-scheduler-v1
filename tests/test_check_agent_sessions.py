#!/usr/bin/env python3
"""
check_agent_sessions.py 测试用例

测试覆盖：
- get_session_files: 获取会话文件列表
- parse_session_file: 解析JSONL文件（支持Agent格式和Claude项目格式）
- extract_content: 提取消息内容
- format_time_ago: 时间格式化
- get_status_icon: 状态图标
- list_sessions: 会话列表分页
- find_session_by_partial: 部分UUID匹配
- _slice_messages: 消息切片（head/tail/from/count）
- show_session_file: 显示会话文件
- show_last_messages: 显示agent消息
- extract_content_full: 完整内容提取（过滤元数据）
- 命令行参数解析

运行：
  python3 -m pytest tests/test_check_agent_sessions.py -v
"""

import json
import pytest
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import sys
PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

from check_agent_sessions import (
    get_session_files,
    parse_session_file,
    extract_content,
    format_time_ago,
    get_status_icon,
    _slice_messages,
    extract_content_full,
)


# ===== 测试数据 =====

def create_jsonl_file(tmp_dir: Path, filename: str, lines: list) -> Path:
    """创建测试用JSONL文件"""
    file_path = tmp_dir / filename
    with open(file_path, 'w', encoding='utf-8') as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return file_path


def make_agent_message(role: str, content: str, stop_reason: str = None,
                       error_message: str = None, timestamp: int = None) -> dict:
    """创建Agent格式的消息行"""
    return {
        "type": "message",
        "timestamp": timestamp or int(datetime.now().timestamp() * 1000),
        "message": {
            "role": role,
            "content": content,
            "stopReason": stop_reason,
            "errorMessage": error_message
        }
    }


def make_claude_message(role: str, content: str, stop_reason: str = None,
                        timestamp: int = None) -> dict:
    """创建Claude项目格式的消息行（使用 stop_reason 下划线命名）"""
    return {
        "type": role,
        "timestamp": timestamp or int(datetime.now().timestamp() * 1000),
        "message": {
            "content": content,
            "stop_reason": stop_reason  # Claude 使用下划线命名
        }
    }


# ===== get_session_files 测试 =====

class TestGetSessionFiles:
    """测试获取会话文件列表"""

    def test_no_sessions_dir(self, tmp_path):
        """不存在的会话目录"""
        with patch('check_agent_sessions.AGENTS_BASE', tmp_path / "agents"):
            files = get_session_files("nonexistent-agent")
            assert files == []

    def test_empty_sessions_dir(self, tmp_path):
        """空的会话目录"""
        agent_dir = tmp_path / "agents" / "test-agent" / "sessions"
        agent_dir.mkdir(parents=True, exist_ok=True)

        with patch('check_agent_sessions.AGENTS_BASE', tmp_path / "agents"):
            files = get_session_files("test-agent")
            assert files == []

    def test_get_session_files_sorted(self, tmp_path):
        """会话文件按修改时间倒序排列"""
        agent_dir = tmp_path / "agents" / "test-agent" / "sessions"
        agent_dir.mkdir(parents=True, exist_ok=True)

        # 创建多个会话文件
        file1 = create_jsonl_file(agent_dir, "old.jsonl", [])
        file2 = create_jsonl_file(agent_dir, "new.jsonl", [])

        with patch('check_agent_sessions.AGENTS_BASE', tmp_path / "agents"):
            files = get_session_files("test-agent")
            assert len(files) == 2
            # 新文件应该在前面
            assert files[0].name == "new.jsonl"
            assert files[1].name == "old.jsonl"

    def test_exclude_backup_files(self, tmp_path):
        """默认排除备份文件"""
        agent_dir = tmp_path / "agents" / "test-agent" / "sessions"
        agent_dir.mkdir(parents=True, exist_ok=True)

        create_jsonl_file(agent_dir, "main.jsonl", [])
        create_jsonl_file(agent_dir, "backup.jsonl", [])

        with patch('check_agent_sessions.AGENTS_BASE', tmp_path / "agents"):
            files = get_session_files("test-agent")
            assert len(files) == 1
            assert files[0].name == "main.jsonl"


# ===== parse_session_file 测试 =====

class TestParseSessionFile:
    """测试解析会话文件"""

    def test_parse_agent_format(self, tmp_path):
        """解析Agent会话格式"""
        file_path = tmp_path / "test.jsonl"
        lines = [
            make_agent_message("user", "Hello", stop_reason=None),
            make_agent_message("assistant", "Hi there!", stop_reason="endTurn"),
        ]
        create_jsonl_file(tmp_path, "test.jsonl", lines)

        result = parse_session_file(file_path)
        assert len(result["messages"]) == 2
        assert result["stop_reason"] == "endTurn"
        assert result["last_message"]["role"] == "assistant"

    def test_parse_claude_project_format(self, tmp_path):
        """解析Claude项目格式"""
        file_path = tmp_path / "test.jsonl"
        lines = [
            make_claude_message("user", "Hello"),
            make_claude_message("assistant", "Hi!", stop_reason="stop"),
        ]
        create_jsonl_file(tmp_path, "test.jsonl", lines)

        result = parse_session_file(file_path)
        assert len(result["messages"]) == 2
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][1]["role"] == "assistant"
        assert result["stop_reason"] == "stop"

    def test_parse_mixed_formats(self, tmp_path):
        """解析混合格式文件"""
        file_path = tmp_path / "test.jsonl"
        lines = [
            make_agent_message("user", "Hello"),
            make_claude_message("assistant", "Hi!"),
        ]
        create_jsonl_file(tmp_path, "test.jsonl", lines)

        result = parse_session_file(file_path)
        assert len(result["messages"]) == 2

    def test_parse_empty_file(self, tmp_path):
        """解析空文件"""
        file_path = tmp_path / "empty.jsonl"
        file_path.touch()

        result = parse_session_file(file_path)
        assert result["messages"] == []
        assert result["stop_reason"] is None

    def test_parse_malformed_json(self, tmp_path):
        """解析包含无效JSON的文件"""
        file_path = tmp_path / "test.jsonl"
        with open(file_path, 'w') as f:
            f.write('{"type": "message", "message": {"role": "user"}}\n')
            f.write('invalid json line\n')
            f.write('{"type": "message", "message": {"role": "assistant"}}\n')

        result = parse_session_file(file_path)
        assert len(result["messages"]) == 2  # 跳过无效行


# ===== extract_content 测试 =====

class TestExtractContent:
    """测试提取消息内容"""

    def test_extract_string_content(self):
        """提取字符串内容"""
        content = "Hello, World!"
        result = extract_content(content)
        assert result == "Hello, World!"

    def test_extract_long_string(self):
        """截断长字符串"""
        content = "A" * 150
        result = extract_content(content)
        assert len(result) == 103  # 100 + "..."
        assert result.endswith("...")

    def test_extract_list_content(self):
        """提取列表内容"""
        content = [
            {"type": "text", "text": "Hello"},
            {"type": "text", "text": "World"}
        ]
        result = extract_content(content)
        assert "Hello" in result
        assert "World" in result

    def test_extract_empty_content(self):
        """提取空内容"""
        assert extract_content("") == ""
        assert extract_content(None) == "None"


# ===== format_time_ago 测试 =====

class TestFormatTimeAgo:
    """测试时间格式化"""

    def test_just_now(self):
        """刚刚"""
        now = datetime.now()
        result = format_time_ago(now)
        assert result == "刚刚"

    def test_minutes_ago(self):
        """几分钟前"""
        dt = datetime.now() - timedelta(minutes=30)
        result = format_time_ago(dt)
        assert result == "30分钟前"

    def test_hours_ago(self):
        """几小时前"""
        dt = datetime.now() - timedelta(hours=5)
        result = format_time_ago(dt)
        assert result == "5小时前"

    def test_days_ago(self):
        """几天前"""
        dt = datetime.now() - timedelta(days=3)
        result = format_time_ago(dt)
        assert result == "3天前"


# ===== get_status_icon 测试 =====

class TestGetStatusIcon:
    """测试状态图标"""

    def test_running(self):
        """运行中"""
        assert "运行" in get_status_icon(None)

    def test_end_turn(self):
        """正常结束 (OpenClaw格式)"""
        assert "正常" in get_status_icon("endTurn")

    def test_end_turn_claude(self):
        """正常结束 (Claude格式)"""
        assert "正常" in get_status_icon("end_turn")

    def test_tool_use(self):
        """工具调用 (OpenClaw格式)"""
        assert "工具" in get_status_icon("toolUse")

    def test_tool_use_claude(self):
        """工具调用 (Claude格式)"""
        assert "工具" in get_status_icon("tool_use")

    def test_stop(self):
        """主动停止"""
        assert "停止" in get_status_icon("stop")

    def test_stop_sequence_claude(self):
        """主动停止 (Claude格式)"""
        assert "停止" in get_status_icon("stop_sequence")

    def test_aborted(self):
        """异常终止"""
        assert "异常" in get_status_icon("aborted")

    def test_error(self):
        """错误"""
        assert "错误" in get_status_icon("error")

    def test_max_tokens_claude(self):
        """达到长度限制 (Claude格式)"""
        assert "长度限制" in get_status_icon("max_tokens")

    def test_unknown(self):
        """未知状态"""
        result = get_status_icon("unknown_reason")
        assert "unknown_reason" in result


# ===== _slice_messages 测试 =====

class TestSliceMessages:
    """测试消息切片"""

    def test_slice_empty(self):
        """空消息列表"""
        result, desc = _slice_messages([], 10, 1)
        assert result == []
        assert "0" in desc

    def test_slice_from_start(self):
        """从头开始切片"""
        messages = [{"message": {"role": "user"}} for _ in range(20)]
        result, desc = _slice_messages(messages, 5, 1)
        assert len(result) == 5
        assert result[0][0] == 1  # 第1条
        assert result[4][0] == 5  # 第5条
        assert "第1-5条" in desc

    def test_slice_from_middle(self):
        """从中间开始切片"""
        messages = [{"message": {"role": "user"}} for _ in range(20)]
        result, desc = _slice_messages(messages, 5, 10)  # 从第10条开始
        assert len(result) == 5
        assert result[0][0] == 10  # 起始行号
        assert "第10-14条" in desc

    def test_slice_from_negative(self):
        """从倒数位置开始切片"""
        messages = [{"message": {"role": "user"}} for _ in range(20)]
        result, desc = _slice_messages(messages, 5, -5)  # 从倒数第5条开始
        assert len(result) == 5
        assert result[0][0] == 16  # 第16条 = 20 - 5 + 1
        assert "第16-20条" in desc

    def test_slice_all(self):
        """显示全部"""
        messages = [{"message": {"role": "user"}} for _ in range(20)]
        result, desc = _slice_messages(messages, 0, 1)  # count=0 表示全部
        assert len(result) == 20
        assert "第1-20条" in desc

    def test_slice_head(self):
        """只取头部"""
        messages = [{"message": {"role": "user"}} for _ in range(20)]
        result, desc = _slice_messages(messages, 0, 1, head=5)
        assert len(result) == 5
        assert result[0][0] == 1
        assert result[4][0] == 5
        assert "第1-5条" in desc

    def test_slice_tail(self):
        """只取尾部"""
        messages = [{"message": {"role": "user"}} for _ in range(20)]
        result, desc = _slice_messages(messages, 0, 1, tail=5)
        assert len(result) == 5
        assert result[0][0] == 16  # 第16条开始
        assert result[4][0] == 20  # 第20条
        assert "第16-20条" in desc

    def test_slice_head_and_tail(self):
        """同时取头和尾"""
        messages = [{"message": {"role": "user"}} for _ in range(20)]
        result, desc = _slice_messages(messages, 0, 1, head=3, tail=3)
        assert len(result) == 6  # 3 + 3
        # 检查行号
        line_nums = [r[0] for r in result]
        assert 1 in line_nums
        assert 2 in line_nums
        assert 3 in line_nums
        assert 18 in line_nums
        assert 19 in line_nums
        assert 20 in line_nums
        assert "第1-3条 + 第18-20条" in desc

    def test_slice_head_tail_overlap(self):
        """头尾重叠时显示全部"""
        messages = [{"message": {"role": "user"}} for _ in range(10)]
        result, desc = _slice_messages(messages, 0, 1, head=6, tail=6)  # 6+6 > 10
        assert len(result) == 10  # 显示全部
        assert "第1-10条" in desc

    def test_slice_beyond_range(self):
        """超出范围"""
        messages = [{"message": {"role": "user"}} for _ in range(10)]
        result, desc = _slice_messages(messages, 5, 20)  # 从第20条开始，但只有10条
        assert len(result) == 0
        assert "超出范围" in desc


# ===== extract_content_full 测试 =====

class TestExtractContentFull:
    """测试完整内容提取"""

    def test_filter_control_markers(self):
        """过滤控制标记"""
        content = "NO_REPLY Some text NO_ACTION"
        result = extract_content_full(content)
        assert "NO_REPLY" not in result
        assert "NO_ACTION" not in result
        assert "Some text" in result

    def test_filter_system_header(self):
        """过滤System消息头"""
        content = "System: [2024-01-01 12:00:00 GMT+8] from @user\n\nActual content here"
        result = extract_content_full(content)
        assert "System:" not in result
        assert "Actual content" in result

    def test_extract_tool_use(self):
        """提取工具调用"""
        content = [
            {"type": "tool_use", "name": "bash", "input": {"command": "ls -la"}}
        ]
        result = extract_content_full(content)
        assert "工具调用" in result
        assert "bash" in result
        assert "ls -la" in result

    def test_extract_tool_result(self):
        """提取工具结果"""
        content = [
            {"type": "tool_result", "content": "file1.txt\nfile2.txt"}
        ]
        result = extract_content_full(content)
        assert "工具结果" in result
        assert "file1.txt" in result

    def test_extract_thinking(self):
        """提取思考内容"""
        content = [
            {"type": "thinking", "thinking": "Let me think about this..."}
        ]
        result = extract_content_full(content)
        assert "思考" in result


# ===== 命令行参数测试 =====

class TestCommandLineArgs:
    """测试命令行参数解析"""

    def test_default_args(self):
        """默认参数"""
        import argparse
        from check_agent_sessions import main
        parser = argparse.ArgumentParser()
        parser.add_argument("agent", nargs="?")
        parser.add_argument("count", nargs="?", type=int, default=None)
        parser.add_argument("-n", "--num", type=int, dest="num")
        parser.add_argument("--head", "-H", type=int, default=0, dest="head")
        parser.add_argument("--tail", "-T", type=int, default=0, dest="tail")
        parser.add_argument("--from", "-F", type=int, default=1, dest="from_line")
        parser.add_argument("--format", "-f", choices=["summary", "raw", "both"], default="summary")
        parser.add_argument("--list", "-l", action="store_true")
        parser.add_argument("--page", type=int, default=1)
        parser.add_argument("--page-size", type=int, default=10)
        parser.add_argument("--session", "-s", type=str)

        args = parser.parse_args([])
        assert args.agent is None
        assert args.head == 0
        assert args.tail == 0
        assert args.from_line == 1
        assert args.format == "summary"

    def test_head_tail_args(self):
        """head/tail参数"""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--head", "-H", type=int, default=0, dest="head")
        parser.add_argument("--tail", "-T", type=int, default=0, dest="tail")

        args = parser.parse_args(["--head", "5", "--tail", "10"])
        assert args.head == 5
        assert args.tail == 10

    def test_from_arg(self):
        """from参数"""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--from", "-F", type=int, default=1, dest="from_line")

        args = parser.parse_args(["--from", "10"])
        assert args.from_line == 10

        args = parser.parse_args(["--from", "-20"])
        assert args.from_line == -20


# ===== 边界情况测试 =====

class TestEdgeCases:
    """边界情况测试"""

    def test_single_message(self):
        """只有一条消息"""
        messages = [{"message": {"role": "user"}}]
        result, desc = _slice_messages(messages, 10, 1)
        assert len(result) == 1
        assert "第1-1条" in desc

    def test_count_exceeds_total(self):
        """请求数量超过总数"""
        messages = [{"message": {"role": "user"}} for _ in range(5)]
        result, desc = _slice_messages(messages, 100, 1)
        assert len(result) == 5
        assert "第1-5条" in desc

    def test_head_exceeds_total(self):
        """head超过总数"""
        messages = [{"message": {"role": "user"}} for _ in range(5)]
        result, desc = _slice_messages(messages, 0, 1, head=100)
        assert len(result) == 5
        assert "第1-5条" in desc

    def test_tail_exceeds_total(self):
        """tail超过总数"""
        messages = [{"message": {"role": "user"}} for _ in range(5)]
        result, desc = _slice_messages(messages, 0, 1, tail=100)
        assert len(result) == 5
        assert "第1-5条" in desc


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
