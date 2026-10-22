#!/usr/bin/env python3
"""Forma API edition. Standard-library localhost server; no key in frontend/state/logs."""
from __future__ import annotations
import base64, copy, hashlib, ipaddress, json, mimetypes, os, re, secrets, shutil, threading, time, uuid
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, unquote

ROOT=Path(__file__).resolve().parent
RUNTIME=ROOT/'.runtime'; FILES=RUNTIME/'files'; INPUTS=RUNTIME/'inputs'
MAX_BODY=40*1024*1024
TERMINAL={'done','failed','canceled','unknown','interrupted'}
KINDS={'commerce_plan','storyboard','image','video'}
SESSION_TOKEN=secrets.token_urlsafe(24)
LOCK=threading.RLock(); BUSY=set(); JOB_KEYS={}; MEMORY_CONFIG={}
STOP=threading.Event()

class UserError(Exception):
    def __init__(self,message,code='INVALID_INPUT',status=400):
        super().__init__(message); self.code=code; self.status=status
class Ambiguous(Exception): pass

def env_config():
    data={}
    path=ROOT/'.env'
    if path.exists():
        for raw in path.read_text(encoding='utf-8').splitlines():
            line=raw.strip()
            if not line or line.startswith('#') or '=' not in line: continue
            key,value=line.split('=',1); data[key.strip()]=value.strip().strip('"').strip("'")
    data.update({k:v for k,v in os.environ.items() if k.startswith(('DASHSCOPE_','FORMA_'))})
    return data

def config():
    env=env_config()
    c={'api_key':env.get('DASHSCOPE_API_KEY',''),'host':env.get('DASHSCOPE_API_HOST','https://dashscope.aliyuncs.com').rstrip('/'),
       'text_model':env.get('FORMA_TEXT_MODEL','deepseek-v4-pro'),'image_model':env.get('FORMA_IMAGE_MODEL','wan2.6-image'),
       'video_model':env.get('FORMA_VIDEO_MODEL','wan3.0-video'),'concurrency':min(2,max(1,int(env.get('FORMA_API_CONCURRENCY','2'))))}
    c.update(MEMORY_CONFIG)
    return c

def check_host(host):
    u=urlparse(host)
    ok=(u.hostname in {'dashscope.aliyuncs.com','dashscope-intl.aliyuncs.com','dashscope-us.aliyuncs.com'} or (u.hostname or '').endswith('.maas.aliyuncs.com'))
    if not ok or u.scheme!='https' or u.port not in (None,443) or u.username or u.password or u.query or u.fragment or u.path not in ('','/'):
        raise UserError('请填写百炼控制台提供的 HTTPS API Host，不包含接口路径。','INVALID_HOST')
    return host.rstrip('/')

def public_config():
    c=config()
    return {k:v for k,v in c.items() if k!='api_key'} | {'configured':bool(c['api_key']),'token':SESSION_TOKEN,
       'provider':'阿里云百炼','image_limit':1,'video_limit':1,'local_only_inference':False}

def scrub(message):
    text=str(message)
    for key in {config().get('api_key',''),*JOB_KEYS.values()}:
        if key: text=text.replace(key,'[REDACTED]')
    return re.sub(r'sk-[A-Za-z0-9_-]{10,}','[REDACTED]',text)[:500]

class RedirectPolicy(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        safe_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)
OPENER=build_opener(RedirectPolicy)

def safe_url(url):
    u=urlparse(url); h=u.hostname or ''
    allowed=h.endswith(('.aliyuncs.com','.alicdn.com','.aliyun.com'))
    if u.scheme!='https' or not allowed or u.username or u.password or u.port not in (None,443):
        raise UserError('返回的媒体地址不在百炼资源域名范围，已停止自动下载。','MEDIA_HOST_REJECTED')
    return url

