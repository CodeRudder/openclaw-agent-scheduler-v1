#!/usr/bin/env python3
"""
file_line_cache.py 测试用例

测试覆盖：
- parse_read_result: 解析Read工具结果
- find_edit_start_line: 查找Edit起始行号
- LRU缓存淘汰
- 内存限制
- 多种匹配策略

运行：
  python3 -m pytest tests/test_file_line_cache.py -v
"""

import pytest
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

from file_line_cache import (
    parse_read_result,
    find_edit_start_line,
    _normalize_for_hash,
    _add_to_cache,
    get_cache_stats,
    clear_cache,
    MAX_CACHE_FILES,
    MAX_LINES_PER_FILE,
)


# ===== 测试数据 =====

SAMPLE_READ_RESULT = """
Contents of test.py:
     1→def hello():
     2→    print("hello")
     3→    return True
     4→
     5→def world():
     6→    print("world")
"""

SAMPLE_READ_RESULT_ALT = """
→ test.py:
     1│def hello():
     2│    print("hello")
     3│    return True
"""

SAMPLE_MULTIFILE = """
Contents of file1.py:
     1→def a():
     2→    pass

Contents of file2.py:
     1→def b():
     2→    pass
"""


# ===== normalize 测试 =====

class TestNormalize:
    """测试字符串规范化"""

    def test_exact_mode(self):
        """精确模式只strip"""
        assert _normalize_for_hash("  hello  ", exact=True) == "hello"
        assert _normalize_for_hash("\thello\n", exact=True) == "hello"

    def test_loose_mode(self):
        """宽松模式移除所有空白"""
        assert _normalize_for_hash("  hello  world  ", exact=False) == "helloworld"
        assert _normalize_for_hash("\t\nhello\n\tworld\n", exact=False) == "helloworld"


# ===== parse_read_result 测试 =====

class TestParseReadResult:
    """测试解析Read结果"""

    def setup_method(self):
        """每个测试前清空缓存"""
        clear_cache()

    def test_parse_basic_format(self):
        """解析基本格式"""
        result = parse_read_result(SAMPLE_READ_RESULT)
        assert "test.py" in result
        assert len(result["test.py"]) == 6  # 6行（包含空行）

    def test_parse_alt_format(self):
        """解析替代格式（│分隔符）"""
        result = parse_read_result(SAMPLE_READ_RESULT_ALT)
        assert "test.py" in result
        assert len(result["test.py"]) == 3

    def test_parse_multifile(self):
        """解析多文件"""
        result = parse_read_result(SAMPLE_MULTIFILE)
        assert "file1.py" in result
        assert "file2.py" in result

    def test_parse_empty(self):
        """解析空内容"""
        result = parse_read_result("")
        assert result == {}
        result = parse_read_result(None)
        assert result == {}

    def test_line_numbers_correct(self):
        """验证行号正确"""
        result = parse_read_result(SAMPLE_READ_RESULT)
        hash_map = result.get("test.py", {})

        # 验证某些行的hash存在
        hello_hash = hash("def hello():")
        assert hello_hash in hash_map
        assert hash_map[hello_hash] == 1


# ===== find_edit_start_line 测试 =====

