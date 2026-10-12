#!/bin/bash
# 截屏到指定文件
export DISPLAY=:99
OUT="${1:-/tmp/wechat-shot.png}"
scrot -z "$OUT" && echo "saved $OUT"
