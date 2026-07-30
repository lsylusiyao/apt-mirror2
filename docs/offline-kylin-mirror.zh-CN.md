# 完全离线内网的麒麟 APT 镜像

这套流程把联网端和内网端严格分开：联网 Windows 主机通过 WSL 运行
`apt-mirror`，再由 `apt-mirror-offline` 生成可校验的增量包；内网 Linux
从硬盘、动态 VHDX 或多张光盘导入。联网端不会直接连接内网。

## 已核实的官方仓库信息

麒麟官方入口为：

```text
https://archive.kylinos.cn/kylin/KYLIN-ALL
```

官方页面列出的 suite 包括 `10.0`、`10.1`、`10.1-2107-updates`、
`10.1-2203-updates` 和 `10.1-2203-hwe-updates` 等。以
`10.1-2203-updates` 的 Release 元数据为例：

- 组件：`main restricted universe multiverse`
- 架构：`i386 amd64 arm64 armhf mips64el loongarch64 sw64`
- 支持 `Acquire-By-Hash`

不要盲目启用全部 suite。先在目标麒麟机器上运行以下命令，按其真实输出选择：

```bash
dpkg --print-architecture
grep -RhE '^(deb|Suites:|Architectures:)' /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null
```

常见对应关系是 x86-64 选 `amd64`，飞腾/鲲鹏 ARM64 选 `arm64`，龙芯新
世界选 `loongarch64`。仓库配置中的 `arch=` 决定只下载该架构的二进制包；
本项目仍会获取该 suite 必需的 Release 和索引文件。

## 一、准备联网 Windows/WSL

1. 安装 WSL2（示例发行版名为 `Ubuntu`），在 WSL 中安装 Python 3.10+
   和本项目及其依赖。不要尝试在原生 Windows Python 中运行主镜像程序；
   其锁和文件语义是 POSIX 的。内网端只使用标准库，可提前把本项目源码带入
   内网并执行 `python3 -m pip install --no-deps /path/to/apt-mirror2`。
2. 把 [mirror-amd64.list](../examples/kylin/mirror-amd64.list) 复制到
   WSL 的 `/etc/apt/mirror-kylin.list`，按目标系统修改唯一的架构和 suite。
3. 在 WSL 中执行一次小范围测试，确认配置正确后再开始大镜像：

```bash
sudo apt-mirror /etc/apt/mirror-kylin.list
```

示例启用了 `clean` 和 `_autoclean`。只有一次镜像同步完整成功后，
`apt-mirror` 才根据最新包索引推导已不再需要的文件。40% 的数量/容量阈值
会阻止异常的大规模清理。

## 二、硬盘与动态 VHDX

可以直接把增量包放在普通移动硬盘上。若希望使用“实际占用随内容增长”的
虚拟磁盘文件，请在管理员 PowerShell 中运行：

```powershell
.\scripts\windows\New-KylinOfflineVhdx.ps1 `
  -Path 'E:\kylin-transfer.vhdx' `
  -MaximumSizeBytes 2TB `
  -DriveLetter R
```

它创建 `type=expandable` 的动态 VHDX，初始文件很小，写入时自动扩展，
最大不超过指定上限；内部文件系统为 exFAT，便于 Windows 和 Linux 共同使用。
VHDX 文件所在的物理盘自身必须有足够可用空间。动态扩展不是无限扩容，也不能
突破物理盘容量。

在联网机同步官方源并导出增量包：

```powershell
.\scripts\windows\Export-KylinOfflineMirror.ps1 `
  -MediaRoot 'R:\' `
  -WslDistribution Ubuntu `
  -MirrorConfig '/etc/apt/mirror-kylin.list'
```

`/var/lib/apt-mirror-offline` 是联网机 WSL 内的持久状态，不能每轮删除。
它只在读到内网成功导入后生成的 `feedback/ack.json` 时推进增量基线；因此丢盘、
漏导入或导入失败不会静默跳过一批文件。

联网端为提高日常扫描速度，会按文件大小和纳秒级修改时间复用哈希。若还希望
定期排查联网端磁盘的静默位翻转，可在某一轮加 `-RehashSource` 强制重新读取
全部源文件；内网导入端则每轮都会做完整哈希，不使用这个缓存。

