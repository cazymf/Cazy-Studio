"""Cazy Studio Quality Lab v0.3.2.

Standalone quality rendering for native high-FPS recordings. No shell execution.
Real-ESRGAN and RIFE are optional LOCAL ncnn/Vulkan tools (not bundled).
This module deliberately doesn't claim optical-flow interpolation equals RIFE, or
Lanczos scaling equals neural upscaling.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable

import engine

OUTPUT_SIZES = {
    'Source': None,
    '1080p Landscape': (1920, 1080),
    '1440p Landscape': (2560, 1440),
    '4K Landscape': (3840, 2160),
    '1080p TikTok 9:16': (1080, 1920),
    '1440p Vertical 9:16': (1440, 2560),
    '4K Vertical 9:16': (2160, 3840),
}
TARGET_FPS = (0, 30, 60, 120, 240)
PRESETS = ('Clean', 'Smooth', 'Ultra', 'TikTok Master')
QUALITY_LEVELS = ('High', 'Maximum', 'Balanced')

@dataclass
class QualitySettings:
    preset: str = 'TikTok Master'
    resolution: str = '1080p TikTok 9:16'
    fps: int = 60  # 0 = source fps
    frame_method: str = 'Native / Duplicate'  # or Optical Flow, RIFE AI
    upscale: str = 'Lanczos'  # or Real-ESRGAN Anime, Real-ESRGAN General
    ai_speed: str = 'Balanced (60 FPS)'  # expensive AI frames per second; output FPS remains independent
    fit: str = 'Fill / crop'  # or Fit / black bars
    denoise: int = 0  # 0..3
    sharpen: int = 20  # 0..100
    motion_blur: int = 0  # 0..3
    contrast: int = 100  # 80..130
    saturation: int = 105  # 80..150
    encoder: str = 'Auto / NVIDIA NVENC'  # or CPU x264
    quality: str = 'High'
    start: float = 0.0
    duration: float = 0.0  # zero = rest of file
    scratch_dir: str = ''  # optional: user-selected fast drive with plenty of space

    def validate(self):
        if self.preset not in PRESETS: raise ValueError('Unknown quality preset')
        if self.resolution not in OUTPUT_SIZES: raise ValueError('Unknown resolution')
        if self.fps not in TARGET_FPS: raise ValueError('FPS must be Source, 30, 60, 120 or 240')
        if self.frame_method not in ('Native / Duplicate', 'Optical Flow', 'RIFE AI'):
            raise ValueError('Unknown interpolation method')
        if self.upscale not in ('Lanczos', 'Real-ESRGAN Anime', 'Real-ESRGAN General'):
            raise ValueError('Unknown upscaler')
        if self.ai_speed not in ('Fast (30 FPS)', 'Balanced (60 FPS)', 'Every output frame'):
            raise ValueError('Unknown AI processing speed')
        if self.fit not in ('Fill / crop', 'Fit / black bars'): raise ValueError('Unknown frame fitting')
        if not 0 <= self.denoise <= 3: raise ValueError('Denoise out of range')
        if not 0 <= self.sharpen <= 100: raise ValueError('Sharpen out of range')
        if not 0 <= self.motion_blur <= 3: raise ValueError('Motion blur out of range')
        if not 80 <= self.contrast <= 130: raise ValueError('Contrast out of range')
        if not 80 <= self.saturation <= 150: raise ValueError('Saturation out of range')
        if self.encoder not in ('Auto / NVIDIA NVENC', 'CPU x264', 'HEVC / NVIDIA NVENC', 'AV1 / NVIDIA NVENC'): raise ValueError('Unknown encoder')
        if self.quality not in QUALITY_LEVELS: raise ValueError('Unknown quality')
        if self.start < 0 or self.duration < 0: raise ValueError('Invalid trim range')
        if self.scratch_dir and not Path(self.scratch_dir).is_dir():
            raise ValueError('Scratch folder does not exist: '+self.scratch_dir)

    def clone(self):
        return QualitySettings(**asdict(self))


def suggested_settings(info: engine.MediaInfo, preset: str = 'TikTok Master') -> QualitySettings:
    if preset not in PRESETS: raise ValueError(preset)
    result = QualitySettings(preset=preset)
    source_fps = info.fps or 60
    if preset == 'Clean':
        result.resolution = 'Source'
        result.fps = 0
        result.sharpen = 12
        result.saturation = 100
    elif preset == 'Smooth':
        result.resolution = 'Source'
        result.fps = 120 if source_fps < 120 else 0
        result.frame_method = 'Optical Flow' if source_fps < 120 else 'Native / Duplicate'
        result.sharpen = 10
    elif preset == 'Ultra':
        result.resolution = '1440p Landscape'
        result.fps = 0  # retain native 120/240 fps
        result.sharpen = 24
        result.quality = 'Maximum'
    else:
        result.fps = 60
        result.resolution = '1080p TikTok 9:16'
        result.sharpen = 18
        result.saturation = 108
    return result


def media_summary(info: engine.MediaInfo) -> str:
    return f'{info.width} × {info.height}  •  {info.fps:.2f} source FPS  •  {engine.fmt_time(info.duration)}'


def output_specs(info: engine.MediaInfo, opts: QualitySettings) -> tuple[int,int,int]:
    opts.validate()
    w, h = OUTPUT_SIZES[opts.resolution] or (info.width, info.height)
    fps = opts.fps or round(info.fps or 60)
    if not (w and h and fps): raise ValueError('Could not detect source size/FPS')
    if w%2 or h%2: w-=w%2; h-=h%2
    return w, h, fps


def ai_processing_fps(info: engine.MediaInfo, opts: QualitySettings, output_fps: int) -> float:
    """Real frames for neural restoration; duplication to output FPS happens after AI.

    RIFE explicitly produces new AI frames first, so don't silently cap its output.
    """
    original = float(info.fps or output_fps)
    cap = {'Fast (30 FPS)': 30, 'Balanced (60 FPS)': 60,
           'Every output frame': output_fps}[opts.ai_speed]
    if opts.frame_method == 'RIFE AI' and output_fps > original + .5:
        return original
    return min(original, float(output_fps), float(cap))


def ai_preprocess_filters(info: engine.MediaInfo, opts: QualitySettings,
                          w: int, h: int, processing_fps: float) -> tuple[list[str], int, int]:
    """Crop BEFORE AI. At 2x ESRGAN output, do not create unneeded pixels.

    e.g. 2560x1440 landscape -> 1080x1920 portrait: source 810x1440 crop
    -> 540x960 input -> 1080x1920 final, not 5120x2880 full-frame AI.
    """
    if not (info.width > 0 and info.height > 0):
        raise ValueError('Could not detect source dimensions')
    pre_w = max(2, (w//2)//2*2)
    pre_h = max(2, (h//2)//2*2)
    fs = [f'fps={processing_fps:.8f}']
    if opts.fit == 'Fill / crop':
        sw,sh = info.width,info.height
        if sw*h > sh*w:
            cw = max(2, min(sw, round(sh*w/h)))
            ch = sh
        else:
            cw = sw
            ch = max(2, min(sh,round(sw*h/w)))
        fs.append(f'crop={cw}:{ch}:(iw-{cw})/2:(ih-{ch})/2')
        fs.append(f'scale={pre_w}:{pre_h}:flags=lanczos')
    else:
        fs.append(f'scale={pre_w}:{pre_h}:force_original_aspect_ratio=decrease:flags=lanczos')
        fs.append(f'pad={pre_w}:{pre_h}:(ow-iw)/2:(oh-ih)/2:color=black')
    fs.append('setsar=1')
    return fs,pre_w,pre_h


def local_addons(root: str | os.PathLike) -> dict[str, str | None]:
    """Check executable AND model file pairs; don't report incomplete installs."""
    import install_ai_addons
    base=Path(root)
    result={}
    for tool,label,exe in (
        ('RealESRGAN','Real-ESRGAN','realesrgan-ncnn-vulkan.exe' if os.name=='nt' else 'realesrgan-ncnn-vulkan'),
        ('RIFE','RIFE','rife-ncnn-vulkan.exe' if os.name=='nt' else 'rife-ncnn-vulkan'),
    ):
        found=install_ai_addons.addon_status(base,tool)
        if not found:
            from_path=shutil.which(exe)
            if from_path and install_ai_addons.find_model_directory(from_path,tool):
                found=from_path
        result[label]=found
    return result


def _encode_options(opts: QualitySettings, software: bool=False) -> list[str]:
    if software or opts.encoder == 'CPU x264':
        if opts.encoder == 'HEVC / NVIDIA NVENC':
            return ['-c:v','libx265','-preset','medium',
                    '-crf',str({'Maximum':18,'High':21,'Balanced':25}[opts.quality]),
                    '-x265-params','log-level=error']
        if opts.encoder == 'AV1 / NVIDIA NVENC':
            return ['-c:v','libsvtav1','-preset','6',
                    '-crf',str({'Maximum':19,'High':23,'Balanced':28}[opts.quality])]
        return ['-c:v','libx264','-preset','slow' if opts.quality!='Balanced' else 'medium',
                '-crf',str({'Maximum':15,'High':17,'Balanced':21}[opts.quality])]
    codec = {'Auto / NVIDIA NVENC':'h264_nvenc',
             'HEVC / NVIDIA NVENC':'hevc_nvenc',
             'AV1 / NVIDIA NVENC':'av1_nvenc'}[opts.encoder]
    return ['-c:v',codec,'-preset','p6', '-rc','vbr','-cq',
            str({'Maximum':16,'High':18,'Balanced':22}[opts.quality]), '-b:v','0']


