# AI驱动调度系统 - 重新设计方案

**创建时间**: 2026-03-29 18:25
**问题发现**: 代码中硬编码了业务规则，变成了规则驱动而不是AI驱动
**目标**: 将所有决策逻辑交给Claude，代码只负责数据获取和执行

---

## 🎯 核心原则

### 1. 代码职责（固定逻辑）
- ✅ 获取群消息
- ✅ 加载历史记录
- ✅ 构建上下文给Claude
- ✅ 执行Claude的决策
- ✅ 记录执行结果

### 2. Claude职责（智能决策）
- ✅ 分析问题严重性
- ✅ 检查目标群是否已响应
- ✅ 判断是否需要通知
- ✅ 决定通知内容
- ✅ 决定等待还是忽略

---

## 🔄 正确流程

### 步骤1: 数据获取（代码）
```python
# 1. 获取所有群消息
all_group_messages = {
    "qa-acceptance-group": [...最近20条消息...],
    "dev-working-group": [...最近20条消息...],
    "ops-release-group": [...最近20条消息...],
    "plan-design-group": [...最近20条消息...]
}

# 2. 加载通知历史
notification_history = {
    "ops-release-group": {
        "last_notify_time": "18:00:00",
        "notified_issues": ["数据库Schema问题"],
        "times_notified": 3
    }
}
```

### 步骤2: 构建完整上下文（代码）
```python
context = {
    "current_group": "qa-acceptance-group",
    "current_group_messages": [...最近20条...],
    "other_groups_status": {
        "ops-release-group": {
            "recent_messages": [...最近5条...],
            "last_notification": {
                "time": "18:00:00 (28分钟前)",
                "issues": ["数据库Schema问题"],
                "times": 3
            }
        }
    }
}
```

### 步骤3: Claude智能分析（AI）
```
Claude看到:
- 验收群在报告: "数据库Schema错误，阻塞验收"
- 运维群响应: "问题已解决 ✅"
- 历史记录: 28分钟前通知过3次

Claude判断:
- 问题: 目标群已经反馈"已解决"
- 决策: ignore（不再通知）
- 理由: 运维群已明确反馈问题解决，无需重复通知
```

### 步骤4: 执行决策（代码）
```python
if decision.action == "ignore":
    logger.info(f"Claude决策: 忽略 - {decision.reasoning}")
elif decision.action == "wait":
    logger.info(f"Claude决策: 等待 - {decision.reasoning}")
elif decision.action == "notify":
    send_notification(decision)
    record_notification(decision)
```

---

## 📝 新的System Prompt

```
你是一个智能团队协作调度Agent。你的职责是：
1. 全面分析所有群组的消息状态
2. 根据完整上下文做出智能决策
3. 避免重复通知和无效打扰

## 你会看到的完整信息
1. **源群消息**: 提出问题或请求的群组（最近20条）
2. **目标群状态**: 可能被通知的群组（最近5条）
3. **历史通知记录**: 之前的通知时间和内容

## 决策规则（由你判断）
1. **检查目标群响应**:
   - 如果目标群已经响应"已解决"/"已修复"/"完成"，应该返回 `ignore`
   - 如果目标群正在处理中，应该返回 `wait`

2. **避免重复通知**:
   - 如果最近30分钟内通知过相同问题，且目标群没有新进展，应该返回 `wait`
   - 如果问题升级或情况变化，可以再次通知

3. **通知时机**:
   - 只有在真正需要跨群协作，且目标群未响应时，才返回 `notify`

## 输出格式
{
    "action": "notify|wait|ignore",
    "target_group": "群组ID",
    "target_group_name": "群组名称",
    "mention_users": ["本群成员"],
    "message_content": "通知内容",
    "reasoning": "决策理由（必须包含：为什么这个决策）",
    "extracted_issues": ["具体问题"]
}
```

---

## 🚀 实施步骤

### 第1步: 修改数据准备逻辑
- ❌ 删除: 硬编码的`_check_issue_resolved`检查
- ❌ 删除: 硬编码的30分钟去重检查
- ✅ 添加: 将通知历史包含在上下文中

### 第2步: 增强Claude Prompt
- 添加历史通知记录到prompt
- 添加目标群最近响应到prompt
- 强调AI自主判断

### 第3步: 简化执行逻辑
- 信任Claude的决策
- 只执行不判断

---

## ⚠️ 重要原则

**代码不应该替Claude做判断！**

❌ 错误示例：
```python
# 代码硬编码检查
if "已解决" in target_messages:
    skip_notification()  # 不应该由代码判断

if last_notify_time < 30_minutes_ago:
    skip_notification()  # 不应该由代码判断
```

✅ 正确示例：
```python
# 把信息给Claude
context = {
    "target_group_messages": target_messages,
    "last_notification": notification_history
}

# 让Claude判断
decision = claude.analyze(context)

# 执行Claude的决策
if decision.action == "notify":
    send_notification()
```

---

**创建者**: Claw修理者
**下一步**: 实现上述方案