def api_call(host,key,path,payload=None,async_mode=False,timeout=180):
    headers={'Authorization':'Bearer '+key,'Content-Type':'application/json','Accept':'application/json'}
    if async_mode: headers['X-DashScope-Async']='enable'
    request=Request(check_host(host)+path,data=None if payload is None else json.dumps(payload,ensure_ascii=False).encode(),headers=headers,method='GET' if payload is None else 'POST')
    try:
        with OPENER.open(request,timeout=timeout) as res:
            raw=res.read(16*1024*1024)
        data=json.loads(raw)
        if data.get('code') and not data.get('output') and not data.get('choices'):
            raise UserError(scrub(data.get('message') or data['code']),str(data['code']),502)
        return data
    except HTTPError as e:
        try: body=json.loads(e.read(20000)); message=body.get('message') or body.get('error',{}).get('message') or str(e); code=body.get('code') or body.get('error',{}).get('code') or 'HTTP_'+str(e.code)
        except Exception: message='供应商接口返回 HTTP '+str(e.code);code='HTTP_'+str(e.code)
        if payload is not None and e.code>=500: raise Ambiguous('供应商返回 '+str(e.code)+'，提交结果不明；先核查供应商记录，不自动重发。')
        raise UserError(scrub(message),str(code),502)
    except (URLError,TimeoutError,ConnectionError,OSError,json.JSONDecodeError) as e:
        if payload is not None: raise Ambiguous('请求未获得确定响应。可能已被供应商接收，请核查后再决定是否重提。') from e
        raise UserError('查询暂时失败，将保留远端任务 ID。','POLL_NETWORK_ERROR',502) from e

def now(): return int(time.time()*1000)
def atomic_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp')
    with tmp.open('w',encoding='utf-8') as f: json.dump(data,f,ensure_ascii=False); f.flush();os.fsync(f.fileno())
    tmp.replace(path)

RUNTIME.mkdir(exist_ok=True); FILES.mkdir(exist_ok=True); INPUTS.mkdir(exist_ok=True)
try: JOBS=json.loads((RUNTIME/'jobs.json').read_text())
except FileNotFoundError: JOBS={}
except Exception:
    raise RuntimeError('任务记录无法读取。为保护原数据，服务未创建新任务库。请保留 .runtime 并检查 jobs.json。')
for job in JOBS.values():
    if job['status'] in {'submitting','running'} and not job.get('remote_id'):
        job['status']='unknown'; job['error']='服务上次退出时请求结果不明，未自动重提。请核查供应商记录。'
    elif job.get('remote_id') and job['status'] not in {'done','failed','canceled'}:
        job['status']='waiting_remote';job['stage']='恢复查询既有远端任务'

def persist(): atomic_json(RUNTIME/'jobs.json',JOBS)
persist()

def public_job(job):
    data=copy.deepcopy(job);data.pop('request',None);data.pop('fingerprint',None);data.pop('provider_urls',None)
    return data

def store_image(data):
    if not isinstance(data,str) or not re.match(r'^data:image/(jpeg|png|webp);base64,',data):
        raise UserError('图片需由网页转为 JPEG / PNG / WebP data URL。')
    try: head,encoded=data.split(',',1); raw=base64.b64decode(encoded,validate=True)
    except Exception: raise UserError('图片编码无效。')
    if len(raw)>10*1024*1024: raise UserError('输入图片超过 10 MB，请缩小后上传。')
    ext='jpg' if 'jpeg' in head else ('png' if 'png' in head else 'webp')
    name=hashlib.sha256(raw).hexdigest()+'.'+ext; path=INPUTS/name
    if not path.exists(): path.write_bytes(raw)
    return name

def image_data(name):
    path=INPUTS/name
    if not path.is_file() or path.parent!=INPUTS: raise UserError('输入素材不存在，请重新上传。')
    mime=mimetypes.guess_type(name)[0] or 'image/jpeg'
    return 'data:'+mime+';base64,'+base64.b64encode(path.read_bytes()).decode()

def result_image_data(job):
    files=job.get('result',{}).get('files',[])
    if not files: raise UserError('前置任务没有可用图片。','DEPENDENCY_NO_IMAGE')
    path=FILES/files[0]['name']; raw=path.read_bytes()
    if len(raw)>20*1024*1024: raise UserError('前置图片超过视频输入限制，请缩小后重新提交。')
    return 'data:'+files[0]['mime']+';base64,'+base64.b64encode(raw).decode()