拔盘前先在 Windows 中卸载 VHDX：

```powershell
Dismount-DiskImage -ImagePath 'E:\kylin-transfer.vhdx'
```

内网 Linux 若直接读取普通分区，无需特殊操作。若读取 VHDX，安装 `qemu-utils`
和 exFAT 支持，再使用包装脚本；它会在命令结束后自动同步、卸载并断开 NBD：

```bash
sudo ./scripts/linux/with-kylin-offline-vhdx.sh \
  /media/physical-disk/kylin-transfer.vhdx /mnt/kylin-transfer -- \
  ./scripts/linux/import-kylin-offline.sh \
  /mnt/kylin-transfer/outgoing/bundle-20260730T120000 \
  /srv/apt-mirror \
  /mnt/kylin-transfer/feedback \
  prompt
```

默认 `prompt` 会列出上游删除项并要求输入 `DELETE`。无人值守运行时它退化为
只报告，退出码为 3；审核 `deletions-pending.json` 后，以 `apply` 重跑即可。
若删除比例达到 40%，还必须直接调用 CLI 并显式添加
`--allow-large-deletes`，防止上游元数据异常导致镜像被清空。

## 三、多张光盘

导出时指定单卷载荷上限。DVD 建议预留文件系统和控制文件空间，例如：

```powershell
.\scripts\windows\Export-KylinOfflineMirror.ps1 `
  -MediaRoot 'D:\kylin-transfer' `
  -VolumeSize '4300M'
```

输出目录的每个 `volumes\volume-NNNN` 都是自描述卷。把该目录的“内容”刻录
到一张盘（不是只刻录 `payload`）。内网逐张插盘并暂存：

```bash
sudo ./scripts/linux/stage-kylin-volume.sh /media/cdrom /var/tmp/kylin-staging
```

全部卷齐后，脚本会显示完整 bundle 路径，再执行导入：

```bash
sudo ./scripts/linux/import-kylin-offline.sh \
  /var/tmp/kylin-staging/BUNDLE_ID \
  /srv/apt-mirror \
  /var/tmp/kylin-feedback \
  prompt
```

光盘不可写，因此把内网产生的 `ack.json`、`repair-request.json` 或
`deletions-pending.json` 用另一张可写介质带回，并放进下一次导出介质的
`feedback` 目录。

## 四、增量、损坏检测与修复

每个离线 bundle 都包含：

- 目标镜像的完整文件清单（大小和 SHA-256）；
- 相对最近一次内网确认快照的新增/变化文件载荷；
- 从最新上游索引推导出的待删除文件；
- 每卷独立控制文件和校验值。

导入顺序是：先校验介质上的全部载荷，再原子替换变化文件，然后重新读取并
SHA-256 校验内网镜像中的每一个目标文件。最后一步会发现“不在本次增量包里，
但早先已经损坏”的文件。此时不写成功 ACK，而是写出 `repair-request.json`；
把它带回联网端后，下一次导出会强制重新携带这些文件。

也可以定期单独巡检：

```bash
sudo apt-mirror-offline verify /srv/apt-mirror --feedback-dir /media/usb/feedback
```

SHA-256 能检测介质和存储的意外损坏，但不能抵抗能够同时篡改载荷与清单的主动
攻击。对抗此类威胁时还应强制验证麒麟 Release 签名，或在组织内对离线 bundle
另行数字签名。

## 五、给内网客户端提供服务

示例导出的是 `/var/spool/apt-mirror/mirror`，因此内网目录会保留
`archive.kylinos.cn/kylin/KYLIN-ALL` 层级。让 nginx/Apache 的文档根指向
`/srv/apt-mirror` 后，客户端源可写为：

```text
deb http://apt-mirror.intra/archive.kylinos.cn/kylin/KYLIN-ALL 10.1-2203-updates main restricted universe multiverse
```

替换主机名、suite 和架构相关配置后，先在一台测试客户端执行 `apt update`，
再推广到生产内网。
