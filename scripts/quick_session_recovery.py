#!/usr/bin/env python3
"""
快速会话恢复定时任务

每1分钟检查一次群聊agent会话最后消息的stopReason
如果是异常停止（error/aborted）则发送通知激活

用法：
  python3 scripts/quick_session_recovery.py          # 单次检查
  python3 scripts/quick_session_recovery.py --loop   # 循环检查（每60秒）
"""

import json
import time
import requests
import argparse
import re
import sys
from pathlib import Path
from datetime import datetime, timezone

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

# 尝试导入项目配置
try:
    from config.groups import GROUPS as PROJECT_GROUPS
    from config.config import MM_URL as PROJECT_MM_URL
    from config.config import MM_TOKEN as PROJECT_MM_TOKEN
    GROUPS = PROJECT_GROUPS
    MM_URL = PROJECT_MM_URL
    MM_TOKEN = PROJECT_MM_TOKEN
except ImportError:
    # 使用默认配置
    pass

# 项目根目录
PROJECT_DIR = Path(__file__).parent.parent

# Agent会话目录
AGENTS_BASE = Path("/home/gongdewei/.openclaw/agents")

# 群组配置
GROUPS = {
    "dev-working-group": {
        "name": "开发工作群",
        "channel_id": "9fzie6aawjgnfk6dyohf89p1wh",
        "agents": ["fullstack-dev", "architect"]
    },
    "qa-acceptance-group": {
        "name": "验收测试群",
        "channel_id": "ms1m6pa4f7bwdqfs3k95h39z9y",
        "agents": ["qa", "product"]
    },
    "ops-release-group": {
        "name": "运维发布群",
        "channel_id": "ct5gdky3i7f35q46xj6hy46e1c",
        "agents": ["ops", "architect"]
    },
    "plan-design-group": {
        "name": "规划设计群",
        "channel_id": "t1a4qrggwt8bxy6ebrg5xntu1a",
        "agents": ["product", "ui-designer", "architect", "qa"]
    }
}

# Mattermost配置
MM_URL = "http://localhost:8065"
MM_TOKEN = "e7m6gsf96jnwpm951s91eu415w"

# 激活冷却时间（秒）
ACTIVATION_COOLDOWN = 180  # 3分钟内不重复激活

# 记录文件
RECOVERY_RECORD_FILE = PROJECT_DIR / "data" / "session_recovery_records.json"


def load_recovery_records():
    """加载恢复记录"""
    try:
        if RECOVERY_RECORD_FILE.exists():
            with open(RECOVERY_RECORD_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_recovery_records(records):
    """保存恢复记录"""
    try:
        RECOVERY_RECORD_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RECOVERY_RECORD_FILE, 'w', encoding='utf-8') as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 保存记录失败: {e}")


def get_session_files(agent_name: str) -> list:
    """获取agent的最新会话文件"""
    session_dir = AGENTS_BASE / agent_name / "sessions"
    if not session_dir.exists():
        return []

    files = [f for f in session_dir.glob("*.jsonl") if "backup" not in f.name]
    if not files:
        return []

    # 返回最新的文件
    return sorted(files, key=lambda x: x.stat().st_mtime, reverse=True)[:1]


def get_last_message_stop_reason(file_path: Path) -> dict:
    """获取会话文件最后一条消息的stopReason"""
    result = {
        "stop_reason": None,
        "error_message": None,
        "last_activity": None,
        "content": None
    }

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # 从后往前找最后一条消息
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                if msg.get("type") == "message":
                    message = msg.get("message", {})
                    if message.get("role") == "assistant":
                        result["stop_reason"] = message.get("stopReason")
                        result["error_message"] = message.get("errorMessage")
                        result["content"] = message.get("content", "")[:200]

                        # 尝试解析时间戳
                        # 会话文件没有直接的时间戳，用文件修改时间
                        result["last_activity"] = datetime.fromtimestamp(
                            file_path.stat().st_mtime,
                            tz=timezone.utc
                        )
                        break
            except json.JSONDecodeError:
                continue

    except Exception as e:
        print(f"  ⚠️ 读取文件失败: {e}")

    return result


def send_activation_message(group_id: str, agent_name: str) -> bool:
    """发送激活消息"""
    group_config = GROUPS.get(group_id, {})
    channel_id = group_config.get("channel_id", "")
    group_name = group_config.get("name", group_id)

    if not channel_id:
        print(f"  ⚠️ 找不到群 {group_id} 的channel_id")
        return False

    # 简洁激活消息
    message = f"@{agent_name} 🔄 会话异常中断，请继续处理任务。"

    try:
        resp = requests.post(
            f"{MM_URL}/api/v4/posts",
            headers={"Authorization": f"Bearer {MM_TOKEN}"},
            json={
                "channel_id": channel_id,
                "message": message
            },
            timeout=10
        )
        resp.raise_for_status()
        print(f"  ✅ 已发送激活消息到 {group_name} @{agent_name}")
        return True
    except Exception as e:
        print(f"  ❌ 发送激活消息失败: {e}")
        return False


