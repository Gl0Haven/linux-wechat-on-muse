# 固定出口配置（egress_gate.sh 读取）
# 2026-10-06 19:3x: AWS 试验田上线 - 生产值已填入
#
# 链路:
#   WeChat (proxychains, 单跳 http 127.0.0.1:18890)
#   -> chain_proxy.py (127.0.0.1:18890, 本地常驻, keepalive.sh 保活)
#   -> http YOUR_PROXY_IP:3128 (沙箱出口代理, 只允许 CONNECT 到 443 端口)
#   -> socks5 YOUR_PROXY_IP:443 (AWS us-east-2 t4g.micro, 用户名/密码认证)
#   -> 固定公网 IP YOUR_PROXY_IP
#   -> 微信服务器
#
# 注意:
# - proxychains 双跳链 (http 3128 -> socks5) 实测建链失败, 必须经本地 chain_proxy 中转
# - AWS 账号: Building Things (405517818138), $100 credits 至 2027-04-06
# - 实例: i-051ab4b1684f8fb12 (t4g.micro, us-east-2), EIP YOUR_PROXY_IP
# - SOCKS5 账号密码见下方 (此文件 600 权限, 不进日志不进记忆)

# 微信服务器看到的固定出口 IP
EXPECTED_EGRESS_IP="YOUR_PROXY_IP"

# 本地中转代理 (微信 proxychains 的单跳目标)
CHAIN_PROXY="127.0.0.1:18890"

# AWS SOCKS5 (chain_proxy.py 内部使用)
AWS_SOCKS5_HOST="YOUR_PROXY_IP"
AWS_SOCKS5_PORT="443"
AWS_SOCKS5_USER="wxproxy"
AWS_SOCKS5_PASS="Wq7xRt9YzP2mKv4NcB8hFj6"

# AWS 实例信息 (运维用)
AWS_INSTANCE_ID="i-051ab4b1684f8fb12"
AWS_REGION="us-east-2"
AWS_ACCOUNT_ID="405517818138"
