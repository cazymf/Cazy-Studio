"""Regression: Quality Lab export has a real, foreground, live progress window."""
import ast
import os
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
QUALITY=ROOT/'source'/'quality_ui.py'
APP=ROOT/'source'/'app.py'


class ExportProgressSourceTests(unittest.TestCase):
    def test_quality_export_starts_live_window_and_can_restore_it(self):
        code=QUALITY.read_text(encoding='utf-8')
        ast.parse(code)
        for expected in (
            'def _show_render_window(self):',
            "'Show Render Window',self._show_render_window",
            'self.render_popup_progress.set(0.)',
            'self._show_render_window()',
            "win.protocol('WM_DELETE_WINDOW',win.withdraw)",
            'self.render_window.deiconify()',
            'self.render_window.lift()',
            "self.render_popup_title.set(f'{pct:.1f}%  •  Rendering')",
            'self.render_popup_stage.set(f',
            "self._finish_render_window('Saved MP4: '+m[1],successful=True)",
        ):
            self.assertIn(expected,code)

    def test_editor_export_window_is_explicitly_foreground_and_shows_pct(self):
        code=APP.read_text(encoding='utf-8')
        ast.parse(code)
        for expected in (
            'self.export_window.lift()',
            'self.export_window.focus_set()',
            'self.export_window.attributes(',
            "self.export_message.config(text=f'{pct:.1f}%  •  {desc[:65]}')",
            'self.export_bar',
        ):
            self.assertIn(expected,code)


if __name__=='__main__':
    unittest.main()
