# 本地模型和音频的准备

这些步骤供获授权的 Linux 构建服务器和之后的工控机接入使用。Windows 开发机只编辑和检查软件，未运行真实模型。源码包不包含模型权重、Home Assistant 镜像或已安装的 Python 环境。

## JEV：服务器构建，工控机只运行 ONNX

源码固定 EdgeJev 提交 `dce469472e0e6eea396256e9bf866082dcb9490b`，模型为 `convaiinnovations/laya-multilingual` 的 `1720e3e3357cfe1e281542e223f8273b0890ca34` 修订。`requirements/jev-server-build.lock.txt` 是 Linux x86_64 / Python 3.13 的构建依赖及分发文件哈希；运行端的 `itx-runtime.lock.txt` 不包含 PyTorch 或转换工具。

在获授权的 Linux 构建服务器、项目根目录执行：

```sh
mkdir -p models
docker build -f deploy/Dockerfile.jev-build -t moqi-jev-build:2026-10-22 .
docker run --rm -v "$PWD/models:/app/models" moqi-jev-build:2026-10-22
```

此步骤需要网络和模型构建资源。它下载指定模型修订、转换 INT8 ONNX，生成 `models/jev-int8/edgejev.json`、分词器、ONNX 和 `moqi-model-manifest.json`。清单记录实际文件 SHA-256、模型来源、依赖、构建环境及三个开发样例的实际推理输出。构建失败时保留输出及错误，不编写成功记录。

把整个 `models/jev-int8` 文件夹传到工控机的项目 `models/` 中；用构建清单逐个检查文件哈希。源权重和构建环境留在服务器。当前尚未完成这次真实转换或推理，源码检查不构成模型正确率、目标 CPU 兼容性或速度证据。

## 本地语音资产

语音接口使用 Sherpa KWS → Silero VAD → SenseVoice INT8 → 代码/JEV → VITS TTS。固定依赖在 `requirements/itx-audio.lock.txt`；CPU 每个音频模型先使用 1 个线程。真实共存内存和延迟在 ITX 上测量，不能按其他 CPU 的公开成绩推定。

在服务器或之后的目标机项目目录建立 `models/audio`，从 `examples/models/audio-recipe.json` 列出的官方地址下载三个压缩包，以及 `silero_vad.onnx`。保留原压缩包，在同一目录解压；不要把不同年份、chunk 或 epoch 的 KWS 文件混在一起。该配方的 KWS 是 2024-01-01、epoch-12、chunk-16 的 INT8 版本。

下载可用 `curl -L -C - -o 文件名 官方URL` 继续未完成的文件；下载源不支持续传时应明确报错。来源、文件大小和可用的发行方摘要已写在配方里。SenseVoice 压缩包的发行方 SHA-256 为：

```text
7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e
```

在项目根目录执行：

```sh
cp examples/models/keywords.txt models/audio/keywords.txt
python3 tools/export_audio_manifest.py --root models/audio
cd models/audio
sha256sum -c manifest.SHA256SUMS.txt
```

哈希导出工具只使用系统 Python3 标准库，不需要在主机安装模型运行库。它核对完整文件、配方中的压缩包大小和已有发行方摘要，并检查唤醒词 token 是否属于该 KWS 词表；随后生成 `manifest.json` 和 `manifest.SHA256SUMS.txt`。旧压缩包没有发行方摘要时，清单明确标记为本地可信导出所得哈希；这能检查传输一致性，不能冒充发行方验证或真实模型测试。

默认唤醒词为“默契默契”。词表检查只证明 token 存在，误唤醒、漏唤醒、中文识别和扬声器效果仍需实际验证。`audio.example.json` 复制为 `audio.local.json` 后，填入麦克风所在的 HA 区域 ID；可在生成的 `data/runtime-config.json` 中查看同房间实体的 `room` 值。时区必须与 HA 一致，输入/输出设备留空表示用系统默认设备。

之后用 `deploy/compose.audio.yaml` 替换同一运行器的配置，接入 `/dev/snd`；默认仍是影子模式。PCM 只在内存中流转，不写音频文件、不发送到云端。超过上下文的语音会请求澄清；不会删掉用户话语末尾的例外。已知习惯按完整条目选取，模型问题说明超限也会拒绝，避免 EdgeJev 上游布局静默截断。

## 策略自动发布所需的数据

`examples/jev/policy-manifest.example.json` 和 `policy-cases.example.jsonl` 是格式示例，没有伪造 200 条独立验收数据。实际验证清单必须有固定 SHA-256、冻结时间、数据集 ID，以及 `human_review` 或 `verified_adapter` 的独立标签来源。正确标签需写明设备、动作和范围/数值；只写“成功解释”不能验证决策正确。

验证集至少 200 例，其中至少 100 例受支持、20 例必须澄清；它必须在候选产生前冻结。通过后，影子集至少 50 例，其中至少 20 例受支持、5 例须澄清，且全部来自验证通过之后、与原例 ID 不重叠。真实模型评测、增益、精确率/覆盖和必须通过的场景均达标后才自动激活；没有数据或模型时保留原版本并明确等待。

在部署配置的 `jev_policy` 中填入这些清单路径和模型目录后启用；独立后台进程会做验证、影子评测与原子发布。可以用下列开发者入口查看或手动回退，家庭日常无需网页 Apply：

```sh
python -m moqi.policy_worker --config data/runtime-config.json --status
python -m moqi.policy_worker --config data/runtime-config.json --rollback-current --reason "已核实的回退原因"
```

发布、回退、费用、执行和播报账本不随家庭记忆回滚清空。发布后出现两次独立的明确语音否定，会退回前一策略；取消与不回答不算模型答错。

官方依据：[EdgeJev](https://github.com/yzfly/edgejev)、[KWS 模型与自定义唤醒词](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html)、[SenseVoice](https://k2-fsa.github.io/sherpa/onnx/sense-voice/index.html)、[VITS 语音模型](https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/vits.html)。配方与接口于 2026-10-22 核对，实际模型/音频验收仍开放。
