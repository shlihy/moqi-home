"""Explicit loading of a local EdgeJev ONNX model; importing Moqi stays light."""
import json
from pathlib import Path


def model_files(model_path):
    """Read the runtime's artifact contract, including its configured ONNX name."""
    directory = Path(model_path).resolve()
    config_path = directory / 'edgejev.json'
    if not config_path.is_file():
        raise RuntimeError('EdgeJev blocked: missing local artifact edgejev.json')
    config = json.loads(config_path.read_text(encoding='utf-8'))
    if config.get('backend') != 'laya' or config.get('runtime', 'onnx') != 'onnx':
        raise ValueError('Moqi currently supports the EdgeJev laya ONNX backend')
    name = config.get('onnx_file')
    if not isinstance(name, str) or not name:
        raise ValueError('edgejev.json must identify its onnx_file')
    onnx_path = (directory / name).resolve()
    if not onnx_path.is_relative_to(directory):
        raise ValueError('The ONNX artifact must be inside the model directory')
    files = [config_path, directory / 'tokenizer.json', onnx_path]
    for filename in files:
        if not filename.is_file():
            raise RuntimeError('EdgeJev blocked: missing local artifact ' + filename.name)
    return directory, config, files


def load_agent(model_path, *, threads=2):
    directory, _, _ = model_files(model_path)
    try:
        from edgejev import Agent
    except ImportError as exc:
        raise RuntimeError('EdgeJev blocked: optional edgejev runtime is not installed') from exc
    return Agent(str(directory), threads=threads, provider='cpu')
