"""Create the reviewed source/deployment ZIP and portable checksums. No publish."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

from .prepare_repository import ROOT,selection

START='''# 默契：软件准备包

从 [Ubuntu 接入步骤](docs/ubuntu-software-install.md) 继续。
软件与现场验收状态见 [验收台账](docs/jev-memory-acceptance.md)。

本包包含源码、HA 自定义集成、配置示例、固定依赖、模型准备配方和合成回放。
不包含 HA 镜像、模型权重、家庭数据或已安装环境；已有 HA 继续使用。
默认影子模式，云端分析默认关闭，月估算预算为已确认的 US$1。

Ubuntu 中先进入本文件所在目录，执行 `sha256sum -c SHA256SUMS.txt`。
之后按指南准备 HA 集成、token 和运行环境，模型由获授权服务器导出后另传。
普通行为学习不需要网页教学；可选纠正通过语音，23:00–07:00 完全静默。

软件回放使用合成数据。实际 JEV/DeepSeek、音频、ITX 性能和现场设备验收仍开放。
'''


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(destination,replay_report,check_report):
    destination=Path(destination).resolve()
    archive=destination.with_suffix('.zip')
    if destination.exists() or archive.exists():
        raise FileExistsError('Choose a new source bundle folder and ZIP name')
    replay=json.loads(Path(replay_report).read_text(encoding='utf-8-sig'))
    checks=json.loads(Path(check_report).read_text(encoding='utf-8-sig'))
    if (replay.get('schema')!='moqi.software-replay.v1' or replay.get('synthetic') is not True or
            replay.get('passed') is not True or replay.get('cloud_calls')!=0 or
            replay.get('teaching_inputs')!=0 or checks.get('passed') is not True):
        raise ValueError('A source bundle needs passing, correctly scoped software evidence')
    preview=ROOT/'dist/playground/index.html'
    if not preview.is_file():
        raise FileNotFoundError('Build the developer playground before packaging')
    destination.mkdir(parents=True)
    for name in selection():
        source=ROOT/name
        target=destination/name
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(source.read_text(encoding='utf-8-sig').replace('\r\n','\n'),encoding='utf-8',newline='\n')
    target=destination/'dist/playground/index.html'
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(preview.read_bytes())
    evidence=destination/'examples/reports'
    evidence.mkdir(parents=True)
    for filename,value in [('software-replay.json',replay),('software-checks.json',checks)]:
        (evidence/filename).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    (destination/'START-SOFTWARE.md').write_text(START,encoding='utf-8',newline='\n')
    files={path.relative_to(destination).as_posix():sha256(path) for path in sorted(destination.rglob('*')) if path.is_file()}
    manifest=dict(schema='moqi.source-bundle.v1',status='local_review_source_bundle',files=files,
        data='synthetic reports only; no household records, keys or model weights',
        included_ha_image=False,included_model_weights=False,field_validation='open',
        checks='examples/reports/software-checks.json',replay='examples/reports/software-replay.json')
    manifest_path=destination/'SOURCE-MANIFEST.json'
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    files[manifest_path.name]=sha256(manifest_path)
    (destination/'SHA256SUMS.txt').write_text(''.join(digest+'  '+name+'\n' for name,digest in sorted(files.items())),encoding='utf-8',newline='\n')
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as target:
        for path in sorted(destination.rglob('*')):
            if path.is_file(): target.write(path,destination.name+'/'+path.relative_to(destination).as_posix())
    checksum=archive.with_suffix(archive.suffix+'.sha256')
    checksum.write_text(sha256(archive)+'  '+archive.name+'\n',encoding='utf-8',newline='\n')
    return dict(folder=str(destination),zip=str(archive),zip_sha256=sha256(archive),files=len(files))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination')
    parser.add_argument('--replay-report',required=True)
    parser.add_argument('--check-report',required=True)
    args=parser.parse_args()
    print(json.dumps(build(args.destination,args.replay_report,args.check_report),ensure_ascii=False,indent=2))