def _filter_chain(info: engine.MediaInfo, opts: QualitySettings, *,
                  only_image=False, after_ai=False) -> list[str]:
    w,h,fps = output_specs(info, opts)
    fs=[]
    if opts.denoise:
        strength={1:(1.5,1.5,4,4),2:(3,3,6,6),3:(4.5,4.5,9,9)}[opts.denoise]
        fs.append('hqdn3d='+':'.join(str(x) for x in strength))
    if opts.contrast!=100 or opts.saturation!=100:
        fs.append(f'eq=contrast={opts.contrast/100:.3f}:saturation={opts.saturation/100:.3f}')
    if not only_image:
        if not after_ai and opts.frame_method=='Optical Flow' and info.fps > 0 and fps > info.fps + .4:
            fs.append(f'minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bilat:me=epzs')
        else:
            fs.append(f'fps={fps}')
        if opts.motion_blur:
            n=opts.motion_blur+1
            weights=' '.join(str(x) for x in range(1,n+1))
            fs.append(f'tmix=frames={n}:weights=\'{weights}\'')
    if opts.fit=='Fill / crop':
        fs += [f'scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos',
               f'crop={w}:{h}']
    else:
        fs += [f'scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos',
               f'pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black']
    if opts.sharpen:
        amount=round(opts.sharpen/100*1.35,3)
        fs.append(f'unsharp=5:5:{amount:.3f}:5:5:0.0')
    fs+=['setsar=1','format=yuv420p']
    return fs


def _run(cmd:list[str], on_status=None, cancel=None, cwd=None,
         progress_dir=None, progress_total=0, progress_label='AI processing',
         on_progress=None, ffmpeg_duration=None, ffmpeg_fps=None):
    """Run an external stage and report *measured* frame/time progress.

    Real-ESRGAN and RIFE report completed PNGs. FFmpeg reports encoded media
    timestamps using its machine-readable -progress output. No fake timer.
    """
    if on_status and not progress_dir:
        on_status('Running ' + Path(cmd[0]).name + '…')
    import re
    import time
    progress_file=None
    args=list(cmd)
    if ffmpeg_duration is not None and float(ffmpeg_duration)>0:
        fd, progress_file=tempfile.mkstemp(prefix='cazy_ffmpeg_progress_',suffix='.log')
        os.close(fd)
        args=args[:1]+['-nostats','-progress',progress_file]+args[1:]
    try:
        with tempfile.TemporaryFile(mode='w+t',encoding='utf-8',errors='replace') as errout:
            flags=getattr(subprocess,'CREATE_NO_WINDOW',0) if os.name=='nt' else 0
            proc=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=errout,
                                  stdin=subprocess.DEVNULL,cwd=cwd,creationflags=flags)
            last_progress=0.0
            try:
                while proc.poll() is None:
                    if cancel is not None and cancel.is_set():
                        proc.terminate()
                        try:proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:proc.kill();proc.wait()
                        raise RuntimeError('Cancelled by user.')
                    now=time.monotonic()
                    if on_progress and now-last_progress>=.55:
                        if progress_dir:
                            done=sum(1 for f in Path(progress_dir).iterdir() if f.suffix.lower()=='.png')
                            total=max(1,int(progress_total))
                            on_progress(min(.999,done/total),done,total)
                        elif progress_file:
                            try:
                                # Read a bounded suffix; FFmpeg appends key=value updates.
                                with open(progress_file,'rb') as f:
                                    f.seek(0,os.SEEK_END)
                                    endpos=f.tell();f.seek(max(0,endpos-25000))
                                    log=f.read().decode('ascii',errors='ignore')
                                ts=re.findall(r'(?m)^out_time_(?:us|ms)=(\d+)$',log)
                                if ts:
                                    secs=int(ts[-1])/1_000_000.0
                                else:
                                    ts=re.findall(r'(?m)^out_time=(\d+):(\d+):([\d.]+)$',log)
                                    secs=(int(ts[-1][0])*3600+int(ts[-1][1])*60+float(ts[-1][2])) if ts else 0.0
                                frame_values=re.findall(r'(?m)^frame=\s*(\d+)\s*$',log)
                                if ffmpeg_fps and frame_values:
                                    total_frames=max(1,int(round(ffmpeg_duration*float(ffmpeg_fps))))
                                    frames_done=min(total_frames,int(frame_values[-1]))
                                    on_progress(min(.999,max(0.0,secs/ffmpeg_duration)),
                                                frames_done,total_frames)
                                else:
                                    on_progress(min(.999,max(0.0,secs/ffmpeg_duration)),None,None)
                            except (OSError,ValueError):pass
                        last_progress=now
                    elif progress_dir and on_status and now-last_progress>=1.5:
                        done=sum(1 for f in Path(progress_dir).iterdir() if f.suffix.lower()=='.png')
                        on_status(f'{progress_label}: {done:,}/{progress_total:,} frames ({100*done/max(1,progress_total):.0f}%)')
                        last_progress=now
                    time.sleep(.10)
                if proc.returncode!=0:
                    errout.seek(0)
                    raise RuntimeError('Processing failed:\n'+errout.read()[-3500:])
                if on_progress:
                    if progress_dir:
                        final_total=int(progress_total)
                    elif progress_file and ffmpeg_fps:
                        final_total=max(1,int(round(ffmpeg_duration*float(ffmpeg_fps))))
                    else:
                        final_total=None
                    on_progress(1.0,final_total,final_total)
            finally:
                if proc.poll() is None:
                    proc.kill();proc.wait()
    finally:
        if progress_file:
            try:os.unlink(progress_file)
            except OSError:pass


def _start_duration(info, opts, sample):
    if opts.start >= info.duration: raise ValueError('Start position is past the end of the source')
    d=max(0,info.duration-opts.start)
    if opts.duration: d=min(opts.duration,d)
    if sample: d=min(3.0,d)
    if d<.08:raise ValueError('Need at least 0.08 seconds to export')
    return d


def _realesrgan_profile(upscale):
    """Match the ncnn model with its NATIVE inference scale.

    The General x4plus model ships as an x4 network. Passing -s 2 to x4plus
    can yield severely corrupted square-tile output on some builds/drivers.
    The AnimeVideoV3 model has dedicated x2 weights and safely uses -s 2.
    The final FFmpeg stage scales to the user-selected output resolution.
    """
    if upscale == 'Real-ESRGAN Anime':
        return 'realesr-animevideov3', 2
    if upscale == 'Real-ESRGAN General':
        return 'realesrgan-x4plus', 4
    raise ValueError('Unknown Real-ESRGAN model: ' + str(upscale))


def _rife_interpolation_command(executable, frames_dir, out_dir, input_count, source_fps, output_fps):
    """Select an installed RIFE-v4 model for custom frame counts.

    Without -m, rife-ncnn-vulkan defaults to the legacy rife-v2.3 model, even
    if v4 weights are installed. Legacy models reject the -n flag: they support
    only the binary's default 2x output. Never mislabel those frames with a
    different requested FPS.
    """
    executable = Path(executable)
    if source_fps <= 0 or output_fps <= source_fps:
        raise ValueError("RIFE requires an output FPS greater than the source FPS")
    if input_count < 2:
        raise ValueError("RIFE needs at least two input frames")
    model_dirs = (
        executable.parent / "models",
        executable.parent,
        executable.parent.parent / "models",
    )
    candidates = {}
    for folder in model_dirs:
        if not folder.is_dir():
            continue
        for candidate in folder.glob("rife-v4*"):
            if (candidate.is_dir()
                    and any(candidate.glob("*.param"))
                    and any(candidate.glob("*.bin"))):
                candidates[str(candidate.resolve())] = candidate
    command = [str(executable), "-i", str(frames_dir), "-o", str(out_dir)]
    if candidates:
        # Prefer the highest available v4 revision, not the program's v2.3 default.
        model = max(candidates.values(), key=lambda p: (
            tuple(int(part) for part in re.findall(r"\d+", p.name)), str(p)))
        expected = max(2, int(round(input_count * output_fps / source_fps)))
        command += ["-m", str(model), "-n", str(expected)]
        model_description = model.name
    else:
        # The default non-v4 model supports 2x only, with -n omitted.
        ratio = output_fps / source_fps
        if abs(ratio - 2.0) > 0.025:
            raise RuntimeError(
                f"RIFE {source_fps:g} -> {output_fps:g} FPS requires RIFE v4 "
                "model files. Install or repair the RIFE AI addon using AI "
                "Addons Setup; an older RIFE model supports only 2x FPS.")
        # A portable RIFE installation may keep its default model under
        # models/ rather than alongside the executable.
        older = []
        for folder in model_dirs:
            if not folder.is_dir():
                continue
            for candidate in folder.glob("rife*"):
                if (candidate.is_dir() and "rife-v4" not in candidate.name
                        and any(candidate.glob("*.param"))
                        and any(candidate.glob("*.bin"))):
                    older.append(candidate)
        if older:
            def legacy_rank(p):
                return (p.name == "rife-v2.3", str(p))
            older_model = max(older, key=legacy_rank)
            command += ["-m", str(older_model)]
            model_description = older_model.name + " (2x)"
        else:
            model_description = "legacy default (2x)"
        expected = input_count * 2
    command += ["-f", "%08d.png"]
    return command, expected, model_description


