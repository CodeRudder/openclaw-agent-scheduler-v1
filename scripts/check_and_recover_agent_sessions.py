#!/usr/bin/env python3
"""
检查并恢复异常停止的agent会话

检测条件（满足任一即触发重置，OR关系）：
  条件1：最近N条assistant消息全部无效（content text长度 < min_valid_length）
  条件2：最近N条assistant消息全部为stop（stopReason=stop，无toolUse）

恢复方式：重命名会话文件为backup（agent下次启动时会创建新会话）

用法：
  # 检查所有agent（dry-run，只显示不操作）
  python3 scripts/check_and_recover_agent_sessions.py --dry-run

  # 检查并自动重置符合条件的会话
  python3 scripts/check_and_recover_agent_sessions.py

  # 检查指定agent
  python3 scripts/check_and_recover_agent_sessions.py --agent fullstack-dev

  # 详细输出（显示每条消息状态及条件判断）
  python3 scripts/check_and_recover_agent_sessions.py -v
"""

import json
import sys
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone

# Agent会话目录
AGENTS_BASE = Path.home() / ".openclaw" / "agents"

# 有效消息最短字符数：文本内容低于此长度视为无效
DEFAULT_MIN_VALID_LENGTH = 10

# 检查最近N条消息（排除user消息）
DEFAULT_RECENT_MSG_COUNT = 5

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


