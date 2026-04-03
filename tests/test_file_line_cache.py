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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
