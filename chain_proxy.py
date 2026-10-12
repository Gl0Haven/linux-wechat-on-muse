#!/usr/bin/env python3
"""
本地链式代理 (chain_proxy)
==========================
用途: 把微信的 proxychains 单跳 HTTP 代理, 转成
      YOUR_SANDBOX_PROXY_IP:3128 (沙箱出口, 只允许 CONNECT 到 443 端口)
      -> SOCKS5 YOUR_PROXY_IP:443 (AWS 固定出口, 用户名/密码认证)
      -> 目标服务器

为什么需要它:
- 沙箱透明劫持所有出站 TCP, 必须先经 3128 做 HTTP CONNECT
- 3128 只放行 CONNECT 到 443 端口 (1080 等被拒)
- proxychains-ng 的双跳链 (http 3128 -> socks5) 实测建链失败,
  而单跳 http 127.0.0.1:18890 经本代理中转则完全正常

监听: 127.0.0.1:18890 (只接受 CONNECT 方法)
保活: keepalive.sh 会检查进程, 挂了自动拉起
日志: /tmp/chain_proxy.log
"""

import socket
import struct
import threading
import sys

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 18890

# 第 1 跳: 沙箱出口 HTTP 代理
HOP1_HOST, HOP1_PORT = "YOUR_SANDBOX_PROXY_IP", 3128
# 第 2 跳: AWS 固定出口 SOCKS5
HOP2_HOST, HOP2_PORT = "YOUR_PROXY_IP", 443
HOP2_USER = b"wxproxy"
HOP2_PASS = b"Wq7xRt9YzP2mKv4NcB8hFj6"

LOG_PATH = "/tmp/chain_proxy.log"

# P0 实验日志（2026-10-09）：定位消息入库延迟，只读观测
# 记录：连接建立/断开、下行流量脉冲（server→client 有数据且距上次 >5s）
# 绝不据此做自动重启（用户明确要求）
EXP_LOG_PATH = "/tmp/chain_proxy_exp.log"


def exp_log(*a):
    try:
        import time as _t
        with open(EXP_LOG_PATH, "a") as f:
            f.write("%.3f %s\n" % (_t.time(), " ".join(str(x) for x in a)))
    except Exception:
        pass


def log(*a):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(" ".join(str(x) for x in a) + "\n")
    except Exception:
        pass


def recv_exact(s, n):
    d = b""
    while len(d) < n:
        c = s.recv(n - len(d))
        if not c:
            raise ConnectionError("closed while reading")
        d += c
    return d


def build_tunnel(host, port):
    """经 3128 CONNECT -> SOCKS5 握手+认证 -> SOCKS5 CONNECT, 返回已就绪的 socket"""
    s = socket.create_connection((HOP1_HOST, HOP1_PORT), timeout=15)
    s.sendall(
        b"CONNECT " + HOP2_HOST.encode() + b":" + str(HOP2_PORT).encode() +
        b" HTTP/1.1\r\nHost: x\r\n\r\n"
    )
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += s.recv(4096)
    if b"200" not in resp:
        raise ConnectionError("hop1 CONNECT refused: " + resp.split(b"\r\n")[0].decode(errors="replace"))
    # SOCKS5 握手: 只提供用户名/密码认证
    s.sendall(b"\x05\x01\x02")
    if recv_exact(s, 2) != b"\x05\x02":
        raise ConnectionError("socks5 hello failed")
    s.sendall(b"\x01" + bytes([len(HOP2_USER)]) + HOP2_USER +
              bytes([len(HOP2_PASS)]) + HOP2_PASS)
    if recv_exact(s, 2) != b"\x01\x00":
        raise ConnectionError("socks5 auth failed")
    # SOCKS5 CONNECT: IP 用 IPv4 形式 (\x01), 域名用域名形式 (\x03)
    # 2026-10-12 修复: 之前永远用域名形式, IP 会被当域名解析导致失败
    try:
        ip_bytes = socket.inet_aton(host)
        # 是 IPv4 地址
        s.sendall(b"\x05\x01\x00\x01" + ip_bytes + struct.pack("!H", port))
    except (socket.error, OSError):
        # 是域名
        hb = host.encode()
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack("!H", port))
    r = recv_exact(s, 10)
    if r[1] != 0:
        raise ConnectionError("socks5 connect failed: " + r.hex())
    return s


def pipe(a, b, tag="", direction=""):
    """tag: 连接标识；direction: 'down' 表示 server→client（用于 P0 实验观测）"""
    import time as _t
    down_bytes = 0
    up_bytes = 0
    last_down_ts = 0.0
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            n = len(d)
            if direction == "down":
                down_bytes += n
                now = _t.time()
                # 下行脉冲：距上次下行数据 >5s 且本次有数据 → 记一次 BURST
                if now - last_down_ts > 5.0:
                    exp_log("BURST", tag, "down_bytes=%d" % n)
                last_down_ts = now
            else:
                up_bytes += n
            b.sendall(d)
    except Exception:
        pass
    for x in (a, b):
        try:
            x.close()
        except Exception:
            pass
    return up_bytes, down_bytes


def handle(client):
    try:
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = client.recv(4096)
            if not chunk:
                client.close()
                return
            req += chunk
        parts = req.split(b"\r\n")[0].decode(errors="replace").split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            client.sendall(b"HTTP/1.1 405 only CONNECT supported\r\n\r\n")
            client.close()
            return
        host, port_s = parts[1].rsplit(":", 1)
        import time as _t
        open_ts = _t.time()
        tag = "%s:%s" % (host, port_s)
        exp_log("OPEN", tag)
        upstream = build_tunnel(host, int(port_s))
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        t = threading.Thread(target=pipe, args=(client, upstream, tag, "up"), daemon=True)
        t.start()
        up_b, down_b = pipe(upstream, client, tag, "down")
        exp_log("CLOSE", tag, "up=%d down=%d dur=%.1fs" % (up_b, down_b, _t.time() - open_ts))
    except Exception as e:
        log("ERR", repr(e))
        try:
            client.close()
        except Exception:
            pass


def main():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT))
    srv.listen(100)
    log("chain_proxy listening on %s:%d" % (LISTEN_HOST, LISTEN_PORT))
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


if __name__ == "__main__":
    main()
