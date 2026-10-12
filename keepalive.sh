#!/bin/bash
# 保活脚本：确保 Xvfb 和微信客户端一直在运行（由 cron 每 5 分钟调用）
# 全部使用家目录二进制，VM 重置后自愈
export DISPLAY=:99
export PROXYCHAINS_CONF_FILE="$HOME/.proxychains/proxychains4.conf"
LOG=/tmp/wechat-keepalive.log
WECHAT_BIN="$HOME/wechat/wechat"
PC_BIN="$HOME/bin/proxychains4"
# Phase 0（2026-10-07 逆向）：Frida Gadget 注入，autonomous script 零 hook soak
GADGET_SO="$HOME/workspace/wechat-bridge/frida-re/gadget/frida-gadget.so"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

# 0. 自检：关键文件是否还在
# 先跑依赖自愈（家目录化：wxextract、xdotool、scrot、libproxychains.so.4）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# 2026-10-12 修复：mkdir 原子单例锁，替代 flock（flock 有 fd 继承 bug）
# 原因：flock 的 fd 200 会被守护进程子进程继承，锁永不释放；
# 且 python3 -c 设 FD_CLOEXEC 是在子进程里设的，父进程 bash 的 fd 根本没生效。
# mkdir 是原子操作，不涉及 fd，无继承问题。支持 stale 锁检测（holder 已死则清理）。
KEEPALIVE_LOCKDIR="$SCRIPT_DIR/state/keepalive.lockdir"
mkdir -p "$SCRIPT_DIR/state"
_acquire_lock() {
    if mkdir "$KEEPALIVE_LOCKDIR" 2>/dev/null; then
        echo $$ > "$KEEPALIVE_LOCKDIR/pid"
        return 0
    fi
    # 锁已存在，检查 holder 是否还活着
    local old_pid
    old_pid=$(cat "$KEEPALIVE_LOCKDIR/pid" 2>/dev/null)
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
        return 1  # holder 还活着，本次退出
    fi
    # stale 锁（holder 已死），清理后重试
    rm -rf "$KEEPALIVE_LOCKDIR"
    if mkdir "$KEEPALIVE_LOCKDIR" 2>/dev/null; then
        echo $$ > "$KEEPALIVE_LOCKDIR/pid"
        return 0
    fi
    return 1
}
if ! _acquire_lock; then
    echo "[$(date '+%F %T')] 已有 keepalive 实例在运行，本次退出" >> "$LOG"
    exit 0
fi
trap 'rm -rf "$KEEPALIVE_LOCKDIR"' EXIT
# 锁在脚本退出时自动释放（trap），子进程不继承任何 fd
bash "$SCRIPT_DIR/ensure-deps.sh" >> "$LOG" 2>&1 || log "依赖自愈脚本异常，继续保活流程"
export PATH="$HOME/bin:$PATH"

if [ ! -x "$WECHAT_BIN" ]; then
    log "微信二进制丢失 ($WECHAT_BIN 不存在)，需人工处理"
    exit 1
fi
if [ ! -x "$PC_BIN" ]; then
    log "proxychains 丢失 ($PC_BIN 不存在)，需人工处理"
    exit 1
fi

# 1. Xvfb
if ! pgrep -f "Xvfb :99" > /dev/null; then
    log "Xvfb 不在，启动"
    rm -f /tmp/.X99-lock
    Xvfb :99 -screen 0 1280x800x24 >> "$LOG" 2>&1 < /dev/null &
    sleep 3
fi

# 1.5 chain_proxy（AWS 固定出口的本地中转代理）
# 微信 proxychains 单跳 http 127.0.0.1:18890 -> 本代理 -> 3128 CONNECT -> SOCKS5 AWS:443(固定IP)
# 背景: 沙箱只允许 3128 做 CONNECT 到 443 端口; proxychains 双跳链实测建链失败, 单跳经本代理则正常
if ! pgrep -f "[c]hain_proxy\.py" > /dev/null; then
    log "chain_proxy 不在，启动"
    # 2026-10-08 修：内层再加一层 &（双 fork），否则外层 bash 会留一个
    # 等待守护进程的中间 subshell，主脚本退不出、管道 EOF 到不了 tail，
    # cron 契约的 LOGIN_STATE 行永远送不出来（本轮实测卡住）。
    ( cd "$SCRIPT_DIR" && setsid nohup python3 "$SCRIPT_DIR/chain_proxy.py" > /tmp/chain_proxy.log 2>&1 < /dev/null & )
    sleep 2
    if pgrep -f "[c]hain_proxy\.py" > /dev/null; then
        log "chain_proxy 已启动"
    else
        log "chain_proxy 启动失败，需人工处理"
    fi
