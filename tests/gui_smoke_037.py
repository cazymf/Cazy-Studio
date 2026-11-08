"""Run with xvfb-run -a python tests/gui_smoke_037.py.

Load only the AutoDirector window class and queue helpers from app.py; avoid
the unrelated editor's optional video/AI modules in this GUI smoke test.
"""
import ast
import hashlib
import json
import os
import random
import time
import subprocess
import queue
import threading
import tempfile
import tkinter as tk
import types
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

path = Path(__file__).resolve().parents[1] / 'source' / 'app.py'
root_ast = ast.parse(path.read_text(encoding='utf-8'))
wanted = {
    '_autodirector_path', '_autodirector_defaults', '_autodirector_validate',
    '_autodirector_load', '_autodirector_save', '_autodirector_add',
    '_autodirector_tracks', '_autodirector_prepare',
    '_autodirector_scan_cache_path', '_autodirector_source_fingerprint',
    '_autodirector_save_scan_cache', '_autodirector_load_scan_cache'
}
nodes = []
for node in root_ast.body:
    if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name)
        and (target.id.startswith('_AUTODIRECTOR_') or target.id in {
            'BG', 'CARD', 'CARD2', 'BORDER', 'TEXT', 'MUTED', 'ACCENT',
            'ACCENT2', 'GREEN', 'RED', 'YELLOW'
        }) for target in node.targets
    ):
        nodes.append(node)
    elif isinstance(node, ast.FunctionDef) and node.name in wanted:
        nodes.append(node)
    elif isinstance(node, ast.ClassDef) and node.name == 'AutoDirectorView':
        nodes.append(node)
ns = {
    'os': os, 'tk': tk, 'ttk': ttk, 'Path': Path, 'tempfile': tempfile,
    'json': json, 'hashlib': hashlib, 'random': random, 'time': time, 'queue': queue, 'threading': threading,
    'filedialog': filedialog,
    'messagebox': messagebox, 'subprocess': subprocess,
    'quality_engine': types.SimpleNamespace(
        load_preferences=lambda: {'presets': {}},
        resolve_saved_render_preset=lambda name: types.SimpleNamespace(
            resolution='1440p Landscape', fps=60, upscale='Real-ESRGAN Anime',
            frame_method='Native / Duplicate', encoder='Auto / NVIDIA NVENC',
            quality='Maximum'),
        _cazy_tesseract=lambda *args: None),
    'autodirector_fullrun': types.SimpleNamespace(
        normalize_references=lambda refs, old='': {
            k:list(refs.get(k,[])) for k in ('start','switch','complete','failure','blessing')},
        SEGMENT_MAX=200,
        REFERENCE_KINDS={
            'start':'Start Challenge','switch':'On the other side...',
            'complete':'Challenge Completed','failure':'Challenge Failed / Retry',
            'blessing':'Floor / Benediction'})
}
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), ns)

with tempfile.TemporaryDirectory() as temp:
    os.environ['LOCALAPPDATA'] = temp
    recording = Path(temp) / 'selected.mp4'
    recording.write_bytes(b'mock selected video')
    unused = Path(temp) / 'unused.mp4'
    unused.write_bytes(b'unused')
    window = tk.Tk()
    window.geometry('1200x860')
    try:
        view = ns['AutoDirectorView'](window)
        view.pack(fill='both', expand=True)
        window.update_idletasks()
        assert len(view.state['queue']) == 0
        assert view.empty_hint.winfo_manager() == 'pack'
        assert ns['_autodirector_add'](view.state, [recording]) == 1
        view.refresh()
        window.update_idletasks()
        assert len(view.tree.get_children()) == 1
        assert not view.empty_hint.winfo_manager()
        view.tree.selection_set('0')
        view._update_details()
        assert 'selected.mp4' in view.detail_var.get()
        assert view.save()
        assert '1440p Landscape' in view.recipe_details_var.get()
        assert 'Real-ESRGAN Anime' in view.recipe_details_var.get()
        view.remove()
        window.update_idletasks()
        assert not view.state['queue']
        assert view.empty_hint.winfo_manager() == 'pack'
        assert recording.is_file() and unused.is_file()
        # No transition reference: show an actionable preflight instead of
        # scanning silently and opening a huge empty chamber table.
        ns['_autodirector_add'](view.state,[recording])
        view.state['references']={
            'start':[str(recording)], 'switch':[],
            'complete':[str(recording)],'failure':[],'blessing':[]}
        old_dialog=messagebox.askyesno
        messagebox.askyesno=lambda *a,**k:False
        try:
            view.scan_entire_full_run()
            assert not view._auto_busy
            assert 'Tesseract OCR' in view.status_var.get()
        finally:
            messagebox.askyesno=old_dialog
        # The Full Run review is a separate visible table containing BOTH halves.
        full_run={'completed_chambers':1,'reports':[{'duration':14.}],
                  'segments':[
                      {'path':str(recording),'chamber':1,'side':'First Half',
                       'start':0.,'end':6.,'enabled':True,'evidence':'start->switch'},
                      {'path':str(recording),'chamber':1,'side':'Second Half',
                       'start':7.,'end':13.,'enabled':True,'evidence':'start->complete'}]}
        view.review_full_run(full_run)
        window.update_idletasks()
        assert view._review_window.winfo_exists()
        tree=[child for child in view._review_window.winfo_children()[0].winfo_children()
              if isinstance(child,ttk.Treeview)][0]
        assert len(tree.get_children())==2
        view._review_window.destroy()
        # A previously scanned run is reusable after the dialog closes.
        ns['_autodirector_save_scan_cache'](full_run,[str(recording)])
        view.mode_var.set('Full Run')
        view.reopen_saved_full_run()
        window.update_idletasks()
        assert view._review_window.winfo_exists()
        assert 'SAVED SCAN READY' in view.scan_state_var.get()
        assert len([x for x in view._review_window.winfo_children()[0].winfo_children()
                    if isinstance(x,ttk.Treeview)][0].get_children())==2
        view._review_window.destroy()

        # Render progress must be shown in the same visible main panel as OCR
        # scan progress, not merely in a hidden application footer.
        view._auto_busy=True
        view._begin_auto_render('Preparing 2 reviewed halves')
        window.update_idletasks()
        assert view.scan_progress_var.get()==0.
        assert 'RENDERING' in view.scan_state_var.get()
        view._auto_messages.put(('render_progress',(42.5,
            'Half 1/2: RIFE AI interpolation • 17.4 FPS • ETA 9s')))
        view._poll_auto_messages()
        window.update_idletasks()
        assert abs(view.scan_progress_var.get()-42.5)<.001
        assert '42.5%' in view.scan_state_var.get()
        assert 'RIFE AI interpolation' in view.scan_detail_var.get()
        assert view.scan_bar.winfo_manager()=='pack'
        # Out-of-range updates cannot overrun the bar.
        view._auto_messages.put(('render_progress',(112,'Joining reviewed halves')))
        view._poll_auto_messages()
        assert view.scan_progress_var.get()==100.
        previous_showinfo=messagebox.showinfo
        messagebox.showinfo=lambda *a,**k:None
        try:
            view._auto_messages.put(('render_done',{'video':str(recording)}))
            view._poll_auto_messages()
            assert view.scan_state_var.get()=='RENDER COMPLETE — 100%'
            assert view.scan_progress_var.get()==100.
            assert not view._auto_busy
        finally:
            messagebox.showinfo=previous_showinfo
        print('AutoDirector GUI smoke: PASS (library, Full Run review, visible render progress)')
    finally:
        window.destroy()
