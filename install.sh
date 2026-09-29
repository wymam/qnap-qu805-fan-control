#!/usr/bin/env bash
#
# QU805 风扇调速 —— 一键安装脚本
#
# 在 NAS 上以 root 执行（不要从本机远程跑，避免密码出现在命令行/历史里）：
#
#     sudo bash install.sh                 # 全量安装（驱动 + 持久化 + 防复发钩子 + Web UI）
#     sudo bash install.sh --skip-app      # 只修驱动，不部署容器
#     sudo bash install.sh --dry-run       # 只检查环境，不落盘
#
# 脚本幂等，重复执行安全。全程不需要重启。
#
set -euo pipefail

APP_DIR="/vol1/docker/fnos-fan-webui"
APP_PORT="8080"
SKIP_APP=0
DRY_RUN=0
SRC_DIR=""

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------- 输出 ----------
c_ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
c_info() { printf '  · %s\n' "$*"; }
c_warn() { printf '  \033[33m! %s\033[0m\n' "$*"; }
c_err()  { printf '  \033[31m✘ %s\033[0m\n' "$*" >&2; }
step()   { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
die()    { c_err "$*"; exit 1; }

# ---------- 参数 ----------
while [ $# -gt 0 ]; do
    case "$1" in
        --app-dir)   APP_DIR="$2"; shift 2 ;;
        --port)      APP_PORT="$2"; shift 2 ;;
        --src)       SRC_DIR="$2"; shift 2 ;;
        --skip-app)  SKIP_APP=1; shift ;;
        --dry-run)   DRY_RUN=1; shift ;;
        -h|--help)   sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)           die "未知参数：$1（--help 看用法）" ;;
    esac
done

run() { if [ "$DRY_RUN" = 1 ]; then c_info "[dry-run] $*"; else "$@"; fi; }

# ============================================================
step "0. 环境检查"
[ "$(id -u)" = "0" ] || die "请用 root 运行：sudo bash install.sh"
c_ok "root 权限"

for c in python3 make gcc dkms modprobe dmidecode; do
    command -v "$c" >/dev/null 2>&1 && c_ok "找到 $c" || c_warn "缺少 $c（后面用到时会报错）"
done

KVER="$(uname -r)"
c_info "内核：$KVER"
[ -d "/lib/modules/$KVER/build" ] || die "缺少当前内核的头文件：/lib/modules/$KVER/build
      请先安装 linux-headers-$KVER（没有头文件无法编译 DKMS 模块）"

BOARD="$(dmidecode -s baseboard-product-name 2>/dev/null || echo '')"
SYSNAME="$(dmidecode -s system-product-name 2>/dev/null || echo '')"
c_info "主板：${BOARD:-未知}   系统：${SYSNAME:-未知}"
if [ -n "$BOARD" ] && [ "$BOARD" != "SA145" ]; then
    c_warn "主板不是 SA145，本方案针对 QU805/SA145 编写，继续可能不适用"
fi

# ============================================================
step "1. 定位 qnap8528 源码"
if [ -n "$SRC_DIR" ]; then
    SRC="$SRC_DIR"
else
    SRC="$(ls -d /usr/src/qnap8528-* 2>/dev/null | sort -V | tail -1 || true)"
fi
[ -n "$SRC" ] && [ -d "$SRC/src" ] || die "找不到 /usr/src/qnap8528-<版本>/src
      飞牛 fnOS 通常自带该源码；若没有，请先取得 qnap8528 驱动源码放到 /usr/src/"
QNAP_VER="$(basename "$SRC" | sed 's/^qnap8528-//')"
c_ok "源码：$SRC （版本 $QNAP_VER）"
PKG="qnap8528/$QNAP_VER"

# ============================================================
step "2. 给驱动补 QU805/SA145 机型配置"
if grep -q '"QU805"' "$SRC/src/qnap8528.h" 2>/dev/null; then
    c_ok "配置已存在，跳过"
else
    run python3 "$HERE/scripts/patch_qnap_config.py" --src "$SRC" >/dev/null
    c_ok "已插入机型配置（备份：$SRC/src/qnap8528.h.bak-qu805）"
fi

# ============================================================
step "3. 编译并加载驱动"
if dkms status | grep -q "^$PKG, $KVER.*: installed"; then
    c_ok "DKMS 已为 $KVER 安装过"
else
    run dkms install "$PKG" -k "$KVER" || die "DKMS 编译失败，看 /var/lib/dkms/qnap8528/$QNAP_VER/build/make.log"
    c_ok "DKMS 编译完成"
fi

run modprobe qnap8528 skip_hw_check=true || true
if [ -d /sys/module/qnap8528 ]; then
    c_ok "模块已加载"
else
    c_warn "模块没加载起来，看 dmesg | grep qnap8528"
fi

# 清掉失效的旧版本注册（旧版本在新内核上编不过，会拖累 DKMS 的 autoinstall 循环）
for v in $(ls /var/lib/dkms/qnap8528 2>/dev/null | grep -E '^[0-9]' | grep -v "^$QNAP_VER$" || true); do
    run dkms remove -m qnap8528 -v "$v" --all >/dev/null 2>&1 || true
    c_info "清理旧版本注册 qnap8528/$v"
done

# ============================================================
step "4. 持久化：开机自动加载"
if [ "$DRY_RUN" = 0 ]; then
    printf 'qnap8528\n'                            > /tmp/.qnap-ml.conf
    printf 'options qnap8528 skip_hw_check=true\n' > /tmp/.qnap-mp.conf
    install -m 0644 -o root -g root /tmp/.qnap-ml.conf /etc/modules-load.d/qnap8528.conf
    install -m 0644 -o root -g root /tmp/.qnap-mp.conf /etc/modprobe.d/qnap8528.conf
    rm -f /tmp/.qnap-ml.conf /tmp/.qnap-mp.conf
