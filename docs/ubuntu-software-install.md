# Ubuntu 22.04 工控机：软件接入步骤

这份指南用于软件包审阅完成后的接入。已有 Home Assistant 继续使用；此包包含 Moqi 源码、HA 自定义集成、依赖锁、配置和检查工具，不包含 HA 镜像、模型权重或安装好的环境。首次构建依赖需要联网。模型由获授权的服务器准备，再把导出的文件夹传到工控机。

## 1. 上传并校验源码包

在 Windows 用 WinSCP/Samba 把整个文件夹传到 Ubuntu 家目录，保持子目录完整。打开 Ubuntu 终端，进入上传后的文件夹。以下 `moqi-software-2026-10-22` 换成实际文件夹名：

```sh
cd ~/moqi-software-2026-10-22
sha256sum -c SHA256SUMS.txt
uname -m
docker compose version
```

校验项目应全部显示 `OK`，架构应是 `x86_64`。若校验失败，重新传对应文件；若 Docker/Compose 命令不存在，先完成它们的安装。下面所有命令都在项目文件夹中执行。若 Docker 提示权限不足，在相应 `docker` 命令前加 `sudo`，例如 `sudo docker ps`。

## 2. 准备本地配置

```sh
bash deploy/prepare-itx.sh
cp deploy/runtime.env.example deploy/runtime.env
nano config.local.json
```

HA 也在这台 Ubuntu 上时，把 `ha_url` 改为 `http://127.0.0.1:8123`。确认 `timezone` 是家庭所在地时区，例如 `Asia/Shanghai`。示例实体只供独立开发接口使用；新运行器从 HA 读取实际选择，无需在此逐个填写设备名单。

首轮保持 `reflection.enabled` 和 `jev_policy.enabled` 为 `false`。月费用参数已经是 Vincent 确认的 `monthly_budget_usd: 1`。编辑完成按 `Ctrl+O`、回车保存，`Ctrl+X` 退出。

## 3. 在已有 HA 安装 Moqi 集成

先找到已有 HA 容器和配置目录：

```sh
docker ps --format 'table {{.Names}}\t{{.Image}}'
docker inspect HA容器名 --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'
```

`HA容器名` 换成上一步看到的实际名称。找到右侧是 `/config` 的一行，左侧 Source 就是实际 HA 配置目录。下面也要替换路径和容器名：

```sh
HA_CONFIG_DIR='/这里填刚找到的HA配置目录'
sudo mkdir -p "$HA_CONFIG_DIR/custom_components"
sudo cp -a custom_components/moqi "$HA_CONFIG_DIR/custom_components/"
docker restart HA容器名
```

如果已有 `custom_components/moqi`，先备份该文件夹再替换。然后在 HA 的“设置 → 设备与服务 → 添加集成”搜索 **Moqi**。集成当前未在真实 HA 中安装验证；若搜索不到或安装报错，查看该 HA 容器日志并保留错误，不能继续当作安装已成功。

在 HA 实体设置中指定所属区域，并选择集成创建的标签：

- **默契：允许观察**：要采集的实体。
- **默契：允许控制**：允许 Moqi 自动操作的实体，同时允许观察。
- **默契：灯具**：已核实确实接灯的 switch 插座；普通 light 实体自动识别为灯具。
- **默契：室外温度**：已核实的室外温度实体。
- **默契：可靠在场**：经过核实的连续在场信号。普通 PIR 不因为 OFF 就变成“无人”。
- **默契：无窗**：房间确实无窗时，标记对应空调实体。

实体标签决定范围，设备/区域标签不继承权限。保持 **Moqi automatic execution** 开关关闭，先完成现场只读、归因和设备反馈核验。缺少区域、插座用途或热模型输入时，系统会留下待核实项。

## 4. 填入 HA token，启动影子服务

在 HA 个人资料的安全设置创建长期访问 token。只在本机配置文件填写：

```sh
nano deploy/runtime.env
```

