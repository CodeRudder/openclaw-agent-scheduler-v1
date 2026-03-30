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

# 加载.env文件
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass  # dotenv未安装，使用系统环境变量

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

# 飞书配置（应用API方式 - 凭证从环境变量读取）
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_CHAT_ID = CONFIG.get("feishu", {}).get("chat_id", os.environ.get("FEISHU_CHAT_ID", ""))
FEISHU_API_BASE = "https://open.feishu.cn/open-apis"

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
    source_group: str = ""  # 来源群组ID
    raw_messages: str = ""  # AI提取的消息内容（备用）
    agent_raw_message: str = ""  # 从API直接获取的agent最后一条完整消息（保证内容不丢失）
    qa_raw_messages: str = ""  # QA原始消息内容（向后兼容）
    bug_doc_complete: bool = True  # BUG文档是否完整


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

    def get_agent_user_id(self, agent_name: str) -> Optional[str]:
        """通过用户名获取agent的user_id"""
        try:
            # 去除@前缀
            clean_name = agent_name.lstrip('@').lower()

            # 获取所有用户列表，通过用户名匹配
            resp = requests.get(
                f"{MM_URL}/api/v4/users",
                headers=self.headers,
                params={"page": 0, "per_page": 200},
                timeout=10
            )
            if resp.status_code != 200:
                logger.error(f"获取用户列表失败: HTTP {resp.status_code}")
                return None

            users = resp.json()
            for user in users:
                if user.get("username", "").lower() == clean_name:
                    return user.get("id")

            logger.debug(f"未找到用户: {agent_name}")
            return None
        except Exception as e:
            logger.error(f"获取agent user_id失败: {e}")
            return None

    def get_agent_last_raw_message(self, channel_id: str, agent_name: str) -> str:
        """获取指定agent在频道中的最后一条完整消息（检查发送人，不是消息内容）"""
        try:
            # 1. 获取agent的user_id
            agent_user_id = self.get_agent_user_id(agent_name)
            logger.debug(f"🔍 查找agent '{agent_name}' 的user_id: {agent_user_id}")
            if not agent_user_id:
                logger.warning(f"⚠️ 无法找到agent用户: {agent_name}")
                return ""

            # 2. 获取频道消息
            resp = requests.get(
                f"{MM_URL}/api/v4/channels/{channel_id}/posts",
                headers=self.headers,
                params={"page": 0, "per_page": 50},
                timeout=10
            )
            if resp.status_code != 200:
                logger.error(f"获取频道消息失败: HTTP {resp.status_code}")
                return ""

            data = resp.json()
            posts = data.get("posts", {})
            order = data.get("order", [])
            logger.debug(f"🔍 频道消息数: {len(posts)}, agent_user_id: {agent_user_id}")

            # 3. 找到该agent发送的最后一条消息（检查发送人user_id）
            for post_id in reversed(order):
                post = posts.get(post_id, {})
                post_user_id = post.get("user_id", "")
                logger.debug(f"  检查消息: post_user_id={post_user_id}, 匹配={post_user_id == agent_user_id}")
                if post_user_id == agent_user_id:
                    content = post.get("message", "")
                    ts = datetime.fromtimestamp(post.get("create_at", 0) / 1000).strftime("%Y-%m-%d %H:%M:%S")
                    logger.info(f"  📥 找到 {agent_name} 的消息 (发送人ID匹配, {len(content)}字符)")
                    return f"[{ts}] {agent_name}:\n{content}"

            logger.warning(f"⚠️ 频道中未找到 {agent_name} 发送的消息 (user_id: {agent_user_id})")
            return ""
        except Exception as e:
            logger.error(f"获取agent消息失败: {e}")
            return ""

    def get_source_group_raw_messages(self, channel_id: str, limit: int = 5) -> str:
        """直接从Mattermost API获取来源群最近N条完整消息（逐字复制，不丢失任何内容）"""
        try:
            # 获取用户名缓存
            username_cache = {}

            resp = requests.get(
                f"{MM_URL}/api/v4/channels/{channel_id}/posts",
                headers=self.headers,
                params={"page": 0, "per_page": limit},
                timeout=10
            )
            if resp.status_code != 200:
                logger.error(f"获取来源群消息失败: HTTP {resp.status_code}")
                return ""

            data = resp.json()
            posts = data.get("posts", {})
            order = data.get("order", [])

            raw_parts = []
            for post_id in reversed(order[-limit:]):
                post = posts.get(post_id, {})
                user_id = post.get("user_id", "")
                content = post.get("message", "")
                ts = datetime.fromtimestamp(post.get("create_at", 0) / 1000).strftime("%H:%M:%S")

                # 获取用户名
                if user_id not in username_cache:
                    try:
                        user_resp = requests.get(
                            f"{MM_URL}/api/v4/users/{user_id}",
                            headers=self.headers,
                            timeout=5
                        )
                        if user_resp.status_code == 200:
                            username_cache[user_id] = user_resp.json().get("username", user_id)
                        else:
                            username_cache[user_id] = user_id
                    except:
                        username_cache[user_id] = user_id

                username = username_cache[user_id]
                raw_parts.append(f"[{ts}] {username}:\n{content}")

            return "\n\n---\n\n".join(raw_parts)
        except Exception as e:
            logger.error(f"获取来源群原始消息失败: {e}")
            return ""

    def get_last_assistant_message_time(self, jsonl_file: Path) -> Optional[datetime]:
        """从jsonl文件中获取最后一条assistant消息的时间"""
        try:
            last_assistant_time = None
            with open(jsonl_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        # 检查是否是 assistant 消息
                        if msg.get("type") == "message":
                            message = msg.get("message", {})
                            if message.get("role") == "assistant":
                                timestamp_str = msg.get("timestamp")
                                if timestamp_str:
                                    # 解析 ISO 格式时间戳
                                    last_assistant_time = datetime.fromisoformat(
                                        timestamp_str.replace('Z', '+00:00')
                                    )
                    except json.JSONDecodeError:
                        continue
            return last_assistant_time
        except Exception as e:
            logger.debug(f"解析jsonl文件失败 {jsonl_file}: {e}")
            return None

    def check_agent_session_status(self, group_id: str) -> Dict:
        """检查群的agent会话是否超时（基于最后一条assistant消息时间）"""
        agents = GROUPS.get(group_id, {}).get("agents", [])
        if not agents:
            return {"has_session": False, "is_timeout": False, "agents": []}

        result = {"has_session": False, "agents": [], "is_timeout": False}
        agents_base = Path("/home/gongdewei/.openclaw/agents")

        for agent_name in agents:
            agent_session_dir = agents_base / agent_name / "sessions"
            if not agent_session_dir.exists():
                continue

            # 查找最新的 session 文件（排除 backup 文件）
            session_files = [f for f in agent_session_dir.glob("*.jsonl")
                           if "backup" not in f.name]
            if not session_files:
                continue

            latest_file = max(session_files, key=lambda x: x.stat().st_mtime)

            # 尝试从 jsonl 中获取最后一条 assistant 消息时间
            last_activity = self.get_last_assistant_message_time(latest_file)

            if last_activity:
                # 使用 assistant 消息时间计算超时
                time_diff = (datetime.now(last_activity.tzinfo) - last_activity).total_seconds() / 60
            else:
                # 回退：使用文件修改时间
                mtime = latest_file.stat().st_mtime
                time_diff = (datetime.now() - datetime.fromtimestamp(mtime)).total_seconds() / 60

            is_timeout = time_diff > SESSION_TIMEOUT_MINUTES

            if is_timeout:
                result["is_timeout"] = True
                logger.info(f"⚠️ {agent_name} 会话超时 ({int(time_diff)}分钟无响应)")

            result["has_session"] = True
            result["agents"].append({
                "name": agent_name,
                "last_activity": last_activity.isoformat() if last_activity else None,
                "minutes_ago": int(time_diff),
                "is_timeout": is_timeout,
                "session_file": str(latest_file)
            })

        return result

    def analyze_with_claude(self, messages: List[GroupMessage], context: str,
                           target_groups_status: Dict, notification_history: Dict,
                           session_status: Dict, source_group: str = "") -> Optional[SchedulingDecision]:
        """使用Claude分析并决策"""

        # 构建消息摘要
        msg_summary = "\n".join([
            f"- [{datetime.fromtimestamp(m.timestamp).strftime('%H:%M')}] {m.sender}: {m.content[:150]}"
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

请分析以上信息，做出调度决策。注意：raw_messages字段不需要填写，系统会自动从API获取。"""

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
                    "max_tokens": 2000
                },
                timeout=60
            )
            resp.raise_for_status()
            data = resp.json()

            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            logger.debug(f"AI返回内容: {content[:200]}")

            # 解析JSON - 处理截断的JSON
            import re
            json_match = re.search(r'\{[\s\S]*\}', content)
            if not json_match:
                # 尝试修复截断的JSON
                json_match = re.search(r'\{[\s\S]*', content)

            if json_match:
                json_str = json_match.group()
                # 如果JSON不完整（没有闭合的}），尝试补全
                if not json_str.rstrip().endswith('}'):
                    # 找到最后一个完整的键值对
                    # 尝试在最后一个逗号或冒号处截断并闭合
                    for i in range(len(json_str) - 1, -1, -1):
                        if json_str[i] in ['"', ']']:
                            # 找到闭合位置
                            brace_count = 0
                            for j in range(i, -1, -1):
                                if json_str[j] == '}': brace_count += 1
                                if json_str[j] == '{': brace_count -= 1
                            json_str = json_str[:i+1] + '}'
                            break
                    logger.warning(f"JSON被截断，尝试修复")

                try:
                    decision_data = json.loads(json_str)
                except json.JSONDecodeError:
                    logger.error(f"JSON解析失败，原始内容: {json_str[:300]}")
                    return None

                return SchedulingDecision(
                    action=decision_data.get("action", "wait"),
                    target_group=decision_data.get("target_group", ""),
                    target_group_name=decision_data.get("target_group_name", ""),
                    mention_users=decision_data.get("mention_users", []),
                    extracted_issues=decision_data.get("extracted_issues", []),
                    message_content=decision_data.get("message_content", ""),
                    reasoning=decision_data.get("reasoning", ""),
                    source_group=source_group,
                    raw_messages="",
                    qa_raw_messages="",
                    bug_doc_complete=decision_data.get("bug_doc_complete", True)
                )
        except Exception as e:
            logger.error(f"Claude分析失败: {e}")

        return None

    def generate_bug_document(self, decision: SchedulingDecision) -> Optional[str]:
        """生成BUG详细文档，使用QA原始消息，返回文档路径"""
        if not decision.extracted_issues:
            return None

        # 创建BUG文档目录
        bug_dir = PROJECT_DIR / "data" / "bugs"
        bug_dir.mkdir(parents=True, exist_ok=True)

        # 生成文档名（使用时间戳）
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        doc_file = bug_dir / f"bug_report_{timestamp}.md"

        # 构建文档内容 - 使用QA原始消息
        content = f"""# 🐛 BUG报告 - {datetime.now().strftime("%Y-%m-%d %H:%M")}

## 📋 基本信息
- **来源群**: {decision.source_group or '未知'}
- **目标群**: {decision.target_group_name or '未知'}
- **优先级**: P0（需要立即处理）

## ❌ 失败项摘要

"""
        # 添加每个失败项摘要
        for i, issue in enumerate(decision.extracted_issues, 1):
            content += f"{i}. {issue}\n"

        # 添加原始消息（优先使用API直接获取的完整消息，保证内容不丢失）
        raw_content = decision.agent_raw_message or decision.raw_messages or decision.qa_raw_messages
        if raw_content:
            content += f"""
## 📝 原始报告（完整内容）

{raw_content}

"""

        # 添加建议行动
        content += f"""## 📋 需要行动
@{decision.mention_users[0] if decision.mention_users else 'dev'} 请：
1. 查看以上QA原始报告的完整内容
2. 分析根本原因
3. 实现修复方案
4. 修复完成后通知验收团队重新验收

---
*生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}*
*文档路径: {doc_file}*
"""

        # 保存文档
        try:
            with open(doc_file, 'w', encoding='utf-8') as f:
                f.write(content)
            logger.info(f"📄 BUG文档已生成: {doc_file}")
            return str(doc_file)  # 返回全路径
        except Exception as e:
            logger.error(f"生成BUG文档失败: {e}")
            return None

    def send_mattermost_notification(self, decision: SchedulingDecision):
        """发送Mattermost通知（复杂问题自动生成文档）"""

        # 检查文档是否完整
        if not decision.bug_doc_complete and decision.extracted_issues:
            # 文档不完整 → 通知验收群重新生成
            logger.warning("⚠️ BUG文档不完整，通知QA重新生成")
            target_group = "qa-acceptance-group"
            target_channel = GROUPS.get(target_group, {}).get("channel_id", "")
            mention_users = GROUPS.get(target_group, {}).get("agents", [])
            mentions = " ".join([f"@{u.lstrip('@')}" for u in mention_users])

            message = f"""{mentions}

## ⚠️ BUG报告不完整，请重新生成

### 📋 当前报告缺少以下必要信息:
1. **操作步骤**: 具体做了什么操作（UI操作或API调用）
2. **输入数据/参数**: 测试使用的具体数据
3. **实际结果**: 返回的状态码、响应体、错误信息
4. **期望结果**: 应该返回什么

### ❌ 当前报告内容（不完整）:
{chr(10).join([f'- {i}' for i in decision.extracted_issues])}

### 📝 请重新生成完整报告
**重要**：请按以下格式生成BUG报告，并保存到文件：

\`\`\`
data/bugs/TC-XXX_description.md
\`\`\`

**报告格式**:
\`\`\`markdown
# TC-XXX: [测试用例名称]

## 操作步骤
[具体的UI操作步骤或API调用]

## 输入数据/参数
[请求body/query/path参数]

## 实际结果
[返回的状态码、响应体、错误信息]

## 期望结果
[应该返回什么]
\`\`\`

> 💡 **完整BUG报告 + 文件路径 = 开发可以立即开始修复**
"""
        else:
            # 正常通知流程
            target_channel = GROUPS.get(decision.target_group, {}).get("channel_id", "")
            if not target_channel:
                logger.error(f"找不到目标群: {decision.target_group}")
                return False

            # 验收问题必须生成文档
            doc_path = None
            if decision.extracted_issues:
                doc_path = self.generate_bug_document(decision)

            mentions = " ".join([f"@{u.lstrip('@')}" for u in decision.mention_users])

            # 获取原始消息内容（优先使用API直接获取的完整消息）
            raw_content = decision.agent_raw_message or decision.raw_messages or decision.qa_raw_messages

            # 构建消息（有文档时附带文档路径，但仍包含关键原始内容）
            if doc_path:
                key_issues = decision.extracted_issues[:3]
                summary = "\n".join([f"- {i[:100]}..." if len(i) > 100 else f"- {i}" for i in key_issues])

                # 截取原始消息关键部分（保留账号密码等细节）
                raw_preview = raw_content[:1500] if raw_content else ""

                message = f"""{mentions}

## ❌ 验收失败项通知

### 🔴 关键问题摘要
{summary}

### 📋 原始消息内容
{raw_preview}
{"..." if raw_content and len(raw_content) > 1500 else ""}

### 📄 完整报告
详见: `{doc_path}`
"""
            else:
                # 无文档时，直接包含原始消息内容
                raw_content = decision.agent_raw_message or decision.raw_messages or decision.qa_raw_messages
                if raw_content:
                    message = f"""{mentions}

## 📋 跨群消息转发

{decision.message_content}

---
### 📝 原始消息内容
{raw_content[:2000]}{"..." if len(raw_content) > 2000 else ""}
"""
                else:
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

    def get_feishu_token(self) -> Optional[str]:
        """获取飞书tenant_access_token"""
        if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
            return None

        try:
            url = f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal"
            resp = requests.post(
                url,
                json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    return data.get("tenant_access_token")
        except Exception as e:
            logger.error(f"获取飞书token失败: {e}")
        return None

    def send_feishu_notification(self, decision: SchedulingDecision):
        """发送飞书通知（使用应用API）"""
        if not FEISHU_CHAT_ID:
            logger.warning("飞书Chat ID未配置，跳过通知")
            return False

        # 获取token
        token = self.get_feishu_token()
        if not token:
            logger.warning("获取飞书token失败，跳过通知")
            return False

        try:
            # 获取原始消息内容（优先使用API直接获取的完整消息）
            raw_content = decision.agent_raw_message or decision.raw_messages or decision.qa_raw_messages

            # 构建富文本消息
            content_lines = [
                [{"tag": "text", "text": f"【智能调度通知】", "style": ["bold"]}],
                [{"tag": "text", "text": f"目标: {decision.target_group_name}"}],
                [{"tag": "text", "text": f"@对象: {', '.join(decision.mention_users)}"}],
                [{"tag": "text", "text": ""}],
                [{"tag": "text", "text": decision.message_content[:300] if decision.message_content else ""}],
            ]

            # 添加原始消息（包含密码、账号等细节）
            if raw_content:
                content_lines.append([{"tag": "text", "text": ""}])
                content_lines.append([{"tag": "text", "text": "原始消息:", "style": ["bold"] }])
                # 截断过长的内容
                truncated = raw_content[:800] + "..." if len(raw_content) > 800 else raw_content
                content_lines.append([{"tag": "text", "text": truncated}])

            if decision.extracted_issues:
                content_lines.append([{"tag": "text", "text": ""}])
                content_lines.append([{"tag": "text", "text": "问题摘要:", "style": ["bold"] }])
                for issue in decision.extracted_issues[:5]:  # 最多5个
                    content_lines.append([{"tag": "text", "text": f"• {issue[:100]}"}])

            rich_text = {
                "zh_cn": {
                    "title": "调度通知",
                    "content": content_lines
                }
            }

            url = f"{FEISHU_API_BASE}/im/v1/messages"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            payload = {
                "receive_id": FEISHU_CHAT_ID,
                "msg_type": "post",
                "content": json.dumps(rich_text)
            }

            resp = requests.post(
                url,
                headers=headers,
                params={"receive_id_type": "chat_id"},
                json=payload,
                timeout=10
            )

            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    logger.info("✅ 飞书通知发送成功")
                    return True
                else:
                    logger.error(f"飞书通知发送失败: {data.get('msg')}")
            else:
                logger.error(f"飞书HTTP错误: {resp.status_code}")
            return False
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
                self.notification_history, session_status, group_id
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

                # 🔑 关键：获取来源群的原始消息内容
                # 1. 首先尝试获取mention_agent的最后一条消息（检查发送人user_id）
                # 2. 如果agent没有发送过消息，则获取来源群所有最近消息
                source_channel_id = GROUPS.get(decision.source_group, {}).get("channel_id", "")
                if source_channel_id:
                    raw_msg = ""
                    if decision.mention_users:
                        # 尝试获取agent的最后一条消息
                        first_agent = decision.mention_users[0].lstrip('@')
                        raw_msg = self.get_agent_last_raw_message(source_channel_id, first_agent)
                        if raw_msg:
                            logger.info(f"  📥 获取 {first_agent} 的原始消息 ({len(raw_msg)}字符)")

                    # 如果agent没有发送过消息，获取来源群所有最近消息
                    if not raw_msg:
                        raw_msg = self.get_source_group_raw_messages(source_channel_id, limit=5)
                        if raw_msg:
                            logger.info(f"  📥 获取来源群最近消息 ({len(raw_msg)}字符)")

                    if raw_msg:
                        decision.agent_raw_message = raw_msg

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
