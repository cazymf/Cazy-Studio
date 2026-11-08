"""Regression tests for saved recipe presentation and full-run scan diagnostics."""
import ast
import os
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


class SavedPresetAndDetectionTests(unittest.TestCase):
    def test_saved_named_preset_is_not_replaced_with_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            location=Path(d)/'render_presets.json'
            options=quality.QualitySettings(
                preset='Ultra',resolution='4K Landscape',fps=120,
                upscale='Real-ESRGAN Anime',frame_method='RIFE AI',
                encoder='HEVC / NVIDIA NVENC',quality='Maximum',
                sharpen=44,denoise=2)
            prefs=quality.empty_preferences()
            quality.store_named(prefs,'My Genshin Quality 2',options)
            quality.save_preferences(prefs,location)
            with mock.patch.object(quality,'preferences_path',return_value=location):
                loaded=quality.resolve_saved_render_preset('My Genshin Quality 2')
                details=quality.describe_saved_render_preset('My Genshin Quality 2')
                self.assertEqual(loaded.resolution,'4K Landscape')
                self.assertEqual(loaded.fps,120)
                self.assertEqual(loaded.frame_method,'RIFE AI')
                self.assertEqual(loaded.sharpen,44)
                self.assertIn('4K Landscape',details)
                self.assertIn('120',details)
                self.assertIn('Real-ESRGAN Anime',details)
                self.assertIn('HEVC / NVIDIA NVENC',details)
                self.assertIn('RIFE AI',details)
                with self.assertRaisesRegex(ValueError,'not found'):
                    quality.resolve_saved_render_preset('Missing Recipe')

    def test_quality_lab_external_load_updates_all_visible_controls(self):
        path=ROOT/'source'/'quality_ui.py'
        tree=ast.parse(path.read_text(encoding='utf-8'))
        gui=next(n for n in tree.body if isinstance(n,ast.ClassDef)
                 and n.name=='QualityLab')
        method=next(n for n in gui.body if isinstance(n,ast.FunctionDef)
                    and n.name=='apply_named_preset')
        namespace={'quality':quality,'messagebox':types.SimpleNamespace(
            showerror=lambda *a,**k: self.fail('Unexpected preset UI error'))}
        exec(compile(ast.Module(body=[method],type_ignores=[]),
                     str(path),'exec'),namespace)
        with tempfile.TemporaryDirectory() as d:
            location=Path(d)/'prefs.json'
            opts=quality.QualitySettings(
                preset='Ultra',resolution='1440p Landscape',fps=240,
                upscale='Real-ESRGAN General',quality='Maximum')
            prefs=quality.empty_preferences()
            quality.store_named(prefs,'Real Saved Settings',opts)
            quality.save_preferences(prefs,location)
            observed={}
            fake=types.SimpleNamespace(
                busy=False,_suspend_autosave=False,prefs={},
                _refresh_saved_presets=lambda:None,
                saved_var=types.SimpleNamespace(
                    set=lambda v:observed.__setitem__('selected',v)),
                _apply_render_settings=lambda recipe:observed.__setitem__(
                    'recipe',recipe),
                status=types.SimpleNamespace(
                    set=lambda text:observed.__setitem__('status',text)))
            with mock.patch.object(quality,'preferences_path',return_value=location):
                namespace['apply_named_preset'](fake,'Real Saved Settings')
            self.assertEqual(observed['selected'],'Real Saved Settings')
            self.assertEqual(observed['recipe'].resolution,'1440p Landscape')
            self.assertEqual(observed['recipe'].fps,240)
            self.assertEqual(observed['recipe'].upscale,'Real-ESRGAN General')
            self.assertFalse(fake._suspend_autosave)

    def test_abyss_side_transition_matches_after_background_color_changes(self):
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest('OpenCV unavailable')
        with tempfile.TemporaryDirectory() as td:
            ref=np.zeros((540,960,3),dtype=np.uint8)
            ref[:]=(40,24,8)
            alternate=ref.copy()
            alternate[:]=(120,24,90)
            for image in (ref,alternate):
                cv2.putText(image,'On the other side...', (300,190),
                            cv2.FONT_HERSHEY_SIMPLEX,1,(255,255,255),2,
                            cv2.LINE_AA)
            location=str(Path(td)/'transition.png')
            cv2.imwrite(location,ref)
            bank=quality.load_reference_bank({'switch':[location]})
            label,score=quality.match_reference(alternate,bank)
            self.assertEqual(label,'switch')
            self.assertGreaterEqual(score,.82)

    def test_all_event_types_are_reported_from_mock_gameplay(self):
        """OCR observations, not reference images, determine the chamber cuts."""
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest('OpenCV unavailable')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            recording=root/'OBS.avi'
            sequence=['start',None,'switch',None,'start',None,
                      'complete',None]
            colors={'start':60,'switch':110,'complete':180,None:0}
            writer=cv2.VideoWriter(str(recording),
                         cv2.VideoWriter_fourcc(*'MJPG'),10,(960,540))
            self.assertTrue(writer.isOpened())
            for kind in sequence:
                frame=np.full((540,960,3),colors[kind],dtype=np.uint8)
                for _ in range(10):
                    writer.write(frame)
            writer.release()
            def fake_ocr(frame,executable,cancel=None):
                val=int(frame[0,0,0])
                if val < 15:
                    return None,0.,''
                kind=min(('start','switch','complete'),
                         key=lambda k:abs(colors[k]-val))
                return kind,.97,'Recognized game text'
            with mock.patch.object(quality,'_cazy_tesseract',return_value='fake-local-ocr'), \
                 mock.patch.object(quality,'read_abyss_frame_text',fake_ocr), \
                 mock.patch.object(quality,'read_abyss_start_text',
                                   return_value=(None,0.,'')):
                result=quality.scan_abyss_recording(
                    recording,{},sample_seconds=1)
            counts=result['event_counts']
            self.assertEqual(counts['start'],2,result)
            self.assertEqual(counts['switch'],1,result)
            self.assertEqual(counts['complete'],1,result)
            self.assertEqual(result['completed_chambers'],1,result)
            self.assertEqual(len(result['segments']),2,result)
            self.assertEqual(result['segments'][0]['start'],0.0)
            self.assertEqual(result['segments'][1]['start'],2.0)
            self.assertEqual(result['detection_method'],'Local text OCR')


if __name__=='__main__':
    unittest.main()
