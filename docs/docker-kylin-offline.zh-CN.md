# 用 Docker 运行完全离线的麒麟 APT 镜像站

本镜像同时提供：

- nginx HTTP 镜像站；
- `apt-mirror` 和 `apt-mirror-offline` 命令；
- `scripts/linux` 中的导出、导入、分卷暂存、完整校验、VHDX 创建和临时挂载脚本；
- 示例麒麟配置 `/etc/apt/mirror-kylin.list`；
- 仓库源码、Windows 脚本和本文档，统一放在容器的 `/opt/apt-mirror2`。

容器是 Linux 容器，因此能直接执行的是 `scripts/linux`。`scripts/windows` 会被
一并收进镜像供查阅，但 PowerShell/Windows 磁盘操作必须在 Windows 宿主机执行。
完全离线时，除在线 `apt-mirror` 同步外，其余 Linux 离线流程都不需要网络。

## 一、构建并搬入离线环境

在仓库根目录构建。Dockerfile 必须使用仓库根作为构建上下文：

```bash
docker build \
  -f docker/kylin-offline/Dockerfile \
  -t kylin-offline-mirror:local \
  .
```

构建阶段需要从 Debian/PyPI 下载依赖。构建完成后，镜像本身已经包含运行依赖，
可用 `docker save/load` 搬到无网络的 Docker 主机：

```bash
docker save -o kylin-offline-mirror.tar kylin-offline-mirror:local
# 将 tar 文件带入内网后：
docker load -i kylin-offline-mirror.tar
```

镜像的默认命令是启动 nginx，默认监听容器 `80` 端口。nginx 直接发布镜像根，
客户端 URL 延用包含 `archive.kylinos.cn` 的原有层级。建议先采用下一节的目录
映射；它不需要给容器内核级权限，也适用于 Docker Desktop。

## 二、推荐方式：宿主机挂载磁盘，容器映射目录

假设宿主机上的最终镜像根为 `/srv/apt-mirror`，其中直接包含
`archive.kylinos.cn/kylin/KYLIN-ALL`；传输介质挂在 `/media/kylin-transfer`：

```bash
docker run -d \
  --name kylin-mirror \
  --restart unless-stopped \
  -p 8080:80 \
  --mount type=bind,src=/srv/apt-mirror,dst=/srv/apt-mirror \
  --mount type=bind,src=/media/kylin-transfer,dst=/mnt/kylin-transfer \
  kylin-offline-mirror:local
```

查看状态和 HTTP 健康检查：

```bash
docker ps --filter name=kylin-mirror
curl -f http://127.0.0.1:8080/__health
```

如果仅发布、不再导入，可给镜像目录的 `--mount` 增加 `readonly`。需要导入增量
时必须以可写方式挂载并重建容器。传输目录若需要写 ACK、修复请求或暂存分卷，
也必须可写。

若宿主机上挂载的是 VHDX：Linux 可先用
`scripts/linux/with-kylin-offline-vhdx.sh` 或系统工具挂载其第一分区，再把挂载后的
目录传给 Docker；Windows 可先用 `Mount-DiskImage` 挂载 VHDX，再通过 Docker
Desktop 共享得到的目录。这也是 Docker Desktop/WSL 环境的推荐方法。

## 三、按文件映射 VHDX，并在 Linux 容器内挂载

这一方式满足把宿主机 `.vhdx` 文件用 `--mount` 映射到容器 `/` 下的需求，但
依赖 Linux 宿主机的 NBD 内核模块和特权容器。Docker Desktop 的 Linux VM 未必
提供 NBD；不支持时请使用上一节的目录映射。

先在 Linux Docker 宿主机加载 NBD。两个 VHDX 需要两个不同设备：

```bash
sudo modprobe nbd max_part=16
test -b /dev/nbd0 && test -b /dev/nbd1
```

以下命令把目标数据盘和传输盘分别放到容器根目录
`/kylin-target.vhdx`、`/kylin-transfer.vhdx`。入口脚本把它们挂到
`/mnt/kylin-mirror`、`/mnt/kylin-transfer`。nginx 发布目标盘内的 `mirror`
目录，客户端通过 URL 中的 `archive.kylinos.cn` 层级访问其内容：

