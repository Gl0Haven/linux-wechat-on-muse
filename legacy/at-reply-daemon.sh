#!/bin/bash
# @ 秒回守护进程：每 15 秒轮询一次 DB，发现 @GloHaven 立即发送确认回复
while true; do
  python3 ~/workspace/wechat-bridge/at_reply.py >> ~/workspace/wechat-bridge/at_reply.log 2>&1
  sleep 15
done
