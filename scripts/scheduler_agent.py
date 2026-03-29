#!/usr/bin/env python3
"""
智能调度Agent - 主调度入口

功能：
- 整合监控、分析、执行、反馈各层
- 支持命令行参数
- 支持单次执行和持续运行
- 支持dry-run模式
"""

import os
import sys
import json
import time
import argparse
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
from logging.handlers import RotatingFileHandler

# 添加路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from intelligent_scheduling.task_monitor import TaskMonitor, TaskEvent, EventType
from intelligent_scheduling.ai_analyzer import AIAnalyzer, NotificationDecision, DecisionType
from intelligent_scheduling.notification_executor import NotificationExecutor, ExecutionResult
from adapters.task_system_adapter import TaskSystemAdapter, TaskSystemConfig
from adapters.mattermost_adapter import MattermostAdapter, MattermostConfig


# ============= 日志配置 =============

def setup_logging(config: Dict, verbose: bool = False) -> logging.Logger:
    """配置日志"""
    log_config = config.get("logging", {})
    log_level = logging.DEBUG if verbose else getattr(logging, log_config.get("level", "INFO"))
    log_dir = Path(log_config.get("dir", "logs/intelligent_scheduling"))
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("SchedulerAgent")
    logger.setLevel(log_level)

    # 文件处理器
    log_file = log_dir / "scheduler.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=log_config.get("max_size_mb", 100) * 1024 * 1024,
        backupCount=log_config.get("backup_count", 7)
    )
    file_handler.setLevel(log_level)
    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    file_handler.setFormatter(file_formatter)

    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    )
    console_handler.setFormatter(console_formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


# ============= 反馈优化层 =============

class FeedbackOptimizer:
    """反馈优化层"""

    def __init__(self, config: Dict):
        self.config = config
        log_dir = Path(config.get("logging", {}).get("dir", "logs/intelligent_scheduling"))
        log_dir.mkdir(parents=True, exist_ok=True)

        self.decision_log_file = log_dir / "decisions.jsonl"
        self.metrics = {
            "total_decisions": 0,
            "successful_executions": 0,
            "failed_executions": 0,
            "ai_calls": 0,
            "ai_failures": 0,
            "total_latency_ms": 0
        }

    def log_decision(
        self,
        event: TaskEvent,
        decision: NotificationDecision,
        result: ExecutionResult
    ):
        """记录调度决策"""
        record = {
            "timestamp": datetime.now().isoformat(),
            "event": {
                "type": event.event_type.value,
                "task_id": event.task_id
            },
            "decision": {
                "type": decision.decision_type.value,
                "target_groups": decision.target_groups,
                "mention_agents": decision.mention_agents,
                "confidence": decision.confidence
            },
            "execution": {
                "success": result.success,
                "latency_ms": result.latency_ms,
                "error": result.error
            }
        }

        # 写入日志文件
        with open(self.decision_log_file, 'a') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')

        # 更新指标
        self.metrics["total_decisions"] += 1
        if result.success:
            self.metrics["successful_executions"] += 1
        else:
            self.metrics["failed_executions"] += 1
        self.metrics["total_latency_ms"] += result.latency_ms

    def get_metrics(self) -> Dict:
        """获取指标"""
        metrics = self.metrics.copy()
        if metrics["total_decisions"] > 0:
            metrics["success_rate"] = metrics["successful_executions"] / metrics["total_decisions"]
            metrics["avg_latency_ms"] = metrics["total_latency_ms"] / metrics["total_decisions"]
        else:
            metrics["success_rate"] = 0
            metrics["avg_latency_ms"] = 0
        return metrics

    def generate_report(self) -> str:
        """生成报告"""
        metrics = self.get_metrics()
        report = f"""
# 智能调度系统报告

**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 调度统计

| 指标 | 值 |
|------|-----|
| 总决策数 | {metrics['total_decisions']} |
| 成功执行 | {metrics['successful_executions']} |
| 失败执行 | {metrics['failed_executions']} |
| 成功率 | {metrics['success_rate']:.2%} |
| 平均延迟 | {metrics['avg_latency_ms']:.0f}ms |
"""
        return report


# ============= 异常处理层 =============

class ExceptionHandler:
    """异常处理层"""

    def __init__(self, config: Dict, executor: NotificationExecutor):
        self.config = config
        self.executor = executor
        cb_config = config.get("circuit_breaker", {})
        self.failure_threshold = cb_config.get("failure_threshold", 5)
        self.reset_timeout = cb_config.get("reset_timeout", 300)

        self.failure_count = 0
        self.circuit_open = False
        self.last_failure_time = 0

    def handle_failure(self, error: Exception, context: Dict) -> bool:
        """处理失败"""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            self.circuit_open = True

        # 尝试发送告警
        try:
            self._send_alert(error, context)
        except Exception as e:
            logging.error(f"发送告警失败: {e}")

        return not self.circuit_open

    def should_skip(self) -> bool:
        """检查是否应该跳过（熔断器）"""
        if not self.circuit_open:
            return False

        # 检查是否可以恢复
        if time.time() - self.last_failure_time > self.reset_timeout:
            logging.info("熔断器尝试恢复")
            self.circuit_open = False
            self.failure_count = 0
            return False

        return True

    def reset(self):
        """重置熔断器"""
        self.failure_count = 0
        self.circuit_open = False

    def _send_alert(self, error: Exception, context: Dict):
        """发送告警"""
        # 发送到Mattermost
        alert_message = f"🚨 调度系统异常: {str(error)[:200]}"
        # 可以调用executor发送告警，但需要避免循环


# ============= 主调度Agent =============

class SchedulerAgent:
    """智能调度Agent"""

    def __init__(self, config_path: str):
        """初始化调度Agent"""
        # 加载配置
        self.config = self._load_config(config_path)

        # 初始化日志
        self.logger = logging.getLogger("SchedulerAgent")

        # 初始化各层
        self._init_components()

        # 运行状态
        self.is_running = False

        self.logger.info("智能调度Agent初始化完成")

    def _load_config(self, config_path: str) -> Dict:
        """加载配置"""
        with open(config_path) as f:
            config = json.load(f)

        # 替换环境变量
        config_str = json.dumps(config)
        import re
        env_vars = re.findall(r'\$\{(\w+)\}', config_str)
        for var in env_vars:
            value = os.getenv(var, "")
            config_str = config_str.replace(f"${{{var}}}", value)

        return json.loads(config_str)

    def _init_components(self):
        """初始化组件"""
        # 任务系统适配器配置
        task_config = self.config.get("task_system", {})
        self.task_config = TaskSystemConfig(
            base_url=task_config.get("base_url", "http://localhost:5100"),
            timeout=task_config.get("timeout", 10),
            retry_count=task_config.get("retry_count", 3),
            retry_delay=task_config.get("retry_delay", 1)
        )

        # 任务监控器
        self.monitor = TaskMonitor(
            task_system_config=self.task_config,
            poll_interval=self.config.get("monitoring", {}).get("poll_interval", 300),
            timeout_thresholds=self.config.get("monitoring", {}).get("timeout_thresholds", {})
        )

        # AI分析器
        self.analyzer = AIAnalyzer(self.config)

        # 通知执行器
        self.executor = NotificationExecutor(self.config)

        # 异常处理器
        self.exception_handler = ExceptionHandler(self.config, self.executor)

        # 反馈优化器
        self.feedback = FeedbackOptimizer(self.config)

    def run(self, dry_run: bool = False, once: bool = False, verbose: bool = False):
        """
        运行调度

        Args:
            dry_run: 预览模式，不实际发送
            once: 单次执行模式
            verbose: 详细日志
        """
        self.is_running = True
        poll_interval = self.config.get("monitoring", {}).get("poll_interval", 300)

        self.logger.info(f"调度启动: dry_run={dry_run}, once={once}, poll_interval={poll_interval}s")

        while self.is_running:
            try:
                # 检查熔断器
                if self.exception_handler.should_skip():
                    self.logger.warning("熔断器开启，跳过本次调度")
                    time.sleep(poll_interval)
                    continue

                # 执行调度
                self._run_once(dry_run, verbose)

                # 重置熔断器
                self.exception_handler.reset()

            except Exception as e:
                self.logger.error(f"调度执行失败: {e}")
                self.exception_handler.handle_failure(e, {})

            if once:
                break

            if self.is_running:
                self.logger.info(f"等待 {poll_interval} 秒后进行下一轮调度...")
                time.sleep(poll_interval)

    def _run_once(self, dry_run: bool, verbose: bool):
        """执行一次调度"""
        start_time = time.time()

        self.logger.info("=" * 50)
        self.logger.info(f"开始调度 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        # 1. 获取事件
        events = self.monitor.get_events()
        self.logger.info(f"检测到 {len(events)} 个事件")

        if not events:
            self.logger.info("没有需要处理的事件")
            return

        # 2. 处理每个事件
        for event in events:
            try:
                self._process_event(event, dry_run, verbose)
            except Exception as e:
                self.logger.error(f"处理事件失败 [{event.event_type.value}]: {e}")

        # 3. 输出统计
        elapsed = time.time() - start_time
        metrics = self.feedback.get_metrics()
        self.logger.info(f"调度完成: 耗时 {elapsed:.2f}s, 成功率 {metrics['success_rate']:.2%}")

    def _process_event(self, event: TaskEvent, dry_run: bool, verbose: bool):
        """处理单个事件"""
        self.logger.info(f"处理事件: {event.event_type.value} - {event.task_id}")

        # 构建事件数据
        event_data = {
            "event_id": f"{event.event_type.value}_{event.task_id}_{int(time.time())}",
            "task_id": event.task_id,
            "change_type": event.event_type.value,
            "task": event.task_data if hasattr(event, 'task_data') else {}
        }

        # 调用AI分析
        decision = self.analyzer.analyze(event_data)

        if verbose:
            self.logger.debug(f"AI决策: {decision.decision_type.value}")
            self.logger.debug(f"目标群组: {decision.target_groups}")
            self.logger.debug(f"@对象: {decision.mention_agents}")
            self.logger.debug(f"置信度: {decision.confidence}")

        # 如果是等待或忽略，跳过
        if decision.decision_type in [DecisionType.WAIT, DecisionType.IGNORE]:
            self.logger.info(f"跳过事件 [{decision.decision_type.value}]: {decision.reasoning}")
            return

        # 执行通知
        result = self.executor.execute(
            decision={
                "target_groups": decision.target_groups,
                "mention_agents": decision.mention_agents,
                "message_template": decision.message_template,
                "message_content": decision.message_content,
                "reasoning": decision.reasoning,
                "task_id": event.task_id
            },
            dry_run=dry_run
        )

        # 记录反馈
        self.feedback.log_decision(event, decision, result)

        if result.success:
            self.logger.info(f"通知发送成功: {result.channel}")
        else:
            self.logger.warning(f"通知发送失败: {result.error}")

    def stop(self):
        """停止调度"""
        self.is_running = False
        self.logger.info("调度停止")

    def get_status(self) -> Dict:
        """获取状态"""
        return {
            "is_running": self.is_running,
            "metrics": self.feedback.get_metrics(),
            "circuit_breaker": {
                "open": self.exception_handler.circuit_open,
                "failure_count": self.exception_handler.failure_count
            }
        }


# ============= 命令行入口 =============

def main():
    parser = argparse.ArgumentParser(description="智能调度Agent")

    parser.add_argument(
        "--config", "-c",
        default="config/intelligent_scheduling.json",
        help="配置文件路径"
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="预览模式，不实际发送通知"
    )
    parser.add_argument(
        "--once", "-1",
        action="store_true",
        help="单次执行模式"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="详细日志"
    )
    parser.add_argument(
        "--report", "-r",
        action="store_true",
        help="生成报告并退出"
    )

    args = parser.parse_args()

    # 切换工作目录
    os.chdir("/home/gongdewei/.openclaw/workspace-main")

    # 加载配置
    with open(args.config) as f:
        config = json.load(f)

    # 配置日志
    logger = setup_logging(config, args.verbose)

    # 创建Agent
    agent = SchedulerAgent(args.config)

    if args.report:
        print(agent.feedback.generate_report())
        return

    # 运行
    try:
        agent.run(dry_run=args.dry_run, once=args.once, verbose=args.verbose)
    except KeyboardInterrupt:
        agent.stop()
        print("\n调度已停止")


if __name__ == "__main__":
    main()
