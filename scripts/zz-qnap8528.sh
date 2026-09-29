#!/bin/sh
# zz-qnap8528 —— 内核安装钩子：为每次新装的内核精确重建 qnap8528 模块。
#
# 位置：/etc/kernel/postinst.d/zz-qnap8528
# 由 linux-image-* 的 postinst 通过 run-parts 调用，参数 $1 = 新内核版本。
#
# 为什么需要它：内核升级时，DKMS 的通用 autoinstall **不保证**会遍历到本模块
# （实测同一批里 it87 被重编译了，qnap8528 连目录都没建），结果模块缺失、
# 风扇失控。这里绕开 autoinstall，只认 qnap8528，一条命令、确定执行。
#
# 约定：无论成败都 exit 0，绝不阻断内核安装。

K="$1"
[ -n "$K" ] || exit 0

LOG=/var/log/qnap8528-dkms.log
log() { echo "$(date '+%F %T') [kernel-hook] $*" >>"$LOG"; }

SRC=$(ls -d /usr/src/qnap8528-* 2>/dev/null | sort -V | tail -1)
[ -n "$SRC" ] || { log "skip: 找不到 /usr/src/qnap8528-*（源码目录不在？）"; exit 0; }
VER=${SRC##*-}

[ -d "/lib/modules/$K/build" ] || { log "skip: 内核 $K 的头文件还没装"; exit 0; }

DKMS=$(command -v dkms 2>/dev/null || echo /usr/sbin/dkms)
[ -x "$DKMS" ] || { log "skip: 找不到 dkms 可执行文件"; exit 0; }

log "开始为内核 $K 编译 qnap8528/$VER"
if "$DKMS" install "qnap8528/$VER" -k "$K" >>"$LOG" 2>&1; then
    log "成功：qnap8528/$VER 已为 $K 安装"
else
    log "失败：qnap8528/$VER 为 $K 编译出错，请查看上面的输出（并确认已装 linux-headers-$K）"
fi

exit 0
