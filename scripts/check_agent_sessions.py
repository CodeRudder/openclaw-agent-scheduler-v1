#!/usr/bin/env python3
"""
检查所有工作群agent的会话状态

用法：
  # 查看所有agent状态概览
  python3 scripts/check_agent_sessions.py

  # 查看指定agent的最新会话消息
  python3 scripts/check_agent_sessions.py fullstack-dev           # 最后10条消息（摘要格式）
  python3 scripts/check_agent_sessions.py fullstack-dev 20        # 最后20条消息
  python3 scripts/check_agent_sessions.py fullstack-dev -n 5      # 最后5条消息
  python3 scripts/check_agent_sessions.py fullstack-dev -n 0      # 全部消息

  # 行定位
  python3 scripts/check_agent_sessions.py fullstack-dev --head 5        # 前5条
  python3 scripts/check_agent_sessions.py fullstack-dev --tail 20       # 后20条
  python3 scripts/check_agent_sessions.py fullstack-dev -H 3 -T 3      # 前3条+后3条
  python3 scripts/check_agent_sessions.py fullstack-dev --from 10 -n 5  # 从第10条开始，显示5条
  python3 scripts/check_agent_sessions.py fullstack-dev --from -20      # 从倒数第20条开始

  # 列出agent的所有会话
  python3 scripts/check_agent_sessions.py fullstack-dev --list
  python3 scripts/check_agent_sessions.py fullstack-dev -l

  # 查看指定会话（支持部分匹配）
  python3 scripts/check_agent_sessions.py --session /full/path/to/session.jsonl  # 完整路径
  python3 scripts/check_agent_sessions.py fullstack-dev -s 5417ea64              # UUID片段匹配
  python3 scripts/check_agent_sessions.py fullstack-dev -s 245d89c2 20 -f raw   # 组合使用

格式选项：
  summary - 内容摘要（默认）
  raw     - 原始JSON格式
  both    - 摘要和原始都显示

行定位参数：
  -n, --num N    显示N条消息（0=全部）
  --head, -H N   显示前N条
  --tail, -T N   显示后N条
  --from, -F N   起始行号。正数=第N条（1-based），负数=倒数第|N|条
"""

import json
import sys
import argparse
import time
from pathlib import Path
from datetime import datetime, timezone

# Agent会话目录
AGENTS_BASE = Path("/home/gongdewei/.openclaw/agents")

# Claude项目目录
CLAUDE_PROJECTS_BASE = Path.home() / ".claude" / "projects"

# 项目根目录
PROJECT_DIR = Path(__file__).parent.parent

# Follow模式刷新间隔（秒）
FOLLOW_INTERVAL = 2

# 工作群配置
GROUPS = {
    "dev-working-group": {
        "name": "开发工作群",
        "agents": ["fullstack-dev", "architect"]
    },
    "qa-acceptance-group": {
        "name": "验收测试群",
        "agents": ["qa", "product"]
    },
    "ops-release-group": {
        "name": "运维发布群",
        "agents": ["ops", "architect"]
    },
    "plan-design-group": {
        "name": "规划设计群",
        "agents": ["product", "ui-designer", "architect", "qa"]
    }
}


# ===== Claude项目相关函数 =====

def get_claude_projects() -> list:
    """获取所有Claude项目

    Returns:
        [(dir_name, dir_path, display_name), ...]
        display_name: 从路径中提取的可读项目名
    """
    if not CLAUDE_PROJECTS_BASE.exists():
        return []

    projects = []
    for project_dir in CLAUDE_PROJECTS_BASE.iterdir():
        if not project_dir.is_dir():
            continue
        # 跳过隐藏目录
        if project_dir.name.startswith('.'):
            continue

        # 从目录名提取可读项目名
        # 例如: -home-gongdewei-work-projects-code-rudder-openclaw-agent-scheduler
        # -> code-rudder/openclaw-agent-scheduler
        dir_name = project_dir.name
        parts = dir_name.split('-')
        # 过滤掉常见的home前缀
        display_parts = []
        skip = True
        for part in parts:
            if skip and part in ('home', 'gongdewei', 'work', 'projects'):
                continue
            skip = False
            display_parts.append(part)

        display_name = '/'.join(display_parts) if display_parts else dir_name
        projects.append((dir_name, project_dir, display_name))

    return sorted(projects, key=lambda x: x[2])


def find_claude_project(query: str) -> Path:
    """根据项目名模糊匹配查找Claude项目

    Args:
        query: 项目名或部分项目名

    Returns:
        项目目录路径，未找到返回None
    """
    projects = get_claude_projects()

    if not projects:
        return None

    # 先尝试精确匹配
    for dir_name, dir_path, display_name in projects:
        if query == dir_name or query == display_name:
            return dir_path

    # 再尝试模糊匹配
    matches = []
    for dir_name, dir_path, display_name in projects:
        if query.lower() in dir_name.lower() or query.lower() in display_name.lower():
            matches.append((dir_name, dir_path, display_name))

    if len(matches) == 1:
        return matches[0][1]

    if len(matches) > 1:
        print(f"⚠️ 找到 {len(matches)} 个匹配 '{query}' 的项目：")
        for i, (dir_name, dir_path, display_name) in enumerate(matches, 1):
            print(f"  [{i}] {display_name} ({dir_name})")
        print(f"\n💡 请使用更精确的项目名")
        return None

    return None


