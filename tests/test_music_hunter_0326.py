"""Music Hunter v0.3.26 beta: fresh discovery and opt-in MP3 import."""
import datetime
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


def date_ago(days):
    return (datetime.datetime.now(datetime.timezone.utc).date() -
            datetime.timedelta(days=days)).strftime('%Y%m%d')


class FakeYT:
    def __init__(self,data,opts=None):
        self.data=data
        self.options=opts or {}

    def YoutubeDL(self,opts):
        return FakeYT(self.data,opts)

    def __enter__(self):
        return self

    def __exit__(self,*args):
        return False

    def extract_info(self,url,download=False):
        if url.startswith('ytsearchdate'):
            return {'entries':[{'id':key,'url':'https://www.youtube.com/watch?v='+key}
                               for key in self.data]}
        item=self.data[url.split('v=')[-1]]
        if download:
            output=Path(self.options['outtmpl'].replace('%(ext)s','m4a'))
            output.write_bytes(b'fake-audio')
        return item


class MusicHunterTests(unittest.TestCase):
    def test_accurate_date_parsing(self):
        self.assertIsNone(quality._music_hunter_date('ten years ago'))
        self.assertIsNone(quality._music_hunter_date('20260231'))
        self.assertEqual(quality._music_hunter_date('20261001'),
                         datetime.date(2026,10,1))

    def test_only_recent_verified_uploads_are_recommended(self):
        data={
            'AAAAAAAAAAA':{'id':'AAAAAAAAAAA','title':'New Boss Battle',
                'upload_date':date_ago(8),'uploader':'Creator A',
                'webpage_url':'https://www.youtube.com/watch?v=AAAAAAAAAAA'},
            'BBBBBBBBBBB':{'id':'BBBBBBBBBBB','title':'Ancient Boss Track',
                'upload_date':date_ago(2000),'uploader':'Old Channel'},
            'CCCCCCCCCCC':{'id':'CCCCCCCCCCC','title':'Mystery Battle',
                'upload_date':None,'uploader':'No Metadata'},
            'DDDDDDDDDDD':{'id':'DDDDDDDDDDD','title':'Old Song Reupload',
                'upload_date':date_ago(1),'release_date':date_ago(1400)},
            'EEEEEEEEEEE':{'id':'EEEEEEEEEEE','title':'Recent Orchestral',
                'upload_date':date_ago(18),'uploader':'Creator B',
                'license':'Creative Commons Attribution'},
        }
        fake=FakeYT(data)
        with mock.patch.object(quality,'_music_hunter_ytdlp',return_value=fake):
            found=quality.music_hunter_search('epic boss battle music',
                                               months=6,limit=10)
        self.assertEqual([x['title'] for x in found],
                         ['New Boss Battle','Recent Orchestral'])
        self.assertIn('Not verified',found[0]['license'])
        self.assertEqual(found[1]['license'],'Creative Commons Attribution')

    def test_window_selection_and_direct_url_paths_present(self):
        app=(ROOT/'source'/'app.py').read_text(encoding='utf-8')
        self.assertIn('♫ MUSIC HUNTER (BETA)',app)
        self.assertIn('class MusicHunterWindow(',app)
        self.assertIn('Or paste video URL:',app)
        self.assertIn('self.permission_var.get()',app)
        self.assertIn("self.owner.music_var.set(self.folder_var.get())",app)

    def test_no_arbitrary_url_download(self):
        for bad in ('http://www.youtube.com/watch?v=AAAAAAAAAAA',
                    'https://example.com/video',
                    'file:///C:/secret',
                    'https://youtube.com.evil.test/watch?v=AAAAAAAAAAA'):
            with self.assertRaises(ValueError):
                quality.music_hunter_import(bad,'unused')

    def test_mp3_import_uses_existing_ffmpeg_without_altering_user_video(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            ffmpeg=root/'ffmpeg-test.exe'
            ffmpeg.write_bytes(b'fake')
            fake=FakeYT({'AAAAAAAAAAA':{
                'id':'AAAAAAAAAAA','title':'Fresh Orchestral Battle'}})
            def fake_run(command,**kwargs):
                Path(command[-1]).write_bytes(b'MP3FAKE'*120)
                return types.SimpleNamespace(returncode=0,stderr=b'')
            with mock.patch.object(quality,'_music_hunter_ytdlp',return_value=fake), \
                 mock.patch.object(quality.engine,'get_ffmpeg',return_value=str(ffmpeg),
                                   create=True), \
                 mock.patch.object(quality.subprocess,'run',side_effect=fake_run):
                out=quality.music_hunter_import(
                    'https://www.youtube.com/watch?v=AAAAAAAAAAA',
                    root/'music')
                self.assertTrue(Path(out).is_file())
                self.assertEqual(Path(out).suffix,'.mp3')
                self.assertIn('Fresh Orchestral Battle',Path(out).name)
                self.assertTrue(str(root/'music') in out)
                self.assertEqual(list((root/'music').glob('cazy_music_*')),[])
                second=quality.music_hunter_import(
                    'https://www.youtube.com/watch?v=AAAAAAAAAAA',
                    root/'music')
                self.assertNotEqual(out,second)
                self.assertTrue(Path(second).is_file())


if __name__=='__main__':
    unittest.main()
