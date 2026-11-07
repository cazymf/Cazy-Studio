"""Run with xvfb-run -a python tests/gui_smoke_037.py.

Load only the AutoDirector window class and queue helpers from app.py; avoid
the unrelated editor's optional video/AI modules in this GUI smoke test.
"""
import ast
import json
import os
import random
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
    '_autodirector_tracks', '_autodirector_prepare'
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
    'json': json, 'random': random, 'queue': queue, 'threading': threading,
    'filedialog': filedialog,
    'messagebox': messagebox, 'subprocess': subprocess,
    'quality_engine': types.SimpleNamespace(load_preferences=lambda: {'presets': {}}),
    'autodirector_fullrun': types.SimpleNamespace(
        normalize_references=lambda refs, old='': {
            k:list(refs.get(k,[])) for k in ('start','switch','complete','failure','blessing')},
        REFERENCE_KINDS={
            'start':'Start Challenge','switch':'On the other side...',
            'complete':'Challenge Completed','failure':'Challenge Failed / Retry',
            'blessing':'Floor / Benediction'},SEGMENT_MAX=200)
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
        view.remove()
        window.update_idletasks()
        assert not view.state['queue']
        assert view.empty_hint.winfo_manager() == 'pack'
        assert recording.is_file() and unused.is_file()
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
        print('AutoDirector GUI smoke: PASS (open, add, select, save, remove, empty state)')
    finally:
        window.destroy()
