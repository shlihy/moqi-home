"""Wake → bounded VAD → local ASR contracts and optional Sherpa/PortAudio adapters.

Model and audio libraries are lazy imports. Audio buffers stay in memory; this
module has no audio-file writer or cloud client. Real inference belongs on the
authorized target/server, never in the Windows development verification.
"""
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
from queue import Full,Queue
from threading import Event,Thread
from uuid import uuid4
from zoneinfo import ZoneInfo

from .assets import verify
from .speech import PCM


@dataclass(frozen=True)
class Utterance:
    text:str
    room:str
    session_id:str
    at:datetime
    request_id:str


class VoiceFrontend:
    """Single microphone/room, short wake sessions and strictly bounded segments."""
    def __init__(self,wake,vad,asr,room,*,wake_phrases=()):
        self.wake,self.vad,self.asr,self.room=wake,vad,asr,room
        self.wake_phrases=tuple(sorted(wake_phrases,key=len,reverse=True))
        self.session=None
        self.until=None
        self.previous=None

    def reset(self):
        self.session=self.until=None
        self.vad.reset()

    def feed(self,audio,at,*,playback_active=False):
        if at.tzinfo is None or audio.sample_rate!=16000 or audio.duration>.2:
            raise ValueError('Microphone input must be timestamped mono 16 kHz chunks of at most 200 ms')
        if self.previous and at<self.previous:
            self.reset()
            raise ValueError('Microphone clock moved backwards')
        self.previous=at
        if playback_active:
            self.vad.reset()
            return []
        if self.until is not None and at>=self.until:
            self.reset()
        if self.session is None:
            if not self.wake.accept(audio): return []
            self.session=uuid4().hex
            self.until=at+timedelta(seconds=8)
            self.vad.reset()
            return []
        results=[]
        for segment in self.vad.accept(audio):
            if not isinstance(segment,PCM) or segment.sample_rate!=16000 or segment.duration>12:
                self.reset()
                raise ValueError('VAD emitted an invalid or overlong segment')
            text=self.asr.transcribe(segment).strip()
            for phrase in self.wake_phrases:
                if text.startswith(phrase):
                    text=text[len(phrase):].lstrip('，,。 !！')
                    break
            if not text: continue
            if len(text)>500:
                self.reset()
                raise ValueError('ASR result is too long for a household utterance')
            results.append(Utterance(text,self.room,self.session,at,uuid4().hex))
            self.until=at+timedelta(seconds=60)
        return results


def _samples(audio):
    import numpy as np
    return np.frombuffer(audio.data,dtype='<i2').astype(np.float32)/32768


def _pcm(samples,rate):
    import numpy as np
    samples=np.asarray(samples)
    if not np.all(np.isfinite(samples)):
        raise ValueError('Local audio model returned nonfinite samples')
    return PCM((np.clip(samples,-1,1)*32767).astype('<i2').tobytes(),rate)


class SherpaWake:
    def __init__(self,files):
        import sherpa_onnx as sherpa
        self.model=sherpa.KeywordSpotter(tokens=files['kws_tokens'],encoder=files['kws_encoder'],
            decoder=files['kws_decoder'],joiner=files['kws_joiner'],keywords_file=files['keywords'],
            num_threads=1,provider='cpu')
        self.stream=self.model.create_stream()

    def accept(self,audio):
        self.stream.accept_waveform(audio.sample_rate,_samples(audio))
        hit=False
        while self.model.is_ready(self.stream):
            self.model.decode_stream(self.stream)
            if self.model.get_result(self.stream):
                hit=True
                self.model.reset_stream(self.stream)
        return hit


class SherpaVAD:
    def __init__(self,files):
        import sherpa_onnx as sherpa
        config=sherpa.VadModelConfig()
        config.silero_vad.model=files['vad']
        config.silero_vad.min_silence_duration=.5
        config.silero_vad.min_speech_duration=.25
        config.silero_vad.max_speech_duration=8
        config.sample_rate=16000
        config.num_threads=1
        self.window=config.silero_vad.window_size
        self.model=sherpa.VoiceActivityDetector(config,buffer_size_in_seconds=16)
        self.buffer=b''

    def reset(self): self.model.reset(); self.buffer=b''

    def accept(self,audio):
        self.buffer+=audio.data
        size=self.window*2
        while len(self.buffer)>=size:
            self.model.accept_waveform(_samples(PCM(self.buffer[:size],16000)))
            self.buffer=self.buffer[size:]
        results=[]
        while not self.model.empty():
            # front.samples is valid only until the next detector method.
            results.append(_pcm(self.model.front.samples,16000))
            self.model.pop()
        return results


