#!/usr/bin/env python3
"""
检查并恢复异常停止的agent会话

检测条件（满足任一即触发重置）：
  1. 连续N条 assistant 消息内容为空 且 stopReason=stop（自动停止）
  2. 连续 T 分钟内没有新的 assistant 消息（无响应超时）

恢复方式：重命名会话文件为backup（agent下次启动时会创建新会话）

用法：
  # 检查所有agent（dry-run，只显示不操作）
  python3 scripts/check_and_recover_agent_sessions.py --dry-run

  # 检查并自动重置符合条件的会话
  python3 scripts/check_and_recover_agent_sessions.py

  # 检查指定agent
  python3 scripts/check_and_recover_agent_sessions.py --agent fullstack-dev

  # 调整空消息检测阈值（默认3次）
  python3 scripts/check_and_recover_agent_sessions.py --threshold 3

  # 调整无响应超时阈值（默认30分钟，0=禁用）
  python3 scripts/check_and_recover_agent_sessions.py --timeout 30

  # 详细输出
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

# 默认检测阈值：连续N条空内容assistant消息触发重置
DEFAULT_EMPTY_THRESHOLD = 3

# 默认无响应超时阈值（分钟），0=禁用
DEFAULT_TIMEOUT_MINUTES = 30

# 活跃窗口：只对最近N小时内有过活动的会话进行超时重置（避免误重置长期空闲的agent）
DEFAULT_ACTIVE_WINDOW_HOURS = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s"
)
logger = logging.getLogger(__name__)


def get_last_assistant_message_time(jsonl_file: Path) -> datetime:
    """获取最后一条assistant消息的时间戳

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


