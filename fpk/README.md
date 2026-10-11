# 打包成飞牛应用（FPK）

把「qnap8528 驱动 + 风扇温控 Web 界面」打包成**一个可离线安装的飞牛（fnOS）应用**。
安装后不再需要 Docker、不需要手工放 systemd 单元和内核钩子 —— 在「应用中心 → 手动安装」
里选一个 `.fpk` 文件即可。

> 这是 [`README.md`](../README.md) 里那套手工安装方式的**打包版**。
> 两条路都能用：手工方式适合排障与理解原理，FPK 适合日常维护与重装。

---

## 目录结构

```text
fpk/
├── README.md                      ← 本文件
├── tools/
│   ├── build_fpk.py               # 打包脚本：本地清理 → 送 NAS /tmp → fnpack build → 取回
│   ├── fetch-common.sh            # 取 fnOS 应用生命周期框架 cmd/common（见下）
│   ├── fetch-driver.sh            # 取 qnap8528 驱动源码（本仓库不分发）
│   └── make-icons.py              # 生成应用图标（本仓库不存二进制）
└── qu805-fancontrol/              # 应用项目源
    ├── manifest                   # 应用元信息（名称 / 版本 / 端口 / 描述）
    ├── config/
    │   ├── privilege              # run-as: root（开 DKMS、写 pwm1 需要）
    │   └── resource               # port-config + systemd-unit
    ├── wizard/config
    ├── cmd/                       # 生命周期脚本（安装 / 卸载 / 升级 / 服务控制）
    │   ├── common                 # ← 需要 tools/fetch-common.sh 取回来
    │   ├── main                   # start / stop / restart / status / log
    │   ├── service-setup          # 本应用的全部逻辑都在这里
    │   └── install_init / install_callback / uninstall_* / upgrade_* / config_*
    └── app/                       # 应用载荷（安装时展开到 TRIM_APPDEST）
        ├── fan_webui.py           # ← 不在仓库里：tools/sync-app.sh 从 app/ 复制
        ├── driver-src/            # qnap8528 驱动源码（需 tools/fetch-driver.sh 补齐）
        ├── tools/patch_qnap_config.py   # ← 同上，从 scripts/ 复制
        └── ui/config + images/    # 应用中心里的入口与图标（图标需 make-icons.py 生成）
```

---

## 为什么缺两个目录

| 缺什么 | 去哪拿 | 为什么不在仓库里 |
|---|---|---|
| `cmd/common` | `tools/fetch-common.sh` | 这是飞牛应用的**生命周期框架**，不是本项目的代码。它出现在系统里已安装的第三方应用（如 `fancontrol`、`it87-kmod`）中，作者各不相同，无法确认许可，因此不随本仓库分发 |
| `app/driver-src/src/qnap8528.{c,h}` | `tools/fetch-driver.sh` | `qnap8528` 驱动版权归原作者，本仓库只提供**配置补丁**（`patch_qnap_config.py`），不重新分发驱动源码 |
| `ICON.PNG` / `app/ui/images/*.png` | `tools/make-icons.py` | 二进制文件，用脚本按需生成；`make-icons.py` 依赖 Pillow |
| `app/fan_webui.py`、`app/tools/patch_qnap_config.py` | `tools/sync-app.sh` | 与仓库根的 `app/fan_webui.py`、`scripts/patch_qnap_config.py` **完全同一份**，仓库里不重复存两份，打包前同步即可 |

补齐命令：

```bash
bash tools/sync-app.sh          # 先同步主程序与补丁脚本（源在仓库根）
./tools/fetch-common.sh          # 从本机已装的 fnOS 应用里取
./tools/fetch-driver.sh          # 从 /usr/src/qnap8528-* 取（挑版本最高的，优先取未打补丁的）
python3 tools/make-icons.py      # 生成 4 个图标文件
```

---

## 应用做了什么

安装时（`cmd/install_callback` → `cmd/service-setup` 的 `service_postinst`）：