class SenseVoice:
    def __init__(self,files):
        import sherpa_onnx as sherpa
        self.model=sherpa.OfflineRecognizer.from_sense_voice(model=files['asr'],tokens=files['asr_tokens'],
            num_threads=1,use_itn=False,debug=False)

    def transcribe(self,audio):
        stream=self.model.create_stream()
        stream.accept_waveform(audio.sample_rate,_samples(audio))
        self.model.decode_stream(stream)
        return stream.result.text


class SherpaTTS:
    def __init__(self,files,*,speaker=0,speed=1.):
        import sherpa_onnx as sherpa
        config=sherpa.OfflineTtsConfig(model=sherpa.OfflineTtsModelConfig(
            vits=sherpa.OfflineTtsVitsModelConfig(model=files['tts'],tokens=files['tts_tokens'],lexicon=files['tts_lexicon']),
            num_threads=1,provider='cpu',debug=False),
            rule_fsts=','.join(files[name] for name in ('tts_phone','tts_date','tts_number') if name in files),
            max_num_sentences=1)
        if not config.validate(): raise ValueError('Invalid local Sherpa TTS configuration')
        self.model=sherpa.OfflineTts(config)
        self.generation=sherpa.GenerationConfig()
        self.generation.sid=speaker
        self.generation.speed=speed

    def synthesize(self,text):
        audio=self.model.generate(text,self.generation)
        return _pcm(audio.samples,audio.sample_rate)


class PortAudioPlayer:
    def __init__(self,*,device=None,timezone_name='Asia/Shanghai'):
        self.device,self.zone=device,ZoneInfo(timezone_name)
        self.stream=None

    @property
    def busy(self): return self.stream is not None and self.stream.active

    def play(self,audio):
        import numpy as np
        import sounddevice as sd
        if self.busy: raise RuntimeError('Audio output is already playing')
        if self.stream is not None: self.stream.close()
        samples=np.frombuffer(audio.data,dtype='<i2')
        offset=0
        def callback(output,frames,timing,status):
            nonlocal offset
            output.fill(0)
            hour=datetime.now(self.zone).hour
            if hour>=23 or hour<7: raise sd.CallbackStop
            count=min(frames,len(samples)-offset)
            output[:count,0]=samples[offset:offset+count]
            offset+=count
            if offset==len(samples): raise sd.CallbackStop
        self.stream=sd.OutputStream(device=self.device,channels=1,dtype='int16',
            samplerate=audio.sample_rate,blocksize=max(1,audio.sample_rate//50),callback=callback)
        self.stream.start()

    def close(self):
        if self.stream is not None: self.stream.abort(); self.stream.close(); self.stream=None


class MicrophoneFeed:
    def __init__(self,frontend,player,*,device=None):
        self.frontend,self.player,self.device=frontend,player,device
        self.queue=Queue(maxsize=4)
        self.stop=Event()
        self.error=None
        self.thread=Thread(target=self._run,name='moqi-microphone',daemon=True)

    def _run(self):
        import sounddevice as sd
        try:
            with sd.RawInputStream(device=self.device,channels=1,samplerate=16000,dtype='int16',blocksize=1600) as stream:
                while not self.stop.is_set():
                    data,overflow=stream.read(1600)
                    if overflow: self.frontend.reset(); continue
                    for value in self.frontend.feed(PCM(bytes(data),16000),datetime.now(timezone.utc),playback_active=self.player.busy):
                        try: self.queue.put_nowait(value)
                        except Full:
                            self.error='voice_queue_overflow'
                            self.frontend.reset()
                            return
        except Exception:
            # No ASR text, audio or device exception payload is written to logs.
            self.error='audio_capture_or_recognition_failed'

    def start(self): self.thread.start()
    def close(self): self.stop.set(); self.thread.join(timeout=2)


def create(path):
    path=Path(path).resolve()
    config=json.loads(path.read_text(encoding='utf-8-sig'))
    required=['kws_tokens','kws_encoder','kws_decoder','kws_joiner','keywords','vad',
              'asr','asr_tokens','tts','tts_tokens','tts_lexicon']
    manifest=(path.parent/config['manifest']).resolve()
    value=json.loads(manifest.read_text(encoding='utf-8-sig'))
    required+=[name for name in ('tts_phone','tts_date','tts_number') if name in value['files']]
    files=verify(manifest,required)
    player=PortAudioPlayer(device=config.get('output_device'),timezone_name=config['timezone'])
    frontend=VoiceFrontend(SherpaWake(files),SherpaVAD(files),SenseVoice(files),config['room'],
                           wake_phrases=config.get('wake_phrases',[]))
    microphone=MicrophoneFeed(frontend,player,device=config.get('input_device'))
    return config,SherpaTTS(files,speaker=config.get('speaker',0),speed=config.get('speed',1.)),player,microphone
