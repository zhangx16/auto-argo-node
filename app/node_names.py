"""Protocol suffixes are presentation metadata; preserve the user's stored base name."""
import re
from urllib.parse import urlsplit,parse_qs

SUFFIXES={'vless-ws-argo':'cf','reality':'Vless','hysteria2':'hy2'}
LABELS={'vless-ws-argo':'Argo / CF','reality':'Reality Vision','hysteria2':'Hysteria2'}


def protocol(node):
    value=node.get('protocol')
    if value in SUFFIXES:return value
    try:
        u=urlsplit(node.get('share_link',''));q=parse_qs(u.query)
        if u.scheme in ('hysteria2','hy2'):return 'hysteria2'
        if u.scheme=='vless' and q.get('security')==['reality']:return 'reality'
        if u.scheme=='vless' and q.get('type')==['ws']:return 'vless-ws-argo'
    except (ValueError,TypeError):pass
    return ''


def format_name(name,kind,limit=80):
    name=str(name or '').strip();suffix=SUFFIXES.get(kind)
    if not suffix:return name
    pattern=r'(?:[\s\-·_]+)'+re.escape(suffix)+r'$'
    while re.search(pattern,name,re.I):name=re.sub(pattern,'',name,flags=re.I).rstrip()
    base=name[:max(1,limit-len(suffix)-1)].rstrip() or '节点'
    return base+'-'+suffix
