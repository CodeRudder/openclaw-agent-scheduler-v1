#!/usr/bin/env python3
"""
文件行号缓存模块

用于存储 Read 工具结果的行号信息，支持 Edit 工具查找真实行号。
使用 hash(line_content) -> line_num 映射，限制内存使用不超过 10MB。

内存估算：
- 每个文件：5000行 × (8字节hash + 4字节行号) = 60KB
- 100个文件：6MB
- 预留空间：4MB
- 总计：10MB
"""

import re
from typing import Dict, Optional

# 内存限制常量
MAX_CACHE_FILES = 100  # 最多缓存100个文件
MAX_LINES_PER_FILE = 5000  # 每个文件最多5000行

# 全局缓存（使用模块级变量）
_file_cache: Dict[str, Dict[int, int]] = {}  # {file_path: {hash(line): line_num}}
_cache_order: list = []  # [file_path, ...] 用于LRU淘汰


def _normalize_for_hash(s: str, exact: bool = True) -> str:
    """规范化字符串用于hash计算

    Args:
        s: 原始字符串
        exact: True=精确匹配（只strip），False=宽松匹配（移除所有空白）

    Returns:
        规范化后的字符串
    """
    if exact:
        return s.strip()
    return ''.join(s.split())


def _add_to_cache(file_path: str, hash_map: Dict[int, int]):
    """添加文件到缓存（带LRU淘汰）

    Args:
        file_path: 文件路径
        hash_map: {hash(line_content): line_num}
    """
    global _file_cache, _cache_order

    # 如果已存在，移到末尾（最近使用）
    if file_path in _file_cache:
        _cache_order.remove(file_path)
    else:
        # 检查是否需要淘汰
        while len(_file_cache) >= MAX_CACHE_FILES:
            # 淘汰最久未使用的
            oldest = _cache_order.pop(0)
            del _file_cache[oldest]

    # 限制每个文件的行数（保留后面的行，通常Edit操作在文件末尾）
    if len(hash_map) > MAX_LINES_PER_FILE:
        items = list(hash_map.items())[-MAX_LINES_PER_FILE:]
        hash_map = dict(items)

    _file_cache[file_path] = hash_map
    _cache_order.append(file_path)


def parse_read_result(content: str, file_path: str = None) -> Dict[str, Dict[int, int]]:
    """解析Read工具的结果，提取文件内容

    支持两种格式：
    1. 带行号前缀：如 "     1→def hello():" 或 "     1│def hello():"
    2. 纯文本格式：无行号前缀，自动从1开始编号

    使用 hash(line_content) -> line_num 映射，减少内存占用

    Args:
        content: Read工具的输出内容
        file_path: 可选的文件路径（用于纯文本格式）

    Returns:
        {file_path: {hash(line_content): line_num, ...}}
    """
    result = {}
    if not content or not isinstance(content, str):
        return result

    lines = content.split('\n')
    current_file = None
    file_hash_map = {}

    # 匹配带行号的格式
    # 格式1: "     1→def hello():" 或 "     1│def hello():"
    # 格式2: "    10 | content"
    line_pattern = re.compile(r'^\s*(\d+)\s*[→│|]\s*(.*)$')

    # 匹配文件路径标记
    # 格式1: "Contents of file.py:"
    # 格式2: "→ file.py:"
    # 格式3: "file.py:"
    file_pattern = re.compile(
        r'^(?:→\s*)?(?:Contents of\s+)?'
        r'(.+\.(?:py|js|ts|java|go|rs|c|cpp|h|hpp|md|txt|json|yaml|yml|sh|html|css|xml))'
        r':?\s*$'
    )

    # 检测是否有带行号的行
    has_numbered_lines = any(line_pattern.match(line) for line in lines[:20])

    for line in lines:
        # 检测文件路径标记
        file_match = file_pattern.match(line.strip())
        if file_match:
            if current_file and file_hash_map:
                _add_to_cache(current_file, file_hash_map)
                result[current_file] = file_hash_map
            current_file = file_match.group(1).rstrip(':')
            file_hash_map = {}
            continue

        # 尝试解析带行号的行
        match = line_pattern.match(line)
        if match:
            line_num = int(match.group(1))
            line_content = match.group(2)
            # 使用 hash 存储，减少内存
            content_hash = hash(line_content.strip())
            file_hash_map[content_hash] = line_num

    # 保存最后一个文件
    if current_file and file_hash_map:
        _add_to_cache(current_file, file_hash_map)
        result[current_file] = file_hash_map
    elif not has_numbered_lines and file_path:
        # 纯文本格式：没有检测到带行号的行，使用传入的 file_path
        # 为每行生成递增行号（从1开始）
        file_hash_map = {}
        for line_num, line in enumerate(lines, start=1):
            content_hash = hash(line.rstrip())
            file_hash_map[content_hash] = line_num
        if file_hash_map:
            _add_to_cache(file_path, file_hash_map)
            result[file_path] = file_hash_map

    return result


