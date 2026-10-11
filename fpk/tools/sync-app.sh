#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# 把仓库根目录的最新源码同步进打包工程，避免仓库里同一份文件存两份导致漂移。
# 用法：仓库任意位置执行   bash fpk/tools/sync-app.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # <repo>/fpk/tools
ROOT="$(cd "$HERE/../.." && pwd)"                      # <repo>
PKG="$ROOT/fpk/qu805-fancontrol"                       # 打包工程根

mkdir -p "$PKG/app/tools"
cp "$ROOT/app/fan_webui.py"             "$PKG/app/fan_webui.py"
cp "$ROOT/scripts/patch_qnap_config.py" "$PKG/app/tools/patch_qnap_config.py"

echo "已同步到 $PKG："
echo "  app/fan_webui.py              <- app/fan_webui.py"
echo "  app/tools/patch_qnap_config.py <- scripts/patch_qnap_config.py"
