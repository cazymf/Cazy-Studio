"""Headless Tk smoke test of Quality Lab's actual export progress popup."""
import ast
import os
import queue
import tempfile
import threading
import time
import tkinter as tk
import types
from pathlib import Path
from tkinter import ttk

ROOT=Path(__file__).resolve().parents[1]
source=ast.parse((ROOT/'source'/'quality_ui.py').read_text(encoding='utf-8'))
cls=next(n for n in source.body if isinstance(n,ast.ClassDef) and n.name=='QualityLab')
needed={'label','button','_show_render_window','_finish_render_window',
        'export','cancel_job','_poll'}
found=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in needed]
assert len(found)==len(needed),needed-{n.name for n in found}

CARD='#141d2d'
CARD2='#1c273a'
TEXT='#edf3ff'
MUTED='#92a4bd'
ACCENT='#9b7cff'
ACCENT2='#5abacb'
GREEN='#50d2a3'
finished=threading.Event()
dialog_messages=[]
def fake_render(path,target,opts,status,cancel,sample,root,progress):
    status('Starting GPU render')
    progress(42.5,'Encoding MP4: NVIDIA NVENC',51,120)
    if not finished.wait(timeout=5):
        raise RuntimeError('Mock render did not receive completion signal')
    progress(100.,'Complete',120,120)
    return target

namespace={
    'tk':tk,'ttk':ttk,'os':os,'Path':Path,'threading':threading,'queue':queue,
    '__file__':str(ROOT/'source'/'quality_ui.py'),
    'CARD':CARD,'CARD2':CARD2,'TEXT':TEXT,'MUTED':MUTED,
    'ACCENT':ACCENT,'ACCENT2':ACCENT2,'GREEN':GREEN,
    'filedialog':types.SimpleNamespace(asksaveasfilename=lambda **kw:
                                      str(Path(tempfile.gettempdir())/'cazy_gui_test.mp4')),
    'quality':types.SimpleNamespace(render_quality=fake_render),
    'messagebox':types.SimpleNamespace(
        showinfo=lambda *a,**kw: dialog_messages.append(('info',a)),
        showerror=lambda *a,**kw: dialog_messages.append(('error',a)))
}
exec(compile(ast.Module(body=found,type_ignores=[]),
             str(ROOT/'source'/'quality_ui.py'),'exec'),namespace)

class Harness(tk.Toplevel):
    def __init__(self,root):
        super().__init__(root)
        self.configure(bg=CARD)
        self.path=str(Path(tempfile.gettempdir())/'mock_source.mp4')
        self.busy=False
        self.cancel=threading.Event()
        self.jobs=queue.Queue()
        self.status=tk.StringVar(value='Ready')
        self.progress_title=tk.StringVar(value='0% • Ready')
        self.progress_bar=ttk.Progressbar(self,maximum=100)
        self.progress_bar.pack(fill='x')
        self.render_window=None
        self.render_popup_title=tk.StringVar(value='0.0% • Preparing')
        self.render_popup_stage=tk.StringVar(value='Waiting…')
        self.render_popup_progress=tk.DoubleVar(value=0.)
        self.last_export=None
        self.cache_dir=types.SimpleNamespace(name=tempfile.gettempdir())
    _collect=lambda self:types.SimpleNamespace()
    _save_last_used=lambda self:None

for method in needed:
    setattr(Harness,method,namespace[method])

root=tk.Tk()
root.withdraw()
quality=Harness(root)
quality.export(sample=False)
root.update()
assert quality.busy
assert quality.render_window.winfo_exists()
assert 'Live Render Progress' in quality.render_window.title()
assert '0.0%' in quality.render_popup_title.get()
quality.render_window.withdraw()
assert quality.render_window.state()=='withdrawn'
quality._show_render_window()
root.update()
assert quality.render_window.state()!='withdrawn'

deadline=time.monotonic()+4
while time.monotonic()<deadline and quality.render_popup_progress.get()<42:
    quality._poll()
    root.update()
    time.sleep(.05)
assert quality.render_popup_progress.get()==42.5
assert 'NVIDIA NVENC' in quality.render_popup_stage.get()
assert '42.5%' in quality.render_popup_title.get()

finished.set()
deadline=time.monotonic()+4
while time.monotonic()<deadline and quality.busy:
    quality._poll()
    root.update()
    time.sleep(.05)
assert not quality.busy
assert quality.render_popup_progress.get()==100.
assert 'Render complete' in quality.render_popup_title.get()
assert dialog_messages and dialog_messages[-1][0]=='info'
quality.destroy()
root.destroy()
print('Quality Lab render popup GUI smoke: PASS (opens, restores, progresses, finishes)')
