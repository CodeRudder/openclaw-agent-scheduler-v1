#!/usr/bin/env python3
"""
Claude驱动的智能调度系统

核心逻辑：Claude Agent完整处理整个调度流程
1. 读取群消息，提炼具体问题
2. 分析问题，决策通知策略
3. 生成具体通知内容
4. 执行跨群通知（Mattermost + 飞书）

配置说明：
- config/groups.yaml: 工作群配置、Agent映射、API配置
- config/prompts/decision_prompt.md: AI决策提示词
"""

import os
import sys
import json
import time
import logging
import requests
import yaml
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass

# 获取脚本所在目录，支持相对路径
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_DIR = SCRIPT_DIR.parent

# 配置文件路径
CONFIG_FILE = PROJECT_DIR / "config" / "groups.yaml"
PROMPT_FILE = PROJECT_DIR / "config" / "prompts" / "decision_prompt.md"
HISTORY_FILE = PROJECT_DIR / "data" / "notification_history.json"

# 日志目录
LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> Dict:
    """加载YAML配置文件"""
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"配置文件不存在: {CONFIG_FILE}")

    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def load_prompt() -> str:
    """加载AI决策提示词"""
    if not PROMPT_FILE.exists():
        raise FileNotFoundError(f"提示词文件不存在: {PROMPT_FILE}")

    with open(PROMPT_FILE, 'r', encoding='utf-8') as f:
        return f.read()


# 加载配置
try:
    CONFIG = load_config()
except Exception as e:
    print(f"❌ 配置加载失败: {e}")
    sys.exit(1)

# 从配置中提取
GROUPS = {
    group_id: {
        "channel_id": group["room_id"],
        "name": group["name"],
        "agents": group.get("agents", []),
        "target_groups": group.get("target_groups", [])
    }
    for group_id, group in CONFIG.get("groups", {}).items()
}

AGENT_ROLES = CONFIG.get("agent_roles", {"executors": [], "advisors": []})

# Mattermost配置
MM_URL = CONFIG.get("mattermost", {}).get("url", "http://localhost:8066")
MM_TOKEN = os.environ.get("MATTERMOST_TOKEN", "owwch961rjyn9ctj3qth7j9gra")

# LiteLLM配置
LITELLM_URL = CONFIG.get("litellm", {}).get("url", "http://localhost:4000")
LITELLM_MODEL = CONFIG.get("litellm", {}).get("model", "claude-sonnet-4-6")
LITELLM_API_KEY = os.environ.get("LITELLM_API_KEY", "sk-litellm-openclaw-2026-03-15")

# 飞书配置
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", CONFIG.get("feishu", {}).get("webhook_url", ""))

# 调度配置
SESSION_TIMEOUT_MINUTES = CONFIG.get("scheduler", {}).get("session_timeout_minutes", 10)
MESSAGES_PER_GROUP = CONFIG.get("scheduler", {}).get("messages_per_group", 20)

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / f"claude_scheduler_{datetime.now().strftime('%Y-%m-%d')}.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class GroupMessage:
    """群消息"""
    group_id: str
    group_name: str
    sender: str
    content: str
    timestamp: float


@dataclass
class SchedulingDecision:
    """调度决策"""
    action: str  # notify, wait, ignore
    target_group: str
    target_group_name: str
    mention_users: List[str]
    extracted_issues: List[str]  # 提炼出的具体问题
    message_content: str = ""  # 详细消息内容
    reasoning: str = ""  # 决策理由