def get_claude_project_sessions(project_dir: Path) -> list:
    """获取Claude项目的会话文件列表

    Args:
        project_dir: 项目目录路径

    Returns:
        会话文件路径列表，按修改时间倒序
    """
    if not project_dir.exists():
        return []

    # 查找所有.jsonl文件
    files = list(project_dir.glob("*.jsonl"))

    # 排除备份文件
    files = [f for f in files if "backup" not in f.name.lower()]

    return sorted(files, key=lambda x: x.stat().st_mtime, reverse=True)


# ===== OpenClaw Agent相关函数 =====

def list_all_agents(limit: int = 0) -> list:
    """列出所有OpenClaw agents

    Args:
        limit: 限制返回数量（0=全部）

    Returns:
        [(agent_name, session_count, latest_mtime), ...]
    """
    if not AGENTS_BASE.exists():
        return []

    agents = []
    for agent_dir in AGENTS_BASE.iterdir():
        if not agent_dir.is_dir():
            continue

        agent_name = agent_dir.name
        sessions_dir = agent_dir / "sessions"

        if not sessions_dir.exists():
            continue

        # 获取会话文件数量
        session_files = get_session_files(agent_name)
        session_count = len(session_files)

        if session_count == 0:
            continue

        # 获取最新会话的修改时间
        latest_mtime = session_files[0].stat().st_mtime

        agents.append((agent_name, session_count, latest_mtime))

    # 按最新修改时间排序
    agents.sort(key=lambda x: x[2], reverse=True)

    if limit > 0:
        return agents[:limit]

    return agents


# ===== Follow模式 =====

def follow_session(session_path: Path, count: int = 10):
    """Follow模式：持续监控会话新消息

    Args:
        session_path: 会话文件路径
        count: 显示最后N条消息（用于初始显示）
    """
    import signal

    # 处理Ctrl+C
    def signal_handler(sig, frame):
        print("\n\n✅ 退出Follow模式")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    print(f"📡 Follow模式: {session_path}")
    print(f"   每{FOLLOW_INTERVAL}秒刷新，按Ctrl+C退出")
    print("=" * 80)

    last_count = 0

    # 首次显示最后count条消息
    session_info = parse_session_file(session_path)
    messages = session_info.get("raw_messages", [])
    total = len(messages)

    if total > 0:
        display_count = min(count, total)
        for i, raw_msg in enumerate(messages[-display_count:], total - display_count + 1):
            print_single_message(i, raw_msg)
        last_count = total

    print("=" * 80)
    print(f"📊 当前共 {total} 条消息， 等待新消息...")

    # 持续监控
    while True:
        time.sleep(FOLLOW_INTERVAL)

        session_info = parse_session_file(session_path)
        messages = session_info.get("raw_messages", [])
        current_count = len(messages)

        if current_count > last_count:
            # 只显示新增的消息
            new_messages = messages[last_count:]
            for i, raw_msg in enumerate(new_messages, last_count + 1):
                print_single_message(i, raw_msg)
            print("-" * 40)
            print(f"📊 共 {current_count} 条消息 (+{current_count - last_count})")
            last_count = current_count


def print_single_message(line_num: int, raw_msg: dict):
    """打印单条消息（用于Follow模式）"""
    msg = raw_msg.get("message", {})
    timestamp = raw_msg.get("timestamp")

    role = msg.get("role", "?")
    content_raw = msg.get("content", "")
    content = extract_content_full(content_raw, max_len=200)

    # 格式化时间
    time_str = ""
    if timestamp:
        try:
            if isinstance(timestamp, (int, float)):
                ts = datetime.fromtimestamp(timestamp / 1000)
            else:
                ts = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                if ts.tzinfo is not None:
                    ts = ts.astimezone().replace(tzinfo=None)
            time_str = ts.strftime('%H:%M:%S')
        except:
            time_str = str(timestamp)[:8] if timestamp else ""

    role_icon = "🤖" if role == "assistant" else "👤" if role == "user" else "🔧"
    print(f"\n[{line_num}] {role_icon} {role} | ⏰ {time_str or '无时间'}")
    if content and content.strip():
        print(f"    📝 {content}")


def get_session_files(agent_name: str, include_backups: bool = False) -> list:
    """获取agent的所有会话文件

    Args:
        agent_name: agent名称
        include_backups: 是否包含备份/重置文件
    """
    session_dir = AGENTS_BASE / agent_name / "sessions"
    if not session_dir.exists():
        return []

    if include_backups:
        # 包含所有jsonl文件（包括备份和重置）
        files = list(session_dir.glob("*.jsonl")) + list(session_dir.glob("*.jsonl.*"))
    else:
        # 只包含主会话文件（排除备份）
        files = [f for f in session_dir.glob("*.jsonl") if "backup" not in f.name.lower()]

    return sorted(files, key=lambda x: x.stat().st_mtime, reverse=True)