def get_last_assistant_message_time(jsonl_file: Path) -> datetime:
    """获取最后一条assistant消息的时间戳（任意内容）

    Returns:
        最后一条assistant消息的时间（带时区），未找到返回None
    """
    last_time = None
    try:
        with open(jsonl_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    if msg.get("type") == "message":
                        message = msg.get("message", {})
                        if message.get("role") == "assistant":
                            ts = msg.get("timestamp")
                            if ts:
                                if isinstance(ts, (int, float)):
                                    last_time = datetime.fromtimestamp(
                                        ts / 1000, tz=timezone.utc)
                                elif isinstance(ts, str):
                                    last_time = datetime.fromisoformat(
                                        ts.replace('Z', '+00:00'))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.debug(f"读取时间戳失败 {jsonl_file}: {e}")
    return last_time


def get_last_valid_assistant_message_time(jsonl_file: Path,
                                          min_valid_length: int = DEFAULT_MIN_VALID_LENGTH) -> datetime:
    """获取最后一条有效assistant消息的时间戳（文本长度>=min_valid_length）

    扫描整个文件，避免只看最近N条消息导致遗漏历史有效消息。

    Returns:
        最后一条有效assistant消息的时间（带时区），未找到返回None
    """
    last_valid_time = None
    try:
        with open(jsonl_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    if msg.get("type") == "message":
                        message = msg.get("message", {})
                        if message.get("role") == "assistant":
                            content = message.get("content", "")
                            if _get_text_length(content) >= min_valid_length:
                                ts = msg.get("timestamp")
                                if ts:
                                    if isinstance(ts, (int, float)):
                                        last_valid_time = datetime.fromtimestamp(
                                            ts / 1000, tz=timezone.utc)
                                    elif isinstance(ts, str):
                                        last_valid_time = datetime.fromisoformat(
                                            ts.replace('Z', '+00:00'))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.debug(f"读取有效消息时间戳失败 {jsonl_file}: {e}")
    return last_valid_time


def is_content_empty(content) -> bool:
    """判断消息内容是否为空（无实质文本内容）

    空内容定义：
    - 空字符串 ""
    - 空列表 []
    - 列表中没有 type=text 的项（只有 thinking、tool_use 等非文本内容）
    """
    if isinstance(content, str):
        return content.strip() == ""
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                if text.strip():
                    return False
            elif isinstance(item, str) and item.strip():
                return False
        return True
    return True


def get_session_files(agent_name: str) -> list:
    """获取agent的所有会话文件（排除backup）"""
    session_dir = AGENTS_BASE / agent_name / "sessions"
    if not session_dir.exists():
        return []
    files = [f for f in session_dir.glob("*.jsonl") if "backup" not in f.name.lower()]
    return sorted(files, key=lambda x: x.stat().st_mtime, reverse=True)


def _get_text_length(content) -> int:
    """获取content中text内容的总字符数"""
    if isinstance(content, str):
        return len(content.strip())
    if isinstance(content, list):
        total = 0
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                total += len(item.get("text", "").strip())
            elif isinstance(item, str):
                total += len(item.strip())
        return total
    return 0


def get_last_non_user_messages(jsonl_file: Path, count: int = 5) -> list:
    """获取最后N条非user消息（包括assistant和tool消息）

    用于检测连续stop异常：正常会话中非user消息应包含toolUse类型的assistant消息，
    如果最后N条非user消息全部是assistant且stopReason=stop，说明agent持续停止未执行工具。

    Returns:
        [{"role": str, "stop_reason": str}, ...]
        按时间顺序（最旧→最新）
    """
    try:
        non_user_msgs = []
        with open(jsonl_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    if msg.get("type") == "message":
                        message = msg.get("message", {})
                        role = message.get("role", "")
                        if role != "user":
                            stop_reason = message.get("stopReason") or message.get("stop_reason")
                            non_user_msgs.append({
                                "role": role,
                                "stop_reason": stop_reason,
                            })
                except json.JSONDecodeError:
                    continue
        return non_user_msgs[-count:] if non_user_msgs else []
    except Exception as e:
        logger.debug(f"读取会话文件失败 {jsonl_file}: {e}")
        return []


def get_last_assistant_messages(jsonl_file: Path, count: int = 5) -> list:
    """获取最后N条assistant消息

    Returns:
        [{"content_empty": bool, "content_length": int, "stop_reason": str,
          "content_preview": str, "content_type": list, "timestamp": datetime or None}, ...]
        按时间顺序（最旧→最新）
    """
    try:
        assistant_msgs = []
        with open(jsonl_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    if msg.get("type") == "message":
                        message = msg.get("message", {})
                        if message.get("role") == "assistant":
                            content = message.get("content", "")
                            empty = is_content_empty(content)
                            content_length = _get_text_length(content)

                            # 生成内容预览
                            preview = ""
                            if not empty:
                                if isinstance(content, str):
                                    preview = content[:80]
                                elif isinstance(content, list):
                                    for item in content:
                                        if isinstance(item, dict) and item.get("type") == "text":
                                            preview = item.get("text", "")[:80]
                                            break

                            # 兼容两种命名：stopReason (OpenClaw) 和 stop_reason (Claude)
                            stop_reason = message.get("stopReason") or message.get("stop_reason")

                            # 解析时间戳
                            ts = msg.get("timestamp")
                            timestamp = None
                            if ts:
                                try:
                                    if isinstance(ts, (int, float)):
                                        timestamp = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                                    elif isinstance(ts, str):
                                        timestamp = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                                except Exception:
                                    pass

                            assistant_msgs.append({
                                "content_empty": empty,
                                "content_length": content_length,
                                "stop_reason": stop_reason,
                                "content_preview": preview.replace('\n', ' '),
                                "content_type": _get_content_types(content),
                                "timestamp": timestamp
                            })
                except json.JSONDecodeError:
                    continue
        return assistant_msgs[-count:] if assistant_msgs else []
    except Exception as e:
        logger.debug(f"读取会话文件失败 {jsonl_file}: {e}")
        return []


def _get_content_types(content) -> list:
    """获取content中的type列表（用于调试）"""
    if isinstance(content, list):
        return [item.get("type", "?") for item in content if isinstance(item, dict)]
    return ["str"] if isinstance(content, str) else []


def check_agent_session(agent_name: str,
                        min_valid_length: int = DEFAULT_MIN_VALID_LENGTH,
                        recent_msg_count: int = DEFAULT_RECENT_MSG_COUNT,
                        verbose: bool = False) -> dict:
    """检查单个agent的最新会话

    触发重置条件（满足任一即触发，OR关系）：
      条件1：最近 recent_msg_count 条assistant消息全部无效（content text长度 < min_valid_length）
      条件2：最近 recent_msg_count 条assistant消息全部为stop（stopReason=stop，无toolUse）

    Returns:
        {
            "agent": agent_name,
            "session_file": str or None,
            "should_reset": bool,
            "reason": str,
            "last_messages": [...],
            "short_msg_count": int
        }
    """
    result = {
        "agent": agent_name,
        "session_file": None,
        "should_reset": False,
        "reason": "",
        "last_messages": [],
        "short_msg_count": 0
    }

    session_files = get_session_files(agent_name)
    if not session_files:
        result["reason"] = "无会话文件"
        return result

    latest_file = session_files[0]
    result["session_file"] = str(latest_file)

    # 获取最近 recent_msg_count 条assistant消息
    last_msgs = get_last_assistant_messages(latest_file, count=recent_msg_count)
    result["last_messages"] = last_msgs

    if not last_msgs:
        result["reason"] = "无assistant消息"
        return result

    # 消息数不足N条时不判断（避免新会话误触发）
    if len(last_msgs) < recent_msg_count:
        result["reason"] = f"消息数({len(last_msgs)})不足{recent_msg_count}条，跳过"
        return result

    # ===== 条件1：最近N条消息全部无效（content text长度 < min_valid_length）=====
    short_count = sum(1 for msg in last_msgs if msg["content_length"] < min_valid_length)
    result["short_msg_count"] = short_count
    all_invalid = short_count == len(last_msgs)

    # ===== 条件2：最近N条非user消息全部是assistant且stopReason=stop（无toolUse）=====
    # 正常会话中非user消息应包含toolUse类型的assistant消息，
    # 如果全部是assistant+stop，说明agent持续停止未执行工具
    non_user_msgs = get_last_non_user_messages(latest_file, count=recent_msg_count)
    all_assistant_stop = (
        len(non_user_msgs) >= recent_msg_count
        and all(m["role"] == "assistant" and m["stop_reason"] == "stop" for m in non_user_msgs)
    )

    if verbose:
        logger.info(f"  最近{len(last_msgs)}条assistant消息：")
        for i, msg in enumerate(last_msgs, 1):
            types_str = ",".join(msg["content_type"]) if msg["content_type"] else "-"
            preview = f'"{msg["content_preview"][:40]}"' if msg["content_preview"] else ""
            logger.info(f"    [{i}] {msg['content_length']}字 | stopReason={msg['stop_reason']} | types=[{types_str}] {preview}")
        logger.info(f"  条件1（全部无效<{min_valid_length}字）: {short_count}/{len(last_msgs)} → {'✓' if all_invalid else '✗'}")
        non_user_stop_count = sum(1 for m in non_user_msgs if m["role"] == "assistant" and m["stop_reason"] == "stop")
        logger.info(f"  条件2（最近{len(non_user_msgs)}条非user消息全部assistant+stop）: {non_user_stop_count}/{len(non_user_msgs)} → {'✓' if all_assistant_stop else '✗'}")
        logger.info(f"  判断结果: {'✓ 触发重置' if (all_invalid or all_assistant_stop) else '✗ 正常'}")

    if all_invalid:
        result["should_reset"] = True
        result["reason"] = (
            f"最近{len(last_msgs)}条assistant消息全部无效（text长度<{min_valid_length}字），疑似异常停止"
        )
    elif all_assistant_stop:
        result["should_reset"] = True
        result["reason"] = (
            f"最近{len(non_user_msgs)}条非user消息全部为assistant+stop（无toolUse），会话持续停止"
        )
    else:
        valid_count = len(last_msgs) - short_count
        tool_count = sum(1 for m in last_msgs if m["stop_reason"] == "toolUse")
        result["reason"] = f"最近{len(last_msgs)}条中{valid_count}条有效内容或{tool_count}条toolUse，会话正常"

    return result


def reset_session(session_file: str, dry_run: bool = False) -> bool:
    """重置会话文件（重命名为backup）"""
    session_path = Path(session_file)
    if not session_path.exists():
        logger.warning(f"  ⚠️ 会话文件不存在: {session_file}")
        return False

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = session_path.parent / f"{session_path.stem}_backup_{timestamp}.jsonl"

    if dry_run:
        logger.info(f"  [dry-run] 将重命名: {session_path.name} → {backup_path.name}")
        return True

    try:
        session_path.rename(backup_path)
        logger.info(f"  ✅ 已重置: {session_path.name} → {backup_path.name}")
        return True
    except Exception as e:
        logger.error(f"  ❌ 重置失败: {e}")
        return False


def list_all_agents() -> list:
    """列出所有OpenClaw agents"""
    if not AGENTS_BASE.exists():
        return []
    agents = []
    for agent_dir in AGENTS_BASE.iterdir():
        if agent_dir.is_dir() and not agent_dir.name.startswith('.'):
            sessions_dir = agent_dir / "sessions"
            if sessions_dir.exists():
                agents.append(agent_dir.name)
    return sorted(agents)


def run_check(agents: list = None,
              min_valid_length: int = DEFAULT_MIN_VALID_LENGTH,
              recent_msg_count: int = DEFAULT_RECENT_MSG_COUNT,
              dry_run: bool = False, verbose: bool = False) -> dict:
    """执行检查和恢复

    Args:
        agents: 指定检查的agent列表，None=全部
        min_valid_length: 有效消息最短字符数
        recent_msg_count: 检查最近N条消息
        dry_run: 只检查不实际重置
        verbose: 详细输出

    Returns:
        {"checked": int, "reset": int, "skipped": int, "results": [...]}
    """
    if agents is None:
        agents = list_all_agents()

    if not agents:
        logger.info("❌ 未找到任何OpenClaw agent")
        return {"checked": 0, "reset": 0, "skipped": 0, "results": []}

    summary = {"checked": 0, "reset": 0, "skipped": 0, "results": []}

    for agent_name in agents:
        summary["checked"] += 1
        if verbose:
            logger.info(f"\n🔍 检查 {agent_name}...")
        else:
            logger.debug(f"检查 {agent_name}...")

        result = check_agent_session(
            agent_name,
            min_valid_length=min_valid_length,
            recent_msg_count=recent_msg_count,
            verbose=verbose
        )
        summary["results"].append(result)

        if result["should_reset"]:
            logger.info(f"⚠️  {agent_name}: {result['reason']}")
            if result["session_file"]:
                if reset_session(result["session_file"], dry_run=dry_run):
                    summary["reset"] += 1
                else:
                    summary["skipped"] += 1
        else:
            if verbose:
                logger.info(f"  ✓ {agent_name}: {result['reason']}")
            summary["skipped"] += 1

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="检查并恢复异常停止的agent会话",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 检查所有agent（dry-run，只显示不操作）
  %(prog)s --dry-run

  # 检查并自动重置符合条件的会话
  %(prog)s

  # 检查指定agent
  %(prog)s --agent fullstack-dev

  # 检查多个agent
  %(prog)s --agent fullstack-dev --agent qa

  # 详细输出（显示每条消息状态及条件判断）
  %(prog)s --dry-run -v
        """
    )
    parser.add_argument(
        "--agent", "-a", action="append", dest="agents",
        metavar="AGENT_NAME",
        help="指定检查的agent（可多次使用）。默认检查所有agent"
    )
    parser.add_argument(
        "--min-valid-length", type=int, default=DEFAULT_MIN_VALID_LENGTH,
        dest="min_valid_length",
        help=f"有效消息最短字符数（默认{DEFAULT_MIN_VALID_LENGTH}）"
    )
    parser.add_argument(
        "--recent-count", type=int, default=DEFAULT_RECENT_MSG_COUNT,
        dest="recent_msg_count",
        help=f"检查最近N条消息（默认{DEFAULT_RECENT_MSG_COUNT}）"
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true",
        help="只检查不实际重置（预览模式）"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="详细输出每条消息状态及条件判断"
    )
    parser.add_argument(
        "--list", "-l", action="store_true",
        help="列出所有agent"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # 列出所有agent
    if args.list:
        agents = list_all_agents()
        if not agents:
            print("❌ 未找到任何OpenClaw agent")
            print(f"   检查目录: {AGENTS_BASE}")
            return
        print(f"📋 OpenClaw Agent 列表（共{len(agents)}个）:")
        for a in agents:
            files = get_session_files(a)
            print(f"  - {a}  ({len(files)}个会话)")
        return

    # 执行检查
    dry_label = " [dry-run]" if args.dry_run else ""
    print(f"{'=' * 60}")
    print(f"🔍 Agent会话健康检查{dry_label}")
    print(f"   触发条件（满足任一即重置，OR关系）：")
    print(f"     条件1：最近{args.recent_msg_count}条消息全部无效（text长度<{args.min_valid_length}字）")
    print(f"     条件2：最近{args.recent_msg_count}条消息全部为stop（无toolUse）")
    print(f"   目录: {AGENTS_BASE}")
    print(f"{'=' * 60}")

    summary = run_check(
        agents=args.agents,
        min_valid_length=args.min_valid_length,
        recent_msg_count=args.recent_msg_count,
        dry_run=args.dry_run,
        verbose=args.verbose
    )

    print(f"\n{'=' * 60}")
    print(f"📊 检查结果: 共{summary['checked']}个agent")
    if summary["reset"] > 0:
        action = "将重置" if args.dry_run else "已重置"
        print(f"   🔄 {action}: {summary['reset']}个")
    else:
        print(f"   ✅ 无需重置")
    if args.dry_run and summary["reset"] > 0:
        print(f"\n💡 去掉 --dry-run 参数可执行实际重置")


if __name__ == "__main__":
    main()
