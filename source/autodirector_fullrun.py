"""Cazy AutoDirector: local full Spiral Abyss run detection and ONE-video export.

No cloud APIs and no YouTube integration. Nothing is auto-rendered without
user review. A *completed* Abyss chamber contributes both halves, in order.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import quality_engine as quality


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
    """Strict enough not to call a floor/benediction screen a completion."""
    simple = " ".join(re.sub(r"[^a-z0-9 ]", " ", str(text).lower()).split())
    if any(s in simple for s in ("challenge failed", "challenge failure",
                                  "challenge interrupted", "challenge ended",
                                  "challenge defeat")):
        return "failure"
    if any(s in simple for s in ("on the other side", "other side")):
        return "switch"
    if any(s in simple for s in ("challenge completed", "challenge complete")):
        return "complete"
    if any(s in simple for s in ("start challenge", "challenge started")):
        return "start"
    if ("floor" in simple and "chamber" in simple) or "benediction" in simple:
        return "blessing"
    return None


# Crop screen regions by event, relative to a 960x540 standardized frame.
# Cropping distinct overlays avoids matching moving monsters/backgrounds.
REF_REGIONS = {
    "start": (0.54, 0.44, 0.83, 0.58),
    "switch": (0.27, 0.46, 0.75, 0.65),
    "complete": (0.29, 0.26, 0.73, 0.41),
    "failure": (0.29, 0.26, 0.73, 0.41),
    "blessing": (0.30, 0.03, 0.71, 0.14),
}


def _crop(frame, kind, cv2):
    h, w = frame.shape[:2]
    x1,y1,x2,y2 = REF_REGIONS[kind]
    return cv2.cvtColor(frame[int(y1*h):int(y2*h),int(x1*w):int(x2*w)],
                        cv2.COLOR_BGR2GRAY)


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
            templates[kind].append(template)
    return templates


def match_reference(frame, templates, threshold=.82):
    """Compare each labelled example to its own UI region, independently.

    A high threshold avoids mistaking generic gold overlays for successes.
    Multiple examples per category are supported; no generic color-only success.
    """
    import cv2
    standardized = cv2.resize(frame, (960, 540), interpolation=cv2.INTER_AREA)
    best_kind, best_score = None, 0.0
    for kind, images in templates.items():
        if kind == "blessing":
            continue
        if not images:
            continue
        crop = cv2.GaussianBlur(_crop(standardized,kind,cv2),(3,3),0)
        for template in images:
            reference = cv2.GaussianBlur(template,(3,3),0)
            # Padded local search tolerates HUD motion / capture offsets.
            pad_x = max(5, int(crop.shape[1]*.09))
            pad_y = max(3, int(crop.shape[0]*.12))
            region = cv2.copyMakeBorder(crop,pad_y,pad_y,pad_x,pad_x,
                                        cv2.BORDER_REPLICATE)
            result = cv2.matchTemplate(region, reference,
                                       cv2.TM_CCOEFF_NORMED)
            score = float(cv2.minMaxLoc(result)[1])
            if math.isfinite(score) and score>best_score:
                best_score,best_kind=score,kind
    return (best_kind, round(best_score,3)) if best_score>=threshold else (None,best_score)


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
                first_start=max(0.,at)
                state="first"
            elif state=="between":
                second_start=max(0.,at)
                state="second"
            elif state=="first" and at-last_start>8:
                # A second attempt started before a valid side switch.
                discarded+=1
                first_start=max(0.,at)
                first_end=second_start=switch_at=None
            elif state=="second" and at-last_start>8:
                discarded+=1
                state="first"
                first_start=max(0.,at)
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


def scan_abyss_recording(path,references,progress=None,cancel=None,sample_seconds=1.0):
    """Scan the whole file; do not stop at the first completion event."""
    import cv2
    footage=Path(path)
    if not footage.is_file():
        raise FileNotFoundError(str(footage))
    refs=load_reference_bank(references)
    ocr=quality._cazy_tesseract()
    if not any(refs.values()) and not ocr:
        raise RuntimeError("Add screenshot references for Start Challenge, "
                           "On the other side and Challenge Completed, "
                           "or install Tesseract OCR first.")
    cap=cv2.VideoCapture(str(footage))
    if not cap.isOpened():
        raise RuntimeError("Could not open video: "+footage.name)
    try:
        fps=float(cap.get(cv2.CAP_PROP_FPS) or 0)
        total_frames=int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        duration=total_frames/fps if fps>0 else 0
        if duration<=0 or not math.isfinite(duration):
            raise RuntimeError("Invalid video duration")
        events=[]
        step=max(.4,min(3.,float(sample_seconds)))
        t=0.
        while t<duration:
            if cancel is not None and cancel.is_set():
                raise RuntimeError("Scan cancelled")
            cap.set(cv2.CAP_PROP_POS_MSEC, t*1000)
            ok,frame=cap.read()
            if ok:
                kind,confidence=match_reference(frame,refs)
                evidence="Screenshot reference"
                if kind is None and ocr:
                    h,w=frame.shape[:2]
                    # Prompt + central overlay; do NOT gate OCR on gold pixels.
                    center=frame[int(h*.21):int(h*.67),int(w*.14):int(w*.90)]
                    encoded,binary=cv2.imencode('.png',center)
                    if encoded:
                        try:
                            run=subprocess.run([ocr,'stdin','stdout','--psm','11','-l','eng'],
                                               input=binary.tobytes(),
                                               capture_output=True,timeout=6)
                            recognized=classify_screen_text(
                                run.stdout.decode('utf-8','replace'))
                            if recognized:
                                kind,confidence,evidence=recognized,.97,"Recognized game text"
                        except (OSError,subprocess.TimeoutExpired):
                            pass
                if kind:
                    events.append({"at":round(t,2),"kind":kind,
                                   "confidence":round(confidence,3),
                                   "evidence":evidence})
            if progress:
                progress(min(1.,t/duration))
            t+=step
        if progress:progress(1.)
        events=collapse_events(events)
        output=build_abyss_segments(events,footage,duration)
        return {"path":str(footage),"duration":duration,"ocr_available":bool(ocr),
                "events":events,**output}
    finally:
        cap.release()


def scan_full_run(paths,references,progress=None,cancel=None,sample_seconds=1.0):
    """Scan ALL user-selected recordings in their explicitly chosen order."""
    paths=list(paths)
    if not paths:
        raise ValueError("Add selected recordings first")
    segments=[]
    reports=[]
    chamber=1
    for i,path in enumerate(paths):
        report=scan_abyss_recording(
            path,references,cancel=cancel,sample_seconds=sample_seconds,
            progress=(lambda value,offset=i:
                      progress((offset+value)/len(paths))) if progress else None)
        for seg in report["segments"]:
            seg["chamber"]+=chamber-1
        chamber+=report["completed_chambers"]
        segments.extend(report["segments"])
        reports.append(report)
    if progress:progress(1.)
    return {"mode":"Full Run","segments":segments,"reports":reports,
            "completed_chambers":chamber-1}


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
        base=quality.decode_settings(prefs["presets"][preset_name])
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
