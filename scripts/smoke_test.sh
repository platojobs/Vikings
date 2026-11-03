#!/usr/bin/env bash
# 冒烟测试：登录 → 模拟设备上报 → 校验统计与告警 → 下发指令 → 校验回执
# 前置：./scripts/start_all.sh 已启动 Broker 与网关
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
API="${API:-http://127.0.0.1:8000}"
PY="gateway/.venv/bin/python"

pass() { printf '\033[32m[PASS]\033[0m %s\n' "$1"; }
fail() { printf '\033[31m[FAIL]\033[0m %s\n' "$1"; exit 1; }

echo "1) 健康检查"
curl -s "$API/api/health" | grep -q '"mqttConnected":true' || fail "MQTT 未连接"
pass "网关心跳正常，Broker 已连接"

echo "2) 登录"
TOKEN=$(curl -s -X POST "$API/api/auth/login" -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | "$PY" -c "import sys,json;d=json.load(sys.stdin);print(d['data']['token'])" 2>/dev/null || true)
[ -n "$TOKEN" ] || fail "登录失败"
pass "登录成功，已获取 JWT"

echo "3) 模拟设备上报（注入 2.90V / 78℃ 以触发 critical 告警）"
"$PY" scripts/simulate_device.py --device-id dev-smoke --count 1 --voltage 2.90 --temperature 78 >/dev/null
sleep 2

echo "4) 校验入库与告警"
OVERVIEW=$(curl -s -H "Authorization: Bearer $TOKEN" "$API/api/stats/overview")
echo "$OVERVIEW" | grep -q '"report_today":' || fail "统计接口异常: $OVERVIEW"
ALERTS=$(curl -s -H "Authorization: Bearer $TOKEN" "$API/api/alerts?deviceId=dev-smoke&page=1&pageSize=5")
echo "$ALERTS" | grep -q '"level":"critical"' || fail "未生成 critical 告警: $ALERTS"
pass "上报已入库，critical 告警已生成（AI 降级模板生效）"

echo "5) 下发 calibrate 指令并等待回执"
"$PY" scripts/simulate_device.py --device-id dev-smoke --count 1 --interval 1 >/dev/null &
SIM_PID=$!
sleep 2
CMD_ID=$(curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"type":"calibrate","payload":{"temperatureOffset":0.5}}' \
  "$API/api/devices/dev-smoke/commands" | "$PY" -c "import sys,json;print(json.load(sys.stdin)['data']['cmd_id'])")
sleep 6
STATE=$(curl -s -H "Authorization: Bearer $TOKEN" "$API/api/devices/dev-smoke/commands" | \
  "$PY" -c "import sys,json;d=json.load(sys.stdin);print([c['state'] for c in d['data'] if c['cmd_id']=='$CMD_ID'][0])" 2>/dev/null || echo "unknown")
wait $SIM_PID 2>/dev/null || true
[ "$STATE" = "success" ] || fail "指令未完成，当前状态: $STATE"
pass "指令 $CMD_ID 状态机推进到 success"

echo
printf '\033[32m全部冒烟用例通过\033[0m\n'
