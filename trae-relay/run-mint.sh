#!/usr/bin/env bash
# Trae CN relay 的 JWT 续期包装（供 cron 调用）
#
# 为什么需要：relay 镜像只支持 refreshToken 自动续期，而本仓库只有 sessionid；
# sessionid -> JWT 的链路得由本机脚本承担。JWT 仅约 8 小时有效，
# 故每小时刷新一次（cron: 5 * * * *），留足余量。
#
# 日志：/root/ZaiZaiCat-Checkin/logs/trae-relay.log
set -uo pipefail

BASE="/root/ZaiZaiCat-Checkin"
LOG_DIR="$BASE/logs"
LOG="$LOG_DIR/trae-relay.log"

mkdir -p "$LOG_DIR"
echo "===== $(date '+%F %T') 刷新 Trae CN JWT =====" >> "$LOG"

# 容器没起来时先拉起，避免 curl 报连接错误掩盖真实原因
if ! docker ps --format '{{.Names}}' | grep -qx 'trae-relay'; then
    echo "[WARN] trae-relay 容器未运行，尝试启动" >> "$LOG"
    docker start trae-relay >> "$LOG" 2>&1 || true
    sleep 5
fi

/usr/bin/python3 "$BASE/trae-relay/mint_jwt.py" >> "$LOG" 2>&1
code=$?

if [ $code -ne 0 ]; then
    echo "[FAIL] 退出码 $code —— sessionid 可能已过期，请回浏览器 https://www.trae.cn 复制新的 sessionid 到 config/token.json" >> "$LOG"
fi

# 只保留最近 2000 行，防止日志无限增长
if [ -f "$LOG" ]; then
    tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

exit $code
