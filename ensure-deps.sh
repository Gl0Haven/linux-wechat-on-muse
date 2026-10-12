#!/bin/bash
# 确保所有微信桥接依赖都在家目录就位，VM 重置后自动修复
# 由 keepalive.sh 调用，也可手动运行

set -e

HOME_DIR="$HOME"
BIN_DIR="$HOME/bin"
LIB_DIR="$HOME/lib"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 0. 恢复 machine-id（微信设备指纹，VM 重置会变，导致掉登录）
# 必须在微信启动前恢复
SAVED_MID="$SCRIPT_DIR/machine-id.saved"
# 0b. 固定 hostname（微信可能用它做设备指纹；/etc/hostname 重置会丢）
SAVED_HOST="$SCRIPT_DIR/hostname.saved"
if [ ! -f "$SAVED_HOST" ]; then
    echo "${WECHAT_HOSTNAME:-wechat-bridge}" > "$SAVED_HOST"
    chmod 600 "$SAVED_HOST"
fi
SAVED_H=$(cat "$SAVED_HOST")
CURRENT_H=$(cat /etc/hostname 2>/dev/null || echo "")
if [ "$SAVED_H" != "$CURRENT_H" ]; then
    echo "$SAVED_H" > /etc/hostname 2>/dev/null && \
        echo "[deps] hostname 已恢复为 $SAVED_H" || \
        echo "[deps] 警告: 无法写入 /etc/hostname" >&2
fi
# 运行时 hostname 也对齐（容器内可能不允许，失败忽略）
hostname "$SAVED_H" 2>/dev/null || true

# 0c. VM 重置后指纹对比（找出微信掉线的真正原因）
if [ -x "$SCRIPT_DIR/fingerprint_snapshot.py" ]; then
    "$SCRIPT_DIR/fingerprint_snapshot.py" 2>&1 | head -20
fi
if [ -f "$SAVED_MID" ]; then
    SAVED=$(cat "$SAVED_MID")
    CURRENT=$(cat /etc/machine-id 2>/dev/null || echo "")
    if [ "$SAVED" != "$CURRENT" ]; then
        echo "$SAVED" > /etc/machine-id 2>/dev/null
        if [ "$(cat /etc/machine-id)" = "$SAVED" ]; then
            echo "[deps] machine-id 已恢复（防微信掉登录）"
        else
            echo "[deps] 警告: 无法写入 /etc/machine-id（权限不足），微信可能掉登录" >&2
        fi
    fi
else
    # 首次运行，保存当前 ID
    cat /etc/machine-id > "$SAVED_MID" 2>/dev/null && chmod 600 "$SAVED_MID"
    echo "[deps] machine-id 已保存到 $SAVED_MID"
fi

# 1. libproxychains.so.4 必须在 ~/bin/（proxychains4 只查同目录+系统路径）
if [ ! -f "$BIN_DIR/libproxychains.so.4" ]; then
    if [ -f "$LIB_DIR/libproxychains.so.4" ]; then
        cp "$LIB_DIR/libproxychains.so.4" "$BIN_DIR/"
        echo "[deps] 已复制 libproxychains.so.4 到 ~/bin/"
    else
        echo "[deps] 警告: $LIB_DIR/libproxychains.so.4 不存在" >&2
    fi
fi

# 2. xdotool / scrot 必须在 ~/bin/（防 VM 重置丢系统包）
for tool in xdotool scrot; do
    if [ ! -x "$BIN_DIR/$tool" ]; then
        if [ -x "/usr/bin/$tool" ]; then
            cp "/usr/bin/$tool" "$BIN_DIR/"
            echo "[deps] 已复制 $tool 到 ~/bin/"
        else
            echo "[deps] 警告: $tool 在 /usr/bin/ 也不存在" >&2
        fi
    fi
done

# 2b. xdotool 动态库自检（2026-10-06 更新：login_watch 已改用 python-xlib，
#      不再依赖 xdotool。此处仅保留二进制存在性检查，不再尝试 apt 安装
#      libxdo3——该包在 apt 源中不存在，装不上是预期的。）
if [ ! -x "$BIN_DIR/xdotool" ] && [ ! -x /usr/bin/xdotool ]; then
    echo "[deps] 提示: xdotool 不可用（不影响 login_watch，已切 python-xlib）" >&2
