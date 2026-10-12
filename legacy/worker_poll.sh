#!/bin/bash
# 常驻 worker 轮询助手：检查 @ 队列和主动冒泡条件
# 有 @ 就输出 QUEUE_JSON 并退出码 10；满足主动条件输出 PROACTIVE 并退出码 11；否则退出码 0
BRIDGE="$HOME/workspace/wechat-bridge"
QUEUE="$BRIDGE/at_queue.jsonl"
LAST_PRO="$BRIDGE/state/last_proactive.txt"
MSGLOG="$BRIDGE/messages.jsonl"

# 1. 检查 @ 队列
if [ -s "$QUEUE" ]; then
    echo "QUEUE_JSON"
    cat "$QUEUE"
    exit 10
fi

# 2. 主动冒泡条件检查
if [ -f "$LAST_PRO" ]; then
    last=$(date -d "$(cat "$LAST_PRO")" +%s 2>/dev/null || echo 0)
else
    last=0
fi
now=$(date +%s)
elapsed=$((now - last))
if [ "$elapsed" -lt 1200 ]; then
    exit 0  # 20 分钟冷却未到
fi

# 最近 10 分钟内群里别人发的新消息数（[主群] + [测试群]）
if [ -f "$MSGLOG" ]; then
    count=$(python3 -c "
import json, time, datetime
now = time.time()
n = 0
try:
    for line in open('$MSGLOG'):
        line = line.strip()
        if not line: continue
        try: m = json.loads(line)
        except: continue
        chat = m.get('chat','')
        sender = m.get('sender','')
        if chat not in ('[主群]','[测试群]'): continue
        if not sender: continue
        t = m.get('time','')
        try:
            dt = datetime.datetime.strptime(t, '%Y-%m-%d %H:%M')
            ts = dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp()
            if now - ts < 600 and not m.get('replied'):
                n += 1
        except: pass
except: pass
print(n)
" 2>/dev/null)
    if [ "${count:-0}" -ge 2 ]; then
        echo "PROACTIVE count=$count"
        exit 11
    fi
fi
exit 0
