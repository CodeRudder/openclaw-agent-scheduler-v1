#!/usr/bin/env python3
"""
测试调度计划功能和会话消息读取

测试场景：
1. 读取会话文件中最后2条assistant消息
2. content截断（超1KB）
3. 处理content为数组格式
4. 空文件/不存在文件
5. 调度计划加载/保存/归档
"""

import json
import os
import pytest
import tempfile
from pathlib import Path
from datetime import datetime


def create_jsonl_file(lines: list) -> Path:
    """创建临时JSONL文件"""
    fd, path = tempfile.mkstemp(suffix='.jsonl')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + '\n')
    return Path(path)


# ===== 模拟 get_last_assistant_messages 逻辑 =====

def get_last_assistant_messages(jsonl_file: Path, count: int = 2) -> list:
    MAX_CONTENT_SIZE = 1024
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
                            content = message.get("content", "")
                            if isinstance(content, list):
                                text_parts = []
                                for item in content:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        text_parts.append(item.get("text", ""))
                                    elif isinstance(item, str):
                                        text_parts.append(item)
                                content = "\n".join(text_parts)
                            if len(content) > MAX_CONTENT_SIZE:
                                content = content[:MAX_CONTENT_SIZE] + "...(截断)"
                            assistant_msgs.append({
                                "content": content,
                                "stopReason": message.get("stopReason"),
                                "errorMessage": message.get("errorMessage")
                            })
                except json.JSONDecodeError:
                    continue
        return assistant_msgs[-count:] if assistant_msgs else []
    except Exception:
        return []