def find_edit_start_line(file_cache: Dict[str, Dict[int, int]],
                         file_path: str, old_string: str) -> int:
    """在文件缓存中搜索 old_string 的起始行号

    匹配策略（按优先级）：
    1. Hash精确匹配：取前3行非空行，用hash精确匹配
    2. 宽松匹配：忽略空白差异后hash匹配
    3. 单行匹配：只用第一行非空行匹配

    Args:
        file_cache: 文件内容缓存 {file_path: {hash(line): line_num}}
        file_path: 文件路径
        old_string: 要替换的旧内容

    Returns:
        起始行号，未找到返回 0
    """
    if not old_string or not file_path:
        return 0

    # 尝试多种文件路径匹配
    file_hash_map = None
    target_name = file_path.split('/')[-1]

    for cached_path in file_cache:
        cached_name = cached_path.split('/')[-1]
        # 优先级：完全匹配 > 路径包含 > 文件名匹配
        if file_path == cached_path:
            file_hash_map = file_cache[cached_path]
            break
        elif file_path in cached_path or cached_path in file_path:
            file_hash_map = file_cache[cached_path]
            break
        elif cached_name == target_name:
            file_hash_map = file_cache[cached_path]
            # 不break，继续找更好的匹配

    if not file_hash_map:
        return 0

    old_lines = [l for l in old_string.split('\n') if l.strip()]
    if not old_lines:
        return 0

    # 策略1: Hash精确匹配（取前3行）
    pattern_lines = old_lines[:3]
    result = _match_by_hash(file_hash_map, pattern_lines, exact=True)
    if result > 0:
        return result

    # 策略2: 宽松匹配（忽略空白差异）
    result = _match_by_hash(file_hash_map, pattern_lines, exact=False)
    if result > 0:
        return result

    # 策略3: 单行匹配（只用第一行）
    if old_lines:
        result = _match_by_hash(file_hash_map, [old_lines[0]], exact=True)
        if result > 0:
            return result
        result = _match_by_hash(file_hash_map, [old_lines[0]], exact=False)
        if result > 0:
            return result

    return 0


def _match_by_hash(file_hash_map: Dict[int, int],
                   pattern_lines: list, exact: bool = True) -> int:
    """使用 hash 在文件缓存中搜索模式行

    Args:
        file_hash_map: 文件hash映射 {hash(line): line_num}
        pattern_lines: 要匹配的模式行
        exact: 是否精确匹配（False时忽略空白差异）

    Returns:
        起始行号，未找到返回 0
    """
    if not file_hash_map or not pattern_lines:
        return 0

    # 获取第一行的hash
    first_line = pattern_lines[0]
    first_line_hash = hash(_normalize_for_hash(first_line, exact=exact))

    # 在缓存中查找
    if first_line_hash in file_hash_map:
        return file_hash_map[first_line_hash]

    return 0


def get_cache_stats() -> dict:
    """获取缓存统计信息（用于调试）

    Returns:
        {files: 文件数, total_lines: 总行数, memory_mb: 估计内存使用(MB)}
    """
    total_lines = sum(len(v) for v in _file_cache.values())
    # 每行约12字节（8字节hash key + 4字节行号value）
    memory_bytes = total_lines * 12

    return {
        'files': len(_file_cache),
        'total_lines': total_lines,
        'memory_mb': round(memory_bytes / 1024 / 1024, 2)
    }


def clear_cache():
    """清空缓存"""
    global _file_cache, _cache_order
    _file_cache = {}
    _cache_order = []
