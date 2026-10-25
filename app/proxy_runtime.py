"""Private, short-lived panel-side proxy runtime for functional checks."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from .node_formats import parse_uri,singbox_outbound

@contextmanager
def runtime(link,cancel):
    from . import runtime_budget
    with runtime_budget.slot():
        with _runtime(link,cancel) as port:yield port


@contextmanager
def _runtime(link,cancel):
    outbound=singbox_outbound(parse_uri(link));outbound['tag']='node'
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1',0));port=reserve.getsockname()[1]
    config={'log':{'level':'error'},'dns':{'servers':[{'type':'local','tag':'dns-direct'}]},
      'inbounds':[{'type':'mixed','listen':'127.0.0.1','listen_port':port}],
      'outbounds':[outbound],'route':{'final':'node','default_domain_resolver':{'server':'dns-direct','strategy':'prefer_ipv4'}}}
    with tempfile.TemporaryDirectory(prefix='node-functional-') as tmp:
        file=Path(tmp)/'config.json';file.write_text(json.dumps(config));file.chmod(0o600)
        process=subprocess.Popen(['/usr/local/bin/sing-box-probe','run','-c',str(file)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=os.environ|{'GOMAXPROCS':'1'})
        try:
            for _ in range(50):
                if cancel.is_set():raise InterruptedError()
                if process.poll() is not None:raise ValueError('检测代理启动失败')
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=.1):break
                except OSError:cancel.wait(.05)
            else:raise ValueError('检测代理启动超时')
            yield port
        finally:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=2)
                except subprocess.TimeoutExpired:process.kill();process.wait()