def get_last_assistant_messages(jsonl_file: Path, count: int = 5) -> list:
    """获取最后N条assistant消息

    Returns:
        [{"content_empty": bool, "stop_reason": str, "content_preview": str}, ...]
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

                            assistant_msgs.append({
                                "content_empty": empty,
                                "stop_reason": stop_reason,
                                "content_preview": preview.replace('\n', ' '),
                                "content_type": _get_content_types(content)
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


def check_agent_session(agent_name: str, threshold: int = DEFAULT_EMPTY_THRESHOLD,
                        timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
                        active_window_hours: int = DEFAULT_ACTIVE_WINDOW_HOURS,
                        verbose: bool = False) -> dict:
    """检查单个agent的最新会话

    Returns:
        {
            "agent": agent_name,
            "session_file": str or None,
            "should_reset": bool,
            "reason": str,
            "last_messages": [...],
            "consecutive_empty": int,
            "minutes_since_last_msg": float or None
        }
    """
    result = {
        "agent": agent_name,
        "session_file": None,
        "should_reset": False,
        "reason": "",
        "last_messages": [],
        "consecutive_empty": 0,
        "minutes_since_last_msg": None
    }

    session_files = get_session_files(agent_name)
    if not session_files:
        result["reason"] = "无会话文件"
        return result

    latest_file = session_files[0]
    result["session_file"] = str(latest_file)

    # ===== 检测条件1：无响应超时 =====
    if timeout_minutes > 0:
        last_time = get_last_assistant_message_time(latest_file)
        if last_time:
            now = datetime.now(tz=timezone.utc)
            minutes_since = (now - last_time).total_seconds() / 60
            result["minutes_since_last_msg"] = round(minutes_since, 1)
            if verbose:
                logger.info(f"  最后assistant消息：{int(minutes_since)}分钟前")

            # 只检查"活跃窗口"内的会话（最近 active_window_hours 小时内有活动）
            # 避免误重置长期空闲的agent
            is_in_active_window = minutes_since <= active_window_hours * 60
            if minutes_since >= timeout_minutes and is_in_active_window:
                result["should_reset"] = True
                result["reason"] = (
                    f"已{int(minutes_since)}分钟无assistant消息"
                    f"（超过阈值{timeout_minutes}分钟），疑似无响应停止"
                )
                return result
        else:
            # 无assistant消息，检查文件修改时间
            mtime = latest_file.stat().st_mtime
            minutes_since = (datetime.now() - datetime.fromtimestamp(mtime)).total_seconds() / 60
            result["minutes_since_last_msg"] = round(minutes_since, 1)
            if verbose:
                logger.info(f"  无assistant消息，文件修改：{int(minutes_since)}分钟前")

    # ===== 检测条件2：连续空消息 =====
    # 获取最后 threshold 条 assistant 消息
    last_msgs = get_last_assistant_messages(latest_file, count=threshold)
    result["last_messages"] = last_msgs

    if not last_msgs:
        result["reason"] = "无assistant消息"
        return result

    # 统计从末尾开始连续空内容消息数
    consecutive_empty = 0
    for msg in reversed(last_msgs):
        if msg["content_empty"]:
            consecutive_empty += 1
        else:
            break

    result["consecutive_empty"] = consecutive_empty

    if verbose and last_msgs:
        logger.info(f"  最后{len(last_msgs)}条assistant消息：")
        for i, msg in enumerate(last_msgs, 1):
            empty_mark = "空" if msg["content_empty"] else "有内容"
            types_str = ",".join(msg["content_type"]) if msg["content_type"] else "-"
            preview = f'"{msg["content_preview"][:40]}"' if msg["content_preview"] else ""
            logger.info(f"    [{i}] {empty_mark} | stopReason={msg['stop_reason']} | types=[{types_str}] {preview}")

    if consecutive_empty < threshold:
        result["reason"] = f"连续���消息数({consecutive_empty}) < 阈值({threshold})"
        return result

    # 检查最后一条消息的stopReason（必须是stop才认为是自动停止）
    last_msg = last_msgs[-1]
    stop_reason = last_msg["stop_reason"]
    if stop_reason not in ("stop", "aborted", "error", None):
        result["reason"] = f"连续空消息{consecutive_empty}条，但stopReason={stop_reason}（不是异常停止）"
        return result

    # 满足重置条件
    result["should_reset"] = True
    result["reason"] = (
        f"连续{consecutive_empty}条assistant消息内容为空，"
        f"stopReason={stop_reason}，疑似异常停止"
    )
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


def run_check(agents: list = None, threshold: int = DEFAULT_EMPTY_THRESHOLD,
              timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
              active_window_hours: int = DEFAULT_ACTIVE_WINDOW_HOURS,
              dry_run: bool = False, verbose: bool = False) -> dict:
    """执行检查和恢复

    Args:
        agents: 指定检查的agent列表，None=全部
        threshold: 连续空消息阈值
        timeout_minutes: 无响应超时阈值（分钟），0=禁用
        active_window_hours: 活跃窗口（小时），只检测此窗口内有活动的agent
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

        result = check_agent_session(agent_name, threshold=threshold,
                                     timeout_minutes=timeout_minutes,
                                     active_window_hours=active_window_hours,
                                     verbose=verbose)
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

  # 调整检测阈值（连续N条空内容，默认3）
  %(prog)s --threshold 3

  # 详细输出（显示每条消息状态）
  %(prog)s --dry-run -v
        """
    )
    parser.add_argument(
        "--agent", "-a", action="append", dest="agents",
        metavar="AGENT_NAME",
        help="指定检查的agent（可多次使用）。默认检查所有agent"
    )
    parser.add_argument(
        "--threshold", "-t", type=int, default=DEFAULT_EMPTY_THRESHOLD,
        help=f"连续空消息阈值（默认{DEFAULT_EMPTY_THRESHOLD}）。达到此数触发重置"
    )
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT_MINUTES,
        help=f"无响应超时阈值（分钟，默认{DEFAULT_TIMEOUT_MINUTES}，0=禁用）。"
             f"超过此时间无新assistant消息则重置会话"
    )
    parser.add_argument(
        "--active-window", type=int, default=DEFAULT_ACTIVE_WINDOW_HOURS,
        dest="active_window",
        help=f"活跃窗口（小时，默认{DEFAULT_ACTIVE_WINDOW_HOURS}）。"
             f"只对最近N小时内有过活动的会话进行超时检测，避免误重置长期空闲的agent"
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true",
        help="只检查不实际重置（预览模式）"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="详细输出每条消息状态"
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
    timeout_label = f"，或{args.timeout}分钟无响应（{args.active_window}小时活跃窗口）" if args.timeout > 0 else ""
    print(f"{'=' * 60}")
    print(f"🔍 Agent会话健康检查{dry_label}")
    print(f"   阈值: 连续{args.threshold}条空内容assistant消息 + 自动停止{timeout_label}")
    print(f"   目录: {AGENTS_BASE}")
    print(f"{'=' * 60}")

    summary = run_check(
        agents=args.agents,
        threshold=args.threshold,
        timeout_minutes=args.timeout,
        active_window_hours=args.active_window,
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
