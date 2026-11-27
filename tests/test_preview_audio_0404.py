"""Audio-only preview tests with generated sine waves; no user's media required."""
import ast
import os
import shutil
import subprocess
import tempfile
import threading
import types
import unittest
import wave
import array
from pathlib import Path
APP=Path(__file__).resolve().parents[1]/'source'/'app.py'
TREE=ast.parse(APP.read_text(encoding='utf-8'))
names={'_preview_atempo','_preview_ffmpeg','_preview_zero_frames','_build_preview_audio'}
nodes=[n for n in TREE.body if isinstance(n,ast.FunctionDef) and n.name in names]
nodes += [n for n in TREE.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id.startswith('_PREVIEW_AUDIO_') for t in n.targets)]
ns={'wave':wave,'Path':Path,'tempfile':tempfile,'subprocess':subprocess}
exec(compile(ast.Module(body=nodes,type_ignores=[]),str(APP),'exec'),ns)
FF=shutil.which('ffmpeg')
class AudioPreviewTests(unittest.TestCase):
    def test_speed_filter(self):
        self.assertEqual(ns['_preview_atempo'](.25),'atempo=0.5,atempo=0.5')
        self.assertEqual(ns['_preview_atempo'](4),'atempo=2,atempo=2')
    @unittest.skipUnless(FF,'ffmpeg needed')
    def test_actual_mix_and_offset(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            a=root/'game.wav';b=root/'music.wav';work=root/'work';work.mkdir()
            for path,freq in ((a,440),(b,880)):
                subprocess.run([FF,'-loglevel','error','-y','-f','lavfi','-i',f'sine=frequency={freq}:duration=2','-c:a','pcm_s16le',str(path)],check=True)
            clip=types.SimpleNamespace(path=str(a),kind='video',duration=1.0,start=.25,speed=1.0,volume=.0)
            project=types.SimpleNamespace(clips=[clip],music_path=str(b),music_offset=.25,music_volume=1.)
            out=ns['_build_preview_audio'](project,work,threading.Event(),FF,lambda p:types.SimpleNamespace(has_audio=True))
            with wave.open(out,'rb') as w:
                self.assertEqual(w.getframerate(),44100)
                self.assertEqual(w.getnchannels(),2)
                self.assertAlmostEqual(w.getnframes()/44100,1.,places=2)
                samples=array.array('h',w.readframes(w.getnframes()))
            self.assertLess(max(abs(v) for v in samples[:10000]),10)
            self.assertGreater(max(abs(v) for v in samples[40000:60000]),100)
            # Gameplay without music, trimmed, and sped up.
            clip.volume=1.;clip.speed=2.;clip.duration=.5;project.music_path=''
            out=ns['_build_preview_audio'](project,work,threading.Event(),FF,lambda p:types.SimpleNamespace(has_audio=True))
            with wave.open(out,'rb') as w:
                self.assertAlmostEqual(w.getnframes()/44100,.5,places=2)
                self.assertGreater(max(abs(v) for v in array.array('h',w.readframes(w.getnframes()))),100)
    def test_cancel(self):
        cancel=threading.Event();cancel.set()
        with tempfile.TemporaryDirectory() as temp:
            p=types.SimpleNamespace(clips=[types.SimpleNamespace(duration=1)])
            with self.assertRaisesRegex(RuntimeError,'cancelled'):
                ns['_build_preview_audio'](p,temp,cancel,'ffmpeg',lambda _:None)
    def test_playback_integration(self):
        studio=next(n for n in TREE.body if isinstance(n,ast.ClassDef) and n.name=='Studio')
        methods={n.name:ast.unparse(n) for n in studio.body if isinstance(n,ast.FunctionDef)}
        self.assertIn('self._prepare_preview_audio()',methods['toggle_play'])
        self.assertIn('self._audio_device.position()',methods['_tick'])
        for name in ('_snapshot','_refresh_all','on_close'):
            self.assertIn('self._invalidate_preview_audio()',methods[name])
        for name in ('_start_export','open_autodirector'):
            self.assertIn('self._stop_preview_audio()',methods[name])
if __name__=='__main__':unittest.main()
