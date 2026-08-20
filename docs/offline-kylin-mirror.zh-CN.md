# 完全离线内网的麒麟 APT 镜像

这套流程把联网端和内网端严格分开。联网端可以是 Windows（通过 WSL）或
Linux，内网端也可以是 Windows 或 Linux；两端只通过硬盘、动态 VHDX/VMDK
或多张光盘交换可校验的增量包，联网端不会直接连接内网。

| 联网端 | 内网端 | 联网端脚本 | 内网端脚本 |
| --- | --- | --- | --- |
| Windows/WSL | Windows | `Export-KylinOfflineMirror.ps1` | `Import-KylinOfflineMirror.ps1` |
| Windows/WSL | Linux | `Export-KylinOfflineMirror.ps1` | `import-kylin-offline.sh` |
| Linux | Windows | `export-kylin-offline-mirror.sh` | `Import-KylinOfflineMirror.ps1` |
| Linux | Linux | `export-kylin-offline-mirror.sh` | `import-kylin-offline.sh` |

同步协议与主机操作系统无关：联网端生成 bundle，内网端导入并校验，然后把
`feedback/ack.json` 或修复请求带回。Windows 联网端需要 WSL，是因为在线镜像
程序依赖 POSIX 文件和锁语义；Windows 内网端的导入、暂存和巡检脚本则可直接
使用原生 Python 3。

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

## 一、共同准备与架构选择

1. 在联网 Linux 或 Windows 的 WSL2 中安装 Python 3.10+、本项目及在线同步
   依赖。Windows 示例的 WSL 发行版名为 `Ubuntu`。更新本仓库代码后要重新安装
   当前 checkout；导出脚本会检查 WSL/Linux 的 Python 实际加载了支持续传的版本，
   避免误调用系统中的旧 `apt-mirror`。
2. 把 [mirror-amd64.list](../examples/kylin/mirror-amd64.list) 复制为
   `/etc/apt/mirror-kylin.list`，按目标系统修改唯一的架构和 suite。
3. 先只配置一个 suite 和一种架构做小范围验证，再开始正式同步：

```bash
sudo apt-mirror /etc/apt/mirror-kylin.list
```

示例启用了 `clean` 和 `_autoclean`。只有一次镜像同步完整成功后，
`apt-mirror` 才根据最新包索引推导已不再需要的文件。40% 的数量/容量阈值
会阻止异常的大规模清理。

内网端只使用 Python 标准库，可以提前把源码带入并离线安装：Linux 执行
`python3 -m pip install --no-deps /path/to/apt-mirror2`，Windows 执行
`py -3 -m pip install --no-deps 'D:\software\apt-mirror2'`。

## 二、联网端同步并导出

### Windows/WSL 联网端

普通目录或移动介质已挂载为 `R:` 时执行：

```powershell
.\scripts\windows\Export-KylinOfflineMirror.ps1 `
  -MediaRoot 'R:\' `
  -WslDistribution Ubuntu `
  -MirrorConfig '/etc/apt/mirror-kylin.list'
```

`/var/lib/apt-mirror-offline` 是联网机 WSL 内的持久状态，不能每轮删除。
它只在读到内网成功导入后生成的 `feedback/ack.json` 时推进增量基线；因此丢盘、
漏导入或导入失败不会静默跳过一批文件。

若在线镜像本身存放在已挂载为 `S:` 的源 VHDX 中，配置文件必须把
`base_path` 指向该盘在 WSL 中的路径，例如：

```text
set base_path   /mnt/s/apt-mirror
set mirror_path $base_path/mirror
set skel_path   $base_path/skel
set var_path    $base_path/var
```

然后显式传入同一镜像根目录；此处 `T:` 是另一个传输介质：

```powershell
.\scripts\windows\Export-KylinOfflineMirror.ps1 `
  -MirrorRoot 'S:\apt-mirror\mirror' `
  -StateDirectory 'S:\apt-mirror-offline-state' `
  -MediaRoot 'T:\'
```

PowerShell 脚本会把 Windows 盘符路径转换成 WSL 路径。`MirrorRoot` 必须和
配置中 `mirror_path` 的实际位置相同，否则会导出错误的目录。

### Linux 联网端

Linux 可以直接完成同一流程：

```bash
sudo ./scripts/linux/export-kylin-offline-mirror.sh \
  --config /etc/apt/mirror-kylin.list \
  --mirror-root /var/spool/apt-mirror/mirror \
  --media-root /media/kylin-transfer