fi

# 2. 微信客户端（走家目录 proxychains）
if ! pgrep -f "$HOME/wechat/wechat" > /dev/null; then
    log "wechat 不在，启动"
    # 重启前归档旧日志（掉线取证需要重启前的最后现场）
    if [ -f /tmp/wechat.log ]; then
        ARCHIVE="$HOME/workspace/wechat-bridge/state/wechat_logs/wechat-$(date +%Y%m%d-%H%M%S).log"
        mkdir -p "$(dirname "$ARCHIVE")"
        cp /tmp/wechat.log "$ARCHIVE" 2>/dev/null && log "旧日志已归档: $ARCHIVE"
        # 只保留最近 20 个归档
        ls -t "$HOME/workspace/wechat-bridge/state/wechat_logs"/wechat-*.log 2>/dev/null | tail -n +21 | xargs -r rm -f
    fi
    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u all_proxy \
        "$PC_BIN" -q dbus-run-session -- /lib64/ld-linux-x86-64.so.2 \
        --preload "$GADGET_SO" "$WECHAT_BIN" >> /tmp/wechat.log 2>&1 < /dev/null &
    sleep 5
    if pgrep -f "$HOME/wechat/wechat" > /dev/null; then
        log "wechat 已启动"
    else
        log "wechat 启动失败（进程未出现），需人工处理"
    fi
else
    log "检查通过：Xvfb + wechat 运行中"
fi

# 3. at_watchdog 已退役（2026-10-06 D方案）：
#    它每 5 秒调 db_poller.py --json 直读微信 DB，是第二个 DB reader，
#    会和 wal_reader_daemon 抢游标、互相覆盖 /tmp/wal_merged_*.db。
#    新架构唯一 reader 是 wal_reader_daemon（flock 单例）。
#    这里只负责确保它不在跑，不再拉起。
if pgrep -f "at_watchdog.py" > /dev/null; then
    log "at_watchdog 仍在跑（已退役），kill 掉"
    pkill -f "at_watchdog.py" || true
fi

# 4. WAL 读取进程（新架构的唯一读取者）
# D方案：单例由 wal_reader_daemon 内部 flock 保证，keepalive 只负责拉起，
# 不再杀多余实例（旧逻辑有 race）。flock 抢不到新实例会自动退出。
# 2026-10-06 修：不只查进程存在，还查心跳新鲜度。
# 教训：22:15 进程活着但不干活（入队数 86→6），pgrep 查不出来，漏了 1 小时。
WAL_READER_OK=false
if pgrep -f "wal_reader_daemon.py" > /dev/null; then
    # 进程在，查心跳（5 分钟内有更新才算活）
    HB_FILE="/tmp/wechat-bridge/wal_reader.heartbeat"
    if [ -f "$HB_FILE" ]; then
        HB_AGE=$(($(date +%s) - $(cat "$HB_FILE" 2>/dev/null || echo 0)))
        if [ "$HB_AGE" -lt 300 ]; then
            WAL_READER_OK=true
        else
            log "wal_reader_daemon 心跳过期 (${HB_AGE}s)，判定僵死，重启"
            pkill -9 -f "wal_reader_daemon.py" 2>/dev/null || true
            sleep 2
        fi
    else
        # 无心跳文件，老版本或刚启动，给一次机会
        WAL_READER_OK=true
    fi
fi
if [ "$WAL_READER_OK" = false ]; then
    log "wal_reader_daemon 不在或僵死，启动"
    # 2026-10-08 修：同上，双 fork 脱离（中间 subshell 会持有管道写端导致 tail 等不到 EOF）
    ( cd "$SCRIPT_DIR" && nohup python3 wal_reader_daemon.py > wal_reader.log 2>&1 < /dev/null & )
    sleep 2
    if pgrep -f "wal_reader_daemon.py" > /dev/null; then
        log "wal_reader_daemon 已启动"
    else
        log "wal_reader_daemon 启动失败，需人工处理"
    fi
fi

