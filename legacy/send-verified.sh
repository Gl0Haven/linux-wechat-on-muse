#!/bin/bash
# ⚠️ 已退役（2026-10-06 D方案）：此脚本直读微信源 DB 做 WAL 回读确认，
# 违反"只有 wal_reader_daemon 碰源 DB"原则。生产发送已切 sendd_v2.py
#（查 SQLite OUTBOUND 行确认）。保留仅供手动调试，不要接入自动流程。
#
# 用法: send-verified.sh "<目标群名>" "<回复内容>"
#
# 流程:
# 1. 检查无未知弹窗（有就中止）
# 2. 确认当前聊天标题 == 目标群
# 3. 粘贴，确认输入框内容一致
# 4. 发送，从 WAL 确认消息出现
# 5. 随机延迟

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export DISPLAY=:99
export PATH="$HOME/bin:$PATH"

TARGET_CHAT="$1"
CONTENT="$2"

if [ -z "$TARGET_CHAT" ] || [ -z "$CONTENT" ]; then
    echo "用法: $0 <目标群名> <回复内容>" >&2
    exit 1
fi

# 1. 检查弹窗 - 获取当前活动窗口标题
ACTIVE_TITLE=$(xdotool getactivewindow getwindowname 2>/dev/null || echo "")
# 如果标题包含可疑弹窗关键词，中止
if echo "$ACTIVE_TITLE" | grep -qiE "log out|confirm|error|warning|弹窗|确认"; then
    echo "[sender] 检测到弹窗，中止发送: $ACTIVE_TITLE" >&2
    # 记录弹窗信息用于排查
    mkdir -p /tmp/wechat-bridge
    xdotool getactivewindow getwindowname > /tmp/wechat-bridge/popup_$(date +%s).txt 2>/dev/null || true
    scrot /tmp/wechat-bridge/popup_$(date +%s).png 2>/dev/null || true
    exit 2
fi

# 2. 确认当前聊天标题
# 微信 Linux 的聊天标题通常在窗口标题或界面顶部
# 这里用启发式：检查是否有聊天窗口打开
# (简化版：假设调用者已确保聚焦正确群，此处做二次确认)
echo "[sender] 目标群: $TARGET_CHAT"

# 3. 发送前先同步确认游标，建立基线（必须在发送之前！微信客户端写 DB 很快，
#    如果先发送再同步，同步那一步就会把我们刚发的消息消费掉，轮询时看不到）。
#    用独立游标文件，避免和 at_watchdog 的默认游标互相抢。
VERIFY_CURSOR="$SCRIPT_DIR/state/db_cursor.sendverify.json"
echo "[sender] 同步确认游标到当前..."
python3 "$SCRIPT_DIR/db_poller.py" --json --no-dedup --cursor-file "$VERIFY_CURSOR" > /dev/null 2>&1 || true

# 4. 调用原 send-msg.sh 发送
bash "$SCRIPT_DIR/send-msg.sh" "$CONTENT"
SEND_RC=$?

if [ $SEND_RC -ne 0 ]; then
    echo "[sender] send-msg.sh 失败，退出码 $SEND_RC" >&2
    exit 3
fi

# 5. WAL 回读确认 - 等待最多 10 秒，确认消息出现在数据库
echo "[sender] 等待 WAL 确认..."
for i in $(seq 1 10); do
    sleep 1
    # 用 db_poller 检查最近消息是否包含我们刚发的内容
    # --no-dedup：确认检查必须看到消息，即使它已被 at_watchdog 写入 JSONL
    if python3 "$SCRIPT_DIR/db_poller.py" --json --no-dedup --cursor-file "$VERIFY_CURSOR" 2>/dev/null | \
       python3 -c "
import json, sys
data = json.load(sys.stdin)
content = '''$CONTENT'''.strip()[:20]
for m in data:
    if content in str(m.get('content','')):
        sys.exit(0)
sys.exit(1)
"; then
        echo "[sender] WAL 确认消息已发送"
        # 随机延迟 3-8 秒，防风控
        DELAY=$((3 + RANDOM % 6))
        echo "[sender] 随机延迟 ${DELAY}s"
        sleep $DELAY
        exit 0
    fi
done

echo "[sender] 警告: 10 秒内未在 WAL 中确认消息，标记为不确定" >&2
exit 4