def parse_session_file(file_path: Path) -> dict:
    """解析会话文件，提取关键信息

    支持多种JSONL格式：
    1. Agent会话格式: {"type": "message", "message": {"role": "assistant", ...}}
    2. Claude项目格式: {"type": "user"|"assistant", "message": {...}}
    """
    result = {
        "file": str(file_path),
        "mtime": datetime.fromtimestamp(file_path.stat().st_mtime),
        "messages": [],
        "raw_messages": [],  # 保存原始消息（包含时间戳）
        "last_message": None,
        "stop_reason": None
    }

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    msg_type = msg.get("type", "")

                    # 格式1: Agent会话格式 {"type": "message", "message": {...}}
                    if msg_type == "message":
                        message = msg.get("message", {})
                        result["messages"].append(message)
                        result["raw_messages"].append({
                            "timestamp": msg.get("timestamp"),
                            "message": message
                        })

                    # 格式2: Claude项目格式 {"type": "user"|"assistant", "message": {...}}
                    elif msg_type in ("user", "assistant"):
                        message = msg.get("message", {})
                        # 确保message有role字段
                        if isinstance(message, dict) and "role" not in message:
                            message = dict(message)  # 复制避免修改原对象
                            message["role"] = msg_type
                        result["messages"].append(message)
                        result["raw_messages"].append({
                            "timestamp": msg.get("timestamp"),
                            "message": message
                        })

                except json.JSONDecodeError:
                    continue

        # 获取最后一条消息
        if result["messages"]:
            last_msg = result["messages"][-1]
            result["last_message"] = {
                "role": last_msg.get("role", "?"),
                "content": extract_content(last_msg.get("content", "")),
                "stop_reason": last_msg.get("stopReason"),
                "error_message": last_msg.get("errorMessage")
            }
            result["stop_reason"] = last_msg.get("stopReason")

    except Exception as e:
        result["error"] = str(e)

    return result


def extract_content(content) -> str:
    """提取消息内容为字符串"""
    if isinstance(content, str):
        return content[:100] + "..." if len(content) > 100 else content
    elif isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(item.get("text", ""))
            elif isinstance(item, str):
                text_parts.append(item)
        full_text = "\n".join(text_parts)
        return full_text[:100] + "..." if len(full_text) > 100 else full_text
    return str(content)[:100]


def format_time_ago(dt: datetime) -> str:
    """格式化为'X分钟前'"""
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    diff = now - dt
    minutes = int(diff.total_seconds() / 60)

    if minutes < 1:
        return "刚刚"
    elif minutes < 60:
        return f"{minutes}分钟前"
    elif minutes < 1440:
        return f"{minutes // 60}小时前"
    else:
        return f"{minutes // 1440}天前"


def get_status_icon(stop_reason: str) -> str:
    """获取状态图标"""
    if stop_reason is None:
        return "🔄 运行中"
    elif stop_reason == "endTurn":
        return "✅ 正常结束"
    elif stop_reason == "toolUse":
        return "🔧 工具调用"
    elif stop_reason == "stop":
        return "⏹️ 主动停止"
    elif stop_reason == "aborted":
        return "❌ 异常终止"
    elif stop_reason == "error":
        return "🔴 错误"
    else:
        return f"❓ {stop_reason}"


def list_sessions(agent_name: str, include_backups: bool = True, page: int = 1, page_size: int = 10):
    """列出agent的所有会话文件

    Args:
        agent_name: agent名称
        include_backups: 是否包含备份/重置文件
        page: 页码（从1开始）
        page_size: 每页数量（默认10）
    """
    session_files = get_session_files(agent_name, include_backups=include_backups)

    if not session_files:
        print(f"❌ Agent '{agent_name}' 无会话文件")
        return

    total = len(session_files)
    total_pages = (total + page_size - 1) // page_size

    # 计算分页范围
    start_idx = (page - 1) * page_size
    end_idx = min(start_idx + page_size, total)
    page_files = session_files[start_idx:end_idx]

    print("=" * 80)
    print(f"📋 {agent_name} 会话列表")
    print(f"   共 {total} 个会话，当前第 {page}/{total_pages} 页（每页 {page_size} 个）")
    if include_backups:
        print("   📌 包含备份/重置文件")
    print("=" * 80)

    for i, file_path in enumerate(page_files, start_idx + 1):
        session_info = parse_session_file(file_path)
        mtime = session_info["mtime"]
        msg_count = len(session_info.get("messages", []))
        stop_reason = session_info.get("stop_reason")

        # 文件类型标记
        file_type = ""
        if ".reset" in file_path.name:
            file_type = "🔄[重置]"
        elif ".deleted" in file_path.name:
            file_type = "🗑️[已删除]"
        elif ".backup" in file_path.name.lower():
            file_type = "📦[备份]"

        # 状态标记
        status_mark = ""
        if stop_reason == "error":
            status_mark = "🔴"
        elif stop_reason == "aborted":
            status_mark = "❌"
        elif stop_reason == "stop":
            status_mark = "⏹️"
        elif stop_reason in ("endTurn", "toolUse"):
            status_mark = "✅"
        else:
            status_mark = "❓"

        # 文件大小
        file_size = file_path.stat().st_size
        size_str = f"{file_size / 1024:.1f}KB" if file_size > 1024 else f"{file_size}B"

        print(f"\n[{i}] {status_mark}{file_type} {file_path.name}")
        print(f"    📄 路径: {file_path}")
        print(f"    🕐 修改: {mtime.strftime('%Y-%m-%d %H:%M:%S')} ({format_time_ago(mtime)})")
        print(f"    📊 消息: {msg_count} 条 | 大小: {size_str}")
        print(f"    📌 状态: {get_status_icon(stop_reason)}")

        # 显示最后消息摘要
        if session_info.get("last_message"):
            last_msg = session_info["last_message"]
            content = last_msg.get("content", "(空)")
            if content and content != "(空)":
                print(f"    💬 最后: {content[:80]}...")

    print(f"\n{'=' * 80}")
    # 分页提示
    if page < total_pages:
        print(f"📄 下一页: {agent_name} -l --page {page + 1}")
    if page > 1:
        print(f"📄 上一页: {agent_name} -l --page {page - 1}")
    print(f"💡 查看指定会话: {agent_name} -s <UUID片段>")
    print(f"💡 查看全部: {agent_name} -l --page 1 --page-size {total}")


