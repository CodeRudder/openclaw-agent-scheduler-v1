#!/usr/bin/env python3
"""
检查并恢复异常停止的agent会话

检测条件（三个条件同时满足才触发重置，AND关系）：
  A. 超过30分钟无有效assistant消息（有效=文本内容>=20字符）
     且在2小时活跃窗口内（避免误重置长期空闲的agent）
  B. 最近5条消息中，内容过短的消息 >= 3条
  C. 最近5条消息中，stopReason=stop 次数 >= 2次

恢复方式：重命名会话文件为backup（agent下次启动时会创建新会话）

用法：
  # 检查所有agent（dry-run，只显示不操作）
  python3 scripts/check_and_recover_agent_sessions.py --dry-run

  # 检查并自动重置符合条件的会话
  python3 scripts/check_and_recover_agent_sessions.py

  # 检查指定agent
  python3 scripts/check_and_recover_agent_sessions.py --agent fullstack-dev

  # 调整超时阈值（默认30分钟）
  python3 scripts/check_and_recover_agent_sessions.py --timeout 30

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

# 默认无响应超时阈值（分钟），0=禁用
# 判断条件：最近N分钟内无有效assistant消息（有实质内容且足够长）
DEFAULT_TIMEOUT_MINUTES = 30

# 活跃窗口：只对最近N小时内有过活动的会话进行超时重置（避免误重置长期空闲的agent）
DEFAULT_ACTIVE_WINDOW_HOURS = 2

# 有效消息最短字符数：文本内容低于此长度视为无效（如"收到"、"NO_REPLY"等）
DEFAULT_MIN_VALID_LENGTH = 20

# 频繁stop阈值：最近N条消息中stop次数达到此值认为"频繁stop"
DEFAULT_FREQUENT_STOP_COUNT = 2

# 检查最近N条assistant消息（用于频繁stop和内容过短判断）
DEFAULT_RECENT_MSG_COUNT = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
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
                        timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
                        active_window_hours: int = DEFAULT_ACTIVE_WINDOW_HOURS,
                        min_valid_length: int = DEFAULT_MIN_VALID_LENGTH,
                        frequent_stop_count: int = DEFAULT_FREQUENT_STOP_COUNT,
                        recent_msg_count: int = DEFAULT_RECENT_MSG_COUNT,
                        verbose: bool = False) -> dict:
    """检查单个agent的最新会话

    触发重置条件（三个条件同时满足，AND关系）：
      A. 超过 timeout_minutes 分钟无有效assistant消息（有效=文本长度>=min_valid_length）
         且在 active_window_hours 活跃窗口内（避免误重置长期空闲的agent）
      B. 最近 recent_msg_count 条消息中，有效内容过短的消息 >= 3 条
      C. 最近 recent_msg_count 条消息中，stopReason=stop 的次数 >= frequent_stop_count

    Returns:
        {
            "agent": agent_name,
            "session_file": str or None,
            "should_reset": bool,
            "reason": str,
            "last_messages": [...],
            "minutes_since_last_valid_msg": float or None,
            "short_msg_count": int,
            "stop_count": int
        }
    """
    result = {
        "agent": agent_name,
        "session_file": None,
        "should_reset": False,
        "reason": "",
        "last_messages": [],
        "minutes_since_last_valid_msg": None,
        "short_msg_count": 0,
        "stop_count": 0
    }

    session_files = get_session_files(agent_name)
    if not session_files:
        result["reason"] = "无会话文件"
        return result

    latest_file = session_files[0]
    result["session_file"] = str(latest_file)

    # 获取最近 recent_msg_count 条assistant消息（含content_length和timestamp）
    last_msgs = get_last_assistant_messages(latest_file, count=recent_msg_count)
    result["last_messages"] = last_msgs

    if not last_msgs:
        result["reason"] = "无assistant消息"
        return result

    # ===== 条件A：超过 timeout_minutes 分钟无有效消息（有效=长度>=min_valid_length） =====
    # 扫描整个文件找最后一条有效消息（避免只看最近N条导致遗漏历史有效消息）
    last_valid_ts = get_last_valid_assistant_message_time(latest_file, min_valid_length)

    now = datetime.now(tz=timezone.utc)
    if last_valid_ts:
        minutes_since_valid = (now - last_valid_ts).total_seconds() / 60
    else:
        # 无任何有效消息，用文件修改时间
        mtime = latest_file.stat().st_mtime
        minutes_since_valid = (datetime.now() - datetime.fromtimestamp(mtime)).total_seconds() / 60

    result["minutes_since_last_valid_msg"] = round(minutes_since_valid, 1)

    # 计算活跃窗口：以最后任意assistant消息时间为基准（用最近N条中最新的）
    last_any_ts = None
    for msg in reversed(last_msgs):
        if msg["timestamp"]:
            last_any_ts = msg["timestamp"]
            break
    if last_any_ts is None:
        last_any_ts = get_last_assistant_message_time(latest_file)

    if last_any_ts:
        minutes_since_any = (now - last_any_ts).total_seconds() / 60
    else:
        minutes_since_any = minutes_since_valid

    is_in_active_window = minutes_since_any <= active_window_hours * 60
    cond_a = (minutes_since_valid >= timeout_minutes) and is_in_active_window

    # ===== 条件B：最近N条消息中，内容过短（<min_valid_length）的消息 >= 3条 =====
    SHORT_MSG_THRESHOLD = 3
    short_count = sum(1 for msg in last_msgs if msg["content_length"] < min_valid_length)
    result["short_msg_count"] = short_count
    cond_b = short_count >= SHORT_MSG_THRESHOLD

    # ===== 条件C：最近N条消息中，stopReason=stop 次数 >= frequent_stop_count =====
    stop_count = sum(1 for msg in last_msgs if msg["stop_reason"] == "stop")
    result["stop_count"] = stop_count
    cond_c = stop_count >= frequent_stop_count

    if verbose:
        logger.info(f"  最近{len(last_msgs)}条assistant消息：")
        for i, msg in enumerate(last_msgs, 1):
            length_mark = f"{msg['content_length']}字"
            types_str = ",".join(msg["content_type"]) if msg["content_type"] else "-"
            preview = f'"{msg["content_preview"][:40]}"' if msg["content_preview"] else ""
            logger.info(f"    [{i}] {length_mark} | stopReason={msg['stop_reason']} | types=[{types_str}] {preview}")
        logger.info(f"  条件A（超时{timeout_minutes}分钟无有效消息）: {int(minutes_since_valid)}分钟 → {'✓' if cond_a else '✗'}")
        logger.info(f"  条件B（最近{len(last_msgs)}条中{short_count}条过短，阈值{SHORT_MSG_THRESHOLD}）: {'✓' if cond_b else '✗'}")
        logger.info(f"  条件C（最近{len(last_msgs)}条中stop={stop_count}次，阈值{frequent_stop_count}）: {'✓' if cond_c else '✗'}")

    # ===== 三个条件同时满足才触发重置 =====
    if cond_a and cond_b and cond_c:
        result["should_reset"] = True
        result["reason"] = (
            f"三条件同时满足：A={int(minutes_since_valid)}分钟无有效消息，"
            f"B={short_count}/{len(last_msgs)}条内容过短，"
            f"C={stop_count}/{len(last_msgs)}次stop，疑似无响应停止"
        )
    else:
        unmet = []
        if not cond_a:
            unmet.append(f"A（{int(minutes_since_valid)}分钟<阈值{timeout_minutes}分钟或不在活跃窗口）")
        if not cond_b:
            unmet.append(f"B（{short_count}条过短<阈值{SHORT_MSG_THRESHOLD}条）")
        if not cond_c:
            unmet.append(f"C（stop={stop_count}次<阈值{frequent_stop_count}次）")
        result["reason"] = f"未满足条件：{', '.join(unmet)}"

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
              timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
              active_window_hours: int = DEFAULT_ACTIVE_WINDOW_HOURS,
              min_valid_length: int = DEFAULT_MIN_VALID_LENGTH,
              frequent_stop_count: int = DEFAULT_FREQUENT_STOP_COUNT,
              recent_msg_count: int = DEFAULT_RECENT_MSG_COUNT,
              dry_run: bool = False, verbose: bool = False) -> dict:
    """执行检查和恢复

    Args:
        agents: 指定检查的agent列表，None=全部
        timeout_minutes: 无有效消息超时阈值（分钟）
        active_window_hours: 活跃窗口（小时），只检测此窗口内有活动的agent
        min_valid_length: 有效消息最短字符数
        frequent_stop_count: 频繁stop阈值
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
            timeout_minutes=timeout_minutes,
            active_window_hours=active_window_hours,
            min_valid_length=min_valid_length,
            frequent_stop_count=frequent_stop_count,
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
        "--timeout", type=int, default=DEFAULT_TIMEOUT_MINUTES,
        help=f"无有效消息超时阈值（分钟，默认{DEFAULT_TIMEOUT_MINUTES}）。"
             f"超过此时间无有效assistant消息���为条件A满足"
    )
    parser.add_argument(
        "--active-window", type=int, default=DEFAULT_ACTIVE_WINDOW_HOURS,
        dest="active_window",
        help=f"活跃窗口（小时，默认{DEFAULT_ACTIVE_WINDOW_HOURS}）。"
             f"只对最近N小时内有过活动的会话进行检测，避免误重置长期空闲的agent"
    )
    parser.add_argument(
        "--min-valid-length", type=int, default=DEFAULT_MIN_VALID_LENGTH,
        dest="min_valid_length",
        help=f"有效消息最短字符数（默认{DEFAULT_MIN_VALID_LENGTH}）。"
             f"文本内容低于此长度视为过短（如'收到'、'NO_REPLY'等）"
    )
    parser.add_argument(
        "--frequent-stop", type=int, default=DEFAULT_FREQUENT_STOP_COUNT,
        dest="frequent_stop_count",
        help=f"频繁stop阈值（默认{DEFAULT_FREQUENT_STOP_COUNT}）。"
             f"最近N条消息中stop次数达到此值视为条件C满足"
    )
    parser.add_argument(
        "--recent-count", type=int, default=DEFAULT_RECENT_MSG_COUNT,
        dest="recent_msg_count",
        help=f"检查最近N条消息（默认{DEFAULT_RECENT_MSG_COUNT}）。用于条件B和C的判断"
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
    print(f"   触发条件（AND）：")
    print(f"     A. 超过{args.timeout}分钟无有效assistant消息（有效={args.min_valid_length}+字符）")
    print(f"     B. 最近{args.recent_msg_count}条消息中≥3条内容过短")
    print(f"     C. 最近{args.recent_msg_count}条消息中stop≥{args.frequent_stop_count}次")
    print(f"   活跃窗口: {args.active_window}小时")
    print(f"   目录: {AGENTS_BASE}")
    print(f"{'=' * 60}")

    summary = run_check(
        agents=args.agents,
        timeout_minutes=args.timeout,
        active_window_hours=args.active_window,
        min_valid_length=args.min_valid_length,
        frequent_stop_count=args.frequent_stop_count,
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
