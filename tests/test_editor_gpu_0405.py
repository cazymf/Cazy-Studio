"""Editor GPU pipeline: commands, real CPU fallback media and AI integration."""
import ast
import copy
import os
import shutil
import subprocess
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock
APP=Path(__file__).resolve().parents[1]/'source'/'app.py'
TREE=ast.parse(APP.read_text(encoding='utf-8'))
NAMES={'_editor_encode_args','_editor_cuda_eligible','_editor_clip_command',
       '_editor_fast_render','_editor_ai_base_fps','_render_editor_pipeline',
       '_preview_atempo','_preview_ffmpeg','_preview_zero_frames','_build_preview_audio'}
nodes=[n for n in TREE.body if isinstance(n,ast.FunctionDef) and n.name in NAMES]
nodes += [n for n in TREE.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and (t.id.startswith('_PREVIEW_AUDIO_') or t.id=='_EDITOR_RENDER_MODES') for t in n.targets)]
import wave
ns={'__file__':str(APP),'Path':Path,'tempfile':tempfile,'subprocess':subprocess,
    'copy':copy,'wave':wave,'os':os}
exec(compile(ast.Module(body=nodes,type_ignores=[]),str(APP),'exec'),ns)
FF=shutil.which('ffmpeg');PROBE=shutil.which('ffprobe')

def clip(path,kind='video',start=0.,duration=1.,speed=1.,caption=''):
    return types.SimpleNamespace(path=str(path),kind=kind,start=start,duration=duration,
        speed=speed,caption=caption,volume=1.)
def project(clips,fps=30):
    return types.SimpleNamespace(clips=clips,width=320,height=180,fps=fps,crf=19,
        duration=sum(c.duration for c in clips),music_path='',music_offset=0.,music_volume=.5)
def media_info(path):
    return types.SimpleNamespace(width=320,height=180,fps=30,has_audio=True)
def run_ff(cmd,*a,**kw):
    p=subprocess.run(cmd,capture_output=True)
    if p.returncode:raise RuntimeError(p.stderr.decode(errors='replace'))
    if kw.get('on_progress'):kw['on_progress'](1.,None,None)

class EditorGPU(unittest.TestCase):
    def test_cuda_path_and_cpu_filters(self):
        c=clip('game.mp4')
        self.assertTrue(ns['_editor_cuda_eligible'](c,media_info('x'),320,180,30))
        cmd=ns['_editor_clip_command']('ffmpeg',c,media_info('x'),320,180,30,30,'out.mp4',19,True,True)
        self.assertIn('cuda',cmd)
        self.assertIn('h264_nvenc',cmd)
        self.assertIn('scale_cuda',cmd[cmd.index('-vf')+1])
        c.speed=2
        self.assertFalse(ns['_editor_cuda_eligible'](c,media_info('x'),320,180,30))
        cmd=ns['_editor_clip_command']('ffmpeg',c,media_info('x'),320,180,30,30,'out.mp4',19,False)
        self.assertIn('libx264',cmd)
        self.assertIn('setpts=(PTS-STARTPTS)/2',cmd[cmd.index('-vf')+1])
    def test_ai_uses_timeline_master_and_preserves_settings(self):
        p=project([clip('game.mp4')],120)
        fake=types.SimpleNamespace(local_addons=lambda _: {'Real-ESRGAN':'exe','RIFE':'exe'},
            TARGET_FPS=(0,30,60,120,240),QualitySettings=lambda **kw:types.SimpleNamespace(**kw),
            render_quality=Mock(return_value='output.mp4'))
        ns['quality_engine']=fake;ns['engine']=types.SimpleNamespace(probe=media_info)
        original=ns['_editor_fast_render']
        base=Mock()
        ns['_editor_fast_render']=base
        try:
            out=ns['_render_editor_pipeline'](p,'output.mp4','RIFE AI + Anime','Every output frame',Mock(),threading.Event())
            self.assertEqual(out,'output.mp4')
            self.assertEqual(base.call_args.args[0].fps,30)
            self.assertEqual(p.fps,120)
            self.assertEqual(fake.render_quality.call_args.args[2].frame_method,'RIFE AI')
            self.assertEqual(fake.render_quality.call_args.args[2].fps,120)
        finally:ns['_editor_fast_render']=original
    def test_caption_path_preserves_original_renderer(self):
        p=project([clip('game.mp4',caption='Hello')])
        ns['engine']=types.SimpleNamespace(render=Mock(return_value='out.mp4'))
        self.assertEqual(ns['_editor_fast_render'](p,'out.mp4',Mock(),threading.Event()),'out.mp4')
        ns['engine'].render.assert_called_once()
    @unittest.skipUnless(FF and PROBE,'FFmpeg required')
    def test_real_two_clip_edit_with_audio(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);a=root/'a.mp4';out=root/'out.mp4';music=root/'song.wav'
            subprocess.run([FF,'-loglevel','error','-y','-f','lavfi','-i','testsrc2=s=320x180:r=30:d=3',
                '-f','lavfi','-i','sine=frequency=440:duration=3','-c:v','libx264','-c:a','aac','-shortest',str(a)],check=True)
            subprocess.run([FF,'-loglevel','error','-y','-f','lavfi','-i','sine=frequency=880:duration=2',str(music)],check=True)
            p=project([clip(a,start=.25,duration=1.),clip(a,start=.5,duration=.5,speed=2.)])
            p.music_path=str(music)
            ns['quality_engine']=types.SimpleNamespace(_select_nvenc_ffmpeg=lambda *a,**kw:(FF,False,'CPU'),_run=run_ff)
            ns['engine']=types.SimpleNamespace(get_ffmpeg=lambda:FF,probe=media_info)
            ns['_editor_fast_render'](p,out,Mock(),threading.Event())
            import json
            meta=json.loads(subprocess.check_output([PROBE,'-v','error','-show_streams','-show_format','-of','json',str(out)]))
            self.assertAlmostEqual(float(meta['format']['duration']),1.5,places=1)
            self.assertEqual({s['codec_type'] for s in meta['streams']},{'video','audio'})
            video=next(s for s in meta['streams'] if s['codec_type']=='video')
            self.assertEqual(video['width'],320);self.assertEqual(video['height'],180)
            self.assertEqual(video['avg_frame_rate'],'30/1')
if __name__=='__main__':unittest.main()
