#!/bin/bash
# 移除快速会话恢复定时任务

# 从crontab中移除
crontab -l 2>/dev/null | grep -v "quick_session_recovery.py" | crontab -

echo "✅ 定时任务已移除"