def clean_metadata(data):
    allowed={'project','project_id','project_name','client_kind','shot_id','sku_name','scene','ratio','revision','title','source_asset_id'}
    return {k:v for k,v in data.items() if k in allowed and isinstance(v,(str,int,float,bool,type(None)))}

def submit(data):
    c=config()
    if not c['api_key']: raise UserError('请先在 API 与模型页面填写百炼 API Key。','API_KEY_MISSING',409)
    check_host(c['host'])
    kind=data.get('kind'); rid=data.get('id') or str(uuid.uuid4())
    if kind not in KINDS: raise UserError('不支持的任务类型。')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',rid): raise UserError('任务 ID 格式不正确。')
    fingerprint=hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    with LOCK:
        if rid in JOBS:
            if JOBS[rid]['fingerprint']!=fingerprint: raise UserError('同一任务 ID 已用于不同输入。','IDEMPOTENCY_CONFLICT',409)
            return public_job(JOBS[rid])
    req=copy.deepcopy(data.get('input') or {}); images=req.pop('images',[])
    if len(images)>4: raise UserError('每个图像任务最多四张参考图。')
    req['image_files']=[store_image(x) for x in images]
    if kind=='image' and not images: raise UserError('图像编辑至少需要一张参考图。')
    if kind=='video' and not images and not data.get('depends_on'): raise UserError('视频任务需要首帧或前置分镜任务。')
    if kind in {'commerce_plan','storyboard'} and not str(req.get('text','')).strip():raise UserError('请输入商品简报或故事。')
    if kind=='video':req['duration']=min(10,max(2,int(req.get('duration',3))))
    if len(str(req.get('text','')))>30000 or len(str(req.get('prompt','')))>2000:raise UserError('输入内容过长，请缩短后再提交。')
    model=c['text_model'] if kind in {'commerce_plan','storyboard'} else c[kind+'_model']
    job={'id':rid,'kind':kind,'model':model,'host':c['host'],'status':'queued','stage':'等待调度','created':now(),
         'attempt':0,'history':[],'metadata':clean_metadata(data.get('metadata',{})),'depends_on':data.get('depends_on'),
         'request':req,'fingerprint':fingerprint,'cancel_requested':False,'usage':{},'result':None}
    with LOCK:
        if job['depends_on'] and job['depends_on'] not in JOBS:raise UserError('前置任务不存在。','DEPENDENCY_MISSING')
        if rid in JOBS:
            if JOBS[rid]['fingerprint']!=fingerprint:raise UserError('同一任务 ID 已用于不同输入。','IDEMPOTENCY_CONFLICT',409)
            return public_job(JOBS[rid])
        JOBS[rid]=job;JOB_KEYS[rid]=c['api_key'];persist()
    return public_job(job)

def download_media(url,job_id,index,kind):
    safe_url(url)
    req=Request(url,headers={'User-Agent':'Forma-Local-Demo/2'})
    with OPENER.open(req,timeout=180) as res:
        mime=res.headers.get_content_type();maxbytes=(150 if kind=='video' else 25)*1024*1024
        if int(res.headers.get('Content-Length','0'))>maxbytes:raise UserError('生成媒体过大，未自动缓存。','MEDIA_TOO_LARGE')
        ext='mp4' if kind=='video' else ('jpg' if 'jpeg' in mime else ('webp' if 'webp' in mime else 'png'))
        name=f'{job_id}_{index}.{ext}'; target=FILES/name; temp=target.with_suffix('.download'); size=0
        try:
            with temp.open('wb') as f:
                while True:
                    chunk=res.read(128*1024)
                    if not chunk:break
                    size+=len(chunk)
                    if size>maxbytes:raise UserError('生成媒体超过缓存限制。','MEDIA_TOO_LARGE')
                    f.write(chunk)
            if size<20:raise UserError('供应商返回空媒体。','EMPTY_MEDIA')
            temp.replace(target)
        finally:
            if temp.exists():temp.unlink()
    return {'name':name,'url':'/api/files/'+name,'mime':mime,'bytes':size}

