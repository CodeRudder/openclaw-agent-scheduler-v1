#!/usr/bin/env python3
"""
检查所有工作群agent的会话状态

用法：
  python3 scripts/check_agent_sessions.py                    # 查看所有agent状态
  python3 scripts/check_agent_sessions.py fullstack-dev      # 查看指定agent状态和最后10条消息
  python3 scripts/check_agent_sessions.py fullstack-dev 20   # 查看指定agent最后20条消息
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


def get_session_files(agent_name: str) -> list:
    """获取agent的所有会话文件"""
    session_dir = AGENTS_BASE / agent_name / "sessions"
    if not session_dir.exists():
        return []

    files = [f for f in session_dir.glob("*.jsonl") if "backup" not in f.name]
    return sorted(files, key=lambda x: x.stat().st_mtime, reverse=True)


def parse_session_file(file_path: Path) -> dict:
    """解析会话文件，提取关键信息"""
    result = {
        "file": str(file_path),
        "mtime": datetime.fromtimestamp(file_path.stat().st_mtime),
        "messages": [],
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
                        result["messages"].append(msg.get("message", {}))
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


def show_last_messages(agent_name: str, count: int = 10):
    """显示指定agent的最后n条消息"""
    session_files = get_session_files(agent_name)

    if not session_files:
        print(f"❌ Agent '{agent_name}' 无会话文件")
        return

    latest_file = session_files[0]
    session_info = parse_session_file(latest_file)

    print("=" * 80)
    print(f"📋 {agent_name} 最后 {count} 条消息")
    print(f"📄 会话文件: {latest_file}")
    print(f"🕐 活跃: {format_time_ago(session_info['mtime'])}")
    print(f"📌 状态: {get_status_icon(session_info.get('stop_reason'))}")
    print("=" * 80)

    messages = session_info.get("messages", [])
    if not messages:
        print("（无消息）")
        return

    # 取最后n条
    last_msgs = messages[-count:] if len(messages) > count else messages

    for i, msg in enumerate(last_msgs, 1):
        role = msg.get("role", "?")
        content = extract_content_full(msg.get("content", ""))
        stop_reason = msg.get("stopReason")

        role_icon = "🤖" if role == "assistant" else "👤" if role == "user" else "❓"
        print(f"\n[{i}] {role_icon} {role}")
        if stop_reason:
            print(f"    stopReason: {stop_reason}")
        print(f"    {content}")

    print(f"\n{'=' * 80}")
    print(f"✅ 共显示 {len(last_msgs)} 条消息（总共 {len(messages)} 条）")


def extract_content_full(content, max_len: int = 500) -> str:
    """提取消息内容（完整版，用于显示最后消息）"""
    if isinstance(content, str):
        return content[:max_len] + "..." if len(content) > max_len else content
    elif isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(item.get("text", ""))
            elif isinstance(item, dict) and item.get("type") == "toolUse":
                text_parts.append(f"[工具调用: {item.get('name', '?')}]")
            elif isinstance(item, dict) and item.get("type") == "toolResult":
                text_parts.append(f"[工具结果: {item.get('toolUseId', '?')}]")
            elif isinstance(item, str):
                text_parts.append(item)
        full_text = "\n".join(text_parts)
        return full_text[:max_len] + "..." if len(full_text) > max_len else full_text
    return str(content)[:max_len]


def main():
    parser = argparse.ArgumentParser(description="检查agent会话状态")
    parser.add_argument("agent", nargs="?", help="指定agent名称")
    parser.add_argument("count", nargs="?", type=int, default=10, help="显示最后N条消息（默认10）")
    args = parser.parse_args()

    # 如果指定了agent，显示该agent的详细消息
    if args.agent:
        show_last_messages(args.agent, args.count)
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
    print("💡 提示: 使用 'python3 scripts/check_agent_sessions.py <agent名> [数量]' 查看详细消息")


if __name__ == "__main__":
    main()
