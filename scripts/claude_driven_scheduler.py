#!/usr/bin/env python3
"""
Claude驱动的智能调度系统

核心逻辑：Claude Agent完整处理整个调度流程
1. 读取群消息，提炼具体问题
2. 分析问题，决策通知策略
3. 生成具体通知内容
4. 执行跨群通知（Mattermost + 飞书）
"""

import os
import sys
import json
import time
import logging
import requests
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass

# 配置
WORKSPACE_ROOT = Path("/home/gongdewei/.openclaw/workspace-main")
LOG_DIR = WORKSPACE_ROOT / "logs" / "intelligent_scheduling"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 通知频率限制配置
RATE_LIMIT_HOURS = 1  # 同一群1小时内最多通知1次
NOTIFICATION_HISTORY_FILE = LOG_DIR / "notification_history.json"

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

# Mattermost配置
MM_URL = "http://localhost:8066"
MM_TOKEN = "owwch961rjyn9ctj3qth7j9gra"

# 工作群配置
GROUPS = {
    "dev-working-group": {"channel_id": "9fzie6aawjgnfk6dyohf89p1wh", "name": "开发工作群"},
    "ops-release-group": {"channel_id": "sqd4znntifd47pper67qe7fzua", "name": "运维发布群"},
    "qa-acceptance-group": {"channel_id": "3wo4cnz1ypgbxffdn8kqz35jpy", "name": "验收测试群"},
    "plan-design-group": {"channel_id": "c7uuyhk5mjye8f5yhawb8mneoy", "name": "规划设计群"}
}

# LiteLLM配置
LITELLM_URL = "http://localhost:4000"
LITELLM_MODEL = "glm-5-pool"
LITELLM_API_KEY = "sk-litellm-openclaw-2026-03-15"