else
    c_info "[dry-run] 写 /etc/modules-load.d/qnap8528.conf 与 /etc/modprobe.d/qnap8528.conf"
fi
c_ok "modules-load.d / modprobe.d 就绪"

# 机械盘温度传感器（Web UI 的硬盘高温保护依赖它）
if [ "$DRY_RUN" = 0 ]; then
    printf 'drivetemp\n' > /tmp/.qnap-dt.conf
    install -m 0644 -o root -g root /tmp/.qnap-dt.conf /etc/modules-load.d/drivetemp.conf
    rm -f /tmp/.qnap-dt.conf
fi
run modprobe drivetemp || true
c_ok "drivetemp（磁盘温度）就绪"

# ============================================================
step "5. 开机自愈：模块缺失时自动补编译"
if [ "$DRY_RUN" = 0 ]; then
    install -m 0755 -o root -g root "$HERE/scripts/qnap8528-load.sh"      /usr/local/sbin/qnap8528-load.sh
    install -m 0644 -o root -g root "$HERE/scripts/qnap8528-load.service" /etc/systemd/system/qnap8528-load.service
else
    c_info "[dry-run] 安装 /usr/local/sbin/qnap8528-load.sh 与 systemd unit"
fi
run systemctl daemon-reload
run systemctl enable qnap8528-load.service >/dev/null 2>&1 || true
c_ok "qnap8528-load.service 已 enable"

# ============================================================
step "6. 防复发：内核安装钩子"
if [ "$DRY_RUN" = 0 ]; then
    install -m 0755 -o root -g root "$HERE/scripts/zz-qnap8528.sh" /etc/kernel/postinst.d/zz-qnap8528
else
    c_info "[dry-run] 安装 /etc/kernel/postinst.d/zz-qnap8528"
fi
if command -v run-parts >/dev/null 2>&1; then
    if run-parts --test /etc/kernel/postinst.d 2>/dev/null | grep -q zz-qnap8528; then
        c_ok "run-parts 能识别该钩子（内核升级时会自动重编译）"
    else
        c_warn "run-parts 未列出该钩子，请检查文件名"
    fi
fi

# ============================================================
if [ "$SKIP_APP" = 1 ]; then
    step "7. 跳过容器部署（--skip-app）"
else
    step "7. 部署温控 Web UI 容器"

    if [ ! -d /vol1 ]; then
        c_warn "/vol1 不存在（不是 fnOS 默认存储卷？）请用 --app-dir 指定应用目录"
    fi

    run mkdir -p "$APP_DIR/data"
    if [ "$DRY_RUN" = 0 ]; then
        install -m 0644 -o root -g root "$HERE/app/fan_webui.py" "$APP_DIR/fan_webui.py"
        install -m 0644 -o root -g root "$HERE/app/Dockerfile"    "$APP_DIR/Dockerfile"
        [ -f "$APP_DIR/data/curve-config.json" ] || echo '{}' > "$APP_DIR/data/curve-config.json"
    fi
    c_ok "应用文件已放到 $APP_DIR"

    if command -v docker >/dev/null 2>&1; then
        run docker build -t fnos-fan-webui:latest "$APP_DIR" >/dev/null
        c_ok "镜像构建完成"

        run docker rm -f fnos-fan-webui >/dev/null 2>&1 || true
        run docker run -d --name fnos-fan-webui --restart=unless-stopped \
            --privileged \
            -v /sys/class/hwmon:/sys/class/hwmon:rw \
            -v /sys/class/thermal:/sys/class/thermal:ro \
            -v "$APP_DIR/data:/data" \
            -p "$APP_PORT:8080" \
            fnos-fan-webui:latest >/dev/null
        c_ok "容器已启动，端口 $APP_PORT"
    else
        c_warn "没找到 docker，跳过容器部署"
    fi
fi

# ============================================================
step "8. 验收"
if [ -d /sys/class/hwmon ]; then
    for d in /sys/class/hwmon/hwmon*; do
        [ "$(cat "$d/name" 2>/dev/null)" = "qnap8528" ] && H="$d"
    done
fi
if [ -n "${H:-}" ] && [ -e "$H/pwm1" ]; then
    c_ok "PWM 接口：$H/pwm1（当前 $(cat "$H/pwm1")）"
    c_ok "转速：fan1=$(cat "$H/fan1_input" 2>/dev/null || echo '?') RPM，fan2=$(cat "$H/fan2_input" 2>/dev/null || echo '?') RPM"
else
    c_warn "没找到 qnap8528 的 pwm1，检查 dmesg | grep qnap8528"
fi
if [ -d /sys/module/drivetemp ]; then
    n=$(ls -d /sys/class/hwmon/hwmon*/ 2>/dev/null | while read -r d; do
            [ "$(cat "$d/name" 2>/dev/null)" = "drivetemp" ] && echo x; done | wc -l)
    c_ok "磁盘温度传感器：$n 个 drivetemp"
fi
if [ "$SKIP_APP" = 0 ] && command -v docker >/dev/null 2>&1; then
    if curl -fsS --max-time 5 "http://127.0.0.1:$APP_PORT/api/status" >/dev/null 2>&1; then
        c_ok "Web UI 可访问：http://<NAS_IP>:$APP_PORT"
    else
        c_warn "Web UI 暂时不可访问，稍等几秒或看 docker logs fnos-fan-webui"
    fi
fi

printf '\n\033[1m完成。\033[0m日志在 /var/log/qnap8528-dkms.log，运行日志在 %s/data/fan.log\n' "$APP_DIR"
printf '内核升级后本方案会自动重编译驱动，无需手动干预。\n'
