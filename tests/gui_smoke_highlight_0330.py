"""xvfb-run -a /usr/bin/python3 tests/gui_smoke_highlight_0330.py

Smoke-test only the new optional Tk Highlight Finder window and its UI flows.
"""
import ast
import os
import queue
import subprocess
import tempfile
import threading
import tkinter as tk
import types
from pathlib import Path
from tkinter import filedialog,messagebox,ttk
from PIL import Image,ImageTk

path=Path(__file__).resolve().parents[1]/'source'/'app.py'
parsed=ast.parse(path.read_text(encoding='utf-8'))
nodes=[]
for node in parsed.body:
    if isinstance(node,ast.Assign) and any(
        isinstance(target,ast.Name) and target.id in
        ('BG','CARD','CARD2','BORDER','TEXT','MUTED','ACCENT','ACCENT2',
         'GREEN','RED','YELLOW') for target in node.targets):
        nodes.append(node)
    elif isinstance(node,ast.ClassDef) and node.name=='HighlightFinderWindow':
        nodes.append(node)
ns={'tk':tk,'ttk':ttk,'Path':Path,'os':os,'queue':queue,
    'threading':threading,'subprocess':subprocess,'filedialog':filedialog,
    'messagebox':messagebox,'Image':Image,'ImageTk':ImageTk,
    'quality_engine':types.SimpleNamespace()}
exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec'),ns)

with tempfile.TemporaryDirectory() as temp:
    video=Path(temp)/'test.mp4'
    video.write_bytes(b'original video')
    root=tk.Tk()
    root.geometry('1180x850')
    class DummyOwner(tk.Frame):
        def __init__(self,parent):
            super().__init__(parent)
            self.state={'queue':[{'path':str(video)}]}
        def _selected(self):
            return 0
    owner=DummyOwner(root)
    win=ns['HighlightFinderWindow'](owner)
    try:
        win.geometry('920x680')
        root.update()
        win.update()
        assert win.scan_button.winfo_ismapped(),'Analyze button not visible'
        assert win.export_button.winfo_ismapped(),'Export clips button not visible'
        assert win.export_button.winfo_height()>30,'Export button too small'
        assert (win.export_button.winfo_rooty()+win.export_button.winfo_height()
                <=win.winfo_rooty()+win.winfo_height()),'Export hidden below window'
        assert win.source_var.get()==str(video)
        fake={
            'source':str(video),'duration':120.,'found':2,'frames_analyzed':120,
            'highlights':[
                {'at':20.,'start':17.,'end':25.,'score':81.,'description':'Motion'},
                {'at':56.,'start':53.,'end':61.,'score':88.,'description':'Detail'},
            ],
        }
        win._events.put(('scan',fake))
        win._poll()
        win.update()
        assert len(win.table.get_children())==2
        assert len(win._chosen())==2
        assert win.progress_var.get()==0
        img=Image.new('RGB',(640,220),(70,70,80))
        win._events.put(('preview',img))
        win._poll()
        win.update()
        assert win._preview_photo is not None
        assert win.preview_label.winfo_height()>100
        seen=[]
        win._work=lambda kind,action:seen.append(kind)
        previous=filedialog.askdirectory
        filedialog.askdirectory=lambda **kwargs:str(Path(temp)/'Highlight Clips')
        try:win.export()
        finally:filedialog.askdirectory=previous
        assert seen==['export']
        assert 'Exporting 2 reviewed' in win.status_var.get()
        win.cancel()
        assert win._cancel.is_set()
        print('Highlight Finder GUI: controls visible, shortlist, preview, clip export: PASS')
    finally:
        win.destroy()
        root.destroy()
