"""Source/transfer manifest checks on fake files; no actual model artifacts."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from moqi.assets import verify
from tools.export_audio_manifest import export


class ExportTests(unittest.TestCase):
    def test_export_records_actual_files_and_checksums_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'tokens.txt').write_text('m 0\nò 1\nq 2\nì 3\n',encoding='utf-8')
            (root/'keywords.txt').write_text('m ò q ì @默契\n',encoding='utf-8')
            (root/'vad.onnx').write_bytes(b'SYNTHETIC-NOT-A-MODEL')
            (root/'original.tar.bz2').write_bytes(b'SYNTHETIC-ARCHIVE')
            recipe=root/'recipe.json'
            recipe.write_text(json.dumps(dict(files={'kws_tokens':'tokens.txt','keywords':'keywords.txt','vad':'vad.onnx'},
                standalone_source='https://example.invalid/fixture',archives=[dict(name='original.tar.bz2',
                    bytes=len(b'SYNTHETIC-ARCHIVE'),publisher_sha256=hashlib.sha256(b'SYNTHETIC-ARCHIVE').hexdigest())])),encoding='utf-8')
            path=export(recipe,root)
            value=json.loads(path.read_text(encoding='utf-8'))
            self.assertFalse(value['inference_tested'])
            self.assertEqual(verify(path,['vad'])['vad'],str(root/'vad.onnx'))
            self.assertIn('manifest.json',(root/'manifest.SHA256SUMS.txt').read_text(encoding='utf-8'))
            with self.assertRaises(ValueError): export(recipe,root)