```

如在线镜像存放在源 VHDX，先让配置中的 `base_path` 指向
`/mnt/kylin-source/apt-mirror`，再用独立 NBD 设备挂载后导出：

```bash
sudo ./scripts/linux/with-kylin-offline-vhdx.sh \
  /media/disk/kylin-source.vhdx /mnt/kylin-source /dev/nbd0 -- \
  ./scripts/linux/export-kylin-offline-mirror.sh \
  --config /etc/apt/mirror-kylin.list \
  --mirror-root /mnt/kylin-source/apt-mirror/mirror \
  --state-dir /mnt/kylin-source/apt-mirror-offline-state \
  --media-root /media/kylin-transfer
```

两种联网端都支持 `-SkipOnlineSync`/`--skip-online-sync`，用现有小镜像测试导出
而不访问麒麟站；支持 `-VolumeSize 4300M`/`--volume-size 4300M` 分卷。
联网端日常会按大小和纳秒级修改时间复用哈希。定期使用
`-RehashSource`/`--rehash-source` 可强制重读源镜像，排查源盘静默位翻转；
内网导入端每轮始终做完整 SHA-256 校验。

如果只需预先计算或刷新源镜像的 SHA-256 缓存，不创建 `outgoing` 数据，Linux
使用 `--hash-only`，Windows 使用 `-HashOnly`。此模式不要求 `--media-root`/
`-MediaRoot`；可与 `--skip-online-sync`/`-SkipOnlineSync` 组合，跳过在线同步：

```bash
sudo ./scripts/linux/export-kylin-offline-mirror.sh \
  --skip-online-sync \
  --hash-only \
  --mirror-root /var/spool/apt-mirror/mirror \
  --state-dir /var/lib/apt-mirror-offline
```

Windows/WSL 对应命令：

```powershell
.\scripts\windows\Export-KylinOfflineMirror.ps1 `
  -SkipOnlineSync `
  -HashOnly `
  -MirrorRoot '/var/spool/apt-mirror/mirror' `
  -StateDirectory '/var/lib/apt-mirror-offline'
```

哈希结果保存在状态目录的 `hash-cache.json`，后续正常导出会复用它。

### 下载中断与断点续传

上述 PowerShell 和 Bash 导出脚本都是可重入的最终入口。网络错误、速度过慢或
按 `Ctrl+C` 中止时，脚本不会创建离线 bundle，也不会删除已经完成的文件；未
完成的 HTTP 文件保存在各仓库根下的 `.apt-mirror2-partial` 隐藏目录。使用
**完全相同的配置、`MirrorRoot`/`--mirror-root` 和磁盘挂载位置**重新运行原命令
即可继续，不要删除 `mirror_path`、`skel_path`、`var_path`，也不要在尚未完成时
使用 `-SkipOnlineSync`/`--skip-online-sync`。

恢复请求使用标准 HTTP `Range: bytes=N-`。服务器返回 `206` 时从已有字节追加；
若服务器忽略 Range 并返回 `200`，只会从头重下当前文件，之前已经完成的其他
文件仍然复用。没有可信预期大小的易变 Release 文件会安全地从头下载，通常很小；
有 Release 大小和哈希的索引、`.deb` 等大文件可真正续传。文件完成后会验证总
大小及配置启用的仓库哈希，再原子替换正式文件；错误内容不会覆盖现有可用文件。
断点目录不会进入离线 bundle。

## 三、传输硬盘与动态 VHDX

可以直接把增量包放在普通移动硬盘上。若希望使用“实际占用随内容增长”的
虚拟磁盘文件，请在管理员 PowerShell 中运行：

```powershell
.\scripts\windows\New-KylinOfflineVhdx.ps1 `
  -Path 'E:\kylin-transfer.vhdx' `
  -MaximumSizeBytes 2TB `
  -DriveLetter R
```

Linux 安装 `qemu-utils`、`parted` 和 `exfatprogs` 后也可以创建同样用途的磁盘：

```bash
sudo ./scripts/linux/new-kylin-offline-vhdx.sh \
  --path /media/physical-disk/kylin-transfer.vhdx \
  --maximum-size 2T \
  --nbd-device /dev/nbd0
```

