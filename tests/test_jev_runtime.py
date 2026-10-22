"""Regressions at the actual EdgeJev model-directory boundary."""
import json
import tempfile
import unittest
from pathlib import Path

from moqi.jev_runtime import model_files


class ModelDirectoryTests(unittest.TestCase):
    def test_reads_the_configured_onnx_filename(self):
        # EdgeJev builds choose their own file name; model.onnx is not the contract.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'edgejev.json').write_text(json.dumps({
                'backend': 'laya', 'runtime': 'onnx', 'onnx_file': 'quantized.onnx',
            }), encoding='utf-8')
            (root / 'tokenizer.json').write_text('{}', encoding='utf-8')
            (root / 'quantized.onnx').write_bytes(b'fixture for path validation only')
            resolved, config, files = model_files(root)
            self.assertEqual(resolved, root.resolve())
            self.assertEqual(files[-1].name, config['onnx_file'])

    def test_missing_configured_artifact_reports_its_real_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'edgejev.json').write_text(json.dumps({
                'backend': 'laya', 'onnx_file': 'quantized.onnx',
            }), encoding='utf-8')
            (root / 'tokenizer.json').write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'missing local artifact quantized.onnx'):
                model_files(root)

    def test_model_configuration_cannot_select_files_outside_its_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'edgejev.json').write_text(json.dumps({
                'backend': 'laya', 'onnx_file': '../another-household/model.onnx',
            }), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'inside the model directory'):
                model_files(root)


if __name__ == '__main__':
    unittest.main()
