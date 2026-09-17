import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
import json
spec=importlib.util.spec_from_file_location('exporter',Path(__file__).resolve().parents[1] / 'export_chats.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
valid={'mapping':{'node':{'message':None}}}
class Page:
    def __init__(self, prefix=None): self.prefix=prefix; self.removed=False
    def on(self, event, handler): self.handler=handler
    def remove_listener(self, event, handler): self.removed=True
    def goto(self,*args,**kwargs):
        def emit(path, body):
            self.handler(NS(url='https://chatgpt.com'+path,status=200,request=NS(method='GET',headers={}),json=lambda:body))
        emit('/backend-api/conversation/abc/stream_status', {'status':'COMPLETE'})
        if self.prefix: emit(self.prefix+'/conversation/abc',valid)
    def wait_for_timeout(self, ms): pass
for prefix in ['/backend-api','/backend-api/f']:
    page=Page(prefix)
    assert m.capture_conversation(page,'url','abc')[0]==valid
    assert page.removed
m.NAV_TIMEOUT_MS=0
m.capture_auth=lambda page:{}
m.fetch_with_session=lambda *args:{'status':200,'body':json.dumps(valid)}
assert m.capture_conversation(Page(),'url','abc')[0]==valid
m.fetch_with_session=lambda *args:{'status':200,'body':'{"status":"COMPLETE"}'}
try: m.capture_conversation(Page(),'url','abc')
except ValueError: pass
else: raise AssertionError('Accepted status payload')
m.fetch_with_session=lambda *args:{'status':429,'body':''}
assert m.capture_conversation(Page(),'url','abc')[2]==429
print('PASS: standard and alternate paths; status payload rejected; authenticated fallback; HTTP error propagation')
