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

# 数据目录 / 监听地址都可以由外部指定：
#   · Docker 部署：保持默认 /data + 8080（docker run 挂载 /data）
#   · 飞牛 FPK 应用：由 service-setup 传 --data-dir "$TRIM_PKGVAR" --port "$SERVICE_PORT"
# 环境变量 FAN_DATA_DIR / FAN_PORT / FAN_LISTEN 与命令行参数等效，命令行优先。
DATA_DIR = os.environ.get("FAN_DATA_DIR") or "/data"
CFG_FILE = os.path.join(DATA_DIR, "curve-config.json")
LOG_FILE = os.path.join(DATA_DIR, "fan.log")   # 明文日志，按天保留
HIST_FILE = os.path.join(DATA_DIR, "history.tsv")   # 历史采样，界面曲线用
PORT = int(os.environ.get("FAN_PORT") or 8080)
LISTEN = os.environ.get("FAN_LISTEN") or "0.0.0.0"

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
    "min_pct": 42,          # 安全下限（用户可调），不可低于 HARD_MIN_PCT
    "interval": 2,          # 采样间隔（秒）—— 2 秒是为了让下面的 4 秒确认能真实判定
    "ramp_delay": 4,        # 升档确认：温度需「持续」这么多秒达到高档位才提速
    "drop_delay": 0,        # 降档确认：温度需「持续」这么多秒降到低档位才降速（0=立即）

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
    "hist_interval": 5,         # 历史采样落盘间隔（秒），与控制周期解耦
}

# 转速硬下限（%）。与 cfg["min_pct"] 不同：那是「用户可调的安全下限」，
# 这是**不可突破的物理保护**。硬盘（尤其机械盘）长时间低速运转散热不足，
# 磁头/盘片温升会明显缩短寿命，故任何路径（自动档位、手动模式、高温保护、
# 甚至直接调 API）算出的转速都不得低于它。
# 想再调低？改这里的常量并重启服务，别指望改配置绕过去。
HARD_MIN_PCT = 42