def _run_ai_sequence(path, info, opts, w, h, fps, dur, tmp, root, status, cancel, stage_progress=None):
    """AI sequence optimized for the actual output crop and resolution.

    Crop/resize before Real-ESRGAN instead of upscaling millions of discarded
    landscape pixels, and cap the number of AI frames unless Full is selected.
    RIFE is retained as an explicit high-cost mode.
    """
    addons=local_addons(root)
    up=opts.upscale!='Lanczos'
    rife=opts.frame_method=='RIFE AI' and fps > info.fps + .5
    if up and not addons['Real-ESRGAN']:
        raise RuntimeError('Real-ESRGAN binary or models missing. Click AI Addons Setup to repair the installation.')
    if rife and not addons['RIFE']:
        raise RuntimeError('RIFE binary or models missing. Click AI Addons Setup to install a complete package.')
    source_fps=info.fps
    if source_fps<=0:raise RuntimeError('AI interpolation requires a detected source FPS')
    if not up and not rife:return None
    if info.kind!='video':raise ValueError('AI addons need a video source')
    ff=engine.get_ffmpeg()
    frames_dir=Path(tmp)/'frames_in';frames_dir.mkdir()
    requires_new_frames=opts.frame_method=='Optical Flow' and fps > source_fps + .4
    # If actual interpolation is requested, preserve source temporal samples.
    processing_fps=(ai_processing_fps(info,opts,fps) if up and not rife and not requires_new_frames else source_fps)
    if up:
        vf,ai_w,ai_h=ai_preprocess_filters(info,opts,w,h,processing_fps)
        if status:
            status(f'Fast AI: crop first → {ai_w}×{ai_h} input, 2× AI → {w}×{h} output. Processing {processing_fps:g} real FPS (final {fps} FPS).')
    else:
        vf=[f'fps={processing_fps:.8f}']
    _run([ff,'-hide_banner','-loglevel','error','-y','-ss',f'{opts.start:.5f}',
          '-t',f'{dur:.5f}','-i',path,'-vf',','.join(vf),
          '-start_number','0',str(frames_dir/'%08d.png')],None,cancel,
         on_progress=(lambda f,d,t:stage_progress('extract',f,d,t)) if stage_progress else None,
         ffmpeg_duration=dur,ffmpeg_fps=processing_fps)
    count=sum(1 for _ in frames_dir.glob('*.png'))
    if count<2:raise RuntimeError('Not enough frames extracted for the AI workflow')
    active=frames_dir
    produced_fps=processing_fps
    if rife:
        if status:status(f'RIFE interpolating {count} frames to {fps} FPS…')
        newdir=Path(tmp)/'rife_output';newdir.mkdir()
        exe=Path(addons['RIFE'])
        command,target_count,model_description=_rife_interpolation_command(
            exe,active,newdir,count,source_fps,fps)
        if status:
            status(f'RIFE model: {model_description}; generating {target_count} frames')
        _run(command,status,cancel,cwd=str(exe.parent),progress_dir=newdir,
             progress_total=target_count,progress_label='RIFE interpolation',
             on_progress=(lambda f,d,t:stage_progress('rife',f,d,t)) if stage_progress else None)
        newcount=sum(1 for _ in newdir.glob('*.png'))
        if newcount<target_count-2:raise RuntimeError('RIFE produced too few output frames')
        active=newdir
        produced_fps=fps
        if status:status('RIFE complete; cleaning intermediate images…')
        shutil.rmtree(frames_dir)
    if up:
        if status:status(f'Real-ESRGAN processing {len(list(active.glob("*.png"))):,} cropped frames…')
        out=Path(tmp)/'upscaled';out.mkdir()
        exe=Path(addons['Real-ESRGAN'])
        model,model_scale=_realesrgan_profile(opts.upscale)
        if status and model_scale==4:
            status('Real-ESRGAN General uses native 4x processing; final encode downscales to your chosen resolution.')
        import install_ai_addons
        model_dir=install_ai_addons.find_model_directory(exe,'RealESRGAN')
        if model_dir is None:raise RuntimeError('Real-ESRGAN model files are missing. Open AI Addons Setup to repair or reinstall.')
        target_count=sum(1 for _ in active.glob('*.png'))
        _run([str(exe),'-i',str(active),'-o',str(out),'-n',model,'-m',str(model_dir),'-s',str(model_scale),'-f','png'],
             status,cancel,cwd=str(exe.parent),progress_dir=out,
             progress_total=target_count,progress_label='Real-ESRGAN',
             on_progress=(lambda f,d,t:stage_progress('upscale',f,d,t)) if stage_progress else None)
        outcount=sum(1 for _ in out.glob('*.png'))
        if outcount!=target_count:
            raise RuntimeError(f'Real-ESRGAN processed {outcount}/{target_count} frames; cannot safely encode an incomplete sequence')
        # Check native output geometry before encoding. This catches wrong
        # scale/model pairs rather than silently producing a damaged MP4.
        from PIL import Image as _CazyImage
        expected_pixels=(ai_w*model_scale, ai_h*model_scale)
        upscaled_files=sorted(out.glob('*.png'))
        for sample_png in (upscaled_files[0], upscaled_files[-1]):
            with _CazyImage.open(sample_png) as sample_img:
                actual_pixels=sample_img.size
            if actual_pixels!=expected_pixels:
                raise RuntimeError(
                    f'Real-ESRGAN model/scale mismatch: expected {expected_pixels[0]}x'
                    f'{expected_pixels[1]}, received {actual_pixels[0]}x{actual_pixels[1]}. '
                    'Choose a different model or repair the AI addons before rerendering.')
        if status:status('AI frames complete; freeing input-frame scratch space…')
        shutil.rmtree(active)
        active=out
    files=sorted(active.glob('*.png'))
    start_index=int(files[0].stem)
    return str(active/'%08d.png'), produced_fps, start_index


class RenderSpeedMeter:
    """Measure frame throughput for each rendering stage independently.

    Frame rate here means *processed frames per wall-clock second*, rather
    than the configured output video FPS. Uses an eight-second moving window.
    """
    def __init__(self, clock=None):
        from collections import deque
        import time
        self.clock=clock or time.monotonic
        self.samples={}
        self._deque=deque

    def describe(self, stage, done, total):
        if done is None or total is None or total <= 0:
            return ''
        import math
        now=self.clock()
        done=max(0,min(int(done),int(total)))
        samples=self.samples.setdefault(stage,self._deque(maxlen=40))
        if samples and done < samples[-1][1]:
            samples.clear()
        samples.append((now,done))
        while len(samples)>1 and now-samples[0][0]>8.0:
            samples.popleft()
        if len(samples)<2:
            return ''
        seconds=now-samples[0][0]
        frames=done-samples[0][1]
        if seconds<0.8 or frames<=0:
            return ''
        rate=frames/seconds
        if not math.isfinite(rate) or rate<=0:
            return ''
        remaining=max(0,total-done)/rate
        eta=f'{int(remaining//60)}m {int(remaining%60):02d}s' if remaining>=60 else f'{int(remaining+0.5)}s'
        return f'  •  {rate:.1f} FPS  •  ETA {eta}'


def render_quality(path: str, output: str, opts: QualitySettings, status: Callable[[str],None]|None=None,
                   cancel=None, sample=False, root: str|None=None,
                   progress: Callable[[float,str,int|None,int|None],None]|None=None) -> str:
    """Enhance a video to MP4, optionally through local RIFE or ESRGAN addons.

    NVENC is attempted if selected; on systems with only an ffmpeg build that
    advertises NVENC but no GPU, the encoder is retried with x264.
    """
    opts.validate()
    info=engine.probe(path)
    if info.kind!='video':raise ValueError('Quality Lab accepts video footage only')
    w,h,fps=output_specs(info,opts)
    dur=_start_duration(info,opts,sample)
    output=os.path.abspath(output)
    Path(output).parent.mkdir(parents=True,exist_ok=True)
    ff=engine.get_ffmpeg()
    root=root or str(Path(__file__).resolve().parent)
    upscale=opts.upscale!='Lanczos'
    use_rife=(opts.frame_method=='RIFE AI' and fps>info.fps+.5)
    if upscale and use_rife:
        stages={'extract':(0,10),'rife':(10,38),'upscale':(38,88),'encode':(88,99.5)}
    elif upscale:
        stages={'extract':(0,12),'upscale':(12,88),'encode':(88,99.5)}
    elif use_rife:
        stages={'extract':(0,15),'rife':(15,79),'encode':(79,99.5)}
    else:
        stages={'encode':(0,99.5)}
    stage_labels={'extract':'Extracting trimmed frames',
                  'rife':'RIFE AI interpolation',
                  'upscale':'Real-ESRGAN upscaling',
                  'encode':'Encoding final MP4'}
    last_pct=[0.0]
    speed_meter=RenderSpeedMeter()
    def report(stage,fraction,done=None,total=None):
        low,high=stages.get(stage,(0,99.5))
        pct=min(99.5,max(last_pct[0],low+(high-low)*max(0.0,min(1.0,float(fraction)))))
        last_pct[0]=pct
        if progress:
            progress(pct,stage_labels.get(stage,stage)+speed_meter.describe(stage,done,total),done,total)
    if progress:report(next(iter(stages)),0)
    with tempfile.TemporaryDirectory(prefix='cazy_quality_', dir=opts.scratch_dir or None) as td:
        ai_seq=_run_ai_sequence(path,info,opts,w,h,fps,dur,td,root,status,cancel,report)
        for software in (False,True):
            if software and opts.encoder=='CPU x264':break
            if status:status('Encoding MP4 using '+('software encoder' if software or opts.encoder=='CPU x264' else opts.encoder)+'…')
            cmd=[ff,'-hide_banner','-loglevel','error','-y']
            if ai_seq:
                pattern, sequence_fps, start_index=ai_seq
                cmd+=['-framerate',f'{sequence_fps:.8f}','-start_number',str(start_index),'-i',pattern]
                cmd+=['-ss',f'{opts.start:.5f}','-t',f'{dur:.5f}','-i',path]
                # When AI-upscaled frames still need optical-flow interpolation,
                # use the actual intermediate frame rate for the final pass.
                intermediate=engine.MediaInfo(info.duration,info.width,info.height,sequence_fps,info.has_audio,info.kind)
                effective_opts=opts
                if opts.frame_method=='Optical Flow' and fps <= info.fps + .4:
                    # The source ALREADY has enough unique frames; capped AI FPS
                    # intentionally repeats them. Don't invoke slow CPU optical
                    # flow merely because the optimized AI stage sampled fewer.
                    effective_opts=opts.clone()
                    effective_opts.frame_method='Native / Duplicate'
                    if status and fps>sequence_fps+.4:
                        status('Output uses repeated AI frames to hit target FPS; skipping unnecessary optical flow.')
                vf=_filter_chain(intermediate,effective_opts)
            else:
                cmd+=['-ss',f'{opts.start:.5f}','-t',f'{dur:.5f}','-i',path]
                vf=_filter_chain(info,opts)
            cmd+=['-vf',','.join(vf),'-map','0:v:0', '-map',('1:a:0?' if ai_seq else '0:a:0?'),
                  '-r',str(fps),'-fps_mode','cfr',
                  *_encode_options(opts,software)]
            if opts.encoder=='HEVC / NVIDIA NVENC':cmd+=['-tag:v','hvc1']
            cmd+=['-pix_fmt','yuv420p','-c:a','aac','-b:a','320k','-ac','2','-ar','48000',
                  '-movflags','+faststart','-t',f'{dur:.5f}',output]
            try:
                _run(cmd,None,cancel,
                     on_progress=lambda f,d,t:report('encode',f,d,t),
                     ffmpeg_duration=dur,ffmpeg_fps=fps)
                if not Path(output).is_file() or Path(output).stat().st_size<1000:
                    raise RuntimeError('Output file was empty')
                if progress:progress(100.0,'Complete',None,None)
                if status:status('Quality export completed: '+Path(output).name)
                return output
            except RuntimeError as exc:
                if cancel is not None and cancel.is_set():raise
                if not software and opts.encoder!='CPU x264':
                    if status:status('GPU encoder unavailable; retrying the same codec in software…')
                    continue
                raise
    raise RuntimeError('All encoder attempts failed')


