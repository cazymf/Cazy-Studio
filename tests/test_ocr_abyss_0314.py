"""OCR Full Run: actual Genshin UI text, no screenshots, 2s lead-in."""
import os
import shutil
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


class OCRFullRunTests(unittest.TestCase):
    def test_challenge_text_labels(self):
        self.assertEqual(quality.classify_screen_text('Start Challenge'),'start')
        self.assertEqual(quality.classify_screen_text('Start Challnge'),'start')
        self.assertEqual(quality.classify_screen_text('On the other side...'),'switch')
        self.assertEqual(quality.classify_screen_text('Challenge Completed'),'complete')
        self.assertEqual(quality.classify_screen_text('Challenge Failed'),'failure')
        self.assertIsNone(quality.classify_screen_text('Remaining challenge time 420 sec'))

    def test_preroll_is_two_seconds_for_both_halves(self):
        events=[{'at':12,'kind':'start'}, {'at':50,'kind':'switch'},
                {'at':60,'kind':'start'}, {'at':95,'kind':'complete'}]
        result=quality.build_abyss_segments(events,'OBS.mp4',100)
        halves=result['segments']
        self.assertEqual(len(halves),2)
        self.assertEqual(halves[0]['start'],10)
        self.assertEqual(halves[1]['start'],58)
        self.assertEqual(halves[0]['end'],49.85)
        self.assertEqual(halves[1]['end'],97.)

    def test_preroll_never_starts_before_zero(self):
        events=[{'at':1,'kind':'start'}, {'at':22,'kind':'switch'},
                {'at':30,'kind':'start'}, {'at':52,'kind':'complete'}]
        result=quality.build_abyss_segments(events,'OBS.mp4',60)
        self.assertEqual(result['segments'][0]['start'],0)
        self.assertEqual(result['segments'][1]['start'],28)

    def test_text_detection_no_longer_reads_reference_images(self):
        source=(ROOT/'source'/'quality_engine.py').read_text(encoding='utf-8')
        beg=source.index('def scan_abyss_recording(')
        end=source.index('def scan_full_run(',beg)
        scanner=source[beg:end]
        self.assertIn('read_abyss_frame_text',scanner)
        self.assertNotIn('load_reference_bank(',scanner)
        self.assertNotIn('match_reference(',scanner)

    def test_ocr_detection_requires_local_executable(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('numpy unavailable')
        with self.assertRaisesRegex(RuntimeError,'Tesseract'):
            quality.read_abyss_frame_text(
                np.zeros((100,150,3),dtype='uint8'),None)

    def test_real_local_ocr_reads_three_game_text_triggers(self):
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest('opencv unavailable')
        executable=shutil.which('tesseract')
        if not executable:
            self.skipTest('Tesseract not installed')
        cases=[
            ('start','Start Challenge',(495,290),1.05),
            ('switch','On the other side...',(285,210),1.1),
            ('complete','Challenge Completed',(300,212),1.1),
        ]
        for expected,text,xy,size in cases:
            with self.subTest(trigger=expected):
                frame=np.zeros((540,960,3),dtype=np.uint8)
                frame[:]=(20,18,10)
                cv2.putText(frame,text,xy,cv2.FONT_HERSHEY_SIMPLEX,
                            size,(245,245,245),3,cv2.LINE_AA)
                kind,confidence,evidence=quality.read_abyss_frame_text(
                    frame,executable)
                self.assertEqual(kind,expected,evidence)
                self.assertGreater(confidence,.9)
                self.assertIn('OCR text',evidence)

    def test_custom_ocr_executable_path(self):
        with tempfile.TemporaryDirectory() as d:
            program=Path(d)/'tesseract.exe'
            program.write_bytes(b'placeholder')
            self.assertEqual(quality._cazy_tesseract(str(program)),str(program))
            self.assertIsNone(quality._cazy_tesseract(str(Path(d)/'missing.exe')))


if __name__=='__main__':
    unittest.main()
