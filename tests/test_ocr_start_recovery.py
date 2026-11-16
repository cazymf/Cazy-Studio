"""Keep completed chamber pairs when brief Start Challenge prompts are missed."""
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
engine = sys.modules.setdefault('engine', types.ModuleType('engine'))
if not hasattr(engine, 'MediaInfo'):
    engine.MediaInfo = type('MediaInfo', (object,), {})
import quality_engine as q


class StartRecoveryTests(unittest.TestCase):
    def scan(self, times, timers, overlays):
        def read_text(frame, *args, **kwargs):
            kind = overlays.get(frame)
            return kind, .97 if kind else 0., 'OCR overlay' if kind else ''
        profile = {'black_blank': False, 'transition': False, 'result': False}
        with patch.dict(sys.modules, {'cv2': types.ModuleType('cv2')}), \
                patch.object(q, '_cazy_ocr_video_info', return_value=(162.87, 1600, 900)), \
                patch.object(q, '_cazy_ocr_frame_stream',
                             side_effect=lambda *args: iter((t, t) for t in times)), \
                patch.object(q, '_abyss_screen_profile', return_value=profile), \
                patch.object(q, 'read_abyss_start_text', return_value=(None, 0., '')), \
                patch.object(q, 'read_abyss_frame_text', side_effect=read_text), \
                patch.object(q, '_cazy_read_challenge_timer',
                             side_effect=lambda frame, *args: timers.get(frame)):
            return q._cazy_scan_abyss_external(
                'recording.mp4', 'tesseract', 'ffmpeg', None, None, .8, None)

    def test_three_completed_chambers_with_missed_prompts_produce_six_halves(self):
        times = [5,6,26,30,31,44,52,53,69,74,75,93,101,102,123,127,128,155]
        timers = {5:599,6:598,30:578,31:577,52:599,53:598,
                  74:579,75:578,101:599,102:598,127:577,128:576}
        report = self.scan(times, timers, {
            26:'switch',44:'complete',69:'switch',93:'complete',
            123:'switch',155:'complete'})
        self.assertEqual(report['completed_chambers'], 3)
        self.assertEqual(len(report['segments']), 6)
        self.assertEqual(report['event_counts']['start'], 6)
        self.assertEqual([s['chamber'] for s in report['segments']], [1,1,2,2,3,3])
        self.assertEqual(report['segments'][0]['start'], 3.)
        self.assertTrue(all(s['review_required'] for s in report['segments']))

    def test_frozen_timer_cannot_create_a_start(self):
        report = self.scan([5,6,7,8,26,44], {5:599,6:599,7:599,8:599},
                           {26:'switch',44:'complete'})
        self.assertEqual(report['event_counts']['start'], 0)
        self.assertEqual(report['segments'], [])

    def test_unrelated_or_stale_timer_readings_cannot_create_a_start(self):
        report = self.scan([5,6,12,26,44], {5:599,6:550,12:549},
                           {26:'switch',44:'complete'})
        self.assertEqual(report['event_counts']['start'], 0)
        self.assertEqual(report['segments'], [])

    def test_failed_chamber_is_still_discarded(self):
        report = self.scan([5,6,26,30,31,40], {5:599,6:598,30:578,31:577},
                           {26:'switch',40:'failure'})
        self.assertEqual(report['segments'], [])
        self.assertEqual(report['discarded_attempts'], 1)


if __name__ == '__main__':
    unittest.main()
