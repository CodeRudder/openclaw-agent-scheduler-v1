# AI驱动调度系统 - 修复总结

## 🔴 核心问题
**我把智能决策又变回了规则驱动！**

在代码中硬编码了：
- ❌ `_check_issue_resolved()`: 检查目标群是否已解决
- ❌ `_can_notify()`: 30分钟去重检查
- ❌ 在`run()`中预过滤：如果目标群已解决就跳过分析

**结果**: Claude只看到过滤后的信息，无法做真正的智能决策

---

## ✅ 正确方案

### 原则
- **代码职责**: 获取数据、准备上下文、执行决策、记录结果
- **Claude职责**: 分析问题、检查状态、判断是否通知、生成内容

### 修改点

#### 1. 增强SYSTEM_PROMPT ✅ 已完成
```
添加:
- "你会收到的完整上下文"说明
- "智能决策规则（由你判断）"
- 历史通知和目标群状态的概念
```

#### 2. 简化run()方法 ⏳ 待实施
```python
# ❌ 删除硬编码检查
- if self._check_issue_resolved(target, target_messages): skip
- if last_time < 30_minutes_ago: skip

# ✅ 准备完整上下文
target_groups_status = {...}  # 所有目标群的最近消息
notification_history = {...}  # 历史通知记录

# ✅ 让Claude看到所有信息
decision = self.analyze_with_claude(
    messages,
    context,
    target_groups_status,  # 新增
    notification_history   # 新增
)

# ✅ 信任Claude决策
if decision.action == "notify":
    send_notification()
```

#### 3. 修改analyze_with_claude() ⏳ 待实施
```python
def analyze_with_claude(self, messages, context,
                       target_groups_status=None,
                       notification_history=None):
    """让Claude看到完整信息"""

    # 构建完整prompt
    prompt = f"""
    ## 当前群组消息
    {messages}

    ## 目标群最新状态
    {target_groups_status}

    ## 历史通知记录
    {notification_history}

    ## 任务
    请根据完整信息判断：
    1. 是否存在阻塞问题
    2. 目标群是否已响应
    3. 是否最近通知过
    4. 综合决策
    """

    # Claude自己判断
    return claude_decision
```

---

## 📊 对比

### 修复前（错误）
```
代码检查 → 过滤信息 → Claude只看到部分 → 可能重复通知
```

### 修复后（正确）
```
准备完整上下文 → Claude看到所有信息 → AI智能判断 → 正确决策
```

---

## 🎯 下一步

### 选项1: 快速修复
只修改run()和analyze_with_claude()，保留现有结构

### 选项2: 重新实现
创建新的v2版本，清晰分离职责

---

## 💡 关键洞察

**硬编码规则 vs AI决策**

硬编码:
```python
if "已解决" in messages:
    return "ignore"  # 代码判断
```

AI决策:
```python
context = "目标群反馈: 已解决"
decision = claude.analyze(context)  # Claude判断
# Claude可能返回: "ignore, 因为目标群已反馈解决"
# 或者: "notify, 虽然目标群说解决但问题仍存在"
```

**关键**: 让AI看到完整信息，自己做判断！

---

**创建时间**: 2026-03-29 18:30
**状态**: 设计完成，待实施