将 `HA_TOKEN=` 后面填入 token。此时 DeepSeek 密钥可以留空。保存退出，再执行：

```sh
docker compose --env-file deploy/runtime.env -f deploy/compose.yaml --profile software build moqi-runtime moqi-worker
docker compose --env-file deploy/runtime.env -f deploy/compose.yaml --profile software up -d moqi-runtime moqi-worker
docker compose --env-file deploy/runtime.env -f deploy/compose.yaml --profile software logs --tail 60 moqi-runtime moqi-worker
```

这组命令只启动/更新 Moqi 两个服务，不重建已有 HA。运行器等待 HA 配置可读、且至少有一个分配好区域的所选实体，然后生成 `data/runtime-config.json`；后台进程等待这个文件。容器显示 Up 只说明进程已启动，实际连接、数据和归因仍需检查。

两个服务的分工：运行器持有 HA token，负责本地事件和决策；后台进程持有之后配置的 DeepSeek key，负责整理、评测和策略候选。日常响应不等待 DeepSeek。修改基础配置后，重新执行上面的 `up -d --force-recreate moqi-runtime moqi-worker`，让两个服务加载新设置。HA 的实体选择/区域变化由运行器自动同步。

## 5. 读取实际状态和费用

```sh
docker compose --env-file deploy/runtime.env -f deploy/compose.yaml --profile software run --rm --no-deps moqi-worker python -m moqi.cli --config data/runtime-config.json memory-status
docker compose --env-file deploy/runtime.env -f deploy/compose.yaml --profile software run --rm --no-deps moqi-worker python -m moqi.cli --config data/runtime-config.json llm-cost
```

第一条只读取本地记忆状态，第二条只读取费用账本，不触发云调用。开始没有习惯属于正常状态；HA 普通状态变化默认归因 unknown，不会仅凭 `user_id` 当成手动操作。实际按钮、米家操作和旧自动化的归因属于接下来的硬件核验。

本地每日整理在家庭时间 03:00 之后运行，并写入独立 Git 记忆目录。若之后启用 DeepSeek，03:05 之后每天最多一次，费用不足则暂停云端更新，本地部分继续工作。费用包含未知结果的预留，并分别记录真实 token/缓存 usage；不是服务商银行卡级扣款上限。

## 6. 软件回放和之后的模型/音频接入

可以在新的隔离目录运行合成回放；不要把该目录或示例数据当作实际家庭数据库：

```sh
docker compose --env-file deploy/runtime.env -f deploy/compose.yaml --profile software run --rm --no-deps moqi-worker python -m moqi.scenarios --directory data/software-replay-001
```

最后应输出 `passed: true`、`synthetic: true`，并生成 `data/software-replay-001/report.json`。重复运行使用新的目录名。报告验证软件行为，不代表现场数据或模型质量。

模型导出、哈希校验、语音配置和独立策略数据按[模型和音频准备](model-and-audio-setup.md)完成。之后接入音频时用同一运行器的覆盖配置：

```sh
docker compose --env-file deploy/runtime.env -f deploy/compose.yaml -f deploy/compose.audio.yaml --profile software up -d --build moqi-runtime moqi-worker
```

此阶段仍为影子模式。音频、实时在场和首播恢复有独立检查；23:00–07:00 不播放任何语音。不能完成必需播报的动作不派发。

现场验收通过后，才叠加 `deploy/compose.live.yaml` 更新同一个运行器，并在 HA 开启执行开关。第一版实际执行只支持已获信任的低风险灯具；空调等继续学习/影子评估。不要同时另开一个控制进程。

## 停止与保留数据

```sh
docker compose --env-file deploy/runtime.env -f deploy/compose.yaml --profile software stop moqi-runtime moqi-worker
```

这会停止 Moqi 两个服务，保留 HA 和本地数据。配置、token、家庭数据库、语音会话及记忆日记留在本机；公开示例仅使用合成数据。