def find_session_by_partial(agent_name: str, partial: str, include_backups: bool = True) -> Path:
    """通过部分匹配查找会话文件

    Args:
        agent_name: agent名称
        partial: 部分文件名或UUID片段
        include_backups: 是否包含备份/重置文件

    Returns:
        匹配的文件路径，如果多个匹配返回None并显示列表，无匹配返回None
    """
    session_files = get_session_files(agent_name, include_backups=include_backups)

    if not session_files:
        print(f"❌ Agent '{agent_name}' 无会话文件")
        return None

    # 匹配包含partial的文件
    matches = [f for f in session_files if partial.lower() in f.name.lower()]

    if not matches:
        print(f"❌ 未找到匹配 '{partial}' 的会话文件")
        print(f"💡 提示: 使用 '{agent_name} --list' 查看所有会话")
        return None

    if len(matches) == 1:
        return matches[0]

    # 多个匹配，显示列表让用户选择
    print(f"⚠️ 找到 {len(matches)} 个匹配 '{partial}' 的会话：")
    for i, f in enumerate(matches, 1):
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        file_type = ""
        if ".reset" in f.name:
            file_type = " 🔄[重置]"
        elif ".deleted" in f.name:
            file_type = " 🗑️[已删除]"
        elif ".backup" in f.name.lower():
            file_type = " 📦[备份]"
        print(f"  [{i}]{file_type} {f.name} ({mtime.strftime('%m-%d %H:%M')})")
    print(f"\n💡 请使用更精确的匹配字符串")
    return None


def _slice_messages(raw_messages: list, count: int, from_line: int = 1,
                    head: int = 0, tail: int = 0) -> tuple:
    """根据参数切片消息列表

    Args:
        raw_messages: 全部消息列表
        count: 显示数量（0=全部）
        from_line: 起始行号（1-based）。正数=第N条，负数=倒数第|N|条
        head: 取前N条（与tail可同时使用，拼接头+尾）
        tail: 取后N条（与head可同时使用，拼接头+尾）

    Returns:
        (sliced_messages, range_desc)
        sliced_messages: 带原始行号的消息列表 [(line_num, msg), ...]
        range_desc: 范围描述字符串，如 "第1-5条 / 共100条"
    """
    total = len(raw_messages)
    if total == 0:
        return [], "共 0 条"

    # head/tail 模式：拼接头+尾
    if head > 0 or tail > 0:
        result = []

        if head > 0 and tail > 0:
            # 检查是否重叠
            if head + tail >= total:
                # 全部显示
                for i, raw_msg in enumerate(raw_messages, 1):
                    result.append((i + 1, raw_msg))
                range_desc = f"第1-{total}条 / 共{total}条"
            else:
                # 头部
                for i in range(head):
                    result.append((i + 1, raw_messages[i]))
                # 尾部
                for i in range(total - tail, total):
                    result.append((i + 1, raw_messages[i]))
                range_desc = f"第1-{head}条 + 第{total - tail + 1}-{total}条 / 共{total}条"
        elif head > 0:
            end_line = min(head, total)
            for i in range(end_line):
                result.append((i + 1, raw_messages[i]))
            range_desc = f"第1-{end_line}条 / 共{total}条"
        else:
            start_line = max(1, total - tail + 1)
            for i in range(start_line - 1, total):
                result.append((i + 1, raw_messages[i]))
            range_desc = f"第{start_line}-{total}条 / 共{total}条"

        return result, range_desc

    # from_line 模式
    if from_line >= 0:
        start = max(0, from_line - 1)
    else:
        start = max(0, total + from_line)

    if start >= total:
        return [], f"第{from_line}条起（超出范围）/ 共{total}条"

    end = total if count <= 0 else min(start + count, total)

    result = []
    for i in range(start, end):
        result.append((i + 1, raw_messages[i]))

    start_line = start + 1
    end_line = start + len(result)
    range_desc = f"第{start_line}-{end_line}条 / 共{total}条"

    return result, range_desc