def validate_result(kind,data,req):
    if not isinstance(data,dict):raise UserError('模型结果不是 JSON 对象。','SCHEMA_INVALID')
    if kind=='commerce_plan':
        for k in ['headline','subtitle','visual_prompt']:
            if not isinstance(data.get(k),str) or not data[k].strip():raise UserError('商品方案缺少 '+k,'SCHEMA_INVALID')
            data[k]=data[k][:1200 if k=='visual_prompt' else 120]
        data['selling_points']=[str(x)[:100] for x in data.get('selling_points',[])][:6]
        data['warnings']=[str(x)[:200] for x in data.get('warnings',[])][:6]
    else:
        shots=data.get('shots');roles=req.get('roles',[]);ids={r['id'] for r in roles}
        if not isinstance(shots,list) or not 1<=len(shots)<=24:raise UserError('分镜数量应在 1–24 之间。','SCHEMA_INVALID')
        for i,s in enumerate(shots):
            if not isinstance(s,dict) or any(not isinstance(s.get(k),str) or not s[k].strip() for k in ['title','prompt']):raise UserError('分镜标题或画面描述不完整。','SCHEMA_INVALID')
            if not isinstance(s.get('subtitle'),str):raise UserError('分镜字幕应为字符串，无对白时允许空字符串。','SCHEMA_INVALID')
            if s.get('role') not in ids:raise UserError('分镜引用了未提供的角色 ID。','ROLE_ID_INVALID')
            s['episode']=min(12,max(1,int(s.get('episode',1))));s['duration']=min(10,max(2,int(s.get('duration',3))))
            
            for optional in ['imagePrompt','motionPrompt']:
                if optional in s:
                    if not isinstance(s[optional],str):raise UserError('分镜画面与动作描述应为字符串。','SCHEMA_INVALID')
                    s[optional]=s[optional][:1200]
            s['title']=s['title'][:80];s['prompt']=s['prompt'][:1200];s['subtitle']=s['subtitle'][:200]
    return data

def run_text(job,key):
    req=job['request'];kind=job['kind']
    if kind=='commerce_plan':
        system='你是电商视觉制作助理。只返回json对象，不添加markdown。只使用用户给定事实，禁止编造材质、认证、保温时长、品牌或功效。结构为{"headline":"短标题","subtitle":"副标题","visual_prompt":"中文场景构图提示词，不改变商品外观","selling_points":["已知卖点"],"warnings":["缺失事实或注意事项"]}。'
    else:
        system='你是短剧分镜助理。只返回json对象，不添加markdown。按用户故事拆分6至12个可拍摄镜头，最多24镜。结构为{"title":"剧名","shots":[{"episode":1,"title":"镜头名","prompt":"镜头意图","imagePrompt":"单一开始画面的构图，不把先后动作画成重复人物","motionPrompt":"谁在何处如何运动、摄影机运动、结束状态；多人和反射需明确对应","subtitle":"字幕或对白","role":"提供的角色id","duration":3}]}。只能引用给定角色id，时长2到10秒。不要凭空新增剧情或人物；角色、人数、空间位置、光线和动作落点跨镜头连续。玻璃倒影须对应同一实体人物；无法确定的关系在prompt里标注待用户确认。屏幕文字由后期添加，不要求图像模型生成可读文字。'
    user=json.dumps({'text':req.get('text',''),'roles':req.get('roles',[]),'product':req.get('product',''),'skus':req.get('skus',[])},ensure_ascii=False)
    payload={'model':job['model'],'messages':[{'role':'system','content':system},{'role':'user','content':user}],
             'stream':False,'enable_thinking':False,'max_tokens':6000,'response_format':{'type':'json_object'}}
    with LOCK:job['provider_post_count']=job.get('provider_post_count',0)+1;persist()
    raw=api_call(job['host'],key,'/compatible-mode/v1/chat/completions',payload)
    try: content=raw['choices'][0]['message']['content'];data=json.loads(content)
    except (KeyError,IndexError,TypeError,json.JSONDecodeError):raise UserError('文本接口未返回可解析的 JSON，结果已记录为失败，不会套用预置答案。','INVALID_JSON')
    with LOCK:
        job['usage']=raw.get('usage',{});job['provider_request_id']=raw.get('id') or raw.get('request_id');job['raw_text']=content[:60000]
    return {'data':validate_result(kind,data,req)}

