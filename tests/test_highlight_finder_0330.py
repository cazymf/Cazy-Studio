"""Visual Highlight Finder beta: independent from Abyss OCR and Full Run."""
import json
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
import quality_engine as quality


class HighlightFinderTests(unittest.TestCase):
    def test_rank_picks_distinct_motion_peaks_and_returns_chronological_clips(self):
        samples=[
            {'at':10.,'score':75.,'motion':22.,'detail':900.},
            {'at':11.,'score':94.,'motion':38.,'detail':1200.},
            {'at':24.,'score':83.,'motion':28.,'detail':900.},
            {'at':40.,'score':68.,'motion':20.,'detail':700.},
            {'at':50.,'score':7.,'motion':1.,'detail':20.},
        ]
        highlights=quality.rank_highlight_samples(samples,duration=80,
                                                   clip_seconds=8,limit=3)
        self.assertEqual(len(highlights),3)
        self.assertEqual([x['at'] for x in highlights],[11.,24.,40.])
        self.assertTrue(all(x['end']-x['start']>=7.99 for x in highlights))
        self.assertEqual(highlights[0]['number'],1)
        self.assertEqual(highlights[2]['number'],3)
        self.assertTrue(all(x['review_required'] for x in highlights))
        self.assertIn('not neural AI',highlights[0]['method'])

    def test_ranking_handles_boundaries_and_no_motion(self):
        result=quality.rank_highlight_samples(
            [{'at':1.,'score':88.},{'at':29.,'score':89.}],
            duration=30.,clip_seconds=8,limit=3)
        self.assertEqual(result[0]['start'],0.)
        self.assertEqual(result[-1]['end'],30.)
        self.assertEqual(quality.rank_highlight_samples(
            [{'at':10.,'score':0.}],duration=30),[])

    def test_highlight_scan_uses_isolated_frame_stream_and_never_video_capture(self):
        import cv2
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp:
            video=Path(tmp)/'gameplay.mp4'
            video.write_bytes(b'placeholder')
            def frames(*args,**kwargs):
                for n in range(48):
                    frame=np.full((120,220,3),(55,35,25),dtype=np.uint8)
                    # Moving high-contrast colored bars give a physical motion signal.
                    x=(n*11)%165
                    cv2.rectangle(frame,(x,25),(x+30,110),(220,150,255),-1)
                    if 16<=n<=31:
                        cv2.rectangle(frame,(15,20),(205,100),
                                      (20+(n%2)*230,210,230),-1)
                    yield n/2,frame
            with mock.patch.object(quality,'_cazy_ocr_decoder_candidates',
                                   return_value=['fake_ffmpeg']), \
                 mock.patch.object(quality,'_cazy_ocr_video_info',
                                   return_value=(24.,220,120)), \
                 mock.patch.object(quality,'_cazy_ocr_frame_stream',
                                   side_effect=frames) as stream:
                report=quality.highlight_finder_scan(video,limit=3)
            self.assertGreater(report['frames_analyzed'],0)
            self.assertGreaterEqual(report['found'],1,report)
            self.assertEqual(report['method'],
                             'Visual motion/detail scoring (not neural AI)')
            self.assertEqual(report['source'],str(video))
            self.assertEqual(stream.call_count,1)
            self.assertEqual(report['highlights'][0]['review_required'],True)

    def test_highlight_export_keeps_source_and_creates_separate_clips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/'gameplay.mp4'
            source.write_bytes(b'Original never changes')
            ffmpeg=root/'ffmpeg.exe'
            ffmpeg.write_bytes(b'fake binary')
            logs=[]
            def fake_run(command,**kwargs):
                logs.append(command)
                target=Path(command[-1])
                target.write_bytes(b'VALIDMP4'*110)
                return types.SimpleNamespace(returncode=0,stderr=b'')
            clips=[
                {'at':10.,'start':6.,'end':14.},
                {'at':35.,'start':31.,'end':39.},
            ]
            with mock.patch.object(quality.engine,'get_ffmpeg',
                                   return_value=str(ffmpeg),create=True),\
                 mock.patch.object(quality.subprocess,'run',side_effect=fake_run):
                result=quality.export_highlight_clips(source,clips,root/'Export')
            self.assertEqual(len(result['videos']),2)
            self.assertEqual(source.read_bytes(),b'Original never changes')
            self.assertTrue(all(Path(x).is_file() for x in result['videos']))
            self.assertTrue(all('-map' in x and '0:a?' in x for x in logs))
            self.assertFalse(any('concat' in x for command in logs for x in command))
            self.assertTrue(result['original_unchanged'])

    def test_export_refuses_invalid_cuts(self):
        with tempfile.TemporaryDirectory() as tmp:
            video=Path(tmp)/'video.mp4'
            video.write_bytes(b'input')
            with self.assertRaisesRegex(ValueError,'Select 1'):
                quality.export_highlight_clips(video,[],Path(tmp)/'export')
            with mock.patch.object(quality.engine,'get_ffmpeg',
                                   return_value='ffmpeg',create=True):
                with self.assertRaisesRegex(ValueError,'Invalid highlight'):
                    quality.export_highlight_clips(
                        video,[{'start':12.,'end':4.}],Path(tmp)/'export')

    def test_ui_does_not_edit_existing_abyss_ocr_or_full_run(self):
        app=(ROOT/'source'/'app.py').read_text(encoding='utf-8')
        self.assertIn('class HighlightFinderWindow(tk.Toplevel):',app)
        self.assertIn('✦ HIGHLIGHT FINDER (BETA)',app)
        self.assertIn('self._highlight_finder_window=HighlightFinderWindow(self)',app)
        self.assertIn("self._work('scan',lambda:quality_engine.highlight_finder_scan(",app)
        self.assertIn("self._work('export',lambda:quality_engine.export_highlight_clips(",app)
        self.assertIn("'RENDER ONE FULL RUN'",app)
        self.assertIn("read_abyss_start_text(",(
            ROOT/'source'/'quality_engine.py').read_text(encoding='utf-8'))


if __name__=='__main__':
    unittest.main()
