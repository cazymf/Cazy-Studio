"""Real Tk smoke: xvfb-run -a /usr/bin/python3 tests/gui_smoke_creative_0400.py"""
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

path=Path(__file__).resolve().parents[1]/'source'/'app.py'
parsed=ast.parse(path.read_text(encoding='utf-8'))
nodes=[]
for node in parsed.body:
    if isinstance(node,ast.Assign) and any(
        isinstance(target,ast.Name) and target.id in
        ('BG','CARD','CARD2','BORDER','TEXT','MUTED','ACCENT','ACCENT2',
         'GREEN','RED','YELLOW') for target in node.targets):
        nodes.append(node)
    elif isinstance(node,ast.ClassDef) and node.name=='CreativeLabWindow':
        nodes.append(node)
with tempfile.TemporaryDirectory() as td:
    root_dir=Path(td)
    source=root_dir/'OBS.mp4'
    source.write_bytes(b'original bytes')
    destination=root_dir/'Export'
    def report_path(folder,title='Creative Report'):
        dest=Path(folder)
        dest.mkdir(parents=True,exist_ok=True)
        return dest/'Cazy_Creative_Report.md'
    ns={
        'tk':tk,'ttk':ttk,'Path':Path,'os':os,'queue':queue,
        'threading':threading,'subprocess':subprocess,
        'filedialog':filedialog,'messagebox':messagebox,
        'APP_VERSION':'0.4.0',
        '_autodirector_choose_export_music':lambda state:'',
        'quality_engine':types.SimpleNamespace(
            creative_bug_report_path=report_path,
            creative_music_offset=lambda beats,anchor:0.,
            creative_vsr_preflight=lambda *args:{
                'message':'Model checkpoint not configured',
                'ready':False,'status':'Setup required'})}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec'),ns)
    root=tk.Tk()
    root.geometry('1230x910')
    class DummyOwner(tk.Frame):
        def __init__(self,parent):
            super().__init__(parent)
            self.state={'queue':[{'path':str(source)}],
                        'random_music':False,'music_folder':''}
        def _selected(self):return 0
    owner=DummyOwner(root)
    win=ns['CreativeLabWindow'](owner)
    try:
        win.geometry('1040x720')
        win.destination_var.set(str(destination))
        root.update()
        win.update()
        assert len(win.tabs.tabs())==7,len(win.tabs.tabs())
        assert win.source_var.get()==str(source)
        assert win.analyze_button.winfo_ismapped()
        assert win.cancel_button.winfo_ismapped()
        assert win.progress.winfo_ismapped()
        sample={'source':str(source),'found':2,'highlights':[
            {'at':2.0,'start':1.0,'end':4.0,'score':80.0},
            {'at':7.0,'start':6.0,'end':9.0,'score':70.0}],
            'frames_analyzed':100}
        win._events.put(('highlights',sample))
        win._poll()
        win.update()
        assert len(win._selected_highlights())==2
        assert len(win.table.get_children())==2
        for name in ('Shorts','Cinematic','Thumbnail',
                     'Music Sync','RealBasicVSR + EGVSR','Bug Notes'):
            win.tabs.select(win.pages[name])
            win.update()
        assert win.check_vsr()['ready'] is False
        previous=messagebox.showinfo
        messagebox.showinfo=lambda *args,**kwargs:None
        try:win.save_bug_notes()
        finally:messagebox.showinfo=previous
        report=destination/'Cazy_Creative_Report.md'
        assert report.is_file()
        text=report.read_text(encoding='utf-8')
        assert '## [ ] Shorts Generator' in text
        assert '## [ ] RealBasicVSR + EGVSR' in text
        assert 'Studio version: 0.4.0' in text
        print('Creative Lab GUI: 7 tabs, highlights, model setup, bug report: PASS')
    finally:
        win.destroy()
        root.destroy()