def run_image(job,key):
    req=job['request'];ratio=req.get('ratio','1:1');size={'1:1':'1280*1280','3:4':'960*1280','9:16':'720*1280','16:9':'1280*720'}.get(ratio,'1280*1280')
    payload={'model':job['model'],'input':{'messages':[{'role':'user','content':[{'text':req.get('prompt','保持主体一致，制作清晰商业图片。')}]+[{'image':image_data(x)} for x in req['image_files']]}]},'parameters':{'size':size,'n':1,'enable_interleave':False,'prompt_extend':False,'watermark':True}}
    with LOCK:job['provider_post_count']=job.get('provider_post_count',0)+1;persist()
    raw=api_call(job['host'],key,'/api/v1/services/aigc/multimodal-generation/generation',payload)
    content=raw.get('output',{}).get('choices',[{}])[0].get('message',{}).get('content',[])
    urls=[x['image'] for x in content if isinstance(x,dict) and x.get('image')]
    if not urls:raise UserError('图像接口没有返回图片。','NO_IMAGE_OUTPUT')
    with LOCK:job['stage']='下载图片到本机';job['usage']=raw.get('usage',{});job['provider_request_id']=raw.get('request_id');job['provider_urls']=urls;persist()
    return {'files':[download_media(url,job['id']+'_'+str(job['attempt']),i,'image') for i,url in enumerate(urls)]}

def poll_video(job,key):
    start=time.monotonic();failures=0
    while time.monotonic()-start<1800 and not STOP.is_set():
        try: raw=api_call(job['host'],key,'/api/v1/tasks/'+job['remote_id'],timeout=45)
        except UserError as e:
            failures+=1
            with LOCK:job['stage']='保留任务 ID，等待再次查询';job['poll_error']=scrub(e);persist()
            STOP.wait(min(15,3+failures*2));continue
        output=raw.get('output',{});status=output.get('task_status','UNKNOWN')
        with LOCK:
            job['provider_status']=status;job['provider_request_id']=raw.get('request_id',job.get('provider_request_id'));job['usage']=raw.get('usage',job.get('usage',{}));job['last_polled']=now();persist()
        if status=='SUCCEEDED':
            url=output.get('video_url')
            if not url:raise UserError('视频任务成功但没有返回媒体地址。','NO_VIDEO_URL')
            with LOCK:job['stage']='下载视频到本机';job['provider_urls']=[url];persist()
            return {'files':[download_media(url,job['id']+'_'+str(job['attempt']),0,'video')]}
        if status in {'FAILED','CANCELED'}:raise UserError(scrub(output.get('message') or '供应商任务失败'),output.get('code') or 'REMOTE_'+status)
        with LOCK:job['stage']='远端'+{'PENDING':'排队中','RUNNING':'生成中','UNKNOWN':'状态暂不可知'}.get(status,status);persist()
        STOP.wait(4)
    raise UserError('查询暂停，远端任务 ID 已保存；可继续查询，勿重新创建。','POLL_PAUSED')

def run_video(job,key):
    if not job.get('remote_id'):
        req=job['request']
        if job.get('depends_on'):first=result_image_data(JOBS[job['depends_on']])
        else:first=image_data(req['image_files'][0])
        payload={'model':job['model'],'input':{'prompt':req.get('prompt','镜头缓慢推进，人物自然动作。'),'media':[{'type':'first_frame','url':first}]},
                 'parameters':{'resolution':'720P','ratio':req.get('ratio','16:9'),'duration':req.get('duration',3),'audio':False,'prompt_extend':False,'watermark':True}}
        with LOCK:job['provider_post_count']=job.get('provider_post_count',0)+1;persist()
        raw=api_call(job['host'],key,'/api/v1/services/aigc/video-generation/video-synthesis',payload,async_mode=True,timeout=90)
        rid=raw.get('output',{}).get('task_id')
        if not rid:raise Ambiguous('视频提交未取得任务 ID，先核查供应商记录。')
        with LOCK:job['remote_id']=rid;job['provider_request_id']=raw.get('request_id');job['status']='waiting_remote';job['stage']='已提交远端任务';persist()
    return poll_video(job,key)

