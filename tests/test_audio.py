"""Audio protocol and artifact integrity fixtures; no model or device is opened."""
import hashlib
import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path

from moqi.assets import verify
from moqi.audio import VoiceFrontend
from moqi.speech import PCM

AT=datetime(2026,10,22,2,tzinfo=timezone.utc)
CHUNK=PCM(b'\0\0'*1600,16000)


class Wake:
    def __init__(self): self.detected=False
    def accept(self,audio): value=self.detected; self.detected=False; return value


class VAD:
    def __init__(self): self.segments=[]
    def reset(self): self.segments=[]
    def accept(self,audio): value=self.segments; self.segments=[]; return value


class ASR:
    def __init__(self): self.calls=0
    def transcribe(self,audio): self.calls+=1; return '今晚别自动关餐厅灯'


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.wake,self.vad,self.asr=Wake(),VAD(),ASR()
        self.front=VoiceFrontend(self.wake,self.vad,self.asr,'dining')

    def test_wake_required_short_sessions_and_playback_do_not_transcribe_household_background(self):
        self.vad.segments=[CHUNK]
        self.assertEqual(self.front.feed(CHUNK,AT),[])
        self.assertEqual(self.asr.calls,0)
        self.wake.detected=True
        self.assertEqual(self.front.feed(CHUNK,AT+timedelta(seconds=1)),[])
        self.vad.segments=[CHUNK]
        first=self.front.feed(CHUNK,AT+timedelta(seconds=2))[0]
        self.vad.segments=[CHUNK]
        second=self.front.feed(CHUNK,AT+timedelta(seconds=3))[0]
        self.assertEqual(first.session_id,second.session_id)
        self.assertNotEqual(first.request_id,second.request_id)
        self.vad.segments=[CHUNK]
        self.assertEqual(self.front.feed(CHUNK,AT+timedelta(seconds=4),playback_active=True),[])
        self.assertEqual(self.asr.calls,2)
        self.assertEqual(self.front.feed(CHUNK,AT+timedelta(seconds=64)),[])

    def test_overlong_or_clock_reversed_audio_never_reaches_asr(self):
        self.wake.detected=True
        self.front.feed(CHUNK,AT)
        self.vad.segments=[PCM(b'\0\0'*16000*13,16000)]
        with self.assertRaisesRegex(ValueError,'overlong'):
            self.front.feed(CHUNK,AT+timedelta(seconds=1))
        self.assertEqual(self.asr.calls,0)
        with self.assertRaisesRegex(ValueError,'backwards'):
            self.front.feed(CHUNK,AT)

    def test_model_manifest_hash_checks_and_path_boundary_require_real_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            artifact=root/'fixture.onnx'
            artifact.write_bytes(b'SYNTHETIC-NOT-A-MODEL')
            manifest=root/'manifest.json'
            value=dict(schema_version=1,files={'vad':dict(path='fixture.onnx',sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())})
            manifest.write_text(json.dumps(value))
            self.assertEqual(verify(manifest,['vad'])['vad'],str(artifact.resolve()))
            artifact.write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError,'does not match'): verify(manifest,['vad'])
            value['files']['vad']['path']='../outside.onnx'
            manifest.write_text(json.dumps(value))
            with self.assertRaises(ValueError): verify(manifest,['vad'])


if __name__=='__main__': unittest.main()
