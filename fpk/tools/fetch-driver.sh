#!/bin/bash
# ============================================================================
# 取 qnap8528 驱动源码，放进 FPK 项目的 app/driver-src/。
#
# 为什么需要这个脚本：qnap8528 驱动的版权归其原作者，本仓库只提供**配置补丁**
# （patch_qnap_config.py），不重新分发 200 KB 的驱动源码。
#
# 源码从哪来：飞牛系统里通常已经有一份（`/usr/src/qnap8528-<版本>/`，
# 是 DKMS 的构建源）。本脚本挑**版本号最高**的那一份，并**优先取未打补丁的原版**。
#
# 用法：
#   ./fetch-driver.sh                  # 自动挑版本最高的一份
#   ./fetch-driver.sh --list           # 只列出本机有哪些版本
#   ./fetch-driver.sh --from /usr/src/qnap8528-1.21
#   ./fetch-driver.sh --out <目录>      # 默认 ../qu805-fancontrol/app/driver-src
#
# 重要：**要取未打补丁的原版**。项目里的 patch_qnap_config.py 是幂等的，
# 会在安装时补上 QU805/SA145 配置；如果这里就取打好的版本，
# 补丁脚本会直接跳过，也就无法确认到底改了什么。
# ============================================================================
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT_DEFAULT="$(cd "$HERE/.." && pwd)/qu805-fancontrol/app/driver-src"

MODE=auto
FROM=""
OUT="$OUT_DEFAULT"
while [ $# -gt 0 ]; do
    case "$1" in
        --list) MODE=list ;;
        --from) shift; FROM="${1:-}"; MODE=copy ;;
        --out)  shift; OUT="${1:-}" ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
    shift
done

ver_key() {   # 把版本号转成可排序的数字串
    echo "$1" | sed 's/^qnap8528-//' \
        | awk -F'[._-]' '{for(i=1;i<=NF;i++){printf "%05d", ($i ~ /^[0-9]+$/ ? $i : 0)}}'
}

list_versions() {
    local d
    for d in /usr/src/qnap8528-*; do
        [ -d "$d/src" ] || continue
        printf '%s\t%s\n' "$(ver_key "$(basename "$d")")" "$d"
    done | sort -r
}

if [ "$MODE" = list ]; then
    echo "== 本机的 qnap8528 源码目录 =="
    list_versions | while IFS=$'\t' read -r k p; do
        h="$p/src/qnap8528.h"
        # DKMS 源码常是 root-only（-rw-r-----），普通用户读不到。
        # 先用 [ -r ] 判可读性再决定读不读 —— 直接 `wc -c < 不可读文件` 会往 stderr
        # 吐一条 Permission denied，看起来像脚本坏了。
        if [ ! -f "$h" ]; then
            info="缺少 src/qnap8528.h"
        elif [ ! -r "$h" ]; then
            info="qnap8528.h 存在（需 root 才能读）"
        else
            info="$(wc -c < "$h" | tr -d ' ') 字节 qnap8528.h"
        fi
        printf '  %-34s %s\n' "$p" "$info"
    done
    echo
    echo "（用 sudo 跑本脚本、或直接 --from 指定目录，可避开权限问题）"
    exit 0
fi

if [ -n "$FROM" ]; then
    SRC="$FROM"
else
    SRC="$(list_versions | head -1 | cut -f2)"
fi

[ -n "${SRC:-}" ] || {
    cat >&2 <<'EOF'
没有找到 /usr/src/qnap8528-*。

这个目录是 DKMS 的构建源，飞牛系统里通常自带；如果确实没有，请：
  1) 确认你装过/编译过这个驱动（手工装过一次就会有），
  2) 或者从你获取该驱动的来源（上游仓库 / 设备厂商包）取一份，用 --from 指定。
EOF
    exit 1
}

[ -d "$SRC/src" ] || { echo "$SRC 下没有 src/，不像是驱动源码根目录" >&2; exit 1; }
[ -f "$SRC/src/qnap8528.h" ] || { echo "$SRC/src/qnap8528.h 不存在" >&2; exit 1; }

# DKMS 的源码目录常是 root-only（-rw-r-----），普通用户读不到 —— 早点说清楚，
# 免得后面 cp 报一堆 Permission denied 让人以为脚本坏了
if [ ! -r "$SRC/src/qnap8528.h" ] || [ ! -r "$SRC/src/qnap8528.c" ]; then
    cat >&2 <<EOF
读不到 $SRC 下的源码文件（权限不足）。

DKMS 的 /usr/src/qnap8528-* 通常是 root-only，请改用它重跑：

    sudo $0 $([ -n "$FROM" ] && echo "--from $SRC ")--out "$OUT"

EOF
    exit 1
fi

echo "== 源目录：$SRC =="
# 优先用未打补丁的原版（.bak-qu805），否则用当前的 .h
H_SRC="$SRC/src/qnap8528.h"
if [ -f "$SRC/src/qnap8528.h.bak-qu805" ]; then
    if grep -q '"QU805"' "$SRC/src/qnap8528.h" 2>/dev/null; then
        echo "   当前 qnap8528.h 已含 QU805 补丁 → 改用 .bak-qu805（未打补丁的原版）"
        H_SRC="$SRC/src/qnap8528.h.bak-qu805"
    fi
fi
if grep -q '"QU805"' "$H_SRC" 2>/dev/null; then
    echo "   ⚠ 取到的 .h 里已经有 QU805 配置项 —— 安装时补丁脚本会直接跳过。"
    echo "     如果你能拿到真正未打补丁的原版，建议换掉它。"
fi

mkdir -p "$OUT/src"
for f in dkms.conf Makefile; do
    [ -f "$SRC/$f" ] && cp "$SRC/$f" "$OUT/$f" && echo "   -> $f"
done
[ -f "$SRC/src/Makefile" ] && cp "$SRC/src/Makefile" "$OUT/src/Makefile" && echo "   -> src/Makefile"
cp "$SRC/src/qnap8528.c" "$OUT/src/qnap8528.c" && echo "   -> src/qnap8528.c"
cp "$H_SRC" "$OUT/src/qnap8528.h" && echo "   -> src/qnap8528.h"

echo
echo "== 完成 =="
du -sh "$OUT" 2>/dev/null | sed 's/^/   /'
ls -l "$OUT" "$OUT/src" | sed 's/^/   /'
cat <<'EOF'

提醒：app/driver-src/NOTE.md 里写了补丁的原理与验证方式；
      安装时 cmd/service-setup 会自动跑 patch_qnap_config.py 打补丁再 DKMS 编译。
EOF
