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
                                frame_values=re.findall(r'(?m)^frame=\s*(\d+)\s*
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
        target_count=max(count,int(round((count-1)*fps/source_fps))+1)
        exe=Path(addons['RIFE'])
        _run([str(exe),'-i',str(active),'-o',str(newdir),'-n',str(target_count),'-f','%08d.png'],
             status,cancel,cwd=str(exe.parent),progress_dir=newdir,
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
        model='realesr-animevideov3' if opts.upscale=='Real-ESRGAN Anime' else 'realesrgan-x4plus'
        import install_ai_addons
        model_dir=install_ai_addons.find_model_directory(exe,'RealESRGAN')
        if model_dir is None:raise RuntimeError('Real-ESRGAN model files are missing. Open AI Addons Setup to repair or reinstall.')
        target_count=sum(1 for _ in active.glob('*.png'))
        _run([str(exe),'-i',str(active),'-o',str(out),'-n',model,'-m',str(model_dir),'-s','2','-f','png'],
             status,cancel,cwd=str(exe.parent),progress_dir=out,
             progress_total=target_count,progress_label='Real-ESRGAN',
             on_progress=(lambda f,d,t:stage_progress('upscale',f,d,t)) if stage_progress else None)
        outcount=sum(1 for _ in out.glob('*.png'))
        if outcount!=target_count:
            raise RuntimeError(f'Real-ESRGAN processed {outcount}/{target_count} frames; cannot safely encode an incomplete sequence')
        if status:status('AI frames complete; freeing input-frame scratch space…')
        shutil.rmtree(active)
        active=out
    files=sorted(active.glob('*.png'))
    start_index=int(files[0].stem)
    return str(active/'%08d.png'), produced_fps, start_index


class RenderSpeedMeter:
    """Measure throughput from completed frame counters, not GPU utilization.

    Each pipeline stage has independent timing. Uses an 8-second moving window.
    The speed is processed frames per wall-clock second (AI processing FPS).
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
        items=self.samples.setdefault(stage,self._deque(maxlen=40))
        if items and done < items[-1][1]:
            items.clear()
        items.append((now,done))
        while len(items)>1 and now-items[0][0]>8.0:
            items.popleft()
        if len(items)<2:
            return ''
        seconds=now-items[0][0]
        frames=done-items[0][1]
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
    subprocess.run(base+['-vf',','.join(base_filters),a],check=True,capture_output=True)
    filters=_filter_chain(info,opts,only_image=True)+['scale=960:-2:flags=lanczos']
    subprocess.run(base+['-vf',','.join(filters),b],check=True,capture_output=True)
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


def _cazy_tesseract():
    """Find optional local OCR executable without an API or network call."""
    import shutil
    program = shutil.which('tesseract')
    if program:
        return program
    if os.name == 'nt':
        for key in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
            base = os.environ.get(key)
            if base:
                candidate = Path(base) / 'Tesseract-OCR' / 'tesseract.exe'
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
                                                timeout=8, check=False)
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
        opts = decode_settings(settings['presets'][preset_name])
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
            run = subprocess.run(cmd, capture_output=True, text=True, check=False)
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
,log)
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
                    on_progress(1.0, int(progress_total) if progress_dir else None,
                                int(progress_total) if progress_dir else None)
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
         ffmpeg_duration=dur)
    count=sum(1 for _ in frames_dir.glob('*.png'))
    if count<2:raise RuntimeError('Not enough frames extracted for the AI workflow')
    active=frames_dir
    produced_fps=processing_fps
    if rife:
        if status:status(f'RIFE interpolating {count} frames to {fps} FPS…')
        newdir=Path(tmp)/'rife_output';newdir.mkdir()
        target_count=max(count,int(round((count-1)*fps/source_fps))+1)
        exe=Path(addons['RIFE'])
        _run([str(exe),'-i',str(active),'-o',str(newdir),'-n',str(target_count),'-f','%08d.png'],
             status,cancel,cwd=str(exe.parent),progress_dir=newdir,
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
        model='realesr-animevideov3' if opts.upscale=='Real-ESRGAN Anime' else 'realesrgan-x4plus'
        import install_ai_addons
        model_dir=install_ai_addons.find_model_directory(exe,'RealESRGAN')
        if model_dir is None:raise RuntimeError('Real-ESRGAN model files are missing. Open AI Addons Setup to repair or reinstall.')
        target_count=sum(1 for _ in active.glob('*.png'))
        _run([str(exe),'-i',str(active),'-o',str(out),'-n',model,'-m',str(model_dir),'-s','2','-f','png'],
             status,cancel,cwd=str(exe.parent),progress_dir=out,
             progress_total=target_count,progress_label='Real-ESRGAN',
             on_progress=(lambda f,d,t:stage_progress('upscale',f,d,t)) if stage_progress else None)
        outcount=sum(1 for _ in out.glob('*.png'))
        if outcount!=target_count:
            raise RuntimeError(f'Real-ESRGAN processed {outcount}/{target_count} frames; cannot safely encode an incomplete sequence')
        if status:status('AI frames complete; freeing input-frame scratch space…')
        shutil.rmtree(active)
        active=out
    files=sorted(active.glob('*.png'))
    start_index=int(files[0].stem)
    return str(active/'%08d.png'), produced_fps, start_index


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
    def report(stage,fraction,done=None,total=None):
        low,high=stages.get(stage,(0,99.5))
        pct=min(99.5,max(last_pct[0],low+(high-low)*max(0.0,min(1.0,float(fraction)))))
        last_pct[0]=pct
        if progress:
            progress(pct,stage_labels.get(stage,stage),done,total)
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
                     ffmpeg_duration=dur)
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
    subprocess.run(base+['-vf',','.join(base_filters),a],check=True,capture_output=True)
    filters=_filter_chain(info,opts,only_image=True)+['scale=960:-2:flags=lanczos']
    subprocess.run(base+['-vf',','.join(filters),b],check=True,capture_output=True)
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


def _cazy_tesseract():
    """Find optional local OCR executable without an API or network call."""
    import shutil
    program = shutil.which('tesseract')
    if program:
        return program
    if os.name == 'nt':
        for key in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
            base = os.environ.get(key)
            if base:
                candidate = Path(base) / 'Tesseract-OCR' / 'tesseract.exe'
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
                                                timeout=8, check=False)
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
        opts = decode_settings(settings['presets'][preset_name])
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
            run = subprocess.run(cmd, capture_output=True, text=True, check=False)
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
