#!/usr/bin/env python3
"""
Mock调度器测试框架

Mock所有外部依赖，使调度流程可以在无外部服务环境下测试：
- Mattermost API (requests.get/post)
- LLM API (requests.post → LiteLLM)
- 飞书API (requests.post)
- 文件I/O (json.load/dump)
- Agent会话文件 (JSONL)

用法：
  from mock_scheduler import MockScheduler, MockDataFactory

  scheduler = MockScheduler(tmp_dir)
  scheduler.set_mm_response(channel_id, messages)
  scheduler.set_llm_response(decisions, analysis, updated_plan)
  scheduler.run()
"""

import json
import os
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
from unittest.mock import patch, MagicMock
from dataclasses import dataclass

# ===== Mock Response =====

class MockResponse:
    """模拟requests.Response"""

    def __init__(self, json_data=None, status_code=200, text=""):
        self._json = json_data or {}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


# ===== Mock数据工厂 =====

class MockDataFactory:
    """生成各类mock测试数据"""

    @staticmethod
    def mm_messages(count: int = 5, group_id: str = "dev-working-group",
                    senders: list = None, contents: list = None) -> dict:
        """生成Mattermost消息列表响应"""
        if senders is None:
            senders = ["fullstack-dev", "claw-admin", "architect"]
        if contents is None:
            contents = [
                "BUG修复完成，已提交代码",
                "请继续修复剩余BUG",
                "架构评审通过"
            ]

        posts = []
        base_time = datetime.now().timestamp()
        for i in range(count):
            posts.append({
                "id": f"msg_{i:04d}",
                "create_at": int((base_time - i * 60) * 1000),
                "user_id": f"user_{senders[i % len(senders)]}",
                "channel_id": f"channel_{group_id}",
                "message": contents[i % len(contents)],
                "props": {}
            })

        return {
            "order": [p["id"] for p in posts],
            "posts": {p["id"]: p for p in posts},
            "next_post_id": ""
        }

    @staticmethod
    def mm_user(username: str = "fullstack-dev", user_id: str = None) -> dict:
        """生成Mattermost用户响应"""
        return {
            "id": user_id or f"user_{username}",
            "username": username,
            "nickname": username,
            "first_name": "",
            "last_name": ""
        }

    @staticmethod
    def llm_response(decisions: list = None, analysis: dict = None,
                     updated_plan: dict = None) -> dict:
        """生成LLM API响应

        Args:
            decisions: 决策列表，None表示空列表
            analysis: 分析结果
            updated_plan: 更新的计划
        """
        if decisions is None:
            decisions = []
        if analysis is None:
            analysis = {
                "current_version": "V5.9",
                "overall_progress": "开发修复中",
                "tasks": [],
                "blockers": [],
                "version_status": {
                    "dev_complete": False,
                    "qa_passed": False,
                    "product_confirmed": False,
                    "env_stable": True
                },
                "blocking_tasks": []
            }
        if updated_plan is None:
            updated_plan = {
                "current_version": "V5.9",
                "overall_status": "in_progress",
                "milestones": [
                    {"id": "M1", "name": "V5.9开发", "status": "in_progress",
                     "progress": "开发中", "assigned_to": "fullstack-dev"}
                ],
                "next_actions": ["继续开发"]
            }

        content = json.dumps({
            "analysis": analysis,
            "decisions": decisions,
            "updated_plan": updated_plan
        }, ensure_ascii=False)

        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": content
                },
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300}
        }

    @staticmethod
    def decision(target_group: str = "dev-working-group",
                 target_group_name: str = "开发工作群",
                 mention_users: list = None,
                 extracted_issues: list = None,
                 message_content: str = "",
                 reasoning: str = "测试决策",
                 source_group: str = "qa-acceptance-group",
                 action: str = "notify") -> dict:
        """生成单个决策数据"""
        return {
            "action": action,
            "target_group": target_group,
            "target_group_name": target_group_name,
            "mention_users": mention_users or ["fullstack-dev"],
            "extracted_issues": extracted_issues or ["TC-TEST-001: 测试用例失败"],
            "message_content": message_content or "请修复测试用例",
            "reasoning": reasoning,
            "source_group": source_group,
            "raw_messages": "",
            "qa_raw_messages": "",
            "bug_doc_complete": True
        }

    @staticmethod
    def session_jsonl(assistant_messages: list = None) -> str:
        """生成JSONL会话内容

        Args:
            assistant_messages: assistant消息列表，每项是dict
                [{"content": "...", "stopReason": "stop", "timestamp": 1234}, ...]
        """
        if assistant_messages is None:
            assistant_messages = [
                {"content": "任务处理中...", "stopReason": "endTurn"}
            ]

        lines = []
        for msg in assistant_messages:
            if isinstance(msg, str):
                msg = {"content": msg, "stopReason": "endTurn"}

            lines.append(json.dumps({
                "type": "message",
                "timestamp": msg.get("timestamp", int(datetime.now().timestamp() * 1000)),
                "message": {
                    "role": msg.get("role", "assistant"),
                    "content": msg.get("content", ""),
                    "stopReason": msg.get("stopReason", "endTurn"),
                    "errorMessage": msg.get("errorMessage")
                }
            }, ensure_ascii=False))

        return "\n".join(lines)

    @staticmethod
    def plan(milestones: list = None, version: str = "V5.9",
             status: str = "in_progress") -> dict:
        """生成调度计划"""
        if milestones is None:
            milestones = [
                {"id": "M1", "name": "V5.9开发", "status": "in_progress",
                 "progress": "开发中", "assigned_to": "fullstack-dev",
                 "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M")},
                {"id": "M2", "name": "V5.9验收", "status": "pending",
                 "progress": "等待开发完成", "assigned_to": "qa",
                 "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
            ]
        return {
            "current_version": version,
            "overall_status": status,
            "milestones": milestones,
            "next_actions": [],
            "last_updated": datetime.now().isoformat()
        }

    @staticmethod
    def notification_history(entries: list = None) -> dict:
        """生成通知历史"""
        if entries is None:
            entries = []
        return {"history": entries}