def evaluation(job):
    row={k:job.get(k) for k in ['id','kind','model','host','attempt','status','created','started','finished','remote_id','provider_request_id','usage','error_code','revalidated_at']}
    row['elapsed_ms']=max(0,(job.get('finished') or now())-(job.get('started') or now()))
    row['schema_valid']=bool(job['status']=='done' and job['kind'] in {'commerce_plan','storyboard'})
    row['queue_ms']=max(0,(job.get('started') or now())-job['created'])
    row['submitted_to_finished_ms']=max(0,(job.get('finished') or now())-job['created'])
    row['provider_post_count']=job.get('provider_post_count',0)
    row['notice']='elapsed_ms为首次调度至结束含下载/中断等待；submitted_to_finished_ms含排队，不是纯推理时延。费用以账单为准。'
    with (RUNTIME/'evaluations.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')

def worker(jid):
    job=JOBS[jid]
    try:
        c=config();key=JOB_KEYS.get(jid) or c['api_key']
        if not key:raise UserError('尚未配置 API Key。','API_KEY_MISSING')
        if jid not in JOB_KEYS and c['host']!=job['host']:raise UserError('当前 API Host 与任务提交时不同，请恢复对应地域配置。','HOST_CHANGED')
        if shutil.disk_usage(RUNTIME).free<256*1024*1024:raise UserError('本机缓存空间不足，请先保留和整理已有文件。','DISK_LOW')
        with LOCK:
            if job['status']=='canceled' and not job.get('remote_id'):return
            job.setdefault('started',now());job['status']='waiting_remote' if job.get('remote_id') else 'submitting';job['stage']='查询既有远端任务' if job.get('remote_id') else '请求供应商';persist()
        result=run_text(job,key) if job['kind'] in {'commerce_plan','storyboard'} else (run_image(job,key) if job['kind']=='image' else run_video(job,key))
        with LOCK:
            job['result']=result;job['status']='canceled' if job.get('cancel_requested') else 'done';job['stage']='取消后返回的结果已缓存，未自动应用' if job.get('cancel_requested') else '结果已保存';job['finished']=now();job.pop('error',None);job.pop('error_code',None);persist();evaluation(job)
    except Ambiguous as e:
        with LOCK:job['status']='unknown';job['error']=scrub(e);job['error_code']='SUBMISSION_UNKNOWN';job['stage']='需要核查，未自动重发';job['finished']=now();persist();evaluation(job)
    except Exception as e:
        with LOCK:
            code=getattr(e,'code','LOCAL_ERROR');job['status']='interrupted' if job.get('remote_id') and code in {'POLL_PAUSED','POLL_NETWORK_ERROR','API_KEY_MISSING','HOST_CHANGED'} else ('canceled' if job.get('cancel_requested') else 'failed')
            job['error']=scrub(e);job['error_code']=code;job['stage']='保留输入与远端 ID';job['finished']=now();persist();evaluation(job)
    finally:
        with LOCK:BUSY.discard(jid)

def scheduler():
    while not STOP.wait(.4):
        with LOCK:
            c=config()
            if not c['api_key']:continue
            for job in JOBS.values():
                if len(BUSY)>=c['concurrency']:break
                if job['id'] in BUSY or job['status'] not in {'queued','waiting_remote'}:continue
                if job['kind'] in {'image','video'} and any(JOBS[x]['kind']==job['kind'] for x in BUSY):continue
                dep=JOBS.get(job.get('depends_on'))
                if dep and dep['status']!='done':job['stage']='等待前置任务：'+dep['status'];continue
                BUSY.add(job['id']);job['status']='waiting_remote' if job.get('remote_id') else 'running';persist()
                threading.Thread(target=worker,args=(job['id'],),daemon=True).start()

