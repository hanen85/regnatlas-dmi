#!/usr/bin/env python3
"""RegnAtlas: static website plus safe DMI Climate Data API proxy."""
import hashlib,json,mimetypes,os,re,time,threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs,urlencode
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
HERE=Path(__file__).resolve().parent
CACHE=HERE/'.cache'
DMI='https://opendataapi.dmi.dk/v2/climateData/collections/'
class App(BaseHTTPRequestHandler):
 def send(self,status,body,mime='application/json; charset=utf-8'):
  self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)));self.send_header('Access-Control-Allow-Origin','same-origin');self.send_header('X-Content-Type-Options','nosniff');self.end_headers()
  try:self.wfile.write(body)
  except (BrokenPipeError,ConnectionResetError):pass
 def error(self,status,message):self.send(status,json.dumps({'error':message},ensure_ascii=False).encode())
 def do_GET(self):
  path=urlsplit(self.path)
  if path.path=='/api/dmi':return self.dmi(path.query)
  if path.path not in ('/','/index.html'):return self.error(404,'Ikke fundet')
  self.send(200,(HERE/'index.html').read_bytes(),'text/html; charset=utf-8')
 def dmi(self,query):
  q={k:v[-1] for k,v in parse_qs(query).items()}
  if set(q)-{'grid','parameterId','timeResolution','bbox','datetime','offset','limit','sortorder'}:return self.error(400,'Ukendt parameter')
  if q.get('grid') not in ('10kmGridValue','20kmGridValue') or q.get('parameterId')!='acc_precip' or q.get('timeResolution') not in ('month','day'):return self.error(400,'Ugyldig dataforespørgsel')
  if q.get('sortorder')!='from,ASC':return self.error(400,'Ugyldig sortering')
  try:
   coords=list(map(float,q.get('bbox','').split(',')))
   assert len(coords)==4 and 7<=coords[0]<coords[2]<=17 and 54<=coords[1]<coords[3]<=59
   limit=int(q.get('limit','1000'));offset=int(q.get('offset','0'))
   assert 0<limit<=10000 and 0<=offset<=500000
   assert re.fullmatch(r'\d{4}-\d\d-\d\dT00:00:00Z/\d{4}-\d\d-\d\dT23:59:59Z',q.get('datetime',''))
  except (ValueError,AssertionError):return self.error(400,'Ugyldigt område eller periode')
  remote=DMI+q['grid']+'/items?'+urlencode({k:q[k] for k in ('parameterId','timeResolution','bbox','datetime','offset','limit','sortorder')})
  key=hashlib.sha256(remote.encode()).hexdigest()
  CACHE.mkdir(parents=True,exist_ok=True)
  cache=CACHE/(key+'.json')
  if cache.exists() and time.time()-cache.stat().st_mtime<604800:return self.send(200,cache.read_bytes())
  try:
   req=Request(remote,headers={'Accept':'application/json','User-Agent':'RegnAtlas/1.0'})
   with urlopen(req,timeout=50) as resp:data=resp.read(22*1024*1024+1)
   if len(data)>22*1024*1024:return self.error(413,'DMI svar for stort')
   parsed=json.loads(data)
   if not isinstance(parsed.get('features'),list):return self.error(502,'Uventet DMI-format')
   cache.write_bytes(data)
   self.send(200,data)
  except HTTPError as e:self.error(502,'DMI returnerede HTTP '+str(e.code))
  except (URLError,TimeoutError,OSError,ValueError) as e:self.error(502,'Kunne ikke hente data fra DMI: '+str(e))
 def log_message(self,fmt,*args):print(fmt%args,flush=True)
def diagnostics():
 samples=[
  ('original',{'parameterId':'acc_precip','timeResolution':'month','bbox':'11.93085,54.99919,11.93825,55.00526','datetime':'2016-11-01T00:00:00Z/2016-11-30T23:59:59Z','sortorder':'from,ASC','limit':'5'}),
  ('no_sort',{'parameterId':'acc_precip','timeResolution':'month','bbox':'11.93085,54.99919,11.93825,55.00526','datetime':'2016-11-01T00:00:00Z/2016-11-30T23:59:59Z','limit':'5'}),
  ('large_bbox',{'parameterId':'acc_precip','timeResolution':'month','bbox':'11.8,54.9,12.1,55.2','datetime':'2016-11-01T00:00:00Z/2016-11-30T23:59:59Z','limit':'5'}),
  ('simple',{'parameterId':'acc_precip','timeResolution':'month','limit':'1'}),
 ]
 for name,query in samples:
  url=DMI+'10kmGridValue/items?'+urlencode(query)
  try:
   with urlopen(Request(url,headers={'Accept':'application/json','User-Agent':'RegnAtlas/1.0'}),timeout=15) as resp: data=resp.read(600)
   print('DMI DIAG',name,'OK',data[:250].decode(errors='replace'),flush=True)
  except HTTPError as e:print('DMI DIAG',name,'HTTP',e.code,e.read(1500).decode(errors='replace'),flush=True)
  except Exception as e:print('DMI DIAG',name,'EXC',repr(e),flush=True)

if __name__=='__main__':
 port=int(os.getenv('PORT','8765'))
 print('RegnAtlas lytter på port',port,flush=True)
 threading.Thread(target=diagnostics,daemon=True).start()
 ThreadingHTTPServer(('0.0.0.0',port),App).serve_forever()