```bash
docker run -d \
  --name kylin-mirror \
  --restart unless-stopped \
  --privileged \
  -p 8080:80 \
  --mount type=bind,src=/absolute/disks/kylin-target.vhdx,dst=/kylin-target.vhdx \
  --mount type=bind,src=/absolute/inbox/kylin-transfer.vhdx,dst=/kylin-transfer.vhdx \
  -e KYLIN_MIRROR_VHDX=/kylin-target.vhdx \
  -e KYLIN_TRANSFER_VHDX=/kylin-transfer.vhdx \
  kylin-offline-mirror:local
```

所有 `src` 都要使用宿主机绝对路径，文件必须在启动前存在。如果目标盘内的镜像
实际位于 `apt-mirror/mirror`，增加：

```text
-e KYLIN_MIRROR_SUBDIR=apt-mirror/mirror
```

若“外网更新过的 VHDX”本身已经包含完整、可发布的镜像，把它作为
`KYLIN_MIRROR_VHDX` 即可；若它只负责携带 `outgoing/bundle-*` 和 `feedback`，
则把它作为 `KYLIN_TRANSFER_VHDX`，再执行下一节的导入。

只挂目标 VHDX 时，省略传输盘的 `--mount` 和 `KYLIN_TRANSFER_VHDX`。只读发布
目标盘时增加 `-e KYLIN_MIRROR_READ_ONLY=1`；需要导入时不要设置它。传输盘也可
用 `KYLIN_TRANSFER_READ_ONLY=1` 只读挂载，但这会阻止写入反馈文件。

可调整的高级参数如下：

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `KYLIN_MIRROR_NBD` | `/dev/nbd0` | 目标 VHDX 的 NBD 设备 |
| `KYLIN_MIRROR_PARTITION` | `/dev/nbd0p2` | 目标 VHDX 的分区设备路径；也兼容填写分区号（如 `1`） |
| `KYLIN_MIRROR_MOUNT` | `/mnt/kylin-mirror` | 目标 VHDX 挂载点 |
| `KYLIN_MIRROR_SUBDIR` | `mirror` | 挂载文件系统内的镜像相对路径 |
| `KYLIN_TRANSFER_NBD` | `/dev/nbd1` | 传输 VHDX 的 NBD 设备 |
| `KYLIN_TRANSFER_PARTITION` | `/dev/nbd1p1` | 传输 VHDX 的分区设备路径；也兼容填写分区号 |
| `KYLIN_TRANSFER_MOUNT` | `/mnt/kylin-transfer` | 传输 VHDX 挂载点 |

如果宿主机分配的 NBD 设备不同，需要同时设置设备和对应的分区路径。例如使用
`/dev/nbd2` 的第二分区时，增加 `-e KYLIN_MIRROR_NBD=/dev/nbd2` 和
`-e KYLIN_MIRROR_PARTITION=/dev/nbd2p2`。两项必须指向同一个 NBD 设备。

部分 Docker 宿主机虽然能在 `lsblk` 中看到分区，却不会在容器的 `/dev` 下创建分区
节点。入口脚本会在连接 VHDX 后执行 `partprobe` 和 `blockdev --rereadpt`，并在需要
时根据 `/sys/class/block` 中的主次设备号用 `mknod` 创建分区节点；容器需要使用
`--privileged` 才能完成这一步。

不要让同一个 VHDX 同时被宿主机、另一个容器或虚拟机以读写方式挂载。正常使用
`docker stop -t 30 kylin-mirror` 停止容器；入口脚本会先同步、卸载文件系统并断开
NBD。不要用 `docker kill -s KILL`，它没有机会清理挂载。

## 四、在运行中的容器执行离线功能

以下命令同时适用于两种运行方式。目录方式的目标根是 `/srv/apt-mirror`；VHDX
方式则把下例中的目标根换成 `/mnt/kylin-mirror/mirror`。

需要浏览或调用仓库中的其他内容时可进入容器；Linux 脚本目录已加入 `PATH`：