class TestFindEditStartLine:
    """测试查找Edit起始行"""

    def setup_method(self):
        """每个测试前清空缓存"""
        clear_cache()

    def test_find_exact_match(self):
        """精确匹配"""
        file_cache = parse_read_result(SAMPLE_READ_RESULT)
        old_string = 'def hello():\n    print("hello")\n    return True'
        result = find_edit_start_line(file_cache, "test.py", old_string)
        assert result == 1

    def test_find_partial_lines(self):
        """只匹配部分行"""
        file_cache = parse_read_result(SAMPLE_READ_RESULT)
        old_string = 'def hello():\n    print("hello")'
        result = find_edit_start_line(file_cache, "test.py", old_string)
        assert result == 1

    def test_find_single_line(self):
        """单行匹配"""
        file_cache = parse_read_result(SAMPLE_READ_RESULT)
        old_string = 'def world():'
        result = find_edit_start_line(file_cache, "test.py", old_string)
        assert result == 5

    def test_find_with_different_whitespace(self):
        """宽松匹配（空白差异）"""
        file_cache = parse_read_result(SAMPLE_READ_RESULT)
        old_string = 'def hello():\n    print("hello")'  # 可能与缓存中的缩进不同
        result = find_edit_start_line(file_cache, "test.py", old_string)
        # 应该能找到（宽松匹配）
        assert result >= 1

    def test_not_found(self):
        """未找到"""
        file_cache = parse_read_result(SAMPLE_READ_RESULT)
        old_string = 'def nonexistent():'
        result = find_edit_start_line(file_cache, "test.py", old_string)
        assert result == 0

    def test_empty_inputs(self):
        """空输入"""
        assert find_edit_start_line({}, "test.py", "content") == 0
        assert find_edit_start_line({"test.py": {}}, "", "content") == 0
        assert find_edit_start_line({"test.py": {}}, "test.py", "") == 0

    def test_path_fuzzy_match(self):
        """路径模糊匹配"""
        file_cache = parse_read_result(SAMPLE_READ_RESULT)
        # 使用完整路径查找
        result = find_edit_start_line(file_cache, "/path/to/test.py", "def hello():")
        assert result == 1


# ===== LRU缓存测试 =====

class TestLRUCache:
    """测试LRU缓存淘汰"""

    def setup_method(self):
        clear_cache()

    def test_add_to_cache(self):
        """添加到缓存"""
        hash_map = {hash("line1"): 1, hash("line2"): 2}
        _add_to_cache("file1.py", hash_map)
        stats = get_cache_stats()
        assert stats['files'] == 1
        assert stats['total_lines'] == 2

    def test_lru_eviction(self):
        """LRU淘汰"""
        # 添加超过限制的文件
        for i in range(MAX_CACHE_FILES + 10):
            hash_map = {hash(f"line{i}"): i}
            _add_to_cache(f"file{i}.py", hash_map)

        stats = get_cache_stats()
        assert stats['files'] <= MAX_CACHE_FILES

    def test_update_moves_to_end(self):
        """更新文件移到末尾（最近使用）"""
        _add_to_cache("file1.py", {hash("a"): 1})
        _add_to_cache("file2.py", {hash("b"): 2})
        _add_to_cache("file1.py", {hash("c"): 3})  # 更新file1

        from file_line_cache import _cache_order
        # file1应该移到最后
        assert _cache_order[-1] == "file1.py"


# ===== 内存限制测试 =====

class TestMemoryLimit:
    """测试内存限制"""

    def setup_method(self):
        clear_cache()

    def test_lines_per_file_limit(self):
        """每个文件行数限制"""
        # 创建一个超大的hash_map
        hash_map = {hash(f"line{i}"): i for i in range(MAX_LINES_PER_FILE + 1000)}
        _add_to_cache("big_file.py", hash_map)

        stats = get_cache_stats()
        # 应该被截断到MAX_LINES_PER_FILE
        assert stats['total_lines'] <= MAX_LINES_PER_FILE

    def test_memory_estimate(self):
        """内存估算"""
        hash_map = {hash(f"line{i}"): i for i in range(1000)}
        _add_to_cache("test.py", hash_map)

        stats = get_cache_stats()
        # 1000行 * 12字节 = 12000字节 ≈ 0.01 MB
        assert stats['memory_mb'] < 1


# ===== 纯文本格式测试 =====

