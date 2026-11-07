"""Cazy Studio 0.3.3 — project/source matching and high-FPS preview for Windows.
Open source Python/Tk UI; FFmpeg rendering lives in engine.py.
"""
from __future__ import annotations
import copy
import json
import random
import tempfile
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk, simpledialog

from PIL import Image, ImageDraw, ImageFont, ImageTk
import engine
import quality_engine
import quality_ui
import quality_engine as autodirector_fullrun
import update_center
import engine as preview_tools
from preview_worker import PreviewDecoder

try:
    import cv2
except ImportError:
    cv2 = None

BG = '#0b101c'
CARD = '#141d2d'
CARD2 = '#1c273a'
BORDER = '#2a3952'
TEXT = '#edf3ff'
MUTED = '#92a4bd'
ACCENT = '#9b7cff'
ACCENT2 = '#5abacb'
GREEN = '#50d2a3'
RED = '#f0788f'
YELLOW = '#eac16f'

class Studio(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Cazy Studio v0.3.15  |  Silent OCR')
        self.geometry('1530x930')
        self.minsize(1070, 710)
        self.configure(bg=BG)
        self.project = engine.Project()
        self.project_file: str | None = None
        self.media_paths: list[str] = []
        self.selected = -1
        self.playhead = 0.0
        self.playing = False
        self.play_last = time.monotonic()
        self.frame_last = 0.0
        self.preview_fps_var = tk.StringVar(value='30')
        self.preview_quality_var = tk.StringVar(value='Auto')
        self.preview_target = 30
        self.preview_actual = 0.0
        self.preview_count = 0
        self.preview_stats_from = time.monotonic()
        self.preview_decoder = PreviewDecoder()
        self.preview_generation = 0
        self.preview_caption = ''
        self._pending_resize = None
        self.quality_lab_window = None
        self.active_section = 'editor'
        self.cached_cap = None
        self.cached_path = None
        self.cached_pic = None
        self.cached_img = None
        self.preview_placeholder = None
        self.zoom = tk.DoubleVar(value=1.0)
        self.res_var = tk.StringVar(value='1280 × 720')
        self.fps_var = tk.StringVar(value='30')
        self.quality_var = tk.StringVar(value='High')
        self.undo_stack = []
        self.redo_stack = []
        self.jobs = queue.Queue()
        self.export_cancel = threading.Event()
        self.exporting = False
        self.status_var = tk.StringVar(value='Ready. Import some footage to start editing.')
        self._make_styles()
        self._make_menu()
        self._make_layout()
        self._bind_keys()
        self._show_inspector()
        self._draw_timeline()
        self.after(32, self._tick)
        self.after(25, self._poll_preview)
        self.after(120, self._poll_jobs)
        self.protocol('WM_DELETE_WINDOW', self.on_close)

    def _make_styles(self):
        style = ttk.Style(self)
        try: style.theme_use('clam')
        except tk.TclError: pass
        style.configure('Dark.TFrame', background=BG)
        style.configure('Card.TFrame', background=CARD)
        style.configure('Card.TLabel', background=CARD, foreground=TEXT, font=('Segoe UI', 10))
        style.configure('Muted.TLabel', background=CARD, foreground=MUTED, font=('Segoe UI', 9))
        style.configure('Field.TEntry', fieldbackground=CARD2, background=CARD2, foreground=TEXT, insertcolor=TEXT, bordercolor=BORDER)
        style.configure('Field.TCombobox', fieldbackground=CARD2, background=CARD2, foreground=TEXT,
                        arrowcolor=TEXT, bordercolor=BORDER)
        style.map('Field.TCombobox', fieldbackground=[('readonly',CARD2)], foreground=[('readonly',TEXT)])
        style.configure('Cazy.Horizontal.TProgressbar', troughcolor=CARD2, background=ACCENT, bordercolor=CARD2)

    def _make_menu(self):
        menu = tk.Menu(self, bg=CARD, fg=TEXT, activebackground=ACCENT, activeforeground='#101018', tearoff=0)
        filemenu = tk.Menu(menu, tearoff=0)
        filemenu.add_command(label='New Project                 Ctrl+N', command=self.new_project)
        filemenu.add_command(label='Open Project                 Ctrl+O', command=self.open_project)
        filemenu.add_command(label='Open Included Demo', command=self.open_demo)
        filemenu.add_command(label='Save Project                  Ctrl+S', command=self.save_project)
        filemenu.add_command(label='Save Project As...', command=lambda: self.save_project(True))
        filemenu.add_separator()
        filemenu.add_command(label='Import Media                    Ctrl+I', command=self.import_media)
        filemenu.add_command(label='Export Video                   Ctrl+E', command=self.export_dialog)
        filemenu.add_command(label='Quality Lab                     Ctrl+L', command=self.open_quality_lab)
        filemenu.add_command(label='AutoDirector Workspace       Ctrl+B', command=self.open_autodirector)
        filemenu.add_command(label='Check for Updates', command=self.open_update_center)
        filemenu.add_separator()
        filemenu.add_command(label='Exit', command=self.on_close)
        editmenu = tk.Menu(menu, tearoff=0)
        editmenu.add_command(label='Undo                       Ctrl+Z', command=self.undo)
        editmenu.add_command(label='Redo                       Ctrl+Y', command=self.redo)
        editmenu.add_separator()
        editmenu.add_command(label='Split Clip at Playhead        S', command=self.split_at_playhead)
        editmenu.add_command(label='Duplicate Clip             Ctrl+D', command=self.duplicate_clip)
        editmenu.add_command(label='Remove Selected           Delete', command=self.remove_clip)
        menu.add_cascade(label='File', menu=filemenu)
        menu.add_cascade(label='Edit', menu=editmenu)
        self.config(menu=menu)

    def _button(self, parent, label, cmd, primary=False, width=None, small=False):
        b = tk.Button(parent, text=label, command=cmd, bg=ACCENT if primary else CARD2,
                      fg='#11101e' if primary else TEXT, activebackground='#ba9dff' if primary else BORDER,
                      activeforeground='#11101e' if primary else TEXT, borderwidth=0,
                      cursor='hand2', font=('Segoe UI', 9 if small else 10, 'bold'),
                      padx=12 if not small else 8, pady=7 if not small else 5,
                      width=width)
        return b

    def _label(self, parent, text, size=10, color=TEXT, bold=False, bg=CARD):
        return tk.Label(parent, text=text, fg=color, bg=bg,
                        font=('Segoe UI',size,'bold' if bold else 'normal'), anchor='w')

    def _make_layout(self):
        toolbar = tk.Frame(self, bg=BG, height=75)
        toolbar.pack(side='top',fill='x',padx=16,pady=(13,7))
        toolbar.pack_propagate(False)
        tk.Label(toolbar,text='CAZY',bg=BG,fg=ACCENT,font=('Segoe UI',22,'bold')).pack(side='left')
        tk.Label(toolbar,text='STUDIO',bg=BG,fg=TEXT,font=('Segoe UI',22,'bold')).pack(side='left',padx=(5,22))
        tk.Label(toolbar,text='v0.3.11',bg=BG,fg=MUTED,font=('Segoe UI',9)).pack(side='left',padx=(0,12))
        self._button(toolbar,'EDITOR',self.show_editor,small=True).pack(side='left',padx=(0,5))
        self._button(toolbar,'✦ AUTODIRECTOR',self.open_autodirector,True,small=True).pack(side='left')
        self._button(toolbar,'Export MP4  ↗',self.export_dialog).pack(side='right',padx=(8,0))
        self._button(toolbar,'✦ QUALITY LAB',self.open_quality_lab,True).pack(side='right',padx=(8,0))
        self._button(toolbar,'↻ UPDATES',self.open_update_center,small=True).pack(side='right',padx=(8,0))
        self._button(toolbar,'Save Project',self.save_project).pack(side='right',padx=4)
        self._button(toolbar,'+ Import Media',self.import_media).pack(side='right',padx=4)
        self._button(toolbar,'Try Demo',self.open_demo,small=True).pack(side='right',padx=4)
        self._button(toolbar,'New',self.new_project,small=True).pack(side='right',padx=4)

        self.editor_workspace = tk.Frame(self,bg=BG)
        self.editor_workspace.pack(side='top',fill='both',expand=True)
        content = tk.Frame(self.editor_workspace,bg=BG)
        content.pack(side='top',fill='both',expand=True,padx=16,pady=3)
        content.grid_columnconfigure(0,weight=0,minsize=245)
        content.grid_columnconfigure(1,weight=1)
        content.grid_columnconfigure(2,weight=0,minsize=290)
        content.grid_rowconfigure(0,weight=1)
        self.media_panel = tk.Frame(content,bg=CARD,highlightbackground=BORDER,highlightthickness=1,width=245)
        self.media_panel.grid(row=0,column=0,sticky='nsew',padx=(0,9))
        self.media_panel.pack_propagate(False)
        self.preview_panel = tk.Frame(content,bg=CARD,highlightbackground=BORDER,highlightthickness=1)
        self.preview_panel.grid(row=0,column=1,sticky='nsew',padx=(0,9))
        self.inspect_panel = tk.Frame(content,bg=CARD,highlightbackground=BORDER,highlightthickness=1,width=290)
        self.inspect_panel.grid(row=0,column=2,sticky='nsew')
        self.inspect_panel.pack_propagate(False)

        self._label(self.media_panel,'MEDIA LIBRARY',11,ACCENT2,True).pack(anchor='w',padx=14,pady=(16,11))
        self._label(self.media_panel,'Import videos, images and audio.',9,MUTED).pack(anchor='w',padx=14)
        mf=tk.Frame(self.media_panel,bg=CARD)
        mf.pack(fill='x',padx=12,pady=12)
        self._button(mf,'+ Files',self.import_media,small=True).pack(side='left',padx=(0,5))
        self._button(mf,'→ Timeline',self.add_selected_media,small=True).pack(side='left')
        self.media_list=tk.Listbox(self.media_panel,bg=CARD,fg=TEXT,selectbackground='#43346e',
                                   selectforeground=TEXT,highlightthickness=0,bd=0,
                                   relief='flat',activestyle='none',font=('Segoe UI',10),exportselection=False)
        self.media_list.pack(fill='both',expand=True,padx=9,pady=(0,8))
        self.media_list.bind('<Double-Button-1>',lambda e:self.add_selected_media())
        self._button(self.media_panel,'♪ Set Background Music',self.pick_music,small=True).pack(fill='x',padx=12,pady=(4,7))
        self.music_status=self._label(self.media_panel,'No background audio',9,MUTED)
        self.music_status.pack(fill='x',padx=14,pady=(0,16))

        ph=tk.Frame(self.preview_panel,bg=CARD)
        ph.pack(fill='x',padx=16,pady=(14,8))
        self._label(ph,'PROGRAM MONITOR',11,ACCENT2,True).pack(side='left')
        self._label(ph,'PREVIEW FPS',9,MUTED).pack(side='left',padx=(15,5))
        self.preview_fps_cb=ttk.Combobox(ph,textvariable=self.preview_fps_var,values=['30','60','120'],width=5,state='readonly',style='Field.TCombobox')
        self.preview_fps_cb.pack(side='left')
        self.preview_fps_cb.bind('<<ComboboxSelected>>',self._change_preview_fps)
        self.preview_fps_stats=self._label(ph,'target 30 | actual --',9,MUTED)
        self.preview_fps_stats.pack(side='left',padx=(7,2))
        self.dim_label=self._label(ph,'PROJECT 1280 × 720  •  30 FPS',9,MUTED)
        self.dim_label.pack(side='right')
        monitor_settings = tk.Frame(self.preview_panel,bg=CARD)
        monitor_settings.pack(fill='x',padx=16,pady=(0,7))
        self._label(monitor_settings,'PREVIEW QUALITY',9,MUTED).pack(side='left',padx=(0,6))
        self.preview_quality_cb=ttk.Combobox(monitor_settings,textvariable=self.preview_quality_var,
            values=list(preview_tools.PREVIEW_QUALITIES),width=10,state='readonly',style='Field.TCombobox')
        self.preview_quality_cb.pack(side='left')
        self.preview_quality_cb.bind('<<ComboboxSelected>>',self._change_preview_quality)
        self._button(monitor_settings,'MATCH SOURCE',self.match_source,small=True).pack(side='right',padx=(6,0))
        self._button(monitor_settings,'PROJECT SETTINGS',self.project_settings_dialog,small=True).pack(side='right')
        self._label(monitor_settings,'Auto = fastest  |  output settings are separate',8,MUTED).pack(side='left',padx=(9,0))
        self.preview_area=tk.Frame(self.preview_panel,bg='#070b13')
        self.preview_area.pack(fill='both',expand=True,padx=16,pady=(0,9))
        self.preview_area.bind('<Configure>',self._preview_resized)
        self.preview_label=tk.Label(self.preview_area,bg='#070b13',fg=MUTED,
                                    text='Drop footage into the timeline to start editing',
                                    font=('Segoe UI',13))
        self.preview_label.pack(fill='both',expand=True)
        controls=tk.Frame(self.preview_panel,bg=CARD)
        controls.pack(fill='x',padx=16,pady=(2,15))
        self._button(controls,'◀  Frame',lambda:self.seek(self.playhead - 1/self.project.fps),small=True).pack(side='left')
        self.play_btn=self._button(controls,'▶  Play',self.toggle_play,width=9,small=True)
        self.play_btn.pack(side='left',padx=8)
        self._button(controls,'Frame  ▶',lambda:self.seek(self.playhead + 1/self.project.fps),small=True).pack(side='left')
        self.clock_label=self._label(controls,'00:00.00 / 00:00.00',10,ACCENT2,True)
        self.clock_label.pack(side='right')

        self._label(self.inspect_panel,'CLIP INSPECTOR',11,ACCENT2,True).pack(anchor='w',padx=14,pady=(16,8))
        self._label(self.inspect_panel,'Choose a timeline clip to edit.',9,MUTED).pack(anchor='w',padx=14,pady=(0,10))
        self.inspect_content=tk.Frame(self.inspect_panel,bg=CARD)
        self.inspect_content.pack(fill='both',expand=True,padx=14,pady=4)

        timeline_shell=tk.Frame(self.editor_workspace,bg=CARD,highlightbackground=BORDER,highlightthickness=1,height=272)
        timeline_shell.pack(side='bottom',fill='x',padx=16,pady=(5,0))
        timeline_shell.pack_propagate(False)
        th=tk.Frame(timeline_shell,bg=CARD)
        th.pack(fill='x',padx=12,pady=(10,6))
        self._label(th,'TIMELINE',11,ACCENT2,True).pack(side='left',padx=(0,15))
        self._button(th,'✂ Split',self.split_at_playhead,small=True).pack(side='left',padx=3)
        self._button(th,'Duplicate',self.duplicate_clip,small=True).pack(side='left',padx=3)
        self._button(th,'←',lambda:self.move_clip(-1),small=True).pack(side='left',padx=3)
        self._button(th,'→',lambda:self.move_clip(1),small=True).pack(side='left',padx=3)
        self._button(th,'Remove',self.remove_clip,small=True).pack(side='left',padx=3)
        tk.Label(th,text='ZOOM',bg=CARD,fg=MUTED,font=('Segoe UI',9)).pack(side='right')
        tk.Scale(th,from_=0.6,to=4.0,resolution=0.1,orient='horizontal',variable=self.zoom,
                 command=lambda _:self._draw_timeline(),length=140,bd=0,sliderrelief='flat',
                 highlightthickness=0,bg=CARD,fg=TEXT,troughcolor=CARD2,showvalue=False).pack(side='right')

        self.timeline=tk.Canvas(timeline_shell,bg='#0c1423',height=178,highlightthickness=0)
        self.timeline.pack(fill='both',expand=True,padx=11)
        sb=tk.Scrollbar(timeline_shell,orient='horizontal',command=self.timeline.xview)
        sb.pack(fill='x',padx=11,pady=(0,5))
        self.timeline.configure(xscrollcommand=sb.set)
        self.timeline.bind('<Button-1>',self._timeline_click)
        self.timeline.bind('<B1-Motion>',self._timeline_drag)
        self.timeline.bind('<Configure>',lambda _:self._draw_timeline())
        self.timeline.bind('<Button-3>',self._right_click_timeline)

        self.autodirector_workspace=tk.Frame(self,bg=BG)
        self.autodirector_view=AutoDirectorView(self.autodirector_workspace)
        self.autodirector_view.pack(fill='both',expand=True)
        footer=tk.Frame(self,bg=BG,height=25)
        self.footer=footer
        footer.pack(side='bottom',fill='x',padx=18)
        self.status_label=tk.Label(footer,textvariable=self.status_var,bg=BG,fg=MUTED,font=('Segoe UI',9),anchor='w')
        self.status_label.pack(side='left',fill='x',expand=True)
        self._label(footer,'QUALITY LAB  •  SOURCE MATCHING  •  HIGH-FPS MONITOR  •  S TO SPLIT',8,MUTED,bg=BG).pack(side='right')

    def _bind_keys(self):
        self.bind_all('<Control-i>',lambda e:self.import_media())
        self.bind_all('<Control-o>',lambda e:self.open_project())
        self.bind_all('<Control-n>',lambda e:self.new_project())
        self.bind_all('<Control-s>',lambda e:self.save_project())
        self.bind_all('<Control-e>',lambda e:self.export_dialog())
        self.bind_all('<Control-z>',lambda e:self.undo())
        self.bind_all('<Control-y>',lambda e:self.redo())
        self.bind_all('<Control-d>',lambda e:self.duplicate_clip())
        self.bind_all('<Control-l>',lambda e:self.open_quality_lab())
        self.bind_all('<Control-b>',lambda e:self.open_autodirector())
        self.bind_all('<Delete>',lambda e:self.remove_clip() if self._keyboard_edits_allowed() else None)
        self.bind_all('<space>',lambda e:self.toggle_play() if self._keyboard_edits_allowed() else None)
        self.bind_all('<Key-s>',lambda e:self.split_at_playhead() if self._keyboard_edits_allowed() else None)

    def _keyboard_edits_allowed(self):
        return self.active_section == 'editor' and not isinstance(self.focus_get(),(tk.Entry,tk.Text,tk.Spinbox,ttk.Entry,ttk.Combobox))

    def _snapshot(self):
        self.undo_stack.append(copy.deepcopy(self.project))
        if len(self.undo_stack)>55: self.undo_stack.pop(0)
        self.redo_stack.clear()

    def undo(self):
        if not self.undo_stack: return
        self.redo_stack.append(copy.deepcopy(self.project))
        self.project=self.undo_stack.pop()
        self._refresh_all()
        self.status_var.set('Undid last edit.')

    def redo(self):
        if not self.redo_stack: return
        self.undo_stack.append(copy.deepcopy(self.project))
        self.project=self.redo_stack.pop()
        self._refresh_all()
        self.status_var.set('Redid edit.')

    def _refresh_all(self):
        self.selected=min(self.selected,len(self.project.clips)-1)
        self.playhead=min(self.playhead,self.project.duration)
        self.dim_label.configure(text=f'PROJECT {self.project.width} × {self.project.height}  •  {self.project.fps} FPS')
        self.res_var.set(f'{self.project.width} × {self.project.height}')
        self.fps_var.set(str(self.project.fps))
        self.music_status.configure(text=Path(self.project.music_path).name[:27] if self.project.music_path else 'No background audio')
        self._draw_timeline()
        self._show_inspector()
        self.show_frame()
        self._update_clock()

    def new_project(self):
        if self.project.clips and not messagebox.askyesno('New project','Start a new project? Unsaved changes will be lost.'):
            return
        self.project=engine.Project()
        self.project_file=None
        self.selected=-1
        self.playhead=0
        self.media_paths=[]
        self.media_list.delete(0,'end')
        self._close_cached_capture()
        self.undo_stack.clear(); self.redo_stack.clear()
        self._refresh_all()
        self.status_var.set('New Cazy Studio project ready.')

    def import_media(self):
        paths=filedialog.askopenfilenames(title='Import footage, images or audio',
             filetypes=[('Media files','*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.png *.jpg *.jpeg *.webp *.bmp *.mp3 *.wav *.m4a *.flac *.ogg *.aac'),('All files','*.*')])
        if not paths:return
        n=0
        for p in paths:
            if engine.kind_for(p)=='unknown': continue
            if p not in self.media_paths:
                self.media_paths.append(p)
                k=engine.kind_for(p)
                sym={'video':'▣','image':'▧','audio':'♫'}.get(k,'·')
                self.media_list.insert('end',f'{sym}  {Path(p).name[:32]}')
                n+=1
        self.status_var.set(f'Imported {n} media file(s). Double-click one to add it to the timeline.')

    def add_selected_media(self):
        sel=self.media_list.curselection()
        if not sel:
            messagebox.showinfo('Media library','Select a video, image or audio file first.')
            return
        path=self.media_paths[sel[0]]
        if engine.kind_for(path)=='audio':
            self._snapshot()
            self.project.music_path=path
            self._refresh_all()
            self.status_var.set('Added background music: '+Path(path).name)
            return
        try:
            info=engine.probe(path)
            if info.duration<=0: raise ValueError('Media duration is unavailable.')
            self._snapshot()
            clip=engine.Clip(path,info.kind,0,info.duration if info.kind=='video' else 5)
            first_clip=not self.project.clips
            self.project.clips.append(clip)
            if first_clip and info.kind=='video':
                self.project.width,self.project.height,self.project.fps=preview_tools.source_project_format(
                    info.width,info.height,info.fps)
            self.selected=len(self.project.clips)-1
            self.seek(self.project.position_of(self.selected))
            self._refresh_all()
            self.status_var.set('Added to timeline: '+clip.label)
        except Exception as exc:
            messagebox.showerror('Cannot import media',str(exc))

    def pick_music(self):
        p=filedialog.askopenfilename(title='Choose background music',
               filetypes=[('Audio','*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.opus'),('All files','*.*')])
        if not p:return
        self._snapshot()
        self.project.music_path=p
        if p not in self.media_paths:
            self.media_paths.append(p)
            self.media_list.insert('end','♫  '+Path(p).name[:32])
        self._refresh_all()
        self.status_var.set('Background music set.')

    def _inspector_entry(self, parent, label, value, row, width=18):
        self._label(parent,label,9,MUTED).grid(row=row,column=0,sticky='w',pady=7)
        v=tk.StringVar(value=str(value))
        entry=ttk.Entry(parent,textvariable=v,style='Field.TEntry',width=width)
        entry.grid(row=row,column=1,sticky='ew',padx=(10,0),pady=7)
        return v

    def _show_inspector(self):
        for ch in self.inspect_content.winfo_children():ch.destroy()
        self.inspect_content.grid_columnconfigure(1,weight=1)
        if self.selected < 0 or self.selected>=len(self.project.clips):
            self._label(self.inspect_content,'No clip selected',11,MUTED).pack(anchor='w',pady=14)
            self._label(self.inspect_content,'Tip: double-click media to add it,\nthen click a clip on the timeline.',9,MUTED).pack(anchor='w')
            ttk.Separator(self.inspect_content,orient='horizontal').pack(fill='x',pady=22)
            self._music_inspector()
            return
        clip=self.project.clips[self.selected]
        self._label(self.inspect_content,clip.label[:31],10,TEXT,True).grid(row=0,column=0,columnspan=2,sticky='w',pady=(0,16))
        self.start_var=self._inspector_entry(self.inspect_content,'Trim in (s)',f'{clip.start:.3f}',1)
        self.end_var=self._inspector_entry(self.inspect_content,'Trim out (s)',f'{clip.end:.3f}',2)
        self.speed_var=self._inspector_entry(self.inspect_content,'Speed',f'{clip.speed:.2f}',3)
        self.volume_var=self._inspector_entry(self.inspect_content,'Volume',f'{clip.volume:.2f}',4)
        self._label(self.inspect_content,'Caption',9,MUTED).grid(row=5,column=0,columnspan=2,sticky='w',pady=(12,4))
        self.caption_box=tk.Text(self.inspect_content,height=4,bg=CARD2,fg=TEXT,insertbackground=TEXT,
                                 relief='flat',font=('Segoe UI',10),wrap='word',highlightthickness=1,
                                 highlightbackground=BORDER)
        self.caption_box.grid(row=6,column=0,columnspan=2,sticky='ew')
        self.caption_box.insert('1.0',clip.caption)
        self._button(self.inspect_content,'APPLY CLIP CHANGES',self.apply_inspector,True).grid(row=7,column=0,columnspan=2,sticky='ew',pady=(17,7))
        self._label(self.inspect_content,'Speed range: 0.10× – 4.00×',9,MUTED).grid(row=8,column=0,columnspan=2,sticky='w')
        self._label(self.inspect_content,'Volume: 0.00 mute, 1.00 normal',9,MUTED).grid(row=9,column=0,columnspan=2,sticky='w',pady=2)
        ttk.Separator(self.inspect_content,orient='horizontal').grid(row=10,column=0,columnspan=2,sticky='ew',pady=22)
        self._label(self.inspect_content,'TIMELINE CLIP',9,ACCENT2,True).grid(row=11,column=0,columnspan=2,sticky='w')
        self._label(self.inspect_content,f'Duration: {engine.fmt_time(clip.duration)}',10,MUTED).grid(row=12,column=0,columnspan=2,sticky='w',pady=6)
        self._label(self.inspect_content,f'Position: {engine.fmt_time(self.project.position_of(self.selected))}',10,MUTED).grid(row=13,column=0,columnspan=2,sticky='w')
        if clip.kind=='video':
            self._button(self.inspect_content,'AUTO SLOW-MO (60 FPS)',self.natural_slow_motion,small=True).grid(row=14,column=0,columnspan=2,sticky='ew',pady=(17,6))
            try:
                source_fps=engine.probe(clip.path).fps
                self._label(self.inspect_content,f'Source: {source_fps:.1f} FPS',9,MUTED).grid(row=15,column=0,columnspan=2,sticky='w')
            except Exception:pass


    def _music_inspector(self):
        music=self.project.music_path
        self._label(self.inspect_content,'BACKGROUND MUSIC',10,ACCENT2,True).pack(anchor='w')
        self._label(self.inspect_content,Path(music).name[:32] if music else 'Not set',9,MUTED).pack(anchor='w',pady=(5,13))
        self._button(self.inspect_content,'Choose Audio',self.pick_music,small=True).pack(anchor='w')
        if music:
            box=tk.Frame(self.inspect_content,bg=CARD)
            box.pack(fill='x',pady=(10,0))
            self.mv=tk.StringVar(value=f'{self.project.music_volume:.2f}')
            self.mo=tk.StringVar(value=f'{self.project.music_offset:.2f}')
            self._label(box,'Music volume (0–2)',9,MUTED).pack(anchor='w')
            ttk.Entry(box,textvariable=self.mv,style='Field.TEntry').pack(fill='x',pady=3)
            self._label(box,'Start offset (seconds)',9,MUTED).pack(anchor='w',pady=(7,0))
            ttk.Entry(box,textvariable=self.mo,style='Field.TEntry').pack(fill='x',pady=3)
            self._button(box,'Apply Music Settings',self.apply_music,small=True).pack(fill='x',pady=(9,5))
            self._button(box,'Remove Background Music',self.remove_music,small=True).pack(fill='x')

    def apply_music(self):
        try:
            vol=float(self.mv.get()); off=float(self.mo.get())
            if not 0<=vol<=2 or not 0<=off<=self.project.duration+3600: raise ValueError('Invalid volume or offset.')
            self._snapshot(); self.project.music_volume=vol;self.project.music_offset=off
            self._draw_timeline(); self.status_var.set('Music settings applied.')
        except ValueError as e:messagebox.showerror('Music settings',str(e))

    def remove_music(self):
        self._snapshot();self.project.music_path='';self._refresh_all()

    def natural_slow_motion(self):
        if self.selected<0:return
        clip=self.project.clips[self.selected]
        if clip.kind!='video':return
        try:
            fps=engine.probe(clip.path).fps
            if fps<=60:raise ValueError('This source is 60 FPS or lower. Use a 120/240 FPS recording for true captured-frame slow motion.')
            speed=60/fps
            if not .10<=speed<=4:raise ValueError('Source FPS is outside this editor speed range.')
            self._snapshot()
            clip.speed=speed
            self._refresh_all()
            self.status_var.set(f'Natural slow motion: {fps:.0f} FPS source → 60 FPS at {speed:.3f}× speed. Export at 60 FPS.')
        except Exception as exc:messagebox.showerror('Natural slow motion',str(exc))

    def apply_inspector(self):
        if self.selected<0:return
        c=self.project.clips[self.selected]
        try:
            start=float(self.start_var.get());end=float(self.end_var.get())
            speed=float(self.speed_var.get());vol=float(self.volume_var.get())
            info=engine.probe(c.path)
            if c.kind=='video' and end > info.duration+0.02:
                raise ValueError(f'Trim out cannot exceed source duration {info.duration:.2f}s.')
            trial=copy.deepcopy(c)
            trial.start,trial.end,trial.speed,trial.volume=start,end,speed,vol
            trial.caption=self.caption_box.get('1.0','end-1c').strip()
            trial.validate()
            self._snapshot()
            self.project.clips[self.selected]=trial
            self._refresh_all()
            self.status_var.set('Clip changes applied.')
        except Exception as exc:messagebox.showerror('Invalid clip settings',str(exc))

    def remove_clip(self):
        if self.selected < 0 or self.selected >= len(self.project.clips): return
        self._snapshot();self.project.clips.pop(self.selected)
        self.selected=min(self.selected,len(self.project.clips)-1)
        self.playhead=min(self.playhead,self.project.duration)
        self._refresh_all()
        self.status_var.set('Removed selected clip.')

    def duplicate_clip(self):
        if self.selected<0:return
        self._snapshot()
        self.project.clips.insert(self.selected+1,copy.deepcopy(self.project.clips[self.selected]))
        self.selected+=1;self._refresh_all()

    def move_clip(self,direction):
        j=self.selected+direction
        if self.selected<0 or j<0 or j>=len(self.project.clips):return
        self._snapshot()
        self.project.clips[j],self.project.clips[self.selected]=self.project.clips[self.selected],self.project.clips[j]
        self.selected=j;self._refresh_all()

    def split_at_playhead(self):
        idx, c,local=self.project.clip_at(self.playhead)
        if c is None:return
        self._snapshot()
        if engine.split_clip(self.project,idx,self.playhead):
            self.selected=idx+1;self._refresh_all();self.status_var.set('Split at playhead.')
        else:
            self.undo_stack.pop()
            self.status_var.set('Move the playhead inside a clip to split.')

    def _timeline_params(self):
        width=max(560,self.timeline.winfo_width())
        pps=(width-105)/max(self.project.duration,15.)*self.zoom.get()
        return 75.,pps

    def _draw_timeline(self):
        if not hasattr(self,'timeline'):return
        canvas=self.timeline
        c=canvas
        c.delete('all')
        t0,pps=self._timeline_params()
        w=max(c.winfo_width(),int(t0 + max(self.project.duration,15)*pps+50))
        c.configure(scrollregion=(0,0,w,176))
        c.create_rectangle(0,0,w,176,fill='#0c1423',outline='')
        c.create_rectangle(0,0,68,176,fill='#141d2d',outline='')
        c.create_text(10,88,text='VIDEO',anchor='w',fill=ACCENT2,font=('Segoe UI',9,'bold'))
        c.create_text(10,156,text='MUSIC',anchor='w',fill=MUTED,font=('Segoe UI',9,'bold'))
        total=max(15,self.project.duration)
        if total<40:step=5
        elif total<120:step=10
        elif total<360:step=30
        else:step=60
        for s in range(0,int(total)+step,step):
            x=t0+s*pps
            c.create_line(x,23,x,173,fill='#223148')
            c.create_text(x+4,9,text=engine.fmt_time(s)[:-3],anchor='nw',fill=MUTED,font=('Consolas',9))
        c.create_rectangle(70,43,w-6,120,fill='#111a2c',outline='#30435d')
        c.create_rectangle(70,133,w-6,173,fill='#111a2c',outline='#30435d')
        x=t0
        for i,clip in enumerate(self.project.clips):
            cw=max(2,clip.duration*pps)
            selected=(i==self.selected)
            col='#7051b8' if selected else ('#3a6593' if clip.kind=='video' else '#427e6d')
            c.create_rectangle(x+1,48,x+cw-1,116,fill=col,outline=ACCENT if selected else '#516d88',width=2 if selected else 1)
            if cw>40:
                name=clip.label[:max(2,int(cw/8))]
                c.create_text(x+8,68,anchor='w',text=name,fill='#f1f5ff',font=('Segoe UI',10,'bold'))
            if cw>80:
                c.create_text(x+8,92,anchor='w',text=f'{clip.duration:.1f}s  •  {clip.speed:g}×',fill='#c6d4ec',font=('Segoe UI',9))
            x+=cw
        if self.project.music_path:
            mx=t0+self.project.music_offset*pps
            c.create_rectangle(mx,139,t0+self.project.duration*pps,169,fill='#1b655d',outline='#6ce3c2')
            c.create_text(mx+8,154,text='♫ '+Path(self.project.music_path).name[:70],fill='#d6fff2',anchor='w',font=('Segoe UI',10))
        px=t0+self.playhead*pps
        c.create_line(px,24,px,173,fill=YELLOW,width=2,tags=('playhead',))
        c.create_polygon(px-7,23,px+7,23,px,36,fill=YELLOW,outline='',tags=('playhead',))

    def _update_timeline_playhead(self):
        if not hasattr(self,'timeline'):return
        t0,pps=self._timeline_params()
        px=t0+self.playhead*pps
        c=self.timeline
        ids=c.find_withtag('playhead')
        if len(ids)>=2:
            c.coords(ids[0],px,24,px,173)
            c.coords(ids[1],px-7,23,px+7,23,px,36)

    def _time_at_canvas_x(self,x):
        t0,pps=self._timeline_params()
        return max(0,min(self.project.duration,(self.timeline.canvasx(x)-t0)/pps))

    def _timeline_click(self,event):
        t=self._time_at_canvas_x(event.x)
        self.seek(t)
        if 43<=event.y<=123:
            idx,clip,_=self.project.clip_at(t)
            if idx!=self.selected:
                self.selected=idx
                self._show_inspector()
                self._draw_timeline()

    def _timeline_drag(self,event):
        self.seek(self._time_at_canvas_x(event.x))

    def _right_click_timeline(self,event):
        self._timeline_click(event)
        menu=tk.Menu(self,tearoff=0)
        menu.add_command(label='Split at Playhead',command=self.split_at_playhead)
        menu.add_command(label='Duplicate Clip',command=self.duplicate_clip)
        menu.add_command(label='Remove Clip',command=self.remove_clip)
        menu.tk_popup(event.x_root,event.y_root)

    def _change_preview_fps(self,event=None):
        try:
            value=int(self.preview_fps_var.get())
            if value not in (30,60,120):raise ValueError()
            self.preview_target=value
            self.frame_last=0.0
            self.preview_count=0
            self.preview_stats_from=time.monotonic()
            self.preview_fps_stats.configure(text=f'target {value} | actual --')
            self.status_var.set(f'Preview target set to {value} FPS (independent of source/export FPS).')
        except ValueError:
            self.preview_fps_var.set(str(self.preview_target))

    def _change_preview_quality(self,event=None):
        quality=self.preview_quality_var.get()
        if quality not in preview_tools.PREVIEW_QUALITIES:
            self.preview_quality_var.set('Auto')
            return
        self._close_cached_capture()
        self.show_frame()
        self.status_var.set(f'Preview processing quality: {quality}. Display pixels depend on monitor size.')

    def _source_for_project(self):
        candidates=[]
        if 0<=self.selected<len(self.project.clips):
            candidates.append(self.project.clips[self.selected])
        candidates.extend(self.project.clips)
        for c in candidates:
            if c.kind=='video' and os.path.isfile(c.path):return c.path
        for path in self.media_paths:
            if engine.kind_for(path)=='video' and os.path.isfile(path):return path
        return None

    def match_source(self):
        path=self._source_for_project()
        if not path:
            messagebox.showinfo('Match source','Import a video and add it to the timeline first.')
            return
        try:
            source=engine.probe(path)
            w,h,fps=preview_tools.source_project_format(source.width,source.height,source.fps)
            self._snapshot()
            self.project.width,self.project.height,self.project.fps=w,h,fps
            self._refresh_all()
            self.status_var.set(f'Project matched to {w}×{h} at {fps} FPS ({Path(path).name}).')
        except Exception as exc:messagebox.showerror('Match source',str(exc))

    def project_settings_dialog(self):
        dialog=tk.Toplevel(self)
        dialog.title('Project Format (independent of preview and export)')
        dialog.configure(bg=CARD)
        dialog.geometry('460x345')
        dialog.resizable(False,False)
        dialog.transient(self);dialog.grab_set()
        self._label(dialog,'PROJECT RESOLUTION & FRAME RATE',13,ACCENT2,True).pack(anchor='w',padx=20,pady=(18,14))
        self._label(dialog,'Project resolution',9,MUTED).pack(anchor='w',padx=20)
        res_var=tk.StringVar(value=f'{self.project.width} × {self.project.height}')
        resolution=ttk.Combobox(dialog,state='readonly',values=preview_tools.PROJECT_RESOLUTIONS,
                                 textvariable=res_var,style='Field.TCombobox')
        resolution.pack(fill='x',padx=20,pady=(3,13))
        self._label(dialog,'Project frame rate',9,MUTED).pack(anchor='w',padx=20)
        fps_var=tk.StringVar(value=str(self.project.fps))
        ttk.Combobox(dialog,state='readonly',values=['24','30','60','120','240'],
                     textvariable=fps_var,style='Field.TCombobox').pack(fill='x',padx=20,pady=(3,13))
        self._label(dialog,'Preview FPS and preview quality are changed above the monitor.',9,MUTED).pack(anchor='w',padx=20,pady=(0,13))
        def apply():
            try:
                w,h=preview_tools.parse_resolution(res_var.get())
                fps=int(fps_var.get())
                if fps not in (24,30,60,120,240):raise ValueError('Unsupported project FPS')
                self._snapshot()
                self.project.width,self.project.height,self.project.fps=w,h,fps
                self._refresh_all()
                dialog.destroy()
            except Exception as exc:messagebox.showerror('Project format',str(exc),parent=dialog)
        self._button(dialog,'APPLY PROJECT FORMAT',apply,True).pack(fill='x',padx=20)

    def open_autodirector(self):
        if self.active_section == 'autodirector':return
        self.playing=False
        self.play_btn.configure(text='▶  Play')
        self.editor_workspace.pack_forget()
        self.autodirector_workspace.pack(side='top',fill='both',expand=True,before=self.footer)
        self.active_section='autodirector'
        self.autodirector_view.refresh()
        self.status_var.set('AutoDirector: select only the videos you want. Cutting and rendering are future stages.')

    def show_editor(self):
        if self.active_section == 'editor':return
        self.autodirector_view.save()
        self.autodirector_workspace.pack_forget()
        self.editor_workspace.pack(side='top',fill='both',expand=True,before=self.footer)
        self.active_section='editor'
        self.status_var.set('Editor ready. Your selected AutoDirector videos stay saved between sessions.')

    def open_update_center(self):
        update_center.open_update_center(self)

    def open_quality_lab(self, selected_preset=None, source_path=None):
        # Opening Quality Lab from AutoDirector must show its selected recipe,
        # rather than the last-used or hardcoded default settings.
        if selected_preset is None and self.active_section == 'autodirector':
            selected_preset = self.autodirector_view.preset_var.get()
            if source_path is None:
                i = self.autodirector_view._selected()
                if i >= 0:
                    source_path = self.autodirector_view.state['queue'][i]['path']
        if self.quality_lab_window is not None:
            try:
                if self.quality_lab_window.winfo_exists():
                    if source_path and Path(source_path).is_file():
                        self.quality_lab_window.load_path(source_path)
                    if selected_preset and selected_preset != 'Use last-used settings':
                        self.quality_lab_window.apply_named_preset(selected_preset)
                    self.quality_lab_window.lift()
                    return
            except tk.TclError:pass
        preferred=source_path if source_path and Path(source_path).is_file() else None
        timeline_clip=None
        if preferred is None and 0<=self.selected<len(self.project.clips):
            clip=self.project.clips[self.selected]
            if clip.kind=='video':
                preferred=clip.path
                timeline_clip=copy.deepcopy(clip)
        if preferred is None:
            for p in self.media_paths:
                if engine.kind_for(p)=='video' and os.path.isfile(p):
                    preferred=p;break
        self.quality_lab_window=quality_ui.QualityLab(
            self,preferred,timeline_clip=timeline_clip,selected_preset=selected_preset)

    def _close_cached_capture(self):
        # A worker thread owns the video capture. Never release it on the UI thread.
        self.preview_generation = self.preview_decoder.invalidate(reset=True)
        self.cached_pic = None

    def _preview_resized(self, event=None):
        # Configure events can fire repeatedly while resizing the window.
        if self._pending_resize is not None:
            self.after_cancel(self._pending_resize)
        self._pending_resize=self.after(90,self._redraw_preview_after_resize)

    def _redraw_preview_after_resize(self):
        self._pending_resize=None
        self.show_frame()

    def show_frame(self):
        if not hasattr(self,'preview_label'):
            return
        idx,clip,local=self.project.clip_at(self.playhead)
        if clip is None:
            self.preview_generation=self.preview_decoder.invalidate()
            self.preview_label.configure(image='',text='Import your first clip  ↗\n\nThen add it to the timeline.')
            self.cached_pic=None
            return
        source_time=clip.start+local*clip.speed
        self.preview_caption=clip.caption.splitlines()[0][:100] if clip.caption.strip() else ''
        self.preview_generation=self.preview_decoder.request(
            clip.path, clip.kind, source_time,
            max(160,self.preview_area.winfo_width()-4),
            max(100,self.preview_area.winfo_height()-4),
            self.preview_quality_var.get())

    def _poll_preview(self):
        try:
            newest=None
            try:
                while True:
                    newest=self.preview_decoder.results.get_nowait()
            except queue.Empty:
                pass
            if newest is not None:
                sequence,frame,error=newest
                if sequence==self.preview_generation:
                    if error:
                        self.preview_label.configure(image='',text='Cannot preview this frame\n'+error[:130])
                        self.cached_pic=None
                    elif frame is not None:
                        width=max(160,self.preview_area.winfo_width()-4)
                        height=max(100,self.preview_area.winfo_height()-4)
                        frame.thumbnail((width,height),Image.Resampling.BILINEAR)
                        if self.preview_caption:
                            draw=ImageDraw.Draw(frame)
                            fontsize=max(13,frame.width//32)
                            try:
                                fontpath=('C:/Windows/Fonts/arialbd.ttf' if os.name=='nt' else '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf')
                                font=ImageFont.truetype(fontpath,fontsize)
                            except OSError:
                                font=ImageFont.load_default()
                            bbox=draw.textbbox((0,0),self.preview_caption,font=font)
                            x=(frame.width-(bbox[2]-bbox[0]))/2
                            draw.text((x,frame.height*.8),self.preview_caption,font=font,
                                      fill='white',stroke_width=2,stroke_fill='black')
                        self.cached_pic=ImageTk.PhotoImage(frame)
                        self.preview_label.configure(image=self.cached_pic,text='')
                        self.preview_count+=1
        except tk.TclError:
            return
        self.after(25,self._poll_preview)

    def _update_clock(self):
        if hasattr(self,'clock_label'):
            self.clock_label.configure(text=f'{engine.fmt_time(self.playhead)} / {engine.fmt_time(self.project.duration)}')

    def seek(self,t):
        self.playhead=max(0,min(t,self.project.duration))
        self.show_frame();self._update_timeline_playhead();self._update_clock()

    def toggle_play(self):
        if not self.project.clips:return
        self.playing=not self.playing
        if self.playing and self.playhead>=self.project.duration: self.playhead=0
        self.play_last=time.monotonic()
        self.play_btn.configure(text='❚❚  Pause' if self.playing else '▶  Play')

    def _tick(self):
        try:
            if self.playing:
                now=time.monotonic()
                self.playhead+=now-self.play_last
                self.play_last=now
                if self.playhead>=self.project.duration:
                    self.playhead=self.project.duration
                    self.playing=False
                    self.play_btn.configure(text='▶  Play')
                if now-self.frame_last >= 1.0/max(1,self.preview_target):
                    self.frame_last=now
                    self.show_frame();self._update_timeline_playhead();self._update_clock()
                if now-self.preview_stats_from >= 1.0:
                    self.preview_actual=self.preview_count/max(.001,now-self.preview_stats_from)
                    self.preview_count=0;self.preview_stats_from=now
                    self.preview_fps_stats.configure(text=f'target {self.preview_target} | actual {self.preview_actual:.0f}')
            # Tk tick cadence targets the selected rate; actual measured FPS is
            # displayed honestly and may be lower due to decoding/screen refresh.
            self.after(max(8, int(1000/max(1,self.preview_target))),self._tick)
        except tk.TclError:
            pass

    def save_project(self,as_new=False):
        path=self.project_file
        if as_new or not path:
            path=filedialog.asksaveasfilename(title='Save Cazy Studio project',defaultextension='.cazy.json',
                         filetypes=[('Cazy Studio Project','*.cazy.json'),('JSON Files','*.json')])
        if not path:return
        try:
            Path(path).write_text(self.project.to_json(),encoding='utf-8')
            self.project_file=path
            self.status_var.set('Project saved: '+Path(path).name)
        except OSError as exc:messagebox.showerror('Save project',str(exc))

    def open_demo(self):
        demo=Path(__file__).resolve().parent/'Demo'/'Demo_Project.cazy.json'
        if demo.is_file():
            self._load_project(str(demo))
        else:
            messagebox.showerror('Demo missing','Demo assets were not included in this installation.')

    def open_project(self):
        path=filedialog.askopenfilename(title='Open Cazy Studio project',filetypes=[('Cazy Studio Project','*.cazy.json *.json')])
        if path:self._load_project(path)

    def _load_project(self,path):
        try:
            project=engine.Project.from_json(Path(path).read_text(encoding='utf-8'))
            # Relative asset paths make the bundled demo portable after extraction.
            base=Path(path).resolve().parent
            for clip in project.clips:
                if not os.path.isabs(clip.path): clip.path=str((base/clip.path).resolve())
            if project.music_path and not os.path.isabs(project.music_path):
                project.music_path=str((base/project.music_path).resolve())
            self.project=project
            self.project_file=path
            self.selected=0 if project.clips else -1
            self.playhead=0
            self.media_paths=[];self.media_list.delete(0,'end')
            for c in project.clips:
                if c.path not in self.media_paths:
                    self.media_paths.append(c.path)
                    self.media_list.insert('end',f'▣  {Path(c.path).name[:32]}')
            if project.music_path and project.music_path not in self.media_paths:
                self.media_paths.append(project.music_path)
                self.media_list.insert('end',f'♫  {Path(project.music_path).name[:32]}')
            self.undo_stack.clear();self.redo_stack.clear()
            self._close_cached_capture()
            self._refresh_all()
            missing=[p for p in self.media_paths if not os.path.isfile(p)]
            if missing:
                messagebox.showwarning('Missing media','Some files moved since the project was saved:\n'+'\n'.join(missing[:5]))
            self.status_var.set('Opened '+Path(path).name)
        except Exception as exc:messagebox.showerror('Open project',str(exc))

    def export_dialog(self):
        if not self.project.clips:
            messagebox.showinfo('Export MP4','Add video or image clips to the timeline first.')
            return
        if self.exporting:return
        dialog=tk.Toplevel(self)
        dialog.title('Export Cazy Studio Video')
        dialog.configure(bg=CARD)
        dialog.geometry('420x380')
        dialog.resizable(False,False)
        dialog.transient(self)
        dialog.grab_set()
        self._label(dialog,'EXPORT YOUR EDIT',15,ACCENT2,True).pack(anchor='w',padx=20,pady=(20,14))
        self._label(dialog,'Resolution',10,MUTED).pack(anchor='w',padx=20)
        resolution=ttk.Combobox(dialog,state='readonly',values=['1280 × 720','1920 × 1080','2560 × 1440','3840 × 2160','1080 × 1920','1440 × 2560','2160 × 3840'],textvariable=self.res_var,style='Field.TCombobox')
        resolution.pack(fill='x',padx=20,pady=(3,11))
        self._label(dialog,'Frame rate',10,MUTED).pack(anchor='w',padx=20)
        ttk.Combobox(dialog,state='readonly',values=['24','30','60','120','240'],textvariable=self.fps_var,style='Field.TCombobox').pack(fill='x',padx=20,pady=(3,11))
        self._label(dialog,'Quality',10,MUTED).pack(anchor='w',padx=20)
        ttk.Combobox(dialog,state='readonly',values=['High','Balanced','Smaller File'],textvariable=self.quality_var,style='Field.TCombobox').pack(fill='x',padx=20,pady=(3,14))
        self._label(dialog,f'Estimated output duration: {engine.fmt_time(self.project.duration)}',9,MUTED).pack(anchor='w',padx=20,pady=(0,11))
        def go():
            dest=filedialog.asksaveasfilename(parent=dialog,title='Export to MP4',defaultextension='.mp4',
                initialfile=Path(self.project_file).stem.replace('.cazy','')+'.mp4' if self.project_file else 'Cazy_Studio_Edit.mp4',
                filetypes=[('MP4 video','*.mp4')])
            if not dest:return
            w,h=map(int,self.res_var.get().replace(' ','').split('×'))
            self.project.width,self.project.height=w,h
            self.project.fps=int(self.fps_var.get())
            self.project.crf={'High':19,'Balanced':23,'Smaller File':27}[self.quality_var.get()]
            dialog.destroy()
            self._start_export(dest)
        self._button(dialog,'EXPORT MP4',go,True).pack(fill='x',padx=20,pady=5)

    def _start_export(self,path):
        if self.exporting:return
        self.exporting=True
        self.export_cancel.clear()
        self.playing=False
        self.play_btn.configure(text='▶  Play')
        self.export_window=tk.Toplevel(self)
        self.export_window.title('Rendering Cazy Studio Project')
        self.export_window.geometry('510x180')
        self.export_window.configure(bg=CARD)
        self.export_window.transient(self)
        self.export_window.protocol('WM_DELETE_WINDOW',self.cancel_export)
        self._label(self.export_window,'RENDER IN PROGRESS',13,ACCENT2,True).pack(anchor='w',padx=20,pady=(19,10))
        self.export_message=self._label(self.export_window,'Preparing...',10,TEXT)
        self.export_message.pack(anchor='w',padx=20)
        self.export_bar=ttk.Progressbar(self.export_window,orient='horizontal',maximum=100,style='Cazy.Horizontal.TProgressbar')
        self.export_bar.pack(fill='x',padx=20,pady=(16,8))
        self._button(self.export_window,'Cancel',self.cancel_export,small=True).pack(anchor='e',padx=20)
        render_project=copy.deepcopy(self.project)
        def worker():
            try:
                result=engine.render(render_project,path,lambda msg,cur,total:self.jobs.put(('progress',msg,cur/max(1,total)*100)),self.export_cancel)
                self.jobs.put(('done',result))
            except Exception as exc:self.jobs.put(('error',str(exc)))
        threading.Thread(target=worker,daemon=True).start()

    def cancel_export(self):
        if not self.exporting:return
        self.export_cancel.set()
        self.status_var.set('Cancelling export...')

    def _poll_jobs(self):
        try:
            while True:
                msg=self.jobs.get_nowait()
                if msg[0]=='progress':
                    _,desc,pct=msg
                    if self.exporting:
                        self.export_message.config(text=desc[:65]);self.export_bar['value']=pct
                        self.status_var.set(desc)
                elif msg[0] in ('done','error'):
                    self.exporting=False
                    if self.export_window.winfo_exists():self.export_window.destroy()
                    if msg[0]=='done':
                        path=msg[1]
                        self.status_var.set('Exported MP4: '+path)
                        if messagebox.askyesno('Export complete','Video exported successfully!\n\n'+path+'\n\nOpen the exported video?'):
                            if os.name=='nt':os.startfile(path)
                            else:subprocess.Popen(['xdg-open',path])
                    elif 'cancelled' in msg[1].lower():
                        self.status_var.set('Export cancelled.')
                    else:
                        messagebox.showerror('Export failed',msg[1][-1700:])
                        self.status_var.set('Export failed. Check error details.')
        except queue.Empty:
            pass
        self.after(110,self._poll_jobs)

    def on_close(self):
        if self.exporting:
            messagebox.showinfo('Export in progress','Cancel the export before exiting.')
            return
        if self._pending_resize is not None:
            self.after_cancel(self._pending_resize)
        self.autodirector_view.save()
        self.preview_decoder.close()
        self.destroy()

# ---- Cazy AutoDirector explicit video library (v0.3.7) ----
# This lives in app.py to keep the existing v0.3.5 Update Center's safe-file
# whitelist compatible: no new install-time Python module is required.
_AUTODIRECTOR_MODES = ('Best Successful Attempts', 'Full Run')
_AUTODIRECTOR_VIDEO_EXTS = {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v'}
_AUTODIRECTOR_MUSIC_EXTS = {'.mp3', '.wav', '.m4a', '.flac', '.ogg', '.aac'}
_AUTODIRECTOR_MAX_JOBS = 500


def _autodirector_path():
    local = os.environ.get('LOCALAPPDATA')
    return (Path(local) / 'CazyStudio' if local else Path.home() / '.cazy_studio') / 'autodirector_queue.json'


def _autodirector_defaults():
    return {
        'schema': 1, 'queue': [], 'music_folder': '', 'random_music': True,
        'avoid_repeats': True, 'mode': _AUTODIRECTOR_MODES[0],
        'visibility': 'Private', 'render_preset': '', 'recent_music': '', 'success_reference': '',
        'references': {}, 'ocr_path': '',
    }


def _autodirector_validate(state):
    if not isinstance(state, dict) or state.get('schema') != 1:
        raise ValueError('Unrecognized AutoDirector queue file format')
    jobs = state.get('queue')
    if not isinstance(jobs, list) or len(jobs) > _AUTODIRECTOR_MAX_JOBS:
        raise ValueError('AutoDirector queue is invalid or too large')
    if state.get('mode') not in _AUTODIRECTOR_MODES:
        raise ValueError('Unknown AutoDirector editing mode')
    if state.get('visibility') not in ('Private','Unlisted','Public'):
        raise ValueError('Unknown YouTube visibility setting')
    if not isinstance(state.get('music_folder'), str) or len(state['music_folder']) > 4096:
        raise ValueError('Invalid music folder')
    if not isinstance(state.get('render_preset'), str) or len(state['render_preset']) > 100:
        raise ValueError('Invalid render preset')
    if not isinstance(state.get('success_reference', ''), str) or len(state.get('success_reference', '')) > 4096:
        raise ValueError('Invalid completion screenshot path')
    if not isinstance(state.get('ocr_path',''),str) or len(state.get('ocr_path',''))>4096:
        raise ValueError('Invalid OCR executable path')
    autodirector_fullrun.normalize_references(state.get('references',{}),
                                                 state.get('success_reference',''))
    if not isinstance(state.get('recent_music'), str) or len(state['recent_music']) > 4096:
        raise ValueError('Invalid previously selected song')
    if not isinstance(state.get('random_music'), bool) or not isinstance(state.get('avoid_repeats'), bool):
        raise ValueError('Invalid music playback options')
    seen=set()
    for job in jobs:
        if not isinstance(job, dict) or not isinstance(job.get('path'), str) or len(job['path']) > 4096:
            raise ValueError('Invalid AutoDirector video entry')
        if not isinstance(job.get('music',''), str) or len(job['music']) > 4096:
            raise ValueError('Invalid AutoDirector music entry')
        if job.get('status') not in ('Queued','Prepared'):
            raise ValueError('Unknown AutoDirector queue status')
        key=os.path.normcase(os.path.abspath(job['path']))
        if key in seen:raise ValueError('Duplicate AutoDirector video in queue')
        seen.add(key)
    return state


def _autodirector_load(path=None):
    path=Path(path) if path is not None else _autodirector_path()
    if not path.is_file():return _autodirector_defaults()
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError) as exc:
        raise ValueError(f'Cannot read AutoDirector queue: {exc}') from exc
    return _autodirector_validate(data)


def _autodirector_save(state,path=None):
    state=_autodirector_validate(state)
    path=Path(path) if path is not None else _autodirector_path()
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='autodirector_',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as out:
            json.dump(state,out,indent=2,ensure_ascii=False)
            out.flush();os.fsync(out.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)
    return path


def _autodirector_add(state,files):
    seen={os.path.normcase(os.path.abspath(job['path'])) for job in state['queue']}
    new=0
    for item in files:
        p=Path(item)
        key=os.path.normcase(os.path.abspath(p))
        if p.suffix.lower() not in _AUTODIRECTOR_VIDEO_EXTS or not p.is_file() or key in seen:
            continue
        if len(state['queue']) >= _AUTODIRECTOR_MAX_JOBS:break
        state['queue'].append({'path':str(p.resolve()),'status':'Queued','music':''})
        seen.add(key);new+=1
    return new


def _autodirector_tracks(folder):
    if not folder or not Path(folder).is_dir():return []
    try:
        return sorted((str(p) for p in Path(folder).iterdir()
                       if p.is_file() and p.suffix.lower() in _AUTODIRECTOR_MUSIC_EXTS),key=str.casefold)
    except OSError:
        return []


def _autodirector_prepare(state,chooser=None):
    """Assign songs to queued jobs; never edits/render/uploads footage."""
    chooser=chooser or random.choice
    tracks=_autodirector_tracks(state['music_folder']) if state['random_music'] else []
    last=state.get('recent_music','')
    count=0
    for job in state['queue']:
        if state['random_music'] and tracks:
            picks=[song for song in tracks if song!=last] if state['avoid_repeats'] and len(tracks)>1 else tracks
            job['music']=chooser(picks)
            last=job['music']
        else:job['music']=''
        job['status']='Prepared'
        count+=1
    state['recent_music']=last
    return count


class AutoDirectorView(tk.Frame):
    """Simple, explicit local video selection for the AutoDirector pipeline.

    Only user-selected files enter this library. The original files are never
    copied, moved, edited or deleted by library operations. This stage plans
    the queue and music; it does not cut footage, render or publish videos.
    """
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        try:
            self.state = _autodirector_load()
        except ValueError as exc:
            self.state = _autodirector_defaults()
            messagebox.showwarning('AutoDirector library', str(exc), parent=self.winfo_toplevel())
        self.mode_var = tk.StringVar(value=self.state['mode'])
        self.music_var = tk.StringVar(value=self.state['music_folder'])
        self.random_var = tk.BooleanVar(value=self.state['random_music'])
        self.avoid_var = tk.BooleanVar(value=self.state['avoid_repeats'])
        self.preset_var = tk.StringVar(value=self.state.get('render_preset', ''))
        self.ocr_var = tk.StringVar(value=self.state.get('ocr_path',''))
        self._full_run_result = None
        self._auto_busy = False
        self._auto_messages = queue.Queue()
        self._auto_cancel = threading.Event()
        self._review_window = None
        self.status_var = tk.StringVar(value='Ready. Choose the exact recordings you want AutoDirector to use.')
        self.scan_progress_var = tk.DoubleVar(value=0.0)
        self.scan_state_var = tk.StringVar(value='SCAN STATUS: Ready')
        self.scan_detail_var = tk.StringVar(value='When scanning, live progress, current video time and triggers appear here.')
        self._scan_started = None
        self._scan_last_update = None
        self._scan_last_fraction = 0.0
        self.detail_var = tk.StringVar(value='Select a recording to see its location and preview it.')
        self._make_view()
        self.refresh()
        self.after(120, self._poll_auto_messages)

    def _label(self, parent, text, size=10, color=TEXT, bold=False):
        return tk.Label(parent, text=text, bg=parent.cget('bg'), fg=color, anchor='w',
                        font=('Segoe UI', size, 'bold' if bold else 'normal'))

    def _button(self, parent, text, callback, primary=False):
        return tk.Button(parent, text=text, command=callback,
                         bg=ACCENT if primary else CARD2,
                         fg='#0d0f18' if primary else TEXT,
                         activebackground='#b99fff' if primary else BORDER,
                         activeforeground='#0d0f18' if primary else TEXT,
                         relief='flat', borderwidth=0, padx=11, pady=8,
                         cursor='hand2', font=('Segoe UI', 9, 'bold'))

    def _check(self, parent, text, variable):
        return tk.Checkbutton(parent, text=text, variable=variable, command=self.save,
                              bg=CARD, fg=TEXT, activebackground=CARD,
                              activeforeground=TEXT, selectcolor=CARD2,
                              highlightthickness=0, anchor='w', font=('Segoe UI', 9))

    def _make_view(self):
        top = tk.Frame(self, bg=BG)
        top.pack(fill='x', padx=18, pady=(15, 8))
        self._label(top, 'CAZY AUTODIRECTOR', 19, ACCENT, True).pack(side='left')
        self._label(top, 'Select videos  →  Scan gameplay  →  Review cuts', 10, MUTED).pack(side='left', padx=18)
        self._button(top, 'Save Library', self.save).pack(side='right')

        note = tk.Frame(self, bg='#172337', highlightbackground=BORDER, highlightthickness=1)
        note.pack(fill='x', padx=18, pady=(0, 10))
        tk.Label(note, bg='#172337', fg=ACCENT2, anchor='w',
                 text='LOCAL WORKSPACE  •  ONLY videos YOU add are included. Only selected videos are scanned. '
                      'Your original recordings stay untouched. No YouTube connection or publishing.',
                 font=('Segoe UI', 9, 'bold'), wraplength=1250, justify='left').pack(fill='x', padx=12, pady=9)

        pane = tk.PanedWindow(self, orient='horizontal', bg=BG,
                             sashwidth=8, sashrelief='flat', bd=0)
        pane.pack(fill='both', expand=True, padx=18, pady=(0, 10))
        library = tk.Frame(pane, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        options = tk.Frame(pane, bg=CARD, highlightbackground=BORDER,
                           highlightthickness=1, width=365)
        pane.add(library, minsize=475, stretch='always')
        pane.add(options, minsize=340, stretch='never')

        self._label(library, '1  YOUR SELECTED VIDEOS', 13, ACCENT2, True).pack(
            anchor='w', padx=15, pady=(15, 4))
        self._label(library, 'Add the exact OBS recordings you want. No folder scanning.',
                    9, MUTED).pack(anchor='w', padx=15, pady=(0, 9))
        actions = tk.Frame(library, bg=CARD)
        actions.pack(fill='x', padx=13, pady=(0, 9))
        self._button(actions, '+ ADD VIDEOS', self.add_files, True).pack(side='left', padx=(0, 6))
        self._button(actions, 'Preview', self.preview_selected).pack(side='left', padx=(0, 6))
        self._button(actions, 'Remove', self.remove).pack(side='left', padx=(0, 6))
        self._button(actions, '↑', lambda: self.move(-1)).pack(side='left', padx=(0, 4))
        self._button(actions, '↓', lambda: self.move(1)).pack(side='left')
        self._button(actions, 'Clear List', self.clear_all).pack(side='right')

        self.empty_hint = tk.Label(library,
            text='No recordings selected yet.  Click + ADD VIDEOS to choose your clips.',
            bg=CARD2, fg=MUTED, font=('Segoe UI', 10), pady=14)
        self.empty_hint.pack(fill='x', padx=12, pady=(0, 8))
        tree_shell = tk.Frame(library, bg=CARD)
        tree_shell.pack(fill='both', expand=True, padx=12, pady=(0, 8))
        self.tree = ttk.Treeview(tree_shell,
                                 columns=('number', 'file', 'state', 'music'),
                                 show='headings', selectmode='browse')
        self.tree.heading('number', text='#')
        self.tree.heading('file', text='SELECTED RECORDING')
        self.tree.heading('state', text='READY?')
        self.tree.heading('music', text='MUSIC')
        self.tree.column('number', width=35, anchor='center', stretch=False)
        self.tree.column('file', width=275, anchor='w')
        self.tree.column('state', width=105, anchor='w', stretch=False)
        self.tree.column('music', width=155, anchor='w')
        self.tree.tag_configure('missing', foreground=RED)
        self.tree.pack(side='left', fill='both', expand=True)
        scrollbar = ttk.Scrollbar(tree_shell, orient='vertical', command=self.tree.yview)
        scrollbar.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind('<Delete>', lambda event: self.remove())
        self.tree.bind('<Double-1>', lambda event: self.preview_selected())
        self.tree.bind('<<TreeviewSelect>>', lambda event: self._update_details())

        info = tk.Frame(library, bg=CARD)
        info.pack(fill='x', padx=14, pady=(0, 7))
        self.count_label = self._label(info, '0 videos selected', 10, ACCENT2, True)
        self.count_label.pack(side='left')
        self._label(info, 'Double-click to preview', 9, MUTED).pack(side='right')
        tk.Label(library, textvariable=self.detail_var, bg=CARD,
                 fg=MUTED, anchor='w', font=('Segoe UI', 9),
                 wraplength=650, justify='left').pack(fill='x', padx=14, pady=(0, 9))

        # Keep the actual scan progress directly ABOVE the scan button,
        # not inside the app-level footer which is often off-screen.
        progress_panel = tk.Frame(library, bg=CARD2, highlightbackground=BORDER,
                                  highlightthickness=1)
        progress_panel.pack(fill='x', padx=14, pady=(0, 8))
        tk.Label(progress_panel, textvariable=self.scan_state_var, bg=CARD2,
                 fg=ACCENT2, font=('Segoe UI', 10, 'bold'),
                 anchor='w').pack(fill='x', padx=10, pady=(7, 3))
        self.scan_bar = ttk.Progressbar(progress_panel, orient='horizontal',
                                        mode='determinate', maximum=100,
                                        variable=self.scan_progress_var)
        self.scan_bar.pack(fill='x', padx=10, pady=(0, 4), ipady=2)
        tk.Label(progress_panel, textvariable=self.scan_detail_var, bg=CARD2,
                 fg=TEXT, font=('Segoe UI', 9), anchor='w',
                 justify='left', wraplength=850).pack(fill='x', padx=10,
                                                      pady=(0, 8))

        bottom = tk.Frame(library, bg=CARD)
        bottom.pack(fill='x', padx=14, pady=(0, 14))
        self._label(bottom, 'OCR triggers + 2-second pre-roll for each half',
                    9, MUTED).pack(side='left', pady=8)
        self._button(bottom, 'SCAN / REVIEW RUN', self.scan_selected, True).pack(side='right')
        self._button(bottom, 'Assign Music', self.prepare).pack(side='right', padx=(0, 7))
        self._button(bottom, 'Cancel Job', self.cancel_full_run_job).pack(side='right', padx=(0, 7))

        self._label(options, '2  EDITING PLAN', 13, ACCENT2, True).pack(
            anchor='w', padx=15, pady=(16, 5))
        self._label(options, 'What should AutoDirector keep?', 9, MUTED).pack(
            anchor='w', padx=15, pady=(0, 4))
        self._combo(options, self.mode_var, _AUTODIRECTOR_MODES).pack(
            fill='x', padx=15, pady=(0, 7))
        self._label(options, 'Full Run joins ALL successful halves into ONE MP4.', 9, ACCENT2).pack(anchor='w', padx=15, pady=(0, 7))
        self._label(options, 'Text OCR detection — review your cuts before rendering.',
                    9, YELLOW).pack(anchor='w', padx=15, pady=(0, 12))

        self._label(options, 'MUSIC', 11, ACCENT2, True).pack(
            anchor='w', padx=15, pady=(0, 5))
        folder = tk.Frame(options, bg=CARD)
        folder.pack(fill='x', padx=15)
        tk.Entry(folder, textvariable=self.music_var, bg=CARD2, fg=TEXT,
                 relief='flat', insertbackground=TEXT, state='readonly',
                 readonlybackground=CARD2).pack(side='left', fill='x',
                                                expand=True, padx=(0, 6), ipady=6)
        self._button(folder, 'Choose Folder', self.choose_music).pack(side='right')
        self._check(options, 'Random song for each video', self.random_var).pack(
            anchor='w', padx=15, pady=(8, 0))
        self._check(options, 'No back-to-back repeats', self.avoid_var).pack(
            anchor='w', padx=15)
        self.music_count = self._label(options, 'No music selected', 9, MUTED)
        self.music_count.pack(anchor='w', padx=15, pady=(3, 11))

        self._label(options, 'RENDER PRESET', 11, ACCENT2, True).pack(
            anchor='w', padx=15, pady=(0, 4))
        self._label(options, 'Use your existing Quality Lab preset', 9, MUTED).pack(
            anchor='w', padx=15)
        self.preset_combo = self._combo(options, self.preset_var,
                                       ('Use last-used settings',))
        self.preset_combo.pack(fill='x', padx=15, pady=(4, 4))
        self.preset_combo.bind('<<ComboboxSelected>>', self.on_saved_preset_change)
        self._button(options, 'Reload Saved Presets', self.reload_presets).pack(anchor='w', padx=15, pady=(0, 4))
        self._button(options, 'View / Edit Saved Recipe in Quality Lab',
                     self.open_selected_preset_in_quality_lab).pack(anchor='w', padx=15, pady=(0, 4))
        self.preset_hint = self._label(options, 'Loading named presets...', 9, MUTED)
        self.preset_hint.pack(anchor='w', padx=15, pady=(0, 4))
        self.recipe_details_var = tk.StringVar(value='Choose a saved recipe to preview its settings.')
        tk.Label(options, textvariable=self.recipe_details_var, bg=CARD2, fg=TEXT,
                 font=('Segoe UI', 9), anchor='w', justify='left',
                 wraplength=310, padx=10, pady=8).pack(fill='x', padx=15, pady=(0, 10))
        self._label(options, 'ABYSS TEXT DETECTION', 11, ACCENT2, True).pack(anchor='w', padx=15, pady=(0, 5))
        self._label(options, 'Reads game text locally; screenshots are no longer needed.',
                    9, MUTED).pack(anchor='w', padx=15, pady=(0, 5))
        controls=tk.Frame(options,bg=CARD)
        controls.pack(fill='x',padx=15)
        self._button(controls, 'Locate Tesseract OCR', self.choose_ocr_engine).pack(side='left')
        self._button(controls, 'Install Help', self.ocr_install_help).pack(side='left',padx=6)
        self.ocr_hint = self._label(options, 'Checking local text recognition...', 9, MUTED)
        self.ocr_hint.pack(anchor='w', padx=15, pady=(4, 8))

        review = tk.Frame(options, bg=CARD2)
        review.pack(fill='x', padx=14, pady=(1, 12))
        tk.Label(review, text='3  REVIEW CUT + EXPORT LOCALLY', bg=CARD2,
                 fg=ACCENT2, anchor='w', font=('Segoe UI', 10, 'bold')).pack(
                     fill='x', padx=10, pady=(10, 5))
        tk.Label(review, text='Final video  •  Thumbnail  •  Title',
                 bg=CARD2, fg=TEXT, anchor='w',
                 font=('Segoe UI', 9)).pack(fill='x', padx=10, pady=(0, 5))
        tk.Label(review, text='Edit start/end timestamps first. YouTube is disabled.',
                 bg=CARD2, fg=MUTED, anchor='w', justify='left', wraplength=290,
                 font=('Segoe UI', 9)).pack(fill='x', padx=10, pady=(0, 10))

        footer = tk.Frame(self, bg=BG)
        footer.pack(fill='x', padx=18, pady=(0, 12))
        self._label(footer, 'FULL RUN: scan selected recordings → review chamber halves → ONE finished MP4. No uploads.',
                    9, ACCENT2, True).pack(anchor='w')
        tk.Label(footer, textvariable=self.status_var, bg=BG, fg=MUTED,
                 anchor='w', font=('Segoe UI', 9), wraplength=1150,
                 justify='left').pack(fill='x', pady=(4, 0))

    def _combo(self, parent, var, values):
        widget = ttk.Combobox(parent, textvariable=var, state='readonly',
                              values=list(values), style='Field.TCombobox')
        widget.bind('<<ComboboxSelected>>', lambda event: self.save())
        return widget

    def refresh(self):
        selected = self.tree.selection()
        index = int(selected[0]) if selected else -1
        for row in self.tree.get_children():
            self.tree.delete(row)
        missing = 0
        for i, job in enumerate(self.state['queue']):
            exists = Path(job['path']).is_file()
            if not exists:
                missing += 1
            label = job['status'] if exists else 'MISSING FILE'
            self.tree.insert('', tk.END, iid=str(i),
                             values=(i + 1, Path(job['path']).name, label,
                                     Path(job['music']).name if job['music'] else '—'),
                             tags=() if exists else ('missing',))
        if 0 <= index < len(self.state['queue']):
            self.tree.selection_set(str(index))
        n = len(self.state['queue'])
        info = f'{n} video' + ('s' if n != 1 else '') + ' selected'
        if missing:
            info += f'  •  {missing} missing'
        self.count_label.configure(text=info, fg=RED if missing else ACCENT2)
        if n:
            self.empty_hint.pack_forget()
        elif not self.empty_hint.winfo_manager():
            self.empty_hint.pack(fill='x', padx=12, pady=(0, 8),
                                 before=self.tree.master)
        self.music_count.configure(
            text=f'{len(_autodirector_tracks(self.music_var.get()))} available songs')
        try:
            presets = quality_engine.load_preferences().get('presets', {})
            names = ['Use last-used settings'] + sorted(presets)
        except (OSError, ValueError):
            names = ['Use last-used settings']
        self.preset_combo.configure(values=names)
        if self.preset_var.get() not in names:
            self.preset_var.set(names[0])
        self.preset_hint.configure(text=f'{len(names)-1} saved Quality Lab presets')
        self.update_saved_recipe_details()
        executable=quality_engine._cazy_tesseract(self.ocr_var.get())
        self.ocr_hint.configure(
            text='OCR ready: '+Path(executable).name if executable
            else 'OCR not installed: locate tesseract.exe')
        self._update_details()

    def _selected(self):
        selected = self.tree.selection()
        return int(selected[0]) if selected else -1

    def _update_details(self):
        i = self._selected()
        if i < 0:
            self.detail_var.set('Select a recording to see its location and preview it.')
            return
        path = Path(self.state['queue'][i]['path'])
        if path.is_file():
            try:
                size = f'{path.stat().st_size / (1024 ** 2):,.1f} MB'
            except OSError:
                size = 'Size unavailable'
            self.detail_var.set(f'{size}   •   {path}')
        else:
            self.detail_var.set(f'Missing file: {path}. Remove it and add its new location.')

    def save(self):
        self.state['music_folder'] = self.music_var.get()
        self.state['random_music'] = self.random_var.get()
        self.state['avoid_repeats'] = self.avoid_var.get()
        self.state['mode'] = self.mode_var.get()
        self.state['render_preset'] = self.preset_var.get()
        self.state['ocr_path'] = self.ocr_var.get()
        # Legacy visibility setting remains in existing saved queues for
        # compatibility, but there is no publishing UI or publishing action.
        try:
            _autodirector_save(self.state)
        except (ValueError, OSError) as exc:
            messagebox.showerror('Save video library', str(exc), parent=self)
            return False
        return True

    def add_files(self):
        files = filedialog.askopenfilenames(
            parent=self, title='Choose ONLY the recordings to process',
            filetypes=[('Video recordings', '*.mp4 *.mkv *.mov *.avi *.webm *.m4v'),
                       ('All files', '*.*')])
        if not files:
            return
        added = _autodirector_add(self.state, files)
        if added:
            self.save()
            self.refresh()
            self.tree.selection_set(str(len(self.state['queue']) - 1))
            self.tree.see(str(len(self.state['queue']) - 1))
            self._update_details()
            self.status_var.set(f'Added {added} recording(s). Only these selected files are in your library.')
        else:
            self.status_var.set('No new videos added. Files may already be selected or unsupported.')

    def preview_selected(self):
        i = self._selected()
        if i < 0:
            messagebox.showinfo('Preview video', 'Select a recording from the list first.', parent=self)
            return
        path = self.state['queue'][i]['path']
        if not Path(path).is_file():
            messagebox.showerror('Video not found', 'The selected video was moved or deleted. '
                                 'Remove it and add its new location.', parent=self)
            return
        try:
            if os.name == 'nt':
                os.startfile(path)
            else:
                import sys
                subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', path])
        except (OSError, AttributeError) as exc:
            messagebox.showerror('Could not preview video', str(exc), parent=self)

    def clear_all(self):
        if not self.state['queue']:
            return
        if not messagebox.askyesno('Clear selected videos',
                                   'Remove every recording from the AutoDirector list?\n\n'
                                   'Your original video files will NOT be deleted.',
                                   parent=self):
            return
        n = len(self.state['queue'])
        self.state['queue'].clear()
        self.save()
        self.refresh()
        self.status_var.set(f'Removed {n} recordings from the list. Original videos are untouched.')

    def remove(self):
        i = self._selected()
        if i < 0:
            return
        name = Path(self.state['queue'][i]['path']).name
        self.state['queue'].pop(i)
        self.save()
        self.refresh()
        if self.state['queue']:
            self.tree.selection_set(str(min(i, len(self.state['queue']) - 1)))
        self._update_details()
        self.status_var.set(f'Removed {name} from AutoDirector only. Original file untouched.')

    def move(self, delta):
        i = self._selected()
        j = i + delta
        if i < 0 or j < 0 or j >= len(self.state['queue']):
            return
        q = self.state['queue']
        q[i], q[j] = q[j], q[i]
        self.save()
        self.refresh()
        self.tree.selection_set(str(j))
        self.tree.see(str(j))
        self._update_details()

    def choose_music(self):
        folder = filedialog.askdirectory(parent=self, title='Choose your music collection')
        if not folder:
            return
        self.music_var.set(folder)
        self.save()
        self.refresh()
        self.status_var.set(
            f'Music collection selected: {len(_autodirector_tracks(folder))} compatible songs.')

    def prepare(self):
        if not self.state['queue']:
            messagebox.showinfo('No selected videos',
                                'Click + ADD VIDEOS and choose at least one recording.',
                                parent=self)
            return
        missing = [Path(job['path']).name for job in self.state['queue']
                   if not Path(job['path']).is_file()]
        if missing:
            messagebox.showerror('Missing recordings',
                                 f'{len(missing)} selected video(s) could not be found.\n\n'
                                 'Remove missing entries and re-add them at their current location.',
                                 parent=self)
            return
        if self.random_var.get() and not _autodirector_tracks(self.music_var.get()):
            if not messagebox.askyesno('No music tracks',
                                       'No compatible music files were found. '
                                       'Prepare the selected videos without music?', parent=self):
                return
        if not self.save():
            return
        count = _autodirector_prepare(self.state)
        self.save()
        self.refresh()
        self.status_var.set(
            f'{count} selected video(s) prepared with local music assignments. '
            'Choose a selected video and click SCAN SELECTED VIDEO to test detection.')


    def on_saved_preset_change(self, event=None):
        self.save()
        self.update_saved_recipe_details()

    def update_saved_recipe_details(self):
        try:
            selected = self.preset_var.get()
            recipe = quality_engine.resolve_saved_render_preset(selected)
            self.recipe_details_var.set(
                f'{selected or "Use last-used settings"}\n'
                f'{recipe.resolution}  |  '
                f'{recipe.fps if recipe.fps else "Source"} FPS\n'
                f'{recipe.upscale}  |  {recipe.frame_method}\n'
                f'{recipe.encoder}  |  {recipe.quality}')
        except (OSError, ValueError) as exc:
            self.recipe_details_var.set('Cannot read saved preset: ' + str(exc))

    def open_selected_preset_in_quality_lab(self):
        name = self.preset_var.get()
        i = self._selected()
        source_path = self.state['queue'][i]['path'] if i >= 0 else None
        self.winfo_toplevel().open_quality_lab(selected_preset=name,source_path=source_path)

    def reload_presets(self):
        previous = self.preset_var.get()
        self.refresh()
        if previous != self.preset_var.get():
            self.save()
        self.status_var.set(f'Quality Lab presets loaded: {len(self.preset_combo["values"])-1}. '
                            'Choose your saved recipe in the Render Preset dropdown.')

    def choose_success_reference(self):
        sample = filedialog.askopenfilename(
            parent=self, title='Select a screenshot showing the Genshin completion screen',
            filetypes=[('Images', '*.png *.jpg *.jpeg *.bmp')])
        if sample:
            self.state['success_reference'] = str(Path(sample).resolve())
            self.save()
            self.refresh()
            self.status_var.set('Example success screenshot selected for future scans.')

    def scan_selected(self):
        if self.mode_var.get() == 'Full Run':
            self.scan_entire_full_run()
            return
        messagebox.showinfo(
            'Text detection',
            'Text-based Abyss detection currently runs in Full Run mode.\n'
            'Select Full Run to cut both halves and join every completed chamber.',
            parent=self)

    def _poll_auto_messages(self):
        try:
            while True:
                event, data = self._auto_messages.get_nowait()
                if event == 'progress':
                    fraction=max(0.,min(1.,float(data)))
                    self.scan_progress_var.set(fraction*100.)
                    if self._auto_busy:
                        self.scan_state_var.set(f'SCANNING: {fraction*100.:.1f}%')
                    self.status_var.set(f'Local fight scan: {fraction*100:.1f}%')
                elif event == 'scan_detail':
                    fraction=max(0.,min(1.,float(data.get('fraction',0.))))
                    elapsed=max(.001,time.monotonic()-(self._scan_started or time.monotonic()))
                    eta=elapsed*(1.-fraction)/fraction if fraction>.015 else None
                    remaining=(f'{int(eta//60)}m {int(eta%60):02d}s remaining'
                               if eta is not None else 'Calculating ETA…')
                    self.scan_progress_var.set(fraction*100.)
                    self.scan_state_var.set(f'SCANNING — {fraction*100.:.1f}%  |  {remaining}')
                    self.scan_detail_var.set(
                        f'Video {data["video_index"]}/{data["total_videos"]}: '
                        f'{data["filename"]}   •   '
                        f'{data["at"]:.1f}s / {data["duration"]:.1f}s   •   '
                        f'Events found: {data["found"]}')
                    self.status_var.set(self.scan_state_var.get())
                elif event == 'scan_done':
                    self._auto_busy = False
                    self.status_var.set(f'Found {len(data["candidates"])} possible result screens. Review suggested times.')
                    self.review_results(data)
                elif event == 'fullrun_done':
                    self._auto_busy = False
                    self.scan_progress_var.set(100.)
                    self.scan_state_var.set('SCAN COMPLETE — 100%')
                    counts=data.get('event_counts',{})
                    self.scan_detail_var.set(
                        f'Actual OCR events: Start {counts.get("start",0)}  •  '
                        f'Other Side {counts.get("switch",0)}  •  '
                        f'Completed {counts.get("complete",0)}  •  '
                        f'Failed {counts.get("failure",0)}')
                    self._full_run_result = data
                    self.status_var.set(
                        f'Found {data["completed_chambers"]} completed chambers / '
                        f'{len(data["segments"])} halves. Review before ONE-video export.')
                    self.review_full_run(data)
                elif event == 'render_progress':
                    pct, step = data
                    self.status_var.set(f'Local rendering: {pct:.0f}% — {step}')
                elif event == 'render_done':
                    self._auto_busy = False
                    self.status_var.set('Finished: ' + data['video'])
                    messagebox.showinfo('AutoDirector finished',
                        'Your local package is ready:\n\n' + '\n'.join(data.values()) +
                        '\n\nNothing was uploaded.', parent=self)
                elif event == 'error':
                    self._auto_busy = False
                    self.scan_state_var.set('SCAN / RENDER STOPPED')
                    self.scan_detail_var.set(str(data))
                    self.status_var.set('AutoDirector: ' + str(data))
                    messagebox.showerror('AutoDirector', str(data), parent=self)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(150, self._poll_auto_messages)

    def review_results(self, result):
        if self._review_window is not None and self._review_window.winfo_exists():
            self._review_window.destroy()
        win = tk.Toplevel(self)
        self._review_window = win
        win.title('Cazy AutoDirector — Review Detected Cuts')
        win.geometry('880x590')
        win.minsize(660, 460)
        win.configure(bg=BG)
        win.transient(self.winfo_toplevel())
        body = tk.Frame(win, bg=BG)
        body.pack(fill='both', expand=True, padx=18, pady=15)
        self._label(body, 'REVIEW DETECTED RESULTS', 17, ACCENT2, True).pack(anchor='w')
        self._label(body, Path(result['path']).name, 10, TEXT, True).pack(anchor='w', pady=(6, 1))
        self._label(body, f'Duration {result["duration"]:.1f}s  •  OCR '
                    + ('available' if result['ocr_available'] else 'not installed')
                    + '  •  Results may include failed attempts: verify manually.',
                    9, YELLOW).pack(anchor='w', pady=(1, 10))
        table = ttk.Treeview(body, show='headings',
                             columns=('when','start','end','match','why'), height=8)
        for key,label,width in [('when','SCREEN AT',90),('start','START',80),
                                 ('end','END',80),('match','CONFIDENCE',100),
                                 ('why','DETECTION EVIDENCE',390)]:
            table.heading(key, text=label)
            table.column(key, width=width, anchor='w')
        table.pack(fill='both', expand=True)
        for n, item in enumerate(result['candidates']):
            table.insert('', tk.END, iid=str(n),
                         values=(item['at'],item['start'],item['end'],
                                 f'{item["confidence"]:.0%}',item['reason']))
        if not result['candidates']:
            self._label(body, 'No candidate found. Enter the desired cut times manually '
                              'or choose a success screenshot and scan again.',
                        9, YELLOW).pack(anchor='w', pady=5)
        fields = tk.Frame(body, bg=BG)
        fields.pack(fill='x', pady=(14, 8))
        start_var = tk.StringVar(value=str(max(0, result['duration'] - 180)))
        end_var = tk.StringVar(value=str(result['duration']))
        self._label(fields,'START (seconds)',9,TEXT,True).pack(side='left')
        tk.Entry(fields, width=12, textvariable=start_var, bg=CARD2, fg=TEXT,
                 insertbackground=TEXT).pack(side='left', padx=(8,18), ipady=6)
        self._label(fields,'END (seconds)',9,TEXT,True).pack(side='left')
        tk.Entry(fields, width=12, textvariable=end_var, bg=CARD2, fg=TEXT,
                 insertbackground=TEXT).pack(side='left', padx=8, ipady=6)

        def pick(_event=None):
            selection = table.selection()
            if selection:
                item = result['candidates'][int(selection[0])]
                start_var.set(str(item['start']))
                end_var.set(str(item['end']))
        table.bind('<<TreeviewSelect>>', pick)
        if result['candidates']:
            table.selection_set('0')
            pick()
        self._label(body, 'Select a candidate, correct START/END, then render. '
                          'Preset comes from your Quality Lab saved presets.',
                    9, MUTED).pack(anchor='w', pady=(4, 11))
        actions = tk.Frame(body, bg=BG)
        actions.pack(fill='x')
        self._button(actions, 'Preview Original', lambda: self._open_auto_source(result['path'])).pack(side='left')
        self._button(actions, 'RENDER THIS CUT LOCALLY',
                     lambda: self.render_reviewed(result,start_var.get(),end_var.get(),table),
                     True).pack(side='right')
        self._button(actions,'Close',win.destroy).pack(side='right',padx=8)

    def _open_auto_source(self, path):
        try:
            if os.name == 'nt':
                os.startfile(path)
            else:
                import sys
                subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open',path])
        except (OSError,AttributeError) as exc:
            messagebox.showerror('Preview error',str(exc),parent=self)

    def render_reviewed(self, result, begin, finish, table):
        if self._auto_busy:
            messagebox.showinfo('Busy','Another AutoDirector job is running.',parent=self)
            return
        try:
            start, end = float(begin),float(finish)
            if not (0 <= start < end <= result['duration']+.15):
                raise ValueError('Times must be between zero and the recording duration.')
        except ValueError as exc:
            messagebox.showerror('Invalid cut',str(exc),parent=self)
            return
        preset = self.preset_var.get()
        try:
            prefs = quality_engine.load_preferences()
            if preset and preset != 'Use last-used settings' and preset not in prefs['presets']:
                raise ValueError('Saved preset not found. Click Reload Saved Presets first.')
        except (ValueError,OSError) as exc:
            messagebox.showerror('Render preset error',str(exc),parent=self)
            return
        folder = filedialog.askdirectory(parent=self,title='Choose a local folder for finished video + thumbnail + title')
        if not folder:
            return
        if not messagebox.askyesno('Confirm local render',
            f'Cut: {start:.1f}s → {end:.1f}s\n'
            f'Preset: {preset or "Use last-used settings"}\n'
            f'Destination: {folder}\n\n'
            'Render locally? Your original video is unchanged; nothing is uploaded.',
            parent=self):
            return
        self.save()
        source = result['path']
        assigned = next((j.get('music','') for j in self.state['queue']
                         if j['path'] == source), '')
        selection = table.selection()
        matched_time = (result['candidates'][int(selection[0])]['at']
                        if selection else None)
        self._auto_busy = True
        def background():
            try:
                output = quality_engine.cazy_render_local_package(
                    source, start, end, folder, preset,
                    music=assigned, success_at=matched_time,
                    cancel=self._auto_cancel,
                    progress=lambda pct,label: self._auto_messages.put(('render_progress',(pct,label))))
                self._auto_messages.put(('render_done',output))
            except Exception as exc:
                self._auto_messages.put(('error',str(exc)))
        threading.Thread(target=background,daemon=True).start()
        if self._review_window is not None and self._review_window.winfo_exists():
            self._review_window.destroy()

    def cancel_full_run_job(self):
        if self._auto_busy:
            self._auto_cancel.set()
            self.scan_state_var.set('CANCELLING SCAN / RENDER…')
            self.scan_detail_var.set('Stopping the current operation.')
            self.status_var.set('Cancelling AutoDirector operation…')


    def manage_references(self):
        """Manage multiple screenshots independently for each Abyss event."""
        from tkinter import simpledialog
        refs=autodirector_fullrun.normalize_references(
            self.state.get('references',{}),self.state.get('success_reference',''))
        window=tk.Toplevel(self)
        window.title('Abyss Detection — Reference Screenshots')
        window.geometry('780x550')
        window.minsize(620,430)
        window.configure(bg=BG)
        body=tk.Frame(window,bg=BG)
        body.pack(fill='both',expand=True,padx=18,pady=16)
        self._label(body,'REFERENCE SCREENSHOT LIBRARY',15,ACCENT2,True).pack(anchor='w')
        self._label(body,'Add multiple examples of each game event. None are uploaded.',
                    9,MUTED).pack(anchor='w',pady=(4,10))
        kind_var=tk.StringVar(value='start')
        choices={v:k for k,v in autodirector_fullrun.REFERENCE_KINDS.items()}
        dropdown=ttk.Combobox(body,textvariable=kind_var,
                               values=list(choices),state='readonly',
                               style='Field.TCombobox')
        dropdown.pack(fill='x',pady=(0,8))
        kind_var.set(autodirector_fullrun.REFERENCE_KINDS['start'])
        listbox=tk.Listbox(body,bg=CARD2,fg=TEXT,selectbackground=ACCENT,
                           selectforeground=BG,relief='flat',font=('Segoe UI',10))
        listbox.pack(fill='both',expand=True)
        status=self._label(body,'',9,MUTED)
        status.pack(anchor='w',pady=7)
        def current_kind():
            return choices[kind_var.get()]
        def refresh():
            listbox.delete(0,tk.END)
            for item in refs[current_kind()]:
                listbox.insert(tk.END,Path(item).name)
            status.configure(text=f'{len(refs[current_kind()])} image(s) for {kind_var.get()}')
        dropdown.bind('<<ComboboxSelected>>',lambda event:refresh())
        def add():
            images=filedialog.askopenfilenames(
                parent=window,title='Add examples for '+kind_var.get(),
                filetypes=[('Screenshots','*.png *.jpg *.jpeg *.bmp'),
                           ('All files','*.*')])
            selected=refs[current_kind()]
            seen={os.path.normcase(os.path.abspath(x)) for x in selected}
            for name in images:
                full=str(Path(name).resolve())
                if os.path.normcase(os.path.abspath(full)) not in seen:
                    if len(selected)>=autodirector_fullrun.REFERENCE_LIMIT:
                        break
                    selected.append(full)
                    seen.add(os.path.normcase(os.path.abspath(full)))
            refresh()
        def remove():
            for index in reversed(listbox.curselection()):
                refs[current_kind()].pop(index)
            refresh()
        def commit():
            self.state['references']=refs
            # Legacy single-image path must not re-add removed screenshots.
            self.state['success_reference']=''
            if self.save():
                self.refresh()
                window.destroy()
        buttons=tk.Frame(body,bg=BG)
        buttons.pack(fill='x',pady=(8,0))
        self._button(buttons,'+ Add Screenshots',add,True).pack(side='left',padx=(0,7))
        self._button(buttons,'Remove Selected',remove).pack(side='left')
        self._button(buttons,'SAVE ALL REFERENCES',commit,True).pack(side='right')
        refresh()

    def ocr_install_help(self):
        import webbrowser
        messagebox.showinfo('Set up local text detection',
            'AutoDirector reads Genshin text with the free local Tesseract OCR engine.\n\n'
            '1. Install Tesseract for Windows with English language support.\n'
            '2. Return here and click Locate Tesseract OCR.\n'
            '3. Select tesseract.exe (often inside Program Files\\Tesseract-OCR).\n\n'
            'The game footage stays on your computer.',parent=self)
        webbrowser.open('https://github.com/UB-Mannheim/tesseract/wiki')

    def choose_ocr_engine(self):
        """Select locally installed tesseract.exe; no screenshots or cloud API."""
        path=filedialog.askopenfilename(
            parent=self,title='Select local Tesseract OCR executable',
            filetypes=[('Tesseract OCR','tesseract.exe'),('Programs','*.exe'),('All files','*.*')])
        if not path:
            return
        if not Path(path).is_file():
            messagebox.showerror('OCR setup','OCR executable was not found.',parent=self)
            return
        self.ocr_var.set(str(Path(path).resolve()))
        self.save()
        self.refresh()
        self.status_var.set('Local text recognition selected: '+self.ocr_var.get())

    def scan_entire_full_run(self):
        if self._auto_busy:
            messagebox.showinfo('AutoDirector busy','Another scan/render is running.',parent=self)
            return
        jobs=list(self.state['queue'])
        if not jobs:
            messagebox.showinfo('Full Run','Click + ADD VIDEOS first.',parent=self)
            return
        paths=[job['path'] for job in jobs]
        missing=[Path(p).name for p in paths if not Path(p).is_file()]
        if missing:
            messagebox.showerror('Missing videos','Re-add these recordings:\n' +
                                 '\n'.join(missing),parent=self)
            return
        if not self.save():
            return
        executable=quality_engine._cazy_tesseract(self.ocr_var.get())
        if not executable:
            self.status_var.set('Local text detection requires Tesseract OCR.')
            if messagebox.askyesno('Set up text detection',
                'AutoDirector now reads Start Challenge / On the other side / '
                'Challenge Completed with local OCR.\n\n'
                'It needs Tesseract OCR installed on your PC.\n'
                'Would you like to locate tesseract.exe?',parent=self):
                self.choose_ocr_engine()
            return
        self._auto_busy=True
        self._auto_cancel.clear()
        self._scan_started=time.monotonic()
        self._scan_last_fraction=0.
        self.scan_progress_var.set(0.)
        self.scan_state_var.set('SCANNING — 0.0%  |  Preparing video…')
        self.scan_detail_var.set(f'Starting OCR scan of {len(paths)} selected recording(s).')
        self.status_var.set(f'Scanning ALL {len(paths)} selected recording(s) in queue order…')
        def work():
            try:
                result=autodirector_fullrun.scan_full_run(
                    paths,{},ocr_path=executable,cancel=self._auto_cancel,
                    progress=lambda n:self._auto_messages.put(('progress',n)),
                    detail=lambda info:self._auto_messages.put(('scan_detail',info)))
                self._auto_messages.put(('fullrun_done',result))
            except Exception as exc:
                self._auto_messages.put(('error',str(exc)))
        threading.Thread(target=work,daemon=True).start()

    def show_detection_report(self,result):
        """Explain zero clips using *actual detected events*, not saved images."""
        win=tk.Toplevel(self)
        win.title('AutoDirector — Detection Report')
        win.geometry('850x570')
        win.minsize(670,450)
        win.configure(bg=BG)
        body=tk.Frame(win,bg=BG)
        body.pack(fill='both',expand=True,padx=18,pady=14)
        self._label(body,'FULL RUN SCAN REPORT',17,ACCENT2,True).pack(anchor='w')
        self._label(body,f'{len(result.get("reports",[]))} recording(s) scanned  '
                         f'•  {result.get("completed_chambers",0)} completed chamber(s)',
                    10,TEXT,True).pack(anchor='w',pady=(8,7))
        counts=result.get('event_counts',{})
        if not counts:
            counts={k:sum(e.get('kind')==k for report in result.get('reports',[])
                          for e in report.get('events',[]))
                    for k in ('start','switch','complete','failure')}
        stats=('ACTUALLY DETECTED IN VIDEO:  '
               f'Start {counts.get("start",0)}  •  '
               f'Other Side {counts.get("switch",0)}  •  '
               f'Completed {counts.get("complete",0)}  •  '
               f'Failed {counts.get("failure",0)}')
        self._label(body,stats,10,ACCENT2,True).pack(anchor='w',pady=(0,8))
        hints=[]
        if not result.get('segments'):
            hints.append('No complete chamber sequence was found. Nothing will be rendered.')
        if not counts.get('switch',0):
            hints.append('No "On the other side..." transition was detected.')
        if not counts.get('start',0):
            hints.append('No "Start Challenge" prompt was detected.')
        if not counts.get('complete',0):
            hints.append('No "Challenge Completed" screen was detected.')
        for report in result.get('reports',[]):
            for note in report.get('notes',[]):
                hints.append(Path(report.get('path','video')).name+': '+note)
        if not hints:
            hints.append('Review the proposed half clips and their cut boundaries.')
        tk.Label(body,text='\n'.join(hints[:9]),bg=CARD2,fg=YELLOW,
                 font=('Segoe UI',9),justify='left',anchor='w',
                 wraplength=790,padx=12,pady=10).pack(fill='x',pady=(0,10))
        self._label(body,'Detected game events (video timestamps)',10,TEXT,True).pack(anchor='w')
        tree=ttk.Treeview(body,columns=('video','time','event','how'),show='headings')
        for col,label,width in [('video','RECORDING',230),('time','TIME (s)',86),
                                ('event','EVENT',135),('how','DETECTION METHOD',270)]:
            tree.heading(col,text=label)
            tree.column(col,width=width,anchor='w')
        tree.pack(fill='both',expand=True,pady=(6,10))
        for report in result.get('reports',[]):
            for event in report.get('events',[]):
                tree.insert('',tk.END,values=(
                    Path(report.get('path','')).name,event.get('at',''),
                    event.get('kind','').upper(),event.get('evidence','')))
        bottom=tk.Frame(body,bg=BG)
        bottom.pack(fill='x')
        self._button(bottom,'Locate OCR Program',
                     lambda:(win.destroy(),self.choose_ocr_engine())).pack(side='left')
        self._button(bottom,'Scan Again',
                     lambda:(win.destroy(),self.scan_entire_full_run())).pack(side='left',padx=7)
        self._button(bottom,'Close',win.destroy).pack(side='right')

    def review_full_run(self,result):
        if not result.get('segments'):
            self.show_detection_report(result)
            return
        if self._review_window is not None and self._review_window.winfo_exists():
            self._review_window.destroy()
        win=tk.Toplevel(self)
        self._review_window=win
        win.title('AutoDirector — Full Run: All Chambers, One Video')
        win.geometry('1150x700')
        win.minsize(850,530)
        win.configure(bg=BG)
        body=tk.Frame(win,bg=BG)
        body.pack(fill='both',expand=True,padx=18,pady=16)
        self._label(body,'FULL RUN  •  ONE FINISHED VIDEO',16,ACCENT2,True).pack(anchor='w')
        summary=f'{result["completed_chambers"]} completed chamber(s) | {len(result["segments"])} half-clips | {len(result["reports"])} recording(s)'
        self._label(body,summary,10,TEXT,True).pack(anchor='w',pady=(7,3))
        self._label(body,'Review the cut boundaries. Disable anything incorrect. '
                         'All enabled halves will be JOINED in order into ONE MP4.',
                    9,YELLOW).pack(anchor='w',pady=(0,10))
        table=ttk.Treeview(body,columns=('use','chamber','side','video','start','end','evidence'),
                           show='headings',selectmode='browse',height=13)
        for field,label,width in (
            ('use','USE',55),('chamber','CHAMBER',83),('side','SIDE',125),
            ('video','RECORDING',175),('start','START',85),('end','END',85),
            ('evidence','DETECTION',330)):
            table.heading(field,text=label)
            table.column(field,width=width,anchor='w')
        table.pack(fill='both',expand=True)
        segments=[dict(seg) for seg in result['segments']]
        for i,seg in enumerate(segments):
            table.insert('',tk.END,iid=str(i),values=(
                'YES' if seg.get('enabled',True) else 'NO',
                seg['chamber'],seg['side'],Path(seg['path']).name,
                f'{seg["start"]:.2f}',f'{seg["end"]:.2f}',seg.get('evidence','manual')))
        fields=tk.Frame(body,bg=BG)
        fields.pack(fill='x',pady=(11,8))
        start=tk.StringVar()
        end=tk.StringVar()
        self._label(fields,'START (sec)',9,TEXT,True).pack(side='left')
        tk.Entry(fields,width=12,bg=CARD2,fg=TEXT,insertbackground=TEXT,
                 textvariable=start).pack(side='left',padx=(6,14),ipady=7)
        self._label(fields,'END (sec)',9,TEXT,True).pack(side='left')
        tk.Entry(fields,width=12,bg=CARD2,fg=TEXT,insertbackground=TEXT,
                 textvariable=end).pack(side='left',padx=(6,14),ipady=7)
        detail=tk.StringVar(value='Select a half to edit the start/end times')
        tk.Label(body,textvariable=detail,fg=MUTED,bg=BG,anchor='w',
                 font=('Segoe UI',9)).pack(fill='x',pady=(0,9))
        def selected_index():
            rows=table.selection()
            return int(rows[0]) if rows else None
        def choose(_event=None):
            i=selected_index()
            if i is None:return
            item=segments[i]
            start.set(str(item['start']))
            end.set(str(item['end']))
            detail.set(f'Chamber {item["chamber"]} {item["side"]}  •  {item["path"]}')
        table.bind('<<TreeviewSelect>>',choose)
        def update_row(i):
            seg=segments[i]
            table.item(str(i),values=(
                'YES' if seg.get('enabled',True) else 'NO',seg['chamber'],
                seg['side'],Path(seg['path']).name,
                f'{seg["start"]:.2f}',f'{seg["end"]:.2f}',
                seg.get('evidence','manual')))
        def apply_times():
            i=selected_index()
            if i is None:return False
            try:
                a,b=float(start.get()),float(end.get())
                if not (0<=a<b):raise ValueError('Start must be before end')
                segments[i]['start']=a
                segments[i]['end']=b
                update_row(i)
                return True
            except ValueError as exc:
                messagebox.showerror('Invalid cut',str(exc),parent=win)
                return False
        def toggle():
            i=selected_index()
            if i is None:return
            segments[i]['enabled']=not segments[i].get('enabled',True)
            update_row(i)
        def preview():
            i=selected_index()
            if i is not None:self._open_auto_source(segments[i]['path'])
        def add_manual():
            if len(segments)>=autodirector_fullrun.SEGMENT_MAX:
                messagebox.showerror('Full Run','Too many segments',parent=win)
                return
            dialog=tk.Toplevel(win)
            dialog.configure(bg=BG)
            dialog.title('Add Half Manually')
            dialog.geometry('520x290')
            panel=tk.Frame(dialog,bg=BG)
            panel.pack(fill='both',expand=True,padx=14,pady=14)
            files=[job['path'] for job in self.state['queue'] if Path(job['path']).is_file()]
            names=[Path(x).name for x in files]
            selection=tk.StringVar(value=names[0] if names else '')
            self._label(panel,'Recording',9,TEXT,True).pack(anchor='w')
            cb=ttk.Combobox(panel,state='readonly',values=names,textvariable=selection)
            cb.pack(fill='x',pady=5)
            a=tk.StringVar(value='0')
            b=tk.StringVar(value='10')
            side=tk.StringVar(value='First Half')
            self._label(panel,'Side',9,TEXT,True).pack(anchor='w')
            ttk.Combobox(panel,state='readonly',values=['First Half','Second Half'],
                         textvariable=side).pack(fill='x',pady=5)
            row=tk.Frame(panel,bg=BG)
            row.pack(fill='x',pady=8)
            tk.Entry(row,textvariable=a,width=10).pack(side='left')
            tk.Entry(row,textvariable=b,width=10).pack(side='left',padx=7)
            def commit_manual():
                try:
                    index=names.index(selection.get())
                    x,y=float(a.get()),float(b.get())
                    if not(0<=x<y):raise ValueError('Bad timestamps')
                    seg={'path':files[index],'chamber':result['completed_chambers']+1,
                         'side':side.get(),'start':x,'end':y,'enabled':True,
                         'evidence':'Manual (reviewed)'}
                    segments.append(seg)
                    i=len(segments)-1
                    table.insert('',tk.END,iid=str(i))
                    update_row(i)
                    table.selection_set(str(i))
                    choose()
                    dialog.destroy()
                except (ValueError,IndexError) as exc:
                    messagebox.showerror('Manual half',str(exc),parent=dialog)
            self._button(panel,'ADD HALF',commit_manual,True).pack(anchor='e',pady=5)
        def export():
            i=selected_index()
            if i is not None and start.get() and end.get():
                if not apply_times():
                    return
            try:
                accepted=autodirector_fullrun.validate_full_run_segments(segments)
                prefs=quality_engine.load_preferences()
                preset=self.preset_var.get()
                if preset and preset!='Use last-used settings' and preset not in prefs['presets']:
                    raise ValueError('Saved preset not found. Reload saved presets.')
            except (ValueError,OSError) as exc:
                messagebox.showerror('Full Run',str(exc),parent=win)
                return
            folder=filedialog.askdirectory(parent=win,title='Where should the ONE finished Full Run video go?')
            if not folder:return
            if not messagebox.askyesno('Export ONE Full Run',
                f'Join {len(accepted)} halves from {len(set(x["path"] for x in accepted))} recordings.\n'
                f'Preset: {preset or "Use last-used settings"}\n'
                f'ONE finished MP4 + thumbnail + title.\n\n'
                'Render now? Your original files will remain untouched.',
                parent=win):
                return
            music=next((j.get('music','') for j in self.state['queue']
                        if j.get('music')), '')
            self._auto_busy=True
            self._auto_cancel.clear()
            def work():
                try:
                    output=autodirector_fullrun.render_full_run(
                        accepted,folder,preset,music=music,cancel=self._auto_cancel,
                        progress=lambda pct,label:
                        self._auto_messages.put(('render_progress',(pct,label))))
                    self._auto_messages.put(('render_done',output))
                except Exception as exc:
                    self._auto_messages.put(('error',str(exc)))
            threading.Thread(target=work,daemon=True).start()
            win.destroy()
        buttons=tk.Frame(body,bg=BG)
        buttons.pack(fill='x',pady=8)
        self._button(buttons,'Apply Times',apply_times).pack(side='left',padx=(0,6))
        self._button(buttons,'Enable / Disable',toggle).pack(side='left',padx=(0,6))
        self._button(buttons,'Preview Source',preview).pack(side='left',padx=(0,6))
        self._button(buttons,'+ Manual Half',add_manual).pack(side='left')
        self._button(buttons,'Scan Report',lambda:self.show_detection_report(result)).pack(
            side='left',padx=6)
        self._button(buttons,'RENDER ONE FULL RUN',export,True).pack(side='right')
        if segments:
            table.selection_set('0')
            choose()



if __name__=='__main__':
    Studio().mainloop()
