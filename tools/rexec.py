# -*- coding: utf-8 -*-
"""在 Windows 上远程执行 Linux 命令（无 sshpass，走 paramiko 密码认证）。

用法：
  python rexec.py -c "uptime; uname -r"          # 单条命令
  python rexec.py -f script.sh                   # 把本地脚本喂给远端 bash -s
  python rexec.py -f -                           # 从本地 stdin 读脚本

凭证：默认读 %TEMP%/.nas_cred.json
      {"host":"<NAS_IP>","port":22,"user":"<用户名>","password":"<密码>"}
      也可用环境变量 NAS_PW / NAS_USER / NAS_HOST 覆盖。
"""
import argparse
import json
import os
import sys

import paramiko

CRED_DEFAULT = os.path.join(os.environ.get("TEMP", os.path.expanduser("~")),
                            ".nas_cred.json")


def load_cred(args):
    cred = {"host": "", "port": 22, "user": "", "password": ""}
    path = os.environ.get("NAS_CRED", CRED_DEFAULT)
    if os.path.exists(path):
        cred.update(json.load(open(path, encoding="utf-8")))
    env_map = {"host": "NAS_HOST", "port": "NAS_PORT",
               "user": "NAS_USER", "password": "NAS_PW"}
    for k, env in env_map.items():
        if os.environ.get(env):
            cred[k] = int(os.environ[env]) if k == "port" else os.environ[env]
    for k in ("host", "user", "password"):
        if args.__dict__.get(k):
            cred[k] = args.__dict__[k]
    if not cred["password"]:
        sys.exit("缺少密码：请写 %s 或设置 NAS_PW" % path)
    return cred


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--command")
    ap.add_argument("-f", "--file")
    ap.add_argument("-H", "--host")
    ap.add_argument("-u", "--user")
    ap.add_argument("-p", "--password")
    ap.add_argument("-t", "--timeout", type=int, default=120)
    args = ap.parse_args()

    cred = load_cred(args)
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(hostname=cred["host"], port=cred.get("port", 22),
                username=cred["user"], password=cred["password"],
                timeout=15, banner_timeout=30, auth_timeout=30,
                look_for_keys=False, allow_agent=False)

    if args.file:
        payload = (sys.stdin.read() if args.file == "-"
                   else open(args.file, encoding="utf-8").read())
        cmd = "bash -s"
    else:
        payload = args.command or sys.stdin.read()
        cmd = "bash -c " + json.dumps(payload)
        payload = None

    stdin, stdout, stderr = cli.exec_command(cmd, timeout=args.timeout,
                                             get_pty=False)
    if payload is not None:
        stdin.write(payload)
    stdin.channel.shutdown_write()

    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    cli.close()

    if out:
        sys.stdout.write(out)
    if err:
        sys.stdout.write("\n--- stderr ---\n" + err)
    print("\n--- exit: %d ---" % rc)
    sys.exit(rc if rc else 0)


if __name__ == "__main__":
    main()
