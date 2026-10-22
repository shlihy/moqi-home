"""Build the pinned local model and record actual artifacts and inference.

Run inside an environment installed from requirements/jev-build-lock.txt:
    python -m tools.build_jev
Model weights and the resulting manifest live under ignored models/.
"""
import argparse
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import time
from urllib.parse import urlparse
from urllib.request import url2pathname
from pathlib import Path

from moqi.jev_runtime import load_agent, model_files

RUNTIME_REVISION = 'dce469472e0e6eea396256e9bf866082dcb9490b'
MODEL_ID = 'convaiinnovations/laya-multilingual'
MODEL_REVISION = '1720e3e3357cfe1e281542e223f8273b0890ca34'


def runtime_provenance():
    distribution = importlib.metadata.distribution('edgejev')
    direct = json.loads(distribution.read_text('direct_url.json') or '{}')
    vcs = direct.get('vcs_info', {})
    if vcs.get('commit_id') == RUNTIME_REVISION:
        revision = vcs['commit_id']
    elif urlparse(direct.get('url', '')).scheme == 'file':
        source = Path(url2pathname(urlparse(direct['url']).path))
        revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
        subprocess.run(['git', '-C', str(source), 'diff', '--exit-code', 'HEAD', '--', 'edgejev'], check=True)
        for filename in (source / 'edgejev').rglob('*.py'):
            installed = Path(distribution.locate_file(filename.relative_to(source)))
            if installed.read_text(encoding='utf-8') != filename.read_text(encoding='utf-8'):
                raise RuntimeError('Installed EdgeJev source differs from the pinned checkout')
    else:
        raise RuntimeError('Install EdgeJev from the pinned source revision in requirements/jev-build-lock.txt')
    if revision != RUNTIME_REVISION:
        raise RuntimeError('The installed EdgeJev revision does not match this build recipe')
    return {'version': distribution.version, 'revision': revision}


def sha256(filename):
    digest = hashlib.sha256()
    with filename.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def build(source, output):
    from huggingface_hub import snapshot_download

    runtime = runtime_provenance()
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError('Preserve the existing model directory and choose a new --output')
    snapshot_download(MODEL_ID, revision=MODEL_REVISION, local_dir=str(source))
    subprocess.run([sys.executable, '-m', 'edgejev.cli', 'build', '--backend', 'laya',
                    '--model', str(source), '--out', str(output), '--precision', 'int8'], check=True)
    _, config, files = model_files(output)
    manifest = {
        'schema': 'moqi.jev-model.v1', 'runtime': runtime,
        'source_model': MODEL_ID, 'source_revision': MODEL_REVISION,
        'backend': config['backend'], 'precision': 'int8',
        'python': platform.python_version(), 'platform': platform.platform(),
        'packages': {d.metadata['Name']: d.version for d in importlib.metadata.distributions()},
        'artifacts': {p.relative_to(output).as_posix(): {'sha256': sha256(p), 'bytes': p.stat().st_size}
                      for p in files},
    }
    started = time.perf_counter()
    agent = load_agent(output)
    manifest['load_ms'] = (time.perf_counter() - started) * 1000
    question = {'intent': {
        'type': 'choice', 'instructions': '用户表达了哪一种家居请求？',
        'criteria': {
            'constraint': '要求限制或禁止某项自动操作',
            'preference': '表达在某种情境下喜欢的舒适温度',
            'exception': '说明过去某次行为是特例，不应作为日常习惯',
            'unknown': '其他或未表达明确请求',
        },
    }}
    # Development smoke cases; these are not the frozen acceptance holdout.
    cases = [
        ('夜里这盏餐厅灯就别自行熄灭了', 'constraint'),
        ('我在书房干活时，二十三度最舒服', 'preference'),
        ('那次是朋友来访才开灯到很晚，平时不是这样', 'exception'),
    ]
    manifest['inference'] = []
    for text, expected in cases:
        started = time.perf_counter()
        result = agent.system_one(text, question)
        manifest['inference'].append({
            'text': text, 'expected': expected, 'result': result,
            'elapsed_ms': (time.perf_counter() - started) * 1000,
        })
    if sys.platform == 'win32':
        import psutil
        manifest['process_peak_memory_bytes'] = psutil.Process().memory_info().peak_wset
    else:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        manifest['process_peak_memory_bytes'] = peak if sys.platform == 'darwin' else peak * 1024
    destination = output / 'moqi-model-manifest.json'
    destination.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'manifest': str(destination), 'inference': manifest['inference']}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default='models/laya-source')
    parser.add_argument('--output', default='models/jev-int8')
    options = parser.parse_args()
    build(options.source, options.output)