def show_session_file(session_path: str, count: int = 10, msg_format: str = "summary",
                      agent_name: str = None, from_line: int = 1,
                      head: int = 0, tail: int = 0):
    """显示指定会话文件的内容

    Args:
        session_path: 会话文件路径或部分匹配字符串
        count: 显示消息数量（0=全部）
        msg_format: 消息格式
        agent_name: 如果提供，则使用部分匹配查找
        from_line: 起始行号（1-based）。正数=第N条，负数=倒数第|N|条
        head: 取前N条
        tail: 取后N条
    """
    # 如果提供了agent_name，尝试部分匹配（包含备份文件）
    if agent_name:
        file_path = find_session_by_partial(agent_name, session_path, include_backups=True)
        if not file_path:
            return
    else:
        file_path = Path(session_path)
        if not file_path.exists():
            print(f"❌ 会话文件不存在: {session_path}")
            return

    if not file_path.suffix == ".jsonl":
        print(f"⚠️ 文件不是 .jsonl 格式: {file_path}")

    session_info = parse_session_file(file_path)

    raw_messages = session_info.get("raw_messages", [])
    total_msgs = len(raw_messages)

    # 切片消息，返回带行号的列表
    sliced_msgs, range_desc = _slice_messages(
        raw_messages, count, from_line, head=head, tail=tail)

    print("=" * 80)
    print(f"📄 会话文件: {file_path}")
    print(f"🕐 修改: {session_info['mtime'].strftime('%Y-%m-%d %H:%M:%S')} ({format_time_ago(session_info['mtime'])})")
    print(f"📌 状态: {get_status_icon(session_info.get('stop_reason'))}")
    print(f"📝 格式: {msg_format}")
    print(f"📊 范围: {range_desc}")
    print("=" * 80)

    if not sliced_msgs:
        print("（无消息）")
        return

    for line_num, raw_msg in sliced_msgs:
        msg = raw_msg.get("message", {})
        timestamp = raw_msg.get("timestamp")

        role = msg.get("role", "?")
        content_raw = msg.get("content", "")
        content = extract_content_full(content_raw)
        stop_reason = msg.get("stopReason")
        error_msg = msg.get("errorMessage")
        msg_type = raw_msg.get("type", "message")

        # 格式化时间（转换为本地时间）
        time_str = ""
        if timestamp:
            try:
                if isinstance(timestamp, (int, float)):
                    # 毫秒时间戳转本地时间
                    ts = datetime.fromtimestamp(timestamp / 1000)
                else:
                    # ISO格式转本地时间
                    ts = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    # 如果有时区信息，转换为本地时间
                    if ts.tzinfo is not None:
                        ts = ts.astimezone().replace(tzinfo=None)
                time_str = ts.strftime('%H:%M:%S')
            except:
                time_str = str(timestamp)[:8] if timestamp else ""

        # 角色图标
        role_icon = "🤖" if role == "assistant" else "👤" if role == "user" else "🔧" if role == "toolResult" else "❓"

        # 消息类型
        type_str = f"({msg_type})" if msg_type != "message" else ""

        print(f"\n[{line_num}] {role_icon} {role} {type_str} | ⏰ {time_str or '无时间'}")

        # 根据format参数决定显示内容
        if msg_format in ("summary", "both"):
            # 内容摘要
            if content and content.strip():
                print(f"    📝 摘要: {content}")
            else:
                print(f"    📝 摘要: (无内容)")

        if msg_format in ("raw", "both"):
            # 原始JSON格式 - 显示完整message对象（不截断）
            raw_json = json.dumps(msg, ensure_ascii=False, indent=2)
            print(f"    📦 原始JSON: {raw_json}")

        # 显示停止原因
        if stop_reason:
            print(f"    ⏹ stopReason: {stop_reason}")

        # 显示错误信息
        if error_msg:
            print(f"    ❌ error: {error_msg[:200]}")

    print(f"\n{'=' * 80}")
    print(f"✅ 共显示 {len(sliced_msgs)} 条消息 / 共 {total_msgs} 条")


def show_last_messages(agent_name: str, count: int = 10, msg_format: str = "summary",
                       from_line: int = 1, head: int = 0, tail: int = 0):
    """显示指定agent的消息

    Args:
        agent_name: agent名称
        count: 显示消息数量（0=全部）
        msg_format: 消息格式 (summary/raw/both)
        from_line: 起始行号（1-based）。正数=第N条，负数=倒数第|N|条
        head: 取前N条
        tail: 取后N条
    """
    session_files = get_session_files(agent_name)

    if not session_files:
        print(f"❌ Agent '{agent_name}' 无会话文件")
        return

    latest_file = session_files[0]
    session_info = parse_session_file(latest_file)

    raw_messages = session_info.get("raw_messages", [])
    total_msgs = len(raw_messages)

    # 切片消息，返回带行号的列表
    sliced_msgs, range_desc = _slice_messages(
        raw_messages, count, from_line, head=head, tail=tail)

    print("=" * 80)
    print(f"📋 {agent_name} 消息")
    print(f"📄 会话文件: {latest_file}")
    print(f"🕐 活跃: {format_time_ago(session_info['mtime'])}")
    print(f"📌 状态: {get_status_icon(session_info.get('stop_reason'))}")
    print(f"📝 格式: {msg_format}")
    print(f"📊 范围: {range_desc}")
    print("=" * 80)

    if not sliced_msgs:
        print("（无消息）")
        return

    for line_num, raw_msg in sliced_msgs:
        msg = raw_msg.get("message", {})
        timestamp = raw_msg.get("timestamp")

        role = msg.get("role", "?")
        content_raw = msg.get("content", "")
        content = extract_content_full(content_raw)
        stop_reason = msg.get("stopReason")
        error_msg = msg.get("errorMessage")
        msg_type = raw_msg.get("type", "message")

        # 格式化时间（转换为本地时间）
        time_str = ""
        if timestamp:
            try:
                if isinstance(timestamp, (int, float)):
                    ts = datetime.fromtimestamp(timestamp / 1000)
                else:
                    ts = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    if ts.tzinfo is not None:
                        ts = ts.astimezone().replace(tzinfo=None)
                time_str = ts.strftime('%H:%M:%S')
            except:
                time_str = str(timestamp)[:8] if timestamp else ""

        role_icon = "🤖" if role == "assistant" else "👤" if role == "user" else "🔧" if role == "toolResult" else "❓"
        type_str = f"({msg_type})" if msg_type != "message" else ""

        print(f"\n[{line_num}] {role_icon} {role} {type_str} | ⏰ {time_str or '无时间'}")

        if msg_format in ("summary", "both"):
            if content and content.strip():
                print(f"    📝 摘要: {content}")
            else:
                print(f"    📝 摘要: (无内容)")

        if msg_format in ("raw", "both"):
            raw_json = json.dumps(msg, ensure_ascii=False, indent=2)
            print(f"    📦 原始JSON: {raw_json}")

        if stop_reason:
            print(f"    ⏹ stopReason: {stop_reason}")

        if error_msg:
            print(f"    ❌ error: {error_msg[:200]}")

    print(f"\n{'=' * 80}")
    print(f"✅ 共显示 {len(sliced_msgs)} 条消息 / 共 {total_msgs} 条")


