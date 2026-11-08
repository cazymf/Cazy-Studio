"""NVENC legacy driver regression and exact high-FPS preset preservation."""
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'source'))
engine=sys.modules.setdefault('engine',types.ModuleType('engine'))
if not hasattr(engine,'MediaInfo'):
    engine.MediaInfo=type('MediaInfo',(object,),{})
import quality_engine as q


class LegacyDriverNVENCTests(unittest.TestCase):
    def setUp(self):
        q._NVENC_PROBE_CACHE.clear()

    def test_codec_selection_respects_saved_preset(self):
        self.assertEqual(q._nvenc_codec('Auto / NVIDIA NVENC'),'h264_nvenc')
        self.assertEqual(q._nvenc_codec('HEVC / NVIDIA NVENC'),'hevc_nvenc')
        self.assertEqual(q._nvenc_codec('AV1 / NVIDIA NVENC'),'av1_nvenc')
        self.assertIsNone(q._nvenc_codec('CPU x264'))

    def test_nvenc_probe_actually_encodes_two_frames_not_just_lists_encoders(self):
        fake=types.SimpleNamespace(returncode=0,stderr=b'')
        with mock.patch.object(q.subprocess,'run',return_value=fake) as called:
            ok,reason=q._probe_nvenc('ffmpeg.exe','h264_nvenc')
        self.assertTrue(ok)
        cmd=called.call_args.args[0]
        self.assertIn('-frames:v',cmd)
        self.assertEqual(cmd[cmd.index('-frames:v')+1],'2')
        self.assertIn('h264_nvenc',cmd)
        self.assertEqual(cmd[cmd.index('-f')+1],'lavfi')

    def test_nvenc_supported_no_download_and_cache(self):
        with mock.patch.object(q,'_probe_nvenc',return_value=(True,'')) as probe, \
             mock.patch.object(q,'_ensure_legacy_ffmpeg') as downloader:
            result=q._select_nvenc_ffmpeg('modern.exe','Auto / NVIDIA NVENC')
            again=q._select_nvenc_ffmpeg('modern.exe','Auto / NVIDIA NVENC')
        self.assertEqual(result,again)
        self.assertEqual(probe.call_count,1)
        self.assertEqual(result[0],'modern.exe')
        self.assertTrue(result[1])
        downloader.assert_not_called()

    def test_nvenc_api_mismatch_selects_compatible_ffmpeg(self):
        old='legacy-ffmpeg-7.1.exe'
        responses=[(False,'Driver does not support the required nvenc API version. Required: 13.1 Found: 13.0'),
                   (True,'')]
        with mock.patch.object(q.os,'name','nt'), \
             mock.patch.object(q,'_probe_nvenc',side_effect=responses) as probe, \
             mock.patch.object(q,'_ensure_legacy_ffmpeg',return_value=old) as downloader:
            actual=q._select_nvenc_ffmpeg('ffmpeg-9.exe','HEVC / NVIDIA NVENC')
        self.assertEqual(actual[0],old)
        self.assertTrue(actual[1])
        self.assertIn('Compatible NVENC',actual[2])
        self.assertEqual(probe.call_count,2)
        self.assertEqual(probe.call_args.args, (old,'hevc_nvenc'))
        downloader.assert_called_once()

    def test_cpu_fallback_displays_reason_after_compatibility_failure(self):
        responses=[(False,'required NVENC API version 13.1'),
                   (False,'No device available for NVENC')]
        logs=[]
        with mock.patch.object(q.os,'name','nt'), \
             mock.patch.object(q,'_probe_nvenc',side_effect=responses), \
             mock.patch.object(q,'_ensure_legacy_ffmpeg',return_value='legacy.exe'):
            selected=q._select_nvenc_ffmpeg('modern.exe','Auto / NVIDIA NVENC',status=logs.append)
        self.assertFalse(selected[1])
        self.assertEqual(selected[0],'modern.exe')
        self.assertIn('Compatible FFmpeg 7.1',selected[2])
        self.assertTrue(any('CPU fallback' in note for note in logs))

    def test_non_driver_gpu_failure_does_not_download(self):
        with mock.patch.object(q,'_probe_nvenc',return_value=(False,'GPU device busy')), \
             mock.patch.object(q,'_ensure_legacy_ffmpeg') as downloader:
            _,supported,reason=q._select_nvenc_ffmpeg(
                'modern.exe','AV1 / NVIDIA NVENC')
        self.assertFalse(supported)
        self.assertIn('GPU device busy',reason)
        downloader.assert_not_called()

    def test_pinned_legacy_release_and_checksum(self):
        self.assertIn('imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl',q._LEGACY_FFMPEG_WHEEL_URL)
        self.assertEqual(len(q._LEGACY_FFMPEG_WHEEL_SHA256),64)
        self.assertEqual(q._LEGACY_FFMPEG_FILENAME,'ffmpeg-win-x86_64-v7.1.exe')

    def test_optical_flow_240_fps_is_not_silently_reduced(self):
        opts=q.QualitySettings(preset='Ultra',resolution='1080p Landscape',
            fps=240,frame_method='Optical Flow',encoder='Auto / NVIDIA NVENC')
        info=types.SimpleNamespace(width=1920,height=1080,fps=60,duration=30,
                                   kind='video',has_audio=True)
        filters=q._filter_chain(info,opts)
        self.assertIn('minterpolate=fps=240',','.join(filters))
        self.assertEqual(q.output_specs(info,opts)[-1],240)
        opts.frame_method='RIFE AI'
        self.assertNotIn('minterpolate',','.join(q._filter_chain(info,opts)))


if __name__=='__main__':
    unittest.main()
