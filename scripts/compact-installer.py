from pathlib import Path
import re,json,hashlib
root=Path(__file__).resolve().parent.parent;raw=(root/'app/vendor/singbox.sh').read_bytes()
assert hashlib.sha256(raw).hexdigest()=='86687380b45b765f8a8d79e073d5c0972c7175497b995e4099c78091a0ae1cf4'
text=raw.decode()
starts=list(re.finditer(r'(?m)^(_[A-Za-z0-9_]+|main)\(\) [({]',text))
funcs={}
for index,m in enumerate(starts):
 stop=starts[index+1].start() if index+1<len(starts) else len(text)
 line_end=text.index('\n',m.end())
 if re.search(r'}(?:\s*#.*)?$',text[m.start():line_end]):end=line_end
 else:
  closes=list(re.finditer(r'(?m)^[})]\s*$',text[m.end():stop]))
  assert closes,m.group(1)
  end=m.end()+closes[-1].end()
 funcs[m.group(1)]=((m.start(),end),text[m.start():end])
assert len(funcs)>150
funcs['_show_node_link']=(funcs['_show_node_link'][0],'_show_node_link() { :; }')
# Only operational roots used by the panel's three protocols and maintenance cron.
refs='\n'.join((root/'app'/f).read_text() for f in ['deploy.py','node-direct.sh','node-fixed.sh','node-repair.sh','node-uninstall.sh','port-update.sh','fixed_jobs.py'])
words=lambda s:set(re.findall(r'(?<![A-Za-z0-9_])_[A-Za-z0-9_]+',s))
keep=(words(refs)&funcs.keys())|{'_argo_keepalive','_cleanup_runtime_logs','_cleanup_main_temp_files'}
while True:
 new=keep|set().union(*(words(funcs[k][1])&funcs.keys() for k in keep))
 if new==keep:break
 keep=new
out=[];end=0
for name,(span,body) in sorted(funcs.items(),key=lambda kv:kv[1][0][0]):
 out.append(text[end:span[0]])
 if name in keep:out.append(body)
 end=span[1]
out.append(text[end:]);s=''.join(out)
# Exports are restricted to the retained closure, including functions used by children.
s=re.sub(r'^export -f .+$',lambda m:'export -f '+' '.join(x for x in m.group().split()[2:] if x in keep),s,flags=re.M)
s=s.rsplit('\nmain',1)[0]
s+='''\n# Panel-owned maintenance entry point; installation uses validated panel jobs.
main() {
 _detect_init_system
 case "${ARGO_ACTION:-}" in
  argo-keepalive) _argo_keepalive;;
  cleanup-logs) _cleanup_runtime_logs;;
  *) echo '请通过面板安装、编辑或卸载 Argo / Reality / Hysteria2 节点。';;
 esac
}
main
'''
# Remove dependencies for unused QR/subscription/Xray/relay convenience paths.
s=s.replace('bash jq curl wget openssl tar unzip flock','bash jq curl wget openssl tar flock').replace('bash curl jq openssl wget tar unzip ca-certificates','bash curl jq openssl wget tar ca-certificates')
s=s.replace('local optional_pkgs="procps nftables socat iproute2 cron lsof"','local optional_pkgs="procps iproute2"')
s=s.replace('DEP_STATE_VERSION="20260831-lowmem-1"','DEP_STATE_VERSION="panel-three-protocol-v1"')
# nft is optional (traffic); unsupported NAT kernels must not trigger dependency reinstall.
s=s.replace('if ! _is_podman_environment && ! command -v nft &>/dev/null; then','if false; then')
s='ARGO_ACTION=${1:-}\n'+s
s='# Generated from verified singbox-lite 5486a730555ee96730791062aefd440a7b892751.\n# Only three panel protocols and their operational dependency closure.\n'+s
s=re.sub(r'(?m)^[ \t]+$','',s)
(root/'app/node-library.sh').write_text(s)
(root/'scripts/node-library-manifest.json').write_text(json.dumps({'upstream_commit':'5486a730555ee96730791062aefd440a7b892751','upstream_sha256':hashlib.sha256(raw).hexdigest(),'retained_functions':sorted(keep),'removed_functions':sorted(funcs.keys()-keep),'original_bytes':len(raw),'compact_bytes':len(s.encode())},indent=2)+'\n')
print('Functions',len(funcs),'->',len(keep),'bytes',len(raw),'->',len(s.encode()))