# 飞书配置
FEISHU_ACCOUNTS = {
    "default": {
        "app_id": "cli_a92ea4f3d2385cb0",
        "app_secret": "CpfF6hVz5xR7L7nHEydjKhFBddIH4WOg"
    }
}
FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
FEISHU_CHAT_ID = "oc_67b5713a4f8b75a2d766ae110f11694f"  # 飞书群ID


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

    SYSTEM_PROMPT = """你是一个智能团队协作调度Agent。你的职责是：
1. 分析工作群消息，提炼具体的阻塞问题和待办事项
2. 根据问题性质和上下文，智能决策是否通知以及通知谁
3. 避免重复通知和无效打扰
4. 生成包含具体问题详情的通知消息

## 你会收到的完整上下文
1. **当前分析群组的消息**: 提出问题或请求的群组（最近10-20条）
2. **可能目标群的最新状态**: 可能被通知的群组（最近3-5条）
3. **历史通知记录**: 之前通知过的时间、次数和内容
4. **通知群组职责和成员信息**

## 智能决策规则（由你判断，不是硬编码）

### 0. 【新增-最高优先级】执行者会话超时检查**
**场景**: 当执行者（如fullstack-dev）会话超时10分钟未响应时 而 **架构师** (如architect)虽然响应， 但无法代替执行者工作
**行动**: **立即通知** 不要等待
**判断标准**:
- 上下文中包含"⚠️ Agent会话超时警告"
- **关键**: 只有"执行者"超时才算严重阻塞
  - fullstack-dev（执行者）超时 → 严重阻塞 → 立即通知
  - architect（架构师）超时 → 轻微 → 可以等待
  - ops（运维）超时 → 严重阻塞 → 立即通知
- **通知内容**: "执行者XX分钟无响应， 请立即处理[问题详情]"
**例子**:
- 验收群阻塞 + fullstack-dev超时28分钟 → **立即通知开发群**执行者超时， 请处理P0问题"
- 架构师已响应 + fullstack-dev超时 → **仍然通知**（架构师≠执行者）

**不要**:
- "architect已响应" → 返回wait ❌ **错误**
- 只有执行者响应才能算问题在处理

### 1. 【关键】问题解决通知（高优先级）
**场景**: 当责任群（如运维/开发）已经解决问题，但请求群（如验收）还在重复反馈阻塞时
**行动**: 必须通知请求群"问题已解决，可以继续"
**例子**:
- 运维群16:10确认"问题已解决✅"
- 验收群16:31还在反馈"第4次阻塞"
- → 应该通知验收群"问题已解决，请继续验收"
**判断标准**:
- 目标群（责任群）最近消息有"已解决"/"已修复"/"完成"/"✅"
- 源群（请求群）还在反馈阻塞/等待
- → 返回 `notify`，通知源群

### 2. 请求协助通知
**场景**: 当请求群（如验收）有阻塞问题，且责任群（如运维）未响应时
**行动**: 通知责任群来处理
**判断标准**:
- 源群有阻塞问题需要跨群协作
- 目标群（责任群）未响应或未确认处理
- → 返回 `notify`，通知责任群

### 3. 等待（wait）
**场景**:
- 目标群正在处理中（有响应但未完成）
- 最近10分钟内通知过相同目标群
且没有新情况
→ 返回 `wait`

### 4. 忽略（ignore）
**场景**:
- **验收通过**: 问题真正解决，源群确认可以继续
- 消息是历史记录（超过4小时）且问题已闭环
→ 返回 `ignore`

**注意**: "验收未通过" ≠ 问题闭环！"验收未通过"意味着发现了新问题（缺陷、阻塞），需要通知责任群处理。
**例子**:
- ❌ 错误: "验收未通过，发现P0功能缺失" → 这不是闭环，是新阻塞！
- ✅ 正确: "验收通过,所有功能正常" → 这才是闭环

## 工作群职责分工与成员（重要：只能@本群成员）
### dev-working-group (开发工作群)
- 职责: 功能开发、代码实现、bug修复
- **成员**: fullstack-dev, architect
- 只能@: @fullstack-dev, @architect

### ops-release-group (运维发布群)
- 职责: 环境部署、容器管理、数据库、基础设施
- **成员**: ops, architect
- 只能@: @ops, @architect

### qa-acceptance-group (验收测试群)
- 职责: 功能验收、测试报告
- **成员**: qa, product
- 只能@: @qa, @product

### plan-design-group (规划设计群)
- 职责: 需求设计、UI设计、产品规划
- **成员**: product, ui-designer, architect, qa
- 只能@: @product, @ui-designer, @architect, @qa

## 协作流程（多群串联）
验收发现阻塞 → 分析问题类型 → 通知责任群处理 → 处理完成后通知验收群继续

### 问题类型与责任群映射:
- 环境问题（Docker/容器/服务器）→ ops-release-group (@ops)
- 数据库问题（Schema/连接/查询）→ ops-release-group (@ops)
- 代码Bug → dev-working-group (@fullstack-dev)
- API问题 → dev-working-group (@fullstack-dev @architect)
- 纯验收问题 → qa-acceptance-group (@qa @product)

### @对象规则（重要）:
1. 只能@目标群的成员
2. 例如: 通知dev-working-group时，只能@fullstack-dev和@architect
3. 例如: 通知ops-release-group时，只能@ops和@architect
4. 不能@非本群成员

## 输出格式
请以JSON格式输出，包含以下字段：
{
    "action": "notify|wait|ignore",
    "target_group": "群组ID（只在notify时需要）",
    "target_group_name": "群组名称（只在notify时需要）",
    "mention_users": ["本群成员1", "本群成员2"],
    "message_content": "通知内容（只在notify时需要）",
    "reasoning": "决策理由（必须说明：为什么选择notify/wait/ignore，特别是针对重复通知和已解决状态的分析）",
    "extracted_issues": ["提炼的具体问题1", "提炼的具体问题2"]
}

## 重要规则
1. 【最高优先级】如果责任群已解决问题，但请求群还在阻塞反馈 → 必须通知请求群
2. 只有真正需要跨群协作时才生成notify决策
3. 如果群里已经在正常处理，返回wait
4. 如果问题已解决且请求群不再反馈，返回ignore
5. mention_users只能包含目标群的成员
6. 通知内容必须具体：
   - **问题解决通知**: "问题已解决，请继续验收" + 解决摘要
   - **请求协助通知**: 具体问题 + 来源 + 需要做什么
7. 不要包含外部链接，直接在消息中说明问题详情
"""

    def __init__(self):
        self.headers = {"Authorization": f"Bearer {MM_TOKEN}"}
        self.llm_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LITELLM_API_KEY}"
        }
        self.notification_history = self._load_notification_history()
        logger.info("Claude驱动调度器初始化完成")

    def _load_notification_history(self) -> Dict:
        """加载通知历史"""
        try:
            if NOTIFICATION_HISTORY_FILE.exists():
                with open(NOTIFICATION_HISTORY_FILE, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"加载通知历史失败: {e}")
        return {}

    def _save_notification_history(self):
        """保存通知历史"""
        try:
            with open(NOTIFICATION_HISTORY_FILE, 'w') as f:
                json.dump(self.notification_history, f, indent=2)
        except Exception as e:
            logger.error(f"保存通知历史失败: {e}")

    def _get_notification_context(self, target_group: str) -> Dict:
        """获取通知上下文（供Claude判断，不做硬编码决策）
        
        返回历史记录，让Claude自己判断是否应该通知
        """
        history = self.notification_history.get(target_group, {})
        if isinstance(history, dict):
            return {
                "last_time": history.get("time", 0),
                "last_issues": history.get("issues", []),
                "times_notified": history.get("times_notified", 1)
            }
        return {"last_time": 0, "last_issues": [], "times_notified": 0}

    def _check_issue_resolved(self, group_id: str, messages: List) -> bool:
        """检查问题是否已解决（目标群反馈）"""
        resolve_keywords = ["问题已解决", "已解决", "已修复", "完成", "✅", "处理完毕"]
        for msg in messages[-5:]:  # 检查最近5条消息
            message_text = getattr(msg, 'content', '') or str(msg)
            if any(kw in message_text for kw in resolve_keywords):
                logger.info(f"✅ 检测到 {group_id} 反馈问题已解决")
                return True
        return False

    def _clear_notification_history(self, target_group: str):
        """清除指定群的通知历史（问题已解决时）"""
        if target_group in self.notification_history:
            del self.notification_history[target_group]
            self._save_notification_history()
            logger.info(f"🗑️ 已清除 {target_group} 的通知历史")

    def check_agent_session_status(self, group_id: str) -> Dict:
        """检查群的agent会话是否超时或无响应

        Args:
            group_id: 群组ID（如dev-working-group）

        Returns:
            Dict: {
                "has_session": bool,  # 是否有活跃会话
                "is_timeout": bool,    # 是否有超时的agent
                "agents": list         # agent状态列表
            }
        """
        # 群组到agent的映射
        GROUP_AGENT_MAP = {
            "dev-working-group": ["fullstack-dev", "architect"],
            "ops-release-group": ["ops", "architect"],
            "qa-acceptance-group": ["qa", "product"],
            "plan-design-group": ["product", "ui-designer", "architect", "qa"]
        }

        agents = GROUP_AGENT_MAP.get(group_id, [])
        if not agents:
            return {"has_session": False, "is_timeout": False, "agents": []}

        result = {
            "has_session": False,
            "agents": [],
            "is_timeout": False
        }

        # Agent目录基路径
        AGENTS_BASE = Path("/home/gongdewei/.openclaw/agents")

        # 检查每个agent的会话状态
        for agent_name in agents:
            try:
                # 查找agent的会话目录
                agent_session_dir = AGENTS_BASE / agent_name / "sessions"

                if not agent_session_dir.exists():
                    continue

                # 找到最近的会话文件
                session_files = list(agent_session_dir.glob("*.jsonl")) + \
                               list(agent_session_dir.glob("*.jsonl.backup.*"))

                if not session_files:
                    continue

                # 获取最新文件
                latest_file = max(session_files, key=lambda x: x.stat().st_mtime)
                mtime = latest_file.stat().st_mtime
                last_activity = datetime.fromtimestamp(mtime)
                time_diff = (datetime.now() - last_activity).total_seconds() / 60  # 分钟

                is_timeout = time_diff > 10  # 10分钟无响应视为超时

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

            except Exception as e:
                logger.error(f"检查会话文件失败 {agent_name}: {e}")

        return result

    def _record_notification(self, target_group: str, extracted_issues: List[str] = None):
        """记录通知时间和问题列表"""
        self.notification_history[target_group] = {
            "time": time.time(),
            "issues": extracted_issues or []
        }
        self._save_notification_history()

    def fetch_group_messages(self, group_id: str, limit: int = 20) -> List[GroupMessage]:
        """获取群消息"""
        messages = []
        try:
            resp = requests.get(
                f"{MM_URL}/api/v4/channels/{group_id}/posts",
                headers=self.headers,
                params={"page": 0, "per_page": limit},
                timeout=10
            )

            if resp.status_code == 200:
                posts = resp.json().get("posts", {})
                for post_id, post in posts.items():
                    # 获取发送者信息
                    sender = "unknown"
                    user_id = post.get("user_id", "")
                    if user_id:
                        try:
                            user_resp = requests.get(
                                f"{MM_URL}/api/v4/users/{user_id}",
                                headers=self.headers,
                                timeout=5
                            )
                            if user_resp.status_code == 200:
                                sender = user_resp.json().get("username", "unknown")
                        except:
                            pass

                    messages.append(GroupMessage(
                        group_id=group_id,
                        group_name=GROUPS.get(group_id, {}).get("name", group_id),
                        sender=sender,
                        content=post.get("message", ""),
                        timestamp=post.get("create_at", 0) / 1000
                    ))
                # 按时间排序
                messages.sort(key=lambda x: x.timestamp, reverse=True)
        except Exception as e:
            logger.error(f"获取群消息失败 {group_id}: {e}")
        return messages

    def analyze_with_claude(self, messages: List[GroupMessage], context: str,
                           target_groups_status: Dict = None,
                           notification_history: Dict = None,
                           session_status: Dict = None) -> Optional[SchedulingDecision]:
        """使用Claude分析消息并生成决策（AI驱动版本）

        Args:
            messages: 当前群组的消息
            context: 基础上下文（群组名称等）
            target_groups_status: 可能目标群的最近状态 {group_id: [最近消息]}
            notification_history: 通知历史记录 {group_id: {time, issues, times_notified}}
            session_status: Agent会话状态 {has_session, is_timeout, agents: [...]}
        """
        if not messages:
            return None

        # 构建消息摘要
        msg_summary = []
        for msg in messages[:10]:  # 只处理最近10条
            time_str = datetime.fromtimestamp(msg.timestamp).strftime('%m-%d %H:%M')
            msg_summary.append(f"[{time_str}] {msg.sender}: {msg.content[:200]}")

        # 构建目标群状态摘要（供Claude判断）
        target_status_text = ""
        if target_groups_status:
            target_status_text = "\n## 可能目标群的最新状态\n"
            for group_id, msgs in target_groups_status.items():
                group_name = GROUPS.get(group_id, {}).get("name", group_id)
                target_status_text += f"\n### {group_name} ({group_id})\n"
                if msgs:
                    for msg in msgs[-3:]:  # 最近3条
                        time_str = datetime.fromtimestamp(msg.timestamp).strftime('%m-%d %H:%M')
                        target_status_text += f"[{time_str}] {msg.sender}: {msg.content[:100]}\n"
                else:
                    target_status_text += "（无最新消息）\n"

        # 构建通知历史摘要（供Claude判断是否重复）
        history_text = ""
        if notification_history:
            history_text = "\n## 历史通知记录\n"
            for group_id, history in notification_history.items():
                if isinstance(history, dict):
                    group_name = GROUPS.get(group_id, {}).get("name", group_id)
                    last_time = history.get("time", 0)
                    issues = history.get("issues", [])
                    times_notified = history.get("times_notified", 1)

                    if last_time > 0:
                        time_ago = int((time.time() - last_time) / 60)
                        history_text += f"- {group_name}: {time_ago}分钟前通知过，已通知{times_notified}次，问题: {', '.join(issues)}\n"

        prompt = f"""
## 上下文
{context}

## 当前群组最近消息（最后20条）
**注意**: 消息可能包含历史记录，请自行判断消息的时间、新鲜度和相关性。
- 优先关注最近的消息（1-2小时内）
- 忽略过时的消息（超过4小时的）
- 识别消息之间的因果关系和时间线

{chr(10).join(msg_summary)}

{target_status_text}

{history_text}

## 任务
请根据以上完整信息进行智能决策：

### 决策优先级（按顺序判断）:
**0. 【新增-会话超时检查】**:
   - 检查：上下文中是否包含"⚠️ Agent会话超时警告"
   - 如果有 → 说明对应的agent 10分钟未响应，可能需要重新通知
   - 判断：是否需要通知agent继续处理，或者等待自然恢复
   - **注意**: 只有在有实际阻塞问题且agent确实需要响应时才通知

**1. 【最高优先级】问题解决通知**:
   - 检查：责任群（如运维/开发）最近消息是否包含"已解决"/"已修复"/"完成"/"✅"
   - 检查：当前群（如验收）是否还在反馈"阻塞"/"等待"/"无法继续"
   - 如果都是 → **立即通知当前群"问题已解决，可以继续"**（action=notify）

**2. 请求协助通知**:
   - 检查：当前群有阻塞问题需要跨群协作
   - 检查：责任群未响应或未确认处理
   - 如果都是 → **通知责任群来处理**（action=notify）

**3. 等待**:
   - 责任群正在处理中（有响应但未完成）
   - 10分钟内通知过且无新情况
   → 返回wait

**4. 忽略**:
   - 问题已解决且当前群已知道（不再反馈阻塞）
   - 消息都是历史记录且问题已闭环
   → 返回ignore

### 【关键】验收结果判断（最重要！）
**验收通过** vs **验收未通过**:
- ✅ **验收通过**/验收完成/验收成功 = 问题真正解决 → 可能是ignore
- ❌ **验收未通过**/验收失败/发现缺陷/P0功能缺失 = **发现新问题** = **必须notify开发群**
  - "验收未通过"意味着: 发现了新的阻塞问题（缺陷、功能缺失）
  - 需要通知开发群来修复这些缺陷
  - **这是最高优先级的阻塞场景！**

- ⚠️ 不要把"验收未通过"误判为"问题已闭环"!
  - 验收未通过 ≠ 问题解决
  - 验收未通过 = 需要开发群处理新缺陷

### 判断标准示例
**场景1: 红灯（立即通知）**
- 验收群18:47提交"验收未通过，发现P0功能缺失" → **通知开发群**"验收未通过，需要实现P0功能"
- 运维群确认问题已解决 + 验收群还在反馈阻塞（不知道问题解决） → **通知验收群**"问题已解决，请继续验收"
- 验收群报告新阻塞 + 责任群未响应 → **通知责任群**"验收阻塞，需要处理"

**场景2: 黄灯（等待）**
- 责任群正在处理中（有响应但未完成）
- 10分钟内通知过且无新情况
→ 返回wait

**场景3: 绿灯（忽略）**
- **验收通过** + 所有问题解决 → ignore
- 问题已解决 + 当前群已知道（不再反馈阻塞) → ignore
- 消息都是历史记录且问题已真正闭环 → ignore

**返回格式**:
请以JSON格式返回，包含以下字段：
{{
    "action": "notify|wait|ignore",
    "target_group": "群组ID（仅notify时需要）",
    "target_group_name": "群组名称（仅notify时需要）",
    "mention_users": ["本群成员"],
    "message_content": "通知内容（仅notify时需要）",
    "extracted_issues": ["提炼的具体问题"],
    "reasoning": "决策理由（必须说明：消息时间线、问题状态、为什么做这个决策）"
}}
"""

        # 调用Claude/LiteLLM
        try:
            payload = {
                "model": LITELLM_MODEL,
                "messages": [
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.3,
                "max_tokens": 1500
            }
            resp = requests.post(
                f"{LITELLM_URL}/v1/chat/completions",
                headers=self.llm_headers,
                json=payload,
                timeout=30
            )
            if resp.status_code == 200:
                content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                logger.info(f"✅ Claude分析完成")
                return self._parse_decision(content)
            else:
                logger.error(f"Claude调用失败: HTTP {resp.status_code}")
                return None
        except Exception as e:
            logger.error(f"Claude分析失败: {e}")
            return None

    def _parse_decision(self, content: str) -> Optional[SchedulingDecision]:
        """解析Claude返回的决策"""
        try:
            # 提取JSON
            import re
            json_match = re.search(r'\{[\s\S]*\}', content)
            if not json_match:
                logger.warning("未找到JSON响应")
                return None
            data = json.loads(json_match.group(0))
            action = data.get("action", "wait")
            if action != "notify":
                logger.info(f"Claude决策: {action}, 理由: {data.get('reasoning', '')}")
                return None
            return SchedulingDecision(
                action=action,
                target_group=data.get("target_group", ""),
                target_group_name=data.get("target_group_name", ""),
                mention_users=data.get("mention_users", []),
                message_content=data.get("message_content", ""),
                reasoning=data.get("reasoning", ""),
                extracted_issues=data.get("extracted_issues", [])
            )
        except json.JSONDecodeError as e:
            logger.error(f"JSON解析失败: {e}")
            return None
        except Exception as e:
            logger.error(f"决策解析失败: {e}")
            return None
    def send_notification(self, decision: SchedulingDecision) -> bool:
        """发送通知到Mattermost和飞书"""
        target_config = GROUPS.get(decision.target_group)
        if not target_config:
            logger.error(f"未知的目标群组: {decision.target_group}")
            return False
        channel_id = target_config["channel_id"]
        # 构建Mattermost消息
        mentions = " ".join([f"@{u}" for u in decision.mention_users])
        issues_list = "\n".join([f"• {issue}" for issue in decision.extracted_issues])
        mm_message = f"""📢 跨群协作通知
{mentions}
{decision.message_content}
---
📋 提炼的问题:
{issues_list}
💡 决策理由: {decision.reasoning}
"""
        # 发送到Mattermost
        mm_success = False
        try:
            resp = requests.post(
                f"{MM_URL}/api/v4/posts",
                headers=self.headers,
                json={
                    "channel_id": channel_id,
                    "message": mm_message
                },
                timeout=10
            )
            if resp.status_code == 201:
                logger.info(f"✅ Mattermost通知发送成功 → {decision.target_group_name}")
                mm_success = True
            else:
                logger.error(f"Mattermost发送失败: HTTP {resp.status_code}")
        except Exception as e:
            logger.error(f"发送Mattermost通知失败: {e}")
        # 发送到飞书
        feishu_success = self.send_to_feishu(decision)
        if feishu_success:
            logger.info(f"✅ 飞书通知发送成功")
        else:
            logger.warning(f"⚠️ 飞书通知发送失败")

        # 记录通知历史（用于去重）
        if mm_success or feishu_success:
            self._record_notification(decision.target_group, decision.extracted_issues)
            logger.info(f"📝 已记录通知历史 → {decision.target_group}")

        return mm_success or feishu_success
    def get_feishu_token(self) -> Optional[str]:
        """获取飞书access token"""
        account = FEISHU_ACCOUNTS.get("default")
        if not account:
            return None
        url = f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal"
        try:
            resp = requests.post(
                url,
                json={
                    "app_id": account["app_id"],
                    "app_secret": account["app_secret"]
                },
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    return data.get("tenant_access_token")
            return None
        except Exception as e:
            logger.error(f"获取飞书token失败: {e}")
            return None
    def send_to_feishu(self, decision: SchedulingDecision) -> bool:
        """发送通知到飞书群"""
        token = self.get_feishu_token()
        if not token:
            logger.error("获取飞书token失败")
            return False
        # 构建飞书消息内容
        mentions = " ".join([f"<at user_id=\"{u}\">{u}</at>" for u in decision.mention_users])
        issues_text = "\n".join([f"• {issue}" for issue in decision.extracted_issues])
        # 飞书富文本格式
        content = {
            "zh_cn": {
                "title": "🤖 智能调度通知",
                "content": [
                    [{"tag": "text", "text": "📢 跨群协作通知", "style": ["bold"]}],
                    [{"tag": "text", "text": ""}],
                    [{"tag": "text", "text": f"📍 目标群组: {decision.target_group_name}"}],
                    [{"tag": "text", "text": f"👤 通知对象: {mentions}"}],
                    [{"tag": "text", "text": ""}],
                    [{"tag": "text", "text": "📋 问题详情:", "style": ["bold"]}],
                    [{"tag": "text", "text": decision.message_content}],
                    [{"tag": "text", "text": ""}],
                    [{"tag": "text", "text": "🔍 提炼的具体问题:", "style": ["bold"]}],
                    [{"tag": "text", "text": issues_text}],
                    [{"tag": "text", "text": ""}],
                    [{"tag": "text", "text": f"💡 决策理由: {decision.reasoning}"}],
                    [{"tag": "text", "text": ""}],
                    [{"tag": "text", "text": f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"}]
                ]
            }
        }
        try:
            resp = requests.post(
                f"{FEISHU_API_BASE}/im/v1/messages",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                },
                params={"receive_id_type": "chat_id"},
                json={
                    "receive_id": FEISHU_CHAT_ID,
                    "msg_type": "post",
                    "content": json.dumps(content)
                },
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    return True
                else:
                    logger.error(f"飞书API错误: {data.get('msg')}")
                    return False
            else:
                logger.error(f"飞书HTTP错误: {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"发送飞书消息失败: {e}")
            return False
    def run(self):
        """运行调度"""
        logger.info("=" * 70)
        logger.info(f"🕐 Claude驱动调度开始 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 70)
        decisions_made = []
        notifications_sent = 0
        # 1. 获取所有群的消息
        logger.info("📥 获取工作群消息...")
        all_messages = {}
        for group_id, config in GROUPS.items():
            messages = self.fetch_group_messages(config["channel_id"])
            all_messages[group_id] = messages
            logger.info(f"  {config['name']}: {len(messages)}条消息")
        # 2. 分析需要关注的群（验收群优先）
        priority_groups = ["qa-acceptance-group", "dev-working-group"]

        # 🎯 跟踪本次调度已通知的群（防止重复通知）
        notified_in_this_round = set()

        for group_id in priority_groups:
            config = GROUPS.get(group_id, {})
            messages = all_messages.get(group_id, [])
            if not messages:
                continue

            # 🚫 防止重复通知: 如果当前群刚在本次调度中被通知过，跳过
            if group_id in notified_in_this_round:
                logger.info(f"\n⏭ 跳过 {config.get('name', group_id)} - 刚在本次调度中被通知")
                continue

            logger.info(f"\n🧠 分析 {config.get('name', group_id)}...")
            logger.info(f"  消息数: {len(messages)}条（不去时间过滤，让Claude判断）")

            # 🎯 新增：检查agent会话状态（防止会话超时）
            session_status = self.check_agent_session_status(group_id)
            if session_status["has_session"]:
                timeout_agents = [a for a in session_status["agents"] if a["is_timeout"]]
                if timeout_agents:
                    logger.info(f"  ⚠️ 检测到 {len(timeout_agents)} 个agent会话超时:")
                    for agent in timeout_agents:
                        logger.info(f"    - {agent['name']}: {agent['minutes_ago']}分钟无响应")

            # 🎯 AI驱动修复：不去时间过滤，让Claude自己判断消息相关性
            # 1. 确定可能的目标群
            if group_id == "qa-acceptance-group":
                possible_targets = ["ops-release-group", "dev-working-group", "plan-design-group"]
            elif group_id == "dev-working-group":
                possible_targets = ["ops-release-group", "plan-design-group"]
            else:
                possible_targets = []

            # 2. 收集目标群的最新状态（最后20条）
            target_groups_status = {}
            for target in possible_targets:
                target_messages = all_messages.get(target, [])
                # 直接传递所有消息，让Claude判断
                if target_messages:
                    target_groups_status[target] = target_messages

            # 3. 收集通知历史记录
            notification_history = {}
            for target in possible_targets:
                if target in self.notification_history:
                    notification_history[target] = self.notification_history[target]

            # 构建基础上下文
            context = f"当前分析群组: {config.get('name', group_id)} ({group_id})"
            context += f"\n消息总数: {len(messages)}条（请自行判断消息的新鲜度和相关性）"

            # 添加会话超时信息到上下文
            if session_status["is_timeout"]:
                timeout_info = "\n\n## ⚠️ Agent会话超时警告\n"
                timeout_info += "以下agent会话已超时（10分钟无响应），可能需要重新通知:\n"
                for agent in timeout_agents:
                    timeout_info += f"- {agent['name']}: {agent['minutes_ago']}分钟无响应\n"
                context += timeout_info

            # 4. Claude智能分析（传入完整信息，让Claude自己判断）
            decision = self.analyze_with_claude(
                messages[:20],  # 最后20条消息
                context,
                target_groups_status=target_groups_status,
                notification_history=notification_history,
                session_status=session_status  # 新增：传递会话状态
            )

            if decision:
                decisions_made.append(decision)
                logger.info(f"\n📋 Claude决策:")
                logger.info(f"  动作: {decision.action}")
                if decision.action == "notify":
                    logger.info(f"  目标: {decision.target_group_name}")
                    logger.info(f"  @对象: {', '.join(decision.mention_users)}")
                    logger.info(f"  提炼问题: {decision.extracted_issues}")
                logger.info(f"  理由: {decision.reasoning}")

                # 5. 执行Claude的决策（信任AI）
                if decision.action == "notify":
                    if self.send_notification(decision):
                        notifications_sent += 1
                        # 记录通知历史（供下次Claude参考）
                        self._record_notification(
                            decision.target_group,
                            decision.extracted_issues
                        )
                        # 🎯 标记被通知的群（防止本次调度重复通知）
                        notified_in_this_round.add(decision.target_group)
        # 5. 总结
        logger.info("")
        logger.info("=" * 70)
        logger.info(f"📊 调度总结:")
        logger.info(f"  分析群组: {len(all_messages)}个")
        logger.info(f"  生成决策: {len(decisions_made)}个")
        logger.info(f"  发送通知: {notifications_sent}个")
        logger.info("=" * 70)
        logger.info(f"🏁 调度结束 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("")
        return {
            "groups_analyzed": len(all_messages),
            "decisions_made": len(decisions_made),
            "notifications_sent": notifications_sent
        }
def main():
    """主函数"""
    scheduler = ClaudeDrivenScheduler()
    result = scheduler.run()
    return 0 if result["notifications_sent"] >= 0 else 1
if __name__ == "__main__":
    sys.exit(main())
