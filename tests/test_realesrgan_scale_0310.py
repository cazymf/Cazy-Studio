"""Regression: x4plus must never run at x2; validate output frame geometry.

The CLI is faked so these tests are deterministic without installing
Real-ESRGAN, requiring a GPU, or uploading any user images.
"""
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'source'))
sys.modules.setdefault('engine',types.ModuleType('engine'))
engine=sys.modules['engine']
if not hasattr(engine,'MediaInfo'):
    engine.MediaInfo=type('MediaInfo',(object,),{})
spec=importlib.util.spec_from_file_location('cazy_quality_grid_fix',BASE/'source'/'quality_engine.py')
quality=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=quality
spec.loader.exec_module(quality)


class ModelScaleFixTests(unittest.TestCase):
    def test_models_use_native_scale(self):
        self.assertEqual(quality._realesrgan_profile('Real-ESRGAN General'),
                         ('realesrgan-x4plus',4))
        self.assertEqual(quality._realesrgan_profile('Real-ESRGAN Anime'),
                         ('realesr-animevideov3',2))
        with self.assertRaisesRegex(ValueError,'Unknown Real-ESRGAN model'):
            quality._realesrgan_profile('Lanczos')

    def run_sequence(self, method, malformed=False):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            exe=root/'realesrgan-ncnn-vulkan'
            exe.write_bytes(b'fake binary')
            model_dir=root/'models'
            model_dir.mkdir()
            settings=quality.QualitySettings(upscale=method, fps=30)
            info=types.SimpleNamespace(width=32,height=16,fps=30,kind='video')
            real_commands=[]
            original_addons=sys.modules.get('install_ai_addons')
            addon_stub=types.ModuleType('install_ai_addons')
            addon_stub.find_model_directory=lambda path,which: model_dir
            sys.modules['install_ai_addons']=addon_stub

            def fake_run(cmd,*args,**kwargs):
                if cmd[0]=='fake-ffmpeg':
                    output_dir=Path(cmd[-1]).parent
                    output_dir.mkdir(exist_ok=True)
                    for index in range(2):
                        Image.new('RGB',(16,8),(15,130,230)).save(
                            output_dir/f'{index:08d}.png')
                    return
                assert cmd[0]==str(exe)
                real_commands.append(cmd)
                output_dir=Path(cmd[cmd.index('-o')+1])
                input_dir=Path(cmd[cmd.index('-i')+1])
                actual_scale=int(cmd[cmd.index('-s')+1])
                scale=2 if malformed else actual_scale
                for file in input_dir.glob('*.png'):
                    with Image.open(file) as frame:
                        enlarged=frame.resize((frame.width*scale,
                                               frame.height*scale))
                        enlarged.save(output_dir/file.name)

            fake_engine=types.SimpleNamespace(get_ffmpeg=lambda:'fake-ffmpeg')
            try:
                with mock.patch.object(quality,'engine',fake_engine), \
                     mock.patch.object(quality,'local_addons',
                                       lambda root:{'Real-ESRGAN':str(exe),'RIFE':None}), \
                     mock.patch.object(quality,'ai_preprocess_filters',
                                       lambda *args:(['fps=30'],16,8)), \
                     mock.patch.object(quality,'_run',fake_run):
                    if malformed:
                        with self.assertRaisesRegex(RuntimeError,
                                                    'model/scale mismatch'):
                            quality._run_ai_sequence('source.mp4',info,
                                settings,32,16,30,.1,str(root),
                                str(root),None,None)
                    else:
                        output=quality._run_ai_sequence('source.mp4',info,
                            settings,32,16,30,.1,str(root),
                            str(root),None,None)
                        result=Path(output[0].replace('%08d','00000000'))
                        with Image.open(result) as image:
                            expected=4 if method=='Real-ESRGAN General' else 2
                            self.assertEqual(image.size,(16*expected,8*expected))
            finally:
                if original_addons is None:
                    sys.modules.pop('install_ai_addons',None)
                else:
                    sys.modules['install_ai_addons']=original_addons
            self.assertEqual(len(real_commands),1)
            actual_scale=int(real_commands[0][real_commands[0].index('-s')+1])
            self.assertEqual(actual_scale,4 if method=='Real-ESRGAN General' else 2)
            return real_commands[0]

    def test_general_invokes_native_x4_and_validates_dimensions(self):
        command=self.run_sequence('Real-ESRGAN General')
        self.assertEqual(command[command.index('-n')+1],'realesrgan-x4plus')

    def test_anime_stays_native_x2(self):
        command=self.run_sequence('Real-ESRGAN Anime')
        self.assertEqual(command[command.index('-n')+1],'realesr-animevideov3')

    def test_invalid_x2_output_is_rejected_instead_of_encoded(self):
        self.run_sequence('Real-ESRGAN General',malformed=True)


if __name__=='__main__':
    unittest.main()
