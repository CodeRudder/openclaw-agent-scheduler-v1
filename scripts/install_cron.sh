#!/bin/bash
# 安装快速会话恢复定时任务（cron方式，每1分钟执行一次）

SCRIPT_PATH="/home/gongdewei/work/projects/code-rudder/openclaw-agent-scheduler/scripts/quick_session_recovery.py"
LOG_DIR="/home/gongdewei/work/projects/code-rudder/openclaw-agent-scheduler/logs"
CRON_JOB="* * * * * /usr/bin/python3 $SCRIPT_PATH >> $LOG_DIR/session_recovery.log 2>&1"

# 创建日志目录
mkdir -p "$LOG_DIR"

# 检查脚本是否存在
if [ ! -f "$SCRIPT_PATH" ]; then
    echo "❌ 脚本不存在: $SCRIPT_PATH"
    exit 1
fi

# 检查是否已安装
if crontab -l 2>/dev/null | grep -q "quick_session_recovery.py"; then
    echo "✅ 定时任务已安装"
    echo ""
    echo "当前配置："
    crontab -l 2>/dev/null | grep "quick_session_recovery.py"
    echo ""
    echo "如需移除，运行: ./scripts/uninstall_cron.sh"
    exit 0
fi

# 添加定时任务
(crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -

echo "✅ 定时任务安装成功！"
echo ""
echo "📋 配置信息："
echo "   脚本路径: $SCRIPT_PATH"
echo "   日志路径: $LOG_DIR/session_recovery.log"
echo "   执行频率: 每1分钟"
echo ""
echo "📝 查看日志: tail -f $LOG_DIR/session_recovery.log"
echo "📝 查看定时任务: crontab -l"
echo ""
echo "如需移除，运行: ./scripts/uninstall_cron.sh"