class ClaudeDrivenScheduler:
    """Claude驱动的智能调度器"""

    def __init__(self):
        self.headers = {"Authorization": f"Bearer {MM_TOKEN}"}
        self.llm_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LITELLM_API_KEY}"
        }
        self.notification_history = self._load_notification_history()

        # 加载提示词
        try:
            self.system_prompt = load_prompt()
            logger.info("AI决策提示词加载成功")
        except Exception as e:
            logger.error(f"加载提示词失败: {e}")
            self.system_prompt = "你是一个智能调度助手。"

        logger.info("Claude驱动调度器初始化完成")

    def _load_notification_history(self) -> Dict:
        """加载通知历史"""
        try:
            if HISTORY_FILE.exists():
                with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"加载通知历史失败: {e}")
        return {"history": []}

    def _save_notification_history(self):
        """保存通知历史"""
        try:
            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.notification_history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存通知历史失败: {e}")

    def get_group_messages(self, channel_id: str, limit: int = MESSAGES_PER_GROUP) -> List[GroupMessage]:
        """获取群消息（限制总大小不超过30KB）"""
        MAX_TOTAL_SIZE = 30 * 1024  # 30KB
        MAX_MESSAGE_LENGTH = 2000   # 单条消息最大长度

        try:
            # Mattermost API v4: /api/v4/channels/{channel_id}/posts
            resp = requests.get(
                f"{MM_URL}/api/v4/channels/{channel_id}/posts",
                headers=self.headers,
                params={"page": 0, "per_page": limit},
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()

            messages = []
            posts = data.get("posts", {})
            total_size = 0
            # posts是字典，需要按order排序
            order = data.get("order", [])
            for post_id in reversed(order[-limit:]):  # 获取最新的N条
                post = posts.get(post_id, {})
                content = post.get("message", "")

                # 截断过长的消息
                if len(content) > MAX_MESSAGE_LENGTH:
                    content = content[:MAX_MESSAGE_LENGTH] + "...[已截断]"

                # 检查总大小
                if total_size + len(content) > MAX_TOTAL_SIZE:
                    logger.debug(f"  消息总大小达到{MAX_TOTAL_SIZE}字节限制，停止获取")
                    break

                messages.append(GroupMessage(
                    group_id=channel_id,
                    group_name="",
                    sender=post.get("user_id", ""),
                    content=content,
                    timestamp=post.get("create_at", 0) / 1000
                ))
                total_size += len(content)

            return messages
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                logger.warning(f"频道不存在或无权访问: {channel_id}，请检查 config/groups.yaml 中的 room_id 配置")
            else:
                logger.error(f"获取群消息失败 (HTTP {e.response.status_code}): {e}")
            return []
        except requests.exceptions.RequestException as e:
            logger.error(f"网络请求失败: {e}")
            return []
        except Exception as e:
            logger.error(f"获取群消息失败: {e}")
            return []

    def check_agent_session_status(self, group_id: str) -> Dict:
        """检查群的agent会话是否超时"""
        agents = GROUPS.get(group_id, {}).get("agents", [])
        if not agents:
            return {"has_session": False, "is_timeout": False, "agents": []}

        result = {"has_session": False, "agents": [], "is_timeout": False}
        agents_base = Path("/home/gongdewei/.openclaw/agents")

        for agent_name in agents:
            agent_session_dir = agents_base / agent_name / "sessions"
            if not agent_session_dir.exists():
                continue

            session_files = list(agent_session_dir.glob("*.jsonl"))
            if not session_files:
                continue

            latest_file = max(session_files, key=lambda x: x.stat().st_mtime)
            mtime = latest_file.stat().st_mtime
            time_diff = (datetime.now() - datetime.fromtimestamp(mtime)).total_seconds() / 60

            is_timeout = time_diff > SESSION_TIMEOUT_MINUTES

            if is_timeout:
                result["is_timeout"] = True
                logger.info(f"⚠️ {agent_name} 会话超时 ({int(time_diff)}分钟无响应)")

            result["has_session"] = True
            result["agents"].append({
                "name": agent_name,
                "last_activity": mtime,
                "minutes_ago": int(time_diff),
                "is_timeout": is_timeout,
                "session_file": str(latest_file)
            })

        return result

    def analyze_with_claude(self, messages: List[GroupMessage], context: str,
                           target_groups_status: Dict, notification_history: Dict,
                           session_status: Dict) -> Optional[SchedulingDecision]:
        """使用Claude分析并决策"""

        # 构建消息摘要
        msg_summary = "\n".join([
            f"- [{datetime.fromtimestamp(m.timestamp).strftime('%H:%M')}] {m.sender}: {m.content[:100]}"
            for m in messages[-20:]
        ])

        # 构建目标群状态
        target_status = ""
        for group_id, msgs in target_groups_status.items():
            group_name = GROUPS.get(group_id, {}).get("name", group_id)
            target_status += f"\n### {group_name}:\n"
            for m in msgs[-5:]:
                target_status += f"- [{datetime.fromtimestamp(m.timestamp).strftime('%H:%M')}] {m.sender}: {m.content[:80]}\n"

        # 构建通知历史
        history_str = ""
        if notification_history.get("history"):
            history_str = "### 最近通知记录:\n"
            for h in notification_history["history"][-5:]:
                history_str += f"- {h.get('timestamp', '')}: 通知{h.get('target', '')} - {h.get('reason', '')[:50]}\n"

        # 构建会话超时信息
        session_info = ""
        if session_status["is_timeout"]:
            session_info = "\n## ⚠️ Agent会话超时警告\n"
            session_info += "以下agent会话已超时，可能需要重新通知:\n"
            for agent in session_status["agents"]:
                if agent["is_timeout"]:
                    role = "执行者" if agent["name"] in AGENT_ROLES.get("executors", []) else "顾问"
                    session_info += f"- {agent['name']} ({role}): {agent['minutes_ago']}分钟无响应\n"

        user_prompt = f"""{context}

{session_info}

## 当前分析群组消息 (最近20条):
{msg_summary}

## 目标群最新状态:
{target_status}

{history_str}

请分析以上信息，做出调度决策。"""

        try:
            resp = requests.post(
                f"{LITELLM_URL}/v1/chat/completions",
                headers=self.llm_headers,
                json={
                    "model": LITELLM_MODEL,
                    "messages": [
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    "temperature": 0.3,
                    "max_tokens": 1000
                },
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json()

            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

            # 解析JSON
            import re
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                decision_data = json.loads(json_match.group())
                return SchedulingDecision(
                    action=decision_data.get("action", "wait"),
                    target_group=decision_data.get("target_group", ""),
                    target_group_name=decision_data.get("target_group_name", ""),
                    mention_users=decision_data.get("mention_users", []),
                    extracted_issues=decision_data.get("extracted_issues", []),
                    message_content=decision_data.get("message_content", ""),
                    reasoning=decision_data.get("reasoning", "")
                )
        except Exception as e:
            logger.error(f"Claude分析失败: {e}")

        return None

    def generate_bug_document(self, decision: SchedulingDecision) -> Optional[str]:
        """生成BUG详细文档，返回文档路径"""
        if not decision.extracted_issues:
            return None

        # 创建BUG文档目录
        bug_dir = PROJECT_DIR / "data" / "bugs"
        bug_dir.mkdir(parents=True, exist_ok=True)

        # 生成文档名（使用时间戳）
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        doc_file = bug_dir / f"bug_report_{timestamp}.md"

        # 构建文档内容
        content = f"""# 🐛 BUG报告 - {datetime.now().strftime("%Y-%m-%d %H:%M")}

## 📋 基本信息
- **来源群**: {decision.source_group or '未知'}
- **目标群**: {decision.target_group_name or '未知'}
- **优先级**: P0（需要立即处理）

## ❌ 失败项详情

"""
        # 添加每个失败项
        for i, issue in enumerate(decision.extracted_issues, 1):
            content += f"### {i}. {issue}\n\n"

        # 添加建议行动
        content += f"""## 📋 需要行动
@{decision.mention_users[0] if decision.mention_users else 'dev'} 请：
1. 查看以上失败项详情
2. 分析根本原因
3. 实现修复方案
4. 修复完成后通知验收团队重新验收

---
*生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}*
*文档路径: data/bugs/bug_report_{timestamp}.md*
"""

        # 保存文档
        try:
            with open(doc_file, 'w', encoding='utf-8') as f:
                f.write(content)
            logger.info(f"📄 BUG文档已生成: {doc_file}")
            return f"data/bugs/bug_report_{timestamp}.md"
        except Exception as e:
            logger.error(f"生成BUG文档失败: {e}")
            return None

    def send_mattermost_notification(self, decision: SchedulingDecision):
        """发送Mattermost通知（复杂问题自动生成文档）"""
        target_channel = GROUPS.get(decision.target_group, {}).get("channel_id", "")
        if not target_channel:
            logger.error(f"找不到目标群: {decision.target_group}")
            return False

        # 检查是否需要生成文档（问题详情超过300字符）
        issues_content = "\n".join([f"- {i}" for i in decision.extracted_issues])
        doc_path = None

        if len(issues_content) > 300 and decision.extracted_issues:
            doc_path = self.generate_bug_document(decision.extracted_issues, decision.target_group_name)

        mentions = " ".join([f"@{u}" for u in decision.mention_users])

        # 构建消息
        if doc_path:
            # 复杂问题：关键摘要 + 文档链接
            key_issues = decision.extracted_issues[:3]
            summary = "\n".join([f"- {i[:100]}..." if len(i) > 100 else f"- {i}" for i in key_issues])

            message = f"""{mentions}

## ❌ 验收失败项通知

### 🔴 关键问题摘要
{summary}

### 📋 完整问题详情
📄 请查看: `{doc_path}`

> 💡 文档包含完整的失败原因、API路径、错误信息等
"""
        else:
            # 简单问题：直接发送
            message = f"{mentions}\n\n{decision.message_content}"

        try:
            resp = requests.post(
                f"{MM_URL}/api/v4/posts",
                headers=self.headers,
                json={
                    "channel_id": target_channel,
                    "message": message
                },
                timeout=10
            )
            resp.raise_for_status()
            logger.info(f"✅ Mattermost通知发送成功 → {decision.target_group_name}")
            return True
        except Exception as e:
            logger.error(f"Mattermost通知发送失败: {e}")
            return False

    def send_feishu_notification(self, decision: SchedulingDecision):
        """发送飞书通知"""
        if not FEISHU_WEBHOOK:
            return False

        try:
            content = f"""【智能调度通知】
目标: {decision.target_group_name}
@对象: {', '.join(decision.mention_users)}

{decision.message_content}

问题摘要:
{chr(10).join(['• ' + i for i in decision.extracted_issues])}
"""
            resp = requests.post(
                FEISHU_WEBHOOK,
                json={"msg_type": "text", "content": {"text": content}},
                timeout=10
            )
            resp.raise_for_status()
            logger.info("✅ 飞书通知发送成功")
            return True
        except Exception as e:
            logger.error(f"飞书通知发送失败: {e}")
            return False

    def run(self):
        """执行调度"""
        logger.info("=" * 70)
        logger.info(f"🕐 Claude驱动调度开始 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 70)

        decisions_made = 0
        notifications_sent = 0
        notified_groups = set()

        for group_id, group_config in GROUPS.items():
            group_name = group_config["name"]
            logger.info(f"\n🧠 分析 {group_name}...")

            # 获取消息
            messages = self.get_group_messages(group_config["channel_id"])
            if not messages:
                logger.info(f"  跳过: 无消息")
                continue

            logger.info(f"  消息数: {len(messages)}条")

            # 检查会话状态
            session_status = self.check_agent_session_status(group_id)
            if session_status["is_timeout"]:
                timeout_agents = [a for a in session_status["agents"] if a["is_timeout"]]
                logger.info(f"  ⚠️ 检测到 {len(timeout_agents)} 个agent会话超时")

            # 获取目标群状态（增加消息数量以获取完整验收报告）
            target_groups = group_config.get("target_groups", [])
            target_status = {}
            for target_id in target_groups:
                if target_id in GROUPS:
                    # 验收群获取20条消息，其他群获取5条
                    limit = 20 if "acceptance" in target_id or "qa" in target_id else 5
                    target_status[target_id] = self.get_group_messages(
                        GROUPS[target_id]["channel_id"], limit=limit
                    )

            # 构建上下文
            context = f"当前分析群组: {group_name} ({group_id})"

            # Claude分析
            decision = self.analyze_with_claude(
                messages, context, target_status,
                self.notification_history, session_status
            )

            if not decision:
                logger.info(f"  ⏭ 跳过: 无有效决策")
                continue

            logger.info(f"\n📋 Claude决策:")
            logger.info(f"  动作: {decision.action}")
            logger.info(f"  目标: {decision.target_group_name}")
            logger.info(f"  @对象: {', '.join(decision.mention_users)}")
            logger.info(f"  提炼问题: {decision.extracted_issues}")
            logger.info(f"  理由: {decision.reasoning[:100]}...")

            decisions_made += 1

            if decision.action == "notify" and decision.target_group:
                if decision.target_group in notified_groups:
                    logger.info(f"  ⏭ 跳过 {decision.target_group_name} - 已在本轮通知过")
                    continue

                # 发送通知
                if self.send_mattermost_notification(decision):
                    self.send_feishu_notification(decision)
                    notifications_sent += 1
                    notified_groups.add(decision.target_group)

                    # 记录历史
                    self.notification_history.setdefault("history", []).append({
                        "timestamp": datetime.now().isoformat(),
                        "source_group": group_id,
                        "target_group": decision.target_group,
                        "reason": decision.reasoning[:100]
                    })
                    self._save_notification_history()
                    logger.info(f"📝 已记录通知历史 → {group_id}")

        logger.info(f"\n{'=' * 70}")
        logger.info(f"📊 调度总结:")
        logger.info(f"  分析群组: {len(GROUPS)}个")
        logger.info(f"  生成决策: {decisions_made}个")
        logger.info(f"  发送通知: {notifications_sent}个")
        logger.info("=" * 70)
        logger.info(f"🏁 调度结束 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def main():
    """主入口"""
    scheduler = ClaudeDrivenScheduler()
    scheduler.run()


if __name__ == "__main__":
    main()
