#!/usr/bin/env python3
"""Verify pinned official artifacts and index locally built protocol/probe binaries."""
import hashlib,json,tarfile,urllib.request,shutil
from pathlib import Path
VERSION='1.14.3'
root=Path(__file__).resolve().parents[1]/'artifacts/releases';root.mkdir(parents=True,exist_ok=True)
def fetch(url,path):
    with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'auto-argo-node-build'}),timeout=180) as src,path.open('wb') as dst:shutil.copyfileobj(src,dst)
previous=json.loads((root/'manifest.json').read_text()) if (root/'manifest.json').exists() else {'files':{}}
for version in (VERSION,'1.13.21'):
    with urllib.request.urlopen(f'https://api.github.com/repos/SagerNet/sing-box/releases/tags/v{version}',timeout=30) as r:release=json.load(r)
    for arch in ('amd64','arm64'):
        name=f'sing-box-{version}-linux-{arch}.tar.gz';asset=next(a for a in release['assets'] if a['name']==name)
        digest=asset['digest'];assert digest.startswith('sha256:') and len(digest)==71
        archive=root/(name+'.download');dest=root/f'sing-box-original-{arch}' if version==VERSION else root/f'sing-box-original-{version}-{arch}'
        known=previous.get('files',{}).get(dest.name,{}).get('sha256')
        if dest.exists() and known and hashlib.sha256(dest.read_bytes()).hexdigest()==known:continue
        fetch(asset['browser_download_url'],archive)
        assert hashlib.sha256(archive.read_bytes()).hexdigest()==digest[7:]
        with tarfile.open(archive,'r:gz') as tar:
            member=tar.getmember(f'sing-box-{version}-linux-{arch}/sing-box');assert member.isfile()
            with tar.extractfile(member) as src,dest.open('wb') as dst:shutil.copyfileobj(src,dst)
        dest.chmod(0o755);archive.unlink()
manifest={'core_version':VERSION,'core_source':'7054cac5124465ffdb145870d7c01885566711b1','files':{}}
for f in sorted(root.iterdir()):
    if f.name.startswith(('sing-box-','monitor-agent-')) and f.is_file() and not f.name.endswith(('.download','.new')):manifest['files'][f.name]={'sha256':hashlib.sha256(f.read_bytes()).hexdigest(),'size':f.stat().st_size,'version':('1.2.2+argo2' if f.name.startswith('monitor-agent-') else '1.13.21' if '-1.13.21-' in f.name else VERSION)}
manifest['locked_core_source']='628cb31ffa79cffffd34c2f9cde6cae044e4fc12'
manifest['monitor_extension']='2026-10-23.2'
(root/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print('Release manifest:',len(manifest['files']),'verified/indexed binaries')