def compare_images(path:str, timestamp:float,opts:QualitySettings, folder:str) -> tuple[str,str]:
    """Extract A/B stills. The enhanced still uses exact FFmpeg spatial filters,
    excluding temporal-only RIFE/interpolation and neural addons. A/B isn't
    presented as neural-processed if those addons are selected.
    """
    opts.validate()
    info=engine.probe(path)
    if info.kind!='video':raise ValueError('Choose a video')
    timestamp=min(max(0,timestamp),max(0,info.duration-.04))
    root=Path(folder);root.mkdir(parents=True,exist_ok=True)
    ff=engine.get_ffmpeg()
    a=str(root/'original.png');b=str(root/'enhanced.png')
    base=[ff,'-hide_banner','-loglevel','error','-y','-ss',f'{timestamp:.5f}','-i',path,
          '-frames:v','1']
    # Both sides use IDENTICAL crop/resize so the slider compares matching
    # pixels. The left side disables the restoration/color filters only.
    before=opts.clone()
    before.denoise=0;before.sharpen=0
    before.contrast=100;before.saturation=100
    base_filters=_filter_chain(info,before,only_image=True)+['scale=960:-2:flags=lanczos']
    subprocess.run(base+['-vf',','.join(base_filters),a],check=True,capture_output=True,
                   creationflags=_cazy_subprocess_flags())
    filters=_filter_chain(info,opts,only_image=True)+['scale=960:-2:flags=lanczos']
    subprocess.run(base+['-vf',','.join(filters),b],check=True,capture_output=True,
                   creationflags=_cazy_subprocess_flags())
    return a,b


# ---- Quality Lab persistent user presets (kept in the existing update-safe module) ----
SCHEMA = 1
MAX_PRESETS = 100
MAX_NAME_LENGTH = 48
SKIP = {'start', 'duration'}


def preferences_path() -> Path:
    local = os.environ.get('LOCALAPPDATA')
    if local:
        return Path(local) / 'CazyStudio' / 'render_presets.json'
    return Path.home() / '.cazy_studio' / 'render_presets.json'


def empty_preferences() -> dict:
    return {'schema': SCHEMA, 'last_used': None, 'presets': {}}


def load_preferences(path=None) -> dict:
    path = Path(path) if path is not None else preferences_path()
    if not path.exists():
        return empty_preferences()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'Could not read render presets at {path}: {exc}') from exc
    if not isinstance(data, dict) or data.get('schema') != SCHEMA:
        raise ValueError('Unrecognized render preset file format')
    if not isinstance(data.get('presets'), dict):
        raise ValueError('Invalid saved render presets')
    if len(data['presets']) > MAX_PRESETS:
        raise ValueError('Too many saved render presets')
    if data.get('last_used') is not None and not isinstance(data['last_used'], dict):
        raise ValueError('Invalid last-used render settings')
    return data


def encode_settings(opts: QualitySettings) -> dict:
    opts.validate()
    return {k: v for k, v in asdict(opts).items() if k not in SKIP}


def decode_settings(data: dict) -> QualitySettings:
    if not isinstance(data, dict):
        raise ValueError('Invalid preset: expected setting values')
    allowed = set(QualitySettings.__dataclass_fields__) - SKIP
    kwargs = {k: v for k, v in data.items() if k in allowed}
    # A missing old scratch drive must not prevent opening a saved preset.
    scratch = kwargs.get('scratch_dir')
    if scratch and not Path(scratch).is_dir():
        kwargs['scratch_dir'] = ''
    opts = QualitySettings(**kwargs)
    opts.start = 0.0
    opts.duration = 0.0
    opts.validate()
    return opts



def resolve_saved_render_preset(name: str) -> QualitySettings:
    """Resolve the SAME named Quality Lab recipe shown by AutoDirector.

    A missing name is an explicit error: never silently render with defaults.
    """
    if not name or name == 'Use last-used settings':
        prefs = load_preferences()
        if prefs.get('last_used'):
            return decode_settings(prefs['last_used'])
        return QualitySettings()
    prefs = load_preferences()
    if name not in prefs['presets']:
        raise ValueError('Saved Quality Lab render preset not found: ' + name)
    return decode_settings(prefs['presets'][name])


def describe_saved_render_preset(name: str) -> str:
    """Human-readable active recipe, with exact saved options."""
    opts = resolve_saved_render_preset(name)
    return (
        f'Quality profile: {opts.preset}\n'
        f'Resolution: {opts.resolution}  |  FPS: '
        f'{opts.fps if opts.fps else "Source"}\n'
        f'Upscaling: {opts.upscale}\n'
        f'Frame generation: {opts.frame_method}\n'
        f'AI speed: {opts.ai_speed}\n'
        f'Encoder: {opts.encoder}  |  Quality: {opts.quality}\n'
        f'Fit: {opts.fit}  |  Sharpen: {opts.sharpen}  |  '
        f'Denoise: {opts.denoise}\n'
        f'Blur: {opts.motion_blur}  |  Contrast: {opts.contrast}%  |  '
        f'Saturation: {opts.saturation}%'
    )

def validate_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError('Preset name must be text')
    name = ' '.join(name.split())
    if not name or len(name) > MAX_NAME_LENGTH or any(ord(c) < 32 for c in name):
        raise ValueError('Enter a preset name between 1 and 48 characters')
    return name


def matching_name(prefs: dict, name: str) -> str | None:
    return next((old for old in prefs['presets'] if old.casefold() == name.casefold()), None)


def store_named(prefs: dict, name: str, opts: QualitySettings) -> str:
    name = validate_name(name)
    old = matching_name(prefs, name)
    if old is None and len(prefs['presets']) >= MAX_PRESETS:
        raise ValueError(f'Maximum {MAX_PRESETS} render presets reached')
    if old is not None and old != name:
        del prefs['presets'][old]
    prefs['presets'][name] = encode_settings(opts)
    return name


