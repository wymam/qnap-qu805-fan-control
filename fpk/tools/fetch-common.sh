#!/bin/bash
# ============================================================================
# 取飞牛应用的「生命周期框架」cmd/common。
#
# 为什么需要这个脚本：cmd/common 是飞牛应用的通用生命周期框架（负责
# install/uninstall/upgrade/config 各阶段的编排、daemon 的启动与状态判定、
# 日志与数据目录校验等），**不是本项目的代码**。它出现在系统里已安装的第三方
# 应用（如 fancontrol、it87-kmod）中，作者各不相同，无法确认许可，
# 因此本仓库不随包分发，改为在本机取一份。
#
# 用法：
#   ./fetch-common.sh                 # 自动挑一个完整的（字节数最大）复制过来
#   ./fetch-common.sh --list          # 只列出候选，不复制
#   ./fetch-common.sh --from <路径>    # 指定用某个应用/某个文件
#   ./fetch-common.sh --out <路径>     # 指定输出（默认 ../qu805-fancontrol/cmd/common）
#
# 说明：飞牛的 fnpack create 生成的项目骨架里**没有** common，
# 说明它不是 SDK 自动产物，而是随应用一起分发的框架副本。
# ============================================================================
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT_DEFAULT="$(cd "$HERE/.." && pwd)/qu805-fancontrol/cmd/common"
APPS_DIRS="/var/apps"

# 框架必须提供的函数（少一个这个副本就不可用）
REQUIRED="install_log call_func install_init install_callback uninstall_init \
uninstall_callback upgrade_init upgrade_callback config_init config_callback \
start_daemon stop_daemon daemon_status wait_for_status save_wizard_variables \
sync_var_folder"

# 已知可用的副本（本项目的 cmd/service-setup 就是对着 fancontrol 的这份写的）。
# 这两个是「原生 + Web UI」应用，与自己要做的形态最接近，优先选它们；
# 都不在时再按字节数从大到小挑第一个通过校验的。
KNOWN_GOOD="fancontrol music-cleaner"

list_candidates() {
    local d
    for d in "$APPS_DIRS"/*/cmd/common; do
        [ -r "$d" ] || continue
        printf '%s\t%s\n' "$(wc -c < "$d" 2>/dev/null | tr -d ' ' || echo 0)" "$d"
    done | sort -rn
}

verify() {
    local f="$1" missing="" fn
    for fn in $REQUIRED; do
        grep -qE "^(function )?${fn}[[:space:]]*\(\)" "$f" || missing="$missing $fn"
    done
    if [ -n "$missing" ]; then
        echo "   ⚠ 缺少函数:$missing"
        return 1
    fi
    head -1 "$f" | grep -q '^#!' || { echo "   ⚠ 不像可执行脚本（没有 shebang）"; return 1; }
    return 0
}

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

echo "== 扫描候选（$APPS_DIRS/*/cmd/common）=="
CANDS="$(list_candidates)"
if [ -z "$CANDS" ]; then
    cat >&2 <<'EOF'
没有找到任何候选。

cmd/common 只存在于**已安装的飞牛应用**里。请先确认：
  1) 你确实在飞牛 fnOS 上执行本脚本（不是 macOS / 普通 Debian）；
  2) 「应用中心」里至少装了一个应用（系统自带的也算），例如 fancontrol；
  3) 或者用 --from <路径> 直接指定一个 common 文件。
EOF
    exit 1
fi

printf '%9s  %s\n' "字节" "路径"
echo "$CANDS" | while IFS=$'\t' read -r sz p; do printf '%9s  %s\n' "$sz" "$p"; done

if [ "$MODE" = list ]; then
    echo
    echo "用 ./fetch-common.sh 自动取最完整的一份，或用 --from <路径> 指定。"
    exit 0
fi

if [ -n "$FROM" ]; then
    if [ -d "$FROM" ]; then
        FROM="$FROM/cmd/common"
    fi
    SRC="$FROM"
else
    SRC=""
    # 1) 先按「已知可用」的顺序找
    for app in $KNOWN_GOOD; do
        cand="$APPS_DIRS/$app/cmd/common"
        [ -r "$cand" ] || continue
        echo "== 校验已知可用的 $cand =="
        if verify "$cand"; then SRC="$cand"; break; fi
    done
    # 2) 都没有则按字节数从大到小挑第一个通过校验的
    if [ -z "$SRC" ]; then
        while IFS=$'\t' read -r sz p; do
            [ -n "$p" ] || continue
            echo "== 校验 $p（$sz 字节）=="
            if verify "$p"; then SRC="$p"; break; fi
        done <<< "$CANDS"
    fi
fi

[ -n "$SRC" ] || { echo "没有可用的 common 副本" >&2; exit 1; }
[ -r "$SRC" ] || { echo "读不到 $SRC" >&2; exit 1; }

echo "== 采用 $SRC（$(wc -c < "$SRC" | tr -d ' ') 字节）=="
verify "$SRC" || { echo "这个副本不完整，拒绝使用（可 --from 指定别的）" >&2; exit 1; }

mkdir -p "$(dirname "$OUT")"
cp "$SRC" "$OUT"
chmod 755 "$OUT"
echo "== 已写入 $OUT =="

cat <<'EOF'

注意：这只是本机副本，用于**本地构建**。如果你的发行版/仓库许可有疑虑，
请自行向飞牛官方确认该框架的许可条款。
EOF
