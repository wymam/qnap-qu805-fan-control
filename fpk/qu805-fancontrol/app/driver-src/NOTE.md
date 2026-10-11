# 驱动源码（qnap8528 1.21）

本目录是 **上游原始源码**（未打补丁），安装时由 `../tools/patch_qnap_config.py`
现场补上 QU805 / SA145 机型配置项。**不要在这里预先改源码**，否则补丁脚本的
幂等判断会失去意义、也无法确认到底改了什么。

| 文件 | 说明 |
|---|---|
| `dkms.conf` | PACKAGE_NAME=qnap8528 / PACKAGE_VERSION=1.21，DKMS 用 |
| `Makefile` | 上游顶层 Makefile |
| `src/Makefile` | 内核模块 Makefile（`make -C src/ KERNELDIR=...`） |
| `src/qnap8528.c` | 驱动主体 |
| `src/qnap8528.h` | 含机型配置表 `qnap8528_configs[]`，**补丁就打在它里面** |

## 为什么必须打这个补丁

qnap8528 驱动内部有一张「主板型号 → 风扇 / 槽位 / LED 能力」表，只认登记过的机型。
QU805 用的 iEi SA145 不在表里，于是 `modprobe` 直接失败：

```
qnap8528 @ qnap8528_find_config: Could not find configuration for device
qnap8528: probe with driver qnap8528 failed with error -524
```

驱动里的匹配是**子串匹配**（`strstr(mb_model, config.mb_model)`），所以只要往表里
加一条 `"QU805", "SA145"` 就能命中形如 `MB=70006SA14500xxxxRS` 的主板串。
补丁脚本会在配置表末尾的 `{ NULL }` 终止符**之前**插入一个配平的配置块，
插入前后都做自检（花括号数量、终止符是否还在），失败即中止且不落盘。

## 打补丁后的验证

```sh
grep -n '"QU805", "SA145"' /usr/src/qnap8528-1.21/src/qnap8528.h
```

打完补丁还需要重编译，这一步由应用的 `cmd/service-setup` 自动完成：

```sh
dkms install qnap8528/1.21 -k "$(uname -r)" --force
modprobe qnap8528 skip_hw_check=true
```

`skip_hw_check=true` 是必需的 —— 本机 EC 的硬件 ID 校验过不了，不加会 `-524`。
