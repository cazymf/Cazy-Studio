"""OCR scan regression: short transition screens, progress, real-time event counts."""
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


class OCRScanProgressTests(unittest.TestCase):
    def test_real_black_transition_and_success_ocr(self):
        import cv2
        import numpy as np
        program=shutil.which('tesseract')
        if not program:
            self.skipTest('Tesseract not installed')
        transition=np.zeros((540,960,3),dtype=np.uint8)
        cv2.putText(transition,'On the other side...', (278,280),
                    cv2.FONT_HERSHEY_SIMPLEX,.95,(245,245,245),2,cv2.LINE_AA)
        profile=quality._abyss_screen_profile(transition,cv2)
        self.assertTrue(profile['transition'],profile)
        label,confidence,evidence=quality.read_abyss_frame_text(
            transition,program)
        self.assertEqual(label,'switch',evidence)

        success=np.zeros((540,960,3),dtype=np.uint8)
        success[:]=(20,16,11)
        cv2.putText(success,'Challenge Completed',(265,190),
                    cv2.FONT_HERSHEY_SIMPLEX,1.03,(240,240,240),3,cv2.LINE_AA)
        label,confidence,evidence=quality.read_abyss_frame_text(
            success,program)
        self.assertEqual(label,'complete',evidence)

    def test_dark_blank_loading_screen_is_not_an_event(self):
        import cv2
        import numpy as np
        black=np.zeros((540,960,3),dtype=np.uint8)
        profile=quality._abyss_screen_profile(black,cv2)
        self.assertTrue(profile['black_blank'])
        self.assertFalse(profile['transition'])

    def test_short_half_second_transition_is_sampled_and_progress_reported(self):
        """Completes one chamber from a very short 0.6-second side switch."""
        import cv2
        import numpy as np
        program=shutil.which('tesseract')
        if not program:
            self.skipTest('Tesseract not installed')
        with tempfile.TemporaryDirectory() as directory:
            recording=Path(directory)/'OBS_Complete_Abyss.avi'
            writer=cv2.VideoWriter(str(recording),
                       cv2.VideoWriter_fourcc(*'MJPG'),10,(960,540))
            self.assertTrue(writer.isOpened())
            for frame_number in range(135):
                second=frame_number/10
                frame=np.zeros((540,960,3),dtype=np.uint8)
                if 1.0<=second<2.4:
                    frame[:]=(45,37,27)
                    cv2.putText(frame,'Start Challenge',(530,285),
                        cv2.FONT_HERSHEY_SIMPLEX,1.0,(255,255,255),3,cv2.LINE_AA)
                elif 3.0<=second<3.6:
                    cv2.putText(frame,'On the other side...', (278,280),
                        cv2.FONT_HERSHEY_SIMPLEX,1.0,(255,255,255),3,cv2.LINE_AA)
                elif 5.0<=second<6.5:
                    frame[:]=(45,37,27)
                    cv2.putText(frame,'Start Challenge',(530,285),
                        cv2.FONT_HERSHEY_SIMPLEX,1.0,(255,255,255),3,cv2.LINE_AA)
                elif 10.0<=second<12.3:
                    frame[:]=(20,16,11)
                    cv2.putText(frame,'Challenge Completed',(265,190),
                        cv2.FONT_HERSHEY_SIMPLEX,1.1,(255,255,255),3,cv2.LINE_AA)
                else:
                    frame[:]=(36,25,18)
                writer.write(frame)
            writer.release()
            reports=[]
            done=[]
            output=quality.scan_full_run(
                [str(recording)],{},ocr_path=program,
                progress=lambda fraction:done.append(fraction),
                detail=lambda data:reports.append(data))
            counts=output['event_counts']
            self.assertGreaterEqual(counts['start'],2,counts)
            self.assertGreaterEqual(counts['switch'],1,counts)
            self.assertGreaterEqual(counts['complete'],1,counts)
            self.assertEqual(len(output['segments']),2,output)
            self.assertEqual(output['completed_chambers'],1,output)
            self.assertTrue(done)
            self.assertEqual(done[-1],1.)
            self.assertTrue(reports)
            self.assertEqual(reports[-1]['filename'],recording.name)
            self.assertEqual(reports[-1]['video_index'],1)
            self.assertEqual(reports[-1]['total_videos'],1)
            self.assertGreater(reports[-1]['fraction'],.9)
            self.assertIn('Switch',reports[-1]['found'])
            self.assertAlmostEqual(output['segments'][0]['start'],0,
                                   delta=.81)
            self.assertAlmostEqual(output['segments'][1]['start'],3.0,
                                   delta=.81)

    def test_progress_panel_is_visible_next_to_scan_controls(self):
        source=(ROOT/'source'/'app.py').read_text(encoding='utf-8')
        self.assertIn("SCAN STATUS: Ready",source)
        self.assertIn("variable=self.scan_progress_var",source)
        self.assertIn("elif event == 'scan_detail':",source)
        self.assertIn("Events found:",source)
        self.assertIn("self.scan_state_var.set('SCAN COMPLETE",source)
        self.assertIn("detail=lambda info:self._auto_messages.put(('scan_detail',info))",source)


if __name__=='__main__':
    unittest.main()
