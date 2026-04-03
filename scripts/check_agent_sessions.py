#!/usr/bin/env python3
"""
检查所有工作群agent的会话状态

用法：
  # 查看所有agent状态概览
  python3 scripts/check_agent_sessions.py

  # 查看指定agent的最新会话消息
  python3 scripts/check_agent_sessions.py fullstack-dev           # 最后10条消息（摘要格式）
  python3 scripts/check_agent_sessions.py fullstack-dev 20        # 最后20条消息
  python3 scripts/check_agent_sessions.py fullstack-dev 10 -f raw # 原始JSON格式

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
"""

import json
import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone

# Agent会话目录
AGENTS_BASE = Path("/home/gongdewei/.openclaw/agents")

# 项目根目录
PROJECT_DIR = Path(__file__).parent.parent

# Agent会话目录
AGENTS_BASE = Path("/home/gongdewei/.openclaw/agents")

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
    """解析会话文件，提取关键信息"""
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
                    if msg.get("type") == "message":
                        message = msg.get("message", {})
                        result["messages"].append(message)
                        # 保存原始消息（包含时间戳等元信息）
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


def show_session_file(session_path: str, count: int = 10, msg_format: str = "summary", agent_name: str = None):
    """显示指定会话文件的内容

    Args:
        session_path: 会话文件路径或部分匹配字符串
        count: 显示消息数量
        msg_format: 消息格式
        agent_name: 如果提供，则使用部分匹配查找
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

    print("=" * 80)
    print(f"📄 会话文件: {file_path}")
    print(f"🕐 修改: {session_info['mtime'].strftime('%Y-%m-%d %H:%M:%S')} ({format_time_ago(session_info['mtime'])})")
    print(f"📌 状态: {get_status_icon(session_info.get('stop_reason'))}")
    print(f"📝 格式: {msg_format}")
    print("=" * 80)

    raw_messages = session_info.get("raw_messages", [])
    if not raw_messages:
        print("（无消息）")
        return

    total_msgs = len(raw_messages)
    # 取最后n条
    last_msgs = raw_messages[-count:] if len(raw_messages) > count else raw_messages

    for i, raw_msg in enumerate(last_msgs, 1):
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

        print(f"\n[{i}] {role_icon} {role} {type_str} | ⏰ {time_str or '无时间'}")

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
    print(f"✅ 共显示 {len(last_msgs)} 条消息（总共 {total_msgs} 条）")


def show_last_messages(agent_name: str, count: int = 10, msg_format: str = "summary"):
    """显示指定agent的最后n条消息

    Args:
        agent_name: agent名称
        count: 显示消息数量
        msg_format: 消息格式 (summary/raw/both)
    """
    session_files = get_session_files(agent_name)

    if not session_files:
        print(f"❌ Agent '{agent_name}' 无会话文件")
        return

    latest_file = session_files[0]
    session_info = parse_session_file(latest_file)

    print("=" * 80)
    print(f"📋 {agent_name} 最后 {count} 条消息")
    print(f"📄 会话文件: {latest_file}")  # 显示绝对路径
    print(f"🕐 活跃: {format_time_ago(session_info['mtime'])}")
    print(f"📌 状态: {get_status_icon(session_info.get('stop_reason'))}")
    print(f"📝 格式: {msg_format}")
    print("=" * 80)

    raw_messages = session_info.get("raw_messages", [])
    if not raw_messages:
        print("（无消息）")
        return

    # 取最后n条
    last_msgs = raw_messages[-count:] if len(raw_messages) > count else raw_messages

    for i, raw_msg in enumerate(last_msgs, 1):
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

        print(f"\n[{i}] {role_icon} {role} {type_str} | ⏰ {time_str or '无时间'}")

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
    print(f"✅ 共显示 {len(last_msgs)} 条消息（总共 {len(raw_messages)} 条）")


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
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                text = clean_text(text)
                if text.strip():
                    text_parts.append(text.strip())
            elif isinstance(item, dict) and item.get("type") == "toolUse":
                text_parts.append(f"[工具调用: {item.get('name', '?')}]")
            elif isinstance(item, dict) and item.get("type") == "toolResult":
                text_parts.append(f"[工具结果: {item.get('toolUseId', '?')}]")
            elif isinstance(item, str):
                text = clean_text(item)
                if text.strip():
                    text_parts.append(text.strip())
        full_text = "\n".join(text_parts)
        return full_text[:max_len] + "..." if len(full_text) > max_len else full_text
    return str(content)[:max_len]


def main():
    parser = argparse.ArgumentParser(
        description="检查agent会话状态",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s                                    # 查看所有agent状态概览
  %(prog)s fullstack-dev                      # 查看agent最后10条消息
  %(prog)s fullstack-dev 20 -f raw            # 查看最后20条消息（原始JSON）
  %(prog)s fullstack-dev --list               # 列出agent会话（默认第1页，10条）
  %(prog)s fullstack-dev -l --page 2          # 列出第2页会话
  %(prog)s fullstack-dev -l --page-size 20    # 每页20条
  %(prog)s fullstack-dev -s 5417ea64          # 通过UUID片段查看会话
  %(prog)s -s /full/path/to/file.jsonl        # 完整路径查看会话
        """
    )
    parser.add_argument("agent", nargs="?", help="指定agent名称")
    parser.add_argument("count", nargs="?", type=int, default=10, help="显示最后N条消息（默认10）")
    parser.add_argument("--format", "-f", choices=["summary", "raw", "both"], default="summary",
                        help="消息显示格式: summary(摘要), raw(原始JSON), both(两者)")
    parser.add_argument("--list", "-l", action="store_true", help="列出agent的所有会话文件")
    parser.add_argument("--page", type=int, default=1, help="会话列表页码（默认1）")
    parser.add_argument("--page-size", type=int, default=10, help="每页会话数量（默认10）")
    parser.add_argument("--session", "-s", type=str, help="查看指定会话文件（支持部分UUID匹配，需配合agent参数）")
    args = parser.parse_args()

    # 查看指定会话文件
    if args.session:
        # 判断是否是完整路径
        session_path = Path(args.session)
        if session_path.exists() or session_path.is_absolute():
            # 完整路径模式
            show_session_file(args.session, args.count, args.format, agent_name=None)
        else:
            # 部分匹配模式，需要agent参数
            if not args.agent:
                print("❌ 部分匹配模式需要指定agent名称")
                print("用法: python3 scripts/check_agent_sessions.py <agent名> -s <uuid片段>")
                print("或使用完整路径: python3 scripts/check_agent_sessions.py -s /full/path/to/session.jsonl")
                return
            show_session_file(args.session, args.count, args.format, agent_name=args.agent)
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
        show_last_messages(args.agent, args.count, args.format)
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
    print("💡 提示: 使用 'python3 scripts/check_agent_sessions.py <agent名> [数量] [-f summary|raw|both]' 查看详细消息")


if __name__ == "__main__":
    main()
