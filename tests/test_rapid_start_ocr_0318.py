"""Regression: short Start Challenge prompts in every Abyss chamber and side."""
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


class RapidStartOCRTests(unittest.TestCase):
    def test_focused_start_ocr_reads_real_prompt_text(self):
        import cv2
        import numpy as np
        executable=shutil.which('tesseract')
        if not executable:
            self.skipTest('Tesseract is unavailable')
        frame=np.full((540,960,3),(43,35,24),dtype=np.uint8)
        cv2.rectangle(frame,(515,247),(795,302),(29,27,26),-1)
        cv2.putText(frame,'Start Challenge',(530,285),
                    cv2.FONT_HERSHEY_SIMPLEX,1.,(255,255,255),3,cv2.LINE_AA)
        for threshold in (False,True):
            with self.subTest(threshold=threshold):
                kind,confidence,evidence=quality.read_abyss_start_text(
                    frame,executable,threshold=threshold)
                self.assertEqual(kind,'start',evidence)
                self.assertGreater(confidence,.9)

    def test_rapid_start_checks_do_not_spawn_windows_console(self):
        import numpy as np
        fake=types.SimpleNamespace(returncode=0,stdout=b'Start Challenge\n',
                                   stderr=b'')
        frame=np.zeros((540,960,3),dtype=np.uint8)
        with mock.patch.object(quality,'_cazy_subprocess_flags',
                               return_value=0x08000000),\
             mock.patch.object(quality.subprocess,'run',return_value=fake) as called:
            event=quality.read_abyss_start_text(frame,'tesseract.exe')
        self.assertEqual(event[0],'start')
        self.assertEqual(called.call_args.kwargs['creationflags'],0x08000000)

    def test_two_chambers_see_all_four_brief_start_prompts(self):
        import cv2
        import numpy as np
        executable=shutil.which('tesseract')
        if not executable:
            self.skipTest('Tesseract is unavailable')
        with tempfile.TemporaryDirectory() as folder:
            video=Path(folder)/'short_prompts.avi'
            fps=20
            length=18.5
            writer=cv2.VideoWriter(str(video),
                cv2.VideoWriter_fourcc(*'MJPG'),fps,(960,540))
            self.assertTrue(writer.isOpened())
            def between(t,lo,hi):
                return lo<=t<hi
            for number in range(int(length*fps)):
                t=number/fps
                frame=np.full((540,960,3),(43,35,24),dtype=np.uint8)
                if (between(t,1.05,1.30) or between(t,4.75,5.0)
                        or between(t,9.35,9.6) or between(t,13.05,13.3)):
                    cv2.rectangle(frame,(515,247),(795,302),(29,27,26),-1)
                    cv2.putText(frame,'Start Challenge',(530,285),
                        cv2.FONT_HERSHEY_SIMPLEX,1.,(255,255,255),3,cv2.LINE_AA)
                elif between(t,3.0,3.65) or between(t,11.0,11.65):
                    frame[:]=0
                    cv2.putText(frame,'On the other side...', (278,280),
                        cv2.FONT_HERSHEY_SIMPLEX,1.,(255,255,255),3,cv2.LINE_AA)
                elif between(t,6.6,8.) or between(t,15.,17.):
                    frame[:]=(20,16,11)
                    cv2.putText(frame,'Challenge Completed',(265,190),
                        cv2.FONT_HERSHEY_SIMPLEX,1.,(255,255,255),3,cv2.LINE_AA)
                writer.write(frame)
            writer.release()
            result=quality.scan_abyss_recording(video,ocr_path=executable)
            counts=result['event_counts']
            self.assertEqual(counts['start'],4,result)
            self.assertEqual(counts['switch'],2,result)
            self.assertEqual(counts['complete'],2,result)
            self.assertEqual(result['completed_chambers'],2,result)
            self.assertEqual(len(result['segments']),4,result)
            for item in result['segments']:
                self.assertNotIn('start inferred',item['evidence'].lower())
            # Actual prompt appearances are within a quarter second and both
            # sides begin exactly two seconds before their recognized events.
            segments=result['segments']
            self.assertAlmostEqual(segments[0]['start'],0.,delta=.4)
            self.assertAlmostEqual(segments[1]['start'],2.75,delta=.5)
            self.assertAlmostEqual(segments[2]['start'],7.35,delta=.5)
            self.assertAlmostEqual(segments[3]['start'],11.05,delta=.5)


if __name__=='__main__':
    unittest.main()
