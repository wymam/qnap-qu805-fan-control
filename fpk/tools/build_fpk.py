#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把飞牛（fnOS）fpk 项目打包成 .fpk —— 在 macOS/Linux 上执行，借 NAS 的 fnpack 构建。

为什么不能本地构建：`fnpack` 是飞牛自带的 Linux x86-64 Go 二进制
（NAS 上 /usr/local/bin/fnpack），macOS 上跑不了。所以流程是：
    本地清理 + 打包 → 送到 NAS 的 /tmp → fnpack build → 取回 .fpk → 清理

**只构建，不安装**：全程不碰 /var/apps、/vol1/@app*、systemd、DKMS 等任何线上路径，
只在 NAS 的 /tmp 下建一个临时目录，用完删掉。

用法
----
    # 1) 凭证（三选一，与 rexec.py 一致）
    #    a. NAS_* 环境变量：NAS_HOST / NAS_PORT / NAS_USER / NAS_PASSWORD
    #    b. --cred 指向 JSON：{"host":...,"port":22,"user":...,"password":...}
    #    c. 默认依次找 临时目录/.nas_cred.json、~/.nas_cred.json、./.nas_cred.json

    python3 build_fpk.py --project-dir ./qu805-fancontrol
    python3 build_fpk.py --project-dir ./qu805-fancontrol --out ./dist
    python3 build_fpk.py --project-dir ./qu805-fancontrol --keep-remote   # 保留远端临时目录

