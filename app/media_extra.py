"""Additional service evidence adapters; reachability is distinct from account/playback access."""
import json
import re
from urllib.parse import urlsplit

EXTRA={
 'apple_tv':{'name':'Apple TV','targets':['https://gspe1-ssl.ls.apple.com/pep/gcc','https://tv.apple.com/'],'hosts':('gspe1-ssl.ls.apple.com','tv.apple.com')},
 'spotify':{'name':'Spotify','targets':['https://www.spotify.com/signup/'],'hosts':('spotify.com',)},
 'youtube_video':{'name':'YouTube','targets':['https://www.youtube.com/'],'hosts':('youtube.com',)},
 'tiktok':{'name':'TikTok','targets':['https://www.tiktok.com/explore'],'hosts':('tiktok.com',)},
 'chatgpt':{'name':'ChatGPT','targets':['https://chatgpt.com/cdn-cgi/trace','https://chatgpt.com/'],'hosts':('chatgpt.com',)},
 'gemini':{'name':'Gemini','targets':['https://gemini.google.com/?hl=en'],'hosts':('gemini.google.com',)},
 'claude':{'name':'Claude','targets':['https://claude.ai/cdn-cgi/trace','https://claude.ai/'],'hosts':('claude.ai','claude.com')},
 'grok':{'name':'Grok','targets':['https://grok.com/cdn-cgi/trace','https://grok.com/'],'hosts':('grok.com',)},
}

def region_hint(platform,pages):
    text='\n'.join(p['text'] for p in pages)
    if platform in ('chatgpt','claude','grok'):
        match=re.search(r'^loc=([A-Z]{2})\s*$',text,re.M)
        if match:return match[1]
    if platform=='apple_tv' and pages:
        value=pages[0]['text'].strip()
        if re.fullmatch(r'[A-Z]{2}',value):return value
    patterns=[r'"(?:detectedCountry|countryCode|country|region|INNERTUBE_CONTEXT_GL)"\s*:\s*"([A-Z]{2})"']
    if platform=='gemini':patterns.insert(0,r',2,1,200,"([A-Z]{3})"')
    for pattern in patterns:
        match=re.search(pattern,text)
        if match:return match[1]
    return ''

def visible_text(text):
    return re.sub(r'<(?:script|style)\b[^>]*>.*?(?:</(?:script|style)>|$)','',text,flags=re.I|re.S)

def classify_extra(platform,pages,base):
    if platform not in EXTRA:return None
    text='\n'.join(p['text'] for p in pages);lower=text.lower();last=pages[-1];html=last['text'];small=html.lower()
    # Only explicit service error codes prove a regional refusal; no maintained country allowlist.
    for page in pages:
        try:document=json.loads(page['text'])
        except (ValueError,TypeError):continue
        if isinstance(document,dict):
            error=document.get('error',{})
            code=error.get('code',error.get('type','')) if isinstance(error,dict) else error
            if code in ('unsupported_country','unsupported_country_region_territory','country_not_supported','region_not_supported','geo_restricted'):
                return base|{'status':'restricted','detail':'服务接口明确返回国家或地区不支持'}
    path=urlsplit(last.get('url','')).path.lower()
    if any(p in path for p in ('unsupported-country','unsupported-region','location-not-supported')):
        return base|{'status':'restricted','detail':'服务跳转至国家或地区不支持页面'}
    if platform=='tiktok' and path.startswith('/hk/notfound'):
        return base|{'status':'restricted','region':'HK','detail':'TikTok 返回香港地区不可用页面'}
    if last['code']!=200:return None
    if platform=='apple_tv':
        if re.search(r'"(?:isCountrySupported|isSupportedInCountry)"\s*:\s*false',html):
            return base|{'status':'restricted','detail':'Apple TV 页面明确表示当前地区不受支持'}
        if re.search(r'"(?:isCountrySupported|isSupportedInCountry)"\s*:\s*true',html):
            return base|{'status':'available','detail':'Apple TV 返回地区支持信号；未验证账号、订阅或播放'}
        if ('apple tv' in small or 'apple-tv' in small) and any(s in small for s in ('<title','__ember','application/json')):
            return base|{'status':'reachable','detail':'Apple TV 入口可访问；Apple 地区不等同于 TV 订阅或片库解锁'}
    elif platform=='spotify':
        supported=re.search(r'"(?:is_country_launched|isCountryLaunched)"\s*:\s*(true|false)',html)
        if supported:
            return base|{'status':'available' if supported[1]=='true' else 'restricted','detail':'Spotify 返回地区开放信号；未验证账号或播放' if supported[1]=='true' else 'Spotify 明确返回当前地区未开放'}
        if 'spotify' in small and any(s in small for s in ('sign up','signup','__next_data__')):
            return base|{'status':'reachable','detail':'Spotify 注册入口可访问；未创建账号或验证 Premium、播放'}
    elif platform=='youtube_video':
        if base['region'] and ('ytinitialdata' in small or 'ytcfg.set' in small):
            return base|{'status':'available','detail':'YouTube 首页返回服务地区及应用数据；未验证具体视频、版权或 Premium'}
    elif platform=='tiktok':
        if base['region'] and any(s in small for s in ('__universal_data_for_rehydration__','webapp.app-context','sigi_state')):
            return base|{'status':'available','detail':'TikTok 返回网页应用数据及地区；未验证账号、上传或视频播放'}
    elif platform=='gemini':
        if 'gemini' in small and ('wiz_global_data' in small or 'sign in' in small):
            return base|{'status':'reachable','detail':'Gemini 网页入口可访问；账号、对话和付费功能未经验证'}
    elif platform=='chatgpt':
        if 'chatgpt' in small and any(s in small for s in ('__reactrouter','__next','log in','sign in')):
            return base|{'status':'reachable','detail':'ChatGPT 网页入口可访问；未登录或发送对话，不代表账号/API 权限'}
    elif platform=='claude':
        if 'claude' in small and any(s in small for s in ('__next','log in','sign in','continue with')):
            return base|{'status':'reachable','detail':'Claude 网页入口可访问；未登录或调用模型'}
    elif platform=='grok':
        if 'grok' in small and any(s in small for s in ('__next','sign in','sign up','log in')):
            return base|{'status':'reachable','detail':'Grok 网页入口可访问；账号、对话和订阅权限未经验证'}
    return None
