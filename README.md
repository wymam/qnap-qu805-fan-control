# QU805 风扇调速（威联通 QU805 + 飞牛 fnOS）

让 **威联通 QU805**（iEi **SA145** 主板 / **ITE8528E** EC 芯片）在刷了 **飞牛 fnOS**
之后重新拿回风扇 PWM 控制权，并用一个 Docker 容器 + Web UI 做温度自适应调速。

顺带解决了三件让人头疼的事：

| 痛点 | 本项目的做法 |
|---|---|
| `modprobe qnap8528` 报 `No such device` | 给驱动补一条 `QU805 / SA145` 机型配置（子串匹配 `MB=` 串） |
| **系统更新后风扇又失控** | 内核安装钩子 + 开机自愈脚本，**下次升级不用再人工修** |
| 只能命令行写 pwm | Web UI：温度档位表、手动模式、机械盘高温保护、持久化日志 |

同主板的 **QU605 / QU805** 系列应该都适用（未验证，欢迎反馈）。

> **English TL;DR** — Restore PWM fan control on a QNAP QU805 (iEi SA145 / ITE8528E EC)
> running fnOS, then run a small Docker + Web UI app for temperature‑based fan curves,
> per‑disk temperature protection, and persistent logs. Includes a kernel postinst hook
> so a kernel upgrade no longer silently breaks the fan driver.

---

## 目录

