"""FFmpeg crash diagnostics: blank stderr must never produce a blank error."""
import ast
import os
import sys
import tempfile
import traceback
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


class RenderCrashLoggingTests(unittest.TestCase):
    def test_no_stderr_still_displays_exit_code_and_saves_command(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.dict(os.environ,{'LOCALAPPDATA':folder}):
                command=[sys.executable,'-c','import sys;sys.exit(23)']
                with self.assertRaisesRegex(RuntimeError,'0x00000017') as failed:
                    q._run(command)
                error=str(failed.exception)
                self.assertIn('exited with code 23',error)
                self.assertIn('Error log:',error)
                self.assertNotEqual(error.strip(),'Processing failed:')
                diagnostic=q._cazy_render_error_log_path()
                self.assertTrue(diagnostic.is_file())
                content=diagnostic.read_text(encoding='utf-8')
                self.assertIn('exited with code 23',content)
                self.assertIn('Command:',content)
                self.assertIn('External process stderr: (empty',content)

    def test_stderr_preserved_in_message_and_local_log(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.dict(os.environ,{'LOCALAPPDATA':folder}):
                script="import sys; print('broken video filter',file=sys.stderr); sys.exit(9)"
                with self.assertRaisesRegex(RuntimeError,'broken video filter') as failed:
                    q._run([sys.executable,'-c',script])
                self.assertIn('exit',str(failed.exception))
                content=q._cazy_render_error_log_path().read_text(encoding='utf-8')
                self.assertIn('broken video filter',content)

    def test_autodirector_handles_exceptions_without_a_message(self):
        source=(ROOT/'source'/'app.py').read_text(encoding='utf-8')
        tree=ast.parse(source)
        ui=next(x for x in tree.body if isinstance(x,ast.ClassDef) and
                x.name=='AutoDirectorView')
        method=next(x for x in ui.body if isinstance(x,ast.FunctionDef) and
                    x.name=='_auto_error_text')
        ns={'quality_engine':q,'traceback':traceback}
        exec(compile(ast.Module(body=[method],type_ignores=[]),
                     'app.py','exec'),ns)
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.dict(os.environ,{'LOCALAPPDATA':folder}):
                try:
                    raise RuntimeError('')
                except RuntimeError as exc:
                    message=ns['_auto_error_text'](None,exc)
                self.assertIn('RuntimeError (no error message)',message)
                self.assertIn('Error log:',message)
                saved=q._cazy_render_error_log_path().read_text()
                self.assertIn('Python traceback:',saved)

    def test_error_log_button_is_real_and_persists_after_popup(self):
        source=(ROOT/'source'/'app.py').read_text(encoding='utf-8')
        self.assertIn("self._button(bottom, 'Error Log', self.open_last_render_error)",source)
        self.assertIn("def open_last_render_error(self):",source)
        self.assertIn("os.startfile(str(path))",source)
        self.assertIn("self._auto_error_text(exc)",source)


if __name__=='__main__':
    unittest.main()
