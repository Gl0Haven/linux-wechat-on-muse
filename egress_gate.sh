#!/bin/bash
# egress_gate.sh - 微信启动门控：固定出口检查
#
# 用户决策（2026-10-06 18:05）：
#   先用 A'（Tailscale tunnel → 用户机器 SOCKS5）做 reset A/B 验证；
#   验证成功后再上生产级固定出口（方案 B）。
#
# 门控逻辑（fail closed）：
#   VM boot → 恢复 machine-id/依赖 → 检查固定 proxy 可达
#   → 查询出口 IP → 必须 == 预期固定 IP → 才允许启动微信
#   出口不对 → 不启动微信，避免先从随机 IP 登录一次再切（更可疑）

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 配置文件：期望的固定出口 IP 和代理地址
# A' 实验阶段由用户填入；生产阶段（方案 B）换成 VPS/静态代理
CONFIG="$SCRIPT_DIR/egress_config.sh"

if [ ! -f "$CONFIG" ]; then
    echo "[gate] 未配置固定出口（$CONFIG 不存在），门控未启用，允许启动（当前为直连旋转出口模式）"
    exit 0
fi

source "$CONFIG"

# EXPECTED_EGRESS_IP: 期望的固定出口公网 IP
# EGRESS_CHECK_PROXY: 用于查询出口 IP 的代理（A' 阶段：经 Tailscale tunnel 到用户 SOCKS5）
if [ -z "$EXPECTED_EGRESS_IP" ]; then
    echo "[gate] EXPECTED_EGRESS_IP 未设置，门控未启用"
    exit 0
fi

echo "[gate] 检查固定出口，期望 IP: $EXPECTED_EGRESS_IP"

# 查询当前经固定代理的出口 IP
if [ -n "$EGRESS_CHECK_PROXY" ]; then
    ACTUAL_IP=$(curl -s --max-time 15 -x "$EGRESS_CHECK_PROXY" https://api.ipify.org 2>/dev/null || echo "")
else
    echo "[gate] EGRESS_CHECK_PROXY 未设置，无法验证"
    exit 1
fi

if [ -z "$ACTUAL_IP" ]; then
    echo "[gate] FAIL: 固定代理不可达，拒绝启动微信（fail closed）"
    exit 1
fi

echo "[gate] 实际出口 IP: $ACTUAL_IP"

if [ "$ACTUAL_IP" = "$EXPECTED_EGRESS_IP" ]; then
    echo "[gate] PASS: 出口 IP 符合预期，允许启动微信"
    exit 0
else
    echo "[gate] FAIL: 出口 IP 不符（期望 $EXPECTED_EGRESS_IP，实际 $ACTUAL_IP），拒绝启动微信（fail closed）"
    exit 1
fi
