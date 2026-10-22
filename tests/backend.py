from pathlib import Path
import shutil, importlib.util, threading, base64, json, uuid
import tempfile
fixture=tempfile.TemporaryDirectory(prefix='forma-backend-check-')
root=Path(fixture.name)
shutil.copy2(Path(__file__).resolve().parents[1]/'server.py',root/'server.py')
spec=importlib.util.spec_from_file_location('forma_backend_test',root/'server.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.JOBS.clear();m.JOB_KEYS.clear();m.BUSY.clear()
m.config=lambda:{'api_key':'fixture-key-not-a-real-credential','host':'https://dashscope.aliyuncs.com','text_model':'test-text','image_model':'test-image','video_model':'test-video','concurrency':2}
# Force simultaneous same-ID requests through the I/O gap before final commit.
barrier=threading.Barrier(2);original_store=m.store_image
m.store_image=lambda x:(barrier.wait(),original_store(x))[1]
original_persist=m.persist;commits=[]
def persist():commits.append(1);original_persist()
m.persist=persist
body={'id':'unit-idempotency-0001','kind':'image','input':{'images':['data:image/png;base64,'+base64.b64encode(b'fixture image bytes').decode()],'prompt':'test'},'metadata':{}}
results=[];errors=[]
def submit():
 try:results.append(m.submit(body))
 except Exception as e:errors.append(str(e))
a=threading.Thread(target=submit);b=threading.Thread(target=submit);a.start();b.start();a.join(5);b.join(5)
assert not errors and len(results)==2 and len(m.JOBS)==1 and len(commits)==1,(errors,len(commits))
# Resuming a known remote ID only queries; it never creates another paid video task.
calls=[]
def fake_api(host,key,path,payload=None,**kw):
 calls.append({'path':path,'is_post':payload is not None})
 return {'output':{'task_status':'SUCCEEDED','video_url':'https://sample.oss-cn-beijing.aliyuncs.com/video.mp4'}}
m.api_call=fake_api;m.download_media=lambda *args,**kwargs:{'name':'fixture.mp4','url':'/api/files/fixture.mp4','mime':'video/mp4','bytes':100}
job={'id':'unit-video-resume','kind':'video','model':'test-video','host':'https://dashscope.aliyuncs.com','status':'waiting_remote','created':m.now(),'remote_id':'already-created-remote-id','request':{},'attempt':0,'usage':{}}
out=m.run_video(job,'fixture-key-not-a-real-credential')
assert calls and all(not c['is_post'] for c in calls) and out['files']
# A timed-out POST becomes unknown, with no automatic resubmission or false success.
m.store_image=original_store
request={'id':'unit-unknown-post','kind':'commerce_plan','input':{'text':'fixture'},'metadata':{}}
j=m.submit(request)
def ambiguous(*args,**kwargs):raise m.Ambiguous('fixture uncertain response')
m.api_call=ambiguous;m.BUSY.add(j['id']);m.worker(j['id'])
assert m.JOBS[j['id']]['status']=='unknown'
assert m.JOBS[j['id']]['provider_post_count']==1
assert not m.JOBS[j['id']].get('result') and j['id'] not in m.BUSY
print(json.dumps({'concurrent_same_id_one_commit':'PASS','remote_resume_no_post':'PASS','uncertain_post_not_success':'PASS','network_calls':'all mocked; no real credentials used'}))
# Silent shots are valid; a failed text response can be revalidated with no new POST.
story={'title':'fixture','shots':[{'title':'silent shot','prompt':'a room at dawn','subtitle':'','role':'role-a','duration':3,'episode':1}]}
assert m.validate_result('storyboard',story,{'roles':[{'id':'role-a'}]})['shots'][0]['subtitle']==''
j=m.JOBS['unit-unknown-post'];j.update(kind='storyboard',status='failed',raw_text=json.dumps(story),request={'roles':[{'id':'role-a'}]},history=[],error='old local validation error')
count=j['provider_post_count'];m.job_action(j['id'],'revalidate',{})
assert j['status']=='done' and j['provider_post_count']==count and j['history'][-1]['previous_error']=='old local validation error'
j['status']='canceled'
try:m.job_action(j['id'],'revalidate',{});raise AssertionError('canceled result should remain canceled')
except m.UserError:pass
assert j['status']=='canceled'
print(json.dumps({'silent_subtitle_valid':'PASS','revalidate_without_new_post':'PASS','revalidate_preserves_cancel':'PASS'}))
