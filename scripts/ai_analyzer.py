"""
AI分析层 - 使用Claude/LiteLLM进行智能决策

功能：
- 分析任务事件，决策通知策略
- 理解团队协作上下文
- 生成通知消息内容
"""

import os
import json
import time
import logging
import requests
from typing import Dict, List, Optional
from dataclasses import dataclass
from enum import Enum


logger = logging.getLogger(__name__)


class DecisionType(Enum):
    """决策类型"""
    NOTIFY_GROUP = "notify_group"
    ESCALATE = "escalate"
    WAIT = "wait"
    IGNORE = "ignore"


@dataclass
class NotificationDecision:
    """通知决策结果"""
    decision_type: DecisionType
    target_groups: List[str]
    mention_agents: List[str]
    message_template: str
    message_content: str
    reasoning: str
    confidence: float


class AIAnalyzer:
    """AI分析器 - 使用Claude进行智能决策"""

    SYSTEM_PROMPT = """你是一个智能任务调度Agent。你需要基于任务信息和团队协作上下文，决策应该通知哪些群组和人员。

## 输出格式要求
请严格以JSON格式输出决策结果，不要包含任何其他文本：
{
    "decision_type": "notify_group|escalate|wait|ignore",
    "target_groups": ["群组名称1", "群组名称2"],
    "mention_agents": ["agent1", "agent2"],
    "message_template": "task|urgent|progress|daily",
    "message_content": "通知内容描述",
    "reasoning": "决策理由",
    "confidence": 0.95
}

## 决策规则
1. 新开发任务 → 通知dev-working-group，@负责人
2. 开发完成 → 通知qa-acceptance-group，@qa
3. 验收不通过 → 通知dev-working-group，@原开发负责人
4. 任务超时 → 通知dev-working-group，@负责人+architect
5. 需求变更 → 通知plan-design-group，@产品+设计师
6. 测试完成 → 通知plan-design-group，@product
7. 低优先级小改动 → decision_type设为wait或ignore

## 群组说明
- dev-working-group: 开发工作群，包含fullstack-dev, architect
- qa-acceptance-group: 验收群，包含qa, product
- plan-design-group: 规划设计群，包含product, ui-designer, architect, qa
"""

    TASK_CONTEXT_TEMPLATE = """
## 任务信息
- 任务ID: {task_id}
- 任务类型: {task_type}
- 当前状态: {status}
- 负责人: {assignee}
- 创建时间: {created_at}
- 更新时间: {updated_at}
- 变更类型: {change_type}
- 任务描述: {description}

## 上下文信息
- 任务已运行时间: {elapsed_time}
- 是否超时: {is_timeout}
- 优先级: {priority}

请基于以上信息，做出通知决策。
"""

    def __init__(self, config: Dict):
        """初始化AI分析器"""
        self.config = config
        ai_config = config.get("ai_config", {})

        self.provider = ai_config.get("provider", "claude")
        self.model = ai_config.get("model", "glm-5-pool")
        self.base_url = ai_config.get("base_url", "http://localhost:4000")
        self.api_key = ai_config.get("api_key", os.getenv("LITELLM_API_KEY", ""))
        self.timeout = ai_config.get("timeout", 30)
        self.max_retries = ai_config.get("max_retries", 3)

        # 熔断器状态
        self.failure_count = 0
        self.circuit_breaker_threshold = config.get("circuit_breaker", {}).get("failure_threshold", 5)
        self.circuit_open = False
        self.last_failure_time = 0
        self.reset_timeout = config.get("circuit_breaker", {}).get("reset_timeout", 300)

        logger.info(f"AI分析器初始化完成: model={self.model}, base_url={self.base_url}")

    def analyze(self, task_event: Dict) -> NotificationDecision:
        """
        分析任务事件，返回通知决策

        Args:
            task_event: 任务事件信息

        Returns:
            NotificationDecision: 通知决策
        """
        # 检查熔断器状态
        if self._should_circuit_break():
            logger.warning("熔断器开启，使用降级决策")
            return self._fallback_decision(task_event)

        # 构建prompt
        prompt = self._build_prompt(task_event)

        # 调用AI
        for attempt in range(self.max_retries):
            try:
                response = self._call_ai(prompt)
                decision = self._parse_response(response)

                if decision:
                    self._reset_circuit_breaker()
                    return decision
                else:
                    logger.warning(f"AI响应解析失败，尝试 {attempt + 1}/{self.max_retries}")

            except Exception as e:
                logger.error(f"AI调用失败 (尝试 {attempt + 1}/{self.max_retries}): {e}")
                self._record_failure()

                if attempt < self.max_retries - 1:
                    time.sleep(1)
                    continue

        # 所有尝试都失败，使用降级决策
        logger.warning("AI调用全部失败，使用降级决策")
        return self._fallback_decision(task_event)

    def _build_prompt(self, task_event: Dict) -> str:
        """构建prompt"""
        task = task_event.get("task", {})

        # 计算已运行时间
        created_at = task.get("created_at", 0)
        elapsed_time = time.time() - created_at if created_at else 0

        # 判断是否超时
        task_type = task.get("type", "development")
        timeout_threshold = self.config.get("monitoring", {}).get("timeout_thresholds", {}).get(task_type, 28800)
        is_timeout = elapsed_time > timeout_threshold

        context = self.TASK_CONTEXT_TEMPLATE.format(
            task_id=task.get("id", "unknown"),
            task_type=task_type,
            status=task.get("status", "unknown"),
            assignee=task.get("assignee", "unknown"),
            created_at=task.get("created_at", "N/A"),
            updated_at=task.get("updated_at", "N/A"),
            change_type=task_event.get("change_type", "unknown"),
            description=task.get("description", "无描述")[:200],
            elapsed_time=f"{elapsed_time/3600:.1f}小时" if elapsed_time > 0 else "未知",
            is_timeout="是" if is_timeout else "否",
            priority=task.get("priority", "normal")
        )

        return f"{self.SYSTEM_PROMPT}\n\n{context}"

    def _call_ai(self, prompt: str) -> str:
        """调用AI API"""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }

        payload = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 500
        }

        url = f"{self.base_url}/v1/chat/completions"

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=self.timeout
        )

        if response.status_code == 200:
            data = response.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "")
        else:
            raise Exception(f"API调用失败: HTTP {response.status_code} - {response.text}")

    def _parse_response(self, response: str) -> Optional[NotificationDecision]:
        """解析AI响应"""
        try:
            # 尝试提取JSON
            json_str = self._extract_json(response)
            if not json_str:
                return None

            data = json.loads(json_str)

            # 验证必需字段
            required_fields = ["decision_type", "target_groups", "mention_agents", "message_template"]
            for field in required_fields:
                if field not in data:
                    logger.warning(f"缺少必需字段: {field}")
                    return None

            return NotificationDecision(
                decision_type=DecisionType(data.get("decision_type", "notify_group")),
                target_groups=data.get("target_groups", []),
                mention_agents=data.get("mention_agents", []),
                message_template=data.get("message_template", "task"),
                message_content=data.get("message_content", ""),
                reasoning=data.get("reasoning", ""),
                confidence=data.get("confidence", 0.8)
            )

        except json.JSONDecodeError as e:
            logger.error(f"JSON解析失败: {e}")
            return None
        except Exception as e:
            logger.error(f"响应解析失败: {e}")
            return None

    def _extract_json(self, text: str) -> Optional[str]:
        """从文本中提取JSON"""
        import re

        # 尝试直接匹配JSON对象
        json_match = re.search(r'\{[\s\S]*\}', text)
        if json_match:
            return json_match.group(0)

        return None

    def _fallback_decision(self, task_event: Dict) -> NotificationDecision:
        """降级决策 - 基于任务类型和工作群职责的规则映射"""
        task = task_event.get("task", {})
        change_type = task_event.get("change_type", "")
        task_type = task.get("type", "development")
        assignee = task.get("assignee", "")

        # 任务类型到群组的映射
        type_to_group = {
            "development": "dev-working-group",
            "testing": "qa-acceptance-group",
            "qa": "qa-acceptance-group",
            "requirement": "plan-design-group",
            "design": "plan-design-group",
            "deployment": "dev-working-group"
        }

        # 根据任务类型获取目标群组
        target_group = type_to_group.get(task_type, "dev-working-group")

        # 简单规则映射
        if change_type == "new_task":
            return NotificationDecision(
                decision_type=DecisionType.NOTIFY_GROUP,
                target_groups=[target_group],
                mention_agents=[assignee] if assignee else [],
                message_template="task",
                message_content=f"新任务: {task.get('description', '')[:100]}",
                reasoning=f"降级决策: 新{task_type}任务通知到{target_group}",
                confidence=0.6
            )
        elif change_type == "status_changed":
            new_status = task_event.get("new_status", "")
            if new_status == "completed":
                # 开发完成 → 通知验收群
                if task_type in ["development", "design"]:
                    return NotificationDecision(
                        decision_type=DecisionType.NOTIFY_GROUP,
                        target_groups=["qa-acceptance-group"],
                        mention_agents=["qa"],
                        message_template="task",
                        message_content=f"任务完成，请验收: {task.get('id', '')}",
                        reasoning="降级决策: 开发完成通知验收",
                        confidence=0.6
                    )
                # 验收完成 → 通知产品设计群
                elif task_type in ["testing", "qa"]:
                    return NotificationDecision(
                        decision_type=DecisionType.NOTIFY_GROUP,
                        target_groups=["plan-design-group"],
                        mention_agents=["product"],
                        message_template="progress",
                        message_content=f"验收通过: {task.get('id', '')}",
                        reasoning="降级决策: 验收通过通知产品",
                        confidence=0.6
                    )
            elif new_status == "blocked":
                # 验收不通过 → 通知开发群
                return NotificationDecision(
                    decision_type=DecisionType.NOTIFY_GROUP,
                    target_groups=["dev-working-group"],
                    mention_agents=[assignee] if assignee else [],
                    message_template="urgent",
                    message_content=f"验收不通过，需要修复: {task.get('id', '')}",
                    reasoning="降级决策: 验收不通过通知开发",
                    confidence=0.6
                )
        elif change_type == "timeout":
            # 超时任务 - 根据任务类型决定升级对象
            if task_type in ["testing", "qa"]:
                # 验收任务超时 → @qa + @product
                mention_agents = ["qa", "product"]
            else:
                # 开发任务超时 → @负责人 + @architect
                mention_agents = [assignee, "architect"] if assignee else ["architect"]

            return NotificationDecision(
                decision_type=DecisionType.ESCALATE,
                target_groups=[target_group],
                mention_agents=mention_agents,
                message_template="urgent",
                message_content=f"任务超时: {task.get('id', '')}",
                reasoning="降级决策: 任务超时升级",
                confidence=0.6
            )

        # 默认：忽略
        return NotificationDecision(
            decision_type=DecisionType.WAIT,
            target_groups=[],
            mention_agents=[],
            message_template="task",
            message_content="",
            reasoning="降级决策: 等待处理",
            confidence=0.5
        )

    def _should_circuit_break(self) -> bool:
        """检查是否应该触发熔断"""
        if not self.circuit_open:
            return False

        # 检查是否可以尝试恢复
        if time.time() - self.last_failure_time > self.reset_timeout:
            logger.info("熔断器尝试恢复")
            self.circuit_open = False
            return False

        return True

    def _record_failure(self):
        """记录失败"""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.circuit_breaker_threshold:
            self.circuit_open = True
            logger.warning(f"熔断器触发: 连续失败 {self.failure_count} 次")

    def _reset_circuit_breaker(self):
        """重置熔断器"""
        self.failure_count = 0
        self.circuit_open = False


def test_ai_analyzer():
    """测试AI分析器"""
    import sys
    sys.path.insert(0, "/home/gongdewei/.openclaw/workspace-main")

    # 加载配置
    with open("/home/gongdewei/.openclaw/workspace-main/config/intelligent_scheduling.json") as f:
        config = json.load(f)

    # 创建分析器
    analyzer = AIAnalyzer(config)

    # 测试事件
    test_event = {
        "task_id": "test-001",
        "change_type": "new_task",
        "task": {
            "id": "test-001",
            "type": "development",
            "status": "pending",
            "assignee": "fullstack-dev",
            "description": "实现智能调度系统的AI分析模块",
            "created_at": time.time(),
            "priority": "high"
        }
    }

    print("\n测试1: 新任务分析")
    decision = analyzer.analyze(test_event)
    print(f"决策类型: {decision.decision_type}")
    print(f"目标群组: {decision.target_groups}")
    print(f"@对象: {decision.mention_agents}")
    print(f"消息模板: {decision.message_template}")
    print(f"置信度: {decision.confidence}")
    print(f"理由: {decision.reasoning}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test_ai_analyzer()
