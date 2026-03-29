"""
通知执行层 - 执行通知决策

功能：
- 接收AI决策，执行Mattermost通知
- 支持消息模板
- 支持批量发送
- 支持重试机制
"""

import time
import json
import logging
import sys
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from adapters.mattermost_adapter import MattermostAdapter, MattermostConfig, MessageTemplate


logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    """执行结果"""
    success: bool
    channel: str
    message: str
    error: Optional[str] = None
    latency_ms: int = 0
    timestamp: float = 0


class NotificationExecutor:
    """通知执行器"""

    # 消息模板映射
    TEMPLATE_MAP = {
        "task": MessageTemplate.TASK,
        "urgent": MessageTemplate.URGENT,
        "progress": MessageTemplate.PROGRESS,
        "meeting": MessageTemplate.MEETING,
        "daily": MessageTemplate.DAILY
    }

    def __init__(self, config: Dict, adapter: Optional[MattermostAdapter] = None):
        """初始化通知执行器"""
        self.config = config

        # Mattermost适配器
        if adapter:
            self.adapter = adapter
        else:
            mm_config = config.get("mattermost", {})
            self.adapter = MattermostAdapter(MattermostConfig(
                base_url=mm_config.get("base_url", "http://localhost:8066"),
                admin_token=mm_config.get("admin_token", ""),
                channel_map=self._build_channel_map(),
                agent_map=self._build_agent_map()
            ))

        # 重试配置
        self.retry_count = config.get("notification", {}).get("retry_count", 3)
        self.retry_delay = config.get("notification", {}).get("retry_delay", 1)

        # 执行历史（用于统计）
        self.execution_history: List[ExecutionResult] = []
        self.max_history_size = 1000

        logger.info("通知执行器初始化完成")

    def _build_channel_map(self) -> Dict[str, str]:
        """构建群组映射"""
        groups = self.config.get("groups", {})
        channel_map = {}

        for group_name, group_config in groups.items():
            channel_id = group_config.get("channel_id", "")
            if channel_id:
                # 支持简短名称和完整名称
                channel_map[group_name] = channel_id
                # 添加简短名称映射
                if "-" in group_name:
                    short_name = group_name.split("-")[0]
                    channel_map[short_name] = channel_id

        return channel_map

    def _build_agent_map(self) -> Dict[str, str]:
        """构建Agent映射"""
        agent_mapping = self.config.get("agent_mapping", {})
        agent_map = {}

        for agent_name, agent_config in agent_mapping.items():
            mm_username = agent_config.get("mm_username", agent_name)
            agent_map[agent_name] = mm_username

        return agent_map

    def execute(self, decision: Dict, dry_run: bool = False) -> ExecutionResult:
        """
        执行通知决策

        Args:
            decision: AI决策结果
            dry_run: 是否为预览模式

        Returns:
            ExecutionResult: 执行结果
        """
        start_time = time.time()
        timestamp = time.time()

        target_groups = decision.get("target_groups", [])
        mention_agents = decision.get("mention_agents", [])
        message_template = decision.get("message_template", "task")
        message_content = decision.get("message_content", "")

        # 如果没有目标群组，返回成功
        if not target_groups:
            logger.info("没有目标群组，跳过通知")
            return ExecutionResult(
                success=True,
                channel="",
                message="No target groups",
                latency_ms=int((time.time() - start_time) * 1000),
                timestamp=timestamp
            )

        # 获取消息模板
        template = self.TEMPLATE_MAP.get(message_template, MessageTemplate.TASK)

        # 构建消息内容
        full_message = self._build_full_message(decision)

        results = []
        for group in target_groups:
            result = self._send_to_group(
                channel=group,
                message=full_message,
                agents=mention_agents,
                template=template,
                dry_run=dry_run
            )
            results.append(result)

        # 汇总结果
        success = all(r.success for r in results)
        latency_ms = int((time.time() - start_time) * 1000)

        execution_result = ExecutionResult(
            success=success,
            channel=",".join(target_groups),
            message=full_message,
            error=None if success else "部分发送失败",
            latency_ms=latency_ms,
            timestamp=timestamp
        )

        # 记录到历史
        self._record_execution(execution_result)

        return execution_result

    def execute_batch(self, decisions: List[Dict], dry_run: bool = False) -> List[ExecutionResult]:
        """
        批量执行通知决策

        Args:
            decisions: 决策列表
            dry_run: 是否为预览模式

        Returns:
            List[ExecutionResult]: 执行结果列表
        """
        results = []
        for decision in decisions:
            result = self.execute(decision, dry_run)
            results.append(result)
            # 避免请求过快
            time.sleep(0.1)

        return results

    def _send_to_group(
        self,
        channel: str,
        message: str,
        agents: List[str],
        template: MessageTemplate,
        dry_run: bool
    ) -> ExecutionResult:
        """发送消息到单个群组"""
        start_time = time.time()

        for attempt in range(self.retry_count):
            try:
                success = self.adapter.send_notification(
                    channel=channel,
                    message=message,
                    agents=agents,
                    template=template,
                    dry_run=dry_run
                )

                if success:
                    return ExecutionResult(
                        success=True,
                        channel=channel,
                        message=message,
                        latency_ms=int((time.time() - start_time) * 1000),
                        timestamp=time.time()
                    )
                else:
                    logger.warning(f"发送失败 (尝试 {attempt + 1}/{self.retry_count}): {channel}")

                    if attempt < self.retry_count - 1:
                        time.sleep(self.retry_delay)
                        continue

            except Exception as e:
                logger.error(f"发送异常 (尝试 {attempt + 1}/{self.retry_count}): {e}")

                if attempt < self.retry_count - 1:
                    time.sleep(self.retry_delay)
                    continue

        # 所有尝试都失败
        return ExecutionResult(
            success=False,
            channel=channel,
            message=message,
            error=f"发送失败，已重试 {self.retry_count} 次",
            latency_ms=int((time.time() - start_time) * 1000),
            timestamp=time.time()
        )

    def _build_full_message(self, decision: Dict) -> str:
        """构建完整消息内容"""
        content = decision.get("message_content", "")
        reasoning = decision.get("reasoning", "")
        task_id = decision.get("task_id", "")

        parts = [content]

        if task_id:
            parts.append(f"\n任务ID: {task_id}")

        if reasoning and len(reasoning) < 100:
            parts.append(f"\n💡 {reasoning}")

        return "\n".join(parts)

    def _record_execution(self, result: ExecutionResult):
        """记录执行结果"""
        self.execution_history.append(result)

        # 限制历史大小
        if len(self.execution_history) > self.max_history_size:
            self.execution_history = self.execution_history[-self.max_history_size:]

    def get_statistics(self) -> Dict:
        """获取执行统计"""
        if not self.execution_history:
            return {
                "total": 0,
                "success": 0,
                "failed": 0,
                "success_rate": 0,
                "avg_latency_ms": 0
            }

        total = len(self.execution_history)
        success = sum(1 for r in self.execution_history if r.success)
        failed = total - success
        avg_latency = sum(r.latency_ms for r in self.execution_history) / total

        return {
            "total": total,
            "success": success,
            "failed": failed,
            "success_rate": success / total if total > 0 else 0,
            "avg_latency_ms": int(avg_latency)
        }

    def get_recent_failures(self, limit: int = 10) -> List[ExecutionResult]:
        """获取最近失败的执行"""
        failures = [r for r in self.execution_history if not r.success]
        return failures[-limit:]


def test_notification_executor():
    """测试通知执行器"""
    # 加载配置
    with open("/home/gongdewei/.openclaw/workspace-main/config/intelligent_scheduling.json") as f:
        config = json.load(f)

    # 创建执行器
    executor = NotificationExecutor(config)

    # 测试决策
    test_decision = {
        "target_groups": ["dev-working-group"],
        "mention_agents": ["fullstack-dev"],
        "message_template": "task",
        "message_content": "测试通知：智能调度系统开发中",
        "reasoning": "测试决策",
        "task_id": "test-001"
    }

    print("\n测试1: 单次执行 (dry-run)")
    result = executor.execute(test_decision, dry_run=True)
    print(f"成功: {result.success}")
    print(f"延迟: {result.latency_ms}ms")

    print("\n测试2: 获取统计")
    stats = executor.get_statistics()
    print(f"统计: {stats}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test_notification_executor()
