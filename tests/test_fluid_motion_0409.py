import ast
import math
import shutil
import subprocess
import unittest
from pathlib import Path

path=Path(__file__).resolve().parents[1]/'source'/'quality_engine.py'
tree=ast.parse(path.read_text(encoding='utf-8'))
node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='fluid_motion_filters')
ns={'math':math}
exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),ns)
filters=ns['fluid_motion_filters']

class FluidMotion(unittest.TestCase):
    def test_native_shutter(self):
        self.assertEqual(filters(120,60,180),['fps=60'])
        self.assertEqual(filters(120,60,360),["tmix=frames=2:weights='1 1'",'fps=60'])
        self.assertEqual(filters(240,60,270),["tmix=frames=3:weights='1 1 1'",'fps=60'])
        self.assertEqual(filters(120,60,270),["tmix=frames=2:weights='0.5 1'",'fps=60'])
    def test_optical_flow(self):
        result=filters(120,60,180,True)
        self.assertIn('scd=fdiff',result[0])
        self.assertTrue(result[1].startswith('tmix='))
        self.assertEqual(result[-1],'fps=60')
    def test_invalid(self):
        with self.assertRaises(ValueError): filters(30,60,180)
    @unittest.skipUnless(shutil.which('ffmpeg'),'requires FFmpeg')
    def test_real_media(self):
        for vectors in (False,True):
            cmd=['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i',
                 'testsrc2=size=160x96:rate=120:duration=1',
                 '-vf',','.join(filters(120,60,360,vectors)),
                 '-f','null','-']
            subprocess.run(cmd,check=True,capture_output=True)

if __name__=='__main__': unittest.main()
