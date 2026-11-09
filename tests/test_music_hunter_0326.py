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
        if url.startswith('ytsearch'):
            if url.startswith('ytsearchdate'):
                raise AssertionError('Removed ytsearchdate command called')
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

    def test_diagnostics_when_every_song_is_old(self):
        data={'AAAAAAAAAAA':{'id':'AAAAAAAAAAA','title':'Old Epic Boss Song',
               'upload_date':date_ago(2000),'uploader':'Old Channel'}}
        with mock.patch.object(quality,'_music_hunter_ytdlp',return_value=FakeYT(data)):
            report=quality.music_hunter_search('epic orchestral music',months=6,
                                               return_report=True)
        self.assertEqual(report['results'],[])
        self.assertEqual(report['stats']['old'],1)
        self.assertIn('1 too old',report['message'])
        self.assertEqual(report['stats']['candidate_count'],1)

    def test_video_search_works_without_removed_date_sort_extractor(self):
        data={'AAAAAAAAAAA':{'id':'AAAAAAAAAAA','title':'Brand New Soundtrack',
               'upload_date':date_ago(5),'uploader':'Creator'}}
        calls=[]
        class LogYT(FakeYT):
            def YoutubeDL(self,opts):
                return self
            def extract_info(self,url,download=False):
                calls.append(url)
                return super().extract_info(url,download=download)
        with mock.patch.object(quality,'_music_hunter_ytdlp',return_value=LogYT(data)):
            report=quality.music_hunter_search('boss theme music',return_report=True)
        self.assertEqual(len(report['results']),1)
        self.assertTrue(any(x.startswith('ytsearch') for x in calls))
        self.assertFalse(any(x.startswith('ytsearchdate') for x in calls))
        self.assertEqual(report['results'][0]['title'],'Brand New Soundtrack')

    def test_metadata_errors_are_explained_when_only_undated_results_exist(self):
        data={'AAAAAAAAAAA':{'id':'AAAAAAAAAAA','title':'Undated Song',
               'uploader':'Creator'}}
        with mock.patch.object(quality,'_music_hunter_ytdlp',return_value=FakeYT(data)):
            report=quality.music_hunter_search('battle music',return_report=True)
        self.assertEqual(report['results'],[])
        self.assertIn('1 undated',report['message'])
        self.assertIn('Try a shorter query',report['message'])

    def test_window_selection_and_direct_url_paths_present(self):
        app=(ROOT/'source'/'app.py').read_text(encoding='utf-8')
        self.assertIn('♫ MUSIC HUNTER (BETA)',app)
        self.assertIn('class MusicHunterWindow(',app)
        self.assertIn('return_report=True',app)
        self.assertIn("report.get('message')",app)
        self.assertIn('Or paste video URL:',app)
        self.assertIn('self.permission_var.get()',app)
        self.assertIn("self.owner.music_var.set(folder)",app)

    def test_batch_select_all_skips_existing_tracks(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'Already Here - AAAAAAAAAAA.mp3').write_bytes(b'a')
            calls=[]
            def fake_import(url,destination,progress=None):
                ident=quality._music_hunter_video_id(url)
                calls.append(ident)
                if ident=='CCCCCCCCCCC':
                    raise RuntimeError('YouTube video unavailable')
                file=Path(destination)/('Imported - '+ident+'.mp3')
                file.write_bytes(b'audio')
                return str(file)
            urls=['https://www.youtube.com/watch?v=AAAAAAAAAAA',
                  'https://www.youtube.com/watch?v=BBBBBBBBBBB',
                  'https://youtu.be/BBBBBBBBBBB',
                  'https://youtu.be/CCCCCCCCCCC']
            notifications=[]
            with mock.patch.object(quality,'music_hunter_import',side_effect=fake_import):
                result=quality.music_hunter_import_batch(
                    urls,root,progress=lambda number,total,msg:
                    notifications.append((number,total,msg)))
            self.assertEqual(len(result['downloaded']),1)
            self.assertEqual(len(result['skipped']),2)
            self.assertEqual(len(result['failed']),1)
            self.assertIn('YouTube video unavailable',result['failed'][0]['error'])
            self.assertEqual(calls,['BBBBBBBBBBB','CCCCCCCCCCC'])
            self.assertTrue(Path(result['downloaded'][0]).exists())
            self.assertEqual(notifications[-1][0],4)

    def test_batch_can_be_stopped_between_tracks(self):
        import threading
        with tempfile.TemporaryDirectory() as folder:
            cancelled=threading.Event()
            calls=[]
            def fake_import(url,destination,progress=None):
                calls.append(url)
                cancelled.set()
                output=Path(destination)/'Track - AAAAAAAAAAA.mp3'
                output.write_bytes(b'complete')
                return str(output)
            with mock.patch.object(quality,'music_hunter_import',side_effect=fake_import):
                result=quality.music_hunter_import_batch([
                    'https://youtu.be/AAAAAAAAAAA',
                    'https://youtu.be/BBBBBBBBBBB'],folder,cancel=cancelled)
            self.assertEqual(len(calls),1)
            self.assertTrue(result['cancelled'])
            self.assertEqual(len(result['downloaded']),1)

    def test_managed_music_library_is_update_safe(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.dict(os.environ,{'LOCALAPPDATA':folder}):
                expected=Path(folder)/'CazyStudio'/'Music'
                self.assertEqual(quality.music_hunter_library_path(),expected)

    def test_batch_ui_select_all_visible_and_pinned(self):
        app=(ROOT/'source'/'app.py').read_text(encoding='utf-8')
        self.assertIn('self.table.selection_set(*items)',app)
        self.assertIn("selectmode='extended'",app)
        self.assertIn('self.select_all()',app)
        self.assertIn('self.footer.pack(side=',app)
        self.assertIn('⬇ DOWNLOAD ALL SELECTED → MP3 → CAZY MUSIC FOLDER',app)
        self.assertIn('music_hunter_import_batch(',app)
        self.assertIn('self.progress_bar.pack(',app)

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
