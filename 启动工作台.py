#!/usr/bin/env python3
from pathlib import Path
import json, os, runpy, threading, urllib.request, webbrowser
os.chdir(Path(__file__).resolve().parent)
port=os.environ.get('FORMA_PORT','18767')
config=Path('.env')
if config.exists() and 'FORMA_PORT' not in os.environ:
    for line in config.read_text().splitlines():
        if line.strip().startswith('FORMA_PORT='):port=line.split('=',1)[1].strip()
url='http://127.0.0.1:'+port+'/forma/'
try:
    with urllib.request.urlopen('http://127.0.0.1:'+port+'/api/status',timeout=1) as response:
        info=json.load(response)
    if isinstance(info,dict) and all(k in info for k in ['text_model','image_model','video_model','configured']):
        print('Forma 已在运行，打开现有工作台。')
        webbrowser.open(url)
        raise SystemExit(0)
except (OSError,ValueError):
    pass
threading.Timer(1.5,lambda:webbrowser.open(url)).start()
try:
    runpy.run_path('server.py',run_name='__main__')
except OSError:
    print('启动失败：请确认端口 '+port+' 未被其他程序占用。可在 .env 修改 FORMA_PORT 后重试。')
    raise SystemExit(1)
