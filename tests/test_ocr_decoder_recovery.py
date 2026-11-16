"""Regression coverage for isolated OCR decoder recovery."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
import quality_engine as q


class OCRDecoderRecoveryTests(unittest.TestCase):
    def test_compatible_decoder_precedes_bundled_on_windows(self):
        with tempfile.TemporaryDirectory() as folder:
            compatible = Path(folder) / 'compatible.exe'
            compatible.touch()
            with patch.object(q, '_legacy_ffmpeg_cache_path', return_value=compatible), \
                    patch.object(q.engine, 'get_ffmpeg', return_value='bundled.exe', create=True), \
                    patch.object(q.shutil, 'which', return_value=None):
                candidates = q._cazy_ocr_decoder_candidates()
            if os.name == 'nt':
                self.assertEqual(candidates, [str(compatible), 'bundled.exe'])

    @unittest.skipUnless(os.name == 'nt', 'Windows compatibility recovery')
    def test_crashed_decoder_recovers_using_downloaded_build(self):
        with tempfile.NamedTemporaryFile() as recording, \
                patch.object(q, '_cazy_tesseract', return_value='tesseract.exe'), \
                patch.object(q, '_cazy_ocr_decoder_candidates', return_value=['broken.exe']), \
                patch.object(q, '_ensure_legacy_ffmpeg', return_value='compatible.exe') as setup, \
                patch.object(q, '_cazy_scan_abyss_external', side_effect=[
                    q._CazyOCRDecoderFailure('0xC0000005'), {'events': []}]) as scan:
            self.assertEqual(q.scan_abyss_recording(recording.name), {'events': []})
            self.assertEqual([call.args[2] for call in scan.call_args_list],
                             ['broken.exe', 'compatible.exe'])
            setup.assert_called_once()

    @unittest.skipUnless(os.name == 'nt', 'Windows compatibility recovery')
    def test_failed_setup_preserves_decoder_error(self):
        with tempfile.NamedTemporaryFile() as recording, \
                patch.object(q, '_cazy_tesseract', return_value='tesseract.exe'), \
                patch.object(q, '_cazy_ocr_decoder_candidates', return_value=['broken.exe']), \
                patch.object(q, '_ensure_legacy_ffmpeg', side_effect=OSError('offline')), \
                patch.object(q, '_cazy_scan_abyss_external',
                             side_effect=q._CazyOCRDecoderFailure('0xC0000005')):
            with self.assertRaises(RuntimeError) as error:
                q.scan_abyss_recording(recording.name)
            self.assertIn('0xC0000005', str(error.exception))
            self.assertIn('offline', str(error.exception))

    def test_cancellation_does_not_start_another_decoder(self):
        import threading
        cancel = threading.Event()
        def fail(*args):
            cancel.set()
            raise q._CazyOCRDecoderFailure('decoder stopped')
        with tempfile.NamedTemporaryFile() as recording, \
                patch.object(q, '_cazy_tesseract', return_value='tesseract.exe'), \
                patch.object(q, '_cazy_ocr_decoder_candidates', return_value=['broken.exe']), \
                patch.object(q, '_ensure_legacy_ffmpeg') as setup, \
                patch.object(q, '_cazy_scan_abyss_external', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'Scan cancelled'):
                q.scan_abyss_recording(recording.name, cancel=cancel)
            setup.assert_not_called()


if __name__ == '__main__':
    unittest.main()
