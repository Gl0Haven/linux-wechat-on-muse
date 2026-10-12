#!/bin/bash
# 微信启动脚本 —— 全部使用家目录下的二进制，VM 重置后无需重装
export DISPLAY=:99
export PROXYCHAINS_CONF_FILE="$HOME/.proxychains/proxychains4.conf"
# 经 proxychains 走出口代理；必须去掉代理 env，避免 Qt 与 proxychains 双重代理
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u all_proxy \
  "$HOME/bin/proxychains4" -q dbus-run-session -- "$HOME/wechat/wechat" > /tmp/wechat.log 2>&1 &
echo "wechat launched under proxychains, pid $!"