1. **主板校验** —— 读 DMI，非 `iEi SA145` / `QuNAS-X05` 直接拒绝安装并说明原因。
   强行安装可设 `QU805_FORCE_INSTALL=1`，但 EC 不同可能写坏别人的寄存器。
2. **构建工具链检查** —— `dkms` / `gcc` / `make` / `linux-headers-$(uname -r)`；
   缺了先试 `apt-get` 补装，仍失败就给出明确的手动命令并**让安装失败**。
3. **部署驱动源码** 到 `/usr/src/qnap8528-<版本>`（已存在则先改名备份），
   再跑 `patch_qnap_config.py` 打上 `QU805 / SA145` 机型配置。
4. **DKMS 现场编译并装入当前内核** —— 全程离线，不下载任何外部包。
5. 写 `/etc/modprobe.d/qnap8528.conf`（`options qnap8528 skip_hw_check=true`）与
   `/etc/modules-load.d/qnap8528.conf`，并加载模块。
6. **接管已有手工安装** —— 见下。
7. Web 服务由飞牛生成的 systemd 单元拉起。

重启后由 systemd 拉起应用 → `service_prestart` 比对 `/etc/qu805-fancontrol-kver`
与当前内核：**内核变了就自动重编**，没变就秒过。这条比内核 postinst 钩子更可靠，
因为它不依赖飞牛是否走了标准内核钩子。

---

## 接管已有手工安装（可回滚）

若检测到下列痕迹，会**移走并备份**（不是直接删）：

| 旧痕迹 | 处理 |
|---|---|
| `/etc/kernel/postinst.d/zz-qnap8528` | 移动到备份目录 + 移除 |
| `/usr/local/sbin/qnap8528-load.sh` | 同上 |
| `/etc/systemd/system/qnap8528-load.service` | `systemctl disable` + 移动 |
| Docker 容器 `fnos-fan-webui` | `docker update --restart=no` + `docker stop` |

备份位置：`<应用数据目录>/legacy-backup-<时间戳>/`，里面同时生成 **`RESTORE.sh`**，
执行它即可把旧的手工安装原样放回（**前提是先停掉本应用**，否则两个写者会抢 `pwm1`）。

**为什么要接管**：旧容器和旧 systemd 单元同样会写 `pwm1`，两个写者互相覆盖会让风扇来回跳。

---

## 关键参数

| 项 | 值 | 说明 |
|---|---|---|
| 监听端口 | **8605** | 避开常见占用（8080 常被别的服务占、9887 是 `fancontrol` 的）|
| 数据目录 | `/vol1/@appdata/qu805-fancontrol/` | `curve-config.json` / `fan.log` / `history.tsv` |
| 服务日志 | 同上 `service.log` | 框架日志，记录安装与接管过程 |
| 内核标记 | `/etc/qu805-fancontrol-kver` | 记录上次编译的内核版本，用于升级自愈 |

端口与数据目录都能改：端口在 `manifest` 的 `service_port` 与 `app/ui/config` 的
`"port"` 两处（**两处必须一致**，参考应用都是写死字面值而不是 `{port}` 占位符）；
数据目录由 `cmd/service-setup` 里的 `SERVICE_COMMAND` 传给 `fan_webui.py --data-dir`。

---

## 怎么打包

`fnpack` 是飞牛自带的 **Linux x86-64 Go 二进制**（`/usr/local/bin/fnpack`），
**macOS / Windows 上跑不了**，所以流程是：本地清理 + 打包 → 送到 NAS 的 `/tmp`
→ `fnpack build` → 取回 `.fpk`。`tools/build_fpk.py` 把这几步封成一条命令：

```bash
# 凭证三选一：NAS_* 环境变量 / --cred 指 JSON / 默认找 .nas_cred.json
export NAS_HOST=192.168.0.10 NAS_USER=youruser NAS_PASSWORD='...'
python3 tools/build_fpk.py --project-dir ./qu805-fancontrol
```