fi

# 2c. scrot 动态库自检（2026-10-07 教训：libImlib2.so.1 曾在 VM 重置后缺失，
#      导致 login_watch 自动截图失败。2026-10-10 用户批准：缺失时尝试 apt 安装，
#      不再只告警——os-intent 自动恢复不可靠，同一天已坏两次。
#      2026-10-10 补：用绝对路径定位 scrot（本节跑在 PATH 修复之前，
#      command -v 会找不到 ~/bin/scrot）；安装前先 apt-get update，
#      否则 VM 重置后包列表过期会报 Unable to locate package。）
_scrot_bin=""
for _cand in "$BIN_DIR/scrot" /usr/bin/scrot; do
    if [ -x "$_cand" ]; then _scrot_bin="$_cand"; break; fi
done
unset _cand
if [ -n "$_scrot_bin" ]; then
    _missing_lib=$(ldd "$_scrot_bin" 2>/dev/null | grep "not found" || true)
    if [ -n "$_missing_lib" ]; then
        echo "[deps] scrot 缺动态库，尝试 apt 安装 libimlib2: $_missing_lib" >&2
        _apt_get="apt-get"
        if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
            _apt_get="sudo apt-get"
        fi
        # 2026-10-11 修：apt 加超时。沙箱直连网络不通时 apt-get update 会卡住数小时，
        # 拖住整轮 keepalive（实测 18:19 卡到 21:15，桥接栈 3 小时没拉起）。update 60s、install 120s。
        timeout 60 $_apt_get update -q >/dev/null 2>&1 || true
        if timeout 120 $_apt_get install -y -q libimlib2 >/dev/null 2>&1; then
            echo "[deps] libimlib2 安装完成，scrot 恢复"
        else
            echo "[deps] 警告: apt 安装 libimlib2 失败/超时，截图仍会失败" >&2
        fi
        unset _apt_get
    fi
    unset _missing_lib
fi
unset _scrot_bin

# 3. wxextract 必须可 import（~/.local 下，pip --user 安装）
# 2026-10-11 补超时：沙箱网络不通时 pip 会卡住，拖住 keepalive（同 apt 教训）
if ! python3 -c "import wxextract" 2>/dev/null; then
    echo "[deps] wxextract 缺失，正在重装到 ~/.local ..."
    timeout 180 pip install --user --break-system-packages -q \
        git+https://github.com/boujuan/extract-wechat-messages-linux.git 2>&1 | tail -2
    if python3 -c "import wxextract" 2>/dev/null; then
        echo "[deps] wxextract 重装成功"
    else
        echo "[deps] 错误: wxextract 重装失败/超时" >&2
        exit 1
    fi
fi

# 3b. pycryptodome 必须在 ~/.local（wxextract 解密依赖，系统版 VM 重置会丢）
if ! python3 -c "import sys; sys.path.insert(0, '$HOME/.local/lib/python3.12/site-packages'); from Crypto.Cipher import AES" 2>/dev/null; then
    echo "[deps] pycryptodome 缺失，正在重装到 ~/.local ..."
    timeout 180 pip install --user --break-system-packages -q pycryptodome pycryptodomex zstandard 2>&1 | tail -1
    echo "[deps] pycryptodome 重装完成"
fi

# 3c. 微信图片 DAT 解密密钥预热（2026-10-11：kvcomm 派生，VM 重置后自动恢复）
# 只打码输出，不暴露密钥明文
if [ -f "$SCRIPT_DIR/dat_key.py" ]; then
    python3 "$SCRIPT_DIR/dat_key.py" ensure 2>&1 | head -1 || echo "[deps] dat_key 预热失败（图片解密将降级为截图）"
fi

# 4. 确保 ~/bin 在 PATH 最前面
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) export PATH="$BIN_DIR:$PATH" ;;
esac

echo "[deps] 依赖检查通过"
