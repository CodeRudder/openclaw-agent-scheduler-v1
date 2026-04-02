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
import os
import yaml
from pathlib import Path
from datetime import datetime, timezone

# 添加项目路径
PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

# 加载全部配置（从groups.yaml读取）
def load_all_config():
    """从groups.yaml加载全部配置"""
    config = {}
    groups_file = PROJECT_DIR / "config" / "groups.yaml"
    if groups_file.exists():
        try:
            with open(groups_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
        except Exception:
            pass
    return config

# 加载配置
ALL_CONFIG = load_all_config()
GROUPS = ALL_CONFIG.get("groups", {})

# Mattermost配置 - 从groups.yaml读取
MM_CONFIG = ALL_CONFIG.get("mattermost", {})
MM_URL = os.environ.get("MM_URL", MM_CONFIG.get("url", "http://localhost:8066"))
MM_ADMIN_USER = MM_CONFIG.get("admin_user", "")
MM_ADMIN_PASSWORD = MM_CONFIG.get("admin_password", "")

# Token缓存
_MM_TOKEN = None

def get_mm_token():
    """获取Mattermost Token（登录获取）"""
    global _MM_TOKEN
    if _MM_TOKEN:
        return _MM_TOKEN

    # 优先使用环境变量
    env_token = os.environ.get("MM_TOKEN")
    if env_token:
        _MM_TOKEN = env_token
        return _MM_TOKEN

    # 使用用户名密码登录获取token
    if MM_ADMIN_USER and MM_ADMIN_PASSWORD:
        try:
            resp = requests.post(
                f"{MM_URL}/api/v4/users/login",
                json={
                    "login_id": MM_ADMIN_USER,
                    "password": MM_ADMIN_PASSWORD
                },
                timeout=10
            )
            if resp.status_code == 200:
                _MM_TOKEN = resp.headers.get("Token", "")
                return _MM_TOKEN
        except Exception as e:
            print(f"⚠️ Mattermost登录失败: {e}")

    return ""

# Agent会话目录
AGENTS_BASE = Path("/home/gongdewei/.openclaw/agents")

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
    """发送激活消息（不@agent，避免触发通知风暴）"""
    group_config = GROUPS.get(group_id, {})
    channel_id = group_config.get("room_id", group_config.get("channel_id", ""))
    group_name = group_config.get("name", group_id)

    if not channel_id:
        print(f"  ⚠️ 找不到群 {group_id} 的room_id")
        return False

    # 获取token
    token = get_mm_token()
    if not token:
        print(f"  ⚠️ 无法获取Mattermost Token")
        return False

    # 简洁激活消息（@agent触发回复）
    message = f"@{agent_name} 🔄 会话异常中断，请继续处理任务。"

    try:
        resp = requests.post(
            f"{MM_URL}/api/v4/posts",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "channel_id": channel_id,
                "message": message
            },
            timeout=10
        )
        resp.raise_for_status()
        print(f"  ✅ 已发送激活消息到 {group_name}")
        return True
    except Exception as e:
        print(f"  ❌ 发送激活消息失败: {e}")
        return False


def check_and_recover():
    """检查所有agent会话并恢复异常停止的

    ⚠️ 只激活有执行中/阻塞任务的agent，空闲agent禁止激活
    """
    print(f"\n{'='*60}")
    print(f"🔍 会话恢复检查 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # 加载调度计划，获取有任务的agent列表
    active_agents = set()  # 有执行中/阻塞任务的agent
    try:
        plan_file = PROJECT_DIR / "data" / "scheduling_plan.json"
        if plan_file.exists():
            with open(plan_file, 'r', encoding='utf-8') as f:
                plan = json.load(f)

            for milestone in plan.get("milestones", []):
                status = milestone.get("status", "")
                assigned_to = milestone.get("assigned_to", "")
                if status in ("in_progress", "blocked") and assigned_to:
                    # assigned_to可能是"all"或具体agent名
                    if assigned_to == "all":
                        continue
                    active_agents.add(assigned_to.lstrip('@').lower())

            print(f"📋 有活跃任务的agent: {active_agents if active_agents else '无'}")
    except Exception as e:
        print(f"⚠️ 加载调度计划失败: {e}")

    # 加载恢复记录
    records = load_recovery_records()
    now = datetime.now(timezone.utc)
    recovered_count = 0
    skipped_count = 0

    for group_id, group_config in GROUPS.items():
        group_name = group_config.get("name", group_id)
        agents = group_config.get("agents", [])

        if not agents:
            continue

        print(f"\n📁 {group_name}")

        for agent_name in agents:
            agent_key = agent_name.lower()
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
                # ⚠️ 关键检查：是否有活跃任务？
                if agent_key not in active_agents:
                    print(f" → ⏭️ 跳过（无活跃任务）")
                    skipped_count += 1
                    continue

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
    print(f"✅ 检查完成")
    print(f"   - 恢复异常会话: {recovered_count} 个")
    print(f"   - 跳过空闲agent: {skipped_count} 个")
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
        group_name = group_config.get("name", group_id)
        agents = group_config.get("agents", [])

        if not agents:
            continue

        print(f"\n📁 {group_name}")

        for agent_name in agents:
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