```bash
docker exec -it kylin-mirror bash
cd /opt/apt-mirror2
ls scripts/linux docs
```

### 导入联网端带回的增量

先查看实际 bundle 名称：

```bash
docker exec kylin-mirror \
  find /mnt/kylin-transfer/outgoing -maxdepth 1 -type d -name 'bundle-*' -print
```

交互审核上游删除项并导入：

```bash
docker exec -it kylin-mirror \
  import-kylin-offline.sh \
  /mnt/kylin-transfer/outgoing/bundle-20260818T120000 \
  /srv/apt-mirror \
  /mnt/kylin-transfer/feedback \
  prompt
```

退出码 `3` 表示删除仍待审核。查看
`/mnt/kylin-transfer/feedback/deletions-pending.json` 后，确认接受才把最后一个参数
改为 `apply` 重跑。达到 40% 大删除保护线时，必须绕过包装脚本显式确认：

```bash
docker exec -it kylin-mirror \
  apt-mirror-offline import \
  /mnt/kylin-transfer/outgoing/bundle-20260818T120000 \
  /srv/apt-mirror \
  --feedback-dir /mnt/kylin-transfer/feedback \
  --delete-policy apply \
  --allow-large-deletes
```

### 全量校验和修复请求

```bash
docker exec kylin-mirror \
  test-kylin-offline-mirror.sh \
  /srv/apt-mirror \
  /mnt/kylin-transfer/feedback
```

校验发现损坏时会生成 `repair-request.json`，将反馈目录带回联网端即可在下一轮
强制重新携带损坏文件。

### 分卷/光盘暂存

创建容器时，把宿主机的光盘/卷挂载点额外映射为容器
`/mnt/current-volume`。每次换盘并重新建立该映射后运行：

```bash
docker exec kylin-mirror \
  stage-kylin-volume.sh \
  /mnt/current-volume \
  /mnt/kylin-transfer/staging
```

卷齐后脚本会给出完整 bundle 路径，再按前述方式导入。

### 不联网地从现有镜像生成 bundle

`--skip-online-sync` 不访问外网，可在离线容器中使用：

```bash
docker exec kylin-mirror \
  export-kylin-offline-mirror.sh \
  --skip-online-sync \
  --mirror-root /srv/apt-mirror \
  --state-dir /mnt/kylin-transfer/export-state \
  --media-root /mnt/kylin-transfer
```

`export-state` 必须持久保留，只有读到内网成功 ACK 后才推进增量基线。

### 新建动态 VHDX

此操作也需要 NBD 和特权模式。把一个宿主机目录映射进一次性容器，VHDX 会在该
目录中创建：

```bash
sudo modprobe nbd max_part=16
docker run --rm -it \
  --privileged \
  --mount type=bind,src=/absolute/disks,dst=/disks \
  --entrypoint new-kylin-offline-vhdx.sh \
  kylin-offline-mirror:local \
  --path /disks/new-kylin.vhdx \
  --maximum-size 2T \
  --nbd-device /dev/nbd0
```

不要在主服务容器占用同一个 NBD 设备时执行此命令。

## 五、麒麟客户端配置

nginx 默认直接发布最终镜像根，且拒绝访问以点开头的内部状态/断点目录。假设
Docker 宿主机内网地址为 `192.168.10.20`、端口映射为 `8080`，客户端源延用
包含 `archive.kylinos.cn` 的原有层级：

```text
deb http://192.168.10.20:8080/archive.kylinos.cn/kylin/KYLIN-ALL 10.1-2203-updates main restricted universe multiverse
```

suite 和架构要与实际麒麟系统一致。先执行：

```bash
sudo apt update
```

如果出现 `404`，先在容器中确认以下文件层级，而不是修改 nginx URL：

```bash
docker exec kylin-mirror \
  find /var/www/kylin-mirror/archive.kylinos.cn/kylin/KYLIN-ALL/dists \
  -maxdepth 2 -name Release -print
```

详细的联网端同步、ACK 基线、删除审核、修复反馈和多盘流程见
[完全离线内网的麒麟 APT 镜像](offline-kylin-mirror.zh-CN.md)。