class TestParsePlainText:
    """测试解析纯文本格式（OpenClaw Agent Read 结果）"""

    def setup_method(self):
        clear_cache()

    def test_parse_plain_text_with_file_path(self):
        """解析纯文本格式（带 file_path 参数）"""
        content = """# IDENTITY.md

## 角色信息

- Name: Fullstack Developer
- Role: 全栈开发工程师

## 核心职责

### 前端开发
1. Web应用开发
2. 移动应用开发
"""
        result = parse_read_result(content, file_path="IDENTITY.md")
        assert "IDENTITY.md" in result
        hash_map = result["IDENTITY.md"]
        # 验证某些行的 hash 存在
        assert hash("# IDENTITY.md") in hash_map
        assert hash_map[hash("# IDENTITY.md")] == 1
        assert hash("- Name: Fullstack Developer") in hash_map

    def test_parse_plain_text_line_numbers(self):
        """验证纯文本格式的行号从1开始"""
        content = "line1\nline2\nline3"
        result = parse_read_result(content, file_path="test.txt")
        hash_map = result.get("test.txt", {})
        assert hash_map.get(hash("line1")) == 1
        assert hash_map.get(hash("line2")) == 2
        assert hash_map.get(hash("line3")) == 3

    def test_parse_plain_text_empty_content(self):
        """空内容"""
        result = parse_read_result("", file_path="test.txt")
        assert result == {} or result.get("test.txt") == {}

    def test_find_line_in_plain_text(self):
        """在纯文本缓存中查找行"""
        content = """### 核心特质
- 精通前后端开发技术
- 熟悉DevOps工具和流程
- 快速响应和解决问题
"""
        result = parse_read_result(content, file_path="IDENTITY.md")
        file_cache = result

        # 查找多行内容
        old_string = "### 核心特质\n- 精通前后端开发技术\n- 熟悉DevOps工具和流程"
        line_num = find_edit_start_line(file_cache, "IDENTITY.md", old_string)
        assert line_num == 1  # 第一行开始

    def test_find_line_in_plain_text_middle(self):
        """在纯文本缓存中间查找行"""
        content = """# Header

## Section 1
content 1

## Section 2
content 2
"""
        result = parse_read_result(content, file_path="test.md")
        file_cache = result

        old_string = "## Section 2\ncontent 2"
        line_num = find_edit_start_line(file_cache, "test.md", old_string)
        assert line_num == 6  # 第6行开始


# ===== 真实会话消息测试 =====

class TestRealSessionData:
    """使用真实会话数据片段测试"""

    def setup_method(self):
        clear_cache()

    def test_real_openclaw_read_result(self):
        """测试 OpenClaw Agent 真实 Read 结果"""
        # 从真实会话中提取的 IDENTITY.md 片段
        content = """# IDENTITY.md - 身份定义

## 🤖 自身定位

**我是AI Agent，非人类！**

### 核心特征
- ✅ AI驱动的Agent，24/7工作
- ✅ 无需睡眠、休息、下班时间
- ✅ 任务到达立即响应
- ✅ 随时监控项目进度

### 禁止行为
- ❌ 说"晚安"、"休息"、"下班"、"明日继续"
- ❌ 因时间原因延迟任务

---

## 🎯 角色信息

- **Name:** Fullstack Developer (全栈开发工程师)
- **Role:** 全栈开发工程师 / DevOps工程师
- **Vibe:** 技术全面、追求卓越、快速响应
- **专长:** 前端开发、后端开发、DevOps、全栈技术选型
- **能力:** 独立完成前后端开发、配置CI/CD、部署运维

### 核心特质
- 精通前后端开发技术
- 熟悉DevOps工具和流程
- 快速响应和解决问题
- 追求代码质量和系统稳定性
"""
        result = parse_read_result(content, file_path="/home/user/.openclaw/workspace/IDENTITY.md")
        file_cache = result

        # 查找真实 Edit 操作的 old_string
        old_string = """- **Name:** Fullstack Developer (全栈开发工程师)
- **Role:** 全栈开发工程师 / DevOps工程师
- **Vibe:** 技术全面、追求卓越、快速响应
- **专长:** 前端开发、后端开发、DevOps、全栈技术选型
- **能力:** 独立完成前后端开发、配置CI/CD、部署运维

### 核心特质
- 精通前后端开发技术
- 熟悉DevOps工具和流程
- 快速响应和解决问题
- 追求代码质量和系统稳定性"""

        line_num = find_edit_start_line(
            file_cache,
            "/home/user/.openclaw/workspace/IDENTITY.md",
            old_string
        )
        # 应该在第21行开始（角色信息部分）
        assert line_num == 21

    def test_real_claude_read_result(self):
        """测试 Claude 项目格式（带行号前缀）"""
        # Claude 项目的 Read 工具结果格式
        content = """Contents of scheduler.py:
     1→def hello():
     2→    print("hello")
     3→    return True
     4→
     5→def world():
     6→    print("world")
"""
        result = parse_read_result(content)
        assert "scheduler.py" in result
        hash_map = result["scheduler.py"]
        assert hash_map[hash('def hello():')] == 1
        assert hash_map[hash('def world():')] == 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