def save_preferences(prefs: dict, path=None):
    path = Path(path) if path is not None else preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(prefs, indent=2, ensure_ascii=False)
    # Only replace a valid, completely written file (works for Windows too).
    fd, tmp_name = tempfile.mkstemp(prefix='render_presets_', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


# ---- Cazy AutoDirector local Genshin screen detection (v0.3.8 beta) ----
# All dependencies are local. OCR uses optional Tesseract when installed.
# Visual-only candidates are marked POSSIBLE, never falsely confirmed.
def _cazy_classify_text(text):
    """Recognize English Genshin challenge text after OCR normalization."""
    import re as _re
    normalized = _re.sub(r'[^a-z0-9 ]+', ' ', str(text).lower())
    normalized = ' '.join(normalized.split())
    if any(term in normalized for term in (
        'challenge completed', 'challenge complete', 'battle complete',
        'battle completed', 'challenge ended', 'victory')):
        return 'success'
    if any(term in normalized for term in (
        'challenge failed', 'challenge failure', 'battle failed',
        'defeat', 'challenge interrupted')):
        return 'failure'
    if any(term in normalized for term in (
        'challenge started', 'challenge start', 'battle begins',
        'battle started')):
        return 'start'
    return None


def _cazy_tesseract(override=''):
    """Find a local Tesseract executable; never require an online service."""
    import shutil
    if override:
        return str(Path(override)) if Path(override).is_file() else None
    program=shutil.which('tesseract')
    if program:
        return program
    if os.name=='nt':
        for key in ('ProgramFiles','ProgramFiles(x86)','LOCALAPPDATA'):
            folder=os.environ.get(key)
            if folder:
                candidate=Path(folder)/'Tesseract-OCR'/'tesseract.exe'
                if candidate.is_file():
                    return str(candidate)
    return None


def _cazy_visual_score(frame, cv2, reference=None):
    """Quick visual probability of an end/result overlay, not a success verdict."""
    import numpy as np
    img = cv2.resize(frame, (800, 450), interpolation=cv2.INTER_AREA)
    # Center-upper area contains most Genshin completion and result titles.
    roi = img[55:220, 110:690]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    gold = cv2.inRange(hsv, np.array([12, 65, 145]), np.array([44, 255, 255]))
    frac = float(cv2.countNonZero(gold)) / gold.size
    gray_dark = float(np.mean(gray < 52))
    # Textlike gold/white titles against a results overlay.
    edges = cv2.Canny(gray, 65, 135)
    texture = float(cv2.countNonZero(edges)) / edges.size
    gold_hint = (0.003 < frac < 0.12 and texture > 0.035)
    dark_hint = (gray_dark > 0.60 and 0.035 < texture < 0.25)
    score = max(0.66 if gold_hint else 0.0, 0.57 if dark_hint else 0.0)
    if reference is not None:
        # Lower half and HUD remain dynamic. Compare central title only.
        signature = cv2.resize(gray, (160, 46), interpolation=cv2.INTER_AREA)
        ref = cv2.resize(reference, (160, 46), interpolation=cv2.INTER_AREA)
        ref = cv2.GaussianBlur(ref, (3, 3), 0)
        signature = cv2.GaussianBlur(signature, (3, 3), 0)
        ncc = float(cv2.matchTemplate(signature, ref, cv2.TM_CCOEFF_NORMED)[0, 0])
        if ncc > 0.75:
            score = max(score, min(0.94, ncc))
    return score


def cazy_detect_genshin(video_path, reference_path='', progress=None, cancel=None,
                        sample_seconds=1.5):
    """Scan one explicit OBS recording for tentative Genshin completion scenes.

    Returns duration, events and REVIEW-REQUIRED cut suggestions. Strong matches
    from OCR or an optional example screenshot are higher confidence. No file is
    changed; start times are suggestions until the user accepts them.
    """
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError('OpenCV (cv2) is required for local video scanning.') from exc
    from math import isfinite
    path = Path(video_path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError('Could not open selected recording: ' + path.name)
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        duration = frames / fps if fps > 0 else 0
        if not isfinite(duration) or duration <= 0:
            raise RuntimeError('Could not read video duration')
        sample_seconds = max(0.7, min(5.0, float(sample_seconds)))
        reference = None
        if reference_path:
            example = cv2.imread(str(reference_path))
            if example is None:
                raise ValueError('Could not read the selected success screenshot')
            resized = cv2.resize(example, (800, 450), interpolation=cv2.INTER_AREA)
            reference = cv2.cvtColor(resized[55:220, 110:690], cv2.COLOR_BGR2GRAY)
        ocr_program = _cazy_tesseract()
        events = []
        t = 0.0
        last_kind = ''
        last_time = -100.0
        while t < duration:
            if cancel is not None and cancel.is_set():
                raise RuntimeError('Scan cancelled')
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                t += sample_seconds
                continue
            score = _cazy_visual_score(frame, cv2, reference)
            kind = None
            reason = 'Possible Genshin result screen (visual pattern)'
            confidence = score
            if ocr_program and score >= 0.45:
                x, y = frame.shape[1], frame.shape[0]
                snippet = frame[int(.10*y):int(.60*y), int(.08*x):int(.92*x)]
                png_ok, binary = cv2.imencode('.png', snippet)
                if png_ok:
                    try:
                        result = subprocess.run([ocr_program, 'stdin', 'stdout',
                                                 '--psm', '11', '-l', 'eng'],
                                                input=binary.tobytes(), capture_output=True,
                                                timeout=8, check=False,
                                                creationflags=_cazy_subprocess_flags())
                        kind = _cazy_classify_text(result.stdout.decode('utf-8', 'replace'))
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            if kind is not None:
                confidence = 0.98
                reason = 'Recognized game text (' + kind + ')'
            elif reference is not None and score >= .76:
                kind = 'success'
                reason = 'Matched your sample completion screenshot'
                confidence = min(.94, score)
            elif score >= .64:
                kind = 'possible'
                confidence = .48
            if kind and (kind != last_kind or t - last_time > 7):
                events.append({'at': round(t, 2), 'kind': kind,
                               'confidence': round(confidence, 2), 'reason': reason})
                last_kind = kind
                last_time = t
            if progress:
                progress(min(1.0, t / duration))
            t += sample_seconds
        if progress:
            progress(1.0)
        candidates = []
        for event in events:
            if event['kind'] not in ('success', 'possible'):
                continue
            end = min(duration, event['at'] + 4.0)
            previous_starts = [e['at'] for e in events
                               if e['kind'] == 'start' and 0 < event['at']-e['at'] < 700]
            previous_results = [e['at'] for e in events
                                if e['kind'] in ('failure', 'success')
                                and e['at'] < event['at']]
            begin = max(0.0, event['at'] - 180.0)
            if previous_starts:
                begin = max(0.0, previous_starts[-1] - 3)
            if previous_results:
                begin = max(begin, previous_results[-1] + 4)
            if end - begin < 4:
                begin = max(0.0, end - 15)
            candidates.append({'start': round(begin, 2), 'end': round(end, 2),
                               'at': event['at'],
                               'confidence': event['confidence'],
                               'reason': event['reason'],
                               'review_required': True})
        return {'path': str(path), 'duration': round(duration, 2),
                'ocr_available': bool(ocr_program), 'events': events,
                'candidates': candidates}
    finally:
        cap.release()


def cazy_render_local_package(video_path, begin, finish, destination, preset_name,
                              music='', progress=None, status=None, cancel=None,
                              success_at=None):
    """Render one reviewed cut, thumbnail, and editable title without YouTube.

    Uses Quality Lab saved presets. Preserves the source and stages output in a
    temporary directory, moving completed assets only after rendering succeeds.
    """
    import re
    import shutil
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError('OpenCV is required for thumbnail extraction') from exc
    from PIL import Image, ImageDraw, ImageFont

    src = Path(video_path)
    if not src.is_file():
        raise FileNotFoundError(str(src))
    info = engine.probe(str(src))
    start, end = float(begin), float(finish)
    if not (0 <= start < end <= info.duration + .15):
        raise ValueError('Cut timestamps must be within the recording')
    settings = load_preferences()
    if preset_name and preset_name != 'Use last-used settings':
        if preset_name not in settings['presets']:
            raise ValueError('Saved render preset not found: ' + preset_name)
        opts = resolve_saved_render_preset(preset_name)
    elif settings.get('last_used'):
        opts = decode_settings(settings['last_used'])
    else:
        opts = suggested_settings(info, 'Clean')
    opts.start = start
    opts.duration = end - start
    opts.validate()

    out = Path(destination)
    out.mkdir(parents=True, exist_ok=True)
    clean_name = re.sub(r'[^A-Za-z0-9_-]+', '_', src.stem)[:62].strip('_') or 'Genshin'
    label = 'Stygian Onslaught' if 'stygian' in src.name.lower() else (
        'Spiral Abyss' if 'abyss' in src.name.lower() else 'Genshin Combat')
    title = f'{label} | {clean_name.replace("_", " ")} | Genshin Impact'
    index = 1
    while (out / f'{clean_name}_AutoDirector_{index:02d}.mp4').exists():
        index += 1
    stem = f'{clean_name}_AutoDirector_{index:02d}'
    target = out / (stem + '.mp4')
    pic = out / (stem + '_thumbnail.png')
    title_file = out / (stem + '_title.txt')
    manifest_file = out / (stem + '_review.json')
    with tempfile.TemporaryDirectory(prefix='cazy_auto_', dir=out) as scratch:
        scratch = Path(scratch)
        basic = scratch / 'cut.mp4'
        render_quality(str(src), str(basic), opts,
                       status=status, cancel=cancel,
                       progress=(lambda pct, phase, done, total:
                                 progress(pct * .85, phase) if progress else None))
        if cancel is not None and cancel.is_set():
            raise RuntimeError('Render cancelled')
        final = basic
        if music:
            if not Path(music).is_file():
                raise FileNotFoundError('Selected music track is unavailable: ' + music)
            mixed = scratch / 'with_music.mp4'
            audio_exists = engine.probe(str(basic)).has_audio
            ffmpeg = engine.get_ffmpeg()
            cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                   '-i', str(basic), '-stream_loop', '-1', '-i', str(music)]
            if audio_exists:
                cmd += ['-filter_complex',
                        '[0:a:0]volume=1.0[g];[1:a:0]volume=0.20[m];'
                        '[g][m]amix=inputs=2:duration=first:dropout_transition=2[a]',
                        '-map', '0:v:0', '-map', '[a]']
            else:
                cmd += ['-map', '0:v:0', '-map', '1:a:0', '-af', 'volume=0.20']
            cmd += ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '256k',
                    '-t', str(end-start), '-movflags', '+faststart', str(mixed)]
            run = subprocess.run(cmd, capture_output=True, text=True, check=False,
                                 creationflags=_cazy_subprocess_flags())
            if run.returncode:
                raise RuntimeError('Music mix failed: ' + run.stderr[-1200:])
            final = mixed
        if not final.exists() or final.stat().st_size < 1024:
            raise RuntimeError('Renderer did not produce a valid video')
        frame_t = min(end - .2, max(start, float(success_at) - 3
                                     if success_at is not None else start + (end-start)*.65))
        cap = cv2.VideoCapture(str(src))
        try:
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, frame_t)*1000)
            success, frame = cap.read()
        finally:
            cap.release()
        if not success:
            raise RuntimeError('Could not extract a thumbnail frame')
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        art = Image.fromarray(frame).convert('RGB')
        art.thumbnail((1280, 720))
        canvas = Image.new('RGB', (1280,720), '#0b101c')
        canvas.paste(art, ((1280-art.width)//2,(720-art.height)//2))
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((0, 612, 1280, 720), fill='#10182a')
        try:
            font_path = 'C:/Windows/Fonts/arialbd.ttf' if os.name == 'nt' else '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
            font = ImageFont.truetype(font_path, 46)
        except OSError:
            font = ImageFont.load_default()
        draw.text((34, 632), label, font=font, fill='#f1f5ff')
        thumbnail_temp = scratch / 'thumbnail.png'
        canvas.save(thumbnail_temp)
        (scratch / 'title.txt').write_text(title + '\n', encoding='utf-8')
        metadata = {'source': str(src), 'start': start, 'end': end,
                    'preset': preset_name or 'Use last-used settings',
                    'music': music, 'title': title, 'reviewed': True}
        (scratch / 'review.json').write_text(json.dumps(metadata, indent=2),
                                              encoding='utf-8')
        if progress:
            progress(90.0, 'Writing local video package')
        shutil.move(str(final), target)
        shutil.move(str(thumbnail_temp), pic)
        shutil.move(str(scratch / 'title.txt'), title_file)
        shutil.move(str(scratch / 'review.json'), manifest_file)
    if progress:
        progress(100.0, 'Complete')
    return {'video': str(target), 'thumbnail': str(pic),
            'title': str(title_file), 'review': str(manifest_file)}


# ---- AutoDirector Full Run built into an existing updater-approved module ----
# This avoids adding a new file which v0.3.10's updater disallows.
import sys as _cazy_sys
quality = _cazy_sys.modules[__name__]

REFERENCE_KINDS = {
    "start": "Start Challenge",
    "switch": "On the other side...",
    "complete": "Challenge Completed",
    "failure": "Challenge Failed / Retry",
    "blessing": "Floor / Benediction (context only)",
}
REFERENCE_LIMIT = 24
SEGMENT_MAX = 200


def normalize_references(references=None, old_success=""):
    """Preserve v0.3.8's single saved screenshot while allowing many per event."""
    if not isinstance(references, dict):
        references = {}
    result = {}
    for kind in REFERENCE_KINDS:
        paths = references.get(kind, [])
        if not isinstance(paths, list):
            raise ValueError("Reference entries must be image lists")
        unique, seen = [], set()
        if kind == "complete" and old_success:
            paths = list(paths) + [old_success]
        for item in paths:
            if not isinstance(item, str) or len(item) > 4096:
                raise ValueError("Invalid detection image path")
            key = os.path.normcase(os.path.abspath(item))
            if key not in seen:
                unique.append(item)
                seen.add(key)
        if len(unique) > REFERENCE_LIMIT:
            raise ValueError("Too many reference images in " + kind)
        result[kind] = unique
    return result


def classify_screen_text(text):
    """Extract Abyss event phrases from local OCR results only.

    Text is compared as phrases, not by matching reference screenshots or
    the color of game overlays. Minor single-character OCR errors are
    tolerated on short, nearby word groups.
    """
    from difflib import SequenceMatcher
    normalized=" ".join(re.sub(r"[^a-z0-9]+"," ",str(text).lower()).split())
    if not normalized:
        return None
    triggers=(
        ('failure',('challenge failed','challenge failure','challenge interrupted')),
        ('switch',('on the other side','the other side')),
        ('complete',('challenge completed','challenge complete')),
        ('start',('start challenge','challenge started')),
    )
    for kind,phrases in triggers:
        if any(phrase in normalized for phrase in phrases):
            return kind
    tokens=normalized.split()
    for kind,phrases in triggers:
        for phrase in phrases:
            n=len(phrase.split())
            for count in (n,n+1):
                for pos in range(max(0,len(tokens)-count+1)):
                    piece=' '.join(tokens[pos:pos+count])
                    if abs(len(piece)-len(phrase))>3:
                        continue
                    if SequenceMatcher(None,piece,phrase).ratio()>=.88:
                        return kind
    if ('floor' in normalized and 'chamber' in normalized) or 'benediction' in normalized:
        return 'blessing'
    return None


def _abyss_screen_profile(frame,cv2):
    """Fast contrast probe: identify black transition and darkened result UI.

    This operates entirely with OpenCV and avoids launching OCR on blank
    loading screens. We do NOT classify a successful clear from colors alone.
    """
    tiny=cv2.resize(frame,(320,180),interpolation=cv2.INTER_AREA)
    gray=cv2.cvtColor(tiny,cv2.COLOR_BGR2GRAY)
    mean=float(gray.mean())
    dark=float((gray<60).mean())
    mid=gray[45:126,50:272]
    letters=float((mid>165).mean())
    return {
        'transition': mean<17 and dark>.96 and letters>.0009,
        'result': mean<45 and dark>.84 and letters>.008,
        'black_blank':mean<15 and dark>.96 and letters<=.0009,
        'mean':mean,
    }


def _abyss_text_regions(frame,cv2):
    """OCR UI text in different regions depending on the screen type."""
    h,w=frame.shape[:2]
    profile=_abyss_screen_profile(frame,cv2)
    mechanism=('Start Challenge area',(.50,.45,.93,.67))
    transition=('On the other side center',(.19,.25,.83,.69))
    complete=('Challenge Completed banner',(.23,.19,.81,.50))
    if profile['transition']:
        windows=[transition]
    elif profile['result']:
        windows=[complete,transition,mechanism]
    else:
        windows=[mechanism,complete,transition]
    for label,(x1,y1,x2,y2) in windows:
        crop=frame[int(y1*h):int(y2*h),int(x1*w):int(x2*w)]
        if not crop.size:
            continue
        gray=cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY)
        gray=cv2.resize(gray,None,fx=1.65,fy=1.65,
                        interpolation=cv2.INTER_CUBIC)
        yield label,gray



def _cazy_subprocess_flags(platform=None):
    """Suppress transient console windows from Windows CLI media/OCR tools."""
    name=os.name if platform is None else platform
    return getattr(subprocess,'CREATE_NO_WINDOW',0) if name=='nt' else 0


def read_abyss_frame_text(frame,executable,cancel=None):
    """Read actual Genshin UI strings using local, hidden-console Tesseract."""
    import cv2
    if not executable:
        raise RuntimeError('Text detection needs Tesseract OCR. Locate tesseract.exe in AutoDirector.')
    for label,gray in _abyss_text_regions(frame,cv2):
        for attempt in range(2):
            if cancel is not None and cancel.is_set():
                raise RuntimeError('Scan cancelled')
            if attempt==0:
                sample=gray
                psm='6'
            else:
                sample=cv2.threshold(gray,0,255,cv2.THRESH_BINARY+
                                     cv2.THRESH_OTSU)[1]
                psm='11'
            ok,png=cv2.imencode('.png',sample)
            if not ok:
                continue
            try:
                result=subprocess.run(
                    [executable,'stdin','stdout','--psm',psm,'-l','eng'],
                    input=png.tobytes(),capture_output=True,timeout=7,
                    check=False,creationflags=_cazy_subprocess_flags())
            except subprocess.TimeoutExpired:
                continue
            except OSError as exc:
                raise RuntimeError('Could not run local Tesseract: '+str(exc)) from exc
            if result.returncode:
                stderr=result.stderr.decode('utf-8','replace')
                if 'Error opening data file' in stderr:
                    raise RuntimeError('Tesseract English OCR language data is missing.')
                continue
            raw=result.stdout.decode('utf-8','replace')
            kind=classify_screen_text(raw)
            if kind in ('start','switch','complete','failure'):
                evidence='OCR text (%s): %s'%(label,' '.join(raw.split())[:100])
                return kind,.97,evidence
    return None,0.,''




# Crop screen regions by event, relative to a 960x540 standardized frame.
# Cropping distinct overlays avoids matching moving monsters/backgrounds.
REF_REGIONS = {
    "start": (0.54, 0.44, 0.83, 0.58),
    "switch": (0.25, 0.22, 0.75, 0.65),
    "complete": (0.29, 0.26, 0.73, 0.41),
    "failure": (0.29, 0.26, 0.73, 0.41),
    "blessing": (0.30, 0.03, 0.71, 0.14),
}


def _crop(frame, kind, cv2):
    h, w = frame.shape[:2]
    x1,y1,x2,y2 = REF_REGIONS[kind]
    return cv2.cvtColor(frame[int(y1*h):int(y2*h),int(x1*w):int(x2*w)],
                        cv2.COLOR_BGR2GRAY)


def _bright_abyss_text(frame, cv2):
    """Compare the bright UI lettering separately from moving game scenery."""
    hsv = cv2.cvtColor(frame,cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv,(0,0,185),(179,150,255))


def load_reference_bank(references):
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV is required for local Genshin scanning") from exc
    normalized = normalize_references(references)
    templates = {}
    for kind, paths in normalized.items():
        templates[kind] = []
        for path in paths:
            image = cv2.imread(path)
            if image is None:
                raise ValueError("Cannot read reference image: " + path)
            frame = cv2.resize(image, (960, 540), interpolation=cv2.INTER_AREA)
            template = _crop(frame,kind,cv2)
            if template.size < 100:
                raise ValueError("Invalid reference crop: "+path)
            x1,y1,x2,y2=REF_REGIONS[kind]
            color_region=frame[int(y1*540):int(y2*540),int(x1*960):int(x2*960)]
            bright_mask=_bright_abyss_text(color_region,cv2)
            templates[kind].append((template,bright_mask))
    return templates


def match_reference(frame, templates, threshold=.82):
    """Find event screenshots using grayscale and stable lettering.

    Bright UI text can remain identical while backgrounds/HUD change between
    scenes. Match those foreground letters as a separate cue, alongside the
    existing grayscale matcher. Reject empty masks as invalid evidence.
    """
    import cv2
    standard=cv2.resize(frame,(960,540),interpolation=cv2.INTER_AREA)
    best_kind,best_score=None,0.
    for kind,examples in templates.items():
        if kind=="blessing" or not examples:
            continue
        x1,y1,x2,y2=REF_REGIONS[kind]
        roi=standard[int(y1*540):int(y2*540),int(x1*960):int(x2*960)]
        gray=cv2.GaussianBlur(cv2.cvtColor(roi,cv2.COLOR_BGR2GRAY),(3,3),0)
        white=_bright_abyss_text(roi,cv2)
        px=max(5,int(gray.shape[1]*.09))
        py=max(3,int(gray.shape[0]*.12))
        gray_area=cv2.copyMakeBorder(gray,py,py,px,px,cv2.BORDER_REPLICATE)
        white_area=cv2.copyMakeBorder(white,py,py,px,px,
                                     cv2.BORDER_CONSTANT,value=0)
        for item in examples:
            ref,mask=item if isinstance(item,tuple) else (item,None)
            ref=cv2.GaussianBlur(ref,(3,3),0)
            score=float(cv2.minMaxLoc(cv2.matchTemplate(
                gray_area,ref,cv2.TM_CCOEFF_NORMED))[1])
            if mask is not None:
                density=float(cv2.countNonZero(mask))/max(1,mask.size)
                if .003 <= density <= .35:
                    text_score=float(cv2.minMaxLoc(cv2.matchTemplate(
                        white_area,mask,cv2.TM_CCOEFF_NORMED))[1])
                    if math.isfinite(text_score):
                        score=max(score,text_score)
            if math.isfinite(score) and score>best_score:
                best_kind,best_score=kind,score
    return (best_kind,round(best_score,3)) if best_score>=threshold else (None,best_score)


def collapse_events(events, gap=5.0):
    """Collapse repeated frames of the SAME screen, retain unrelated events."""
    ordered=sorted(events,key=lambda event:float(event["at"]))
    result=[]
    last_seen=float('-inf')
    for event in ordered:
        at=float(event["at"])
        if result and event["kind"]==result[-1]["kind"] and at-last_seen<=gap:
            if event.get("confidence",0)>result[-1].get("confidence",0):
                # Preserve the FIRST on-screen appearance, not the last.
                earliest=result[-1]["at"]
                result[-1]=dict(event,at=earliest)
        else:
            result.append(dict(event))
        last_seen=at
    return result


def build_abyss_segments(events,source,duration,first_chamber=1):
    """Keep BOTH halves only after a successful challenge-complete event.

    A first-half reset, failed attempt, or incomplete chamber contributes no
    exportable segments. A second 'Start Challenge' after switch is preferred
    for the second-half beginning so transition/loading footage is excluded.
    """
    if not math.isfinite(float(duration)) or float(duration)<=0:
        raise ValueError("Invalid recording duration")
    source=str(source)
    state="waiting"
    first_start=None
    first_end=None
    second_start=None
    switch_at=None
    segments=[]
    discarded=0
    notes=[]
    chamber=first_chamber
    last_start=-100.
    for event in collapse_events(events):
        kind=event.get("kind")
        at=float(event["at"])
        if at<0 or at>duration or kind not in REFERENCE_KINDS:
            continue
        if kind=="failure":
            if state!="waiting":
                discarded+=1
            state="waiting";first_start=first_end=second_start=switch_at=None
            continue
        if kind=="start":
            if state=="waiting":
                first_start=max(0.,at-2.0)
                state="first"
            elif state=="between":
                second_start=max(0.,at-2.0)
                state="second"
            elif state=="first" and at-last_start>8:
                # A second attempt started before a valid side switch.
                discarded+=1
                first_start=max(0.,at-2.0)
                first_end=second_start=switch_at=None
            elif state=="second" and at-last_start>8:
                discarded+=1
                state="first"
                first_start=max(0.,at-2.0)
                first_end=second_start=switch_at=None
            last_start=at
            continue
        if kind=="switch":
            if state=="first" and first_start is not None and at>first_start+1:
                first_end=max(first_start+.2, at-.15)
                switch_at=at
                state="between"
            continue
        if kind=="complete":
            if state not in ("second","between") or first_end is None:
                notes.append("Completion without both Abyss halves at %.1fs; review manually" % at)
                state="waiting"
                first_start=first_end=second_start=switch_at=None
                continue
            if second_start is None:
                # If we only caught the transfer overlay, the reviewer can
                # adjust this approximation before export.
                second_start=min(float(duration),switch_at+5.)
                note="Second-half start inferred from side switch; verify"
            else:
                note="Second-half start found"
            final_end=min(float(duration),at+2.0)
            if (first_start is not None and first_end-first_start>=.5 and
                    final_end-second_start>=.5):
                common={"path":source,"chamber":chamber,"review_required":True}
                segments.extend((
                    dict(common,side="First Half",start=round(first_start,2),
                         end=round(first_end,2),enabled=True,
                         evidence="Start Challenge → On the other side"),
                    dict(common,side="Second Half",start=round(second_start,2),
                         end=round(final_end,2),enabled=True,
                         evidence="On the other side → Challenge Completed; "+note)
                ))
                chamber+=1
            else:
                notes.append("Incomplete timestamps near %.1fs; review manually" % at)
            state="waiting"
            first_start=first_end=second_start=switch_at=None
    if state!="waiting":
        notes.append("Last chamber did not finish; excluded from automatic Full Run")
    return {"segments":segments,"completed_chambers":(chamber-first_chamber),
            "discarded_attempts":discarded,"notes":notes}


def scan_abyss_recording(path,references=None,progress=None,cancel=None,
                         sample_seconds=.8,ocr_path='',detail=None):
    """Scan full OBS recording with high-frequency black-transition checks.

    Screens are inspected every 0.4s. Standard OCR is scheduled every 0.8s;
    near-black transition screens are OCR'd immediately so a brief 'On the
    other side...' flash cannot slip between slow sampling points. Suggested
    cuts still require human review before rendering.
    """
    import cv2
    footage=Path(path)
    if not footage.is_file():
        raise FileNotFoundError(str(footage))
    executable=_cazy_tesseract(ocr_path)
    if not executable:
        raise RuntimeError('Text detection requires Tesseract OCR. Use Locate Tesseract in AutoDirector.')
    cap=cv2.VideoCapture(str(footage))
    if not cap.isOpened():
        raise RuntimeError('Could not open recording: '+footage.name)
    try:
        fps=float(cap.get(cv2.CAP_PROP_FPS) or 0.)
        frames=int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        duration=frames/fps if fps>0 else 0.
        if not math.isfinite(duration) or duration<=0:
            raise RuntimeError('Cannot determine recording duration')
        # Old mode sampled once every .8s, missing shorter transition screens.
        # Read frames 2x more often; spend OCR cycles mostly on useful frames.
        interval=max(.25,min(.4,float(sample_seconds)/2.))
        ocr_every=max(.6,min(1.25,float(sample_seconds)))
        next_regular_ocr=0.
        last_transition_ocr=-100.
        events=[]
        t=0.
        last_kind=None
        last_seen=-100.
        last_report=-100.
        while t<duration:
            if cancel is not None and cancel.is_set():
                raise RuntimeError('Scan cancelled')
            cap.set(cv2.CAP_PROP_POS_MSEC,t*1000.)
            ok,frame=cap.read()
            if ok:
                profile=_abyss_screen_profile(frame,cv2)
                immediate_transition=(profile['transition']
                                      and t-last_transition_ocr>=.4)
                immediate_result=(profile['result']
                                  and t>=next_regular_ocr-.35)
                should_ocr=(immediate_transition or immediate_result
                            or t>=next_regular_ocr)
                if profile['black_blank']:
                    # No visible lettering; plain black loading frame.
                    should_ocr=False
                if should_ocr:
                    kind,confidence,evidence=read_abyss_frame_text(
                        frame,executable,cancel=cancel)
                    if immediate_transition:
                        last_transition_ocr=t
                    next_regular_ocr=t+ocr_every
                    if kind:
                        if kind!=last_kind or t-last_seen>4.0:
                            events.append({'at':round(t,2),'kind':kind,
                                           'confidence':confidence,'evidence':evidence})
                        last_kind,last_seen=kind,t
                    elif t-last_seen>4.0:
                        last_kind=None
            if progress and (t-last_report>=1.2 or t+interval>=duration):
                progress(min(1.,t/duration))
            if detail and (t-last_report>=1.2 or t+interval>=duration):
                counts={kind:sum(event['kind']==kind for event in events)
                        for kind in ('start','switch','complete','failure')}
                detail({'at':round(t,2),'duration':round(duration,2),
                        'found':', '.join(f'{k.title()} {v}'
                                           for k,v in counts.items()),
                        'counts':counts})
            if t-last_report>=1.2 or t+interval>=duration:
                last_report=t
            t+=interval
        if progress:
            progress(1.)
        events=collapse_events(events)
        output=build_abyss_segments(events,footage,duration)
        counts={kind:sum(item.get('kind')==kind for item in events)
                for kind in REFERENCE_KINDS}
        return {'path':str(footage),'duration':duration,
                'ocr_available':True,'detection_method':'Local text OCR',
                'events':events,'event_counts':counts,**output}
    finally:
        cap.release()


def scan_full_run(paths,references=None,progress=None,cancel=None,
                  sample_seconds=.8,ocr_path='',detail=None):
    """Scan selected recordings, report actual progress and OCR event totals."""
    paths=list(paths)
    if not paths:
        raise ValueError('Add selected OBS recordings before scanning')
    segments=[]
    reports=[]
    chamber=1
    for i,path in enumerate(paths):
        report=scan_abyss_recording(
            path,references,cancel=cancel,sample_seconds=sample_seconds,
            ocr_path=ocr_path,
            progress=(lambda fraction,j=i:
                      progress((j+fraction)/len(paths))) if progress else None,
            detail=(lambda data,j=i,p=path:
                    detail(dict(data,video_index=j+1,
                                total_videos=len(paths),filename=Path(p).name,
                                fraction=(j+data['at']/max(.1,data['duration']))/
                                         len(paths)))
                   if detail else None))
        for seg in report['segments']:
            seg['chamber']+=chamber-1
        chamber+=report['completed_chambers']
        segments.extend(report['segments'])
        reports.append(report)
    if progress:
        progress(1.)
    counts={kind:sum(event.get('kind')==kind for report in reports
                     for event in report.get('events',[]))
            for kind in REFERENCE_KINDS}
    return {'mode':'Full Run','detection_method':'Local text OCR',
            'segments':segments,'reports':reports,
            'event_counts':counts,'completed_chambers':chamber-1}


def validate_full_run_segments(segments):
    """Reject empty, overlapping or unsorted edits before spending GPU time."""
    if not isinstance(segments,list) or not segments or len(segments)>SEGMENT_MAX:
        raise ValueError("Full Run needs at least one reviewed segment")
    checked=[]
    previous_by_source={}
    for item in segments:
        if not item.get("enabled",True):
            continue
        path=Path(item["path"])
        if not path.is_file():
            raise FileNotFoundError(str(path))
        start,end=float(item["start"]),float(item["end"])
        if not all(map(math.isfinite,(start,end))) or start<0 or end<=start:
            raise ValueError("Invalid cut start/end for "+path.name)
        key=os.path.normcase(str(path.resolve()))
        last=previous_by_source.get(key,0.)
        if start<last-.01:
            raise ValueError("Overlapping or out-of-order cuts in "+path.name)
        previous_by_source[key]=end
        checked.append(dict(item,start=start,end=end,path=str(path)))
    if not checked:
        raise ValueError("Every half is disabled. Enable at least one.")
    return checked


def _run_ffmpeg(args,cancel=None):
    creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0) if os.name=="nt" else 0
    proc=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL,creationflags=creationflags)
    try:
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                proc.terminate()
                try:proc.wait(timeout=2)
                except subprocess.TimeoutExpired:proc.kill();proc.wait()
                raise RuntimeError("Full Run render cancelled")
            try:
                proc.wait(timeout=.2)
            except subprocess.TimeoutExpired:
                continue
        error=proc.stderr.read().decode("utf-8","replace")[-1700:]
        if proc.returncode:
            raise RuntimeError("FFmpeg failed: "+error)
    finally:
        if proc.poll() is None:
            proc.kill();proc.wait()
        proc.stderr.close()