两者都创建动态 VHDX，初始文件很小，写入时自动扩展，
最大不超过指定上限；内部文件系统为 exFAT，便于 Windows 和 Linux 共同使用。
VHDX 文件所在的物理盘自身必须有足够可用空间。动态扩展不是无限扩容，也不能
突破物理盘容量。Linux 创建脚本会拒绝覆盖现有路径，初始化完成后自动断开 NBD。

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

## 四、内网 Windows 与 VHDX/VMDK

内网 Windows 安装 Python 3.10+ 后，只需离线安装本项目本身；导入功能只使用
Python 标准库：

```powershell
py -3 -m pip install --no-deps 'D:\software\apt-mirror2'
```

介质已经挂载为盘符时，可以直接使用与 Linux Bash 脚本等效的 PowerShell：

```powershell
# 导入并在需要时交互确认删除
.\scripts\windows\Import-KylinOfflineMirror.ps1 `
  -Bundle 'R:\outgoing\bundle-20260730T120000' `
  -MirrorRoot 'D:\apt-mirror' `
  -FeedbackDirectory 'R:\feedback' `
  -DeletePolicy prompt

# 定期全量 SHA-256 巡检
.\scripts\windows\Test-KylinOfflineMirror.ps1 `
  -MirrorRoot 'D:\apt-mirror' `
  -FeedbackDirectory 'R:\feedback'
```

前文 `New-KylinOfflineVhdx.ps1` 创建的是动态 **VHDX**，不是 VMDK。Windows
可以原生挂载 VHD/VHDX。以下包装器会挂载镜像、执行导入子进程，并在成功或失败
后卸载。`{MEDIA_ROOT}` 会替换为实际盘符根目录：

```powershell
$importCommand = @(
  'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
  (Resolve-Path '.\scripts\windows\Import-KylinOfflineMirror.ps1').Path,
  '{MEDIA_ROOT}outgoing\bundle-20260730T120000',
  'D:\apt-mirror',
  '{MEDIA_ROOT}feedback',
  'prompt'
)

.\scripts\windows\Invoke-WithKylinOfflineDisk.ps1 `
  -ImagePath 'E:\kylin-transfer.vhdx' `
  -DriveLetter R `
  -Command $importCommand
```

Windows 本身不能原生挂载真正的 `.vmdk`。若介质确实是 VMDK，需要安装包含
`vmware-mount.exe` 的 VMware Virtual Disk Development Kit/DiskMount，再执行：

```powershell
.\scripts\windows\Invoke-WithKylinOfflineDisk.ps1 `
  -ImagePath 'E:\kylin-transfer.vmdk' `
  -DriveLetter R `
  -VmdkVolume 1 `
  -VmwareMountPath 'C:\Program Files (x86)\VMware\VMware Virtual Disk Development Kit\bin\vmware-mount.exe' `
  -Command $importCommand
```

VMDK 必须包含 Windows 能识别的文件系统；不要同时把同一个 VMDK 连接给虚拟机，
也不要绕过快照链直接写入某个增量 extent。包装器按读写模式挂载，以便把 ACK
和修复请求写回 `feedback`。

## 五、源 VHDX 与目标 VHDX 如何同步

可以用现有方法同步两个 VHDX 中的镜像内容，但同步对象是**挂载后的文件树**，
不是两个 `.vhdx` 文件的原始块：

1. 联网端挂载源 VHDX，运行在线镜像并生成经 SHA-256 校验的增量 bundle。
2. 卸载源 VHDX，把 bundle 通过普通硬盘、光盘或另一个传输 VHDX 带入内网。
3. 内网端挂载目标 VHDX，导入 bundle，复核删除列表并完整校验目标文件树。
4. 卸载目标 VHDX，把 `ack.json` 或 `repair-request.json` 带回联网端。

这样源、目标 VHDX 可以一直留在各自安全域。两者的分区布局、空闲块和动态扩展
状态可以不同，只要镜像根目录内容一致即可。不要用二进制比较、块复制或同时
读写同一个 VHDX 来替代上述流程：它们不能利用当前文件级基线，也不能安全表达
经过审核的上游删除。任何时候都不能把同一个 VHDX 以读写方式同时挂到两台主机
或两个虚拟机。

如果传输介质本身也是 VHDX，它是源盘和目标盘之外的**第三个传输 VHDX**。
Linux 同时挂载两个镜像时必须使用不同 NBD 设备。以下示例在内网把传输 VHDX
挂为 `/dev/nbd0`，把目标 VHDX 挂为 `/dev/nbd1`：

