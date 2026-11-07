"""Cazy Studio update manifest verification, staging, installation, and rollback.

No third-party modules. Only author-approved *app root files* may be replaced;
projects, .venv, Addons, presets, exports, and recordings are not touched.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

REPOSITORY = 'cazymf/Cazy-Studio'
API_URL = 'https://api.github.com/repos/' + REPOSITORY + '/releases/latest'
PRODUCT = 'CazyStudio'
APP_VERSION = '0.3.12'
MAX_ZIP_BYTES = 120 * 1024 * 1024
MAX_UNPACKED_BYTES = 230 * 1024 * 1024
MAX_FILES = 80
BACKUP_DIR = '.cazy-update-backups'
BACKUP_STATE = '.cazy-last-update.json'
SAFE_FILES = {
    'Cazy Studio.exe', 'app.py', 'preview_worker.py', 'engine.py', 'quality_engine.py', 'quality_ui.py', 'autodirector_fullrun.py',
    'install_ai_addons.py', 'update_center.py', 'update_core.py', 'update_worker.py',
    'version.py', 'requirements.txt', 'launcher_windows.go',
    'Setup_Cazy_Studio.bat', 'Start_Cazy_Studio.bat', 'Launch Cazy Studio.vbs',
    'Build_Standalone_EXE.bat', 'Install_AI_Addons.bat',
    'Restore_Previous_Version.bat', 'Upgrade_v0_2_to_v0_3.bat', 'README_START_HERE.md', 'README_UPDATES.md'
}
SAFE_FILE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_. -]{0,90}\.(?:py|exe|bat|vbs|txt|md|json)$')

USER_AGENT = 'Cazy-Studio-Update-Center/0.3'


def version_tuple(value):
    match = re.fullmatch(r'v?(\d{1,3})\.(\d{1,3})\.(\d{1,3})', str(value))
    if not match:
        raise ValueError('Unexpected update version: ' + str(value))
    return tuple(int(n) for n in match.groups())


def release_update_info(release: dict, current=APP_VERSION) -> dict | None:
    if release.get('draft') or release.get('prerelease'):
        return None
    latest_tag = release.get('tag_name', '')
    latest = version_tuple(latest_tag)
    if latest <= version_tuple(current):
        return None
    version = '.'.join(map(str, latest))
    asset_name = 'Cazy_Studio_v' + '_'.join(map(str, latest)) + '_Update.zip'
    for asset in release.get('assets', []):
        if asset.get('name') != asset_name or asset.get('state') != 'uploaded':
            continue
        url = asset.get('browser_download_url', '')
        u = urllib.parse.urlparse(url)
        if u.scheme != 'https' or u.netloc != 'github.com' or not u.path.startswith('/cazymf/Cazy-Studio/releases/download/'):
            raise ValueError('Update asset URL is not from our official GitHub repository')
        digest = asset.get('digest') or ''
        if not re.fullmatch(r'sha256:[a-fA-F0-9]{64}', digest):
            raise ValueError('GitHub has not provided a SHA-256 digest for this release asset')
        size = int(asset.get('size') or 0)
        if size <= 0 or size > MAX_ZIP_BYTES:
            raise ValueError('Release update ZIP size is invalid')
        return {'version': version, 'url': url, 'sha256': digest[7:].lower(), 'size': size,
                'notes': str(release.get('body') or '')[:1800]}
    raise ValueError('Latest release exists, but is missing '+asset_name+'; publish the matching update ZIP')


def get_latest_release(timeout=20):
    request = urllib.request.Request(API_URL, headers={'Accept':'application/vnd.github+json', 'User-Agent':USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            return json.loads(r.read(2*1024*1024))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # Repo has no published releases yet.
        raise


def download_update(info, dest, on_progress=None, opener=None):
    """Download to a file and validate the GitHub-reported SHA256."""
    path = Path(dest)
    request = urllib.request.Request(info['url'], headers={'User-Agent':USER_AGENT})
    response = (opener or urllib.request.urlopen)(request, timeout=120)
    digest = hashlib.sha256()
    count = 0
    try:
        with response as resp, path.open('wb') as out:
            while True:
                chunk = resp.read(1024*1024)
                if not chunk: break
                count += len(chunk)
                if count > MAX_ZIP_BYTES: raise ValueError('Update download exceeds size limit')
                digest.update(chunk)
                out.write(chunk)
                if on_progress: on_progress(count, info['size'])
        if count != info['size']: raise ValueError('Download size mismatch')
        if digest.hexdigest().lower() != info['sha256']:
            raise ValueError('SHA-256 mismatch; the update was not installed')
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def inspect_update_zip(archive, expected_version=None):
    """Validate each root file, disallow traversal, disallow symlinks, check hashes."""
    archive=Path(archive)
    with zipfile.ZipFile(archive) as zf:
        infos=zf.infolist()
        if len(infos) > MAX_FILES + 1:
            raise ValueError('Too many entries in update ZIP')
        names=[i.filename for i in infos]
        if len(names) != len(set(names)):
            raise ValueError('Duplicate filenames are not allowed in update ZIP')
        if names.count('update_manifest.json') != 1:
            raise ValueError('Missing update_manifest.json')
        manifest=json.loads(zf.read('update_manifest.json'))
        if manifest.get('product') != PRODUCT or manifest.get('format') != 1:
            raise ValueError('Update does not belong to Cazy Studio')
        version=manifest.get('version')
        version_tuple(version)
        if expected_version is not None and version_tuple(version) != version_tuple(expected_version):
            raise ValueError('Update version differs from the GitHub release')
        files=manifest.get('files')
        if not isinstance(files,dict) or not files:
            raise ValueError('Empty or invalid file manifest')
        if 'app.py' not in files or 'update_core.py' not in files:
            raise ValueError('Update is missing required application files')
        if set(names) != set(files)|{'update_manifest.json'}:
            raise ValueError('ZIP contains unexpected files')
        total=0
        for item in infos:
            name=item.filename
            if item.is_dir() or name.startswith('/') or '\\' in name or '/' in name or '..' in name:
                raise ValueError('Unexpected update path: '+name)
            if (item.external_attr>>16)&0o170000 == 0o120000:
                raise ValueError('Symlink not allowed in update')
            if name == 'update_manifest.json': continue
            if name not in SAFE_FILES or not SAFE_FILE.fullmatch(name):
                raise ValueError('Update is not allowed to replace: '+name)
            total+=item.file_size
            if total > MAX_UNPACKED_BYTES: raise ValueError('Update unpacked size too large')
            expected=files.get(name)
            if not isinstance(expected,str) or not re.fullmatch(r'[a-f0-9]{64}',expected):
                raise ValueError('Invalid file checksum for '+name)
            if hashlib.sha256(zf.read(name)).hexdigest()!=expected:
                raise ValueError('Update file hash mismatch: '+name)
        return manifest


def _restore(root: Path, folder: Path, state: dict):
    """Restore exact files from a previous version."""
    replaced=state['replaced']
    created=state['created']
    for name in reversed(created):
        p=root/name
        if p.is_file():p.unlink()
    for name in reversed(replaced):
        old=folder/name
        if old.is_file():
            shutil.copy2(old,root/name)


def apply_update_zip(archive, destination, version):
    """Apply verified update only after Studio exits. Transactional rollback on errors."""
    root=Path(destination).resolve()
    manifest=inspect_update_zip(archive, version)
    current_version_file=root/'version.py'
    # The caller checks newest release, but enforce monotonicity again.
    if current_version_file.exists():
        text=current_version_file.read_text('utf-8')
        m=re.search(r'APP_VERSION\s*=\s*[\'"](\d+\.\d+\.\d+)[\'"]',text)
        if m and version_tuple(manifest['version'])<=version_tuple(m.group(1)):
            raise ValueError('Update is not newer than installed version')
    backups=root/BACKUP_DIR
    backups.mkdir(parents=True,exist_ok=True)
    stamp=time.strftime('%Y%m%d-%H%M%S')
    folder=backups/(stamp+'-'+manifest['version'].replace('.','_'))
    folder.mkdir()
    replaced=[]
    created=[]
    files=list(manifest['files'])
    try:
        # Back up ALL current files first (so an in-use file fails before changing anything).
        for name in files:
            target=root/name
            if target.is_symlink():raise ValueError('Refusing to overwrite symbolic link: '+name)
            if target.exists():
                if not target.is_file():raise ValueError('Unexpected directory at '+name)
                shutil.copy2(target,folder/name)
                replaced.append(name)
            else:created.append(name)
        # Stage and replace each file by atomic rename on same volume.
        with zipfile.ZipFile(archive) as zf:
            for name in files:
                target=root/name
                staged=root/(name+'.cazy-updating')
                try:
                    with zf.open(name) as src,staged.open('wb') as out:
                        shutil.copyfileobj(src,out)
                    os.replace(staged,target)
                finally:
                    staged.unlink(missing_ok=True)
        state={'from_backup':folder.name,'version':manifest['version'],
               'replaced':replaced,'created':created}
        (root/BACKUP_STATE).write_text(json.dumps(state,indent=2),encoding='utf-8')
        return state
    except Exception:
        _restore(root,folder,{'replaced':replaced,'created':created})
        raise


def rollback_latest(destination):
    root=Path(destination).resolve()
    state_path=root/BACKUP_STATE
    if not state_path.is_file():raise ValueError('No previous update to roll back')
    state=json.loads(state_path.read_text('utf-8'))
    name=state.get('from_backup','')
    if not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[0-9_]+',name):
        raise ValueError('Invalid rollback reference')
    folder=root/BACKUP_DIR/name
    if not folder.is_dir():raise ValueError('Backup files are missing')
    for filename in state['replaced']:
        if filename not in SAFE_FILES or not (folder/filename).is_file():
            raise ValueError('Incomplete rollback backup')
    for filename in state['created']:
        if filename not in SAFE_FILES:raise ValueError('Invalid rollback filename')
    _restore(root,folder,state)
    state_path.unlink()
    return state


def build_update_zip(source_root, archive, version):
    """Developer helper for creating release ZIP with per-file hashes."""
    root=Path(source_root)
    safe_files=sorted(x for x in root.iterdir() if x.is_file() and SAFE_FILE.fullmatch(x.name)
                      and x.name in SAFE_FILES)
    files={x.name:hashlib.sha256(x.read_bytes()).hexdigest() for x in safe_files}
    manifest={'product':PRODUCT,'format':1,'version':version,'files':files}
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=7) as zf:
        zf.writestr('update_manifest.json',json.dumps(manifest,indent=2))
        for file in safe_files:zf.write(file,file.name)
    return inspect_update_zip(archive,version)