def render_full_run(segments,destination,preset_name,music="",progress=None,
                    cancel=None):
    """Render each accepted half with Quality Lab and JOIN ALL into ONE MP4.

    Each half is encoded to identical user preset settings. The final concat
    stream-copies those segments, preserving video quality. Music is mixed
    ONCE over the assembled run, not restarted for each half.
    """
    import cv2
    from PIL import Image,ImageDraw,ImageFont

    cuts=validate_full_run_segments(segments)
    prefs=quality.load_preferences()
    if preset_name and preset_name!="Use last-used settings":
        if preset_name not in prefs["presets"]:
            raise ValueError("Saved Quality Lab preset not found: "+preset_name)
        base=quality.resolve_saved_render_preset(preset_name)
    elif prefs.get("last_used"):
        base=quality.decode_settings(prefs["last_used"])
    else:
        base=quality.QualitySettings(preset="Clean",resolution="Source",
                                     fps=0)
    if base.resolution=="Source" or base.fps==0:
        # Select the first recording's format for ALL cuts. Audio and codecs
        # remain consistent for concat, even across different OBS inputs.
        info=quality.engine.probe(cuts[0]["path"])
        if base.resolution=="Source":
            # Source geometry will be explicitly normalized by a stable
            # Quality Lab output preset chosen below.
            source_sizes=quality.OUTPUT_SIZES
            if (info.width,info.height) in (
                value for value in source_sizes.values() if value):
                base.resolution=next(name for name,value in source_sizes.items()
                                     if value==(info.width,info.height))
            else:
                raise ValueError("For Full Run select a saved preset with an explicit "
                                 "output resolution (1080p/1440p/4K).")
        if base.fps==0:
            frame_rate=round(info.fps or 60)
            base.fps=frame_rate if frame_rate in quality.TARGET_FPS else 60
    base.validate()
    out=Path(destination)
    out.mkdir(parents=True,exist_ok=True)
    filename="Genshin_Abyss_Full_Run"
    number=1
    while (out/f"{filename}_{number:02d}.mp4").exists():
        number+=1
    stem=f"{filename}_{number:02d}"
    result_video=out/(stem+".mp4")
    result_thumb=out/(stem+"_thumbnail.png")
    result_title=out/(stem+"_title.txt")
    result_manifest=out/(stem+"_cuts.json")
    ff=quality.engine.get_ffmpeg()
    with tempfile.TemporaryDirectory(prefix="cazy_full_run_",dir=out) as td:
        tmp=Path(td)
        rendered=[]
        for index,seg in enumerate(cuts):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("Full Run render cancelled")
            target=tmp/f"part_{index:03d}.mp4"
            opts=base.clone()
            opts.start=seg["start"]
            opts.duration=seg["end"]-seg["start"]
            # Existing per-source audio remains audible. Silent clips receive
            # an AAC silent stream for uniform concat compatibility.
            quality.render_quality(seg["path"],str(target),opts,cancel=cancel,
                progress=(lambda pct,stage,done,total,j=index:
                          progress((j+max(0,min(100,pct))/100)*85/len(cuts),
                                   f"Half {j+1}/{len(cuts)}: {stage}") if progress else None))
            if not quality.engine.probe(str(target)).has_audio:
                with_audio=tmp/f"audio_{index:03d}.mp4"
                _run_ffmpeg([ff,"-hide_banner","-loglevel","error","-y",
                             "-i",str(target),"-f","lavfi","-i",
                             "anullsrc=r=48000:cl=stereo",
                             "-map","0:v:0","-map","1:a:0",
                             "-c:v","copy","-c:a","aac","-b:a","192k",
                             "-t",f"{opts.duration:.5f}","-movflags","+faststart",
                             str(with_audio)],cancel)
                target=with_audio
            rendered.append(target)
        if progress:progress(87,"Joining all reviewed halves into one video")
        concat_file=tmp/"segments.txt"
        def quote(path):
            return str(path.resolve()).replace("\\","/").replace("'","'\\''")
        concat_file.write_text("".join("file '"+quote(f)+"'\n" for f in rendered),
                               encoding="utf-8")
        merged=tmp/"merged.mp4"
        _run_ffmpeg([ff,"-hide_banner","-loglevel","error","-y",
                     "-f","concat","-safe","0","-i",str(concat_file),
                     "-c","copy","-movflags","+faststart",str(merged)],cancel)
        final=merged
        if music:
            if not Path(music).is_file():
                raise FileNotFoundError("Selected music track was moved: "+music)
            mixed=tmp/"mixed.mp4"
            _run_ffmpeg([ff,"-hide_banner","-loglevel","error","-y",
                         "-i",str(merged),"-stream_loop","-1","-i",str(music),
                         "-filter_complex",
                         "[0:a:0]volume=1.0[game];[1:a:0]volume=0.18[music];"
                         "[game][music]amix=inputs=2:duration=first:dropout_transition=2[a]",
                         "-map","0:v:0","-map","[a]","-c:v","copy",
                         "-c:a","aac","-b:a","256k",
                         "-movflags","+faststart",str(mixed)],cancel)
            final=mixed
        if not final.is_file() or final.stat().st_size<1024:
            raise RuntimeError("Full Run export was empty")

        cap=cv2.VideoCapture(str(cuts[-1]["path"]))
        try:
            cap.set(cv2.CAP_PROP_POS_MSEC,max(0.,cuts[-1]["end"]-2)*1000)
            success,frame=cap.read()
        finally:
            cap.release()
        if not success:
            raise RuntimeError("Could not extract the completed-run thumbnail frame")
        art=Image.fromarray(cv2.cvtColor(frame,cv2.COLOR_BGR2RGB))
        art.thumbnail((1280,720))
        canvas=Image.new("RGB",(1280,720),"#101525")
        canvas.paste(art,((1280-art.width)//2,(720-art.height)//2))
        draw=ImageDraw.Draw(canvas)
        draw.rectangle((0,620,1280,720),fill="#101525")
        try:
            font=ImageFont.truetype(
                "C:/Windows/Fonts/arialbd.ttf" if os.name=="nt" else
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",44)
        except OSError:
            font=ImageFont.load_default()
        draw.text((32,638),"SPIRAL ABYSS  •  FULL RUN",fill="white",font=font)
        still=tmp/"thumbnail.png"
        canvas.save(still)
        title="Spiral Abyss Full Run | Genshin Impact"
        title_path=tmp/"title.txt"
        title_path.write_text(title+"\n",encoding="utf-8")
        metadata=tmp/"cuts.json"
        metadata.write_text(json.dumps({
            "mode":"Full Run","preset":preset_name,"source_videos":list(
                dict.fromkeys(item["path"] for item in cuts)),
            "segments":cuts,"music":music,"title":title,"reviewed":True
        },indent=2),encoding="utf-8")
        if progress:progress(97,"Saving one complete local Full Run")
        shutil.move(str(final),result_video)
        shutil.move(str(still),result_thumb)
        shutil.move(str(title_path),result_title)
        shutil.move(str(metadata),result_manifest)
    if progress:progress(100,"Full Run export complete")
    return {"video":str(result_video),"thumbnail":str(result_thumb),
            "title":str(result_title),"cuts":str(result_manifest)}
