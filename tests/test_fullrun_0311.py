"""Full Run: transitions -> completed chamber halves -> ONE final MP4."""
import json
import os
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
sys.modules.setdefault('engine',types.ModuleType('engine'))
engine=sys.modules['engine']
if not hasattr(engine,'MediaInfo'):
    engine.MediaInfo=type('MediaInfo',(object,),{})
import autodirector_fullrun as fullrun


def ev(at,kind,confidence=.98):
    return {'at':at,'kind':kind,'confidence':confidence}


class FullRunLogicTests(unittest.TestCase):
    def test_text_labels_are_distinct(self):
        self.assertEqual(fullrun.classify_screen_text('F  Start Challenge'),'start')
        self.assertEqual(fullrun.classify_screen_text('On the other side...'),'switch')
        self.assertEqual(fullrun.classify_screen_text('Challenge Completed'),'complete')
        self.assertEqual(fullrun.classify_screen_text('Challenge Failed'),'failure')
        self.assertEqual(fullrun.classify_screen_text('Floor 12 Chamber 1'),'blessing')
        self.assertIsNone(fullrun.classify_screen_text('Damage 100340'))

    def test_legacy_and_multiple_screenshots(self):
        refs=fullrun.normalize_references({
            'start':['start1.png','start2.png'],
            'switch':['transition1.png','transition2.png','transition3.png'],
            'complete':['complete1.png']},old_success='legacy.png')
        self.assertEqual(len(refs['start']),2)
        self.assertEqual(len(refs['switch']),3)
        self.assertEqual(len(refs['complete']),2)

    def test_repeated_start_prompts_are_one_event(self):
        stream=[ev(t,'start') for t in range(1,31)]
        events=fullrun.collapse_events(stream)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['at'],1)

    def test_two_completed_chambers_become_four_halves(self):
        stream=[
            ev(2,'blessing'),ev(5,'start'),ev(45,'switch'),ev(52,'start'),
            ev(82,'complete'),ev(91,'blessing'),ev(98,'start'),
            ev(132,'switch'),ev(139,'start'),ev(181,'complete')]
        result=fullrun.build_abyss_segments(stream,'input.mp4',200)
        self.assertEqual(result['completed_chambers'],2)
        segments=result['segments']
        self.assertEqual(len(segments),4)
        self.assertEqual([x['side'] for x in segments],
                         ['First Half','Second Half','First Half','Second Half'])
        self.assertEqual([x['chamber'] for x in segments],[1,1,2,2])
        self.assertEqual([(x['start'],x['end']) for x in segments],
                         [(5.0,44.85),(52.0,84.0),(98.0,131.85),(139.0,183.0)])

    def test_failed_attempt_is_discarded_not_exported(self):
        events=[
            ev(1,'start'),ev(20,'switch'),ev(26,'start'),ev(40,'failure'),
            ev(50,'start'),ev(75,'switch'),ev(79,'start'),ev(110,'complete')]
        result=fullrun.build_abyss_segments(events,'input.mp4',130)
        self.assertEqual(result['discarded_attempts'],1)
        self.assertEqual(result['completed_chambers'],1)
        self.assertEqual(len(result['segments']),2)
        self.assertEqual(result['segments'][0]['start'],50.0)
        self.assertEqual(result['segments'][1]['start'],79.0)

    def test_incomplete_chambers_do_not_leak_into_full_run(self):
        events=[ev(2,'start'),ev(15,'switch'),ev(21,'start')]
        result=fullrun.build_abyss_segments(events,'input.mp4',60)
        self.assertFalse(result['segments'])
        self.assertTrue(result['notes'])

    def test_no_second_start_is_inferred_and_requires_review(self):
        stream=[ev(3,'start'),ev(22,'switch'),ev(35,'complete')]
        result=fullrun.build_abyss_segments(stream,'input.mp4',43)
        self.assertEqual(len(result['segments']),2)
        self.assertTrue(all(x['review_required'] for x in result['segments']))
        self.assertEqual(result['segments'][1]['start'],27.0)

    def test_one_full_run_across_queue_preserves_recording_order(self):
        files=['first.mp4','second.mp4']
        canned=[
            fullrun.build_abyss_segments([
                ev(2,'start'),ev(14,'switch'),ev(20,'start'),ev(35,'complete')],
                files[0],40),
            fullrun.build_abyss_segments([
                ev(2,'start'),ev(16,'switch'),ev(21,'start'),ev(32,'complete')],
                files[1],38)]
        for j,report in enumerate(canned):
            report['path']=files[j]
            report['duration']=40
        def fake_scanner(path,*args,**kwargs):
            return canned[files.index(path)]
        with mock.patch.object(fullrun,'scan_abyss_recording',fake_scanner):
            report=fullrun.scan_full_run(files,{},sample_seconds=1)
        self.assertEqual(report['completed_chambers'],2)
        self.assertEqual([x['chamber'] for x in report['segments']],[1,1,2,2])
        self.assertEqual([x['path'] for x in report['segments']],
                         [files[0],files[0],files[1],files[1]])

    def test_overlapping_cuts_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'original.mp4'
            p.write_bytes(b'original')
            first={'path':str(p),'start':1,'end':12,'side':'First Half','enabled':True}
            second={'path':str(p),'start':11,'end':20,'side':'Second Half','enabled':True}
            with self.assertRaisesRegex(ValueError,'Overlapping'):
                fullrun.validate_full_run_segments([first,second])
            second['start']=13
            self.assertEqual(len(fullrun.validate_full_run_segments([first,second])),2)

    def test_onscreen_references_match_correct_kinds(self):
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest('cv2/numpy not installed')
        with tempfile.TemporaryDirectory() as d:
            bank={kind:[] for kind in fullrun.REFERENCE_KINDS}
            images=[]
            for kind,text,x,y in (
                ('start','START CHALLENGE',530,285),
                ('switch','ON THE OTHER SIDE',272,260),
                ('complete','CHALLENGE COMPLETED',295,195)):
                frame=np.full((540,960,3),(5,19,38),dtype=np.uint8)
                cv2.putText(frame,text,(x,y),cv2.FONT_HERSHEY_SIMPLEX,
                            .85,(255,240,235),2)
                path=str(Path(d)/(kind+'.png'))
                cv2.imwrite(path,frame)
                bank[kind].append(path)
                images.append((kind,frame))
            refs=fullrun.load_reference_bank(bank)
            for kind,frame in images:
                match,confidence=fullrun.match_reference(frame,refs)
                self.assertEqual(match,kind,(kind,match,confidence))
                self.assertGreaterEqual(confidence,.82)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),
                         'FFmpeg unavailable')
    def test_final_export_stitches_two_halves_into_one_video(self):
        import cv2
        import quality_engine as quality
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            source=root/'OBS.mp4'
            ff=shutil.which('ffmpeg')
            command=[ff,'-hide_banner','-loglevel','error','-y','-f','lavfi',
                     '-i','color=c=blue:s=320x180:r=30:d=5',
                     '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=5',
                     '-shortest','-c:v','mpeg4','-q:v','5','-c:a','aac',
                     '-ac','2','-ar','48000',str(source)]
            subprocess.run(command,check=True,capture_output=True)
            self.assertTrue(source.is_file())
            preset=quality.QualitySettings(preset='Clean',
                    resolution='1080p Landscape',fps=30)
            prefs=quality.empty_preferences()
            quality.store_named(prefs,'My Abyss Export',preset)
            rendered=[]
            def fake_render(path,output,opts,**kwargs):
                rendered.append((opts.start,opts.duration,opts.resolution))
                subprocess.run([
                    ff,'-hide_banner','-loglevel','error','-y',
                    '-ss',str(opts.start),'-i',path,'-t',str(opts.duration),
                    '-r','30','-c:v','mpeg4','-q:v','5',
                    '-c:a','aac','-ar','48000','-ac','2',output
                ],check=True,capture_output=True)
            class FakeEngine:
                get_ffmpeg=staticmethod(lambda:ff)
                probe=staticmethod(lambda path: types.SimpleNamespace(
                    width=320,height=180,fps=30,duration=5.0,
                    has_audio=True,kind='video'))
            segments=[
                {'path':str(source),'chamber':1,'side':'First Half',
                 'start':.2,'end':1.6,'enabled':True},
                {'path':str(source),'chamber':1,'side':'Second Half',
                 'start':2.1,'end':4.1,'enabled':True}]
            with mock.patch.object(quality,'engine',FakeEngine), \
                 mock.patch.object(quality,'load_preferences',lambda:prefs), \
                 mock.patch.object(quality,'render_quality',fake_render):
                output=fullrun.render_full_run(
                    segments,root/'Finished','My Abyss Export')
            self.assertEqual(len(rendered),2)
            self.assertEqual([t[:2] for t in rendered],[(.2,1.4),(2.1,2.0)])
            self.assertTrue(all(Path(p).is_file() for p in output.values()))
            self.assertEqual(len(list((root/'Finished').glob('*.mp4'))),1)
            manifest=json.loads(Path(output['cuts']).read_text())
            self.assertEqual(len(manifest['segments']),2)
            probe=subprocess.run([
                shutil.which('ffprobe'),'-v','error','-show_entries',
                'format=duration','-of','default=nw=1:nk=1',
                output['video']],capture_output=True,text=True,check=True)
            length=float(probe.stdout.strip())
            self.assertAlmostEqual(length,3.4,delta=.45)


if __name__=='__main__':
    unittest.main()