# ===== Mock调度器 =====

class MockScheduler:
    """Mock调度器 - 模拟所有外部依赖

    用法：
        mock = MockScheduler(tmp_dir)
        mock.setup_active_plan()  # 设置有活跃任务的计划
        mock.set_session_stop_reason("fullstack-dev", "stop")  # 设置会话状态
        mock.set_llm_decisions([])  # 设置LLM返回的决策
        mock.run()  # 执行调度
        assert mock.sent_posts["channel_id"] == [...]  # 验证发送的消息
    """

    def __init__(self, tmp_dir: str):
        self.tmp_dir = Path(tmp_dir)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

        # 数据目录
        self.data_dir = self.tmp_dir / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 会话目录
        self.sessions_dir = self.tmp_dir / "agents"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        # 记录发送的消息
        self.sent_posts = {}  # channel_id -> [messages]
        self.sent_feishu = []  # 飞书消息列表

        # 预定义响应
        self._mm_responses = {}  # url_pattern -> response
        self._llm_response = None
        self._feishu_response = MockResponse({"code": 0, "msg": "ok"})

        # mock patches
        self._patches = []

    def setup(self):
        """启动所有mock"""
        self._setup_data_files()

    def teardown(self):
        """停止所有mock"""
        # 清理引用，但不恢复module（由restore_module负责）
        pass

    def __enter__(self):
        self.setup()
        return self

    def __exit__(self, *args):
        self.teardown()

    # ===== 配置方法 =====

    def setup_active_plan(self, milestones: list = None):
        """设置有活跃任务的计划"""
        plan = MockDataFactory.plan(milestones=milestones)
        self._write_json(self.data_dir / "scheduling_plan.json", plan)
        return plan

    def setup_completed_plan(self):
        """设置已完成的计划"""
        plan = MockDataFactory.plan(
            status="completed",
            milestones=[
                {"id": "M1", "name": "V5.8开发", "status": "completed",
                 "progress": "完成", "assigned_to": "fullstack-dev"}
            ]
        )
        self._write_json(self.data_dir / "scheduling_plan.json", plan)
        return plan

    def setup_empty_plan(self):
        """设置空计划"""
        self._write_json(self.data_dir / "scheduling_plan.json", {})

    def setup_notification_history(self, entries: list = None):
        """设置通知历史"""
        history = MockDataFactory.notification_history(entries)
        self._write_json(self.data_dir / "notification_history.json", history)

    def create_session(self, agent_name: str, messages: list = None,
                       stop_reason: str = "endTurn", session_id: str = None):
        """创建agent会话文件"""
        if session_id is None:
            import uuid
            session_id = str(uuid.uuid4())

        session_dir = self.sessions_dir / agent_name / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)

        if messages is None:
            messages = [{"content": f"{agent_name}处理任务中", "stopReason": stop_reason}]

        content = MockDataFactory.session_jsonl(messages)
        session_file = session_dir / f"{session_id}.jsonl"
        session_file.write_text(content, encoding='utf-8')
        return session_file

    def set_session_stop_reason(self, agent_name: str, stop_reason: str,
                                error_message: str = None):
        """快捷设置agent会话的stopReason"""
        msg = {"content": f"agent最后消息", "stopReason": stop_reason}
        if error_message:
            msg["errorMessage"] = error_message
        self.create_session(agent_name, messages=[msg])

    def set_mm_messages(self, channel_id: str, count: int = 5,
                        senders: list = None, contents: list = None):
        """设置群的MM消息响应"""
        response = MockDataFactory.mm_messages(count, senders=senders, contents=contents)
        self._mm_responses[f"posts_{channel_id}"] = response

    def set_llm_response(self, decisions: list = None, analysis: dict = None,
                         updated_plan: dict = None):
        """设置LLM API响应"""
        self._llm_response = MockDataFactory.llm_response(decisions, analysis, updated_plan)

    def set_llm_decisions(self, decisions: list):
        """快捷设置LLM决策"""
        self.set_llm_response(decisions=decisions)

    def set_llm_error(self):
        """设置LLM API错误"""
        self._llm_response = MockResponse({}, status_code=500)

    def set_mm_error(self, channel_id: str = None):
        """设置MM API错误"""
        if channel_id:
            self._mm_responses[f"posts_{channel_id}"] = MockResponse({}, status_code=500)
        else:
            self._mm_responses["error_all"] = True

    # ===== 内部方法 =====

    def _write_json(self, path: Path, data: dict):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _read_json(self, path: Path) -> dict:
        if path.exists():
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, ValueError):
                return {}
        return {}

    def _setup_data_files(self):
        """初始化数据文件"""
        if not (self.data_dir / "notification_history.json").exists():
            self._write_json(self.data_dir / "notification_history.json", {"history": []})
        if not (self.data_dir / "scheduling_plan.json").exists():
            self._write_json(self.data_dir / "scheduling_plan.json", {})

    def _handle_request(self, method: str, url: str, **kwargs):
        """处理mock请求"""
        if method == "POST":
            json_data = kwargs.get("json", {})
            # Mattermost发消息
            if "/api/v4/posts" in url:
                channel_id = json_data.get("channel_id", "")
                message = json_data.get("message", "")
                if channel_id not in self.sent_posts:
                    self.sent_posts[channel_id] = []
                self.sent_posts[channel_id].append(message)
                return MockResponse({"id": "mock_post_id", "status": "ok"})

            # 飞书发消息
            if "feishu.cn" in url:
                self.sent_feishu.append(json_data)
                return self._feishu_response

            # LLM API
            if "/v1/chat/completions" in url:
                if self._llm_response is None:
                    self.set_llm_response()
                if isinstance(self._llm_response, MockResponse):
                    return self._llm_response
                return MockResponse(self._llm_response)

        elif method == "GET":
            # Mattermost获取消息
            if "/api/v4/posts" in url:
                for key, resp in self._mm_responses.items():
                    if key.startswith("posts_"):
                        if isinstance(resp, MockResponse):
                            return resp
                        return MockResponse(resp)
                # 默认空消息
                return MockResponse(MockDataFactory.mm_messages(0))

            # Mattermost获取用户
            if "/api/v4/users/" in url:
                username = url.split("/users/")[-1].split("/")[0]
                return MockResponse(MockDataFactory.mm_user(username))

            return MockResponse({})

        return MockResponse({})

    # ===== 创建调度器 =====

    def get_scheduler_with_mock(self):
        """创建调度器并替换外部依赖

        返回 (scheduler, module) 用于后续restore_module()
        """
        from scripts.claude_driven_scheduler import ClaudeDrivenScheduler
        import scripts.claude_driven_scheduler as sched_module

        # 保存原始值
        self._originals = {
            "PLAN_FILE": sched_module.PLAN_FILE,
            "HISTORY_FILE": sched_module.HISTORY_FILE,
            "requests": sched_module.requests,
        }

        # 替换为mock值
        sched_module.PLAN_FILE = self.data_dir / "scheduling_plan.json"
        sched_module.HISTORY_FILE = self.data_dir / "notification_history.json"

        import requests as real_requests
        mock_req = MagicMock()
        mock_req.get = lambda url, **kwargs: self._handle_request("GET", url, **kwargs)
        mock_req.post = lambda url, **kwargs: self._handle_request("POST", url, **kwargs)
        mock_req.HTTPError = real_requests.HTTPError
        sched_module.requests = mock_req

        # 创建调度器（此时读取mock的文件）
        scheduler = ClaudeDrivenScheduler()

        return scheduler, sched_module

    def restore_module(self, sched_module):
        """恢复模块原始值"""
        for key, value in self._originals.items():
            setattr(sched_module, key, value)

    # ===== 验证辅助 =====

    def get_plan(self) -> dict:
        """获取当前调度计划"""
        return self._read_json(self.data_dir / "scheduling_plan.json")

    def get_history(self) -> dict:
        """获取通知历史"""
        return self._read_json(self.data_dir / "notification_history.json")

    def get_sent_mm_posts(self, channel_id: str = None) -> list:
        """获取发送的MM消息"""
        if channel_id:
            return self.sent_posts.get(channel_id, [])
        all_posts = []
        for posts in self.sent_posts.values():
            all_posts.extend(posts)
        return all_posts

    def get_sent_feishu_posts(self) -> list:
        """获取发送的飞书消息"""
        return self.sent_feishu