- [1. 问题现象](#1-问题现象)
- [2. 原理](#2-原理)
- [3. 环境要求](#3-环境要求)
- [4. 快速开始](#4-快速开始)
- [5. 分步说明](#5-分步说明)
- [6. Web UI](#6-web-ui)
- [7. 温控规则](#7-温控规则)
- [8. 系统更新后自动恢复（防复发）](#8-系统更新后自动恢复防复发)
- [9. 运行日志](#9-运行日志)
- [10. 故障排查](#10-故障排查)
- [11. 回滚](#11-回滚)
- [12. 常见问题](#12-常见问题)
- [13. 目录结构](#13-目录结构)
- [14. 许可与致谢](#14-许可与致谢)

---

## 1. 问题现象

QU805 原厂是 QuTS/QTS 系统，风扇由 EC 芯片管理。刷成飞牛 fnOS 后，常见两种症状：

- 风扇**一直全速**转，或者干脆不转；
- `/sys/class/hwmon` 下**找不到任何 `pwm*` 接口**，`sensors` 也看不到风扇转速。

手动加载驱动会失败：

```console
# modprobe qnap8528
modprobe: FATAL: Module qnap8528 not found in directory /lib/modules/<内核版本>
```

如果模块已编译但加载失败，`dmesg` 里是这样：

```text
qnap8528 @ qnap8528_ec_hw_check: Could not locate IT8528 EC device
qnap8528 @ qnap8528_find_config: Searching configs for a match with MB=<主板串>
qnap8528 @ qnap8528_find_config: Could not find configuration for device
qnap8528: probe with driver qnap8528 failed with error -524
```

---

## 2. 原理

### 2.1 为什么风扇会失控

飞牛 fnOS 自带 `qnap8528` 驱动源码（`/usr/src/qnap8528-<版本>/`），但**没有为新内核编译过**，
而且驱动内部的机型表里**没有 QU805**。两层原因叠在一起，就成了「风扇不可控」。

### 2.2 为什么 `modprobe` 一定失败

驱动里维护一张「主板型号 → 风扇 / 槽位 / LED 能力」的表，只认登记过的机型。
匹配逻辑（`src/qnap8528.c`）是**子串匹配**：

```c
if (strstr(mb_model, qnap8528_configs[i].mb_model))
```

本机 `MB=` 串形如 `70006SA14500xxxxRS`，所以只要给表里补一条
`mb_model = "SA145"` 的记录就能命中 —— 这就是 [`scripts/patch_qnap_config.py`](scripts/patch_qnap_config.py)
做的事。

另外 EC 的硬件 ID 校验在本机过不了，加载时必须带 `skip_hw_check=true`（跳过校验不影响功能）。

> `it87` 模块在这台机器上同样 `No such device`，它不是正解，别在它上面浪费时间。

### 2.3 架构

```text
┌─────────────────────────────────────────────────────────────────────┐
│  fnOS                                                                 │
│                                                                       │
│  ┌──────────────────┐   /sys/class/hwmon/hwmonX/pwm1   ┌───────────┐  │
│  │ qnap8528 (DKMS)  │◄─────────────────────────────────│ 风扇 1 / 2│  │
│  │ ITE8528E EC      │        写 0-255 即调速           │  (EC)     │  │
│  └──────────────────┘                                  └───────────┘  │
│           ▲                                                           │
│           │ 补机型配置 + dkms install + modprobe skip_hw_check=true    │
│           │                                                           │
│  ┌────────┴─────────┐                                                 │
│  │ 防复发（自动）    │  /etc/kernel/postinst.d/zz-qnap8528  ← 升级时    │
│  │                  │  /usr/local/sbin/qnap8528-load.sh    ← 开机时    │
│  └──────────────────┘                                                 │
│                                                                       │
│  ┌────────────────────────────────────────────────────────┐           │
│  │ Docker: fnos-fan-webui   (--privileged)                │           │
│  │   /sys/class/hwmon :rw  →  读温度 / 写 pwm1             │           │
│  │   /sys/class/thermal:ro                                │           │
│  │   /vol1/docker/.../data → curve-config.json + fan.log  │           │
│  │   :8080 → Web UI + JSON API                            │           │
│  └────────────────────────────────────────────────────────┘           │
│                                                                       │
│  drivetemp (内核模块) → 每块 SATA 盘的 SMART 温度，暴露成 hwmon          │
└─────────────────────────────────────────────────────────────────────┘
```

要点：

- 风扇温度的**执行端**是 `qnap8528` 驱动暴露的 `hwmon` 节点，写 `pwm1` 即可调速；
- 磁盘温度不需要 `smartmontools` —— 内核 `drivetemp` 模块会把每块 SATA 盘的
  SMART 温度直接暴露成 `hwmon`（`name=drivetemp`）；
- 容器靠**软链文本的末段**（都是 SCSI 地址，如 `2:0:0:0`）把 `hwmon` 配对回
  `/dev/sdX`，再读 `queue/rotational` 区分机械盘和 SSD，因此**不需要额外挂载**。

---

## 3. 环境要求

| 项目 | 要求 |
|---|---|
| 机型 | 威联通 QU805（或同主板 QU605），主板 iEi **SA145**，EC **ITE8528E** |
| 系统 | 飞牛 fnOS（其他 Debian 系应该也行，未验证） |
| 必备 | `dkms`、`gcc`、`make`、`python3`、当前内核的 `linux-headers-<版本>` |
| 可选 | `docker`（部署 Web UI 时需要）、`dmidecode`（自检用） |
| 源码 | `/usr/src/qnap8528-<版本>/`（fnOS 自带） |

> **检查内核头文件**：`ls /lib/modules/$(uname -r)/build` 必须存在，
> 否则无法编译 DKMS 模块。缺的话先 `apt install linux-headers-$(uname -r)`。

---

## 4. 快速开始

```bash
# 1) 把仓库放到 NAS 上（或直接 scp 上去）
git clone https://github.com/wymam/qnap-qu805-fan-control.git
cd qnap-qu805-fan-control

# 2) 看一眼环境，不落盘
sudo bash install.sh --dry-run

# 3) 全量安装（驱动 + 持久化 + 防复发 + Web UI），全程不用重启
sudo bash install.sh
```

装完打开 `http://<NAS_IP>:8080` 即可。

只想修驱动、不要 Web UI：

```bash
sudo bash install.sh --skip-app
```

---

## 5. 分步说明

不想用一键脚本的话，这是它内部做的事。

### 5.1 建立 SSH 通道（可选）

macOS/Linux 有 `sshpass` 就用它；没有的话用 `expect` 建 ControlMaster：

```bash
cat > /tmp/ssh_mux.exp <<'EOF'
#!/usr/bin/expect -f
set timeout 30
spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o NumberOfPasswordPrompts=1 -o ControlMaster=yes \
  -o ControlPath=/tmp/ssh_mux_%h -o ControlPersist=1800 -Nf <用户名>@<NAS_IP>
expect { -re "(?i)password:" { send "<密码>\r" } timeout { puts "TIMEOUT"; exit 1 } }
expect { -re "(?i)password:" { puts "AUTH_FAILED"; exit 1 } timeout { puts "MUX_OK" } }
EOF
chmod +x /tmp/ssh_mux.exp && /usr/bin/expect /tmp/ssh_mux.exp
S=/tmp/ssh_mux_<NAS_IP>
ssh -o ControlPath=$S <用户名>@<NAS_IP> 'bash -s' < 本地脚本.sh
```

Windows 上没有 `sshpass` 也没有 `expect`，可以用仓库里的
[`tools/rexec.py`](tools/rexec.py)（paramiko 密码认证 + 管道喂脚本）：

```bash
PY="C:/Users/<你>/.../python.exe"      # 任意带 paramiko 的 Python
$PY -m pip install paramiko
$PY tools/rexec.py -c 'uname -r; uptime'    # 单条命令
$PY tools/rexec.py -f 本地脚本.sh           # 多行脚本
```

凭证从环境变量或 `%TEMP%\.nas_cred.json` 读取，**不要写进仓库**。

### 5.2 硬件体检

```bash
uname -r
dmidecode -s baseboard-product-name      # 应输出 SA145
dmidecode -s system-product-name         # 应输出 QuNAS-X05
for d in /sys/class/hwmon/hwmon*; do echo "$d : $(cat $d/name)"; done
ls -1 /sys/class/hwmon/*/pwm*            # 修复前无输出；修好后 = hwmon3/pwm1
sensors
```

> ⚠️ **不要用 `find /sys/class/hwmon -name "pwm*"` 判断有没有 PWM**：
> `hwmon*` 是符号链接，`find` 默认不跟随，**即使 `pwm1` 存在也永远返回 0**。
> 用 `ls -1 /sys/class/hwmon/*/pwm*`。

### 5.3 补机型配置

```bash
python3 scripts/patch_qnap_config.py --dry-run     # 先看要插什么
sudo python3 scripts/patch_qnap_config.py
```

脚本会在配置表末尾的 `{ NULL }` 终止符**之前**插入一个括号配平的配置块，
并自动备份成 `qnap8528.h.bak-qu805`。幂等，重复跑不会重复插入。

参数可调：`--model`（标识名）、`--board`（主板子串，必须能命中 `MB=` 串）、
`--fans`（风扇 EC 索引，默认 `1,2`）。

### 5.4 编译并加载

```bash
K=$(uname -r)
sudo dkms install qnap8528/<版本> -k $K
sudo modprobe qnap8528 skip_hw_check=true
lsmod | grep qnap8528
dmesg | tail -6
```

成功的标志：

```text
qnap8528 @ qnap8528_find_config: Model MB code match found
qnap8528 @ qnap8528_find_config: Device model is QU805
qnap8528 @ qnap8528_register_hwmon: Hwmon device registered
```

编译时若出现 <code>cp: cannot stat '/lib/modules/&lt;K&gt;/build/.config'</code>，
**可以忽略** —— 只是 DKMS 想拷内核 `.config` 而已，模块照常编译、签名、加载。

### 5.5 验证 PWM 真的能控速

```bash
H=$(dirname $(ls /sys/class/hwmon/*/pwm1 | head -1))
ORIG=$(cat $H/pwm1)
echo 180 > $H/pwm1; sleep 6; cat $H/fan1_input   # 转速应明显上升
echo 230 > $H/pwm1; sleep 6; cat $H/fan1_input
echo $ORIG > $H/pwm1                              # 务必恢复原值
```

> 只往**提速**方向测，降速/停转有风险。
> 容器在自动模式下每 5 秒会按曲线覆写 `pwm1`，想干净地测就先
> `docker stop fnos-fan-webui`，测完再 `start`。

### 5.6 持久化

```bash
# 开机自动加载（注意：先写普通文件再 sudo cp，别用 tee -a 管道——
# sudo 时间戳没过期时它不读 stdin，会把密码吃进目标文件）
printf 'qnap8528\n'                            > /tmp/ml.conf
printf 'options qnap8528 skip_hw_check=true\n' > /tmp/mp.conf
sudo cp /tmp/ml.conf /etc/modules-load.d/qnap8528.conf
sudo cp /tmp/mp.conf /etc/modprobe.d/qnap8528.conf

# 磁盘温度传感器
printf 'drivetemp\n' > /tmp/dt.conf
sudo cp /tmp/dt.conf /etc/modules-load.d/drivetemp.conf
sudo modprobe drivetemp      # 立即生效，不用重启
```

### 5.7 部署 Web UI

```bash
sudo mkdir -p /vol1/docker/fnos-fan-webui/data
sudo cp app/fan_webui.py app/Dockerfile /vol1/docker/fnos-fan-webui/
sudo docker build -t fnos-fan-webui:latest /vol1/docker/fnos-fan-webui

sudo docker rm -f fnos-fan-webui 2>/dev/null
sudo docker run -d --name fnos-fan-webui --restart=unless-stopped \
  --privileged \
  -v /sys/class/hwmon:/sys/class/hwmon:rw \
  -v /sys/class/thermal:/sys/class/thermal:ro \
  -v /vol1/docker/fnos-fan-webui/data:/data \
  -p 8080:8080 \
  fnos-fan-webui:latest
```

两个挂载是**硬要求**：

- `--privileged`：否则 `pwm1` 写入静默失败，界面看着正常但风扇纹丝不动；
- `/sys/class/hwmon` 必须 `:rw`（不是 `:ro`）。

> 驱动是**在容器运行期间**才恢复的话，记得 `docker restart fnos-fan-webui`：
> 自动模式的控制循环会缓存首次探测到的 pwm 路径。

---

## 6. Web UI

### 6.1 功能

- 实时显示 **温度 / 机械盘最高温 / 转速百分比 / PWM 原始值 / 实测 RPM**
- 温度档位表可**增删改**，保存即生效
- **高温滞后保持**：超过阈值后维持触发时的档位一段时间，避免转速抖动
- **机械硬盘高温保护**：任一机械盘达到阈值 → 转速提到指定值并保持一段时间
- **手动模式**：拖动滑块 → 暂存 → 确认应用（两步防误操作）
- 温度源可切换：`coretemp`（CPU）/ `qnap8528`（EC 传感器）/ `max`（取最高）
- 运行日志：带日期、落盘、保留 N 天
- **未保存修改保护**：改过的输入框不会被自动刷新覆盖

### 6.2 界面

页面分五个区：顶部指标卡片、温度档位规则、机械硬盘高温保护、
手动模式、运行日志。有未保存的修改时，标题旁会出现
「有未保存的修改 · 自动刷新已暂停覆盖」标签，对应面板的保存按钮变为琥珀色。

### 6.3 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | Web UI |
| GET | `/api/status` | 温度 / PWM / RPM / 模式 / 档位 / 保持倒计时 / 磁盘温度 / 日志 |
| GET | `/api/log?lines=N` | 明文日志，`N=0` 取全部（默认 500，上限 20000） |
| POST | `/api/curve` | 更新档位表、滞后参数、温度源、安全下限 |
| POST | `/api/hdd` | 更新机械盘保护规则 |
| POST | `/api/logcfg` | 更新日志保留天数并立即清理 → `{ok, retention_days, dropped}` |
| POST | `/api/set-pwm-pending` | 暂存手动转速（不写硬件） |
| POST | `/api/apply` | 确认应用到硬件 |
| POST | `/api/mode` | 切换 `auto` / `manual` |

配置文件 `/vol1/docker/fnos-fan-webui/data/curve-config.json` **才是权威值**，
UI 上看到的未必是最新的，排查时直接读它。

---

## 7. 温控规则

### 7.1 温度档位 + 高温滞后保持

按「温度不高于 X → Y%」从上往下匹配第一个命中的档位。例如：

| 温度 | 转速 |
|---|---|
| ≤ 60 °C | 60% |
| ≤ 80 °C | 80% |
| ≤ 90 °C | 90% |
| 其他 | 100% |

**高温滞后保持**：温度超过 `hold_trigger` 时，记下当时的档位，
之后即使温度掉下来，也维持 `hold_seconds` 秒再回到曲线。
把 `hold_seconds` 设为 `0` 即等于关闭（此时不会再出现
「触发保持 xx% / 0s」这类等于没生效的日志）。

`min_pct` 是安全下限，防止风扇停转。

### 7.2 机械硬盘高温保护

| 配置项 | 默认 | 说明 |
|---|---|---|
| `hdd_enabled` | 是 | 总开关 |
| `hdd_trigger_temp` | 44 °C | 任一**被监视**磁盘达到即触发 |
| `hdd_pct` | 90 % | 触发后的转速**下限** |
| `hdd_hold_seconds` | 300 s | 持续时长（每轮达标都会刷新） |
| `hdd_watch` | `rotational` | `rotational` 仅机械盘 / `all` 全部 SATA 盘 / `list` 自定义 |
| `hdd_sensors` | — | `hdd_watch=list` 时监视的设备名，如 `["sdb","sdc"]` |

语义上有两点值得注意：

1. 它是**转速下限**而不是强制值：`pct = max(当前档位, hdd_pct)`。
   正常情况（CPU 不热）就是 90%；万一 CPU 同时高温、曲线要 100%，不会被它压下来。
2. **自动 / 手动模式都生效** —— 硬盘保护优先于手动静音。

---

## 8. 系统更新后自动恢复（防复发）

这是本项目最实用的一部分。**只做驱动修复，下次内核升级还会挂** —— 实测
升级到新内核时，同一批 DKMS 模块里的 `it87` 被自动重编译了，
`qnap8528` 却连 `/var/lib/dkms/qnap8528/<版本>/<新内核>` 目录都没建出来。
DKMS 的通用 `autoinstall` 遍历**不保证**能轮到本模块，所以不能依赖它。

三层防护，各管一段：

| 层级 | 机制 | 触发时机 |
|---|---|---|
| ① 内核安装钩子 | `/etc/kernel/postinst.d/zz-qnap8528` | 安装 `linux-image-*` 时由 postinst 经 `run-parts` 调用 |
| ② 开机自愈 | `qnap8528-load.service`（`WantedBy=sysinit.target`） | 每次开机，模块缺失就先补编译再加载 |
| ③ 手动兜底 | 三条命令 + 重启容器 | 前两层都失灵时 |

① 只认目标版本、一条命令、确定执行，无论成败都 `exit 0`，**绝不阻断内核安装**，
输出写到 `/var/log/qnap8528-dkms.log`。

**启动顺序是安全的**：`docker.service` 的 `After=` 里含 `basic.target` 与
`sysinit.target`，而 ② 挂在 `sysinit.target` 上，所以**容器不可能早于驱动启动**。

验证钩子会被调用：

```bash
run-parts --test /etc/kernel/postinst.d | grep zz-qnap8528
```

**升级后 30 秒自检**：

```bash
ls -1 /sys/class/hwmon/*/pwm* && cat /sys/class/hwmon/hwmon3/fan1_input
```

有输出就是全好。没输出：

```bash
modprobe qnap8528 skip_hw_check=true
tail -20 /var/log/qnap8528-dkms.log      # 日志会告诉你卡在哪一层
```

**已知的残留风险**（三层都拦不住）：

| 风险 | 兜底 |
|---|---|
| 新内核改了 API，驱动源码编不过 | 只能等上游更新或自行打补丁；日志里有编译错误 |
| `/usr/src/qnap8528-*` 或 `/var/lib/dkms/qnap8528` 被清掉 | 重新放置源码 + `dkms add` |
| `linux-headers-<新内核>` 在钩子之后才装 | ② 开机会重试；仍缺则补装头文件后重跑钩子 |
| 系统更新是镜像式 OTA、整体覆盖 `/etc` | 重跑 `install.sh` |

---

## 9. 运行日志

| 项目 | 说明 |
|---|---|
| 格式 | `YYYY-MM-DD HH:MM:SS  <消息>` |
| 位置 | 容器内 `/data/fan.log` → 宿主 `/vol1/docker/fnos-fan-webui/data/fan.log`，重启不丢 |
| 保留 | 默认 **7 天**（`log_retention_days` 可调），超期自动删除 |
| 时机 | 启动时强制清理一次，运行中每小时最多一次 |
| 安全 | 原子写（`tmp` + `os.replace`）；**时间戳解析不出来的行一律保留** |

> **容器时区**：`python:3.11-slim` 默认跑 UTC，日志时间会差 8 小时。
> 本项目的 [`app/Dockerfile`](app/Dockerfile) 里加了 `ENV TZ=Asia/Shanghai`
> （Debian 基础镜像自带 zoneinfo）。启动第一条日志会自报时区，便于自查。

---

## 10. 故障排查

| 现象 | 原因 | 处置 |
|---|---|---|
| `modprobe: No such device` | 缺机型配置 / 未加 `skip_hw_check` | 补配置 + `skip_hw_check=true` |
| dmesg `Could not find configuration` | 配置表无匹配款 | 确认 `mb_model` 是 `MB=` 串的子串 |
| `Module qnap8528 not found in directory /lib/modules/<新内核>` | 内核升级后没为它重编译 | `dkms install qnap8528/<版本> -k $(uname -r)`，并部署第 8 节的两层防护 |
| 编译 `bad exit status: 2` | 补丁破坏了 `qnap8528.h` 语法（多为丢了 `};`） | 从 `.bak-qu805` 恢复后重打 |
| 编译时 `cp: cannot stat '.../build/.config'` | DKMS 想拷内核 `.config` | 无害，忽略 |
| `find /sys/class/hwmon -name 'pwm*'` 返回 0 但明明有 pwm1 | `hwmon*` 是符号链接 | 改用 `ls -1 /sys/class/hwmon/*/pwm*` |
| 模块有了但没有 `pwm1` | `.fans` 为空或 probe 失败 | 看 dmesg 有无 `Hwmon device registered` |
| UI 显示正常但风扇不转 | 容器没 `--privileged`，或 hwmon 挂成 `:ro` | 重建容器，两者都要对 |
| Web UI 报「未找到 PWM 接口」但 `pwm1` 已存在 | 容器启动时缓存了 hwmon 路径 | `docker restart fnos-fan-webui` |
| 磁盘表格空白 / 类型显示「未知」 | 没加载 `drivetemp`，或容器读不到 `/sys/block` | `modprobe drivetemp`；容器需能读默认 `/sys` |
| 日志时间差 8 小时 | 容器跑 UTC | Dockerfile 里 `ENV TZ=Asia/Shanghai` 后重建镜像 |
| RPM 显示 65535 / 0 | 风扇索引填了不存在的风扇 | 调 `.fans` 数组 |
| `dkms` 命令找不到（普通用户） | `/usr/sbin` 不在 PATH 里 | 走 `sudo`，或用全路径 `/usr/sbin/dkms` |

---

## 11. 回滚

```bash
sudo cp /usr/src/qnap8528-<版本>/src/qnap8528.h.bak-qu805 \
        /usr/src/qnap8528-<版本>/src/qnap8528.h
sudo dkms remove qnap8528/<版本> -k $(uname -r)
sudo modprobe -r qnap8528
sudo docker rm -f fnos-fan-webui
sudo rm -f /etc/modules-load.d/qnap8528.conf /etc/modprobe.d/qnap8528.conf \
           /etc/modules-load.d/drivetemp.conf \
           /etc/systemd/system/qnap8528-load.service \
           /etc/kernel/postinst.d/zz-qnap8528 \
           /usr/local/sbin/qnap8528-load.sh
sudo systemctl daemon-reload
```

只想撤掉「防复发」那层、保留风扇功能的话，删掉后两个文件 + `daemon-reload` 即可。

---

## 12. 常见问题

**Q：会不会影响保修 / 有风险吗？**
改的是 DKMS 模块的源码副本和内核加载参数，不动 EC 固件、不动主板。回滚就是恢复备份文件。
但**写 pwm 会让风扇转速偏离原厂曲线**，请自行评估散热余量。

**Q：为什么不直接用 QNAP 原厂的 `fancontrol`？**
那是 QTS 下的东西，fnOS 里没有对应守护进程。

**Q：`skip_hw_check=true` 安全吗？**
它只跳过「EC 硬件 ID 是否匹配」这一步校验，不影响后续读写。本机实测长期稳定。

**Q：支持 QU605 吗？**
主板同为 SA145，大概率可以，但未验证。

**Q：容器镜像能直接用 Docker Hub 上的吗？**
没有。这个镜像是本站自建的（`Dockerfile` 只做一件事：把 `fan_webui.py` 塞进
`python:3.11-slim`），必须本地构建。

**Q：为什么不用 `smartctl` 读磁盘温度？**
要额外装 `smartmontools` 并给容器 `/dev` 权限。内核 `drivetemp` 模块零依赖、
零额外挂载就能做到，实测读数与 `smartctl` 完全一致。

---

## 13. 目录结构

```text
.
├── install.sh                      # 一键安装（幂等，无需重启）
├── app/
│   ├── fan_webui.py                # 温控 Web UI + JSON API（单文件，无第三方依赖）
│   └── Dockerfile                  # 基于 python:3.11-slim，含 TZ
├── scripts/
│   ├── patch_qnap_config.py        # 给驱动补 QU805/SA145 机型配置（幂等 + 自检）
│   ├── zz-qnap8528.sh              # 内核 postinst 钩子：升级时精确重编译
│   ├── qnap8528-load.sh            # 开机自愈：模块缺失就补编译再加载
│   └── qnap8528-load.service       # 对应 systemd unit
└── tools/
    └── rexec.py                    # Windows 端远程执行助手（paramiko）
```

---

## 14. 许可与致谢

- 本项目采用 [MIT 许可证](LICENSE)。
- `app/fan_webui.py` **改自社区原帖作者自建的版本**（该版本未附许可证声明）。
  本仓库在它基础上重写了前端刷新逻辑、新增机械盘高温保护、持久化日志与
  保留策略等。若原作者有异议，请提 Issue，会立即调整。
- `qnap8528` 驱动版权归其原作者所有，本项目只提供**配置补丁**，不重新分发驱动源码。
- 感谢 fnOS 社区里分享踩坑经验的朋友们。

**免责声明**：修改内核模块与风扇控制策略存在风险（包括但不限于过热、数据丢失、
硬件损坏）。请自行评估并承担后果。建议先在可回滚的环境中验证。
