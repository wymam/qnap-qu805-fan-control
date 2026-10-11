#!/bin/sh
# qnap8528-load.sh —— 开机自愈加载 qnap8528 风扇驱动。
#
# 位置：/usr/local/sbin/qnap8528-load.sh
# 由 systemd/qnap8528-load.service 在 sysinit.target 阶段调用。
#
# 逻辑：模块已加载 → 什么都不做；
#       模块存在但没加载 → 直接 modprobe；
#       模块压根没有（典型场景：内核升级后 DKMS 没跟上）→ 先补编译再加载。
# 全程追加日志到 /var/log/qnap8528-dkms.log，永不返回非 0。

K=$(uname -r)
LOG=/var/log/qnap8528-dkms.log
log() { echo "$(date '+%F %T') [boot] $*" >>"$LOG"; }

# 已经加载就不用管
[ -d /sys/module/qnap8528 ] && exit 0

# 当前内核没有这个模块 → 补编译
if ! modinfo -k "$K" qnap8528 >/dev/null 2>&1; then
    SRC=$(ls -d /usr/src/qnap8528-* 2>/dev/null | sort -V | tail -1)
    if [ -n "$SRC" ]; then
        VER=${SRC##*-}
        log "内核 $K 缺少 qnap8528 模块，尝试补编译 qnap8528/$VER"
        DKMS=$(command -v dkms 2>/dev/null || echo /usr/sbin/dkms)
        if [ -x "$DKMS" ]; then
            if "$DKMS" install "qnap8528/$VER" -k "$K" >>"$LOG" 2>&1; then
                log "补编译成功"
            else
                log "补编译失败（请确认已安装 linux-headers-$K）"
            fi
        else
            log "找不到 dkms，跳过编译"
        fi
    else
        log "找不到 /usr/src/qnap8528-*，无法补编译"
    fi
fi

# 加载（EC 的硬件 ID 校验在本机过不了，必须跳过）
if modprobe qnap8528 skip_hw_check=true 2>>"$LOG"; then
    log "模块已加载"
else
    log "modprobe qnap8528 失败"
fi

exit 0
