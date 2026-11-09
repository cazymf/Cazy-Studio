"""Cazy Studio 0.4.0 all-in-one Creative Lab: real local media exports."""
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'source'))
engine=sys.modules.setdefault('engine',types.ModuleType('engine'))
if not hasattr(engine,'MediaInfo'):
    engine.MediaInfo=type('MediaInfo',(object,),{})
import quality_engine as creative

FF=shutil.which('ffmpeg')
PROBE=shutil.which('ffprobe')


def make_test_video(target,seconds=5):
    command=[FF,'-hide_banner','-loglevel','error','-y',
        '-f','lavfi','-i',f'testsrc2=size=320x180:rate=30:duration={seconds}',
        '-f','lavfi','-i',f'sine=frequency=480:sample_rate=48000:duration={seconds}',
        '-c:v','mpeg4','-q:v','5','-c:a','aac','-ar','48000',
        '-ac','2','-shortest',str(target)]
    subprocess.run(command,capture_output=True,check=True)


class CreativeSuiteLogic(unittest.TestCase):
    def test_highlight_order_and_no_overlap(self):
        examples=[
            {'start':2.,'end':7.,'score':32,'at':4.},
            {'start':4.,'end':8.,'score':95,'at':6.},
            {'start':13.,'end':19.,'score':70,'at':15.},
            {'start':24.,'end':31.,'score':30,'at':26.},
        ]
        chosen=creative._creative_clips(examples,15)
        self.assertEqual(len(chosen),3)
        self.assertEqual([x['at'] for x in chosen],[6.,15.,26.])
        self.assertAlmostEqual(chosen[-1]['end']-chosen[0]['start']>0,True)
        self.assertEqual(sum(x['end']-x['start'] for x in chosen),15.)

    def test_energy_offset_is_explicit_and_optional(self):
        peaks=[{'at':3.,'strength':2.4},{'at':12.,'strength':8.},
               {'at':60.,'strength':2.}]
        self.assertEqual(creative.creative_music_offset(peaks,anchor=2),10.)
        self.assertEqual(creative.creative_music_offset([],anchor=2),0.)

    def test_vsr_models_not_silently_faked(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            weights=base/'real.pth'
            weights.write_bytes(b'fake')
            python=base/'python.exe'
            python.write_bytes(b'fake')
            config=base/'configs'/'realbasicvsr_x4.py'
            config.parent.mkdir()
            config.write_text('x')
            (base/'inference_realbasicvsr.py').write_text('x')
            status=creative.creative_vsr_preflight(
                'RealBasicVSR',base,weights,python)
            self.assertTrue(status['ready'],status)
            status=creative.creative_vsr_preflight(
                'EGVSR',base,weights,python)
            self.assertFalse(status['ready'])
            self.assertIn('cazy_egvsr_video.py',status['message'])
            with self.assertRaisesRegex(ValueError,'Choose RealBasicVSR or EGVSR'):
                creative.creative_vsr_preflight('Lanczos')

    def test_bug_report_paths_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            a=creative.creative_bug_report_path(td,'All Feature Bugs')
            a.write_text('first report')
            b=creative.creative_bug_report_path(td,'All Feature Bugs')
            self.assertNotEqual(a,b)
            b.write_text('second report')
            self.assertEqual(a.read_text(),'first report')

    def test_ui_has_all_roadmap_tabs_and_bug_notes(self):
        text=(ROOT/'source'/'app.py').read_text(encoding='utf-8')
        self.assertIn('✦ CREATIVE LAB — ALL FEATURES',text)
        self.assertIn('class CreativeLabWindow(tk.Toplevel):',text)
        for name in ('Highlights','Shorts','Cinematic','Thumbnail',
                     'Music Sync','RealBasicVSR + EGVSR','Bug Notes'):
            self.assertIn("'"+name+"'",text)
        self.assertIn('def save_bug_notes(self):',text)
        self.assertIn('def preview_vsr(self):',text)
        self.assertIn('quality_engine.creative_export_montage(',text)
        self.assertIn('quality_engine.creative_export_thumbnail(',text)
        self.assertIn('quality_engine.creative_music_sync_copy(',text)
        self.assertIn('quality_engine.creative_vsr_ab_preview(',text)
        self.assertIn('def build_abyss_segments(',(
            ROOT/'source'/'quality_engine.py').read_text(encoding='utf-8'))


@unittest.skipUnless(FF and PROBE,'FFmpeg/FFprobe needed')
class CreativeSuiteMedia(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.source=self.root/'OBS.mp4'
        make_test_video(self.source,seconds=6)
        self.music=self.root/'music.mp3'
        subprocess.run([FF,'-hide_banner','-loglevel','error','-y',
            '-f','lavfi','-i','sine=frequency=880:sample_rate=48000:duration=7',
            '-c:a','libmp3lame','-b:a','160k',str(self.music)],
            capture_output=True,check=True)
        self.probe=lambda path:types.SimpleNamespace(
            width=320,height=180,has_audio=True,duration=6,fps=30)
        self.patchers=[
            mock.patch.object(creative.engine,'get_ffmpeg',return_value=FF,create=True),
            mock.patch.object(creative.engine,'probe',side_effect=self.probe,create=True)]
        for p in self.patchers:p.start()
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patchers)])
        self.clips=[
            {'at':1.1,'start':.3,'end':2.5,'score':90},
            {'at':3.8,'start':2.9,'end':5.1,'score':80}]

    def ffprobe_streams(self,path):
        out=subprocess.run([PROBE,'-v','error','-show_entries',
            'stream=codec_type','-of','default=noprint_wrappers=1',
            str(path)],capture_output=True,text=True,check=True)
        return out.stdout

    def test_vertical_short_is_real_video_with_game_audio(self):
        result=creative.creative_export_montage(
            self.source,self.clips,self.root/'Shorts',mode='shorts',
            target_seconds=30,output_size=(180,320))
        video=Path(result['video'])
        self.assertTrue(video.is_file())
        self.assertTrue(Path(result['manifest']).is_file())
        self.assertIn('video',self.ffprobe_streams(video))
        self.assertIn('audio',self.ffprobe_streams(video))
        self.assertEqual(result['clips'],2)
        self.assertEqual(json.loads(Path(result['manifest']).read_text())['mode'],'shorts')
        self.assertTrue(self.source.is_file())

    def test_cinematic_effects_export_without_changing_full_run(self):
        original=self.source.read_bytes()
        result=creative.creative_export_montage(
            self.source,self.clips,self.root/'Cinematic',mode='cinematic',
            target_seconds=30,strength='Medium',output_size=(320,180))
        self.assertTrue(Path(result['video']).is_file())
        self.assertIn('audio',self.ffprobe_streams(result['video']))
        self.assertEqual(self.source.read_bytes(),original)

    def test_beat_sync_copy_contains_audio(self):
        result=creative.creative_music_sync_copy(
            self.source,self.music,self.root/'Music Sync')
        self.assertTrue(Path(result['video']).is_file())
        self.assertIn('audio',self.ffprobe_streams(result['video']))
        self.assertGreaterEqual(result['offset'],0.)

    def test_thumbnail_png_is_1280_by_720(self):
        from PIL import Image
        result=creative.creative_export_thumbnail(
            self.source,1.5,self.root/'Thumbs',
            title='BOSS FIGHT','GENSHIN IMPACT')
        with Image.open(result['image']) as img:
            self.assertEqual(img.size,(1280,720))
        self.assertEqual(result['title'],'BOSS FIGHT')


if __name__=='__main__':
    unittest.main()
