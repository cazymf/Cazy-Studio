"""Tk quality enhancement dialog for Cazy Studio. All processing is asynchronous."""
from __future__ import annotations
import copy
import os
import queue
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk, simpledialog
from PIL import Image, ImageDraw, ImageTk
import quality_engine as quality
import engine

BG='#0b101c';CARD='#141d2d';CARD2='#1c273a';TEXT='#edf3ff';MUTED='#92a4bd';ACCENT='#9b7cff';ACCENT2='#5abacb';GREEN='#50d2a3'

class QualityLab(tk.Toplevel):
    def __init__(self, master, suggested_path=None, timeline_clip=None, selected_preset=None):
        super().__init__(master)
        self.title('Cazy Studio • Quality Lab v0.3.13')
        self.geometry('1100x880')
        self.minsize(940,670)
        self.configure(bg=BG)
        self.path=None
        self.info=None
        self.timeline_clip=copy.deepcopy(timeline_clip) if timeline_clip is not None else None
        self.use_timeline_trim=tk.BooleanVar(value=bool(timeline_clip))
        self.options=quality.QualitySettings()
        self.busy=False
        self.cancel=threading.Event()
        self.jobs=queue.Queue()
        self.cache_dir=tempfile.TemporaryDirectory(prefix='cazy_compare_')
        self.compare_imgs=[]
        self.compare_tk=None
        self.last_export=None
        self.vars={}
        self._suspend_autosave=True
        self._autosave_task=None
        self.prefs_error=None
        try:
            self.prefs=quality.load_preferences()
        except ValueError as exc:
            self.prefs_error=str(exc)
            self.prefs=quality.empty_preferences()
        self._build()
        self._refresh_saved_presets()
        self._connect_autosave()
        self._suspend_autosave=False
        self._poll_after=self.after(100,self._poll)
        self.protocol('WM_DELETE_WINDOW',self._close)
        if suggested_path and Path(suggested_path).is_file():
            self.load_path(str(suggested_path))
        if selected_preset and selected_preset != 'Use last-used settings':
            self.apply_named_preset(selected_preset)
        if self.prefs_error:
            self.status.set(self.prefs_error + '  •  Existing file was left untouched.')

    def label(self,parent,text,color=MUTED,size=10,bold=False):
        w=tk.Label(parent,text=text,bg=parent.cget('bg'),fg=color,
                   font=('Segoe UI',size,'bold' if bold else 'normal'),anchor='w')
        return w

    def button(self,parent,text,command,primary=False):
        return tk.Button(parent,text=text,command=command,bg=ACCENT if primary else CARD2,
                         fg='#11101e' if primary else TEXT,bd=0,padx=13,pady=8,
                         font=('Segoe UI',10,'bold'),cursor='hand2')

    def option(self,parent,name,field,choices,row,col=0):
        group=tk.Frame(parent,bg=CARD)
        group.grid(row=row,column=col,sticky='ew',padx=8,pady=5)
        self.label(group,name).pack(anchor='w')
        val=getattr(self.options,field)
        var=tk.StringVar(value=('Source FPS' if val==0 else str(val)) if field=='fps' else str(val))
        self.vars[field]=var
        cb=ttk.Combobox(group,values=choices,textvariable=var,state='readonly',width=27,style='Field.TCombobox')
        cb.pack(fill='x',pady=(3,0))
        cb.bind('<<ComboboxSelected>>',self._apply_preset if field=='preset' else lambda e:self._show_recipe())
        return cb

    def slider(self,parent,name,field,low,high,row,col=0):
        g=tk.Frame(parent,bg=CARD);g.grid(row=row,column=col,sticky='ew',padx=8,pady=(5,2))
        self.label(g,name).pack(anchor='w')
        var=tk.IntVar(value=getattr(self.options,field));self.vars[field]=var
        tk.Scale(g,from_=low,to=high,orient='horizontal',variable=var,
                 showvalue=True,highlightthickness=0,bd=0,
                 command=lambda _:self._show_recipe(),bg=CARD,fg=TEXT,troughcolor=CARD2,
                 length=235).pack(fill='x')

    def _build(self):
        top=tk.Frame(self,bg=BG)
        top.pack(fill='x',padx=19,pady=(17,9))
        self.label(top,'CAZY QUALITY LAB',ACCENT,20,True).pack(side='left')
        self.label(top,'Native 120/240 FPS • Quality before editing',MUTED,10).pack(side='left',padx=17)
        self.button(top,'Choose Video',self.choose).pack(side='right')
        source=tk.Frame(self,bg=CARD,padx=14,pady=11)
        source.pack(fill='x',padx=19,pady=(0,10))
        self.source_label=self.label(source,'No video selected',TEXT,11,True)
        self.source_label.pack(fill='x')
        self.source_details=self.label(source,'Select a source clip to analyze FPS, dimensions, and duration.',MUTED,9)
        self.source_details.pack(fill='x',pady=(4,0))
        self.trim_toggle=tk.Checkbutton(source,text='Use selected timeline clip trim (not the full original)',
                                        variable=self.use_timeline_trim,command=self._toggle_timeline_trim,
                                        bg=CARD,fg=GREEN,selectcolor=CARD2,activebackground=CARD,
                                        activeforeground=GREEN,highlightthickness=0,anchor='w')
        self.trim_toggle.pack(fill='x',pady=(8,0))
        self.trim_description=self.label(source,'',GREEN,9)
        self.trim_description.pack(fill='x',pady=(2,0))
        body=tk.Frame(self,bg=BG)
        body.pack(fill='both',expand=True,padx=19)
        body.grid_columnconfigure(0,weight=2)
        body.grid_columnconfigure(1,weight=1)
        controls=tk.Frame(body,bg=CARD,padx=10,pady=10)
        controls.grid(row=0,column=0,sticky='nsew',padx=(0,8))
        controls.grid_columnconfigure(0,weight=1)
        controls.grid_columnconfigure(1,weight=1)
        self.option(controls,'Quality profile','preset',quality.PRESETS,0,0)
        self.option(controls,'Output frame rate','fps',['Source FPS','30','60','120','240'],0,1)
        self.option(controls,'Resolution','resolution',list(quality.OUTPUT_SIZES),1,0)
        self.option(controls,'Frame generation','frame_method',['Native / Duplicate','Optical Flow','RIFE AI'],1,1)
        self.option(controls,'Upscaling method','upscale',['Lanczos','Real-ESRGAN Anime','Real-ESRGAN General'],2,0)
        self.option(controls,'Vertical framing','fit',['Fill / crop','Fit / black bars'],2,1)
        self.slider(controls,'Denoise','denoise',0,3,3,0)
        self.slider(controls,'Sharpening','sharpen',0,100,3,1)
        self.slider(controls,'Motion blur / temporal mix','motion_blur',0,3,4,0)
        self.slider(controls,'Saturation %','saturation',80,150,4,1)
        self.slider(controls,'Contrast %','contrast',80,130,5,0)
        self.option(controls,'Video encoder','encoder',['Auto / NVIDIA NVENC','CPU x264','HEVC / NVIDIA NVENC','AV1 / NVIDIA NVENC'],5,1)
        self.option(controls,'Encoding quality','quality',quality.QUALITY_LEVELS,6,0)
        self.option(controls,'AI speed / real frames','ai_speed',['Fast (30 FPS)','Balanced (60 FPS)','Every output frame'],7,0)
        disk=tk.Frame(controls,bg=CARD);disk.grid(row=8,column=0,columnspan=2,sticky='ew',padx=8,pady=(10,1))
        self.label(disk,'AI scratch workspace (cropped frames; automatically cleaned)',MUTED,9).pack(anchor='w')
        line2=tk.Frame(disk,bg=CARD);line2.pack(fill='x')
        self.vars['scratch_dir']=tk.StringVar(value='')
        tk.Entry(line2,textvariable=self.vars['scratch_dir'],bg=CARD2,fg=TEXT,insertbackground=TEXT,relief='flat').pack(side='left',fill='x',expand=True,padx=(0,6))
        self.button(line2,'Browse',self.choose_scratch).pack(side='right')
        timeframe=tk.Frame(controls,bg=CARD);timeframe.grid(row=6,column=1,sticky='ew',padx=8,pady=5)
        self.label(timeframe,'Start (s) / Duration (s, 0 = full)').pack(anchor='w')
        line=tk.Frame(timeframe,bg=CARD);line.pack(fill='x',pady=(4,0))
        self.vars['start']=tk.StringVar(value='0');self.vars['duration']=tk.StringVar(value='0')
        self.start_entry=tk.Entry(line,textvariable=self.vars['start'],width=10,bg=CARD2,fg=TEXT,insertbackground=TEXT,bd=0,disabledbackground=CARD2,disabledforeground=MUTED)
        self.start_entry.pack(side='left',fill='x',expand=True,padx=(0,5))
        self.duration_entry=tk.Entry(line,textvariable=self.vars['duration'],width=10,bg=CARD2,fg=TEXT,insertbackground=TEXT,bd=0,disabledbackground=CARD2,disabledforeground=MUTED)
        self.duration_entry.pack(side='left',fill='x',expand=True)

        side=tk.Frame(body,bg=CARD,padx=13,pady=12)
        side.grid(row=0,column=1,sticky='nsew')
        self.label(side,'A / B QUALITY COMPARISON',ACCENT2,11,True).pack(anchor='w',pady=(0,11))
        self.compare_panel=tk.Canvas(side,bg='#0b101b',height=194,highlightthickness=0)
        self.compare_panel.pack(fill='x')
        self.compare_panel.bind('<Configure>',lambda e:self._paint_compare())
        self.compare_split=tk.DoubleVar(value=0.5)
        tk.Scale(side,from_=0,to=1,resolution=.01,orient='horizontal',variable=self.compare_split,
                 bg=CARD,fg=TEXT,troughcolor=CARD2,highlightthickness=0,showvalue=0,
                 command=lambda e:self._paint_compare()).pack(fill='x')
        self.label(side,'Slide to compare original vs enhanced. Still previews exclude temporal and AI models.',MUTED,9).pack(anchor='w',pady=(1,12))
        self.button(side,'COMPARE CURRENT FRAME',self.compare).pack(fill='x',pady=4)
        self.button(side,'RENDER 3-SECOND TEST',lambda:self.export(sample=True)).pack(fill='x',pady=4)
        self.button(side,'EXPORT ENHANCED MP4',lambda:self.export(sample=False),True).pack(fill='x',pady=(6,5))
        self.button(side,'Open Last Export',self.open_last).pack(fill='x',pady=4)
        self.button(side,'AI Addons Setup / Repair',self.addons_info).pack(fill='x',pady=4)
        self.label(side,'MY RENDER PRESETS',ACCENT2,10,True).pack(anchor='w',pady=(10,4))
        self.saved_var=tk.StringVar(value='')
        self.saved_cb=ttk.Combobox(side,textvariable=self.saved_var,values=[],state='readonly',style='Field.TCombobox')
        self.saved_cb.pack(fill='x',pady=(0,4))
        preset_buttons=tk.Frame(side,bg=CARD);preset_buttons.pack(fill='x')
        self.button(preset_buttons,'SAVE',self.save_render_preset).pack(side='left',padx=(0,5))
        self.button(preset_buttons,'LOAD',self.load_render_preset).pack(side='left',padx=(0,5))
        self.button(preset_buttons,'DELETE',self.delete_render_preset).pack(side='left')
        self.recipe=self.label(side,'',MUTED,9)
        self.recipe.pack(fill='x',pady=(13,0))
        self.addons_label=self.label(side,'',MUTED,9)
        self.addons_label.pack(fill='x',pady=(8,0))
        self._show_recipe()

        foot=tk.Frame(self,bg=BG)
        foot.pack(fill='x',padx=19,pady=(10,15))
        self.status=tk.StringVar(value='Choose a video. Quality Lab never changes your original source.')
        self.label(foot,'PROCESS STATUS',ACCENT2,9,True).pack(anchor='w')
        tk.Label(foot,textvariable=self.status,fg=TEXT,bg=BG,anchor='w',justify='left',wraplength=790).pack(side='left',fill='x',expand=True,pady=(3,0))
        self.button(foot,'Cancel Job',self.cancel_job).pack(side='right',padx=(9,0))
        self.progress_title=tk.StringVar(value='0%  •  Ready')
        self.label(foot,'RENDER PROGRESS  •  Live processing FPS + ETA',ACCENT2,9,True).pack(anchor='w',pady=(9,0))
        tk.Label(foot,textvariable=self.progress_title,fg=GREEN,bg=BG,anchor='w',
                 font=('Segoe UI',10,'bold')).pack(fill='x',pady=(2,2))
        self.progress_bar=ttk.Progressbar(foot,orient='horizontal',mode='determinate',maximum=100)
        self.progress_bar.pack(fill='x',pady=(2,3))

    def choose_scratch(self):
        folder=filedialog.askdirectory(parent=self,title='Choose AI scratch folder on a drive with free space')
        if folder:self.vars['scratch_dir'].set(folder)

    def choose(self):
        path=filedialog.askopenfilename(parent=self,title='Select high-FPS source footage',filetypes=[('Video files','*.mp4 *.mov *.mkv *.avi *.webm *.m4v'),('All files','*.*')])
        if path:
            self.timeline_clip=None
            self.use_timeline_trim.set(False)
            self.load_path(path)

    def load_path(self,path):
        try:
            info=engine.probe(path)
            if info.kind!='video':raise ValueError('Only video files are supported')
            # Keep the current recipe when switching footage. On first open,
            # restore the previous session's recipe (never an old trim).
            if self.info is not None:
                opts=self._collect()
            elif self.prefs.get('last_used'):
                opts=quality.decode_settings(self.prefs['last_used'])
            else:
                opts=quality.suggested_settings(info)
            self._suspend_autosave=True
            self.path=path;self.info=info;self.options=opts
            self.source_label.configure(text=Path(path).name[:85])
            self.source_details.configure(text=quality.media_summary(info))
            self._apply_render_settings(opts)
            self.vars['start'].set('0')
            self.vars['duration'].set('0')
            self._toggle_timeline_trim()
            self.status.set(f'Loaded {info.fps:.2f} FPS footage. Your render recipe is preserved.')
            self._show_recipe()
        except Exception as exc:messagebox.showerror('Open footage',str(exc),parent=self)
        finally:self._suspend_autosave=False

    def _apply_render_settings(self,opts):
        # Preserve the selected clip's own trim. A saved preset should NEVER
        # change its start/end points or render the full source accidentally.
        for k,var in self.vars.items():
            if k in ('start','duration'):
                continue
            x=getattr(opts,k)
            var.set('Source FPS' if k=='fps' and x==0 else str(x))
        self._show_recipe()

    def _connect_autosave(self):
        for key, var in self.vars.items():
            if key not in ('start','duration'):
                var.trace_add('write',self._schedule_autosave)

    def _schedule_autosave(self,*_):
        if self._suspend_autosave:
            return
        if self._autosave_task is not None:
            self.after_cancel(self._autosave_task)
        self._autosave_task=self.after(650,self._save_last_used)

    def _save_last_used(self):
        self._autosave_task=None
        if self._suspend_autosave or self.prefs_error:
            return
        try:
            opts=self._collect()
            self.prefs['last_used']=quality.encode_settings(opts)
            quality.save_preferences(self.prefs)
        except (ValueError,OSError):
            pass  # The user can still correct an incomplete field; keep old settings.

    def _refresh_saved_presets(self):
        names=sorted(self.prefs['presets'],key=str.casefold)
        self.saved_cb['values']=names
        if self.saved_var.get() not in names:
            self.saved_var.set('')

    def save_render_preset(self):
        if self.busy:
            return
        try:
            opts=self._collect()
        except Exception as exc:
            messagebox.showerror('Save render preset',str(exc),parent=self)
            return
        name=simpledialog.askstring('Save render preset',
            'Name this render recipe (resolution, FPS, AI, encoder, filters):',
            parent=self, initialvalue=self.saved_var.get() or 'My Genshin Quality')
        if name is None:
            return
        try:
            name=quality.validate_name(name)
            previous=quality.matching_name(self.prefs,name)
            if previous is not None and not messagebox.askyesno('Replace saved preset',
                f'Replace preset "{previous}" with your current settings?',parent=self):
                return
            quality.store_named(self.prefs,name,opts)
            quality.save_preferences(self.prefs)
            self.prefs_error=None
            self._refresh_saved_presets()
            self.saved_var.set(name)
            self.status.set(f'Saved render preset "{name}". It stays available after updates.')
        except (OSError,ValueError) as exc:
            messagebox.showerror('Save render preset',str(exc),parent=self)

    def apply_named_preset(self, name):
        """Load a named recipe into every visible Quality Lab control."""
        if self.busy:
            return
        try:
            # Reload from disk so a newly saved or updated preset is applied.
            prefs = quality.load_preferences()
            if name not in prefs['presets']:
                raise ValueError('Saved render preset not found: ' + name)
            opts = quality.decode_settings(prefs['presets'][name])
            self._suspend_autosave = True
            self.prefs = prefs
            self._refresh_saved_presets()
            self.saved_var.set(name)
            self.options = opts
            self._apply_render_settings(opts)
            self.status.set(f'Loaded saved preset "{name}" from AutoDirector. '
                            'These are the actual saved settings.')
        except (OSError, ValueError) as exc:
            messagebox.showerror('Load saved Quality Lab preset',str(exc),parent=self)
        finally:
            self._suspend_autosave = False

    def load_render_preset(self):
        if self.busy:
            return
        name=self.saved_var.get()
        if not name:
            messagebox.showinfo('Saved presets','Choose a saved preset first.',parent=self)
            return
        try:
            opts=quality.decode_settings(self.prefs['presets'][name])
            self._suspend_autosave=True
            self._apply_render_settings(opts)
            self._show_recipe()
            self.status.set(f'Loaded "{name}". Your selected timeline trim is unchanged.')
        except (KeyError,ValueError) as exc:
            messagebox.showerror('Load render preset',str(exc),parent=self)
        finally:
            self._suspend_autosave=False
        self._save_last_used()

    def delete_render_preset(self):
        if self.busy:
            return
        name=self.saved_var.get()
        if not name:
            return
        if not messagebox.askyesno('Delete render preset',f'Delete "{name}"?',parent=self):
            return
        try:
            del self.prefs['presets'][name]
            quality.save_preferences(self.prefs)
            self._refresh_saved_presets()
            self.status.set(f'Deleted render preset "{name}".')
        except (OSError,KeyError) as exc:
            messagebox.showerror('Delete render preset',str(exc),parent=self)

    def _active_timeline_trim(self):
        clip=self.timeline_clip
        if (clip is None or not self.use_timeline_trim.get() or not self.path
                or os.path.normcase(os.path.abspath(clip.path)) != os.path.normcase(os.path.abspath(self.path))):
            return None
        return clip

    def _toggle_timeline_trim(self):
        clip=self._active_timeline_trim()
        if clip is not None:
            self.vars['start'].set(f'{clip.start:.3f}')
            self.vars['duration'].set(f'{clip.end-clip.start:.3f}')
            self.trim_description.configure(text=f'Will render ONLY {clip.end-clip.start:.2f}s: source {clip.start:.2f}s → {clip.end:.2f}s')
        else:
            self.trim_description.configure(text='Use Start / Duration below to choose a source range.')
        available=(self.timeline_clip is not None and self.path and
                   os.path.normcase(os.path.abspath(self.timeline_clip.path)) == os.path.normcase(os.path.abspath(self.path)))
        self.trim_toggle.configure(state='normal' if available else 'disabled')
        for widget in (self.start_entry,self.duration_entry):
            widget.configure(state='disabled' if clip is not None else 'normal')
        self._show_recipe()

    def _apply_preset(self,event=None):
        if not self.info:
            self._show_recipe();return
        # Choosing a preset updates the entire recipe while preserving the
        # selected clip in/out point; all settings remain freely editable.
        try:
            chosen=self.vars['preset'].get()
            oldstart=self.vars['start'].get();oldduration=self.vars['duration'].get()
            preset=quality.suggested_settings(self.info,chosen)
            for k,v in self.vars.items():
                if k in ('start','duration','scratch_dir'):continue
                x=getattr(preset,k)
                v.set('Source FPS' if k=='fps' and x==0 else str(x))
            self.vars['start'].set(oldstart)
            self.vars['duration'].set(oldduration)
            self._show_recipe()
        except Exception as exc:
            self.status.set('Preset error: '+str(exc))

    def _collect(self):
        opts=quality.QualitySettings()
        for k,var in self.vars.items():
            raw=str(var.get()).strip()
            if k in ('start','duration'): value=float(raw)
            elif k=='fps': value=0 if raw=='Source FPS' else int(raw)
            elif k in ('denoise','sharpen','motion_blur','contrast','saturation'):value=int(raw)
            else:value=raw
            setattr(opts,k,value)
        clip=self._active_timeline_trim()
        if clip is not None:
            opts.start=float(clip.start)
            opts.duration=float(clip.end-clip.start)
        opts.validate()
        return opts

    def _show_recipe(self):
        try:
            opts=self._collect()
            if self.info:
                w,h,fps=quality.output_specs(self.info,opts)
                rate=quality.ai_processing_fps(self.info,opts,fps) if opts.upscale!='Lanczos' else fps
                scope=f'\nRender range: {opts.start:.2f}s → '+(f'{opts.start+opts.duration:.2f}s' if opts.duration else 'end of source')
                desc=f'Output: {w} × {h}  |  {fps} FPS\n{opts.upscale}  •  {opts.frame_method}\nAI real frames: {rate:g} FPS  •  {opts.encoder}'+scope
            else:desc='Video quality profiles\nBuilt-in FFmpeg or optional AI models'
            self.recipe.configure(text=desc)
            addons=quality.local_addons(Path(__file__).resolve().parent)
            self.addons_label.configure(text='RIFE: '+('Installed' if addons['RIFE'] else 'Not installed')+'  •  Real-ESRGAN: '+('Installed' if addons['Real-ESRGAN'] else 'Not installed'))
        except Exception:pass

    def _paint_compare(self):
        c=self.compare_panel
        w=max(60,c.winfo_width());h=max(50,c.winfo_height())
        if len(self.compare_imgs)<2:
            c.delete('all');c.create_text(w//2,h//2,text='Choose COMPARE CURRENT FRAME',fill=MUTED,font=('Segoe UI',10))
            return
        a,b=self.compare_imgs
        # letterbox both to same visual canvas size, independent of source aspect.
        def fitted(img):
            im=img.copy();im.thumbnail((w,h),Image.Resampling.LANCZOS)
            bg=Image.new('RGB',(w,h),'#090d15');bg.paste(im,((w-im.width)//2,(h-im.height)//2))
            return bg
        im_a=fitted(a);im_b=fitted(b)
        x=round(self.compare_split.get()*w)
        out=im_a.copy();out.paste(im_b.crop((x,0,w,h)),(x,0))
        d=ImageDraw.Draw(out);d.line((x,0,x,h),fill='#a98aff',width=3)
        self.compare_tk=ImageTk.PhotoImage(out)
        c.delete('all');c.create_image(0,0,image=self.compare_tk,anchor='nw')
        c.create_text(10,10,text='ORIGINAL',anchor='nw',fill='white',font=('Segoe UI',9,'bold'))
        c.create_text(w-10,10,text='ENHANCED',anchor='ne',fill='white',font=('Segoe UI',9,'bold'))

    def compare(self):
        if not self.path or self.busy:return
        try:opts=self._collect()
        except Exception as exc:messagebox.showerror('Settings',str(exc),parent=self);return
        self.busy=True;self.status.set('Rendering original vs enhanced comparison frame…')
        t=opts.start+min(1.0,opts.duration/2 if opts.duration else 1.0)
        def work():
            try:
                a,b=quality.compare_images(self.path,t,opts,self.cache_dir.name)
                self.jobs.put(('compare',a,b))
            except Exception as exc:self.jobs.put(('error',str(exc)))
        threading.Thread(target=work,daemon=True).start()

    def export(self,sample=False):
        if not self.path or self.busy:return
        try:opts=self._collect()
        except Exception as exc:messagebox.showerror('Settings',str(exc),parent=self);return
        suggested=Path(self.path).stem+('_test_3s' if sample else '_CazyQuality')+'.mp4'
        target=filedialog.asksaveasfilename(parent=self,title='Export enhanced MP4',initialfile=suggested,defaultextension='.mp4',filetypes=[('MP4 video','*.mp4')])
        if not target:return
        self._save_last_used()
        self.busy=True;self.cancel.clear();self.status.set('Preparing enhancement pipeline…')
        self.progress_bar['value']=0
        self.progress_title.set('0%  •  Preparing')
        def work():
            try:
                result=quality.render_quality(self.path,target,opts,
                              lambda s:self.jobs.put(('status',s)),self.cancel,sample,
                              root=str(Path(__file__).resolve().parent),
                              progress=lambda pct,stage,done,total:self.jobs.put(('render_progress',pct,stage,done,total)))
                self.jobs.put(('export',result))
            except Exception as exc:self.jobs.put(('error',str(exc)))
        threading.Thread(target=work,daemon=True).start()

    def cancel_job(self):
        if self.busy:self.cancel.set();self.status.set('Cancelling current job…')

    def open_last(self):
        if not self.last_export or not Path(self.last_export).is_file():
            messagebox.showinfo('Export','No completed export yet.',parent=self);return
        if os.name=='nt':os.startfile(self.last_export)
        else:
            import subprocess
            subprocess.Popen(['xdg-open',self.last_export])

    def addons_info(self):
        base=Path(__file__).resolve().parent
        guide=base/'Addons'/'README_AI_ADDONS.txt'
        msg=('True RIFE and Real-ESRGAN need their official portable Vulkan binaries and models.\n\n'
             'Cazy Studio can download both from GitHub when you explicitly approve.\n\n'
             'Install them now? (Opens the installer in a separate console.)')
        if os.name=='nt' and messagebox.askyesno('AI Addons Setup',msg,parent=self):
            installer=base/'Install_AI_Addons.bat'
            if not installer.exists():
                messagebox.showerror('AI Addons','Installer not found: '+str(installer),parent=self)
            else:
                os.startfile(str(installer))
                self.status.set('AI addon installer opened. Reopen Quality Lab after it completes.')
        elif os.name!='nt':
            messagebox.showinfo('AI Addons Setup','On Windows run Install_AI_Addons.bat, or manually extract the official ZIP packages.\n\n'+str(guide),parent=self)

    def _poll(self):
        try:
            while True:
                m=self.jobs.get_nowait()
                if m[0]=='status':self.status.set(m[1])
                elif m[0]=='render_progress':
                    pct,stage,done,total=m[1:]
                    self.progress_bar['value']=pct
                    detail=f'  •  {done:,}/{total:,} frames' if done is not None and total is not None else ''
                    self.progress_title.set(f'{pct:.1f}%  •  {stage}{detail}')
                elif m[0]=='compare':
                    self.compare_imgs=[Image.open(m[1]).convert('RGB'),Image.open(m[2]).convert('RGB')]
                    self._paint_compare();self.busy=False;self.status.set('A/B frame generated. Slide the divider; AI and temporal effects are not included in this still.')
                elif m[0]=='export':
                    self.busy=False;self.last_export=m[1]
                    self.progress_bar['value']=100
                    self.progress_title.set('100%  •  Complete')
                    self.status.set('Export complete: '+m[1])
                    messagebox.showinfo('Cazy Quality Lab','Enhanced MP4 saved:\n'+m[1],parent=self)
                elif m[0]=='error':
                    self.busy=False;self.status.set('Failed: '+m[1][-350:]);messagebox.showerror('Quality Lab',m[1][-1900:],parent=self)
        except queue.Empty:pass
        try:self._poll_after=self.after(120,self._poll)
        except tk.TclError:pass

    def _close(self):
        if self.busy:
            messagebox.showinfo('Processing','Cancel the running job before closing the window.',parent=self)
            return
        if self._autosave_task is not None:
            self.after_cancel(self._autosave_task)
            self._autosave_task=None
        self._save_last_used()
        self.destroy()

    def destroy(self):
        # Cancel scheduled Tk callbacks when the editor (or parent) shuts down.
        for name in ('_poll_after','_autosave_task'):
            task=getattr(self,name,None)
            if task:
                try:self.after_cancel(task)
                except tk.TclError:pass
                setattr(self,name,None)
        try:self.cache_dir.cleanup()
        except Exception:pass
        super().destroy()
