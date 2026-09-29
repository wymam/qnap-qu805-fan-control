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
body{margin:0;background:#12141a;color:#e6e8ee;font-family:-apple-system,"PingFang SC",system-ui,sans-serif;padding:24px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#8b93a7;font-size:13px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:22px}
.card{background:#1b1f28;border:1px solid #2a3040;border-radius:10px;padding:16px}
.card .k{font-size:12px;color:#8b93a7;margin-bottom:6px}
.card .v{font-size:26px;font-weight:600}
.card .v small{font-size:13px;color:#8b93a7;font-weight:400}
.hot{color:#ff6b6b}.warm{color:#ffa94d}.cool{color:#51cf66}
.panel{background:#1b1f28;border:1px solid #2a3040;border-radius:10px;padding:18px;margin-bottom:18px}
.panel h2{font-size:15px;margin:0 0 14px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid #262c3a}
th{color:#8b93a7;font-weight:500;font-size:12px}
input,select{background:#12141a;color:#e6e8ee;border:1px solid #2f3648;border-radius:6px;padding:7px 9px;width:100%}
button{background:#3b7cff;color:#fff;border:0;border-radius:6px;padding:9px 16px;cursor:pointer;font-size:14px}
button.gray{background:#2f3648}
.btn-dirty{background:#8a5a12;color:#ffd8a8;box-shadow:inset 0 0 0 1px #b8862b}
button:active{opacity:.8}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:12px}
input[type=range]{width:260px;padding:0}
input[type=checkbox]{width:auto}
.tag{display:inline-block;padding:3px 9px;border-radius:5px;font-size:12px;background:#2f3648;color:#b6c0d6}
.tag.on{background:#1f6f3f;color:#8ce0a8}
.tag.hold{background:#7a4a12;color:#ffc078}
.tag.off{background:#3a2020;color:#e08a8a}
.log{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#8b93a7;max-height:150px;overflow:auto;white-space:pre-line}
.hint{font-size:12px;color:#6b7488;margin-top:8px;line-height:1.6}
.warn{color:#ffa94d}
.mono{font-family:ui-monospace,Menlo,monospace}
</style></head><body>
<h1>QU805 风扇温控</h1>
<div class="sub">qnap8528 EC 驱动 · ITE8528E · 自动调速中<span id="dirtyTag" class="tag hold" style="display:none;margin-left:8px">有未保存的修改 · 自动刷新已暂停覆盖</span></div>

<div class="grid">
  <div class="card"><div class="k">CPU 温度</div><div class="v" id="temp">--</div></div>
  <div class="card"><div class="k">机械盘最高温</div><div class="v" id="hddmax">--</div></div>
  <div class="card"><div class="k">风扇转速</div><div class="v" id="pct">--</div></div>
  <div class="card"><div class="k">PWM 原始值</div><div class="v" id="pwm">--</div></div>
  <div class="card"><div class="k">实测 RPM</div><div class="v" id="rpm">--</div></div>
  <div class="card"><div class="k">模式 / 状态</div><div class="v" id="mode" style="font-size:15px">--</div></div>
</div>

<div class="panel">
  <h2>温度档位规则</h2>
  <table><thead><tr><th>温度不高于 (°C)</th><th>风扇转速 (%)</th><th></th></tr></thead>
  <tbody id="steps"></tbody></table>
  <div class="row"><button class="gray" onclick="addStep()">+ 增加档位</button></div>
  <div class="row">
    <span style="font-size:13px;color:#8b93a7">高温触发 (°C)</span>
    <input id="hold_trigger" type="number" step="0.1" style="width:100px">
    <span style="font-size:13px;color:#8b93a7">保持时长 (秒)</span>
    <input id="hold_seconds" type="number" step="1" style="width:100px">
    <span style="font-size:13px;color:#8b93a7">安全下限 (%)</span>
    <input id="min_pct" type="number" step="1" style="width:90px">
  </div>
  <div class="row">
    <span style="font-size:13px;color:#8b93a7">温度源</span>
    <select id="temp_source" style="width:180px">
      <option value="coretemp">CPU (coretemp)</option>
      <option value="qnap8528">EC 传感器 (qnap8528)</option>
      <option value="max">两者取最高</option>
    </select>
    <button id="saveCurveBtn" onclick="saveCurve()">保存规则</button>
  </div>
  <div class="hint">超过「高温触发」温度后，即使温度降到很低，也会保持触发时的档位转速运行指定时长，避免转速频繁抖动。</div>
</div>

<div class="panel">
  <h2>机械硬盘高温保护</h2>
  <div class="row">
    <label style="font-size:13px;color:#8b93a7"><input type="checkbox" id="hdd_enabled"> 启用</label>
    <span style="font-size:13px;color:#8b93a7">触发温度 (°C)</span>
    <input id="hdd_trigger_temp" type="number" step="0.1" style="width:90px">
    <span style="font-size:13px;color:#8b93a7">触发转速 (%)</span>
    <input id="hdd_pct" type="number" step="1" style="width:90px">
    <span style="font-size:13px;color:#8b93a7">持续时长 (秒)</span>
    <input id="hdd_hold_seconds" type="number" step="1" style="width:100px">
    <span style="font-size:13px;color:#8b93a7">监视范围</span>
    <select id="hdd_watch" style="width:190px">
      <option value="rotational">仅机械硬盘</option>
      <option value="all">全部 SATA 盘</option>
      <option value="list">自定义列表</option>
    </select>
    <button id="saveHddBtn" onclick="saveHdd()">保存保护规则</button>
  </div>
  <div class="row" id="hddlist_row" style="display:none">
    <span style="font-size:13px;color:#8b93a7">监视设备（逗号分隔）</span>
    <input id="hdd_sensors" type="text" style="width:280px" placeholder="sdb,sdc,sdd,sde">
  </div>
  <div class="row"><span id="hddstate" class="tag">--</span></div>
  <table style="margin-top:12px"><thead><tr>
    <th>设备</th><th>型号</th><th>类型</th><th>温度</th><th>参与监视</th>
  </tr></thead><tbody id="hddtable"></tbody></table>
  <div class="hint">
    任一被监视磁盘达到「触发温度」时，风扇转速不低于「触发转速」并持续「持续时长」秒；
    期间即使硬盘降温也会维持，避免转速抖动。    该保护在自动/手动模式下<b>都生效</b>
    （作为转速下限，常规曲线若要求更高则以更高者为准）。<br>
    磁盘温度来自内核 <span class="mono">drivetemp</span> 模块；若表格为空，
    请在宿主机执行 <span class="mono">modprobe drivetemp</span>（已配置开机自动加载）。
  </div>
</div>

<div class="panel">
  <h2>手动模式</h2>
  <div class="row">
    <input type="range" id="slider" min="0" max="100" step="1" value="80" oninput="document.getElementById('sv').textContent=this.value+'%'">
    <span id="sv" style="width:50px">80%</span>
    <button onclick="setPending()">暂存</button>
    <button class="gray" onclick="applyPending()">✅ 确认应用</button>
    <button class="gray" onclick="setMode('auto')">切回自动</button>
  </div>
  <div class="hint">手动值需点「确认应用」才会真正写入硬件，防止误操作。</div>
</div>

<div class="panel">
  <h2>运行日志</h2>
  <div class="row">
    <span style="font-size:13px;color:#8b93a7">保留天数</span>
    <input id="log_retention_days" type="number" step="1" min="1" max="365" style="width:80px">
    <button id="saveLogBtn" onclick="saveLogCfg()">保存</button>
    <a href="/api/log?lines=0" target="_blank" style="font-size:13px;color:#3b7cff;text-decoration:none">查看 / 下载完整日志</a>
    <span id="logmeta" class="tag">--</span>
  </div>
  <div class="log" id="log" style="max-height:340px"></div>
  <div class="hint">
    日志按 <span class="mono">YYYY-MM-DD HH:MM:SS</span> 明文追加到
    <span class="mono">/data/fan.log</span>（宿主机
    <span class="mono">/vol1/docker/fnos-fan-webui/data/fan.log</span>），容器重启不丢；
    超过「保留天数」的记录会被自动删除。页面显示最近 200 行。
  </div>
</div>

<script>
async function api(u,o){const r=await fetch(u,o);return r.json()}
function cls(t){return t>=85?'hot':(t>=70?'warm':'cool')}
function hcls(t){return t>=44?'hot':(t>=40?'warm':'cool')}
let cur=null;
async function refresh(){
  const s=await api('/api/status');cur=s;
  document.getElementById('temp').innerHTML=s.temp==null?'--':`<span class="${cls(s.temp)}">${s.temp} <small>°C</small></span>`;
  document.getElementById('hddmax').innerHTML=s.hdd_max==null?'--':`<span class="${hcls(s.hdd_max)}">${s.hdd_max} <small>°C</small></span>`;
  document.getElementById('pct').innerHTML=s.pct==null?'--':`${s.pct} <small>%</small>`;
  document.getElementById('pwm').textContent=s.pwm==null?'--':s.pwm+' / 255';
  document.getElementById('rpm').innerHTML=(s.rpm||[]).map(r=>`<div>${r.rpm} <small>RPM (fan${r.fan})</small></div>`).join('')||'--';
  let m=`<span class="tag ${s.mode==='auto'?'on':''}">${s.mode==='auto'?'自动':'手动'}</span> `;
  const left=Math.max(0,Math.round(s.hold_remaining||0));
  if(left>0) m+=`<span class="tag hold">保持中 ${left}s</span> `;
  const hl=Math.max(0,Math.round(s.hdd_hold_remaining||0));
  if(hl>0) m+=`<span class="tag hold">硬盘保护 ${hl}s</span>`;
  document.getElementById('mode').innerHTML=m;
  const hs=document.getElementById('hddstate');
  if(!s.hdd_enabled){hs.className='tag off';hs.textContent='硬盘保护已关闭';}
  else if(hl>0){hs.className='tag hold';hs.textContent=`保护生效中 · 剩余 ${hl}s（转速下限 ${s.hdd_pct}%）`;}
  else if(s.hdd_max==null){hs.className='tag off';hs.textContent='未检测到磁盘温度传感器（宿主机需 modprobe drivetemp）';}
  else {hs.className='tag on';hs.textContent=`待命 · 机械盘最高 ${s.hdd_max}°C / 阈值 ${s.hdd_trigger_temp}°C`;}
  const tb=document.getElementById('hddtable');
  tb.innerHTML=(s.hdd_disks||[]).map(d=>{
    const ty=d.rotational===1?'机械盘':(d.rotational===0?'SSD':'未知');
    return `<tr><td class="mono">${d.dev}</td><td>${d.model||'--'}</td><td>${ty}</td>
      <td><span class="${hcls(d.temp)}">${d.temp} °C</span></td>
      <td>${d.watched?'<span class="tag on">是</span>':'<span class="tag">否</span>'}</td></tr>`;
  }).join('')||'<tr><td colspan="5" class="warn">没有 drivetemp 传感器</td></tr>';
  document.getElementById('log').textContent=(s.log||[]).join('\\n');
  if(!stepsDirty){
    const rows=s.steps.map(stepRow).join('');
    const tb=document.getElementById('steps');
    if(tb.dataset.sig!==rows){tb.innerHTML=rows;tb.dataset.sig=rows;}
  }
  setVal('hold_trigger',s.hold_trigger);
  setVal('hold_seconds',s.hold_seconds);
  setVal('min_pct',s.min_pct);
  setVal('temp_source',s.temp_source);
  setVal('slider',s.manual_pct);
  if(!dirty.has('slider'))document.getElementById('sv').textContent=s.manual_pct+'%';
  setVal('hdd_enabled',s.hdd_enabled);
  setVal('hdd_trigger_temp',s.hdd_trigger_temp);
  setVal('hdd_pct',s.hdd_pct);
  setVal('hdd_hold_seconds',s.hdd_hold_seconds);
  setVal('hdd_watch',s.hdd_watch);
  setVal('hdd_sensors',(s.hdd_sensors||[]).join(','));
  if(!dirty.has('hdd_watch'))document.getElementById('hddlist_row').style.display=(s.hdd_watch==='list')?'flex':'none';
  setVal('log_retention_days',s.log_retention_days);
  const lm=document.getElementById('logmeta');
  if(lm)lm.textContent='保留 '+s.log_retention_days+' 天 · 文件 '+(((s.log_bytes||0)/1024).toFixed(1))+' KB · 显示最近 '+((s.log||[]).length)+' 行';
}

// ===== 未保存修改保护 =====
// 只要某个控件被改过就记入 dirty，自动刷新永不再覆盖它（原版只在「+增加档位」时
// 才置 window.edt，导致手填数值 3 秒后被服务端值冲掉）。
let stepSeq=0, dirty=new Set(), stepsDirty=false;
const CURVE_IDS=['hold_trigger','hold_seconds','min_pct','temp_source'];
const HDD_IDS=['hdd_enabled','hdd_trigger_temp','hdd_pct','hdd_hold_seconds','hdd_watch','hdd_sensors'];
const LOG_IDS=['log_retention_days'];
function isStepId(id){return /^[bp]\\d+$/.test(id)}
function anyDirty(ids){return ids.some(function(i){return dirty.has(i)})}
function setVal(id,v){
  const el=document.getElementById(id);
  if(!el||dirty.has(id)||document.activeElement===el)return;
  if(el.type==='checkbox')el.checked=!!v;else el.value=(v==null?'':v);
}
function clearDirty(pred){
  Array.from(dirty).forEach(function(id){if(pred(id))dirty.delete(id)});
  paintDirty();
}
function paintDirty(){
  const tag=document.getElementById('dirtyTag');
  if(tag)tag.style.display=dirty.size?'inline-block':'none';
  const b1=document.getElementById('saveCurveBtn'),b2=document.getElementById('saveHddBtn'),b3=document.getElementById('saveLogBtn');
  if(b1)b1.className=(anyDirty(CURVE_IDS)||stepsDirty)?'btn-dirty':'';
  if(b2)b2.className=anyDirty(HDD_IDS)?'btn-dirty':'';
  if(b3)b3.className=anyDirty(LOG_IDS)?'btn-dirty':'';
}
document.addEventListener('input',function(e){
  const t=e.target;if(!t||!t.id)return;
  dirty.add(t.id);if(isStepId(t.id))stepsDirty=true;paintDirty();
});
document.addEventListener('change',function(e){
  const t=e.target;if(!t||!t.id)return;
  dirty.add(t.id);paintDirty();
});
function stepRow(x){
  const k=++stepSeq;
  return `<tr><td><input type="number" step="0.1" value="${x.below}" id="b${k}"></td>
     <td><input type="number" step="1" value="${x.pct}" id="p${k}"></td>
     <td><button class="gray" onclick="delStep(${k})">删除</button></td></tr>`;
}
function addStep(){
  if(!cur)return;
  document.getElementById('steps').insertAdjacentHTML('beforeend',stepRow({below:999,pct:100}));
  stepsDirty=true;paintDirty();
}
function delStep(k){
  const r=document.getElementById('b'+k);
  if(r){r.closest('tr').remove();stepsDirty=true;paintDirty();}
}
async function saveCurve(){
  const tbody=document.getElementById('steps');
  const steps=[];
  tbody.querySelectorAll('tr').forEach(function(tr){
    const a=tr.querySelector('input[type=number]');
    const b=tr.querySelectorAll('input[type=number]')[1];
    if(a&&b)steps.push({below:parseFloat(a.value),pct:parseFloat(b.value)});
  });
  steps.sort(function(x,y){return x.below-y.below});
  await api('/api/curve',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({steps,hold_trigger:parseFloat(document.getElementById('hold_trigger').value),
      hold_seconds:parseFloat(document.getElementById('hold_seconds').value),
      min_pct:parseFloat(document.getElementById('min_pct').value),
      temp_source:document.getElementById('temp_source').value})});
  stepsDirty=false;
  clearDirty(function(id){return CURVE_IDS.indexOf(id)>=0||isStepId(id)});
  alert('规则已保存');refresh();
}
document.getElementById('hdd_watch').addEventListener('change',function(){
  document.getElementById('hddlist_row').style.display=(this.value==='list')?'flex':'none';
});
async function saveHdd(){
  const body={
    hdd_enabled:document.getElementById('hdd_enabled').checked,
    hdd_trigger_temp:parseFloat(document.getElementById('hdd_trigger_temp').value),
    hdd_pct:parseFloat(document.getElementById('hdd_pct').value),
    hdd_hold_seconds:parseFloat(document.getElementById('hdd_hold_seconds').value),
    hdd_watch:document.getElementById('hdd_watch').value,
    hdd_sensors:document.getElementById('hdd_sensors').value.split(',').map(function(x){return x.trim()}).filter(function(x){return x})
  };
  const r=await api('/api/hdd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  clearDirty(function(id){return HDD_IDS.indexOf(id)>=0});
  alert(r.ok?'硬盘保护规则已保存':'保存失败');refresh();
}
async function saveLogCfg(){
  const v=parseFloat(document.getElementById('log_retention_days').value);
  const r=await api('/api/logcfg',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({log_retention_days:v})});
  clearDirty(function(id){return LOG_IDS.indexOf(id)>=0});
  alert(r.ok?('已保存（保留 '+r.retention_days+' 天），本次清理 '+r.dropped+' 条超期日志'):'保存失败');
  refresh();
}
async function setPending(){
  const v=parseInt(document.getElementById('slider').value);
  await api('/api/set-pwm-pending',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pct:v})});
  alert('已暂存 '+v+'%，点「确认应用」生效');
}
async function applyPending(){
  await api('/api/apply',{method:'POST'});
  clearDirty(function(id){return id==='slider'});
  refresh();alert('已应用到硬件');
}
async function setMode(m){
  await api('/api/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:m})});
  refresh();
}
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
            if "log_retention_days" in b:
                try:
                    v = float(b["log_retention_days"])
                except Exception:
                    v = 7.0
                cfg["log_retention_days"] = max(1.0, min(365.0, v))
            save_cfg()
            dropped = prune_log(force=True)
            self._send(200, json.dumps({"ok": True, "retention_days": cfg["log_retention_days"],
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
    t = threading.Thread(target=control_loop, daemon=True)
    t.start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
