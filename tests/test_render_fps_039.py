"""Tests for measured rendering throughput and the existing progress callbacks."""
import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT=Path(__file__).resolve().parents[1]
ENGINE=APP_ROOT/'source'/'quality_engine.py'


def extract_progress_helpers():
    tree=ast.parse(ENGINE.read_text(encoding='utf-8'))
    nodes=[node for node in tree.body
           if (isinstance(node,ast.ClassDef) and node.name=='RenderSpeedMeter')
           or (isinstance(node,ast.FunctionDef) and node.name=='_run')]
    ns={'Path':Path,'os':os,'subprocess':subprocess,'tempfile':tempfile}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(ENGINE),'exec'),ns)
    return ns


class RenderProgressTests(unittest.TestCase):
    def test_real_fps_and_eta_are_based_on_completed_frames(self):
        clock=[0.0]
        meter=extract_progress_helpers()['RenderSpeedMeter'](clock=lambda:clock[0])
        self.assertEqual(meter.describe('upscale',0,100),'')
        clock[0]=2.0
        result=meter.describe('upscale',20,100)
        self.assertIn('10.0 FPS',result)
        self.assertIn('ETA 8s',result)
        self.assertEqual(meter.describe('encode',0,100),'')
        clock[0]=4.0
        self.assertIn('10.0 FPS',meter.describe('upscale',40,100))
        self.assertNotIn('FPS',meter.describe('encode',0,100))
        self.assertEqual(meter.describe('extract',None,None),'')
        self.assertEqual(meter.describe('upscale',0,0),'')
        self.assertEqual(meter.describe('upscale',40,100).count('FPS'),1)

    def test_stage_restart_resets_stale_rate(self):
        clock=[0.0]
        meter=extract_progress_helpers()['RenderSpeedMeter'](clock=lambda:clock[0])
        meter.describe('upscale',1,40)
        clock[0]=2.0
        self.assertIn('FPS',meter.describe('upscale',15,40))
        clock[0]=3.0
        self.assertEqual(meter.describe('upscale',2,40),'')
        clock[0]=4.0
        self.assertIn('FPS',meter.describe('upscale',5,40))

    @unittest.skipUnless(os.name=='posix','Synthetic FFmpeg executable runs on POSIX CI')
    def test_ffmpeg_progress_exposes_frame_counters(self):
        progress=[]
        _run=extract_progress_helpers()['_run']
        with tempfile.TemporaryDirectory() as d:
            executable=Path(d)/'fake-ffmpeg'
            executable.write_text(
                '#!/usr/bin/env python3\n'
                'import sys,time\n'
                'args=sys.argv\n'
                'destination=args[args.index("-progress")+1]\n'
                'for count in (5,10,15,20,25,30):\n'
                '    with open(destination,"a",encoding="ascii") as out:\n'
                '        out.write(f"frame={count}\\nout_time_us={count*100000}\\nprogress=continue\\n")\n'
                '    time.sleep(0.35)\n',
                encoding='utf-8')
            executable.chmod(0o755)
            _run([str(executable)],on_progress=lambda frac,done,total:
                 progress.append((frac,done,total)),
                 ffmpeg_duration=3.0,ffmpeg_fps=10)
        self.assertTrue(progress)
        self.assertEqual(progress[-1],(1.0,30,30))
        self.assertTrue(any(done is not None and done>0 for _,done,_ in progress))
        self.assertTrue(all(total==30 for _,_,total in progress if total is not None))

    def test_quality_lab_and_autodirector_display_the_stage_metric(self):
        source=ENGINE.read_text(encoding='utf-8')
        lab=(APP_ROOT/'source'/'quality_ui.py').read_text(encoding='utf-8')
        auto=(APP_ROOT/'source'/'app.py').read_text(encoding='utf-8')
        self.assertIn('speed_meter.describe(stage,done,total)',source)
        self.assertIn("self.progress_title.set(f'{pct:.1f}%  •  {stage}{detail}')",lab)
        self.assertIn("self.status_var.set(f'Local rendering: {pct:.0f}% — {step}')",auto)


if __name__=='__main__':
    unittest.main()
