# OpenClaw Agent Scheduler - 智能任务调度系统

基于 Claude AI 的智能任务调度系统，用于监控工作群消息并自动触发跨群协作通知。

## 功能特性

- **AI驱动决策**: 使用 Claude API 智能分析群聊消息，自动判断是否需要跨群通知
- **执行者超时检测**: 区分架构师和执行者角色，执行者超时自动触发通知
- **配置外置**: 群组配置和提示词分离到独立文件，便于维护
- **多平台通知**: 支持 Mattermost 群聊和飞书消息通知
- **去重机制**: 记录通知历史，避免重复通知
- **上下文感知**: 读取各群最后20条消息，AI自行判断消息相关性

## 目录结构

```
openclaw-agent-scheduler/
├── config/
│   ├── groups.yaml              # 工作群配置（主要配置文件）
│   ├── groups.yaml.example      # 配置示例
│   └── prompts/
│       └── decision_prompt.md   # AI决策提示词
├── data/
│   └── notification_history.json  # 通知历史记录
├── docs/
│   ├── ai-driven-scheduling-redesign.md  # 系统设计文档
│   ├── ai-driven-fix-summary.md          # 修复总结
│   └── 2026-03-29-executor-timeout-fix.md # 超时检测文档
├── logs/                        # 运行日志
├── scripts/
│   └── claude_driven_scheduler.py  # 核心调度器（主入口）
├── README.md
└── requirements.txt
```

## 快速开始

### 1. 环境要求

- Python 3.8+
- LiteLLM API (Claude代理)
- Mattermost 服务
- 飞书 Webhook (可选)

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置

1. 复制配置示例：
```bash
cp config/groups.yaml.example config/groups.yaml
```

2. 编辑 `config/groups.yaml`：
```yaml
# 工作群配置
groups:
  dev-working-group:
    name: 开发工作群
    room_id: "your-channel-id"
    role: requester
    target_groups:
      - qa-acceptance-group
    agents:
      - fullstack-dev
      - architect

# Agent角色分类
agent_roles:
  executors:  # 执行者 - 超时必须通知
    - fullstack-dev
    - ops
  advisors:   # 顾问 - 超时可等待
    - architect
    - product

# API配置
mattermost:
  url: "http://localhost:8066"

litellm:
  url: "http://localhost:4000"
  model: "claude-sonnet-4-6"
```

3. 设置环境变量（推荐）：
```bash
export MATTERMOST_TOKEN="your-token"
export LITELLM_API_KEY="your-api-key"
export FEISHU_WEBHOOK="https://open.feishu.cn/..."
```

### 4. 自定义提示词

编辑 `config/prompts/decision_prompt.md` 调整AI决策规则。

### 5. 运行

```bash
# 单次执行
python scripts/claude_driven_scheduler.py

# 定时调度 (crontab)
*/5 * * * * cd /path/to/openclaw-agent-scheduler && python scripts/claude_driven_scheduler.py >> logs/cron.log 2>&1
```

## 核心概念

### 角色区分

| 角色 | 职责 | 超时处理 |
|------|------|---------|
| 执行者 (executors) | 实际执行任务 (fullstack-dev, ops) | 超时10分钟 → 强制通知 |
| 顾问 (advisors) | 顾问/指导 (architect, product, qa) | 超时可等待 |

### AI决策优先级

1. **执行者超时** → 立即通知 (最高优先级)
2. **问题已解决** → 通知等待群
3. **需要协助** → 通知责任群
4. **等待** → 不通知，继续监控
5. **忽略** → 无需处理

### 通知历史

系统记录每次通知到 `data/notification_history.json`，防止重复通知。

## 配置说明

### config/groups.yaml

| 字段 | 说明 |
|------|------|
| `groups.*.room_id` | Mattermost频道ID |
| `groups.*.role` | 群角色: requester/executor/validator/planner |
| `groups.*.target_groups` | 目标通知群 |
| `groups.*.agents` | 群内Agent成员 |
| `agent_roles.executors` | 执行者列表（超时必通知） |
| `agent_roles.advisors` | 顾问列表（超时可等待） |

### config/prompts/decision_prompt.md

可自定义：
- 决策规则
- 优先级
- 通知格式
- 角色定义

## 文档

- [AI驱动调度设计](docs/ai-driven-scheduling-redesign.md) - 系统架构设计
- [修复总结](docs/ai-driven-fix-summary.md) - 历史问题修复记录
- [执行者超时修复](docs/2026-03-29-executor-timeout-fix.md) - 超时检测优化

## 许可证

MIT License