cfg = {}
state = {
    "temp": None, "pwm": None, "pct": None, "rpm": [], "source": "",
    "hold_until": 0, "hold_pct": None, "last_update": 0, "log": [],
    "hdd_disks": [], "hdd_max": None, "hdd_hold_until": 0, "hdd_active": False,
    # 温度档位「持续确认」状态：curve_pct=已生效档位；ramp_pct=等待确认的目标档位
    "curve_pct": None, "ramp_pct": None, "ramp_since": 0.0, "ramp_remaining": 0.0,
    # 当前转速档的生效时刻：换档时用「上一档持续时长」写日志
    "pct_since": 0.0,
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


def _clear_ramp():
    state["ramp_pct"] = None
    state["ramp_since"] = 0.0
    state["ramp_remaining"] = 0.0


def reset_curve_state():
    """手动模式 / 保存规则时调用：丢弃未完成的档位确认计时。"""
    state["curve_pct"] = None
    _clear_ramp()


def confirm_curve_pct(t, now):
    """按温度查表得到目标档位，但升/降档都要「持续 N 秒」才确认生效。

    目的：CPU 温度瞬时尖峰（编译、突发负载）往往只持续一两秒，
    直接按瞬时值提速会让风扇无谓狂转。现在要求温度真的站稳在
    高档位区间 ramp_delay 秒后才提速。

    注意：机械硬盘高温保护不走这里 —— 那条要求到达温度立即生效。

    **日志纪律**：这里一行日志都不写。
    温度短暂越过档位、确认计时还没走完就掉回去的（绝大多数尖峰属于这类），
    风扇根本没动，记进日志只是噪音。真正换档生效的那一刻由控制循环写日志
    （那时才真的写进了 PWM），本函数只负责把「等了多久、从哪档到哪档」交出去。

    返回 (生效档位, 是否仍在等待确认, 本次确认信息或 None)
      确认信息 = {"from": 原档位, "to": 新档位, "waited": 实际持续秒数}
      只有「确实等够了时间才换档」（delay > 0）时才带信息；
      首轮生效、降档确认设为 0（立即降）都返回 None。
    """
    want = float(target_pct(t))
    cur = state.get("curve_pct")
    up_d = max(0.0, float(cfg.get("ramp_delay", 4)))
    down_d = max(0.0, float(cfg.get("drop_delay", 0)))

    # 首轮（含刚切回自动、刚保存规则）：直接生效，别让风扇干等着
    if cur is None:
        state["curve_pct"] = want
        _clear_ramp()
        return want, False, None

    # 目标就是当前档位 → 取消任何待确认（静默，不记日志）
    if abs(want - cur) < 0.5:
        if state.get("ramp_pct") is not None:
            _clear_ramp()
        return cur, False, None

    delay = up_d if want > cur else down_d
    pend = state.get("ramp_pct")
    if pend is None or abs(float(pend) - want) >= 0.5:
        # 目标档位变了（或首个采样点）→ 重新开始计时。静默计时，不写日志。
        state["ramp_pct"] = want
        state["ramp_since"] = now

    waited = now - float(state.get("ramp_since") or now)
    if waited >= delay - 1e-6:
        state["curve_pct"] = want
        _clear_ramp()
        info = {"from": cur, "to": want, "waited": waited} if delay > 0 else None
        return want, False, info

    state["ramp_remaining"] = max(0.0, delay - waited)
    return cur, True, None


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
    # 最后一道防线：无论调用方是谁（自动曲线 / 手动应用 / 高温保护 / 未来新增的接口），
    # 写进硬件的转速都不得低于 HARD_MIN_PCT。正常路径上调用方已经钳制过，
    # 这里只在「有漏网」时才生效并留一条日志，方便定位是谁绕过了钳制。
    if pct < HARD_MIN_PCT:
        reason = ("%s（目标 %d%% 低于硬性下限 %d%%，已抬到下限）"
                  % (reason, int(pct), HARD_MIN_PCT)).lstrip("（")
        pct = float(HARD_MIN_PCT)
    p = get_pwm_path()
    if not p:
        log("未找到 PWM 接口")
        return False
    ok = wr(p, pct_to_pwm(pct))
    if ok:
        with lock:
            prev = state["pct"]
            now_ts = time.time()
            since = float(state.get("pct_since") or 0.0)
            state["pct"] = pct
            state["pwm"] = pct_to_pwm(pct)
            state["last_update"] = now_ts
            state["pct_since"] = now_ts
        # 换档日志统一带上「上一档维持了多久」，方便回看每次提速/降速的实际持续时间。
        # 首次写 PWM（prev 为 None）没有上一档可说；同档重写（差值 < 0.5%）不算换档。
        if reason or (prev is not None and abs(pct - prev) >= 0.5):
            dur = max(0, int(now_ts - since)) if since > 0 else None
            extra = ""
            if prev is not None and abs(pct - prev) >= 0.5 and dur is not None:
                extra = "（上一档 %d%% 维持 %s）" % (int(prev), fmt_dur(dur))
            if reason:
                log(reason + extra)
            elif prev is not None:
                log("转速 %d%% → %d%%%s" % (int(prev), int(pct), extra))
            else:
                log("转速 %d%%" % int(pct))
    return ok


def fmt_dur(sec):
    """把秒数格式化成人类可读的时长：45s / 12m30s / 1h05m。"""
    sec = int(sec)
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm%02ds" % (sec // 60, sec % 60)
    return "%dh%02dm" % (sec // 3600, (sec % 3600) // 60)


def now_str():
    return time.strftime("%H:%M:%S")


def control_loop():
    last_hist = 0.0
    while True:
        now0 = time.time()
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
                # 生效下限 = max(用户设的安全下限, 硬性下限)
                # 硬件保护优先：用户就算把 min_pct 填成 10，最终也不会低于 HARD_MIN_PCT
                user_min = float(cfg.get("min_pct", HARD_MIN_PCT))
                min_pct = max(HARD_MIN_PCT, user_min)
                now = time.time()

                # 下面钳制那段会读 pending，手动模式没有档位确认，显式给个初值
                pending = False
                if cfg.get("mode", "auto") == "manual":
                    pct = float(cfg.get("manual_pct", 80))
                    reason = "手动模式 %d%%" % pct
                    # 手动模式下不累积档位确认计时，切回自动时重新判定
                    if state.get("curve_pct") is not None:
                        reset_curve_state()
                else:
                    # 温度档位：需「持续」达到才生效（防瞬时尖峰误提速）
                    curve, pending, ramp_info = confirm_curve_pct(t, now)

                    # 高温触发滞后保持（hold_seconds<=0 视为未启用，避免日志里出现
                    # 「触发保持 xx% / 0s」这种等于没生效的误导性文案）
                    holding = hold_seconds > 0 and now < state["hold_until"]
                    if hold_seconds > 0 and t > hold_trigger:
                        state["hold_until"] = now + hold_seconds
                        state["hold_pct"] = curve
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
                        pct = curve
                        if reason == "":
                            if pending:
                                # 还在等确认：转速没动，什么都不记
                                # （瞬时尖峰绝大多数落在这里，记了就是噪音）
                                reason = ""
                            elif ramp_info:
                                # 真的等够了时间才换的档 —— 这才是值得记的一条
                                reason = "%.1f°C 持续 %.0fs，档位 %d%% → %d%%" % (
                                    t, ramp_info["waited"],
                                    int(ramp_info["from"]), int(ramp_info["to"]))
                            else:
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

                # ---- 安全下限钳制 ----
                # 顺序很重要：必须先把下限钳制做掉，再拿最终的 pct 去拼日志。
                # 旧版本先拼日志、后钳制，于是日志写 `49.0°C → 40%`，
                # 而真正写进硬件的是 50% —— 用户看日志以为设置生效了，其实没有。
                # 现在被钳制时直接改写文案，点名「你填的 xx% 没生效」。
                clamped = max(min_pct, min(100, pct))
                if abs(clamped - pct) >= 0.5 and not pending:
                    if min_pct > user_min + 0.5:
                        # 用户把下限调到了硬性下限以下 —— 说清楚是保护在生效，不是没保存
                        reason = ("%.1f°C：档位 %d%% 低于硬性下限 %d%%（硬盘低速散热不足），"
                                  "实际按 %d%% 运行" % (t, int(pct), int(min_pct), int(clamped)))
                    else:
                        reason = ("%.1f°C：档位 %d%% 低于安全下限 %d%%，实际按 %d%% 运行"
                                  % (t, int(pct), int(min_pct), int(clamped)))
                pct = clamped

                # 「该不该写」以 state["pct"] 为唯一事实来源，不用局部变量记。
                # 手动「应用」按钮、机械盘高温保护都会直接改 state["pct"]，
                # 局部变量却不知道，于是出现「算出来和记的一样 → 不写」，
                # 风扇就一直停在别人写进去的值上，且不会自愈。
                # 另外这样还有个附带好处：写失败（wr 返回 False）时 state 不更新，
                # 下一轮会自动重试（旧写法无论成败都更新，写失败就永远不重试了）。
                with lock:
                    written = state.get("pct")
                if written is None or abs(pct - written) >= 0.5:
                    apply_pwm(pct, reason)

            # ---- 记录一条历史采样（t 为 None 时也会记录盘温与转速） ----
            # 控制周期改成 2 秒后，历史采样仍按 hist_interval（默认 5 秒）节流，
            # 否则 7 天的采样量会翻 2.5 倍把 history.tsv 撑大。
            if now0 - last_hist >= max(1.0, float(cfg.get("hist_interval", 5))):
                last_hist = now0
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
    # 归一化：历史配置里可能留着低于硬性下限的值（早期版本允许 40），
    # 读进来就抬到 HARD_MIN_PCT，避免界面上仍显示一个实际不生效的数字。
    try:
        cfg["min_pct"] = max(HARD_MIN_PCT, min(100.0, float(cfg.get("min_pct", HARD_MIN_PCT))))
    except (TypeError, ValueError):
        cfg["min_pct"] = HARD_MIN_PCT
    try:
        cfg["manual_pct"] = max(HARD_MIN_PCT, min(100.0, float(cfg.get("manual_pct", 80))))
    except (TypeError, ValueError):
        cfg["manual_pct"] = 80


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
html{height:100%}
/* 强制适配 1878×869 窗口：页面严格锁定视口高度，底部不留白、也不出整页滚动条。
   内容若超出，只在各自面板 / 卡片内部滚动。 */
body{margin:0;height:100%;overflow:hidden;display:flex;flex-direction:column;background:#12141a;color:#e6e8ee;font:13px/1.5 -apple-system,"PingFang SC",system-ui,sans-serif;padding:12px 14px}
/* 第 3 行给 1fr：剩余高度全给 .cols，两栏面板随之等高拉伸，日志框 / 硬盘表吸收余量 */
.wrap{display:grid;grid-template-rows:auto auto 1fr;gap:10px;width:100%;max-width:2200px;margin:0 auto;flex:1 1 auto;min-height:0;overflow:hidden}
.top{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
h1{font-size:17px;margin:0;font-weight:600}
.top .sub{color:#6b7488;font-size:12px}
.tags{display:flex;gap:6px;align-items:center;margin-left:auto;flex-wrap:wrap}
.metrics{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px}
.metric{background:#1b1f28;border:1px solid #2a3040;border-radius:8px;padding:7px 10px;min-width:0;text-align:center}
.metric .k{font-size:11px;color:#8b93a7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.metric .v{font-size:19px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.metric .v small{font-size:11px;color:#8b93a7;font-weight:400}
.hot{color:#ff6b6b}.warm{color:#ffa94d}.cool{color:#51cf66}
/* 两栏：左＝历史/日志，右＝档位规则/手动模式/硬盘保护 */
.cols{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(430px,1fr);gap:10px;align-items:stretch;min-height:0}
.panel{background:#1b1f28;border:1px solid #2a3040;border-radius:10px;min-width:0;display:flex;flex-direction:column;min-height:0;overflow:hidden}
.sub{padding:11px 13px;min-width:0}
.sub+.sub{border-top:1px solid #262c3a}
.sub:last-child{flex:1 1 auto;display:flex;flex-direction:column;min-height:0;overflow:auto}
/* 左栏：历史曲线占 3/4、运行日志占 1/4；曲线纵向撑满、日志内部滚动 */
.cols .panel:first-child{display:flex;flex-direction:column}
.cols .panel:first-child > .sub:nth-child(1){flex:3 1 0;min-height:0;display:flex;flex-direction:column}
.cols .panel:first-child > .sub:nth-child(1) .chartbox{flex:1 1 auto;min-height:160px}
.cols .panel:first-child > .sub:nth-child(2){flex:1 1 0;min-height:0;overflow:auto}
/* 右栏：温度档位规则撑满剩余空间；机械硬盘高温保护改为自然高度——
   它让出的空间全部给档位规则，同时其整体随档位规则增高而下移；末块仍贴面板底、底部不留空 */
.cols .panel:last-child > .sub:nth-child(1){flex:1 1 auto;min-height:0;display:flex;flex-direction:column}
/* 高温触发 / 升档确认 / 温度源 这两行 + 说明文字整体沉到档位规则底部，
   紧贴上方「手动模式」；档位表留在顶部，中间多出的空间自动分配 */
.pushdown{margin-top:auto}
.cols .panel:last-child > .sub:nth-child(2){flex:0 0 auto}
.cols .panel:last-child > .sub:nth-child(3){flex:0 0 auto;min-height:0}
.cols .panel:last-child > .sub:nth-child(3) > table{flex:0 0 auto}
.mhead{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.mhead h2{font-size:13px;margin:0;font-weight:600;color:#cfd6e6}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:2px 6px;border-bottom:1px solid #262c3a}
th{color:#8b93a7;font-weight:500;font-size:11px}
td input{padding:3px 6px}
/* 限高用 min(px,vh)：1080p 用满 220px，1366×768 这类矮屏自动收，避免整页溢出 */
.steps-box{max-height:min(220px,26vh);overflow:auto}
/* 手动模式：标题 + 滑块 + 数值 + 两个按钮压成一行，比单独占一整行省约 30px */
.man .line{margin-bottom:0;flex-wrap:nowrap}
.man h2{font-size:13px;margin:0;font-weight:600;color:#cfd6e6;white-space:nowrap}
.man input[type=range]{flex:1 1 auto;min-width:60px}
.man .sv{min-width:44px;text-align:center}
/* 按钮不许被压扁成两行文字 */
.man button{white-space:nowrap;flex:0 0 auto}
input,select{background:#12141a;color:#e6e8ee;border:1px solid #2f3648;border-radius:6px;padding:5px 7px;width:100%;font-size:12px}
button{background:#3b7cff;color:#fff;border:0;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:12px}
button.gray{background:#2f3648}
button.mini{padding:4px 9px;font-size:11px}
button:active{opacity:.82}
.btn-dirty{background:#8a5a12;color:#ffd8a8;box-shadow:inset 0 0 0 1px #b8862b}
/* 档位转速低于「安全下限」：实际会被抬到下限，标黄表示"这一格填了也不会生效" */
.step-input.low{background:#3a2a12;border-color:#8a5a12;color:#ffd8a8}
.rbtn.on{background:#3b7cff;box-shadow:inset 0 0 0 1px #7aa6ff}
.line{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-bottom:7px}
.line:last-child{margin-bottom:0}
.lb{font-size:11px;color:#8b93a7;white-space:nowrap}
.spacer{margin-left:auto}
.tag{display:inline-block;padding:2px 7px;border-radius:5px;font-size:11px;background:#2f3648;color:#b6c0d6;white-space:nowrap}
.tag.on{background:#1f6f3f;color:#8ce0a8}
.tag.hold{background:#7a4a12;color:#ffc078}
.tag.off{background:#3a2020;color:#e08a8a}
.chartbox{position:relative;height:min(292px,28vh);min-height:160px;background:#10131a;border-radius:8px;overflow:hidden}
.chartbox svg{width:100%;height:100%;display:block}
.tip{position:absolute;display:none;pointer-events:none;background:#0e1117;border:1px solid #2f3648;border-radius:6px;padding:6px 9px;font-size:11px;line-height:1.6;color:#cfd6e6;white-space:nowrap;z-index:5;box-shadow:0 6px 18px rgba(0,0,0,.45)}
.legend{display:flex;gap:12px;flex-wrap:wrap;align-items:center;font-size:12px;color:#b6c0d6;margin-top:7px}
.legend label{display:flex;gap:5px;align-items:center;cursor:pointer;user-select:none}
.legend i{width:12px;height:3px;border-radius:2px;display:inline-block}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;margin-top:7px}
.stat{background:#151922;border:1px solid #262c3a;border-radius:7px;padding:4px 8px;font-size:11px;color:#8b93a7;min-width:0}
.stat b{display:block;font-size:13px;color:#e6e8ee;font-weight:600;margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* 右栏末块（硬盘保护）撑满整栏高度，表格填充满，避免底部留空位 */
.sub:last-child>table{flex:1 1 auto;min-height:0;overflow:auto}
/* 日志框用 flex 撑满剩余高度（basis 写死，避免被 200 行内容撑爆） */
.log{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:#8b93a7;flex:1 1 64px;min-height:0;overflow:auto;white-space:pre-wrap;word-break:break-all}
.hint{font-size:11px;color:#6b7488;line-height:1.5;margin-top:6px}
.mono{font-family:ui-monospace,Menlo,monospace}
.warn{color:#ffa94d}
input[type=range]{width:100%;padding:0}
input[type=checkbox]{width:auto}
/* 矮屏（1366×768 / 1878×869 窗口）：整体收紧一圈，保证页面一屏装得下。
   1878×869 浏览器窗口真实视口通常只有 700–770px，这里把右栏三块 .sub 的内边距、
   字号、行距、图表高度全部下压，使自然内容高度 < 720px；页面本身 overflow:hidden
   强制锁定视口，即使极端矮屏也不会出现整页滚动条。 */
@media (max-height:820px){
  body{padding:8px 11px}
  .wrap{gap:6px}
  .metric{padding:4px 8px}
  .metric .v{font-size:16px}
  .sub{padding:5px 9px}
  .mhead{margin-bottom:2px}
  .mhead h2{font-size:12px}
  .hint{margin-top:1px;font-size:10px;line-height:1.2}
  .line{margin-bottom:4px}
  .chartbox{height:min(210px,21vh);min-height:110px}
  .legend{margin-top:3px;gap:8px}
  .stats{margin-top:3px;gap:5px}
  .stat{padding:2px 5px}
  .steps-box{max-height:min(132px,20vh)}
  th,td{padding:1px 4px;font-size:10.5px}
}
@media (max-width:1180px){
  .metrics{grid-template-columns:repeat(3,minmax(0,1fr))}
  .cols{grid-template-columns:1fr}
  .stats{grid-template-columns:repeat(2,minmax(0,1fr))}
  /* 单列时宽度不再紧张，手动模式那一行允许换行，别把滑块挤没了 */
  .man .line{flex-wrap:wrap}
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

  <!-- ============ 左：历史曲线 + 运行日志 ============ -->
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
        <svg id="chart" preserveAspectRatio="none"></svg>
        <div id="tip" class="tip"></div>
      </div>
      <div class="legend" id="legend"></div>
      <div class="stats" id="histstats"></div>
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
      <div class="log" id="log" title="最新的日志显示在最上面"></div>
    </div>
  </section>

  <!-- ============ 右：温度档位规则 + 手动模式 + 机械硬盘高温保护 ============ -->
  <section class="panel">
    <div class="sub">
      <div class="mhead">
        <h2>温度档位规则</h2>
        <span id="curveWarn" class="tag hold" style="display:none"
              title="这些档位低于「安全下限」，实际不会生效——最终写入硬件的转速会被抬到下限值"></span>
        <button id="saveCurveBtn" class="mini spacer" onclick="saveCurve()">保存规则</button>
        <button class="gray mini" onclick="addStep()">+ 档位</button>
      </div>
      <div class="steps-box">
        <table><thead><tr><th>温度不高于 (°C)</th><th>风扇转速 (%)</th><th style="width:44px"></th></tr></thead>
        <tbody id="steps"></tbody></table>
      </div>
      <div class="line pushdown">
        <span class="lb">高温触发</span><input id="hold_trigger" type="number" step="0.1" style="width:58px">
        <span class="lb">保持(秒)</span><input id="hold_seconds" type="number" step="1" style="width:58px">
        <span class="lb" title="最终写入硬件的转速不会低于此值。档位表里填了比它更低的转速时，会被抬到这里——这不是「忽略」，日志和上面的警告标签都会说明。程序内有 42% 的硬性下限（低于则硬盘散热不足），这里填得比 42 小也只会按 42 执行。">安全下限</span><input id="min_pct" type="number" step="1" min="42" max="100" style="width:52px">
      </div>
      <div class="line">
        <span class="lb" title="温度需持续这么多秒达到高档位才提速，用来过滤瞬时尖峰">升档确认</span><input id="ramp_delay" type="number" step="1" min="0" style="width:48px">
        <span class="lb" title="温度需持续这么多秒降到低档位才降速，填 0 表示立即降">降档确认</span><input id="drop_delay" type="number" step="1" min="0" style="width:48px">
        <span class="lb">秒</span>
        <span class="lb" style="margin-left:auto">温度源</span>
        <select id="temp_source" style="width:132px">
          <option value="coretemp">CPU (coretemp)</option>
          <option value="qnap8528">EC 传感器 (qnap8528)</option>
          <option value="max">两者取最高</option>
        </select>
      </div>
      <div class="hint">超过「高温触发」后即使温度回落，也保持触发时的档位运行「保持」秒，填 0 关闭。<br>
        升档确认 = 温度要「持续」这么多秒落在高档位区间才提速（过滤 1~2 秒的瞬时尖峰），填 0 = 立即；机械盘高温保护不走这两个确认，到温即生效。</div>
    </div>

    <!-- 手动模式：拖滑块 → 点「应用」一步写入硬件并切手动，不再有「暂存」中间态 -->
    <div class="sub man">
      <div class="line">
        <h2 title="点「应用」会立即写入硬件并切到手动模式；「切回自动」交还给温度曲线。受 42% 硬性下限保护，滑块最低只到 42%">手动模式</h2>
        <input type="range" id="slider" min="42" max="100" step="1" value="80" oninput="document.getElementById('sv').textContent=this.value+'%'">
        <span id="sv" class="tag sv">80%</span>
        <button class="mini" onclick="applyManual()">应用</button>
        <button class="gray mini" onclick="setMode('auto')">切回自动</button>
      </div>
      <div class="hint">低于 42% 会伤硬盘（低速散热不足），故滑块下限为 42%。</div>
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
// 日志：最新的显示在最上面。接口本身仍按时间正序返回（下载下来的日志文件保持正常顺序）
function logText(s){return (s.log||[]).slice().reverse().join(NL)}
// 当前生效的「安全下限」。档位转速低于它就会被抬上去，表格里标黄提醒 ——
// 免得用户以为「明明设了 40% 却没效果」是程序坏了。
// 另有一个不可突破的 42% 硬性下限（见后端 HARD_MIN_PCT，低于则硬盘散热不足）。
let curMinPct=42;
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
  const rr=Math.max(0,Math.round(s.ramp_remaining||0));
  if(s.mode==='auto'&&s.ramp_pending_pct!=null&&rr>0)
    m+=` <span class="tag hold">升档待确认 ${s.ramp_pending_pct}% · 还需 ${rr}s</span>`;
  else if(s.mode==='auto'&&s.ramp_pending_pct!=null&&s.ramp_pending_pct<s.pct)
    m+=` <span class="tag hold">降档待确认 ${s.ramp_pending_pct}%</span>`;
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
  if(logOpen)g('log').textContent=logText(s);

  // 档位值低于「生效下限」时会被抬上去，这是「设了 40% 却没效果」的根因。
  // 把它显式化：面板标题旁给标签，档位表里对应的输入框标黄。
  // 生效下限 = max(用户设的安全下限, 42% 硬性下限)；下限保护硬盘低速散热不足。
  const HARD_MIN=42;
  const mpRaw=parseFloat(dirty.has('min_pct')?g('min_pct').value:s.min_pct)||HARD_MIN;
  curMinPct=Math.max(HARD_MIN,mpRaw);
  const lowSteps=(s.steps||[]).filter(x=>parseFloat(x.pct)<curMinPct);
  const cw=g('curveWarn');
  if(lowSteps.length){
    cw.style.display='';
    cw.textContent = mpRaw<HARD_MIN
      ? `⚠ ${lowSteps.length} 个档位低于 ${HARD_MIN}% 硬性下限（硬盘低速散热不足）`
      : `⚠ ${lowSteps.length} 个档位低于安全下限 ${curMinPct}%`;
  }else{cw.style.display='none';}

  if(!stepsDirty)renderSteps(s.steps);
  setVal('hold_trigger',s.hold_trigger);
  setVal('hold_seconds',s.hold_seconds);
  setVal('min_pct',s.min_pct);
  setVal('ramp_delay',s.ramp_delay);
  setVal('drop_delay',s.drop_delay);
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
const CURVE_IDS=['hold_trigger','hold_seconds','min_pct','temp_source','ramp_delay','drop_delay'];
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
  const low=parseFloat(x.pct) < curMinPct;
  const tip=low?`低于生效下限 ${curMinPct}%，实际按 ${curMinPct}% 运行`:'';
  return `<tr><td><input class="step-input" type="number" step="0.1" value="${x.below}" id="b${k}"></td>`+
    `<td><input class="step-input${low?' low':''}" type="number" step="1" value="${x.pct}" id="p${k}"${low?` title="${tip}"`:''}></td>`+
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
      ramp_delay:parseFloat(g('ramp_delay').value),
      drop_delay:parseFloat(g('drop_delay').value),
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
  if(logOpen&&cur)document.getElementById('log').textContent=logText(cur);
}
// 手动模式一步到位：直接把滑块当前值送上去，写入硬件并切 manual。
// 原来「暂存 → 确认应用」两步里的暂存只是把值存进后端，没有实际意义，已去掉。
async function applyManual(){
  const v=parseInt(document.getElementById('slider').value);
  const r=await api('/api/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pct:v})});
  clearDirty(id=>id==='slider');
  refresh();alert(r.ok?('已应用 '+r.applied+'% 并切到手动模式'):('应用失败：'+(r.msg||'未知错误')));
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
  const n=hist.t.length, T=18, B=16, L=30;
  const hasR=on.some(s=>s.axis==='R');
  const hasR2=on.some(s=>s.axis==='R2');
  const R=(hasR?26:8)+(hasR2?36:0);
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
                s["ramp_delay"] = cfg.get("ramp_delay", 4)
                s["drop_delay"] = cfg.get("drop_delay", 0)
                s["interval"] = cfg.get("interval", 2)
                s["ramp_pending_pct"] = state.get("ramp_pct")
                s["ramp_remaining"] = max(
                    0.0, float(state.get("ramp_remaining") or 0.0))
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
            _p = float(b.get("pct", cfg.get("manual_pct", 80)))
            cfg["pending_pct"] = max(float(HARD_MIN_PCT), min(100.0, _p))
            save_cfg()
            self._send(200, json.dumps({"ok": True, "pending": cfg["pending_pct"]}))
        elif self.path == "/api/apply":
            # 一步到位：body 里带 pct 就用它（前端滑块直读），
            # 不带则退回旧的「先暂存再应用」语义，接口保持向后兼容。
            v = b.get("pct")
            if v is None:
                v = cfg.get("pending_pct")
            if v is None:
                self._send(400, json.dumps({"ok": False, "msg": "没有要应用的转速值"}))
                return
            v = float(v)
            # 手动也不能低于硬性下限（否则用户绕过曲线就能把风扇压到很低）
            v = max(float(HARD_MIN_PCT), min(100.0, v))
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
            for k in ("steps", "hold_trigger", "hold_seconds", "min_pct",
                      "temp_source", "ramp_delay", "drop_delay"):
                if k in b:
                    cfg[k] = b[k]
            # 硬性下限不可突破：填低了直接抬上来（保存即生效，界面会看到被抬后的值）
            try:
                cfg["min_pct"] = max(HARD_MIN_PCT, min(100.0, float(cfg.get("min_pct", HARD_MIN_PCT))))
            except (TypeError, ValueError):
                cfg["min_pct"] = HARD_MIN_PCT
            # 档位规则变了：丢弃未完成的确认计时，下一轮按新规则重新计时
            # （这里不清 curve_pct，否则每次保存都会绕过确认直接跳档）
            with lock:
                _clear_ramp()
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


def _parse_args(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="QU805 风扇温控 Web UI（数据目录 / 端口均可外部指定）")
    ap.add_argument("--data-dir", default=None,
                    help="数据目录，存放 curve-config.json / fan.log / history.tsv")
    ap.add_argument("--port", type=int, default=None, help="监听端口")
    ap.add_argument("--listen", default=None, help="监听地址，默认 0.0.0.0")
    return ap.parse_args(argv)


if __name__ == "__main__":
    _a = _parse_args()
    # 命令行参数优先于环境变量 / 默认值，覆盖后其余函数读到的就是新值
    if _a.data_dir:
        DATA_DIR = os.path.abspath(_a.data_dir)
        CFG_FILE = os.path.join(DATA_DIR, "curve-config.json")
        LOG_FILE = os.path.join(DATA_DIR, "fan.log")
        HIST_FILE = os.path.join(DATA_DIR, "history.tsv")
    if _a.port:
        PORT = int(_a.port)
    if _a.listen:
        LISTEN = _a.listen

    os.makedirs(DATA_DIR, exist_ok=True)
    load_cfg()
    log("=== 服务启动 | TZ=%s | %s | UTC%+.1f | 数据目录 %s | 监听 %s:%d | 日志保留 %s 天 ===" % (
        os.environ.get("TZ") or "未设置", time.tzname[0],
        -time.timezone / 3600.0, DATA_DIR, LISTEN, PORT,
        cfg.get("log_retention_days", 7)))
    _d = prune_log(force=True)
    if _d:
        log("启动清理：删除 %d 条超期日志" % _d)
    _h = prune_history(force=True)
    if _h:
        log("启动清理：删除 %d 条超期历史采样" % _h)
    t = threading.Thread(target=control_loop, daemon=True)
    t.start()
    HTTPServer((LISTEN, PORT), Handler).serve_forever()