退出码：0 成功；非 0 失败（并把远端输出的最后一段回显出来）。
"""

import argparse
import json
import os
import posixpath
import shutil
import subprocess
import sys
import tarfile
import tempfile

REMOTE_BUILD = "/tmp/fpkbuild"
JUNK_NAMES = (".DS_Store",)
JUNK_PREFIXES = ("._",)


# ---------------------------------------------------------------- 凭证
def load_cred(explicit=None):
    cred = {}
    path = explicit
    if not path:
        cands = [os.path.join(tempfile.gettempdir(), ".nas_cred.json"),
                 os.path.join(os.path.expanduser("~"), ".nas_cred.json"),
                 os.path.join(os.getcwd(), ".nas_cred.json")]
        for c in cands:
            if os.path.isfile(c):
                path = c
                break
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            cred = json.load(f)
    for env_key, key in (("NAS_HOST", "host"), ("NAS_USER", "user"),
                         ("NAS_PASSWORD", "password")):
        if os.environ.get(env_key):
            cred[key] = os.environ[env_key]
    if os.environ.get("NAS_PORT"):
        cred["port"] = os.environ["NAS_PORT"]
    cred.setdefault("port", 22)
    missing = [k for k in ("host", "user", "password") if not cred.get(k)]
    if missing:
        sys.exit("缺少凭证字段：%s（用 --cred 指 JSON，或设 NAS_* 环境变量）"
                 % ", ".join(missing))
    return cred


# ---------------------------------------------------------------- 本地清理
def clean_junk(root):
    """删掉 macOS 会产生的伴生文件。不清理会把 ._xxx 打进 fpk。"""
    killed = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames):
            if name == "__MACOSX":
                p = os.path.join(dirpath, name)
                shutil.rmtree(p, ignore_errors=True)
                dirnames.remove(name)
                killed.append(os.path.relpath(p, root))
        for name in filenames:
            if name in JUNK_NAMES or name.startswith(JUNK_PREFIXES):
                p = os.path.join(dirpath, name)
                try:
                    os.remove(p)
                    killed.append(os.path.relpath(p, root))
                except OSError:
                    pass
    return killed


def make_tarball(project_dir, tgz_path):
    """
    用 tar 命令打包（而不是 tarfile 模块）：需要 COPYFILE_DISABLE=1 才不生成
    ._* 伴生文件，这是 macOS 的 tar 特性。--no-xattrs 单独用不够。
    """
    parent = os.path.dirname(os.path.abspath(project_dir))
    name = os.path.basename(os.path.abspath(project_dir))
    env = dict(os.environ, COPYFILE_DISABLE="1")
    cmd = ["tar", "--no-xattrs",
           "--exclude", "._*", "--exclude", ".DS_Store",
           "-czf", tgz_path, name]
    subprocess.run(cmd, cwd=parent, env=env, check=True)


def assert_no_junk(tgz_path, project_name):
    with tarfile.open(tgz_path) as tf:
        bad = [m.name for m in tf.getmembers()
               if os.path.basename(m.name) in JUNK_NAMES
               or os.path.basename(m.name).startswith(JUNK_PREFIXES)]
    if bad:
        sys.exit("打包后仍含 macOS 垃圾文件，已中止：\n  " + "\n  ".join(bad[:20]))
    return True


# ---------------------------------------------------------------- 远端
def main():
    ap = argparse.ArgumentParser(description="打包飞牛 fpk 应用（借 NAS 的 fnpack）")
    ap.add_argument("--project-dir", required=True,
                    help="fpk 项目目录，如 ./qu805-fancontrol")
    ap.add_argument("--out", default=None,
                    help=".fpk 输出目录，默认 <project-dir>/../dist")
    ap.add_argument("--cred", default=None, help="凭证 JSON 路径")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--keep-remote", action="store_true",
                    help="保留 NAS 上的 /tmp/fpkbuild（排障用）")
    args = ap.parse_args()

    proj = os.path.abspath(args.project_dir)
    if not os.path.isfile(os.path.join(proj, "manifest")):
        sys.exit("找不到 %s/manifest —— 这不是一个 fpk 项目目录" % proj)
    name = os.path.basename(proj)

    try:
        import paramiko
    except ImportError:
        sys.exit("缺少 paramiko：pip install paramiko")

    print("== 1. 清理 macOS 伴生文件 ==")
    for p in clean_junk(proj):
        print("   删除", p)
    print("   完成")

    tgz = os.path.join(tempfile.gettempdir(), "%s-src.tgz" % name)
    print("== 2. 本地打包 %s ==" % tgz)
    make_tarball(proj, tgz)
    assert_no_junk(tgz, name)
    print("   %d 字节（已确认不含 ._* / .DS_Store）" % os.path.getsize(tgz))

    cred = load_cred(args.cred)
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(cred["host"], port=int(cred["port"]),
                username=cred["user"], password=cred["password"],
                timeout=20, look_for_keys=False, allow_agent=False)

    def run(script, timeout=None):
        si, so, se = cli.exec_command("bash -s", timeout=timeout or args.timeout)
        si.write(script)
        si.channel.shutdown_write()
        out = so.read().decode("utf-8", "replace")
        err = se.read().decode("utf-8", "replace")
        return out, err

    print("== 3. 送到 NAS 的 %s（只构建，不安装）==" % REMOTE_BUILD)
    run("rm -rf %s && mkdir -p %s" % (REMOTE_BUILD, REMOTE_BUILD))
    sftp = cli.open_sftp()
    sftp.put(tgz, REMOTE_BUILD + "/src.tgz")
    sftp.close()

    print("== 4. fnpack build ==")
    out, err = run("cd %s && tar xzf src.tgz && fnpack build -d %s/%s 2>&1"
                   % (REMOTE_BUILD, REMOTE_BUILD, name))
    print("   " + out.strip().replace("\n", "\n   "))
    if "Packing successfully" not in out:
        print("[远端 stderr] " + err[:800], file=sys.stderr)
        sys.exit("fnpack build 失败")

    print("== 5. 取回 .fpk ==")
    out, _ = run("ls -1 %s/*.fpk 2>/dev/null" % REMOTE_BUILD)
    remote_fpk = out.strip().splitlines()[0] if out.strip() else None
    if not remote_fpk:
        sys.exit("远端没有产出 .fpk")
    local_dir = os.path.abspath(args.out or os.path.join(os.path.dirname(proj), "dist"))
    os.makedirs(local_dir, exist_ok=True)
    local_fpk = os.path.join(local_dir, posixpath.basename(remote_fpk))
    sftp = cli.open_sftp()
    sftp.get(remote_fpk, local_fpk)
    sftp.close()
    print("   -> %s（%d 字节）" % (local_fpk, os.path.getsize(local_fpk)))

    print("== 6. 校验包内清单 ==")
    with tarfile.open(local_fpk) as tf:
        names = tf.getnames()
    bad = [n for n in names
           if os.path.basename(n) in JUNK_NAMES
           or os.path.basename(n).startswith(JUNK_PREFIXES)]
    print("   包内条目 %d 个" % len(names))
    print("   垃圾文件：%s" % ("无 ✔" if not bad else "有 ✘ " + str(bad)))

    if not args.keep_remote:
        run("rm -rf %s" % REMOTE_BUILD)
        print("== 7. 已清理 NAS 临时目录 ==")
    cli.close()
    os.remove(tgz)
    print("\n完成：%s" % local_fpk)
    print("安装方式：飞牛「应用中心 → 手动安装」选择该文件。")


if __name__ == "__main__":
    main()
