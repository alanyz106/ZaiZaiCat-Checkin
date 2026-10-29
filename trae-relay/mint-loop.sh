#!/bin/sh
# 容器内 JWT 续期循环（sidecar）
#
# 为什么用 sidecar 而不是宿主 cron：写 /var/spool/cron 需要超出工作区的写权限，
# 且容器方案在宿主机重装/换 cron 实现后依然有效，容器重启也会自动恢复续期。
#
# 环境变量（由 docker run 注入）：
#   RELAY_PUBLIC_URL  relay 服务地址（同网络下用服务名，如 http://trae-relay:8000）
#   RELAY_API_KEY     relay 的 Bearer key
#   TOKEN_JSON        签到配置路径（挂载进来的 config/token.json）
#   REFRESH_INTERVAL  刷新间隔秒数，默认 3600（JWT 约 8 小时，留足余量）
set -u

INTERVAL="${REFRESH_INTERVAL:-3600}"

# 解释器路径因环境而异：宿主机是 /usr/bin/python3，镜像里是 /usr/local/bin/python3
PY="$(command -v python3 || echo /usr/bin/python3)"

echo "[mint-loop] 启动，间隔 ${INTERVAL}s，relay=${RELAY_PUBLIC_URL:-未设置}，python=$PY"

while true; do
    if "$PY" /app/mint_jwt.py \
        --quiet \
        --relay-url "${RELAY_PUBLIC_URL:-http://trae-relay:8000}" \
        --token-json "${TOKEN_JSON:-/config/token.json}"; then
        echo "[mint-loop] $(date '+%F %T') 刷新成功"
    else
        # 失败不退出：多半是 sessionid 过期，等人去浏览器重新复制。
        # 保留循环以便换上新 sessionid 后自动恢复，无需重启 sidecar。
        echo "[mint-loop] $(date '+%F %T') 刷新失败 —— 检查 sessionid 是否过期" >&2
    fi
    sleep "$INTERVAL"
done