```bash
sudo ./scripts/linux/with-kylin-offline-vhdx.sh \
  /media/inbox/kylin-transfer.vhdx /mnt/kylin-transfer /dev/nbd0 -- \
  ./scripts/linux/with-kylin-offline-vhdx.sh \
  /srv/disks/kylin-target.vhdx /mnt/kylin-target /dev/nbd1 -- \
  ./scripts/linux/import-kylin-offline.sh \
  /mnt/kylin-transfer/outgoing/bundle-20260730T120000 \
  /mnt/kylin-target/mirror \
  /mnt/kylin-transfer/feedback \
  prompt
```

在联网 Linux 上，如果源盘和传输盘也都是 VHDX，同样分别使用两个 NBD：

```bash
sudo ./scripts/linux/with-kylin-offline-vhdx.sh \
  /srv/disks/kylin-source.vhdx /mnt/kylin-source /dev/nbd0 -- \
  ./scripts/linux/with-kylin-offline-vhdx.sh \
  /media/outbox/kylin-transfer.vhdx /mnt/kylin-transfer /dev/nbd1 -- \
  ./scripts/linux/export-kylin-offline-mirror.sh \
  --config /etc/apt/mirror-kylin.list \
  --mirror-root /mnt/kylin-source/apt-mirror/mirror \
  --state-dir /mnt/kylin-source/apt-mirror-offline-state \
  --media-root /mnt/kylin-transfer
```

Windows 可以同时原生挂载多个不同的 VHDX。联网 Windows 使用上一节的 `S:`
源盘和 `T:` 传输盘示例；内网 Windows 可先把传输盘挂为 `T:`，再由包装器把
目标盘临时挂为 `S:`：

```powershell
$importCommand = @(
  'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
  (Resolve-Path '.\scripts\windows\Import-KylinOfflineMirror.ps1').Path,
  'T:\outgoing\bundle-20260730T120000',
  '{MEDIA_ROOT}mirror',
  'T:\feedback',
  'prompt'
)

.\scripts\windows\Invoke-WithKylinOfflineDisk.ps1 `
  -ImagePath 'D:\virtual-disks\kylin-target.vhdx' `
  -DriveLetter S `
  -Command $importCommand
```

若把整个源 VHDX 文件复制到传输盘，每轮复制量仍可能接近该 VHDX 的当前物理
大小；动态 VHDX 的“按需增长”不等于跨硬盘的增量复制。要获得本项目的增量、
损坏修复和删除审核能力，应只搬运 `outgoing/bundle-*` 与 `feedback`。

## 六、多张光盘

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

内网 Windows 则逐张执行：

```powershell
.\scripts\windows\Stage-KylinOfflineVolume.ps1 `
  -MountedDiscOrVolume 'E:\' `
  -StagingDirectory 'D:\kylin-staging'
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

## 七、增量、损坏检测与修复

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
sudo ./scripts/linux/test-kylin-offline-mirror.sh \
  /srv/apt-mirror /media/usb/feedback
```

Windows 对应命令见第四节的 `Test-KylinOfflineMirror.ps1`。

SHA-256 能检测介质和存储的意外损坏，但不能抵抗能够同时篡改载荷与清单的主动
攻击。对抗此类威胁时还应强制验证麒麟 Release 签名，或在组织内对离线 bundle
另行数字签名。

## 八、给内网客户端提供服务

示例导出的是 `/var/spool/apt-mirror/mirror`，因此内网目录会保留
`archive.kylinos.cn/kylin/KYLIN-ALL` 层级。为了不在客户端源中重复上游域名，
让 nginx/Apache 的文档根指向 `/srv/apt-mirror/archive.kylinos.cn`，客户端源写为：

```text
deb http://apt-mirror.intra/kylin/KYLIN-ALL 10.1-2203-updates main restricted universe multiverse
```

替换主机名、suite 和架构相关配置后，先在一台测试客户端执行 `apt update`，
再推广到生产内网。

若希望把 nginx、离线导入/校验工具和 VHDX 支持封装为一个可搬入内网的 Docker
镜像，请参阅 [Docker 完全离线部署指南](docker-kylin-offline.zh-CN.md)。该方案
既支持把宿主机已挂载的目录映射给容器，也支持在 Linux 特权容器内直接挂载映射
到容器根目录的 `.vhdx` 文件；其 nginx 默认隐藏磁盘中的
`archive.kylinos.cn` 目录层级，客户端源可直接使用 `/kylin/KYLIN-ALL`。
