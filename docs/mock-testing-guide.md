# Mock测试规范指南

## 概述

本项目使用Mock测试框架来隔离外部依赖，确保调度器核心逻辑可以在无外部服务环境下进行单元测试和集成测试。

## Mock框架架构

```
tests/
├── mock_scheduler.py      # Mock基础设施
│   ├── MockResponse       # HTTP响应模拟
│   ├── MockDataFactory    # 测试数据工厂
│   └── MockScheduler      # Mock调度器环境
├── test_scheduler_flow.py # 调度流程测试
├── test_scheduling_plan.py # 调度计划逻辑测试
└── test_stop_reason.py    # 会话状态解析测试
```

## 核心组件

### 1. MockResponse

模拟 `requests.Response` 对象：

```python
response = MockResponse({"key": "value"}, status_code=200)
data = response.json()  # {"key": "value"}
response.raise_for_status()  # status_code >= 400 时抛出异常
```

### 2. MockDataFactory

生成各类测试数据：

```python
# Mattermost消息
mm_data = MockDataFactory.mm_messages(count=5, senders=["user1", "user2"])

# LLM响应
llm_data = MockDataFactory.llm_response(
    decisions=[...],
    analysis={...},
    updated_plan={...}
)

# 会话JSONL
jsonl_content = MockDataFactory.session_jsonl([
    {"content": "处理中", "stopReason": "endTurn"},
    {"content": "完成", "stopReason": "stop"}
])

# 调度计划
plan = MockDataFactory.plan(milestones=[...], version="V5.9")
```

### 3. MockScheduler

创建隔离的测试环境：

```python
with MockScheduler(tmp_dir) as mock:
    # 设置测试数据
    mock.setup_active_plan()
    mock.set_session_stop_reason("fullstack-dev", "stop")
    mock.set_llm_decisions([...])

    # 创建调度器
    scheduler, module = mock.get_scheduler_with_mock()

    # 执行测试
    scheduler.run()

    # 验证结果
    assert len(mock.get_sent_mm_posts()) > 0

    # 恢复模块
    mock.restore_module(module)
```

## Mock的外部依赖

| 依赖 | 原始调用 | Mock方式 |
|------|---------|---------|
| Mattermost API | `requests.get/post` → MM_URL | `MockScheduler._handle_request()` |
| LLM API | `requests.post` → LITELLM_URL | `MockScheduler._handle_request()` |
| 飞书API | `requests.post` → feishu.cn | `MockScheduler._handle_request()` |
| 调度计划文件 | PLAN_FILE | 重定向到 tmp_dir/data/ |
| 通知历史文件 | HISTORY_FILE | 重定向到 tmp_dir/data/ |
| Agent会话文件 | ~/.openclaw/agents/ | 重定向到 tmp_dir/agents/ |

## 编写测试规范

### 1. 使用fixture创建环境

```python
@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d

@pytest.fixture
def mock_env(tmp_dir):
    with MockScheduler(tmp_dir) as m:
        yield m
```

### 2. 测试结构

```python
class TestFeatureName:
    """功能描述"""

    def test_normal_case(self, mock_env):
        """正常情况测试"""
        mock_env.setup_active_plan()
        scheduler, module = mock_env.get_scheduler_with_mock()
        try:
            # 执行测试
            result = scheduler.some_method()
            # 验证结果
            assert result is True
        finally:
            mock_env.restore_module(module)

    def test_edge_case(self, mock_env):
        """边界情况测试"""
        ...
```

### 3. 必须清理资源

```python
scheduler, module = mock.get_scheduler_with_mock()
try:
    # 测试代码
    ...
finally:
    mock.restore_module(module)  # 必须调用，恢复模块状态
```

### 4. 验证发送的消息

```python
# 获取所有发送的MM消息
all_posts = mock.get_sent_mm_posts()

# 获取特定频道的消息
channel_posts = mock.get_sent_mm_posts("channel_id")

# 获取飞书消息
feishu_posts = mock.get_sent_feishu_posts()
```

## 测试分类

### 单元测试
- `test_stop_reason.py`: 会话stopReason解析逻辑
- `test_scheduling_plan.py`: 调度计划数据结构和规则验证

### 集成测试
- `test_scheduler_flow.py`: 调度器完整流程测试

## 运行测试

```bash
# 运行所有测试
python3 -m pytest tests/ -v

# 运行特定测试文件
python3 -m pytest tests/test_scheduler_flow.py -v

# 运行特定测试类
python3 -m pytest tests/test_scheduler_flow.py::TestPlanManagement -v

# 运行特定测试方法
python3 -m pytest tests/test_scheduler_flow.py::TestPlanManagement::test_load_plan -v
```

## 新增测试检查清单

- [ ] 使用 `MockScheduler` 创建隔离环境
- [ ] 使用 `try/finally` 确保调用 `restore_module()`
- [ ] 使用 `MockDataFactory` 生成测试数据，不硬编码
- [ ] 验证外部调用（MM/飞书消息）通过 `get_sent_mm_posts()` 等方法
- [ ] 测试类和方法的命名清晰描述测试内容
- [ ] 添加适当的日志输出便于调试

## 注意事项

1. **不要直接导入调度器模块**：调度器模块在导入时会加载配置文件和设置日志，应通过 `MockScheduler.get_scheduler_with_mock()` 创建实例。

2. **不要修改全局状态**：所有文件操作应在临时目录进行，测试结束后自动清理。

3. **不要硬编码测试数据**：使用 `MockDataFactory` 生成符合格式的测试数据。

4. **Mock请求匹配**：`_handle_request` 根据URL特征判断请求类型，确保mock响应正确匹配。

## 扩展Mock功能

当需要mock新的外部依赖时：

1. 在 `MockDataFactory` 添加数据生成方法
2. 在 `MockScheduler._handle_request()` 添加请求处理逻辑
3. 在 `MockScheduler` 添加配置方法（如 `set_xxx_response()`）
4. 更新本文档
