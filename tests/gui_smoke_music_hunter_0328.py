"""Run with xvfb-run -a python tests/gui_smoke_music_hunter_0328.py.

Render the real Music Hunter Tk class under a dummy parent without importing
or invoking external video, decoder, internet, or model dependencies.
"""
import ast
import os
import queue
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
import types

app=Path(__file__).resolve().parents[1]/'source'/'app.py'
tree=ast.parse(app.read_text(encoding='utf-8'))
nodes=[]
for node in tree.body:
    if isinstance(node,ast.Assign) and any(
        isinstance(target,ast.Name) and target.id in (
            'BG','CARD','CARD2','BORDER','TEXT','MUTED','ACCENT',
            'ACCENT2','GREEN','RED','YELLOW') for target in node.targets):
        nodes.append(node)
    elif isinstance(node,ast.ClassDef) and node.name=='MusicHunterWindow':
        nodes.append(node)

with tempfile.TemporaryDirectory() as folder:
    os.environ['LOCALAPPDATA']=folder
    ns={'tk':tk,'ttk':ttk,'Path':Path,'os':os,
        'queue':queue,'threading':threading,
        'filedialog':filedialog,'messagebox':messagebox,
        'quality_engine':types.SimpleNamespace(
            music_hunter_library_path=lambda:Path(folder)/'CazyStudio'/'Music')}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(app),'exec'),ns)
    root=tk.Tk()
    root.geometry('1040x760')
    root.update()
    class DummyOwner(tk.Frame):
        def __init__(self,parent):
            super().__init__(parent)
            self.music_var=tk.StringVar(value='')
            self.saved=False
            self.refreshed=False
        def save(self):
            self.saved=True
            return True
        def refresh(self):
            self.refreshed=True
    owner=DummyOwner(root)
    try:
        win=ns['MusicHunterWindow'](owner)
        win.geometry('900x690')
        win.update()
        assert win.download_button.winfo_ismapped(),'MP3 action is hidden'
        assert win.download_button.winfo_height()>=40,'MP3 action is too small'
        assert win.download_button.winfo_rooty()+win.download_button.winfo_height() <= win.winfo_rooty()+win.winfo_height(),'MP3 action extends below the window'
        assert win.folder_var.get()==str(Path(folder)/'CazyStudio'/'Music')
        for i,ident in enumerate(('AAAAAAAAAAA','BBBBBBBBBBB','CCCCCCCCCCC')):
            win._results.append({'title':'Song '+str(i),
                'url':'https://www.youtube.com/watch?v='+ident,
                'channel':'Artist','upload_date':'2026-11-01',
                'license':'CC0'})
            win.table.insert('',tk.END,iid=str(i),values=(
                '2026-11-01','Song '+str(i),'Artist','CC0'))
        win.select_all()
        assert len(win._selected_items())==3
        assert '3 songs selected'==win.selected_count_var.get()
        win.permission_var.set(True)
        captured=[]
        win._work=lambda kind,action:captured.append((kind,action))
        win.download()
        assert captured[0][0]=='batch'
        assert 'Preparing 3 selected' in win.status_var.get()
        win.clear_selection()
        assert not win._selected_items()
        win.pasted_url_var.set('https://youtu.be/AAAAAAAAAAA')
        win.download()
        assert captured[1][0]=='batch'
        # Finish a mocked batch through the same poll path that real workers use.
        win._messages.put(('batch',{
            'library':win.folder_var.get(),
            'downloaded':[str(Path(folder)/'CazyStudio'/'Music'/'song.mp3')],
            'skipped':[],'failed':[],'cancelled':False,'total':1}))
        original=messagebox.showinfo
        messagebox.showinfo=lambda *a,**k:None
        try:win._poll()
        finally:messagebox.showinfo=original
        assert owner.saved and owner.refreshed
        assert owner.music_var.get()==win.folder_var.get()
        print('Music Hunter GUI smoke: pinned MP3 button, select all, batch import: PASS')
        win.destroy()
    finally:root.destroy()
