"""Offline AutoDirector beta tests with synthetic OBS-like video frames."""
import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'source'))

# quality_engine normally imports the full editor engine; stub for isolated
# synthetic video tests to keep the release pipeline small and deterministic.
engine = types.ModuleType('engine')
engine.MediaInfo = type('MediaInfo',(object,),{})
sys.modules.setdefault('engine',engine)
spec=importlib.util.spec_from_file_location('cazy_quality_038',BASE/'source'/'quality_engine.py')
quality=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=quality
spec.loader.exec_module(quality)


class DetectionTests(unittest.TestCase):
    def test_genshin_text_classification(self):
        classify=quality._cazy_classify_text
        self.assertEqual(classify('CHALLENGE COMPLETED!'),'success')
        self.assertEqual(classify('Battle Complete'),'success')
        self.assertEqual(classify('Challenge Failed'),'failure')
        self.assertEqual(classify('Challenge started'),'start')
        self.assertIsNone(classify('Damage 123456'))

    def test_saved_render_preset_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'presets.json'
            settings=quality.empty_preferences()
            clean=quality.QualitySettings(preset='Clean',resolution='Source',
                                          fps=0,encoder='Auto / NVIDIA NVENC')
            quality.store_named(settings,'My Abyss RTX 4080',clean)
            quality.save_preferences(settings,path)
            loaded=quality.load_preferences(path)
            self.assertIn('My Abyss RTX 4080',loaded['presets'])
            rebuilt=quality.decode_settings(loaded['presets']['My Abyss RTX 4080'])
            self.assertEqual(rebuilt.resolution,'Source')
            self.assertEqual(rebuilt.fps,0)

    def test_success_reference_detects_synthetic_result(self):
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest('opencv-python-headless not installed')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            video=root/'test_observation.avi'
            image=root/'example.png'
            fourcc=cv2.VideoWriter_fourcc(*'MJPG')
            writer=cv2.VideoWriter(str(video),fourcc,10,(480,270))
            self.assertTrue(writer.isOpened())
            plain=np.zeros((270,480,3),dtype=np.uint8)
            # Distinct screen overlay in last half; reference matches frames
            # exactly, independent of OCR and without any online service.
            success=plain.copy()
            cv2.rectangle(success,(0,0),(479,269),(22,17,14),-1)
            cv2.putText(success,'CHALLENGE COMPLETE',(30,105),
                        cv2.FONT_HERSHEY_SIMPLEX,.85,(75,200,240),2)
            cv2.rectangle(success,(130,118),(350,145),(200,90,20),-1)
            cv2.imwrite(str(image),success)
            for frame in range(100):
                writer.write(plain if frame < 50 else success)
            writer.release()
            self.assertGreater(video.stat().st_size,1024)
            report=quality.cazy_detect_genshin(
                video,reference_path=image,sample_seconds=1)
            self.assertGreater(report['duration'],9)
            self.assertTrue(report['candidates'],report)
            self.assertTrue(any(item['confidence']>.7 for item in report['candidates']))
            self.assertTrue(video.is_file())
            self.assertTrue(image.is_file())

    def test_ui_contains_beta_review_and_preset_reload(self):
        app=(BASE/'source'/'app.py').read_text(encoding='utf-8')
        self.assertIn('def scan_selected(self)',app)
        self.assertIn('def review_results(self, result)',app)
        self.assertIn('def reload_presets(self)',app)
        self.assertIn('def render_reviewed(self, result',app)
        self.assertIn("self.state['success_reference']",app)
        self.assertIn('No YouTube connection or publishing',app)


if __name__=='__main__':
    unittest.main()
