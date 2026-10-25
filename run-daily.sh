#!/bin/bash
# ZaiZaiCat-Checkin 定时任务包装脚本
#
# 用法：
#   ./run-daily.sh                 # 跑全部已配置平台
#   ./run-daily.sh trae workbuddy  # 只跑指定平台
#
# 约定：
#   - flock 防并发（定时任务与手动执行互斥）
#   - 逐日日志由 run_all.py 自己写（logs/checkin-YYYYMMDD.log）
#   - 本脚本只负责环境准备 + 兜底输出到 cron.log

set -u

PROJECT_DIR="/root/ZaiZaiCat-Checkin"
cd "$PROJECT_DIR" || exit 1

export TZ="${TZ:-Asia/Shanghai}"
export PYTHONUNBUFFERED="1"

mkdir -p "$PROJECT_DIR/logs" "$PROJECT_DIR/.lock"

# 防并发：定时任务重叠时后到者直接退出
exec 9>"$PROJECT_DIR/.lock/run_daily.lock"
if ! flock -n 9; then
    echo "[run-daily] $(date '+%F %T') 已有任务在运行，本次跳过"
    exit 0
fi

# cron 环境变量极少，显式指定解释器与 PATH
PYTHON_BIN="$(command -v python3)"
export PATH="/usr/local/bin:/usr/bin:/bin:${PATH:-}"

echo "===== $(date '+%F %T') 签到开始 ====="

"$PYTHON_BIN" "$PROJECT_DIR/run_all.py" "$@"
RC=$?

echo "===== $(date '+%F %T') 签到结束 (exit=$RC) ====="
exit "$RC"