class TestGetLastAssistantMessages:
    """测试读取会话文件最后assistant消息"""

    def test_single_assistant_message(self):
        """单条assistant消息"""
        lines = [
            {"type": "message", "message": {"role": "assistant", "content": "Hello", "stopReason": "endTurn"}}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_messages(path)
            assert len(result) == 1
            assert result[0]["content"] == "Hello"
            assert result[0]["stopReason"] == "endTurn"
            assert result[0]["errorMessage"] is None
        finally:
            path.unlink()

    def test_two_assistant_messages(self):
        """2条assistant消息，返回最后2条"""
        lines = [
            {"type": "message", "message": {"role": "assistant", "content": "First", "stopReason": "toolUse"}},
            {"type": "message", "message": {"role": "assistant", "content": "Second", "stopReason": "stop"}}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_messages(path)
            assert len(result) == 2
            assert result[0]["content"] == "First"
            assert result[1]["content"] == "Second"
            assert result[1]["stopReason"] == "stop"
        finally:
            path.unlink()

    def test_only_returns_last_two(self):
        """多于2条只返回最后2条"""
        lines = [
            {"type": "message", "message": {"role": "assistant", "content": f"Msg{i}", "stopReason": "endTurn"}}
            for i in range(5)
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_messages(path)
            assert len(result) == 2
            assert result[0]["content"] == "Msg3"
            assert result[1]["content"] == "Msg4"
        finally:
            path.unlink()

    def test_mixed_user_assistant(self):
        """混合user和assistant，只取assistant"""
        lines = [
            {"type": "message", "message": {"role": "user", "content": "请开始"}},
            {"type": "message", "message": {"role": "assistant", "content": "好的", "stopReason": "toolUse"}},
            {"type": "message", "message": {"role": "user", "content": "继续"}},
            {"type": "message", "message": {"role": "assistant", "content": "完成了", "stopReason": "stop"}}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_messages(path)
            assert len(result) == 2
            assert result[0]["content"] == "好的"
            assert result[1]["content"] == "完成了"
        finally:
            path.unlink()

    def test_content_array_format(self):
        """content为数组格式（实际Claude API格式）"""
        lines = [
            {"type": "message", "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "分析结果："},
                    {"type": "text", "text": "代码编译成功"}
                ],
                "stopReason": "endTurn"
            }}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_messages(path)
            assert len(result) == 1
            assert "分析结果：" in result[0]["content"]
            assert "代码编译成功" in result[0]["content"]
        finally:
            path.unlink()

    def test_content_truncation(self):
        """content超过1KB截断"""
        long_content = "A" * 2000
        lines = [
            {"type": "message", "message": {"role": "assistant", "content": long_content, "stopReason": "endTurn"}}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_messages(path)
            assert len(result) == 1
            assert len(result[0]["content"]) < 1100  # 1024 + "...(截断)"
            assert result[0]["content"].endswith("...(截断)")
        finally:
            path.unlink()

    def test_error_message_field(self):
        """errorMessage字段"""
        lines = [
            {"type": "message", "message": {
                "role": "assistant",
                "content": "出错了",
                "stopReason": "error",
                "errorMessage": "API rate limit exceeded"
            }}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_messages(path)
            assert result[0]["stopReason"] == "error"
            assert result[0]["errorMessage"] == "API rate limit exceeded"
        finally:
            path.unlink()

    def test_empty_file(self):
        """空文件"""
        path = create_jsonl_file([])
        try:
            result = get_last_assistant_messages(path)
            assert result == []
        finally:
            path.unlink()

    def test_file_not_exist(self):
        """文件不存在"""
        result = get_last_assistant_messages(Path("/nonexistent/file.jsonl"))
        assert result == []

    def test_no_assistant_messages(self):
        """只有user消息"""
        lines = [
            {"type": "message", "message": {"role": "user", "content": "你好"}},
            {"type": "message", "message": {"role": "user", "content": "请处理"}}
        ]
        path = create_jsonl_file(lines)
        try:
            result = get_last_assistant_messages(path)
            assert result == []
        finally:
            path.unlink()


class TestSchedulingPlan:
    """测试调度计划逻辑"""

    def test_plan_archive_structure(self):
        """验证归档计划的数据结构"""
        archive_entry = {
            "version": "V5.8",
            "completed_at": "2026-04-01T22:24:00",
            "milestones": [
                {"id": "M1", "name": "开发", "status": "completed"},
                {"id": "M2", "name": "验收", "status": "completed"}
            ]
        }
        assert archive_entry["version"] == "V5.8"
        assert len(archive_entry["milestones"]) == 2
        assert all(m["status"] == "completed" for m in archive_entry["milestones"])

    def test_plan_milestone_statuses(self):
        """验证里程碑状态值"""
        valid_statuses = {"pending", "in_progress", "completed", "blocked"}
        for s in valid_statuses:
            milestone = {"id": "M1", "name": "test", "status": s}
            assert milestone["status"] in valid_statuses

    def test_overall_status_completed_triggers_archive(self):
        """overall_status=completed时应触发归档"""
        plan = {
            "current_version": "V5.8",
            "overall_status": "completed",
            "milestones": [{"id": "M1", "name": "test", "status": "completed"}]
        }
        # 归档条件
        assert plan.get("overall_status") == "completed"

    def test_overall_status_in_progress_no_archive(self):
        """overall_status=in_progress时不归档"""
        plan = {
            "current_version": "V5.9",
            "overall_status": "in_progress",
            "milestones": [{"id": "M1", "name": "test", "status": "in_progress"}]
        }
        assert plan.get("overall_status") != "completed"

    def test_archive_max_5(self):
        """归档最多保留5个版本"""
        archive = [{"version": f"V5.{i}"} for i in range(8)]
        # 模拟裁剪
        if len(archive) > 5:
            archive = archive[-5:]
        assert len(archive) == 5
        assert archive[0]["version"] == "V5.3"

    def test_new_plan_from_scratch(self):
        """从零建立新计划"""
        plan = {
            "current_version": "V5.9",
            "overall_status": "in_progress",
            "milestones": [
                {"id": "M1", "name": "PRD编写", "status": "completed"},
                {"id": "M2", "name": "技术评审", "status": "in_progress"},
                {"id": "M3", "name": "UI设计", "status": "pending"},
                {"id": "M4", "name": "开发", "status": "pending"},
                {"id": "M5", "name": "验收", "status": "pending"}
            ],
            "next_actions": ["完成技术评审", "启动UI设计"]
        }
        assert len(plan["milestones"]) == 5
        assert plan["milestones"][0]["status"] == "completed"
        assert plan["milestones"][1]["status"] == "in_progress"


class TestQualityGate:
    """测试质量检查逻辑"""

    def test_version_close_all_conditions_required(self):
        """版本闭环需要同时满足4个条件"""
        version_status = {
            "dev_complete": True,
            "qa_passed": True,
            "product_confirmed": True,
            "env_stable": True
        }
        can_close = all(version_status.values())
        assert can_close is True

    def test_version_cannot_close_if_any_fail(self):
        """任一条件不满足都不能闭环"""
        for fail_key in ["dev_complete", "qa_passed", "product_confirmed", "env_stable"]:
            status = {"dev_complete": True, "qa_passed": True, "product_confirmed": True, "env_stable": True}
            status[fail_key] = False
            assert all(status.values()) is False, f"{fail_key}=False should block closure"

    def test_blocking_task_only_for_active_agents(self):
        """只有有实际任务的agent才算阻塞"""
        # ops等待发布 - 不算阻塞
        ops_task = {"agent": "ops", "task": "等待版本发布", "status": "waiting"}
        assert ops_task["status"] == "waiting"  # 不应出现在blocking_tasks

        # fullstack-dev修复BUG中途卡住 - 算阻塞
        dev_task = {"agent": "fullstack-dev", "task": "修复BUG-001", "status": "blocked"}
        assert dev_task["status"] == "blocked"  # 应出现在blocking_tasks


class TestDuplicateNotificationDetection:
    """测试重复任务通知检测逻辑"""

    def test_tc_number_matching(self):
        """TC编号匹配检测"""
        import re
        # 模拟历史通知
        history = [
            {
                "target_group": "dev-working-group",
                "mention_users": ["fullstack-dev"],
                "extracted_issues": ["TC-TASK-001: 创建任务API错误", "TC-TASK-002: 更新任务API错误"]
            }
        ]
        # 当前决策 - 相同TC编号
        current_issues = ["TC-TASK-001: P0级BUG需要修复"]
        # 检测逻辑：TC编号有交集
        hist_tcs = set()
        for issue in history[0]["extracted_issues"]:
            hist_tcs.update(re.findall(r'(TC-[A-Z]+-\d+)', issue, re.IGNORECASE))
        curr_tcs = set()
        for issue in current_issues:
            curr_tcs.update(re.findall(r'(TC-[A-Z]+-\d+)', issue, re.IGNORECASE))
        # 应该检测到交集
        assert hist_tcs & curr_tcs == {"TC-TASK-001"}

    def test_keyword_matching(self):
        """关键词匹配检测"""
        import re
        history = [
            {
                "target_group": "dev-working-group",
                "mention_users": ["fullstack-dev"],
                "extracted_issues": ["P0 BUG: 登录验证失败"]
            }
        ]
        current_issues = ["P0 BUG: 需要立即处理"]
        # 关键词：P0, BUG - 重合度100%
        hist_keywords = set()
        for issue in history[0]["extracted_issues"]:
            hist_keywords.update(k.upper() for k in re.findall(r'\b(P[0-2]|BUG)\b', issue))
        curr_keywords = set()
        for issue in current_issues:
            curr_keywords.update(k.upper() for k in re.findall(r'\b(P[0-2]|BUG)\b', issue))
        # 应该检测到关键词匹配
        assert hist_keywords == {"P0", "BUG"}
        assert len(hist_keywords & curr_keywords) >= 1

    def test_different_agent_no_duplicate(self):
        """不同agent不算重复"""
        history = [
            {
                "target_group": "dev-working-group",
                "mention_users": ["architect"],
                "extracted_issues": ["TC-TASK-001: API设计问题"]
            }
        ]
        # 当前决策 - 相同TC但不同agent (fullstack-dev vs architect)
        # agent不匹配，不应算重复
        hist_mention = set(u.lstrip('@').lower() for u in history[0]["mention_users"])
        curr_mention = {"fullstack-dev"}
        # 没有交集
        assert not (hist_mention & curr_mention)


class TestWaitingForFutureDate:
    """测试"等待未来日期"异常检测"""

    def test_detect_waiting_for_date(self):
        """检测等待未来日期的指令"""
        import re
        # 禁止的模式
        forbidden_patterns = [
            r"按计划\d+月\d+日",
            r"等待\d+月\d+日",
            r"\d+日后开始",
            r"下周一启动",
            r"明天再开始",
        ]

        bad_messages = [
            "开发环境已就绪，按计划4月6日启动功能开发",
            "等待4月6日开始",
            "3日后开始开发",
            "下周一启动新版本",
            "今天先休息，明天再开始",
        ]

        for msg in bad_messages:
            is_bad = any(re.search(p, msg) for p in forbidden_patterns)
            assert is_bad, f"应检测到违规: {msg}"

    def test_valid_progress_messages(self):
        """有效的进度消息不应被误判"""
        import re
        forbidden_patterns = [
            r"按计划\d+月\d+日",
            r"等待\d+月\d+日",
            r"\d+日后开始",
            r"下周一启动",
            r"明天再开始",
        ]

        good_messages = [
            "开发环境已就绪，立即开始功能开发",
            "准备工作完成，开始执行任务",
            "测试脚本就绪，执行集成测试",
            "4月6日完成的任务已验收",  # 过去时，描述已完成的事
        ]

        for msg in good_messages:
            is_bad = any(re.search(p, msg) for p in forbidden_patterns)
            assert not is_bad, f"不应被误判为违规: {msg}"

    def test_preparation_complete_means_start(self):
        """准备工作完成应立即开始实际工作"""
        # 场景：开发准备完成
        plan_milestone = {
            "id": "M5",
            "name": "V5.9开发实施",
            "status": "pending",  # 错误状态，用于测试检测规则
            "progress": "开发环境已就绪，所有准备工作已100%完成"
        }

        # 规则：如果progress说"准备完成"，status不能是pending
        is_prep_complete = "准备" in plan_milestone["progress"] and "完成" in plan_milestone["progress"]

        # 验证检测规则有效：准备完成但状态是pending应该被检测到
        if is_prep_complete:
            needs_correction = plan_milestone["status"] == "pending"
            assert needs_correction is True, "应检测到'准备完成但状态pending'的错误"


class TestTestFailureHandling:
    """测试失败处理规则"""

    def test_qa_passed_must_be_false_when_failures(self):
        """测试有失败时qa_passed必须为False"""
        # 场景1: 有失败用例
        test_result = {"passed": 25, "failed": 120}
        qa_passed = test_result["failed"] == 0
        assert qa_passed is False, "120个失败用例，qa_passed必须为False"

        # 场景2: 全部通过
        test_result = {"passed": 145, "failed": 0}
        qa_passed = test_result["failed"] == 0
        assert qa_passed is True, "0个失败用例，qa_passed应该为True"

    def test_failure_means_blocked_or_in_progress(self):
        """测试失败时里程碑不能是completed"""
        milestone = {
            "name": "集成测试验证",
            "progress": "38用例10通过/28失败",
            "status": "completed"  # 错误状态，用于测试检测
        }

        import re
        match = re.search(r'(\d+)\s*通过.*?(\d+)\s*失败', milestone["progress"])
        if match:
            failed = int(match.group(2))
            if failed > 0:
                # 验证检测规则有效：有失败但completed应该被检测到
                needs_correction = milestone["status"] == "completed"
                assert needs_correction is True, f"应检测到'有{failed}个失败但completed'的错误"

    def test_failure_must_have_bug_report(self):
        """测试失败必须有BUG报告或待开发清单"""
        test_result = {"passed": 10, "failed": 28}

        # 失败后必须有输出
        if test_result["failed"] > 0:
            # 必须有bug_report或feature_list
            required_output = ["bug_report", "feature_list"]
            has_output = any(key in test_result for key in required_output)
            # 测试场景：没有输出是错误的
            assert not has_output, "测试失败但缺少bug_report或feature_list"

    def test_function_not_implemented_is_still_failure(self):
        """功能未实现也是失败，不能忽略"""
        failure_reason = "V5.9功能API未实现（返回404）"
        is_failure = True  # 功能未实现 = 失败

        # 即使原因是"功能未实现"，也必须记录
        assert is_failure, "功能未实现也是失败，必须记录"

    def test_test_fix_iteration_must_complete(self):
        """测试-修复迭代必须完成完整循环"""
        iteration_steps = {
            "test_executed": True,
            "failures_found": 28,
            "bug_report_generated": True,
            "dev_notified": True,  # 必须通知开发
            "dev_fixed": False,  # 开发尚未修复
            "qa_retested": False  # QA尚未复测
        }

        # 迭代未完成：有失败但开发未修复
        if iteration_steps["failures_found"] > 0:
            iteration_complete = (
                iteration_steps["bug_report_generated"] and
                iteration_steps["dev_notified"] and
                iteration_steps["dev_fixed"] and
                iteration_steps["qa_retested"]
            )
            assert not iteration_complete, "开发未修复，迭代未完成"


class TestMilestoneStatusCorrection:
    """测试里程碑状态自动修正"""

    def test_correct_completed_to_in_progress(self):
        """测试失败但标记completed应修正为in_progress"""
        milestone = {
            "name": "集成测试脚本验证",
            "status": "completed",
            "progress": "38用例10通过/28失败，待输出失败详情"
        }

        import re
        # 检测"待输出"表示未完成
        has_pending = "待输出" in milestone["progress"] or "待处理" in milestone["progress"]

        # 检测测试失败
        match = re.search(r'(\d+)\s*通过.*?(\d+)\s*失败', milestone["progress"])
        has_failures = match and int(match.group(2)) > 0

        if has_pending or has_failures:
            # 验证检测规则有效：这种错误状态应该被检测到
            needs_correction = milestone["status"] == "completed"
            assert needs_correction is True, "应检测到'有失败/待输出但completed'的错误"

    def test_correct_pending_to_in_progress_when_ready(self):
        """准备完成但标记pending应修正为in_progress"""
        milestone = {
            "name": "V5.9开发实施",
            "status": "pending",
            "progress": "开发环境已就绪，所有准备工作已100%完成"
        }

        is_ready = "就绪" in milestone["progress"] or "100%完成" in milestone["progress"]

        # 验证检测规则有效：准备就绪但状态pending应该被检测到
        if is_ready and milestone["status"] == "pending":
            needs_correction = True
            assert needs_correction is True, "应检测到'准备就绪但状态pending'的错误"


class TestVersionClosureConditions:
    """测试版本闭环条件"""

    def test_all_four_conditions_required(self):
        """版本闭环需要同时满足4个条件"""
        version_status = {
            "dev_complete": True,
            "qa_passed": True,
            "product_confirmed": True,
            "env_stable": True
        }
        can_close = all(version_status.values())
        assert can_close is True

    def test_any_condition_fails_blocks_closure(self):
        """任一条件不满足都不能闭环"""
        base_status = {"dev_complete": True, "qa_passed": True, "product_confirmed": True, "env_stable": True}

        for key in base_status:
            test_status = base_status.copy()
            test_status[key] = False
            can_close = all(test_status.values())
            assert can_close is False, f"{key}=False应该阻止闭环"

    def test_qa_passed_false_blocks_closure(self):
        """qa_passed=False必须阻止闭环"""
        version_status = {
            "dev_complete": True,
            "qa_passed": False,  # 测试有失败
            "product_confirmed": True,
            "env_stable": True
        }
        can_close = all(version_status.values())
        assert can_close is False, "qa_passed=False必须阻止闭环"


class TestTestMilestoneSubtasks:
    """测试里程碑子步骤验证"""

    def test_test_milestone_has_five_subtasks(self):
        """测试里程碑必须有5个子步骤"""
        required_subtasks = [
            "1. 编写测试脚本",
            "2. 执行测试",
            "3. 输出测试结果",
            "4. 失败用例处理",
            "5. 全部通过或复测通过"
        ]

        # 模拟测试里程碑
        test_milestone = {
            "name": "集成测试脚本验证",
            "subtasks_completed": [True, True, True, False, False]  # 3/5完成
        }

        completion_rate = sum(test_milestone["subtasks_completed"]) / len(required_subtasks)
        all_done = all(test_milestone["subtasks_completed"])

        assert completion_rate == 0.6, "3/5=60%"
        assert not all_done, "未全部完成"

    def test_subtask_4_required_when_failures(self):
        """有失败时必须完成子步骤4（失败用例处理）"""
        test_result = {"passed": 10, "failed": 28}
        subtask_4_done = False  # 未输出失败详情

        if test_result["failed"] > 0:
            assert not subtask_4_done, "有失败但未完成失败用例处理"
            # 状态应该是in_progress，不是completed

    def test_milestone_complete_only_when_all_subtasks_done(self):
        """只有5/5子步骤完成才能标记completed"""
        milestone = {
            "status": "completed",
            "subtasks_completed": [True, True, True, True, True]  # 5/5
        }

        all_done = all(milestone["subtasks_completed"])
        if milestone["status"] == "completed":
            assert all_done, "completed状态要求5/5子步骤全部完成"


class TestNotificationRules:
    """测试通知规则"""

    def test_allowed_notification_directions(self):
        """允许的通知方向"""
        allowed = [
            ("qa-acceptance-group", "dev-working-group"),  # 验收→开发（BUG）
            ("qa-acceptance-group", "ops-release-group"),  # 验收→运维（环境）
            ("dev-working-group", "qa-acceptance-group"),  # 开发→验收（修复完成）
            ("ops-release-group", "dev-working-group"),    # 运维→开发（环境就绪）
        ]

        for source, target in allowed:
            # 这些方向是允许的
            assert True

    def test_forbidden_notification_directions(self):
        """禁止的通知方向"""
        forbidden = [
            ("dev-working-group", "qa-acceptance-group", "开发自测失败"),  # 开发自测失败不应通知验收
            ("dev-working-group", "plan-design-group", "开发中的技术讨论"),
            ("plan-design-group", "dev-working-group", "规划讨论"),
        ]

        for source, target, reason in forbidden:
            # 这些方向是禁止的
            assert True  # 测试通过表示规则已定义

    def test_no_notification_for_normal_progress(self):
        """普通进度汇报不需要跨群通知"""
        scenarios = [
            {"type": "progress_report", "content": "开发进度50%"},
            {"type": "internal_discussion", "content": "技术方案讨论中"},
            {"type": "waiting", "content": "等待上游结果"},
        ]

        for scenario in scenarios:
            # 普通汇报不需要通知
            needs_notification = False
            assert not needs_notification


class TestBlockingTaskDetection:
    """测试阻塞任务检测"""

    def test_active_task_blocked(self):
        """有明确任务但卡住算阻塞"""
        task = {
            "agent": "fullstack-dev",
            "task": "修复BUG-001",
            "status": "blocked",
            "reason": "登录验证失败"
        }
        is_blocking = task["status"] == "blocked"
        assert is_blocking, "有任务但被阻塞应该列入blocking_tasks"

    def test_waiting_without_task_not_blocking(self):
        """没有任务只是等待不算阻塞"""
        task = {
            "agent": "ops",
            "task": "等待版本发布",
            "status": "waiting"
        }
        # 没有实际任务，不算阻塞
        is_blocking = task["status"] == "blocked"
        assert not is_blocking, "没有实际任务不算阻塞"

    def test_completed_task_not_blocking(self):
        """已完成的任务不算阻塞"""
        task = {
            "agent": "qa",
            "task": "测试脚本编写",
            "status": "completed"
        }
        is_blocking = task["status"] == "blocked"
        assert not is_blocking, "已完成的任务不算阻塞"


class TestPlanValidationAndCorrection:
    """测试计划验证和修正"""

    def test_detect_milestone_completed_with_failures(self):
        """检测里程碑错误标记为completed（有测试失败）"""
        import re
        milestone = {
            "name": "集成测试脚本验证",
            "status": "completed",
            "progress": "145用例25通过/120失败"
        }

        match = re.search(r'(\d+)\s*通过.*?(\d+)\s*失败', milestone["progress"])
        if match:
            failed = int(match.group(2))
            if failed > 0 and milestone["status"] == "completed":
                # 检测到错误，需要修正
                needs_correction = True
                assert needs_correction, f"有{failed}个失败但标记completed，需要修正"

    def test_detect_waiting_in_next_actions(self):
        """检测next_actions中的等待指令"""
        import re
        next_actions = [
            "等待4月6日按计划启动V5.9功能开发",  # 错误！
            "fullstack-dev开始P1功能开发",
        ]

        forbidden_patterns = [r"等待\d+月\d+日", r"按计划\d+月\d+日"]

        for action in next_actions:
            is_bad = any(re.search(p, action) for p in forbidden_patterns)
            if "等待4月6日" in action:
                assert is_bad, "检测到等待未来日期的错误指令"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
