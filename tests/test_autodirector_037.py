"""Regression tests for Cazy Studio AutoDirector's explicit local video library.

The production app imports optional Windows/editor dependencies, so these tests
load its pure queue helpers directly from the parsed application source.
"""
import ast
import json
import os
import random
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / 'source' / 'app.py'
VERSION = Path(__file__).resolve().parents[1] / 'source' / 'version.py'
HELPERS = {
    '_autodirector_path', '_autodirector_defaults', '_autodirector_validate',
    '_autodirector_load', '_autodirector_save', '_autodirector_add',
    '_autodirector_tracks', '_autodirector_prepare'
}


def load_helpers():
    root = ast.parse(SOURCE.read_text(encoding='utf-8'))
    picked = [node for node in root.body
              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
              and node.name in HELPERS]
    ns = {'os': os, 'Path': Path, 'tempfile': tempfile, 'json': json,
          'random': random, '_AUTODIRECTOR_MAX_JOBS': 500,
          '_AUTODIRECTOR_VIDEO_EXTS': {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v'},
          '_AUTODIRECTOR_MUSIC_EXTS': {'.mp3', '.wav', '.m4a', '.flac', '.ogg', '.aac'},
          '_AUTODIRECTOR_MODES': ('Best Successful Attempts', 'Full Run')}
    code = compile(ast.Module(body=picked, type_ignores=[]), str(SOURCE), 'exec')
    exec(code, ns)
    return ns


class AutoDirectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_helpers()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.ns['_autodirector_defaults']()

    def test_only_explicitly_chosen_videos_are_added(self):
        selected = self.root / 'wanted.mp4'
        unwanted = self.root / 'not_selected.mp4'
        selected.write_bytes(b'example video')
        unwanted.write_bytes(b'unselected')
        add = self.ns['_autodirector_add']
        self.assertEqual(add(self.state, [selected]), 1)
        self.assertEqual(add(self.state, [selected]), 0)
        self.assertEqual(len(self.state['queue']), 1)
        self.assertEqual(Path(self.state['queue'][0]['path']).name, 'wanted.mp4')
        self.assertTrue(unwanted.exists())
        self.assertEqual(unwanted.read_bytes(), b'unselected')

    def test_nonvideo_and_missing_paths_are_rejected(self):
        note = self.root / 'notes.txt'
        note.write_text('hello')
        self.assertEqual(self.ns['_autodirector_add'](
            self.state, [note, self.root / 'missing.mp4']), 0)

    def test_queue_persistence_and_order(self):
        paths = [self.root / name for name in ('two.mkv', 'one.mp4')]
        for file in paths:
            file.write_bytes(b'footage')
        self.assertEqual(self.ns['_autodirector_add'](self.state, paths), 2)
        save_path = self.root / 'queue.json'
        self.ns['_autodirector_save'](self.state, save_path)
        loaded = self.ns['_autodirector_load'](save_path)
        self.assertEqual([Path(j['path']).name for j in loaded['queue']],
                         ['two.mkv', 'one.mp4'])
        self.assertEqual(loaded['schema'], 1)

    def test_song_shuffle_prevents_adjacent_repeats(self):
        for n in ('a.mp3', 'b.mp3'):
            (self.root / n).write_bytes(b'music')
        for n in ('1.mp4', '2.mp4', '3.mp4'):
            (self.root / n).write_bytes(b'video')
        self.ns['_autodirector_add'](
            self.state, [self.root / f'{n}.mp4' for n in (1, 2, 3)])
        self.state['music_folder'] = str(self.root)
        self.assertEqual(self.ns['_autodirector_prepare'](
            self.state, chooser=lambda choices: choices[0]), 3)
        songs = [j['music'] for j in self.state['queue']]
        self.assertNotEqual(songs[0], songs[1])
        self.assertNotEqual(songs[1], songs[2])

    def test_library_is_local_only_and_has_no_folder_import_button(self):
        src = SOURCE.read_text(encoding='utf-8')
        ui = src.split('class AutoDirectorView(tk.Frame):', 1)[1]
        self.assertIn("'+ ADD VIDEOS'", ui)
        self.assertIn('preview_selected', ui)
        self.assertIn('clear_all', ui)
        self.assertIn('MISSING FILE', ui)
        self.assertNotIn('self.visibility_var', ui)
        self.assertNotIn("self.add_folder", ui)
        self.assertIn('No YouTube connection or publishing', ui)

    def test_version_bump_and_valid_python_ast(self):
        self.assertIn("'0.3.10'", VERSION.read_text(encoding='utf-8'))
        ast.parse(SOURCE.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
