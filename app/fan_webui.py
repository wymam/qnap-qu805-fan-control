#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fnos-fan-webui — QU805 (iEi SA145 / ITE8528E EC) 温控风扇 Web UI
通过 qnap8528 驱动暴露的 hwmon sysfs 控制风扇，按 CPU 温度自动调速，
支持高温滞后保持（防止温度在阈值附近抖动导致转速频繁切换），
并支持机械硬盘高温保护（任一机械盘达阈值 → 提到指定转速并持续一段时间）。

机械盘温度来自内核 drivetemp 模块（宿主机需 modprobe drivetemp）：
它把每块 SATA 盘的 SMART 温度暴露成一个 hwmon 设备（name=drivetemp）。
本程序用「hwmon 的 device 软链名」与「/sys/block/sdX/device 软链名」
（两者都是 SCSI 地址，如 2:0:0:0）配对，从而拿到 sdX 并读 queue/rotational
区分机械盘与 SSD。容器无需额外挂载，默认的 /sys 就够。
"""

import glob
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

DATA_DIR = "/data"
CFG_FILE = os.path.join(DATA_DIR, "curve-config.json")
LOG_FILE = os.path.join(DATA_DIR, "fan.log")   # 明文日志，按天保留
HIST_FILE = os.path.join(DATA_DIR, "history.tsv")   # 历史采样，界面曲线用
PORT = 8080

# 默认档位：t <= below 则取该档 pct（按低于阈值匹配，从上往下第一个命中）
DEFAULT_CFG = {
    "temp_source": "coretemp",          # coretemp | qnap8528 | max
    "mode": "auto",                     # auto | manual
    "manual_pct": 80,
    "pending_pct": None,
    "steps": [
        {"below": 60,    "pct": 60},
        {"below": 79.9,  "pct": 80},
        {"below": 89.9,  "pct": 90},
        {"below": 999,   "pct": 100},
    ],
    "hold_trigger": 85,     # 超过此温度触发滞后保持
    "hold_seconds": 300,    # 保持时长（秒）
    "min_pct": 40,          # 安全下限，防止风扇停转
    "interval": 5,          # 采样间隔（秒）

    # ---- 机械硬盘高温保护 ----
    "hdd_enabled": True,        # 是否启用
    "hdd_trigger_temp": 44,     # 任一被监视磁盘达到此温度即触发
    "hdd_pct": 90,              # 触发后风扇转速下限（%）
    "hdd_hold_seconds": 300,    # 触发后保持时长（秒）
    "hdd_watch": "rotational",  # rotational=仅机械盘 | all=全部SATA盘 | list=自定义
    "hdd_sensors": [],          # hdd_watch=list 时要监视的设备名，如 ["sdb","sdc"]

    # ---- 日志 ----
    "log_retention_days": 7,    # 日志保留天数，超期自动删除

    # ---- 历史曲线 ----
    "hist_retention_days": 7,   # 历史采样保留天数，超期自动删除
}

cfg = {}
state = {
    "temp": None, "pwm": None, "pct": None, "rpm": [], "source": "",
    "hold_until": 0, "hold_pct": None, "last_update": 0, "log": [],
    "hdd_disks": [], "hdd_max": None, "hdd_hold_until": 0, "hdd_active": False,
}
lock = threading.Lock()


# ---------------- sysfs 辅助 ----------------
def rd(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def wr(path, val):
    try:
        with open(path, "w") as f:
            f.write(str(val))
        return True
    except Exception:
        return False


def link_base(path):
    """读软链的「文本」取末段。即使目标不在可见范围内也能拿到，
    例如 /sys/class/hwmon/hwmon6/device -> ../../../2:0:0:0 取到 2:0:0:0。"""
    try:
        return os.path.basename(os.readlink(path))
    except Exception:
        return None


def log(msg):
    """写运行日志：内存环形缓冲（界面用）+ 追加明文文件（重启不丢）。"""
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    with lock:
        state["log"].append(line)
        state["log"] = state["log"][-200:]
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


_last_prune = 0.0


def prune_log(force=False):
    """删掉超过 log_retention_days 天的日志行。每小时最多跑一次（force 除外）。"""
    global _last_prune
    now = time.time()
    if not force and now - _last_prune < 3600:
        return 0
    _last_prune = now
    try:
        days = float(cfg.get("log_retention_days", 7) or 7)
    except Exception:
        days = 7
    cutoff = now - days * 86400
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return 0
    keep, dropped = [], 0
    for ln in lines:
        try:
            t = time.mktime(time.strptime(ln[:19], "%Y-%m-%d %H:%M:%S"))
        except Exception:
            keep.append(ln)          # 解析不了的行一律保留，宁可多留不可误删
            continue
        if t >= cutoff:
            keep.append(ln)
        else:
            dropped += 1
    if dropped:
        tmp = LOG_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(keep)
            os.replace(tmp, LOG_FILE)
        except Exception:
            return 0
    return dropped


def read_log_tail(n=200):
    """读日志末尾 n 行；n=0 表示全部。文件很大时只读尾部 256KB。"""
    try:
        size = os.path.getsize(LOG_FILE)
    except Exception:
        return []
    try:
        with open(LOG_FILE, "rb") as f:
            if size > 262144:
                f.seek(size - 262144)
                f.readline()          # 丢掉可能被截断的半行
            data = f.read().decode("utf-8", "replace")
    except Exception:
        return []
    lines = data.splitlines()
    return lines[-n:] if n else lines


# ---------------- 历史采样（界面曲线） ----------------
# 每轮控制循环记一条：epoch,CPU温度,机械盘最高温,指令转速%,风扇RPM均值（无值留空）
# 先缓存在内存，累计约 60 秒再落盘一次，减少磁盘写入。
HIST_TAIL_BYTES = 8 * 1024 * 1024      # 读文件时最多回看 8MB（覆盖 7 天以上采样）
_hist_buf = []
_hist_lock = threading.Lock()
_last_hist_prune = 0.0


def _fmt_num(v, nd=1):
    if v is None:
        return ""
    try:
        return ("%." + str(int(nd)) + "f") % float(v)
    except Exception:
        return ""


def record_history(temp, hdd, pct, rpms):
    """记一条历史采样。四个值全为空则不记（避免写入无意义的空行）。"""
    rpm = None
    try:
        vals = [float(x["rpm"]) for x in (rpms or []) if x.get("rpm") is not None]
        if vals:
            rpm = sum(vals) / len(vals)
    except Exception:
        rpm = None
    if temp is None and hdd is None and pct is None and rpm is None:
        return
    line = "%d,%s,%s,%s,%s\n" % (int(time.time()), _fmt_num(temp),
                                 _fmt_num(hdd), _fmt_num(pct, 0), _fmt_num(rpm, 0))
    with _hist_lock:
        _hist_buf.append(line)
        if len(_hist_buf) >= 12:
            _flush_history_locked()


def _flush_history_locked():
    if not _hist_buf:
        return
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(HIST_FILE, "a", encoding="utf-8") as f:
            f.writelines(_hist_buf)
        del _hist_buf[:]
    except Exception:
        pass


def flush_history():
    with _hist_lock:
        _flush_history_locked()


def prune_history(force=False):
    """删掉超过 hist_retention_days 天的采样。每小时最多跑一次（force 除外）。"""
    global _last_hist_prune
    now = time.time()
    if not force and now - _last_hist_prune < 3600:
        return 0
    _last_hist_prune = now
    flush_history()
    try:
        days = float(cfg.get("hist_retention_days", 7) or 7)
    except Exception:
        days = 7
    cutoff = int(now - days * 86400)
    try:
        with open(HIST_FILE, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return 0
    keep, dropped = [], 0
    for ln in lines:
        try:
            ts = int(ln.split(",", 1)[0])
        except Exception:
            keep.append(ln)          # 认不出来的行一律保留，宁可多留不可误删
            continue
        if ts >= cutoff:
            keep.append(ln)
        else:
            dropped += 1
    if dropped:
        tmp = HIST_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(keep)
            os.replace(tmp, HIST_FILE)
        except Exception:
            return 0
    return dropped


def parse_range(s):
    """把 1h / 6h / 24h / 7d 或纯秒数解析成秒（限制 5 分钟 ~ 90 天）。"""
    s = (s or "").strip().lower()
    try:
        if s.endswith("d"):
            secs = int(float(s[:-1]) * 86400)
        elif s.endswith("h"):
            secs = int(float(s[:-1]) * 3600)
        elif s.endswith("m"):
            secs = int(float(s[:-1]) * 60)
        else:
            secs = int(float(s))
    except Exception:
        secs = 6 * 3600
    return max(300, min(90 * 86400, secs))


def read_history(seconds, points=360):
    """按时间桶聚合历史采样，返回曲线数据（桶内取平均，另附区间极值统计）。

    无论跨 1 小时还是 7 天，返回的点数都固定在 ~points 个，
    因此前端不会因为切换大跨度而变卡或被大数据量拖死。
    """
    flush_history()
    now = int(time.time())
    frm = now - int(seconds)
    try:
        size = os.path.getsize(HIST_FILE)
    except Exception:
        size = 0
    rows = []
    if size:
        try:
            with open(HIST_FILE, "rb") as f:
                if size > HIST_TAIL_BYTES:
                    f.seek(size - HIST_TAIL_BYTES)
                    f.readline()          # 丢掉可能被截断的半行
                rows = f.read().decode("utf-8", "replace").splitlines()
        except Exception:
            rows = []

    n = max(30, min(2000, int(points)))
    bucket = max(1, int(round(max(1, int(seconds)) / float(n))))

    FIELDS = ("cpu", "hdd", "pct", "rpm")
    acc, g, count = {}, {}, 0
    for f in FIELDS:
        g[f] = [0, 0.0, None, None]      # [条数, 累加, 最小, 最大]

    for ln in rows:
        p = ln.split(",")
        if len(p) != 5:
            continue
        try:
            ts = int(p[0])
        except Exception:
            continue
        if ts < frm or ts > now:
            continue
        count += 1
        k = ts // bucket
        a = acc.get(k)
        if a is None:
            a = acc[k] = {"n": 0}
            for f in FIELDS:
                a[f + "_s"] = 0.0
                a[f + "_n"] = 0
        a["n"] += 1
        for f, idx in (("cpu", 1), ("hdd", 2), ("pct", 3), ("rpm", 4)):
            if not p[idx]:
                continue
            try:
                v = float(p[idx])
            except Exception:
                continue
            a[f + "_s"] += v
            a[f + "_n"] += 1
            gg = g[f]
            gg[0] += 1
            gg[1] += v
            gg[2] = v if gg[2] is None else min(gg[2], v)
            gg[3] = v if gg[3] is None else max(gg[3], v)

    b0, b1 = frm // bucket, now // bucket
    t_l = []
    series = {}
    for f in FIELDS:
        series[f] = []
    for k in range(b0, b1 + 1):
        t_l.append(k * bucket)
        a = acc.get(k)
        for f in FIELDS:
            if not a or not a[f + "_n"]:
                series[f].append(None)
                continue
            v = a[f + "_s"] / a[f + "_n"]
            series[f].append(round(v) if f == "rpm" else round(v, 1))

    stats = {}
    for f in FIELDS:
        cnt, tot, mn, mx = g[f]
        if not cnt:
            stats[f] = None
        elif f == "rpm":
            stats[f] = {"min": round(mn), "avg": round(tot / cnt), "max": round(mx)}
        else:
            stats[f] = {"min": round(mn, 1), "avg": round(tot / cnt, 1), "max": round(mx, 1)}

    return {
        "range": int(seconds), "bucket": bucket, "from": frm, "to": now,
        "count": count, "bytes": size,
        "t": t_l, "cpu": series["cpu"], "hdd": series["hdd"],
        "pct": series["pct"], "rpm": series["rpm"], "stats": stats,
    }


def find_hwmon(name):
    for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        if rd(os.path.join(d, "name")) == name:
            return d
    return None


def read_temp_candidates():
    """返回 {来源名: 温度(摄氏度)}"""
    out = {}
    d = find_hwmon("coretemp")
    if d:
        # coretemp 通常 temp1_input 为 Package id 0
        v = rd(os.path.join(d, "temp1_input"))
        if v:
            try:
                out["coretemp"] = int(v) / 1000.0
            except ValueError:
                pass
    d = find_hwmon("qnap8528")
    if d:
        vals = []
        for i in range(1, 9):
            v = rd(os.path.join(d, "temp%d_input" % i))
            if v:
                try:
                    vals.append(int(v) / 1000.0)
                except ValueError:
                    pass
        if vals:
            out["qnap8528"] = max(vals)   # EC 多个传感器取最高
    return out


def read_rpm():
    d = find_hwmon("qnap8528")
    if not d:
        return []
    out = []
    for i in range(1, 9):
        v = rd(os.path.join(d, "fan%d_input" % i))
        if v:
            try:
                out.append({"fan": i, "rpm": int(v)})
            except ValueError:
                pass
    return out


def get_pwm_path():
    d = find_hwmon("qnap8528")
    if not d:
        return None
    for i in range(1, 5):
        p = os.path.join(d, "pwm%d" % i)
        if os.path.exists(p):
            return p
    return None


def pct_to_pwm(pct):
    return max(0, min(255, int(round(pct * 255 / 100.0))))


def target_pct(t):
    for s in cfg.get("steps", DEFAULT_CFG["steps"]):
        if t <= s["below"]:
            return s["pct"]
    return cfg.get("steps", DEFAULT_CFG["steps"])[-1]["pct"]


# ---------------- 磁盘（drivetemp） ----------------
def scan_disks(watch=None):
    """返回磁盘列表：[{dev,hwmon,addr,temp,rotational,model,watched}]"""
    blk = {}
    for sd in sorted(glob.glob("/sys/block/sd*")):
        dev = os.path.basename(sd)
        addr = link_base(os.path.join(sd, "device"))
        rot = rd(os.path.join(sd, "queue/rotational"))
        model = rd(os.path.join(sd, "device", "model")) or ""
        if addr:
            blk[addr] = {
                "dev": dev,
                "rotational": int(rot) if (rot and rot.lstrip("-").isdigit()) else -1,
                "model": model.strip(),
            }

    disks = []
    for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        if rd(os.path.join(d, "name")) != "drivetemp":
            continue
        addr = link_base(os.path.join(d, "device"))
        v = rd(os.path.join(d, "temp1_input"))
        if v is None:
            continue
        try:
            temp = int(v) / 1000.0
        except ValueError:
            continue
        info = blk.get(addr) or {}
        disks.append({
            "dev": info.get("dev", "?"),
            "hwmon": os.path.basename(d),
            "addr": addr or "?",
            "temp": temp,
            # -1 表示拿不到 rotational（/sys/block 不可见），按「未知」处理
            "rotational": info.get("rotational", -1),
            "model": info.get("model", ""),
            "watched": False,
        })

    if watch is None:
        watch = cfg.get("hdd_watch", "rotational")
    if watch == "all":
        picked = disks
    elif watch == "list":
        want = set(cfg.get("hdd_sensors") or [])
        picked = [x for x in disks if x["dev"] in want]
    else:   # rotational：排除已知的 SSD（rotational==0），未知(-1)保留
        picked = [x for x in disks if x["rotational"] != 0]
    for x in picked:
        x["watched"] = True
    return disks, picked


# ---------------- 控制循环 ----------------
def apply_pwm(pct, reason=""):
    p = get_pwm_path()
    if not p:
        log("未找到 PWM 接口")
        return False
    ok = wr(p, pct_to_pwm(pct))
    if ok:
        with lock:
            state["pct"] = pct
            state["pwm"] = pct_to_pwm(pct)
            state["last_update"] = time.time()
        if reason:
            log(reason)
    return ok


def now_str():
    return time.strftime("%H:%M:%S")


def control_loop():
    last_pct = None
    while True:
        try:
            temps = read_temp_candidates()
            src = cfg.get("temp_source", "coretemp")
            if src == "max" and temps:
                t = max(temps.values())
            elif src in temps:
                t = temps[src]
            elif temps:
                t = max(temps.values())
            else:
                t = None

            prune_log()          # 每小时最多执行一次
            prune_history()      # 同上

            # ---- 磁盘温度 ----
            all_disks, watched = scan_disks()
            hdd_max = max([x["temp"] for x in watched], default=None)

            with lock:
                state["temp"] = round(t, 1) if t is not None else None
                state["rpm"] = read_rpm()
                state["source"] = src
                state["hdd_disks"] = all_disks
                state["hdd_max"] = round(hdd_max, 1) if hdd_max is not None else None

            if t is not None:
                hold_trigger = float(cfg.get("hold_trigger", 85))
                hold_seconds = float(cfg.get("hold_seconds", 300))
                min_pct = float(cfg.get("min_pct", 40))
                now = time.time()

                if cfg.get("mode", "auto") == "manual":
                    pct = float(cfg.get("manual_pct", 80))
                    reason = "手动模式 %d%%" % pct
                else:
                    # 高温触发滞后保持（hold_seconds<=0 视为未启用，避免日志里出现
                    # 「触发保持 xx% / 0s」这种等于没生效的误导性文案）
                    holding = hold_seconds > 0 and now < state["hold_until"]
                    if hold_seconds > 0 and t > hold_trigger:
                        state["hold_until"] = now + hold_seconds
                        state["hold_pct"] = target_pct(t)
                        holding = True
                        reason = "高温 %.1f°C 触发保持 %d%% / %ds" % (
                            t, state["hold_pct"], int(hold_seconds))
                    elif holding:
                        reason = "保持中（剩余 %ds）" % int(state["hold_until"] - now)
                    else:
                        reason = ""

                    if holding and state["hold_pct"] is not None:
                        pct = state["hold_pct"]
                    else:
                        pct = target_pct(t)
                        if reason == "":
                            reason = "%.1f°C → %d%%" % (t, pct)

                # ---- 机械盘高温保护（作为转速下限，两种模式下都生效） ----
                was_active = bool(state.get("hdd_active"))
                if cfg.get("hdd_enabled", True) and hdd_max is not None:
                    trig_t = float(cfg.get("hdd_trigger_temp", 44))
                    hold_s = float(cfg.get("hdd_hold_seconds", 300))
                    if hdd_max >= trig_t:
                        state["hdd_hold_until"] = now + hold_s
                active = now < float(state.get("hdd_hold_until") or 0)

                if active:
                    hdd_pct = float(cfg.get("hdd_pct", 90))
                    if not was_active:
                        log("机械盘高温 %.1f°C ≥ %.1f°C，转速提到 %d%%，保持 %ds"
                            % (hdd_max or 0, float(cfg.get("hdd_trigger_temp", 44)),
                               hdd_pct, int(float(cfg.get("hdd_hold_seconds", 300)))))
                    if pct < hdd_pct:
                        pct = hdd_pct
                        reason = "机械盘 %.1f°C 保护中 %d%%（剩余 %ds）" % (
                            hdd_max or 0, hdd_pct,
                            int(state["hdd_hold_until"] - now))
                elif was_active:
                    log("机械盘高温保护结束，恢复常规调速")
                state["hdd_active"] = active

                pct = max(min_pct, min(100, pct))
                if last_pct is None or abs(pct - last_pct) >= 0.5:
                    apply_pwm(pct, reason)
                    last_pct = pct

            # ---- 记录一条历史采样（t 为 None 时也会记录盘温与转速） ----
            with lock:
                _rt, _rh, _rp = state["temp"], state["hdd_max"], state["pct"]
                _rr = list(state["rpm"])
            record_history(_rt, _rh, _rp, _rr)
        except Exception as e:
            log("异常: %s" % e)

        time.sleep(float(cfg.get("interval", 5)))


# ---------------- 配置持久化 ----------------
def load_cfg():
    global cfg
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    if os.path.isfile(CFG_FILE):
        try:
            with open(CFG_FILE) as f:
                cfg.update(json.load(f))
        except Exception:
            pass


def save_cfg():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CFG_FILE, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------- HTTP ----------------
HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>QU805 风扇温控</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box}
body{margin:0;background:#12141a;color:#e6e8ee;font:13px/1.5 -apple-system,"PingFang SC",system-ui,sans-serif;padding:12px 14px}
.wrap{display:grid;gap:10px;max-width:2200px;margin:0 auto}
.top{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
h1{font-size:17px;margin:0;font-weight:600}
.top .sub{color:#6b7488;font-size:12px}
.tags{display:flex;gap:6px;align-items:center;margin-left:auto;flex-wrap:wrap}
.metrics{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px}
.metric{background:#1b1f28;border:1px solid #2a3040;border-radius:8px;padding:7px 10px;min-width:0}
.metric .k{font-size:11px;color:#8b93a7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.metric .v{font-size:19px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.metric .v small{font-size:11px;color:#8b93a7;font-weight:400}
.hot{color:#ff6b6b}.warm{color:#ffa94d}.cool{color:#51cf66}
/* 两栏：左＝历史/手动/日志，右＝档位规则/硬盘保护 */
.cols{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(430px,1fr);gap:10px;align-items:start}
.panel{background:#1b1f28;border:1px solid #2a3040;border-radius:10px;min-width:0}
.sub{padding:11px 13px;min-width:0}
.sub+.sub{border-top:1px solid #262c3a}
.mhead{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.mhead h2{font-size:13px;margin:0;font-weight:600;color:#cfd6e6}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:2px 6px;border-bottom:1px solid #262c3a}
th{color:#8b93a7;font-weight:500;font-size:11px}
td input{padding:3px 6px}
.steps-box{max-height:220px;overflow:auto}
input,select{background:#12141a;color:#e6e8ee;border:1px solid #2f3648;border-radius:6px;padding:5px 7px;width:100%;font-size:12px}
button{background:#3b7cff;color:#fff;border:0;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:12px}
button.gray{background:#2f3648}
button.mini{padding:4px 9px;font-size:11px}
button:active{opacity:.82}
.btn-dirty{background:#8a5a12;color:#ffd8a8;box-shadow:inset 0 0 0 1px #b8862b}
.rbtn.on{background:#3b7cff;box-shadow:inset 0 0 0 1px #7aa6ff}
.line{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-bottom:7px}
.line:last-child{margin-bottom:0}
.lb{font-size:11px;color:#8b93a7;white-space:nowrap}
.spacer{margin-left:auto}
.tag{display:inline-block;padding:2px 7px;border-radius:5px;font-size:11px;background:#2f3648;color:#b6c0d6;white-space:nowrap}
.tag.on{background:#1f6f3f;color:#8ce0a8}
.tag.hold{background:#7a4a12;color:#ffc078}
.tag.off{background:#3a2020;color:#e08a8a}
.chartbox{position:relative;height:min(292px,34vh);min-height:170px;background:#10131a;border-radius:8px;overflow:hidden}
.chartbox svg{width:100%;height:100%;display:block}
.tip{position:absolute;display:none;pointer-events:none;background:#0e1117;border:1px solid #2f3648;border-radius:6px;padding:6px 9px;font-size:11px;line-height:1.6;color:#cfd6e6;white-space:nowrap;z-index:5;box-shadow:0 6px 18px rgba(0,0,0,.45)}
.legend{display:flex;gap:12px;flex-wrap:wrap;align-items:center;font-size:12px;color:#b6c0d6;margin-top:7px}
.legend label{display:flex;gap:5px;align-items:center;cursor:pointer;user-select:none}
.legend i{width:12px;height:3px;border-radius:2px;display:inline-block}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;margin-top:7px}
.stat{background:#151922;border:1px solid #262c3a;border-radius:7px;padding:4px 8px;font-size:11px;color:#8b93a7;min-width:0}
.stat b{display:block;font-size:13px;color:#e6e8ee;font-weight:600;margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.log{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:#8b93a7;max-height:min(108px,14vh);overflow:auto;white-space:pre-wrap;word-break:break-all}
.hint{font-size:11px;color:#6b7488;line-height:1.5;margin-top:6px}
.mono{font-family:ui-monospace,Menlo,monospace}
.warn{color:#ffa94d}
input[type=range]{width:100%;padding:0}
input[type=checkbox]{width:auto}
@media (max-width:1180px){
  .metrics{grid-template-columns:repeat(3,minmax(0,1fr))}
  .cols{grid-template-columns:1fr}
  .stats{grid-template-columns:repeat(2,minmax(0,1fr))}
}
</style></head><body>
<div class="wrap">

<div class="top">
  <h1>QU805 风扇温控</h1>
  <span class="sub">qnap8528 EC · ITE8528E</span>
  <div class="tags">
    <span id="dirtyTag" class="tag hold" style="display:none">有未保存的修改 · 自动刷新已暂停覆盖</span>
    <span id="mode"></span>
  </div>
</div>

<div class="metrics">
  <div class="metric"><div class="k">CPU 温度</div><div class="v" id="temp">--</div></div>
  <div class="metric"><div class="k">机械盘最高温</div><div class="v" id="hddmax">--</div></div>
  <div class="metric"><div class="k">风扇转速</div><div class="v" id="pct">--</div></div>
  <div class="metric"><div class="k">PWM 原始值</div><div class="v" id="pwm">--</div></div>
  <div class="metric"><div class="k">实测 RPM</div><div class="v" id="rpm">--</div></div>
  <div class="metric"><div class="k">生效温度源</div><div class="v" id="src" style="font-size:15px">--</div></div>
</div>

<div class="cols">

  <!-- ============ 左：历史曲线 + 手动模式 + 运行日志 ============ -->
  <section class="panel">
    <div class="sub">
      <div class="mhead">
        <h2>温度 / 转速历史</h2>
        <button class="gray mini rbtn" data-r="1h" onclick="setRange('1h')">1 小时</button>
        <button class="gray mini rbtn" data-r="6h" onclick="setRange('6h')">6 小时</button>
        <button class="gray mini rbtn" data-r="24h" onclick="setRange('24h')">24 小时</button>
        <button class="gray mini rbtn" data-r="7d" onclick="setRange('7d')">7 天</button>
        <span id="histmeta" class="tag spacer">--</span>
      </div>
      <div class="chartbox" id="chartbox">
        <svg id="chart"></svg>
        <div id="tip" class="tip"></div>
      </div>
      <div class="legend" id="legend"></div>
      <div class="stats" id="histstats"></div>
    </div>

    <div class="sub">
      <div class="mhead">
        <h2>手动模式</h2>
        <button class="gray mini" onclick="setPending()">暂存</button>
        <button class="mini" onclick="applyPending()">确认应用</button>
        <button class="gray mini" onclick="setMode('auto')">切回自动</button>
        <span class="lb spacer">需「确认应用」才写入硬件</span>
      </div>
      <div class="line" style="margin-bottom:0">
        <input type="range" id="slider" min="0" max="100" step="1" value="80" oninput="document.getElementById('sv').textContent=this.value+'%'">
        <span id="sv" class="tag" style="min-width:46px;text-align:center">80%</span>
      </div>
    </div>

    <div class="sub">
      <div class="mhead">
        <h2>运行日志</h2>
        <span class="lb">保留天数（日志 + 历史）</span>
        <input id="retention_days" type="number" step="1" min="1" max="365" style="width:58px" title="日志与历史采样共用同一个保留天数">
        <button id="saveLogBtn" class="mini" onclick="saveLogCfg()">保存</button>
        <a href="/api/log?lines=0" target="_blank" style="font-size:11px;color:#3b7cff;text-decoration:none">完整日志</a>
        <span id="logmeta" class="tag spacer">--</span>
        <button class="gray mini" id="logToggleBtn" onclick="toggleLog()">收起</button>
      </div>
      <div class="log" id="log"></div>
    </div>
  </section>

  <!-- ============ 右：温度档位规则 + 机械硬盘高温保护 ============ -->
  <section class="panel">
    <div class="sub">
      <div class="mhead">
        <h2>温度档位规则</h2>
        <button id="saveCurveBtn" class="mini spacer" onclick="saveCurve()">保存规则</button>
        <button class="gray mini" onclick="addStep()">+ 档位</button>
      </div>
      <div class="steps-box">
        <table><thead><tr><th>温度不高于 (°C)</th><th>风扇转速 (%)</th><th style="width:44px"></th></tr></thead>
        <tbody id="steps"></tbody></table>
      </div>
      <div class="line" style="margin-top:8px">
        <span class="lb">高温触发</span><input id="hold_trigger" type="number" step="0.1" style="width:62px">
        <span class="lb">保持(秒)</span><input id="hold_seconds" type="number" step="1" style="width:64px">
        <span class="lb">安全下限</span><input id="min_pct" type="number" step="1" style="width:56px">
      </div>
      <div class="line">
        <span class="lb">温度源</span>
        <select id="temp_source" style="width:148px">
          <option value="coretemp">CPU (coretemp)</option>
          <option value="qnap8528">EC 传感器 (qnap8528)</option>
          <option value="max">两者取最高</option>
        </select>
      </div>
      <div class="hint">超过「高温触发」后即使温度回落，也保持触发时的档位运行「保持」秒；填 0 关闭该功能。</div>
    </div>

    <div class="sub">
      <div class="mhead"><h2>机械硬盘高温保护</h2></div>
      <div class="line">
        <label class="lb" style="display:flex;gap:5px;align-items:center;cursor:pointer"><input type="checkbox" id="hdd_enabled"> 启用</label>
        <span class="lb" title="任一被监视磁盘达到此温度即触发">温度</span><input id="hdd_trigger_temp" type="number" step="0.1" style="width:58px">
        <span class="lb" title="触发后风扇转速的下限">转速</span><input id="hdd_pct" type="number" step="1" style="width:54px">
        <span class="lb" title="触发后维持的秒数">持续</span><input id="hdd_hold_seconds" type="number" step="1" style="width:60px">
        <span class="lb" title="哪些磁盘参与监视">范围</span>
        <select id="hdd_watch" style="width:128px">
          <option value="rotational">仅机械硬盘</option>
          <option value="all">全部 SATA 盘</option>
          <option value="list">自定义列表</option>
        </select>
      </div>
      <div class="line">
        <span id="hddlist_row" style="display:none;align-items:center;gap:6px">
          <span class="lb">设备</span><input id="hdd_sensors" type="text" style="width:160px" placeholder="sdb,sdc,sdd,sde">
        </span>
        <button id="saveHddBtn" onclick="saveHdd()">保存</button>
        <span id="hddstate" class="tag spacer">--</span>
      </div>
      <table><thead><tr>
        <th style="width:64px">设备</th><th>型号</th><th style="width:66px">类型</th><th style="width:76px">温度</th><th style="width:84px">参与监视</th>
      </tr></thead><tbody id="hddtable"></tbody></table>
    </div>
  </section>

</div>
</div>
<script>
async function api(u,o){const r=await fetch(u,o);return r.json()}
function cls(t){return t>=85?'hot':(t>=70?'warm':'cool')}
function hcls(t){return t>=44?'hot':(t>=40?'warm':'cool')}
const NL=`
`;
let cur=null;

async function refresh(){
  const s=await api('/api/status');cur=s;
  const g=id=>document.getElementById(id);
  g('temp').innerHTML=s.temp==null?'--':`<span class="${cls(s.temp)}">${s.temp} <small>°C</small></span>`;
  g('hddmax').innerHTML=s.hdd_max==null?'--':`<span class="${hcls(s.hdd_max)}">${s.hdd_max} <small>°C</small></span>`;
  g('pct').innerHTML=s.pct==null?'--':`${s.pct} <small>%</small>`;
  g('pwm').textContent=s.pwm==null?'--':s.pwm+' / 255';
  g('rpm').innerHTML=(s.rpm||[]).map(r=>`${r.rpm}<small> RPM</small>`).join(' / ')||'--';
  const SRC={coretemp:'CPU (coretemp)',qnap8528:'EC 传感器',max:'两者取最高'};
  g('src').textContent=SRC[s.temp_source]||s.temp_source||'--';
  let m=`<span class="tag ${s.mode==='auto'?'on':''}">${s.mode==='auto'?'自动':'手动'}</span>`;
  const left=Math.max(0,Math.round(s.hold_remaining||0));
  if(left>0)m+=` <span class="tag hold">保持中 ${left}s</span>`;
  const hl=Math.max(0,Math.round(s.hdd_hold_remaining||0));
  if(hl>0)m+=` <span class="tag hold">硬盘保护 ${hl}s</span>`;
  g('mode').innerHTML=m;

  const hs=g('hddstate');
  if(!s.hdd_enabled){hs.className='tag off';hs.textContent='硬盘保护已关闭';}
  else if(hl>0){hs.className='tag hold';hs.textContent=`保护生效中 · 剩余 ${hl}s（转速下限 ${s.hdd_pct}%）`;}
  else if(s.hdd_max==null){hs.className='tag off';hs.textContent='无磁盘温度传感器（宿主机需 modprobe drivetemp）';}
  else {hs.className='tag on';hs.textContent=`待命 · 机械盘最高 ${s.hdd_max}°C / 阈值 ${s.hdd_trigger_temp}°C`;}

  g('hddtable').innerHTML=(s.hdd_disks||[]).map(d=>{
    const ty=d.rotational===1?'机械盘':(d.rotational===0?'SSD':'未知');
    return `<tr><td class="mono">${d.dev}</td><td>${d.model||'--'}</td><td>${ty}</td>
      <td><span class="${hcls(d.temp)}">${d.temp} °C</span></td>
      <td>${d.watched?'<span class="tag on">是</span>':'<span class="tag">否</span>'}</td></tr>`;
  }).join('')||'<tr><td colspan="5" class="warn">没有 drivetemp 传感器</td></tr>';

  g('logmeta').textContent=`日志 ${((s.log_bytes||0)/1024).toFixed(1)} KB · ${(s.log||[]).length} 行 · 历史 ${((s.hist_bytes||0)/1024).toFixed(1)} KB`;
  if(logOpen)g('log').textContent=(s.log||[]).join(NL);

  if(!stepsDirty)renderSteps(s.steps);
  setVal('hold_trigger',s.hold_trigger);
  setVal('hold_seconds',s.hold_seconds);
  setVal('min_pct',s.min_pct);
  setVal('temp_source',s.temp_source);
  setVal('slider',s.manual_pct);
  if(!dirty.has('slider'))g('sv').textContent=s.manual_pct+'%';
  setVal('hdd_enabled',s.hdd_enabled);
  setVal('hdd_trigger_temp',s.hdd_trigger_temp);
  setVal('hdd_pct',s.hdd_pct);
  setVal('hdd_hold_seconds',s.hdd_hold_seconds);
  setVal('hdd_watch',s.hdd_watch);
  setVal('hdd_sensors',(s.hdd_sensors||[]).join(','));
  if(!dirty.has('hdd_watch'))g('hddlist_row').style.display=(s.hdd_watch==='list')?'flex':'none';
  setVal('retention_days',s.retention_days);
}

// ===== 未保存修改保护 =====
// 改过的控件记入 dirty，自动刷新永不再覆盖它。
let stepSeq=0, dirty=new Set(), stepsDirty=false, stepIds=new Set();
const CURVE_IDS=['hold_trigger','hold_seconds','min_pct','temp_source'];
const HDD_IDS=['hdd_enabled','hdd_trigger_temp','hdd_pct','hdd_hold_seconds','hdd_watch','hdd_sensors'];
const RET_IDS=['retention_days'];
function anyDirty(ids){return ids.some(i=>dirty.has(i))}
function isStepEl(el){return !!(el&&el.classList&&el.classList.contains('step-input'))}
function setVal(id,v){
  const el=document.getElementById(id);
  if(!el||dirty.has(id)||document.activeElement===el)return;
  if(el.type==='checkbox')el.checked=!!v;else el.value=(v==null?'':v);
}
function clearDirty(pred){
  Array.from(dirty).forEach(id=>{if(pred(id))dirty.delete(id)});
  paintDirty();
}
function paintDirty(){
  const tag=document.getElementById('dirtyTag');
  if(tag)tag.style.display=dirty.size?'inline-block':'none';
  const b=[['saveCurveBtn',anyDirty(CURVE_IDS)||stepsDirty],
           ['saveHddBtn',anyDirty(HDD_IDS)],
           ['saveLogBtn',anyDirty(RET_IDS)]];
  b.forEach(p=>{
    const el=document.getElementById(p[0]);
    if(el)el.className=p[1]?'btn-dirty':(p[0]==='saveLogBtn'?'mini':'');
  });
}
document.addEventListener('input',e=>{
  const t=e.target;if(!t)return;
  if(t.id)dirty.add(t.id);
  if(isStepEl(t))stepsDirty=true;
  paintDirty();
});
document.addEventListener('change',e=>{
  const t=e.target;if(!t||!t.id)return;
  dirty.add(t.id);paintDirty();
});
function stepRow(x){
  const k=++stepSeq;
  return `<tr><td><input class="step-input" type="number" step="0.1" value="${x.below}" id="b${k}"></td>`+
    `<td><input class="step-input" type="number" step="1" value="${x.pct}" id="p${k}"></td>`+
    `<td><button class="gray mini" onclick="delStep(${k})">删</button></td></tr>`;
}
function renderSteps(steps){
  const rows=(steps||[]).map(stepRow).join('');
  const tb=document.getElementById('steps');
  if(tb.dataset.sig===rows)return;
  tb.innerHTML=rows;
  tb.dataset.sig=rows;
  stepIds=new Set(Array.from(tb.querySelectorAll('input.step-input')).map(e=>e.id));
}
function addStep(){
  if(!cur)return;
  const tb=document.getElementById('steps');
  tb.insertAdjacentHTML('beforeend',stepRow({below:999,pct:100}));
  tb.dataset.sig='';
  stepIds=new Set(Array.from(tb.querySelectorAll('input.step-input')).map(e=>e.id));
  stepsDirty=true;paintDirty();
}
function delStep(k){
  const r=document.getElementById('b'+k);
  if(!r)return;
  const tb=document.getElementById('steps');
  r.closest('tr').remove();
  tb.dataset.sig='';
  stepIds=new Set(Array.from(tb.querySelectorAll('input.step-input')).map(e=>e.id));
  stepsDirty=true;paintDirty();
}
function readSteps(){
  const out=[];
  document.getElementById('steps').querySelectorAll('tr').forEach(tr=>{
    const a=tr.querySelectorAll('input');
    if(a.length>=2)out.push({below:parseFloat(a[0].value),pct:parseFloat(a[1].value)});
  });
  out.sort((x,y)=>x.below-y.below);
  return out;
}
async function saveCurve(){
  const g=id=>document.getElementById(id);
  await api('/api/curve',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({steps:readSteps(),
      hold_trigger:parseFloat(g('hold_trigger').value),
      hold_seconds:parseFloat(g('hold_seconds').value),
      min_pct:parseFloat(g('min_pct').value),
      temp_source:g('temp_source').value})});
  stepsDirty=false;
  clearDirty(id=>CURVE_IDS.indexOf(id)>=0||stepIds.has(id));
  alert('规则已保存');refresh();
}
document.getElementById('hdd_watch').addEventListener('change',function(){
  document.getElementById('hddlist_row').style.display=(this.value==='list')?'flex':'none';
});
async function saveHdd(){
  const g=id=>document.getElementById(id);
  const r=await api('/api/hdd',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      hdd_enabled:g('hdd_enabled').checked,
      hdd_trigger_temp:parseFloat(g('hdd_trigger_temp').value),
      hdd_pct:parseFloat(g('hdd_pct').value),
      hdd_hold_seconds:parseFloat(g('hdd_hold_seconds').value),
      hdd_watch:g('hdd_watch').value,
      hdd_sensors:g('hdd_sensors').value.split(',').map(x=>x.trim()).filter(x=>x)
    })});
  clearDirty(id=>HDD_IDS.indexOf(id)>=0);
  alert(r.ok?'硬盘保护规则已保存':'保存失败');refresh();
}
// 界面上只留一个「保留天数」，保存时把日志与历史采样一起设成同一个值
async function saveLogCfg(){
  const v=parseFloat(document.getElementById('retention_days').value);
  const r=await api('/api/logcfg',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({log_retention_days:v,hist_retention_days:v})});
  clearDirty(id=>RET_IDS.indexOf(id)>=0);
  alert(r.ok?('已保存：日志与历史采样各保留 '+r.retention_days+' 天；本次清理日志 '+r.dropped+' 条、采样 '+r.hist_dropped+' 条'):'保存失败');
  refresh();loadHistory();
}
let logOpen=true;
function toggleLog(){
  logOpen=!logOpen;
  document.getElementById('log').style.display=logOpen?'block':'none';
  document.getElementById('logToggleBtn').textContent=logOpen?'收起':'展开';
  if(logOpen&&cur)document.getElementById('log').textContent=(cur.log||[]).join(NL);
}
async function setPending(){
  const v=parseInt(document.getElementById('slider').value);
  await api('/api/set-pwm-pending',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pct:v})});
  alert('已暂存 '+v+'%，点「确认应用」生效');
}
async function applyPending(){
  await api('/api/apply',{method:'POST'});
  clearDirty(id=>id==='slider');
  refresh();alert('已应用到硬件');
}
async function setMode(m){
  await api('/api/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:m})});
  refresh();
}

// ===== 温度 / 转速历史曲线 =====
// 纯手写 SVG，按容器实际像素绘制（不引外部库：镜像里没有外网）。
// 三根独立纵轴：温度 °C（左） / 指令转速 %（右） / 实测 RPM（最右）。
const SERIES=[
  {k:'cpu',label:'CPU 温度',color:'#4dabf7',axis:'L',unit:'°C',on:true},
  {k:'hdd',label:'机械盘最高温',color:'#ffa94d',axis:'L',unit:'°C',on:true},
  {k:'pct',label:'指令转速',color:'#51cf66',axis:'R',unit:'%',on:true},
  {k:'rpm',label:'实测 RPM',color:'#b197fc',axis:'R2',unit:'RPM',on:true}
];
const RANGE_LABEL={'1h':'1 小时','6h':'6 小时','24h':'24 小时','7d':'7 天'};
let hist=null, range='1h', view={}, resizeTimer=0;

function pad2(n){return String(n).padStart(2,'0')}
function fmtTime(ts,long){
  const d=new Date(ts*1000);
  const hm=pad2(d.getHours())+':'+pad2(d.getMinutes());
  return long?((d.getMonth()+1)+'/'+d.getDate()+' '+hm):hm;
}
function fmtBucket(b){
  if(b<60)return b+' 秒';
  if(b<3600)return Math.round(b/60)+' 分钟';
  return Math.round(b/3600)+' 小时';
}
function niceMax(v){
  if(!(v>0))return 1000;
  const pows=[200,500,1000,1500,2000,2500,3000,4000,5000,6000,8000,10000,12000,15000];
  for(let i=0;i<pows.length;i++){if(v<=pows[i])return pows[i];}
  return Math.ceil(v/1000)*1000;
}
function renderLegend(){
  document.getElementById('legend').innerHTML=SERIES.map(s=>
    `<label title="单位：${s.unit}"><input type="checkbox" ${s.on?'checked':''} onchange="toggleSeries('${s.k}',this.checked)">`+
    `<i style="background:${s.color}"></i>${s.label}（${s.unit}）</label>`).join('');
}
function toggleSeries(k,on){
  SERIES.forEach(s=>{if(s.k===k)s.on=on});
  drawChart();
}
function setRange(r){
  range=r;
  document.querySelectorAll('.rbtn').forEach(b=>{
    b.className='gray mini rbtn'+(b.getAttribute('data-r')===r?' on':'');
  });
  loadHistory();
}
async function loadHistory(){
  hist=await api('/api/history?range='+range+'&points=360');
  const hm=document.getElementById('histmeta');
  if(hm)hm.textContent=(RANGE_LABEL[range]||range)+' · 每 '+fmtBucket(hist.bucket||5)+' · '+
    (hist.t?hist.t.length:0)+' 点 · 采样 '+(hist.count||0)+' 条 · '+((hist.bytes||0)/1024).toFixed(1)+' KB';
  drawChart();renderStats();
}
function renderStats(){
  const st=(hist&&hist.stats)||{};
  const items=[['cpu','CPU 温度','°C'],['hdd','机械盘最高温','°C'],['pct','指令转速','%'],['rpm','实测 RPM','']];
  document.getElementById('histstats').innerHTML=items.map(it=>{
    const k=it[0],label=it[1],unit=it[2],d=st[k];
    if(!d)return `<span class="stat">${label}<b>--</b></span>`;
    const u=unit||'';
    return `<span class="stat">${label} · 低 ${d.min}${u} / 均 ${d.avg}${u}<b>高 ${d.max} ${u}</b></span>`;
  }).join('');
}
function drawChart(){
  const svg=document.getElementById('chart'), box=document.getElementById('chartbox');
  const W=Math.max(320,Math.round(box.clientWidth||800));
  const H=Math.max(140,Math.round(box.clientHeight||292));
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  if(!hist||!hist.t||hist.t.length<2){
    svg.innerHTML=`<text x="${W/2}" y="${H/2}" fill="#6b7488" font-size="12" text-anchor="middle">暂无历史数据，等待采样…（部署后约 1 分钟开始出图）</text>`;
    view={};return;
  }
  const on=SERIES.filter(s=>s.on);
  const n=hist.t.length, T=24, B=20, L=42;
  const hasR=on.some(s=>s.axis==='R');
  const hasR2=on.some(s=>s.axis==='R2');
  const R=(hasR?34:10)+(hasR2?44:0);
  const plotW=Math.max(60,W-L-R), plotH=Math.max(60,H-T-B);
  const X=i=>L+(n>1?plotW*i/(n-1):0);

  const tvals=[], rvals=[];
  on.forEach(s=>{
    const a=hist[s.k]||[];
    for(let i=0;i<a.length;i++){
      if(a[i]==null)continue;
      if(s.axis==='L')tvals.push(a[i]);
      if(s.axis==='R2')rvals.push(a[i]);
    }
  });
  let tLo=0, tHi=100;
  if(tvals.length){
    tLo=Math.min.apply(null,tvals);
    tHi=Math.max.apply(null,tvals);
    const pd=Math.max(1,(tHi-tLo)*0.15);
    tLo=Math.floor(tLo-pd);
    tHi=Math.ceil(tHi+pd);
    if(tHi-tLo<4){const m=(tLo+tHi)/2;tLo=Math.floor(m-2);tHi=Math.ceil(m+2);}
  }
  const rpmHi=niceMax(rvals.length?Math.max.apply(null,rvals)*1.1:0);
  const YL=v=>T+plotH-(v-tLo)/(tHi-tLo)*plotH;
  const YR=v=>T+plotH-v/100*plotH;
  const Y2=v=>T+plotH-v/rpmHi*plotH;

  const p=[];
  for(let g=0;g<=4;g++){
    const y=T+plotH*g/4;
    p.push(`<line x1="${L}" y1="${y.toFixed(1)}" x2="${L+plotW}" y2="${y.toFixed(1)}" stroke="#242a38" stroke-width="1"/>`);
    p.push(`<text x="${L-6}" y="${(y+3.5).toFixed(1)}" fill="#8b93a7" font-size="10" text-anchor="end">${(tLo+(tHi-tLo)*(1-g/4)).toFixed(0)}</text>`);
  }
  p.push(`<text x="${L-6}" y="${T-11}" fill="#4dabf7" font-size="10" text-anchor="end">°C</text>`);
  if(hasR){
    const rx=L+plotW+6;
    for(let g=0;g<=4;g++){
      const y=T+plotH*g/4;
      p.push(`<text x="${rx}" y="${(y+3.5).toFixed(1)}" fill="#51cf66" font-size="10">${(100-100*g/4).toFixed(0)}</text>`);
    }
    p.push(`<text x="${rx}" y="${T-11}" fill="#51cf66" font-size="10">%</text>`);
  }
  if(hasR2){
    const rx2=L+plotW+(hasR?34:10)+6;
    for(let g=0;g<=4;g++){
      const y=T+plotH*g/4;
      p.push(`<text x="${rx2}" y="${(y+3.5).toFixed(1)}" fill="#b197fc" font-size="10">${Math.round(rpmHi*(1-g/4))}</text>`);
    }
    p.push(`<text x="${rx2}" y="${T-11}" fill="#b197fc" font-size="10">RPM</text>`);
  }
  const ticks=6;
  for(let k=0;k<ticks;k++){
    const idx=Math.round((n-1)*k/(ticks-1));
    const x=X(idx);
    const anchor=k===0?'start':(k===ticks-1?'end':'middle');
    p.push(`<text x="${x.toFixed(1)}" y="${H-6}" fill="#6b7488" font-size="10" text-anchor="${anchor}">${fmtTime(hist.t[idx],range==='7d')}</text>`);
  }
  p.push(`<rect x="${L}" y="${T}" width="${plotW}" height="${plotH}" fill="none" stroke="#2a3040" stroke-width="1"/>`);

  const ys={};
  on.forEach(s=>{
    const yf=s.axis==='L'?YL:(s.axis==='R'?YR:Y2);
    ys[s.k]=yf;
    const a=hist[s.k]||[];
    let d='', pen=false;
    for(let i=0;i<a.length;i++){
      if(a[i]==null){pen=false;continue;}
      d+=(pen?'L':'M')+X(i).toFixed(1)+' '+yf(a[i]).toFixed(1)+' ';
      pen=true;
    }
    if(d)p.push(`<path d="${d}" fill="none" stroke="${s.color}" stroke-width="1.7" stroke-linejoin="round" stroke-linecap="round"/>`);
  });
  p.push(`<line id="cross" x1="0" y1="${T}" x2="0" y2="${T+plotH}" stroke="#8b93a7" stroke-width="1" stroke-dasharray="3 3" style="display:none"/>`);
  svg.innerHTML=p.join('');
  view={L:L,plotW:plotW,n:n,X:X,ys:ys,H:H};
}
function onMove(e){
  if(!hist||!hist.t||hist.t.length<2||!view.X)return;
  const box=document.getElementById('chartbox'), r=box.getBoundingClientRect();
  const vx=e.clientX-r.left;
  let i=Math.round((vx-view.L)/(view.plotW||1)*(view.n-1));
  i=Math.max(0,Math.min(view.n-1,i));
  const cross=document.getElementById('cross');
  if(cross){cross.setAttribute('x1',view.X(i));cross.setAttribute('x2',view.X(i));cross.style.display='';}
  let html=`<b>${fmtTime(hist.t[i],range==='7d')}</b>`;
  SERIES.forEach(s=>{
    if(!s.on)return;
    const v=(hist[s.k]||[])[i];
    html+=`<div><span style="color:${s.color}">■</span> ${s.label}：${v==null?'--':v+' '+s.unit}</div>`;
  });
  const tip=document.getElementById('tip');
  tip.innerHTML=html;
  tip.style.display='block';
  const px=view.X(i), tw=tip.offsetWidth||150;
  tip.style.left=Math.max(4,Math.min(px+12,r.width-tw-6))+'px';
  tip.style.top='6px';
}
function onLeave(){
  const c=document.getElementById('cross');
  if(c)c.style.display='none';
  const t=document.getElementById('tip');
  if(t)t.style.display='none';
}
document.getElementById('chart').addEventListener('mousemove',onMove);
document.getElementById('chart').addEventListener('mouseleave',onLeave);
window.addEventListener('resize',()=>{
  clearTimeout(resizeTimer);
  resizeTimer=setTimeout(()=>drawChart(),160);
});
renderLegend();
setRange('1h');
setInterval(loadHistory,30000);
refresh();setInterval(refresh,5000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, HTML, "text/html")
        elif self.path.split("?")[0] == "/api/log":
            n = 500
            parts = self.path.split("?", 1)
            if len(parts) > 1:
                for kv in parts[1].split("&"):
                    if kv.startswith("lines="):
                        try:
                            n = int(kv[6:])
                        except Exception:
                            pass
            n = max(0, min(20000, n))
            body = "\n".join(read_log_tail(n))
            if body:
                body += "\n"
            self._send(200, body, "text/plain")
        elif self.path.split("?")[0] == "/api/history":
            q = {}
            parts = self.path.split("?", 1)
            if len(parts) > 1:
                for kv in parts[1].split("&"):
                    k, _, v = kv.partition("=")
                    q[k] = v
            try:
                pts = int(q.get("points") or 360)
            except Exception:
                pts = 360
            self._send(200, json.dumps(read_history(parse_range(q.get("range")), pts)))
        elif self.path == "/api/status":
            with lock:
                s = dict(state)
                s["hold_remaining"] = max(0, state["hold_until"] - time.time())
                s["hdd_hold_remaining"] = max(0, state["hdd_hold_until"] - time.time())
                s["mode"] = cfg.get("mode", "auto")
                s["manual_pct"] = cfg.get("manual_pct", 80)
                s["steps"] = cfg.get("steps", [])
                s["hold_trigger"] = cfg.get("hold_trigger")
                s["hold_seconds"] = cfg.get("hold_seconds")
                s["min_pct"] = cfg.get("min_pct")
                s["temp_source"] = cfg.get("temp_source")
                s["log"] = read_log_tail(200)
                s["log_retention_days"] = cfg.get("log_retention_days", 7)
                try:
                    s["log_bytes"] = os.path.getsize(LOG_FILE)
                except Exception:
                    s["log_bytes"] = 0
                s["hist_retention_days"] = cfg.get("hist_retention_days", 7)
                s["retention_days"] = cfg.get("log_retention_days", 7)
                try:
                    s["hist_bytes"] = os.path.getsize(HIST_FILE)
                except Exception:
                    s["hist_bytes"] = 0
                for k in ("hdd_enabled", "hdd_trigger_temp", "hdd_pct",
                          "hdd_hold_seconds", "hdd_watch", "hdd_sensors"):
                    s[k] = cfg.get(k, DEFAULT_CFG[k])
            self._send(200, json.dumps(s))
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        b = self._json_body()
        if self.path == "/api/set-pwm-pending":
            cfg["pending_pct"] = float(b.get("pct", cfg.get("manual_pct", 80)))
            save_cfg()
            self._send(200, json.dumps({"ok": True, "pending": cfg["pending_pct"]}))
        elif self.path == "/api/apply":
            v = cfg.get("pending_pct")
            if v is None:
                self._send(400, json.dumps({"ok": False, "msg": "没有暂存的转速值"}))
                return
            cfg["manual_pct"] = v
            cfg["mode"] = "manual"
            cfg["pending_pct"] = None
            save_cfg()
            apply_pwm(v, "手动应用 %d%%" % v)
            self._send(200, json.dumps({"ok": True, "applied": v}))
        elif self.path == "/api/mode":
            cfg["mode"] = b.get("mode", "auto")
            save_cfg()
            self._send(200, json.dumps({"ok": True, "mode": cfg["mode"]}))
        elif self.path == "/api/curve":
            for k in ("steps", "hold_trigger", "hold_seconds", "min_pct", "temp_source"):
                if k in b:
                    cfg[k] = b[k]
            save_cfg()
            self._send(200, json.dumps({"ok": True}))
        elif self.path == "/api/logcfg":
            # 界面上只留一个「保留天数」，一次把日志与历史采样设成同一个值
            for k in ("log_retention_days", "hist_retention_days"):
                if k in b:
                    try:
                        v = float(b[k])
                    except Exception:
                        v = 7.0
                    cfg[k] = max(1.0, min(365.0, v))
            save_cfg()
            dropped = prune_log(force=True)
            hdropped = prune_history(force=True)
            self._send(200, json.dumps({"ok": True,
                                        "retention_days": cfg["log_retention_days"],
                                        "dropped": dropped,
                                        "hist_dropped": hdropped}))
        elif self.path == "/api/histcfg":
            if "hist_retention_days" in b:
                try:
                    v = float(b["hist_retention_days"])
                except Exception:
                    v = 7.0
                cfg["hist_retention_days"] = max(1.0, min(365.0, v))
            save_cfg()
            dropped = prune_history(force=True)
            self._send(200, json.dumps({"ok": True,
                                        "retention_days": cfg["hist_retention_days"],
                                        "dropped": dropped}))
        elif self.path == "/api/hdd":
            for k in ("hdd_enabled", "hdd_trigger_temp", "hdd_pct",
                      "hdd_hold_seconds", "hdd_watch"):
                if k in b:
                    cfg[k] = b[k]
            if "hdd_sensors" in b:
                cfg["hdd_sensors"] = [str(x) for x in (b["hdd_sensors"] or [])]
            save_cfg()
            # 规则变更后清掉保持状态，避免旧状态残留
            with lock:
                state["hdd_hold_until"] = 0
                state["hdd_active"] = False
            self._send(200, json.dumps({"ok": True}))
        else:
            self._send(404, '{"error":"not found"}')


if __name__ == "__main__":
    load_cfg()
    log("=== 容器启动 | TZ=%s | %s | UTC%+.1f | 日志保留 %s 天 ===" % (
        os.environ.get("TZ") or "未设置", time.tzname[0],
        -time.timezone / 3600.0, cfg.get("log_retention_days", 7)))
    _d = prune_log(force=True)
    if _d:
        log("启动清理：删除 %d 条超期日志" % _d)
    _h = prune_history(force=True)
    if _h:
        log("启动清理：删除 %d 条超期历史采样" % _h)
    t = threading.Thread(target=control_loop, daemon=True)
    t.start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
