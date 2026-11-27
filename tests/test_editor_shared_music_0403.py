"""Shared editor/AutoDirector music regression tests (no media/network needed)."""
import ast
import os
import random
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

APP=Path(__file__).resolve().parents[1]/'source'/'app.py'
TREE=ast.parse(APP.read_text(encoding='utf-8'))
NAMES={'_autodirector_tracks','_editor_choose_random_track'}
studio=next(x for x in TREE.body if isinstance(x,ast.ClassDef) and x.name=='Studio')
hunter=next(x for x in TREE.body if isinstance(x,ast.ClassDef) and x.name=='MusicHunterWindow')
methods={'_shared_music_folder','pick_random_music','open_editor_music_hunter','_set_editor_music'}
nodes=[x for x in TREE.body if isinstance(x,ast.FunctionDef) and x.name in NAMES]
nodes += [x for x in studio.body if isinstance(x,ast.FunctionDef) and x.name in methods]
ns={'Path':Path,'os':os,'random':random,'_AUTODIRECTOR_MUSIC_EXTS':{'.mp3','.wav','.m4a','.flac','.ogg','.aac'},
    'messagebox':types.SimpleNamespace(showinfo=Mock()),
    'quality_engine':types.SimpleNamespace(music_hunter_library_path=lambda:Path('default-music'))}
exec(compile(ast.Module(body=nodes,type_ignores=[]),str(APP),'exec'),ns)

class Var:
    def __init__(self,value):self.value=value
    def get(self):return self.value
    def set(self,value):self.value=value

class SharedMusicTests(unittest.TestCase):
    def test_random_and_repeat_avoidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            a,b,c=[str(Path(tmp)/name) for name in ('a.mp3','b.wav','c.flac')]
            for p in (a,b,c):Path(p).touch()
            (Path(tmp)/'not_audio.txt').touch()
            choose=ns['_editor_choose_random_track']
            self.assertEqual(choose(tmp,a,b,True,lambda seq:seq[0]),c)
            self.assertEqual(choose(tmp,a,b,False,lambda seq:seq[0]),a)
            Path(b).unlink();Path(c).unlink()
            self.assertEqual(choose(tmp,a,a),a)
            Path(a).unlink()
            with self.assertRaises(ValueError):choose(tmp)

    def test_editor_uses_live_shared_folder_and_records_song(self):
        with tempfile.TemporaryDirectory() as tmp:
            song=str(Path(tmp)/'song.mp3');Path(song).touch()
            view=types.SimpleNamespace(music_var=Var(tmp),avoid_var=Var(True),
                state={'recent_music':'','music_folder':tmp},save=Mock(return_value=True),
                refresh=Mock(),open_music_hunter=Mock())
            editor=types.SimpleNamespace(autodirector_view=view,
                project=types.SimpleNamespace(music_path=''),
                _set_editor_music=Mock())
            editor._shared_music_folder=lambda:ns['_shared_music_folder'](editor)
            ns['pick_random_music'](editor)
            editor._set_editor_music.assert_called_once_with(song,reset_offset=True)
            self.assertEqual(view.state['recent_music'],song)
            view.save.assert_called_once()
            view.refresh.assert_called_once()
            ns['open_editor_music_hunter'](editor)
            view.open_music_hunter.assert_called_once()
            editor._set_editor_music.reset_mock()
            view.state['recent_music']='previous'
            view.save.return_value=False
            ns['pick_random_music'](editor)
            editor._set_editor_music.assert_not_called()
            self.assertEqual(view.state['recent_music'],'previous')

    def test_music_is_added_to_actual_project_with_undo(self):
        editor=types.SimpleNamespace(project=types.SimpleNamespace(music_path='old',music_offset=9),
            media_paths=[],media_list=Mock(),_snapshot=Mock(),_refresh_all=Mock(),status_var=Mock())
        ns['_set_editor_music'](editor,'new.mp3',reset_offset=True)
        editor._snapshot.assert_called_once()
        self.assertEqual(editor.project.music_path,'new.mp3')
        self.assertEqual(editor.project.music_offset,0)
        self.assertEqual(editor.media_paths,['new.mp3'])
        editor._refresh_all.assert_called_once()

    def test_hunter_defaults_to_shared_folder(self):
        init=next(x for x in hunter.body if isinstance(x,ast.FunctionDef) and x.name=='__init__')
        self.assertIn('owner.music_var.get() or str(default)',ast.unparse(init))
        self.assertIn('self.owner.music_var.set(folder)',ast.unparse(hunter))

if __name__=='__main__':unittest.main()
