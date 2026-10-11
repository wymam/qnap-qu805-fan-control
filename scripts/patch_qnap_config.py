#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
给 qnap8528 驱动的机型配置表补一条 QU805 / SA145 记录。

== 背景 ==
qnap8528 驱动内部维护一张「主板型号 → 风扇 / 槽位 / LED 能力」的表，
只认表里登记过的机型。QU805 用的 iEi SA145 主板不在表里，于是 modprobe 直接失败：

    qnap8528 @ qnap8528_ec_hw_check: Could not locate IT8528 EC device
    qnap8528 @ qnap8528_find_config: Searching configs for a match with MB=<主板串>
    qnap8528 @ qnap8528_find_config: Could not find configuration for device
    qnap8528: probe with driver qnap8528 failed with error -524

驱动里的匹配逻辑是**子串匹配**（src/qnap8528.c）：

    if (strstr(mb_model, qnap8528_configs[i].mb_model))

所以 mb_model 只要填 "SA145" 就能命中形如 MB=70006SA14500xxxxRS 的主板串。

== 用法 ==（在 NAS 上以 root 执行）

    python3 patch_qnap_config.py                    # 自动挑版本号最大的 /usr/src/qnap8528-*
    python3 patch_qnap_config.py --dry-run          # 只打印将要插入的内容
    python3 patch_qnap_config.py --src /usr/src/qnap8528-1.21
    python3 patch_qnap_config.py --model QU805 --board SA145
    python3 patch_qnap_config.py --fans 1,2         # 自定义风扇 EC 索引（默认 1,2）

== 行为约定 ==
* **幂等**：表里已有同型号就直接退出，绝不重复插入。
* **备份**：首次修改前生成 qnap8528.h.bak-qu805。
* **不破坏语法**：只在配置表末尾的 { NULL } 终止符**之前**插入一个括号配平的块，
  并且改完会重新校验「表尾仍是 { NULL } };」以及花括号总量不变。

  这一条不是洁癖 —— 早先用「切片拼接表尾」的写法把终止符的 }; 一起吃掉了，
  结果 DKMS 编译直接 bad exit status: 2，且报错位置离真正原因很远，很费时间。
"""

import argparse
import glob
import os
import re
import shutil
import sys

BACKUP_SUFFIX = ".bak-qu805"

TABLE_RE = re.compile(r"qnap8528_configs\s*\[\s*\]\s*=\s*\{")
# 配置表的终止符：{ NULL } 后面紧跟 };（.slots 里的 { NULL } 后面是 } 和逗号，不会误命中）
TERM_RE = re.compile(r"\{\s*NULL\s*\}\s*\}\s*;")


def find_source(explicit=None):
    """定位 /usr/src/qnap8528-<版本>，取版本号最大的那个。"""
    if explicit:
        cands = [explicit]
    else:
        cands = glob.glob("/usr/src/qnap8528-*")
    cands = [c for c in cands if os.path.isdir(os.path.join(c, "src"))]
    if not cands:
        sys.exit("找不到源码目录（应为 /usr/src/qnap8528-<版本>/src/qnap8528.h）")

    def ver(p):
        m = re.search(r"qnap8528-(.+)$", os.path.basename(p.rstrip("/")))
        if not m:
            return (0,)
        return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[._-]", m.group(1)))

    cands.sort(key=ver)
    return cands[-1]


def build_entry(model, board, fans, indent="\t"):
    """按驱动源码里的既有风格生成配置项文本（制表符缩进）。"""
    f = ", ".join(str(x) for x in fans)
    return (
        f"{indent}{{\n"
        f'{indent}\t"{model}", "{board}", "",\n'
        f"{indent}\t{{\n"
        f"{indent}\t\t.pwr_recovery   = 1,\n"
        f"{indent}\t}},\n"
        f"{indent}\t.fans = (u8[]){{ {f}, 0}},\n"
        f"{indent}\t.slots = (struct qnap8528_slot_config[]){{\n"
        f"{indent}\t\t{{ NULL }}\n"
        f"{indent}\t}}\n"
        f"{indent}}},\n"
    )


def patch(text, entry, model):
    """返回 (新文本, 状态说明)。不改动则新文本与原文相同。"""
    if re.search(r'"%s"\s*,' % re.escape(model), text):
        return text, "already"

    m = TABLE_RE.search(text)
    if not m:
        sys.exit("在源码里找不到配置表 qnap8528_configs[] —— 版本可能差异过大")

    table_start = m.end()
    tail = text[table_start:]
    terms = list(TERM_RE.finditer(tail))
    if not terms:
        sys.exit("找不到配置表终止符 { NULL } }; —— 源码结构与预期不符")

    last = terms[-1]
    line_start = tail.rfind("\n", 0, last.start()) + 1   # 定位到终止符所在行的行首

    new_tail = tail[:line_start] + entry + tail[line_start:]
    out = text[:table_start] + new_tail

    # --- 自检 1：花括号总量必须增加一个配平块（原量 + 插入块的量） ---
    if out.count("{") - text.count("{") != entry.count("{"):
        sys.exit("自检失败：花括号数量异常，已中止（源文件未被修改）")
    if out.count("}") - text.count("}") != entry.count("}"):
        sys.exit("自检失败：花括号数量异常，已中止（源文件未被修改）")

    # --- 自检 2：表尾必须仍是 { NULL } };（就是当年踩过的坑） ---
    if not TERM_RE.search(out[table_start:]):
        sys.exit("自检失败：配置表终止符丢了，已中止（源文件未被修改）")
    if not out.rstrip().endswith("};"):
        sys.exit("自检失败：文件结尾不再是 };，已中止（源文件未被修改）")

    return out, "patched"


def main():
    ap = argparse.ArgumentParser(description="给 qnap8528 补 QU805/SA145 机型配置")
    ap.add_argument("--src", help="源码目录，如 /usr/src/qnap8528-1.21")
    ap.add_argument("--model", default="QU805", help='机型名，默认 QU805（随便填，只是标识）')
    ap.add_argument("--board", default="SA145", help='主板型号子串，必须能命中 MB= 串，默认 SA145')
    ap.add_argument("--fans", default="1,2", help='风扇 EC 索引，逗号分隔，默认 1,2')
    ap.add_argument("--dry-run", action="store_true", help="只打印不落盘")
    args = ap.parse_args()

    src = find_source(args.src)
    header = os.path.join(src, "src", "qnap8528.h")
    if not os.path.isfile(header):
        sys.exit("找不到 %s" % header)

    fans = [int(x) for x in args.fans.replace(" ", "").split(",") if x]

    with open(header, encoding="utf-8") as fp:
        text = fp.read()

    entry = build_entry(args.model, args.board, fans)
    out, status = patch(text, entry, args.model)

    print("源码目录 : %s" % src)
    print("目标文件 : %s" % header)
    print("将插入   :")
    print(entry)

    if status == "already":
        print("=> 表里已有 \"%s\" 配置，无需改动（幂等退出）" % args.model)
        return

    if args.dry_run:
        print("=> --dry-run：未写入任何文件")
        return

    backup = header + BACKUP_SUFFIX
    if not os.path.exists(backup):
        shutil.copy2(header, backup)
        print("已备份   : %s" % backup)
    else:
        print("备份已存在: %s（未覆盖）" % backup)

    with open(header, "w", encoding="utf-8") as fp:
        fp.write(out)
    print("=> 写入完成。接着重编译：")
    print("     dkms install qnap8528/%s -k $(uname -r)" % os.path.basename(src).split("-", 1)[1])
    print("     modprobe qnap8528 skip_hw_check=true")


if __name__ == "__main__":
    main()