def extract_content_full(content, max_len: int = 500) -> str:
    """提取消息内容（完整版，用于显示最后消息）

    过滤掉NO_REPLY等控制标记，跳过元数据显示实际内容
    """
    # 控制标记列表（这些不是实际内容，需要过滤）
    CONTROL_MARKERS = ["NO_REPLY", "NO_ACTION", "SILENT"]

    # 元数据模式（需要跳过的内容）
    METADATA_PATTERNS = [
        # System消息头
        r"^System:\s*\[[\d\-:\sGMT+]+\].*?from\s+@\w+",
        # Conversation info块
        r"Conversation info \(untrusted metadata\):",
        # JSON元数据块
        r'```json\s*\{[^}]*"message_id"[^}]*\}```',
    ]

    def clean_text(text: str) -> str:
        """清理文本，移除元数据"""
        import re
        # 过滤控制标记
        for marker in CONTROL_MARKERS:
            text = text.replace(marker, "")

        # 跳过System消息头和元数据
        lines = text.split('\n')
        content_lines = []
        skip_until_blank = False
        in_json_block = False

        for line in lines:
            # 检测System消息头
            if line.strip().startswith("System:"):
                skip_until_blank = True
                continue

            # 检测JSON块开始
            if "```json" in line and "message_id" in text:
                in_json_block = True
                continue

            # 检测JSON块结束
            if in_json_block and "```" in line:
                in_json_block = False
                continue

            if in_json_block:
                continue

            # 跳过空行后的元数据
            if skip_until_blank:
                if line.strip() == "" or line.strip().startswith("Conversation info"):
                    skip_until_blank = False
                continue

            # 跳过Conversation info行
            if "Conversation info" in line:
                skip_until_blank = True
                continue

            content_lines.append(line)

        return '\n'.join(content_lines).strip()

    if isinstance(content, str):
        content = clean_text(content)
        return content[:max_len] + "..." if len(content) > max_len else content
    elif isinstance(content, list):
        text_parts = []
        for item in content:
            if not isinstance(item, dict):
                if isinstance(item, str):
                    text = clean_text(item)
                    if text.strip():
                        text_parts.append(text.strip())
                continue
            item_type = item.get("type", "")
            if item_type == "text":
                text = item.get("text", "")
                text = clean_text(text)
                if text.strip():
                    text_parts.append(text.strip())
            elif item_type == "toolUse":
                text_parts.append(f"[工具调用: {item.get('name', '?')}]")
            elif item_type == "toolResult":
                text_parts.append(f"[工具结果: {item.get('toolUseId', '?')}]")
            elif item_type == "tool_use":
                name = item.get("name", "?")
                inp = item.get("input", {})
                if isinstance(inp, dict):
                    # 显示工具调用的关键参数
                    key_info = ""
                    if "command" in inp:
                        key_info = f": {inp['command'][:80]}"
                    elif "file_path" in inp:
                        key_info = f": {inp['file_path']}"
                    elif "pattern" in inp:
                        key_info = f": {inp['pattern']}"
                    text_parts.append(f"[工具调用: {name}{key_info}]")
                else:
                    text_parts.append(f"[工具调用: {name}]")
            elif item_type == "tool_result":
                result_content = item.get("content", "")
                if isinstance(result_content, str):
                    preview = result_content[:80].replace('\n', ' ')
                    text_parts.append(f"[工具结果: {preview}...]")
                else:
                    text_parts.append(f"[工具结果]")
            elif item_type == "thinking":
                thinking = item.get("thinking", "")
                if thinking:
                    preview = thinking[:80].replace('\n', ' ')
                    text_parts.append(f"[思考: {preview}...]")
        full_text = "\n".join(text_parts)
        return full_text[:max_len] + "..." if len(full_text) > max_len else full_text
    return str(content)[:max_len]