# 5. sendd_v2（独立发送进程，微信 GUI 唯一写者；worker 只到 READY 不碰 GUI）
#    v2：位置点选导航 + VERIFY_CHAT 模板匹配（2026-10-06 16:10 起为生产版本）
#    2026-10-06 18:55: 用 PID 文件判断，避免 pgrep 竞态导致多实例
#    2026-10-06 23:30 修：加心跳检查，防"进程活着但不干活"（wal_reader 22:15 僵死教训）
PIDFILE="$SCRIPT_DIR/state/sendd_v2.pid"
SENDD_RUNNING=false
if [ -f "$PIDFILE" ]; then
    OLDPID=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$OLDPID" ] && kill -0 "$OLDPID" 2>/dev/null; then
        # 确认是 sendd_v2 进程（不是 PID 复用）
        if ps -p "$OLDPID" -o args= 2>/dev/null | grep -q "sendd_v2.py"; then
            # 再查心跳（5 分钟内有更新才算活）
            SENDD_HB="/tmp/wechat-bridge/sendd_v2.heartbeat"
            if [ -f "$SENDD_HB" ]; then
                HB_AGE=$(($(date +%s) - $(cat "$SENDD_HB" 2>/dev/null || echo 0)))
                if [ "$HB_AGE" -lt 300 ]; then
                    SENDD_RUNNING=true
                else
                    log "sendd_v2 心跳过期 (${HB_AGE}s)，判定僵死"
                fi
            else
                # 无心跳文件（老版本刚启动），暂算运行
                SENDD_RUNNING=true
            fi
        fi
    fi
fi
if [ "$SENDD_RUNNING" = false ]; then
    # 清理僵尸 PID 文件和残留进程
    pkill -9 -f "sendd_v2\.py" 2>/dev/null
    sleep 1
    rm -f "$PIDFILE"
    log "sendd_v2 不在，启动"
    # 2026-10-08 修：同上，双 fork 脱离
    ( cd "$SCRIPT_DIR" && nohup python3 sendd_v2.py > sendd_v2.log 2>&1 < /dev/null & )
    sleep 2
    if [ -f "$PIDFILE" ] && kill -0 "$(cat $PIDFILE)" 2>/dev/null; then
        log "sendd_v2 已启动 (pid $(cat $PIDFILE))"
    else
        log "sendd_v2 启动失败，需人工处理"
    fi
fi
# 已在运行：清理 PID 文件之外的多余实例
if [ "$SENDD_RUNNING" = true ]; then
    _sendd_pids=$(pgrep -f "sendd_v2\.py" || true)
    _sendd_count=$(echo "$_sendd_pids" | grep -c . || true)
    if [ "$_sendd_count" -gt 1 ]; then
        OLDPID=$(cat "$PIDFILE" 2>/dev/null)
        log "sendd_v2 有 $_sendd_count 个实例，保留 $OLDPID，清理其余"
        for _p in $_sendd_pids; do
            if [ "$_p" != "$OLDPID" ]; then
                kill -9 "$_p" 2>/dev/null
            fi
        done
    fi
fi
# 旧 v1 如果还在，杀掉（避免抢单）
if pgrep -f "sendd.py" | grep -v "sendd_v2" > /dev/null; then
    # 精确匹配 sendd.py 但排除 sendd_v2.py
    for pid in $(pgrep -f "python3 sendd.py"); do
        if ! ps -p $pid -o args= | grep -q "sendd_v2"; then
            log "发现旧 sendd v1 (pid $pid)，杀掉"
            kill $pid
        fi
    done
fi

# 5.8 scheduler（2026-10-09 Batch3：轻量调度器，租约/僵尸维护+指标）
if ! pgrep -f "scheduler\.py" > /dev/null; then
    log "scheduler 不在，启动"
    ( cd "$SCRIPT_DIR" && nohup python3 scheduler.py > scheduler.log 2>&1 < /dev/null & )
    sleep 1
fi

# 5.9 解绑后台任务：上面的守护进程（Xvfb/chain_proxy/wechat/wal_reader/sendd_v2/scheduler）
# 以 & 启动后仍是本脚本的子进程，bash 退出时会 do_wait 等待它们，
# 导致脚本永不结束、cron 契约的 LOGIN_STATE 输出不出来（2026-10-08 实测卡住）。
# 守护进程 stdout 已全部重定向到文件，disown 安全退出不影响它们。
disown -a 2>/dev/null || true

# 6. 登录态监测 + 自动恢复（2026-10-08 用户批准）：
#    LOGIN_REQUIRED 时自动点 Enter Weixin（自动登录已验证无需手机确认），
#    最多 3 轮；失败则上报用户。状态变化自动截图。
if [ -f "$SCRIPT_DIR/login_watch.py" ]; then
    # 2026-10-09：加 timeout 防 $(...) 永久阻塞（后台进程继承 stdout 会导致管道永不 EOF）
    LOGIN_OUT=$(timeout 30 python3 "$SCRIPT_DIR/login_watch.py" 2>&1 < /dev/null) || LOGIN_OUT="timeout"
    log "login_watch: $LOGIN_OUT"
    echo "$LOGIN_OUT"  # cron 契约：stdout 输出 LOGIN_STATE=<状态>
fi
