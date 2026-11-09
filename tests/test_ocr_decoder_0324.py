"""v0.3.24: OCR decoding must not crash Cazy Studio's GUI process."""
import contextlib
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'source'))
engine = sys.modules.setdefault('engine', types.ModuleType('engine'))
if not hasattr(engine, 'MediaInfo'):
    engine.MediaInfo = type('MediaInfo', (object,), {})
import quality_engine as quality


class IsolatedOCRDecoderTests(unittest.TestCase):
    def test_native_videoio_is_not_used_by_ocr_or_thumbnail(self):
        source = (ROOT/'source'/'quality_engine.py').read_text(encoding='utf-8')
        scanner = source[source.index('def scan_abyss_recording('):
                         source.index('def scan_full_run(')]
        self.assertNotIn('VideoCapture(', scanner)
        self.assertIn('_cazy_scan_abyss_external', scanner)
        thumbnail = source[source.index('def render_full_run('):]
        self.assertNotIn('VideoCapture(', thumbnail)
        self.assertIn('image2pipe', thumbnail)
        self.assertIn('thumbnail.convert("RGB")', thumbnail)

    def test_unavailable_decoder_is_caught_as_recoverable_python_error(self):
        with mock.patch.object(quality.subprocess, 'Popen',
                               side_effect=OSError('simulated decoder launch crash')):
            with self.assertRaisesRegex(quality._CazyOCRDecoderFailure,
                                        'Could not start FFmpeg OCR decoder'):
                list(quality._cazy_ocr_frame_stream(
                    'dummy.mp4', 'broken-ffmpeg.exe', 960, 540, 8))

    def test_external_decoder_produces_real_frames(self):
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest('OpenCV and NumPy are unavailable')
        program = shutil.which('ffmpeg')
        if not program:
            self.skipTest('FFmpeg is unavailable')
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder)/'frames.avi'
            writer = cv2.VideoWriter(str(video),
                cv2.VideoWriter_fourcc(*'MJPG'), 20, (960, 540))
            self.assertTrue(writer.isOpened())
            for n in range(40):
                image = np.full((540,960,3), n%180, dtype=np.uint8)
                writer.write(image)
            writer.release()
            with contextlib.closing(quality._cazy_ocr_frame_stream(
                    video, program, 960, 540, 8)) as frames:
                sample = list(frames)
            self.assertGreaterEqual(len(sample), 15)
            self.assertEqual(sample[0][1].shape, (540, 960, 3))
            self.assertAlmostEqual(sample[1][0], .125)

    def test_short_abyss_prompts_and_two_second_preroll(self):
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest('OpenCV and NumPy are unavailable')
        tesseract = shutil.which('tesseract')
        ffmpeg = shutil.which('ffmpeg')
        if not tesseract or not ffmpeg:
            self.skipTest('Local OCR/FFmpeg tools unavailable')
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder)/'spiral_abyss.avi'
            writer = cv2.VideoWriter(str(video),
                cv2.VideoWriter_fourcc(*'MJPG'), 20, (960, 540))
            self.assertTrue(writer.isOpened())
            for number in range(165):
                second = number/20
                frame = np.full((540,960,3),(43,35,24),dtype=np.uint8)
                if (1.05<=second<1.5 or 4.25<=second<4.75):
                    cv2.rectangle(frame,(515,247),(795,302),(29,27,26),-1)
                    cv2.putText(frame,'Start Challenge',(530,285),
                        cv2.FONT_HERSHEY_SIMPLEX,1.,(255,255,255),3,cv2.LINE_AA)
                elif 2.75<=second<3.4:
                    frame[:]=0
                    cv2.putText(frame,'On the other side...', (278,280),
                        cv2.FONT_HERSHEY_SIMPLEX,1.,(255,255,255),3,cv2.LINE_AA)
                elif 6.1<=second<7.5:
                    frame[:]=(20,16,11)
                    cv2.putText(frame,'Challenge Completed',(265,190),
                        cv2.FONT_HERSHEY_SIMPLEX,1.03,(255,255,255),3,cv2.LINE_AA)
                writer.write(frame)
            writer.release()
            with mock.patch.object(cv2,'VideoCapture',
                                   side_effect=AssertionError('Native videoio used')):
                report=quality.scan_abyss_recording(video,ocr_path=tesseract)
            self.assertEqual(report['completed_chambers'],1,report)
            self.assertEqual(report['event_counts']['start'],2,report)
            self.assertEqual(report['event_counts']['switch'],1,report)
            self.assertEqual(report['event_counts']['complete'],1,report)
            self.assertEqual(len(report['segments']),2,report)
            self.assertAlmostEqual(report['segments'][0]['start'],0.,delta=.5)
            self.assertAlmostEqual(report['segments'][1]['start'],2.25,delta=.5)
            self.assertEqual(report['detection_method'],
                             'Local text OCR (external decoder)')


if __name__=='__main__':
    unittest.main()