def main():
    parser = argparse.ArgumentParser(
        description="检查agent/Claude会话状态",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # OpenClaw Agent
  %(prog)s                                    # 查看所有agent状态概览
  %(prog)s --agents                           # 列出所有agent（详细信息）
  %(prog)s --agents -n 5                      # 只列出前5个agent
  %(prog)s fullstack-dev                      # 查看agent最后10条消息
  %(prog)s fullstack-dev 20 -f raw            # 查看最后20条消息（原始JSON）
  %(prog)s fullstack-dev --follow             # Follow模式，持续监控新消息
  %(prog)s fullstack-dev --list               # 列出agent会话（默认第1页，10条）
  %(prog)s fullstack-dev -s 5417ea64          # 通过UUID片段查看会话

  # Claude 项目
  %(prog)s --claude                            # 列出所有Claude项目
  %(prog)s -c openclaw-agent-scheduler         # 查看项目最新会话
  %(prog)s -c openclaw-agent-scheduler -l      # 列出项目会话文件
  %(prog)s -c openclaw-agent-scheduler --follow # Follow模式

行定位说明:
  --head N / -H N  显示前N条
  --tail N / -T N  显示后N条（可与--head同时使用，拼接头+尾）
  --from N / -F N  起始行号（1-based）
    N > 0: 从第N条开始
    N < 0: 从倒数第|N|条开始
    默认从第1条开始
        """
    )
    parser.add_argument("agent", nargs="?", help="指定agent名称或Claude项目名")
    parser.add_argument("count", nargs="?", type=int, default=None,
                        help="显示消息数量（默认10，0=全部）。也支持head/tail风格：-5=倒数5条")
    parser.add_argument("-n", "--num", type=int, dest="num",
                        help="显示消息数量（覆盖count参数，也用于--agents限制数量）")
    parser.add_argument("--head", "-H", type=int, default=0, dest="head",
                        help="显示前N条")
    parser.add_argument("--tail", "-T", type=int, default=0, dest="tail",
                        help="显示后N条（可与--head同时使用）")
    parser.add_argument("--from", "-F", type=int, default=1, dest="from_line",
                        help="起始行号。正数=第N条(1-based)，负数=倒数第|N|条")
    parser.add_argument("--format", "-f", choices=["summary", "raw", "both"], default="summary",
                        help="消息显示格式: summary(摘要), raw(原始JSON), both(两者)")
    parser.add_argument("--list", "-l", action="store_true", help="列出agent/项目的所有会话文件")
    parser.add_argument("--page", type=int, default=1, help="会话列表页码（默认1）")
    parser.add_argument("--page-size", type=int, default=10, help="每页会话数量（默认10）")
    parser.add_argument("--session", "-s", type=str, help="查看指定会话文件（支持部分UUID匹配）")

    # 新增参数
    parser.add_argument("--agents", "-A", action="store_true", help="列出所有OpenClaw agents")
    parser.add_argument("--claude", "-c", nargs="?", const="", default=None, dest="claude",
                        help="Claude项目模式。不指定项目时列出所有项目")
    parser.add_argument("--follow", "-w", action="store_true", help="Follow模式，持续监控新消息（每2秒刷新）")

    args = parser.parse_args()

    # ===== 新增功能处理 =====

    # --agents: 列出所有OpenClaw agents
    if args.agents:
        limit = args.num if args.num else 0
        agents = list_all_agents(limit)
        if not agents:
            print("❌ 未找到任何OpenClaw agent")
            return

        print("=" * 80)
        print(f"📋 OpenClaw Agent 列表{'（前' + str(limit) + '个）' if limit else ''}")
        print("=" * 80)

        for i, (agent_name, session_count, latest_mtime) in enumerate(agents, 1):
            mtime_str = datetime.fromtimestamp(latest_mtime).strftime('%Y-%m-%d %H:%M:%S')
            mtime_ago = format_time_ago(datetime.fromtimestamp(latest_mtime))
            print(f"\n[{i}] 👤 {agent_name}")
            print(f"    📊 会话数: {session_count}")
            print(f"    🕐 最新: {mtime_str} ({mtime_ago})")

        print(f"\n{'=' * 80}")
        print(f"✅ 共 {len(agents)} 个agent")
        print("💡 提示: 使用 '%(prog)s <agent名>' 查看详细消息")
        return

    # --claude: Claude项目模式
    if args.claude is not None:
        # 不指定项目名时，列出所有项目
        if not args.claude:
            projects = get_claude_projects()
            if not projects:
                print("❌ 未找到任何Claude项目")
                print(f"💡 检查目录: {CLAUDE_PROJECTS_BASE}")
                return

            print("=" * 80)
            print(f"📋 Claude 项目列表")
            print("=" * 80)

            for i, (dir_name, dir_path, display_name) in enumerate(projects, 1):
                # 获取项目会话文件数量
                sessions = get_claude_project_sessions(dir_path)
                session_count = len(sessions)

                # 获取最新会话的修改时间
                if sessions:
                    latest_mtime = datetime.fromtimestamp(sessions[0].stat().st_mtime)
                    mtime_str = latest_mtime.strftime('%Y-%m-%d %H:%M:%S')
                    mtime_ago = format_time_ago(latest_mtime)
                else:
                    mtime_str = "无会话"
                    mtime_ago = ""

                print(f"\n[{i}] 📁 {display_name}")
                print(f"    📂 目录: {dir_name}")
                print(f"    📊 会话数: {session_count}")
                if mtime_ago:
                    print(f"    🕐 最新: {mtime_str} ({mtime_ago})")

            print(f"\n{'=' * 80}")
            print(f"✅ 共 {len(projects)} 个项目")
            print("💡 提示: 使用 '%(prog)s -c <项目名>' 查看项目会话")
            return

        # 指定项目名时，查找项目
        project_dir = find_claude_project(args.claude)
        if not project_dir:
            return

        # --list: 列出项目会话文件
        if args.list:
            sessions = get_claude_project_sessions(project_dir)
            if not sessions:
                print(f"❌ 项目 '{args.claude}' 无会话文件")
                return

            total = len(sessions)
            total_pages = (total + args.page_size - 1) // args.page_size
            start_idx = (args.page - 1) * args.page_size
            end_idx = min(start_idx + args.page_size, total)
            page_sessions = sessions[start_idx:end_idx]

            print("=" * 80)
            print(f"📋 Claude项目会话列表")
            print(f"   项目: {project_dir.name}")
            print(f"   共 {total} 个会话，当前第 {args.page}/{total_pages} 页")
            print("=" * 80)

            for i, session_file in enumerate(page_sessions, start_idx + 1):
                mtime = datetime.fromtimestamp(session_file.stat().st_mtime)
                size = session_file.stat().st_size
                size_str = f"{size / 1024:.1f}KB" if size > 1024 else f"{size}B"

                print(f"\n[{i}] 📄 {session_file.name}")
                print(f"    🕐 修改: {mtime.strftime('%Y-%m-%d %H:%M:%S')} ({format_time_ago(mtime)})")
                print(f"    📊 大小: {size_str}")

            print(f"\n{'=' * 80}")
            if args.page < total_pages:
                print(f"📄 下一页: -c {args.claude} -l --page {args.page + 1}")
            print(f"💡 查看会话: -c {args.claude} -s <session-id>")
            return

        # 获取最新会话文件
        sessions = get_claude_project_sessions(project_dir)
        if not sessions:
            print(f"❌ 项目 '{args.claude}' 无会话文件")
            return

        latest_session = sessions[0]

        # --follow: Follow模式
        if args.follow:
            follow_session(latest_session, count=10)
            return

        # 显示会话内容
        # 处理count参数
        head = args.head
        tail = args.tail
        use_head_tail = head > 0 or tail > 0

        if args.num is not None:
            count = args.num
            from_line = args.from_line
        elif args.count is not None:
            if args.count < 0:
                from_line = args.count
                count = 0
            else:
                count = args.count
                from_line = args.from_line
        else:
            count = 10 if not use_head_tail else 0
            from_line = args.from_line

        show_session_file(str(latest_session), count, args.format, agent_name=None,
                         from_line=from_line, head=head, tail=tail)
        return

    # ===== 原有功能处理 =====

    # 处理 --head / --tail 参数（优先于 from_line 模式）
    head = args.head
    tail = args.tail
    use_head_tail = head > 0 or tail > 0

    # 处理count参数
    if args.num is not None:
        count = args.num
        from_line = args.from_line
    elif args.count is not None:
        if args.count < 0:
            # head/tail风格：-5 表示从倒数第5条开始
            from_line = args.count
            count = 0
        else:
            count = args.count
            from_line = args.from_line
    else:
        count = 10 if not use_head_tail else 0
        from_line = args.from_line

    # 查看指定会话文件
    if args.session:
        session_path = Path(args.session)
        if session_path.exists() or session_path.is_absolute():
            show_session_file(args.session, count, args.format, agent_name=None,
                             from_line=from_line, head=head, tail=tail)
        else:
            if not args.agent:
                print("❌ 部分匹配模式需要指定agent名称")
                print("用法: python3 scripts/check_agent_sessions.py <agent名> -s <uuid片段>")
                print("或使用完整路径: python3 scripts/check_agent_sessions.py -s /full/path/to/session.jsonl")
                return
            show_session_file(args.session, count, args.format, agent_name=args.agent,
                             from_line=from_line, head=head, tail=tail)
        return

    # 列出agent的所有会话
    if args.list:
        if not args.agent:
            print("❌ 请指定agent名称")
            print("用法: python3 scripts/check_agent_sessions.py <agent名> --list")
            return
        list_sessions(args.agent, page=args.page, page_size=args.page_size)
        return

    # 如果指定了agent，显示该agent的详细消息
    if args.agent:
        show_last_messages(args.agent, count, args.format, from_line=from_line, head=head, tail=tail)
        return

    # 否则显示所有agent的状态概览
    print("=" * 80)
    print(f"📊 Agent会话状态检查 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    for group_id, group_config in GROUPS.items():
        print(f"\n{'─' * 60}")
        print(f"📁 {group_config['name']} ({group_id})")
        print(f"{'─' * 60}")

        for agent_name in group_config["agents"]:
            print(f"\n  👤 {agent_name}")

            session_files = get_session_files(agent_name)

            if not session_files:
                print(f"     ❌ 无会话文件")
                continue

            # 只显示最新的会话文件
            latest_file = session_files[0]
            session_info = parse_session_file(latest_file)

            # 文件路径（相对路径）
            rel_path = latest_file.relative_to(AGENTS_BASE.parent) if AGENTS_BASE.parent in latest_file.parents else latest_file
            print(f"     📄 {rel_path}")

            # 活跃时间
            print(f"     🕐 活跃: {format_time_ago(session_info['mtime'])}")

            # 状态
            stop_reason = session_info.get("stop_reason")
            print(f"     📌 状态: {get_status_icon(stop_reason)}")

            # 最后一条消息
            if session_info.get("last_message"):
                last_msg = session_info["last_message"]
                role = last_msg.get("role", "?")
                content = last_msg.get("content", "(空)")
                error = last_msg.get("error_message")

                role_icon = "🤖" if role == "assistant" else "👤" if role == "user" else "❓"
                print(f"     {role_icon} 最后消息 [{role}]: {content}")

                if error:
                    print(f"     ⚠️ 错误: {error[:100]}")

            # 消息数量
            msg_count = len(session_info.get("messages", []))
            print(f"     📊 消息数: {msg_count}")

    print(f"\n{'=' * 80}")
    print("✅ 检查完成")
    print("💡 提示: 使用 '%(prog)s <agent名> [-n 数量] [--from N] [-f summary|raw|both]' 查看详细消息")


if __name__ == "__main__":
    main()