脚本**只构建、不安装**：全程不碰 `/var/apps`、`/vol1/@app*`、systemd、DKMS 等任何
线上路径，只在 NAS 的 `/tmp/fpkbuild` 下建临时目录，用完删掉。跑完会：
自动清理 macOS 伴生文件、校验包内无 `._*`、打印包内条目数。

### 两个必须知道的坑

1. **macOS 的 `tar` 会混进 `._*` 伴生文件。**
   直接 `tar czf` 会给你每个文件配一个 AppleDouble 文件，一起打进 `.fpk`。
   必须 `COPYFILE_DISABLE=1 tar …`；**只加 `--no-xattrs` 不够**（它去 xattr，
   不去伴生文件）。`build_fpk.py` 已经在做这件事。
2. **顶层 `config/` 会被一并打进 `app.tgz`。**
   较新版本的 `fnpack` 会把项目根的 `config/` 也塞进应用载荷，
   于是 `TRIM_APPDEST` 下会多一份冗余的 `config/`。无害，知道就行。

打包完可以本地校验：

```bash
tar tzf dist/qu805-fancontrol.fpk | sort          # 看包内清单
tar tzf dist/qu805-fancontrol.fpk | grep -E '(^|/)\._|\.DS_Store'   # 应为空
```

---

## 安装 / 卸载

**安装**：飞牛「应用中心 → 手动安装」，选 `qu805-fancontrol.fpk`。

排查安装问题看两处日志：

```bash
sudo tail -50 /var/log/apps/qu805-fancontrol.log
sudo tail -50 /vol1/@appdata/qu805-fancontrol/service.log
```

**卸载**：在应用中心点卸载。`service_postuninst` 会卸模块 → `dkms remove --all`
→ 删 `/usr/src/qnap8528-*` → 删 modprobe / modules-load / 内核标记文件。

> ⚠️ **不要勾选「同时删除数据」**，否则 `legacy-backup-*/`（旧手工安装的备份）
> 会一并消失。

> qnap8528 的 hwmon 只暴露 `pwm1`、没有 `pwm1_enable`，无法把风扇「交还 EC 自动控制」。
> 所以停机 / 卸载时只做兜底：占空比低于 40% 才抬到 40%（正常运行时不会动）。

---

## 打包时踩过的坑（改这块前先看）

| 坑 | 现象 | 解法 |
|---|---|---|
| **`call_func` 会吃掉退出码** | `service_postinst` 里 `exit 1`，安装却显示成功 | `call_func` 的执行形式是 `eval fn 2>&1 \| install_log`，管道只返回末端退出码。`cmd/install_callback` 里**直接调用** `service_postinst \|\| exit 1`，不要走 `call_func` |
| **`SVC_QUIET` 未置位会截断日志** | 每次 `start` 用 `date > ${LOG_FILE}` 清空日志，安装期记录全丢 | `service-setup` 里设 `SVC_QUIET=y`（顺带 `SVC_NO_REDIRECT=y`），并把框架日志 `service.log` 与应用日志 `fan.log` 分开 |
| **`elog` 在数据目录还没建时报错** | `install_init` 阶段就调用，重定向失败留下一行刺眼的 bash 报错 | 先 `mkdir -p "$TRIM_PKGVAR"` 再写，并 `2>/dev/null \|\| true` |
| **`cmd/common` 必须用完整版** | 系统里有两个版本（约 6.7 KB 的精简版和约 14 KB 的完整版），精简版少了 **导出 PATH** 与**启动就绪门控** | 用完整版。飞牛给生命周期脚本的 `TRIM_*` 环境**不含 PATH**，派生的守护进程会因为 `exec: "getent": not found` 挂掉 |

---

## 许可

本目录下的脚本与配置与仓库其余部分一样，采用 [MIT 许可证](../LICENSE)。

`cmd/common`（需自行获取）与 `qnap8528` 驱动源码的版权归各自原作者，
本仓库不分发，只提供获取脚本与配置补丁。