def job_action(jid,action,data):
    with LOCK:
        job=JOBS.get(jid)
        if not job:raise UserError('任务不存在。','NOT_FOUND',404)
        if action in {'retry','resume'}:
            current=config()
            if current['host']!=job['host']:raise UserError('请恢复该任务原始地域的配置后再操作。','HOST_CHANGED',409)
            if not current['api_key']:raise UserError('请先配置 API Key。','API_KEY_MISSING',409)
            JOB_KEYS[jid]=current['api_key']
        if action=='revalidate':
            if job['status']!='failed' or job['kind'] not in {'commerce_plan','storyboard'} or not job.get('raw_text') or jid in BUSY:raise UserError('此任务没有可重新校验的原始文本。')
            result=validate_result(job['kind'],json.loads(job['raw_text']),job['request'])
            job['history'].append({'event':'local_revalidation','previous_status':job['status'],'previous_error':job.get('error'),'at':now()})
            job['result']={'data':result};job['status']='done';job['stage']='原始响应重新校验通过，未再次调用模型';job['revalidated_at']=now();job.pop('error',None);job.pop('error_code',None);evaluation(job)
        elif action=='cancel':
            if job['status']!='done':job['cancel_requested']=True;job['status']='canceled';job['stage']='停止本地后续应用，已提交的远端任务可能继续计费'
        elif action=='resume':
            if not job.get('remote_id'):raise UserError('没有远端任务 ID，无法继续查询。')
            if jid not in BUSY:job['cancel_requested']=False;job['status']='waiting_remote';job.pop('finished',None)
        elif action=='retry':
            if jid in BUSY:raise UserError('当前请求尚未结束，请先等待确定结果。','REQUEST_ACTIVE',409)
            if job['status'] not in {'failed','unknown','interrupted','canceled'}:raise UserError('当前任务不能重试。')
            if job.get('remote_id') and job.get('provider_status') not in {'FAILED','CANCELED'}:
                raise UserError('已有远端 ID，请使用继续查询；如需重做请新建任务。','USE_RESUME',409)
            if not data.get('confirm_cost'):raise UserError('重提可能产生新的费用，请明确确认。','COST_CONFIRMATION_REQUIRED',409)
            job['history'].append({k:job.get(k) for k in ['attempt','status','remote_id','started','finished','error','usage','result']})
            job['attempt']+=1;job['status']='queued';job['cancel_requested']=False;job['stage']='等待用户确认的重试'
            for key in ['remote_id','provider_status','started','finished','error','error_code','result','usage']:job.pop(key,None)
            job['usage']={};job['result']=None
        elif action=='up':
            if job['status']!='queued':raise UserError('仅等待中的任务可重排。')
            keys=list(JOBS);i=keys.index(jid);previous=next((k for k in reversed(keys[:i]) if JOBS[k]['status']=='queued'),None)
            if previous:
                keys.remove(jid);keys.insert(keys.index(previous),jid);ordered={k:JOBS[k] for k in keys};JOBS.clear();JOBS.update(ordered)
        else:raise UserError('不支持的任务操作。')
        persist();return public_job(job)

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*a,**kw):super().__init__(*a,directory=str(ROOT),**kw)
    def log_message(self,fmt,*args):
        # No request body, authorization header, URL query or supplier URL is logged.
        pass
    def json(self,data,status=200):
        raw=json.dumps(data,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(raw)
    def path_only(self):return unquote(urlparse(self.path).path)
    def do_GET(self):
        path=self.path_only()
        try:
            if path=='/api/status':return self.json(public_config())
            if path=='/api/jobs':
                with LOCK:return self.json({'jobs':[public_job(x) for x in JOBS.values()]})
            if path.startswith('/api/jobs/'):
                with LOCK:
                    j=JOBS.get(path.split('/')[3])
                    if not j:raise UserError('任务不存在。','NOT_FOUND',404)
                    return self.json(public_job(j))
            if path=='/api/evaluations':
                f=RUNTIME/'evaluations.jsonl';rows=[json.loads(s) for s in f.read_text().splitlines()] if f.exists() else []
                return self.json({'records':rows,'terminal_event_count':len(rows),'provider_post_count':sum(j.get('provider_post_count',0) for j in JOBS.values())})
            if path.startswith('/api/files/'):
                name=path[len('/api/files/'):]
                if not re.fullmatch(r'[A-Za-z0-9_-]+\.(png|jpg|webp|mp4)',name):raise UserError('无效媒体路径。')
                file=FILES/name
                if not file.is_file():raise UserError('缓存文件不存在。','NOT_FOUND',404)
                size=file.stat().st_size;start=0;end=size-1;partial=False
                if self.headers.get('Range'):
                    m=re.fullmatch(r'bytes=(\d+)-(\d*)',self.headers['Range'])
                    if m:start=int(m[1]);end=min(end,int(m[2]) if m[2] else end);partial=True
                    if start>=size or start>end:self.send_error(416);return
                self.send_response(206 if partial else 200);self.send_header('Content-Type',mimetypes.guess_type(name)[0] or 'application/octet-stream');self.send_header('Content-Length',str(end-start+1));self.send_header('Accept-Ranges','bytes');self.send_header('Cache-Control','private, max-age=3600')
                if partial:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
                self.end_headers()
                with file.open('rb') as f:
                    f.seek(start);remaining=end-start+1
                    while remaining>0:
                        part=f.read(min(128*1024,remaining))
                        if not part:break
                        self.wfile.write(part);remaining-=len(part)
                return
            if path.startswith('/api/'):raise UserError('接口不存在。','NOT_FOUND',404)
            if any(x.startswith('.') for x in Path(path).parts) or path.endswith(('.py','.command')):raise UserError('该文件不通过网页提供。','NOT_FOUND',404)
            if path=='/':self.send_response(302);self.send_header('Location','/forma/');self.end_headers();return
            target=(ROOT/path.lstrip('/')).resolve()
            if ROOT not in target.parents and target!=ROOT:raise UserError('无效路径。')
            return super().do_GET()
        except UserError as e:self.json({'error':str(e),'code':e.code},e.status)
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception as e:self.json({'error':scrub(e),'code':'SERVER_ERROR'},500)
    def do_POST(self):
        try:
            origin=self.headers.get('Origin')
            if origin and urlparse(origin).netloc!=self.headers.get('Host'):raise UserError('请求来源不匹配。','ORIGIN_REJECTED',403)
            if self.headers.get('X-Forma-Token')!=SESSION_TOKEN:raise UserError('页面会话已更新，请刷新。','SESSION_EXPIRED',403)
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=MAX_BODY:raise UserError('请求内容过大或为空。')
            data=json.loads(self.rfile.read(length));path=self.path_only()
            if path=='/api/config':
                if data.get('reload_env'):MEMORY_CONFIG.clear()
                c={};host=data.get('host')
                if host:c['host']=check_host(host)
                for k in ['text_model','image_model','video_model']:
                    if k in data:
                        if not re.fullmatch(r'[A-Za-z0-9._-]{1,100}',data[k]):raise UserError('模型名称格式不正确。')
                        c[k]=data[k]
                if data.get('api_key'):c['api_key']=str(data['api_key']).strip()
                if data.get('clear_key'):c['api_key']=''
                if 'concurrency' in data:c['concurrency']=min(2,max(1,int(data['concurrency'])))
                MEMORY_CONFIG.update(c);return self.json(public_config())
            if path=='/api/jobs':return self.json(submit(data),202)
            m=re.fullmatch(r'/api/jobs/([A-Za-z0-9_-]+)/([a-z]+)',path)
            if m:return self.json(job_action(m[1],m[2],data))
            raise UserError('接口不存在。','NOT_FOUND',404)
        except UserError as e:self.json({'error':str(e),'code':e.code},e.status)
        except (ValueError,TypeError,json.JSONDecodeError):self.json({'error':'请求格式不正确。','code':'INVALID_JSON'},400)
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception as e:self.json({'error':scrub(e),'code':'SERVER_ERROR'},500)

if __name__=='__main__':
    port=int(env_config().get('FORMA_PORT','18767'))
    threading.Thread(target=scheduler,daemon=True).start()
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    print(f'Forma API工作台：http://127.0.0.1:{port}/forma/',flush=True)
    print('API Key仅在本机配置；关闭窗口或按Control+C停止服务。',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:STOP.set();server.server_close()
