#!/usr/bin/env python3
"""Initialize private internal Monitor credentials without printing them."""
import os,secrets
from pathlib import Path
root=Path(__file__).resolve().parents[1]
data=root/'data';data.mkdir(exist_ok=True)
env=data/'monitor-service.env'
if not env.exists():
    fd=os.open(env,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:f.write('ARGO_MONITOR_SERVICE_TOKEN='+secrets.token_hex(32)+'\n')
monitor=data/'monitor';monitor.mkdir(exist_ok=True)
if os.geteuid()==0:os.chown(monitor,65534,65534)
else:raise SystemExit('Run as root to prepare the unprivileged hub data directory.')
print('Private Monitor environment and data directory ready; no public hub port required.')
