"""Regression tests: AutoDirector must choose random folder music at RENDER time."""
import ast
import random
import tempfile
import unittest
from pathlib import Path

SOURCE=Path(__file__).resolve().parents[1]/'source'/'app.py'


def load_music_helpers():
    source=ast.parse(SOURCE.read_text(encoding='utf-8'))
    wanted={'_autodirector_tracks','_autodirector_choose_export_music'}
    nodes=[node for node in source.body
           if isinstance(node,ast.FunctionDef) and node.name in wanted]
    assert len(nodes)==len(wanted)
    ns={'Path':Path,'random':random,'_AUTODIRECTOR_MUSIC_EXTS':
        {'.mp3','.wav','.m4a','.flac','.ogg','.aac'}}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(SOURCE),'exec'),ns)
    return ns


class ExportMusicSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers=load_music_helpers()

    def test_full_run_picks_folder_song_without_manual_assign(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            a=folder/'music a.mp3'
            b=folder/'music b.mp3'
            a.write_bytes(b'AUDIO')
            b.write_bytes(b'AUDIO')
            state={'random_music':True,'avoid_repeats':True,
                   'music_folder':temp,'recent_music':'','queue':[
                       {'path':'recording.mp4','music':''}]}
            select=self.helpers['_autodirector_choose_export_music']
            first=select(state,chooser=lambda choices: choices[0])
            self.assertEqual(first,str(a))
            state['recent_music']=first
            second=select(state,chooser=lambda choices: choices[0])
            self.assertEqual(second,str(b))
            self.assertNotEqual(first,second)
            self.assertEqual(state['queue'][0]['music'],'')

    def test_selected_music_folder_can_contain_mp3_from_hunter(self):
        with tempfile.TemporaryDirectory() as temp:
            music=Path(temp)/'Titan Awakening - videoID001.mp3'
            music.write_bytes(b'MP3')
            state={'random_music':True,'avoid_repeats':True,
                   'music_folder':temp,'recent_music':'',
                   'queue':[]}
            selected=self.helpers['_autodirector_choose_export_music'](state)
            self.assertEqual(selected,str(music))

    def test_empty_unselected_music_folder_does_not_break_clean_render(self):
        state={'random_music':True,'avoid_repeats':True,
               'music_folder':'','recent_music':'','queue':[]}
        self.assertEqual(self.helpers['_autodirector_choose_export_music'](state),'')

    def test_chosen_empty_folder_explains_missing_music(self):
        with tempfile.TemporaryDirectory() as temp:
            state={'random_music':True,'avoid_repeats':True,
                   'music_folder':temp,'recent_music':'','queue':[]}
            with self.assertRaisesRegex(ValueError,'no supported audio tracks'):
                self.helpers['_autodirector_choose_export_music'](state)

    def test_random_disabled_respects_prepared_music_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            track=Path(temp)/'manual.mp3'
            track.write_bytes(b'mp3')
            state={'random_music':False,'avoid_repeats':True,
                   'music_folder':temp,'recent_music':'','queue':[
                       {'path':'a.mp4','music':str(track)},
                       {'path':'b.mp4','music':''}]}
            selected=self.helpers['_autodirector_choose_export_music']
            self.assertEqual(selected(state,source='a.mp4'),str(track))
            self.assertEqual(selected(state,source='b.mp4'),'')
            self.assertEqual(selected(state),str(track))

    def test_export_paths_select_at_render_and_display_track_name(self):
        source=SOURCE.read_text(encoding='utf-8')
        self.assertIn('music=_autodirector_choose_export_music(self.state)',source)
        self.assertIn('assigned = _autodirector_choose_export_music(self.state, source)',source)
        self.assertIn("f'Music: {Path(music).name if music else \"None\"}",source)
        self.assertIn("f'Music: {Path(assigned).name if assigned else \"None\"}",source)
        self.assertGreaterEqual(source.count("self.state['recent_music']=music"),1)
        self.assertGreaterEqual(source.count("self.state['recent_music']=assigned"),1)
        renderer=(Path(__file__).resolve().parents[1]/'source'/'quality_engine.py').read_text(encoding='utf-8')
        self.assertIn('if music:',renderer)
        self.assertIn('"-stream_loop","-1","-i",str(music)',renderer)


if __name__=='__main__':
    unittest.main()
