#!/bin/bash
# 常驻 worker 主循环：500 轮，每轮 10 秒
for i in $(seq 1 500); do
    # 检查队列
    if [ -s at_queue.jsonl ]; then
        echo "[$(date '+%H:%M:%S')] 轮次 $i: 发现队列有内容"
        cat at_queue.jsonl
        echo "---NEED_PROCESSING---"
    fi
    sleep 10
done
echo "500 轮完成"
