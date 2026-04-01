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
PLAN_FILE = PROJECT_DIR / "data" / "scheduling_plan.json"

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
MM_TOKEN = os.environ.get("MATTERMOST_TOKEN", "ex7dwb5j6fruzmjtjjuk34777r")

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
        self.scheduling_plan = self._load_scheduling_plan()

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

    def _load_scheduling_plan(self) -> Dict:
        """加载调度计划"""
        try:
            if PLAN_FILE.exists():
                with open(PLAN_FILE, 'r', encoding='utf-8') as f:
                    plan = json.load(f)
                    if plan:
                        # 检查是否需要归档（版本已闭环）
                        if plan.get("overall_status") == "completed":
                            logger.info(f"📋 版本 {plan.get('current_version')} 已闭环，计划将归档")
                            self._archive_plan(plan)
                            return {}
                        logger.info(f"📋 调度计划加载成功: {plan.get('current_version', '未知版本')}")
                        return plan
        except Exception as e:
            logger.warning(f"加载调度计划失败: {e}")
        return {}

    def _archive_plan(self, plan: Dict):
        """归档已完成的计划"""
        try:
            archive_file = PLAN_FILE.parent / "plan_archive.json"
            archive = []
            if archive_file.exists():
                with open(archive_file, 'r', encoding='utf-8') as f:
                    archive = json.load(f)
            # 添加归档记录
            archive.append({
                "version": plan.get("current_version"),
                "completed_at": datetime.now().isoformat(),
                "milestones": plan.get("milestones", [])
            })
            # 只保留最近5个版本的归档
            if len(archive) > 5:
                archive = archive[-5:]
            with open(archive_file, 'w', encoding='utf-8') as f:
                json.dump(archive, f, ensure_ascii=False, indent=2)
            logger.info(f"📋 已归档版本 {plan.get('current_version')} 的计划")
            # 清空当前计划文件
            with open(PLAN_FILE, 'w', encoding='utf-8') as f:
                json.dump({}, f)
        except Exception as e:
            logger.error(f"归档计划失败: {e}")

    def _save_scheduling_plan(self):
        """保存调度计划"""
        try:
            PLAN_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.scheduling_plan["last_updated"] = datetime.now().isoformat()
            with open(PLAN_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.scheduling_plan, f, ensure_ascii=False, indent=2)
            logger.info("📋 调度计划已保存")
        except Exception as e:
            logger.error(f"保存调度计划失败: {e}")

    def get_group_messages(self, channel_id: str, limit: int = MESSAGES_PER_GROUP) -> List[GroupMessage]:
        """获取群消息 - 提取群成员和claw-admin消息，每成员最后10条"""
        MAX_TOTAL_SIZE = 50 * 1024  # 50KB
        MAX_MESSAGE_LENGTH = 2000   # 单条消息最大长度
        MESSAGES_PER_MEMBER = 10    # 每个成员最多取10条

        # 允许的用户名（群成员 + claw-admin）
        allowed_usernames = {"claw-admin"}  # claw-admin也允许

        # 获取当前群的agent成员列表
        group_agents = set()
        for gid, gcfg in GROUPS.items():
            if gcfg.get("channel_id") == channel_id:
                group_agents = set(a.lstrip('@').lower() for a in gcfg.get("agents", []))
                break
        allowed_usernames.update(group_agents)

        # 获取允许用户的user_id映射
        user_id_map = {}  # user_id -> username
        try:
            resp = requests.get(
                f"{MM_URL}/api/v4/users",
                headers=self.headers,
                params={"page": 0, "per_page": 200},
                timeout=10
            )
            if resp.status_code == 200:
                for user in resp.json():
                    uname = user.get("username", "").lower()
                    if uname in allowed_usernames:
                        user_id_map[user.get("id")] = uname
        except:
            pass

        try:
            resp = requests.get(
                f"{MM_URL}/api/v4/channels/{channel_id}/posts",
                headers=self.headers,
                params={"page": 0, "per_page": limit * 2},
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()

            # 收集所有消息，按sender分组
            member_messages = {}  # user_id -> [GroupMessage, ...]
            posts = data.get("posts", {})
            order = data.get("order", [])

            for post_id in reversed(order):
                post = posts.get(post_id, {})
                user_id = post.get("user_id", "")
                content = post.get("message", "")

                # 只保留允许的用户消息（群成员 + claw-admin）
                if user_id not in user_id_map:
                    continue

                # 截断过长的消息
                if len(content) > MAX_MESSAGE_LENGTH:
                    content = content[:MAX_MESSAGE_LENGTH] + "...[已截断]"

                msg = GroupMessage(
                    group_id=channel_id,
                    group_name="",
                    sender=user_id_map[user_id],
                    content=content,
                    timestamp=post.get("create_at", 0) / 1000
                )

                if user_id not in member_messages:
                    member_messages[user_id] = []
                member_messages[user_id].append(msg)

            # 每个成员只保留最后10条
            all_messages = []
            for uid, msgs in member_messages.items():
                all_messages.extend(msgs[-MESSAGES_PER_MEMBER:])

            # 按时间排序
            all_messages.sort(key=lambda m: m.timestamp)

            # 限制总大小
            result = []
            total_size = 0
            for msg in all_messages:
                if total_size + len(msg.content) > MAX_TOTAL_SIZE:
                    break
                result.append(msg)
                total_size += len(msg.content)

            logger.info(f"  提取成员消息: {len(member_messages)}个成员, {len(result)}条消息")
            return result

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                logger.warning(f"频道不存在或无权访问: {channel_id}")
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

    def get_last_assistant_stop_reason(self, jsonl_file: Path) -> Optional[str]:
        """获取最后一条assistant消息的stopReason（检测异常终止/主动停止）
        如果最后一条消息是user消息（即有新请求），返回None表示处理中
        """
        try:
            last_stop_reason = None
            last_role = None
            with open(jsonl_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        if msg.get("type") == "message":
                            message = msg.get("message", {})
                            role = message.get("role")
                            if role == "assistant":
                                last_stop_reason = message.get("stopReason")
                                last_role = "assistant"
                            elif role == "user":
                                last_role = "user"
                                last_stop_reason = None  # 有新user消息，重置为处理中
                    except json.JSONDecodeError:
                        continue
            # 如果最后一条是user消息，说明agent正在处理，返回None
            return last_stop_reason
        except Exception as e:
            logger.debug(f"解析jsonl stopReason失败 {jsonl_file}: {e}")
            return None

    def get_last_assistant_messages(self, jsonl_file: Path, count: int = 2) -> List[Dict]:
        """获取最后N条assistant消息，返回content(截断1KB)、stopReason、errorMessage"""
        MAX_CONTENT_SIZE = 1024  # 1KB
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
                                # 提取content
                                content = message.get("content", "")
                                # content可能是字符串或数组
                                if isinstance(content, list):
                                    # 提取text内容
                                    text_parts = []
                                    for item in content:
                                        if isinstance(item, dict) and item.get("type") == "text":
                                            text_parts.append(item.get("text", ""))
                                        elif isinstance(item, str):
                                            text_parts.append(item)
                                    content = "\n".join(text_parts)

                                # 截断到1KB
                                if len(content) > MAX_CONTENT_SIZE:
                                    content = content[:MAX_CONTENT_SIZE] + "...(截断)"

                                assistant_msgs.append({
                                    "content": content,
                                    "stopReason": message.get("stopReason"),
                                    "errorMessage": message.get("errorMessage")
                                })
                    except json.JSONDecodeError:
                        continue
            # 返回最后N条
            return assistant_msgs[-count:] if assistant_msgs else []
        except Exception as e:
            logger.debug(f"读取assistant消息失败 {jsonl_file}: {e}")
            return []

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

            # 获取会话停止原因
            stop_reason = self.get_last_assistant_stop_reason(latest_file)
            if stop_reason and stop_reason != "toolUse" and stop_reason != "endTurn":
                logger.info(f"  📋 {agent_name} 会话状态: stopReason={stop_reason}")

            # 获取最后2条assistant消息（用于AI分析）
            last_assistant_msgs = self.get_last_assistant_messages(latest_file, count=2)

            result["has_session"] = True
            result["agents"].append({
                "name": agent_name,
                "last_activity": last_activity.isoformat() if last_activity else None,
                "minutes_ago": int(time_diff),
                "is_timeout": is_timeout,
                "stop_reason": stop_reason,
                "session_file": str(latest_file),
                "last_assistant_messages": last_assistant_msgs
            })

        return result

    def send_activation_message(self, group_id: str, agent_name: str) -> bool:
        """发送激活消息到群，@Agent通知继续处理"""
        channel_id = GROUPS.get(group_id, {}).get("channel_id", "")
        if not channel_id:
            logger.warning(f"找不到群 {group_id} 的channel_id")
            return False

        message = f"@{agent_name} 请继续处理当前任务，会话已超时。"

        try:
            resp = requests.post(
                f"{MM_URL}/api/v4/posts",
                headers=self.headers,
                json={
                    "channel_id": channel_id,
                    "message": message
                },
                timeout=10
            )
            resp.raise_for_status()
            logger.info(f"  📢 已发送激活消息到 {GROUPS.get(group_id, {}).get('name', group_id)} @{agent_name}")
            return True
        except Exception as e:
            logger.error(f"发送激活消息失败: {e}")
            return False

    def send_task_inquiry_message(self, group_id: str, agent_name: str, task_desc: str) -> bool:
        """发送任务询问消息，询问Agent任务完成情况，未完成则继续处理"""
        channel_id = GROUPS.get(group_id, {}).get("channel_id", "")
        if not channel_id:
            logger.warning(f"找不到群 {group_id} 的channel_id")
            return False

        message = f"@{agent_name} 请确认任务进度：{task_desc[:50]}...\n如已完成请回复确认，如未完成请继续处理。"

        try:
            resp = requests.post(
                f"{MM_URL}/api/v4/posts",
                headers=self.headers,
                json={
                    "channel_id": channel_id,
                    "message": message
                },
                timeout=10
            )
            resp.raise_for_status()
            logger.info(f"  📋 已发送任务询问到 {GROUPS.get(group_id, {}).get('name', group_id)} @{agent_name}")
            return True
        except Exception as e:
            logger.error(f"发送任务询问失败: {e}")
            return False

    def reset_agent_session(self, session_file: str, agent_name: str) -> bool:
        """重置agent会话（重命名会话文件）"""
        try:
            session_path = Path(session_file)
            if not session_path.exists():
                logger.warning(f"会话文件不存在: {session_file}")
                return False

            # 重命名为 backup 文件
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = session_path.parent / f"{session_path.stem}_backup_{timestamp}.jsonl"
            session_path.rename(backup_path)
            logger.info(f"  🔄 已重置会话: {session_path.name} → {backup_path.name}")
            return True
        except Exception as e:
            logger.error(f"重置会话失败: {e}")
            return False

    def get_activation_attempts(self, group_id: str, agent_name: str) -> int:
        """获取激活尝试次数"""
        attempts = self.notification_history.get("activation_attempts", {})
        key = f"{group_id}:{agent_name}"
        return attempts.get(key, {}).get("count", 0) if isinstance(attempts.get(key), dict) else attempts.get(key, 0)

    def get_activation_last_activity(self, group_id: str, agent_name: str) -> Optional[str]:
        """获取上次激活时记录的会话活动时间"""
        attempts = self.notification_history.get("activation_attempts", {})
        key = f"{group_id}:{agent_name}"
        entry = attempts.get(key, {})
        if isinstance(entry, dict):
            return entry.get("last_activity")
        return None

    def set_activation_attempt(self, group_id: str, agent_name: str, count: int, last_activity: str):
        """设置激活尝试次数和上次会话活动时间"""
        if "activation_attempts" not in self.notification_history:
            self.notification_history["activation_attempts"] = {}
        key = f"{group_id}:{agent_name}"
        self.notification_history["activation_attempts"][key] = {
            "count": count,
            "last_activity": last_activity
        }
        self._save_notification_history()

    def clear_activation_attempt(self, group_id: str, agent_name: str):
        """清除激活尝试次数（会话恢复活动后调用）"""
        if "activation_attempts" not in self.notification_history:
            return
        key = f"{group_id}:{agent_name}"
        if key in self.notification_history["activation_attempts"]:
            del self.notification_history["activation_attempts"][key]
            self._save_notification_history()

    def handle_timeout_agents(self, blocking_tasks: List[Dict]):
        """处理阻塞任务的agent：AI识别阻塞任务 → 装入检查会话超时 → 仅激活确认超时的
        Args:
            blocking_tasks: AI分析返回的阻塞任务列表，每个包含group_id/agent/task/reason
        """
        if not blocking_tasks:
            logger.info("  ✅ 无阻塞任务")
            return

        agents_base = Path("/home/gongdewei/.openclaw/agents")
        handled = 0

        for bt in blocking_tasks:
            group_id = bt.get("group_id", "")
            agent_name = bt.get("agent", "")
            task_desc = bt.get("task", "?")
            reason = bt.get("reason", "")

            # 兼容：AI可能返回group_name需要转换
            if not group_id:
                group_name = bt.get("group", "")
                for gid, gcfg in GROUPS.items():
                    if gcfg.get("name") == group_name:
                        group_id = gid
                        break

            group_name = GROUPS.get(group_id, {}).get("name", group_id)

            if not agent_name:
                continue

            # ===== 步骤2：检查该agent的会话状态 =====
            agent_session_dir = agents_base / agent_name / "sessions"
            if not agent_session_dir.exists():
                logger.info(f"  ⚠️ {group_name}-{agent_name}: 阻塞但无会话文件，跳过")
                continue

            session_files = [f for f in agent_session_dir.glob("*.jsonl")
                            if "backup" not in f.name]
            if not session_files:
                logger.info(f"  ⚠️ {group_name}-{agent_name}: 阻塞但无活跃会话，跳过")
                continue

            latest_file = max(session_files, key=lambda x: x.stat().st_mtime)

            # ===== 步骤2a：检查会话是否异常终止（stopReason=aborted/error） =====
            stop_reason = self.get_last_assistant_stop_reason(latest_file)
            if stop_reason in ("aborted", "error"):
                logger.info(f"\n  🚨 异常终止: {group_name}-{agent_name}: {task_desc}")
                logger.info(f"     stopReason={stop_reason} → 立即通知继续处理")
                self.send_activation_message(group_id, agent_name)
                self.clear_activation_attempt(group_id, agent_name)
                handled += 1
                continue

            # ===== 步骤2a2：检查会话是否主动停止（stopReason=stop） =====
            if stop_reason == "stop":
                logger.info(f"\n  📋 会话已停止: {group_name}-{agent_name}: {task_desc}")
                logger.info(f"     stopReason=stop → 询问任务完成情况")
                self.send_task_inquiry_message(group_id, agent_name, task_desc)
                self.clear_activation_attempt(group_id, agent_name)
                handled += 1
                continue

            # ===== 步骤2b：检查会话活动是否超时 =====
            last_activity = self.get_last_assistant_message_time(latest_file)

            if last_activity:
                time_diff = (datetime.now(last_activity.tzinfo) - last_activity).total_seconds() / 60
            else:
                mtime = latest_file.stat().st_mtime
                time_diff = (datetime.now() - datetime.fromtimestamp(mtime)).total_seconds() / 60

            is_session_timeout = time_diff > SESSION_TIMEOUT_MINUTES

            logger.info(f"\n  📋 阻塞任务: {group_name}-{agent_name}: {task_desc}")
            logger.info(f"     原因: {reason}")
            logger.info(f"     会话活动: {int(time_diff)}分钟前 {'⚠️ 超时' if is_session_timeout else '✅ 活跃'}")

            if not is_session_timeout:
                # 步骤3：会话未超时，检查是否激活生效了
                last_activity_str = last_activity.isoformat() if last_activity else None
                saved_activity = self.get_activation_last_activity(group_id, agent_name)
                if saved_activity and last_activity_str and last_activity_str != saved_activity:
                    # 会话活动时间变了 → 激活生效，重置计数
                    logger.info(f"     ✅ 会话已恢复活动（活动时间已更新），重置激活计数")
                    self.clear_activation_attempt(group_id, agent_name)
                else:
                    logger.info(f"     → 会话活跃，不激活")
                continue

            # ===== 步骤3：会话确实超时，检查激活是否生效 =====
            attempts = self.get_activation_attempts(group_id, agent_name)
            current_activity_str = last_activity.isoformat() if last_activity else None
            saved_activity = self.get_activation_last_activity(group_id, agent_name)

            # 如果有激活记录且会话活动时间已变化 → 激活生效，重置计数重新开始
            if attempts > 0 and saved_activity and current_activity_str and current_activity_str != saved_activity:
                logger.info(f"     ✅ 激活生效（会话活动已更新），重置激活计数")
                attempts = 0

            if attempts >= 2:
                logger.info(f"     🔄 已激活{attempts}次仍无响应，重置会话")
                if self.reset_agent_session(str(latest_file), agent_name):
                    self.clear_activation_attempt(group_id, agent_name)
                    logger.info(f"     ✅ 会话已重置")
            else:
                logger.info(f"     📢 发送激活消息 (第{attempts+1}次)")
                if self.send_activation_message(group_id, agent_name):
                    # 记录激活次数和当前会话活动时间（下次检查时对比是否变化）
                    current_activity = last_activity.isoformat() if last_activity else ""
                    self.set_activation_attempt(group_id, agent_name, attempts + 1, current_activity)

            handled += 1

        if handled == 0:
            logger.info("  ✅ 所有阻塞任务的agent会话均活跃，无需激活")

    def check_stopped_sessions(self, all_session_status: Dict[str, Dict], analysis: Dict):
        """独立检查所有agent会话是否异常停止，不依赖AI的blocking_tasks

        检查条件：
        1. AI分析中该agent有任务且状态为"处理中"
        2. 会话stopReason为stop/error/aborted

        满足条件则立即发送询问消息
        """
        handled = 0

        # 从AI分析中获取所有"处理中"的任务
        active_tasks = {}  # (group_id, agent_name) -> task_desc
        if analysis:
            for task in analysis.get("tasks", []):
                status = task.get("status", "")
                if status in ("处理中", "超时"):
                    # 需要从group_name反查group_id
                    group_name = task.get("group", "")
                    agent_name = task.get("agent", "")
                    task_desc = task.get("task", "")

                    group_id = None
                    for gid, gcfg in GROUPS.items():
                        if gcfg.get("name") == group_name:
                            group_id = gid
                            break

                    if group_id and agent_name:
                        active_tasks[(group_id, agent_name)] = task_desc

        if not active_tasks:
            logger.info("  ✅ 无处理中的任务")
            return

        # 检查每个有活跃任务的agent的会话状态
        for group_id, agent_name in active_tasks:
            task_desc = active_tasks[(group_id, agent_name)]
            group_name = GROUPS.get(group_id, {}).get("name", group_id)

            # 从session_status中查找该agent
            session_status = all_session_status.get(group_id, {})
            agent_status = None
            for a in session_status.get("agents", []):
                if a.get("name") == agent_name:
                    agent_status = a
                    break

            if not agent_status:
                continue

            stop_reason = agent_status.get("stop_reason")

            # 检查stopReason是否为异常值
            if stop_reason in ("stop", "aborted", "error"):
                logger.info(f"\n  📋 会话异常停止: {group_name}-{agent_name}")
                logger.info(f"     任务: {task_desc[:50]}...")
                logger.info(f"     stopReason={stop_reason} → 发送询问消息")

                if stop_reason == "stop":
                    self.send_task_inquiry_message(group_id, agent_name, task_desc)
                else:
                    # aborted/error 发送激活消息
                    self.send_activation_message(group_id, agent_name)

                self.clear_activation_attempt(group_id, agent_name)
                handled += 1

        if handled == 0:
            logger.info("  ✅ 所有处理中任务的会话状态正常")

    def analyze_with_claude(self, all_group_messages: Dict[str, List[GroupMessage]],
                           all_session_status: Dict[str, Dict],
                           notification_history: Dict,
                           scheduling_plan: Dict = None) -> tuple:
        """综合所有群消息，一次AI调用做出全局决策。
        返回: (decisions: List[SchedulingDecision], analysis: Dict, updated_plan: Dict)
        """
        """综合所有群消息，一次AI调用做出全局决策"""

        # 构建所有群的消息摘要
        groups_summary = ""
        for group_id, messages in all_group_messages.items():
            group_name = GROUPS.get(group_id, {}).get("name", group_id)
            if not messages:
                groups_summary += f"\n### {group_name} ({group_id})\n（无消息）\n"
                continue
            groups_summary += f"\n### {group_name} ({group_id})\n"
            for m in messages:
                ts = datetime.fromtimestamp(m.timestamp).strftime('%H:%M')
                groups_summary += f"- [{ts}] {m.sender}: {m.content[:200]}\n"

        # 构建会话超时信息
        session_info = ""
        agent_session_messages = ""  # Agent会话中最后的assistant消息
        for group_id, status in all_session_status.items():
            group_name = GROUPS.get(group_id, {}).get("name", group_id)
            # 包含超时和异常停止的agent
            notable_agents = [a for a in status.get("agents", [])
                             if a.get("is_timeout") or a.get("stop_reason") in ("stop", "aborted", "error")]
            if notable_agents:
                session_info += f"\n**{group_name}**:\n"
                for agent in notable_agents:
                    role = "执行者" if agent["name"] in AGENT_ROLES.get("executors", []) else "顾问"
                    info = f"  - {agent['name']} ({role}): {agent['minutes_ago']}分钟无响应"
                    sr = agent.get("stop_reason")
                    if sr in ("stop", "aborted", "error"):
                        info += f", 会话已停止(stopReason={sr})"
                    session_info += info + "\n"

            # 收集所有有会话的agent的最后assistant消息
            for agent in status.get("agents", []):
                last_msgs = agent.get("last_assistant_messages", [])
                if last_msgs:
                    agent_session_messages += f"\n**{group_name} - {agent['name']}** (最近{len(last_msgs)}条assistant消息):\n"
                    for i, msg in enumerate(last_msgs, 1):
                        sr = msg.get("stopReason", "")
                        em = msg.get("errorMessage", "")
                        content = msg.get("content", "(空)")
                        agent_session_messages += f"  [{i}] stopReason={sr}"
                        if em:
                            agent_session_messages += f", error={em}"
                        agent_session_messages += f"\n  内容: {content}\n"

        # 构建通知历史
        history_str = ""
        if notification_history.get("history"):
            history_str = "### 最近通知记录:\n"
            for h in notification_history["history"][-10:]:
                issues_str = ", ".join(h.get("extracted_issues", []))[:100]
                history_str += (
                    f"- [{h.get('timestamp', '')[-8:]}] "
                    f"{h.get('source_group', '')} → {h.get('target_group_name', '')} "
                    f"@{','.join(h.get('mention_users', []))}: "
                    f"问题=[{issues_str}] "
                    f"内容={h.get('message_content', '')[:80]}\n"
                )

        # 构建调度计划信息
        plan_str = ""
        if scheduling_plan and scheduling_plan.get("milestones"):
            plan_str = f"### 当前调度计划 (版本: {scheduling_plan.get('current_version', '未知')})\n"
            plan_str += f"整体状态: {scheduling_plan.get('overall_status', '未知')}\n"
            plan_str += "里程碑:\n"
            for m in scheduling_plan.get("milestones", []):
                status_icon = {"completed": "✅", "in_progress": "🔄", "blocked": "🚧", "pending": "⏳"}.get(m.get("status", ""), "•")
                plan_str += f"  {status_icon} {m.get('id', '?')}. {m.get('name', '?')} [{m.get('status', '?')}]: {m.get('progress', '-')}\n"
            if scheduling_plan.get("next_actions"):
                plan_str += "下一步行动:\n"
                for a in scheduling_plan["next_actions"]:
                    plan_str += f"  → {a}\n"
        else:
            plan_str = "暂无调度计划（首次运行或计划已清空）"

        user_prompt = f"""## 所有工作群最新消息
{groups_summary}

## Agent会话内部消息（assistant最后2条，用于判断agent真实工作状态）
{agent_session_messages if agent_session_messages else "无会话消息"}

## Agent会话超时状态
{session_info if session_info else "无超时"}

## 调度计划
{plan_str}

{history_str}

请综合分析以上所有群的消息，梳理项目整体进度和卡点问题，做出跨群调度决策。
如果没有需要跨群协调的事项，decisions返回空数组 []。
注意：raw_messages字段不需要填写，系统会自动从API获取。
必须输出analysis部分，包含项目进度、各群当前任务、卡点问题。
必须输出updated_plan部分，更新调度计划状态。"""

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
                    "max_tokens": 3000
                },
                timeout=90
            )
            resp.raise_for_status()
            data = resp.json()

            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            logger.debug(f"AI返回内容: {content[:300]}")

            # 解析JSON - 新格式: {analysis: {...}, decisions: [...]}
            import re

            json_match = re.search(r'\{[\s\S]*\}', content)
            if not json_match:
                json_match = re.search(r'\{[\s\S]*', content)

            if not json_match:
                logger.info("  AI未返回有效JSON，无需通知")
                return [], {}, {}

            json_str = json_match.group()
            if not json_str.rstrip().endswith('}'):
                for i in range(len(json_str) - 1, -1, -1):
                    if json_str[i] in ['"', ']']:
                        json_str = json_str[:i+1] + '}'
                        break

            try:
                parsed = json.loads(json_str)
            except json.JSONDecodeError:
                logger.error(f"JSON解析失败: {content[:300]}")
                return [], {}, {}

            # 提取分析报告
            analysis = parsed.get("analysis", {})

            # 提取更新后的调度计划
            updated_plan = parsed.get("updated_plan", {})

            # 提取决策列表
            decisions_data = parsed.get("decisions", [])
            if not isinstance(decisions_data, list):
                decisions_data = [decisions_data] if decisions_data else []

            # 转换为SchedulingDecision列表
            results = []
            for d in decisions_data:
                if d.get("action") in ("ignore", "wait"):
                    continue
                results.append(SchedulingDecision(
                    action=d.get("action", "notify"),
                    target_group=d.get("target_group", ""),
                    target_group_name=d.get("target_group_name", ""),
                    mention_users=d.get("mention_users", []),
                    extracted_issues=d.get("extracted_issues", []),
                    message_content=d.get("message_content", ""),
                    reasoning=d.get("reasoning", ""),
                    source_group=d.get("source_group", ""),
                    raw_messages="",
                    qa_raw_messages="",
                    bug_doc_complete=d.get("bug_doc_complete", True)
                ))
            return results, analysis, updated_plan

        except Exception as e:
            logger.error(f"综合分析失败: {e}")
            return [], {}, {}

    def review_raw_message_with_ai(self, raw_content: str, decision: SchedulingDecision) -> str:
        """让AI审核并处理转发的原始消息，确保内容准确无歧义"""
        if not raw_content:
            return raw_content

        # 如果消息很短且是简单通知（如版本闭环），不需要AI审核
        if len(raw_content) < 100 and not decision.extracted_issues:
            return raw_content

        review_prompt = f"""你是项目消息审核员。请审核以下要转发给其他群的消息内容，确保：
1. 内容准确，不含过时信息（已解决的问题不要描述为未解决）
2. 不含歧义或模糊表述
3. 不含混乱的混合内容（旧的失败通知混杂新的修复确认）
4. 保留关键细节（API路径、错误码、账号密码、操作步骤等）
5. 如果原始内容混乱，整理成清晰的结构

## 通知目标
- 目标群: {decision.target_group_name}
- @对象: {', '.join(decision.mention_users)}
- 通知意图: {decision.message_content}

## 待审核的原始消息内容:
{raw_content}

请直接输出审核后的消息内容（不需要解释审核过程）。如果内容合理无需修改，直接原样返回。如果需要修改，输出修改后的版本。"""

        try:
            resp = requests.post(
                f"{LITELLM_URL}/v1/chat/completions",
                headers=self.llm_headers,
                json={
                    "model": LITELLM_MODEL,
                    "messages": [{"role": "user", "content": review_prompt}],
                    "temperature": 0.2,
                    "max_tokens": 2000
                },
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            reviewed = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

            if reviewed:
                logger.info(f"  🔍 AI审核消息完成 (原始{len(raw_content)}字 → 审核{len(reviewed)}字)")
                return reviewed
            else:
                logger.warning("  ⚠️ AI审核返回为空，使用原始消息")
                return raw_content
        except Exception as e:
            logger.warning(f"  ⚠️ AI审核失败: {e}，使用原始消息")
            return raw_content

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
        """发送Mattermost通知（AI已做决策，代码只负责发送）"""

        # AI判断BUG文档不完整 → 通知QA补充
        if not decision.bug_doc_complete and decision.extracted_issues:
            logger.warning("⚠️ AI判定BUG文档不完整，通知QA重新生成")
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

            # 验收问题生成文档
            doc_path = None
            if decision.extracted_issues:
                doc_path = self.generate_bug_document(decision)

            mentions = " ".join([f"@{u.lstrip('@')}" for u in decision.mention_users])

            # 获取原始消息内容
            raw_content = decision.agent_raw_message or decision.raw_messages or decision.qa_raw_messages

            # 构建消息
            if doc_path:
                key_issues = decision.extracted_issues[:3]
                summary = "\n".join([f"- {i[:100]}..." if len(i) > 100 else f"- {i}" for i in key_issues])
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
        """执行调度 - 先收集所有群消息，再综合分析决策"""
        logger.info("=" * 70)
        logger.info(f"🕐 Claude驱动调度开始 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 70)

        # ========== 第一步：收集所有群消息和会话状态 ==========
        all_group_messages = {}   # group_id -> [GroupMessage]
        all_session_status = {}   # group_id -> session_status

        for group_id, group_config in GROUPS.items():
            group_name = group_config["name"]
            messages = self.get_group_messages(group_config["channel_id"])
            all_group_messages[group_id] = messages

            session_status = self.check_agent_session_status(group_id)
            all_session_status[group_id] = session_status

            timeout_agents = [a for a in session_status.get("agents", []) if a.get("is_timeout")]
            logger.info(f"  {group_name}: {len(messages)}条消息"
                        + (f", ⚠️ {len(timeout_agents)}个超时" if timeout_agents else ""))

        total_msgs = sum(len(m) for m in all_group_messages.values())
        logger.info(f"\n📊 消息收集完成: {total_msgs}条消息, {len(GROUPS)}个群")

        # ========== 第二步：综合分析，一次AI调用 ==========
        logger.info(f"\n🧠 综合分析所有群消息...")
        decisions, analysis, updated_plan = self.analyze_with_claude(
            all_group_messages, all_session_status, self.notification_history,
            self.scheduling_plan
        )

        # 输出分析报告
        if analysis:
            logger.info(f"\n{'='*60}")
            logger.info(f"📊 跨群分析报告")
            logger.info(f"{'='*60}")
            if analysis.get("current_version"):
                logger.info(f"  当前版本: {analysis['current_version']}")
            if analysis.get("overall_progress"):
                logger.info(f"  整体进度: {analysis['overall_progress']}")

            # 输出各群Agent任务状态
            tasks = analysis.get("tasks", [])
            if tasks:
                logger.info(f"\n  📋 当前任务 (工作群-Agent-任务):")
                for t in tasks:
                    group = t.get("group", "?")
                    agent = t.get("agent", "?")
                    task = t.get("task", "?")
                    status = t.get("status", "?")
                    status_icon = {"处理中": "🔄", "已完成": "✅", "等待中": "⏳", "超时": "⚠️"}.get(status, "•")
                    logger.info(f"    {status_icon} {group} - {agent}: {task} [{status}]")

            # 输出卡点问题
            blockers = analysis.get("blockers", [])
            if blockers:
                logger.info(f"\n  🚧 卡点问题:")
                for b in blockers:
                    logger.info(f"    - {b}")

            # 输出版本状态
            vs = analysis.get("version_status", {})
            if vs:
                logger.info(f"\n  📦 版本状态:")
                for key, val in vs.items():
                    icon = "✅" if val else "❌"
                    logger.info(f"    {icon} {key}: {val}")

            logger.info(f"{'='*60}")

        # ========== 更新并保存调度计划 ==========
        if updated_plan:
            self.scheduling_plan = updated_plan
            self._save_scheduling_plan()
            # 输出计划状态
            logger.info(f"\n  📋 调度计划更新:")
            logger.info(f"    版本: {updated_plan.get('current_version', '未知')}")
            logger.info(f"    状态: {updated_plan.get('overall_status', '未知')}")
            for m in updated_plan.get("milestones", []):
                status_icon = {"completed": "✅", "in_progress": "🔄", "blocked": "🚧", "pending": "⏳"}.get(m.get("status", ""), "•")
                logger.info(f"    {status_icon} {m.get('id', '?')}. {m.get('name', '?')} - {m.get('progress', '-')}")
            for a in updated_plan.get("next_actions", []):
                logger.info(f"    → {a}")

        # ========== 第二步半：处理阻塞任务（AI识别 → 脚本验证超时 → 激活） ==========
        blocking_tasks = analysis.get("blocking_tasks", []) if analysis else []
        logger.info(f"\n🔍 处理阻塞任务...")
        self.handle_timeout_agents(blocking_tasks)

        # ========== 第二步半b：独立检查会话异常停止 ==========
        # 不依赖AI的blocking_tasks，直接扫描所有有任务且会话异常的agent
        logger.info(f"\n🔍 检查会话异常停止...")
        self.check_stopped_sessions(all_session_status, analysis)

        if not decisions:
            logger.info("  ✅ 无需跨群通知")
            logger.info(f"\n🏁 调度结束 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            return

        # ========== 第三步：执行通知 ==========
        notifications_sent = 0
        notified_groups = set()

        for decision in decisions:
            logger.info(f"\n📋 调度决策:")
            logger.info(f"  目标: {decision.target_group_name}")
            logger.info(f"  @对象: {', '.join(decision.mention_users)}")
            logger.info(f"  问题: {decision.extracted_issues}")
            logger.info(f"  理由: {decision.reasoning[:150]}...")

            if decision.target_group in notified_groups:
                logger.info(f"  ⏭ 跳过 {decision.target_group_name} - 已在本轮通知过")
                continue

            # 获取来源群原始消息
            source_channel_id = GROUPS.get(decision.source_group, {}).get("channel_id", "")
            if source_channel_id:
                raw_msg = ""
                if decision.mention_users:
                    first_agent = decision.mention_users[0].lstrip('@')
                    raw_msg = self.get_agent_last_raw_message(source_channel_id, first_agent)
                    if raw_msg:
                        logger.info(f"  📥 获取 {first_agent} 的原始消息 ({len(raw_msg)}字符)")

                if not raw_msg:
                    raw_msg = self.get_source_group_raw_messages(source_channel_id, limit=5)
                    if raw_msg:
                        logger.info(f"  📥 获取来源群最近消息 ({len(raw_msg)}字符)")

                if raw_msg:
                    decision.agent_raw_message = raw_msg

            # AI审核转发消息
            raw_for_review = decision.agent_raw_message or decision.raw_messages or decision.qa_raw_messages
            if raw_for_review:
                reviewed = self.review_raw_message_with_ai(raw_for_review, decision)
                decision.agent_raw_message = reviewed

            # 发送通知
            if self.send_mattermost_notification(decision):
                self.send_feishu_notification(decision)
                notifications_sent += 1
                notified_groups.add(decision.target_group)

                # 记录历史
                history = self.notification_history.setdefault("history", [])
                history.append({
                    "timestamp": datetime.now().isoformat(),
                    "source_group": decision.source_group,
                    "target_group": decision.target_group,
                    "target_group_name": decision.target_group_name,
                    "mention_users": decision.mention_users,
                    "extracted_issues": decision.extracted_issues[:5],
                    "message_content": decision.message_content[:200],
                    "reason": decision.reasoning[:100]
                })
                if len(history) > 20:
                    self.notification_history["history"] = history[-20:]
                self._save_notification_history()
                logger.info(f"  📝 已记录通知历史")

        logger.info(f"\n{'=' * 70}")
        logger.info(f"📊 调度总结:")
        logger.info(f"  收集消息: {total_msgs}条 ({len(GROUPS)}个群)")
        logger.info(f"  调度决策: {len(decisions)}个")
        logger.info(f"  发送通知: {notifications_sent}个")
        logger.info("=" * 70)
        logger.info(f"🏁 调度结束 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def main():
    """主入口"""
    scheduler = ClaudeDrivenScheduler()
    scheduler.run()


if __name__ == "__main__":
    main()
