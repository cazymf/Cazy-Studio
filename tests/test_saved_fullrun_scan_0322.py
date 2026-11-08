"""Saved OCR scans survive closing the review dialog and restarting Cazy Studio."""
import ast
import hashlib
import json
import os
import tempfile
import types
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'source'/'app.py'
tree=ast.parse(APP.read_text(encoding='utf-8'))
wanted={
    '_autodirector_path','_autodirector_scan_cache_path',
    '_autodirector_source_fingerprint','_autodirector_save_scan_cache',
    '_autodirector_load_scan_cache'
}
nodes=[node for node in tree.body if isinstance(node,ast.FunctionDef)
       and node.name in wanted]
assert len(nodes)==len(wanted),wanted-{node.name for node in nodes}
ns={
    'hashlib':hashlib,'json':json,'os':os,'tempfile':tempfile,'Path':Path,
    'autodirector_fullrun':types.SimpleNamespace(SEGMENT_MAX=200)
}
exec(compile(ast.Module(body=nodes,type_ignores=[]),str(APP),'exec'),ns)


class PersistedFullRunReviewTests(unittest.TestCase):
    def setUp(self):
        self.root=tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.old=os.environ.get('LOCALAPPDATA')
        os.environ['LOCALAPPDATA']=self.root.name
        self.addCleanup(self.restore_env)
        self.video=Path(self.root.name)/'OBS One.mp4'
        self.video.write_bytes(b'original video contents')

    def restore_env(self):
        if self.old is None:
            os.environ.pop('LOCALAPPDATA',None)
        else:
            os.environ['LOCALAPPDATA']=self.old

    def run_data(self, path=None):
        path=str(path or self.video)
        segments=[]
        for chamber in (1,2,3):
            for half in ('First Half','Second Half'):
                start=chamber*12+(0 if half=='First Half' else 6)
                segments.append({
                    'path':path,'chamber':chamber,'side':half,
                    'start':float(start),'end':float(start+5),
                    'enabled':True,'evidence':'OCR reviewed'})
        return {
            'mode':'Full Run','completed_chambers':3,
            'segments':segments,
            'reports':[{'path':path,'duration':105.0,
                        'events':[{'kind':'start','at':12.}]}],
            'event_counts':{'start':6,'switch':3,'complete':3,'failure':0}
        }

    def test_scan_saved_and_reopened_after_restart_without_ocr(self):
        result=self.run_data()
        filename=ns['_autodirector_save_scan_cache'](result,[self.video])
        self.assertTrue(filename.is_file())
        # User edits timestamp and switches off a half. Persist as reviewed.
        loaded=ns['_autodirector_load_scan_cache']([str(self.video)])
        loaded['segments'][0]['start']=10.5
        loaded['segments'][1]['enabled']=False
        loaded['segments'].append({
            'path':str(self.video),'chamber':4,'side':'First Half',
            'start':95.,'end':99.,'enabled':True,'evidence':'Manual (reviewed)'})
        ns['_autodirector_save_scan_cache'](loaded,[str(self.video)])
        reopened=ns['_autodirector_load_scan_cache']([str(self.video)])
        self.assertEqual(reopened['completed_chambers'],3)
        self.assertEqual(len(reopened['segments']),7)
        self.assertEqual(reopened['segments'][0]['start'],10.5)
        self.assertFalse(reopened['segments'][1]['enabled'])
        self.assertEqual(reopened['segments'][-1]['evidence'],'Manual (reviewed)')
        self.assertEqual(self.video.read_bytes(),b'original video contents')

    def test_source_metadata_change_invalidates_saved_timestamps(self):
        ns['_autodirector_save_scan_cache'](self.run_data(),[self.video])
        self.video.write_bytes(b'changed recording contents!')
        with self.assertRaisesRegex(ValueError,'changed since the scan'):
            ns['_autodirector_load_scan_cache']([self.video])

    def test_other_recording_or_queue_order_does_not_reuse_wrong_cuts(self):
        second=Path(self.root.name)/'Other Recording.mp4'
        second.write_bytes(b'other')
        ns['_autodirector_save_scan_cache'](self.run_data(),[self.video])
        with self.assertRaises(FileNotFoundError):
            ns['_autodirector_load_scan_cache']([second])
        result=self.run_data()
        ns['_autodirector_save_scan_cache'](result,[self.video,second])
        self.assertEqual(len(ns['_autodirector_load_scan_cache'](
            [self.video,second])['segments']),6)
        with self.assertRaises(FileNotFoundError):
            ns['_autodirector_load_scan_cache']([second,self.video])

    def test_corrupt_cache_rejected_without_rescanning_automatically(self):
        file=ns['_autodirector_save_scan_cache'](self.run_data(),[self.video])
        file.write_text('{bad json',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'could not be opened'):
            ns['_autodirector_load_scan_cache']([self.video])

    def test_cache_refuses_unselected_source(self):
        unknown=Path(self.root.name)/'not-selected.mp4'
        unknown.write_bytes(b'elsewhere')
        with self.assertRaisesRegex(ValueError,'unselected recording'):
            ns['_autodirector_save_scan_cache'](self.run_data(unknown),[self.video])

    def test_ui_provides_separate_scan_again_and_review_saved_actions(self):
        code=APP.read_text(encoding='utf-8')
        self.assertIn("'REVIEW SAVED CUTS', self.reopen_saved_full_run",code)
        self.assertIn("'SCAN AGAIN (OCR)', self.scan_selected",code)
        self.assertIn('def reopen_saved_full_run(self):',code)
        self.assertIn('def persist_review():',code)
        self.assertIn('_autodirector_save_scan_cache(data,self._current_scan_paths())',code)
        self.assertIn('return persist_review()',code)


if __name__=='__main__':
    unittest.main()
