"""测试 Edit 工具的多种参数格式兼容性"""
import pytest
from scripts.check_agent_sessions import print_single_message


class TestEditToolFormats:
    """测试 Edit 工具的参数格式兼容性"""

    def test_claude_format(self, capsys):
        """测试 Claude 格式：file_path + old_string + new_string"""
        msg = {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "name": "edit",
                        "arguments": {
                            "file_path": "/path/to/file.py",
                            "old_string": "def hello():\n    print('hello')",
                            "new_string": "def hello():\n    print('Hello, World!')"
                        }
                    }
                ]
            },
            "timestamp": "2026-04-04T12:00:00Z"
        }

        file_cache = {}
        print_single_message(1, msg, file_cache)
        captured = capsys.readouterr()

        # 验证显示了文件路径和 diff
        assert "/path/to/file.py" in captured.out
        assert "print('hello')" in captured.out
        assert "print('Hello, World!')" in captured.out

    def test_openclaw_format(self, capsys):
        """测试 OpenClaw Agent 格式：path + oldText + newText"""
        msg = {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "name": "edit",
                        "arguments": {
                            "path": "/workspace/test.js",
                            "oldText": "const x = 1;",
                            "newText": "const x = 2;"
                        }
                    }
                ]
            },
            "timestamp": "2026-04-04T12:00:00Z"
        }

        file_cache = {}
        print_single_message(1, msg, file_cache)
        captured = capsys.readouterr()

        # 验证显示了文件路径和 diff
        assert "/workspace/test.js" in captured.out
        assert "const x = 1" in captured.out
        assert "const x = 2" in captured.out

    def test_mixed_format_path_old_string(self, capsys):
        """测试混合格式：path + old_string + new_string"""
        msg = {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "name": "edit",
                        "arguments": {
                            "path": "/home/user/app.ts",
                            "old_string": "export class App {}",
                            "new_string": "export class App {\n  constructor() {}\n}"
                        }
                    }
                ]
            },
            "timestamp": "2026-04-04T12:00:00Z"
        }

        file_cache = {}
        print_single_message(1, msg, file_cache)
        captured = capsys.readouterr()

        # 验证显示了文件路径和 diff
        assert "/home/user/app.ts" in captured.out
        assert "export class App {}" in captured.out
        assert "constructor()" in captured.out

    def test_mixed_format_file_path_oldtext(self, capsys):
        """测试混合格式：file_path + oldText + newText"""
        msg = {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "name": "edit",
                        "arguments": {
                            "file_path": "/src/utils.py",
                            "oldText": "def add(a, b):\n    return a + b",
                            "newText": "def add(a: int, b: int) -> int:\n    return a + b"
                        }
                    }
                ]
            },
            "timestamp": "2026-04-04T12:00:00Z"
        }

        file_cache = {}
        print_single_message(1, msg, file_cache)
        captured = capsys.readouterr()

        # 验证显示了文件路径和 diff
        assert "/src/utils.py" in captured.out
        assert "def add(a, b)" in captured.out
        assert "def add(a: int, b: int)" in captured.out

    def test_edit_without_old_string(self, capsys):
        """测试没有 old_string/oldText 的情况（应该只显示文件路径）"""
        msg = {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "name": "edit",
                        "arguments": {
                            "path": "/test/file.txt",
                            "new_string": "new content"
                        }
                    }
                ]
            },
            "timestamp": "2026-04-04T12:00:00Z"
        }

        file_cache = {}
        print_single_message(1, msg, file_cache)
        captured = capsys.readouterr()

        # 验证只显示了文件路径，没有 diff
        assert "/test/file.txt" in captured.out
        # 不应该有 diff 标记
        assert "📊" not in captured.out

    def test_tool_use_format(self, capsys):
        """测试 tool_use 格式（Claude API）"""
        msg = {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {
                            "file_path": "/code/main.py",
                            "old_string": "print('test')",
                            "new_string": "print('Test')"
                        }
                    }
                ]
            },
            "timestamp": "2026-04-04T12:00:00Z"
        }

        file_cache = {}
        print_single_message(1, msg, file_cache)
        captured = capsys.readouterr()

        # 验证显示了文件路径和 diff
        assert "/code/main.py" in captured.out
        assert "print('test')" in captured.out
        assert "print('Test')" in captured.out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
