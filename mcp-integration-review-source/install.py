#!/usr/bin/env python3
import argparse, ast, hashlib, json, os, shutil, subprocess, tempfile
from datetime import datetime, timezone
from pathlib import Path
EXPECTED='e96d546e6f0b9b748afdf3c63009941933145ff4d996deb07bd8fdebdc74faf5'
SERVER=Path('/opt/us-trading-bot/unified_server.js')
PAGE=Path('/var/www/trading-bot-dashboard/dist/bots-status.html')
BASE=Path(__file__).resolve().parent
MARKER="// LIVE_ACCOUNT_READS_V1"

def atomic(path, content):
    previous=path.stat() if path.exists() else None
    fd,tmp=tempfile.mkstemp(prefix='.account-install-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(content); f.flush(); os.fsync(f.fileno())
        os.chmod(tmp,(previous.st_mode&0o777) if previous else 0o644)
        if previous: os.chown(tmp,previous.st_uid,previous.st_gid)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    if not SERVER.is_file() or not PAGE.is_file():raise SystemExit('STOP: Expected backend/status page missing.')
    original=SERVER.read_bytes()
    if hashlib.sha256(original).hexdigest()!=EXPECTED:raise SystemExit('STOP: Backend checksum changed. No changes made.')
    node=shutil.which('node')
    if not node:raise SystemExit('STOP: Node.js unavailable.')
    source=original.decode(); anchor='// React SPA fallback: keep API routes above this middleware.'
    if source.count(anchor)!=1:raise SystemExit('STOP: API insertion anchor mismatch.')
    patched=source.replace(anchor,MARKER+"\nrequire('./account_routes')(app, buyRequireSession, buySameOrigin);\n\n"+anchor,1).encode()
    routes=(BASE/'account_routes.js').read_bytes();worker=(BASE/'account_worker.py').read_bytes();page=(BASE/'bots-status.html').read_bytes()
    ast.parse(worker)
    scripts=[patched.decode(),routes.decode(),page.decode().split('<script>',1)[1].split('</script>',1)[0]]
    for script in scripts:
        check=subprocess.run([node,'--check'],input=script,text=True,capture_output=True)
        if check.returncode:raise SystemExit('STOP: JavaScript syntax check failed: '+check.stderr)
    print('Checksum and Python/JavaScript syntax checks passed.')
    print('Install scope: backend route registration, two account modules, Bots Status page.')
    print('No trading files, credentials, session files or permissions will be changed.')
    if not args.apply:
        print('CHECK ONLY. To install: sudo python3 install.py --apply');return
    if os.geteuid()!=0:raise SystemExit('STOP: Installation requires sudo.')
    changes=[(SERVER,patched),(SERVER.parent/'account_routes.js',routes),(SERVER.parent/'account_worker.py',worker),(PAGE,page)]
    for path,_ in changes[1:3]:
        if path.exists():raise SystemExit('STOP: Account module already exists: '+str(path))
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup=Path('/var/backups/trading-dashboard-account-feed')/stamp
    backup.mkdir(parents=True,mode=0o700)
    manifest=[]
    for index,(path,content) in enumerate(changes):
        saved=backup/(str(index)+'.backup')
        existed=path.exists()
        if existed:shutil.copy2(path,saved)
        manifest.append({'target':str(path),'backup':str(saved) if existed else None,'installed_sha256':hashlib.sha256(content).hexdigest()})
    (backup/'manifest.json').write_text(json.dumps(manifest,indent=2))
    completed=[]
    try:
        for path,content in changes:
            atomic(path,content);completed.append(path)
    except Exception:
        for entry in reversed(manifest):
            path=Path(entry['target'])
            if path not in completed:continue
            if entry['backup']:shutil.copy2(entry['backup'],path)
            else:path.unlink(missing_ok=True)
        raise
    print('Installed. Backup manifest: '+str(backup/'manifest.json'))
    print('Not activated yet. Run python3 activate.py as the existing PM2 owner, NOT sudo.')
if __name__=='__main__':main()