def check_and_recover():
    """检查所有agent会话并恢复异常停止的"""
    print(f"\n{'='*60}")
    print(f"🔍 会话恢复检查 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # 加载恢复记录
    records = load_recovery_records()
    now = datetime.now(timezone.utc)
    recovered_count = 0

    for group_id, group_config in GROUPS.items():
        group_name = group_config["name"]
        print(f"\n📁 {group_name}")

        for agent_name in group_config["agents"]:
            session_files = get_session_files(agent_name)

            if not session_files:
                print(f"  👤 {agent_name}: 无会话文件")
                continue

            session_info = get_last_message_stop_reason(session_files[0])
            stop_reason = session_info["stop_reason"]

            # 状态显示
            if stop_reason == "error":
                status = "🔴 error"
            elif stop_reason == "aborted":
                status = "❌ aborted"
            elif stop_reason == "stop":
                status = "⏹️ stop"
            elif stop_reason in ("endTurn", "toolUse"):
                status = "🔄 活跃"
            elif stop_reason is None:
                status = "❓ 未知"
            else:
                status = f"❓ {stop_reason}"

            print(f"  👤 {agent_name}: {status}", end="")

            # 检查是否需要恢复（error/aborted）
            if stop_reason in ("error", "aborted"):
                # 检查冷却时间
                record_key = f"{group_id}:{agent_name}"
                last_recovery = records.get(record_key, {}).get("last_recovery")

                if last_recovery:
                    try:
                        last_time = datetime.fromisoformat(last_recovery.replace('Z', '+00:00'))
                        elapsed = (now - last_time).total_seconds()

                        if elapsed < ACTIVATION_COOLDOWN:
                            print(f" → ⏳ 冷却中({int(elapsed)}s/{ACTIVATION_COOLDOWN}s)")
                            continue
                    except Exception:
                        pass

                # 发送激活消息
                print(f" → 📢 发送激活...")
                if send_activation_message(group_id, agent_name):
                    # 更新记录
                    records[record_key] = {
                        "last_recovery": now.isoformat(),
                        "stop_reason": stop_reason,
                        "error_message": session_info.get("error_message", "")[:100]
                    }
                    recovered_count += 1
            else:
                print()  # 换行

    # 保存记录
    save_recovery_records(records)

    print(f"\n{'='*60}")
    print(f"✅ 检查完成，恢复 {recovered_count} 个异常会话")
    return recovered_count


def main():
    parser = argparse.ArgumentParser(description="快速会话恢复定时任务")
    parser.add_argument("--loop", action="store_true", help="循环检查模式（每60秒）")
    parser.add_argument("--interval", type=int, default=60, help="循环间隔（秒，默认60）")
    parser.add_argument("--status", action="store_true", help="只显示状态，不发送激活消息")
    parser.add_argument("--agent", type=str, help="只检查指定agent")
    args = parser.parse_args()

    if args.status:
        # 只显示状态
        show_status_only()
    elif args.loop:
        print(f"🔄 循环检查模式启动，间隔 {args.interval} 秒")
        print("按 Ctrl+C 停止")
        try:
            while True:
                check_and_recover()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n\n⏹️ 已停止")
    else:
        check_and_recover()


def show_status_only():
    """只显示所有agent的会话状态"""
    print(f"\n{'='*60}")
    print(f"📊 Agent会话状态 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    for group_id, group_config in GROUPS.items():
        group_name = group_config["name"]
        print(f"\n📁 {group_name}")

        for agent_name in group_config["agents"]:
            session_files = get_session_files(agent_name)

            if not session_files:
                print(f"  👤 {agent_name}: 无会话文件")
                continue

            session_info = get_last_message_stop_reason(session_files[0])
            stop_reason = session_info["stop_reason"]

            # 状态显示
            if stop_reason == "error":
                status = "🔴 error（需要恢复）"
            elif stop_reason == "aborted":
                status = "❌ aborted（需要恢复）"
            elif stop_reason == "stop":
                status = "⏹️ stop（主动停止）"
            elif stop_reason in ("endTurn", "toolUse"):
                status = "🔄 活跃（运行中）"
            elif stop_reason is None:
                status = "❓ 未知"
            else:
                status = f"❓ {stop_reason}"

            # 显示最后活动时间
            if session_info.get("last_activity"):
                activity_str = session_info["last_activity"].strftime("%H:%M:%S")
            else:
                activity_str = "未知"

            print(f"  👤 {agent_name}: {status} | 最后活动: {activity_str}")

    print(f"\n{'='*60}")


if __name__ == "__main__":
    main()
