# OpenClaw Agent Scheduler - 智能任务调度系统

基于 Claude AI 的智能任务调度系统，用于监控工作群消息并自动触发跨群协作通知。

## 功能特性

- **AI驱动决策**: 使用 Claude API 智能分析群聊消息，自动判断是否需要跨群通知
- **执行者超时检测**: 区分架构师和执行者角色，执行者超时自动触发通知
- **多平台通知**: 支持 Mattermost 群聊和飞书消息通知
- **去重机制**: 记录通知历史，避免重复通知
- **上下文感知**: 读取各群最后20条消息，AI自行判断消息相关性

## 目录结构

```
openclaw-agent-scheduler/
├── data/
│   └── notification_history.json  # 通知历史记录
├── docs/
│   ├── ai-driven-scheduling-redesign.md  # AI驱动调度设计文档
│   ├── ai-driven-fix-summary.md          # 修复总结
│   └── 2026-03-29-executor-timeout-fix.md # 执行者超时修复
├── scripts/
│   ├── claude_driven_scheduler.py  # 核心调度器 (主入口)
│   ├── ai_analyzer.py              # Claude API 分析器
│   ├── notification_executor.py    # 通知执行器
│   └── scheduler_agent.py          # Agent会话管理
└── README.md
```

## 快速开始

### 1. 环境要求

- Python 3.8+
- LiteLLM API (Claude代理)
- Mattermost 服务
- 飞书 Webhook (可选)

### 2. 配置

编辑 `claude_driven_scheduler.py` 中的配置：

```python
# Mattermost配置
MATTERMOST_URL = "http://localhost:8066"
MATTERMOST_TOKEN = "your-token"

# 飞书配置
FEISHU_WEBHOOK = "https://open.feishu.cn/..."

# LiteLLM配置
LITELLM_URL = "http://localhost:4000"

# 监控群组配置
GROUP_CONFIGS = {
    "dev-working-group": {
        "name": "开发工作群",
        "role": "requester",
        "target_groups": ["qa-acceptance-group"]
    },
    # ...
}
```

### 3. 运行

```bash
# 单次执行
python scripts/claude_driven_scheduler.py

# 定时调度 (crontab)
*/5 * * * * cd /path/to/openclaw-agent-scheduler && python scripts/claude_driven_scheduler.py >> logs/scheduler.log 2>&1
```

## 核心概念

### 角色区分

| 角色 | 职责 | 超时处理 |
|------|------|---------|
| 执行者 (fullstack-dev, backend-dev, ops) | 实际执行任务 | 超时10分钟 → 强制通知 |
| 架构师 (architect, product, qa) | 顾问/指导 | 超时可等待 |

### AI决策优先级

1. **执行者超时** → 立即通知 (最高优先级)
2. **问题已解决** → 通知等待群
3. **需要协助** → 通知责任群
4. **等待** → 不通知，继续监控
5. **忽略** → 无需处理

### 通知历史

系统记录每次通知到 `data/notification_history.json`，防止重复通知：

```json
{
  "history": [
    {
      "timestamp": "2026-03-29T20:00:00",
      "source_group": "dev-working-group",
      "target_group": "qa-acceptance-group",
      "reason": "开发完成，请求验收"
    }
  ]
}
```

## 文档

- [AI驱动调度设计](docs/ai-driven-scheduling-redesign.md) - 系统架构设计
- [修复总结](docs/ai-driven-fix-summary.md) - 历史问题修复记录
- [执行者超时修复](docs/2026-03-29-executor-timeout-fix.md) - 超时检测优化

## 许可证

MIT License
