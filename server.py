#!/usr/bin/env python3
"""RegnAtlas: static website plus safe DMI Climate Data API proxy."""
import hashlib,json,os,re,time,threading,gzip
import analysis_api
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
  compressed=('gzip' in self.headers.get('Accept-Encoding','').lower() and len(body)>4096)
  if compressed: body=gzip.compress(body,compresslevel=5)
  self.send_response(status)
  self.send_header('Content-Type',mime)
  self.send_header('Content-Length',str(len(body)))
  self.send_header('Vary','Accept-Encoding')
  if self.path.startswith('/api/analyze/') or self.path.startswith('/index.html') or self.path=='/':self.send_header('Cache-Control','no-store')
  if compressed:self.send_header('Content-Encoding','gzip')
  self.send_header('Access-Control-Allow-Origin','same-origin')
  self.send_header('X-Content-Type-Options','nosniff')
  if status==200 and self.path.startswith('/api/dmi'):self.send_header('Cache-Control','public, max-age=3600')
  self.end_headers()
  try:self.wfile.write(body)
  except (BrokenPipeError,ConnectionResetError):pass
 def error(self,status,message):self.send(status,json.dumps({'error':message},ensure_ascii=False).encode())
 def do_GET(self):
  path=urlsplit(self.path)
  if path.path=='/api/analyze/status':
   token=parse_qs(path.query).get('id',[''])[0]
   try:return self.send(200,json.dumps(analysis_api.get(token),separators=(',',':')).encode())
   except KeyError:return self.error(404,'Analysen er udløbet eller blev ikke fundet.')
  if path.path=='/api/dmi':return self.dmi(path.query)
  if path.path=='/api/stations':return self.stations(path.query)
  if path.path=='/health':return self.send(200,b'{"status":"ok"}')
  if path.path not in ('/','/index.html'):return self.error(404,'Ikke fundet')
  self.send(200,(HERE/'index.html').read_bytes(),'text/html; charset=utf-8')
 def do_POST(self):
  path=urlsplit(self.path).path
  if path not in ('/api/analyze/start','/api/analyze/cancel'):return self.error(404,'Ikke fundet')
  try:
   size=int(self.headers.get('Content-Length','0'))
   if size<1 or size>4096:return self.error(413,'Ugyldig anmodning')
   payload=json.loads(self.rfile.read(size))
   if path=='/api/analyze/start':
    job=analysis_api.start(payload.get('spec',{}),payload.get('bbox',[]))
    return self.send(200,json.dumps({'id':job}).encode())
   token=str(payload.get('id',''))
   cancelled=analysis_api.cancel(token)
   return self.send(200,json.dumps({'cancelled':cancelled}).encode())
  except (ValueError,TypeError,KeyError) as exc:return self.error(400,str(exc))
  except RuntimeError as exc:return self.error(409,str(exc))
 def dmi(self,query):
  q={k:v[-1] for k,v in parse_qs(query).items()}
  if set(q)-{'grid','parameterId','timeResolution','bbox','datetime','offset','limit','sortorder'}:return self.error(400,'Ukendt parameter')
  if q.get('grid') not in ('10kmGridValue','20kmGridValue') or q.get('parameterId')!='acc_precip' or q.get('timeResolution') not in ('month','day'):return self.error(400,'Ugyldig dataforespørgsel')
  if 'sortorder' in q and q['sortorder'] not in ('from,ASC','from,DESC'):return self.error(400,'Ugyldig sortering')
  try:
   coords=list(map(float,q.get('bbox','').split(',')))
   assert len(coords)==4 and 7<=coords[0]<coords[2]<=17 and 54<=coords[1]<coords[3]<=59
   limit=int(q.get('limit','1000'));offset=int(q.get('offset','0'))
   assert 0<limit<=10000 and 0<=offset<=500000
   assert re.fullmatch(r'\d{4}-\d\d-\d\dT00:00:00Z/\d{4}-\d\d-\d\dT23:59:59Z',q.get('datetime',''))
  except (ValueError,AssertionError):return self.error(400,'Ugyldigt område eller periode')
  # DMI's geographic grid is 10 km; tiny phone selections can contain no grid point.
  # Expand the query to a minimum of about 20 km while staying centered on the selected area.
  west,south,east,north=coords
  midlat=(north+south)/2;midlon=(east+west)/2
  minlat=.20;minlon=.20
  west=min(west,midlon-minlon/2);east=max(east,midlon+minlon/2)
  south=min(south,midlat-minlat/2);north=max(north,midlat+minlat/2)
  q['bbox']=','.join(f'{x:.5f}' for x in (west,south,east,north))
  # Avoid sorting: DMI documentation notes it is slower and not necessary here.
  remote=DMI+q['grid']+'/items?'+urlencode({k:q[k] for k in ('parameterId','timeResolution','bbox','datetime','offset','limit')})
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
   # Keep the geometry, cell metadata and values used by the frontend.
   # Removing duplicate, unused DMI properties makes 30-year analysis much lighter on phones.
   allowed={'parameterId','timeResolution','cellId','from','value','qcStatus'}
   compact={'type':parsed.get('type','FeatureCollection'),'links':parsed.get('links',[]),
            'numberReturned':parsed.get('numberReturned',len(parsed['features'])),'features':[]}
   for f in parsed['features']:
    properties=f.get('properties') or {}
    compact['features'].append({'type':'Feature','geometry':f.get('geometry'),
        'properties':{k:v for k,v in properties.items() if k in allowed}})
   data=json.dumps(compact,separators=(',',':'),ensure_ascii=False).encode()
   cache.write_bytes(data)
   self.send(200,data)
  except HTTPError as e:
   detail=e.read(1000).decode('utf-8',errors='replace').replace('\\n',' ')
   print('DMI API ERROR',e.code,remote,detail[:650],flush=True)
   self.error(502,'DMI returnerede HTTP '+str(e.code)+': '+detail[:300])
  except (URLError,TimeoutError,OSError,ValueError) as e:self.error(502,'Kunne ikke hente data fra DMI: '+str(e))
 def stations(self,query):
  q={k:v[-1] for k,v in parse_qs(query).items()}
  if set(q)!={'bbox'}:return self.error(400,'Ugyldig stationsforespørgsel')
  try:
   bounds=list(map(float,q['bbox'].split(',')))
   assert len(bounds)==4
   w,s,e,n=bounds
   assert 7<=w<e<=17 and 54<=s<n<=59 and e-w<=3.1 and n-s<=2.1
  except (ValueError,AssertionError):return self.error(400,'Ugyldigt kortområde')
  # Stations in an expanded vicinity are informational, not proven interpolation inputs.
  centerx=(w+e)/2;centery=(s+n)/2
  w=max(7,min(w,centerx-.35));e=min(17,max(e,centerx+.35))
  s=max(54,min(s,centery-.28));n=min(59,max(n,centery+.28))
  url=DMI+'station/items?'+urlencode({'bbox':','.join(f'{v:.5f}' for v in (w,s,e,n)),'limit':'1000'})
  key=hashlib.sha256(url.encode()).hexdigest()
  CACHE.mkdir(parents=True,exist_ok=True)
  file=CACHE/('station-'+key+'.json')
  if file.exists() and time.time()-file.stat().st_mtime<604800:return self.send(200,file.read_bytes())
  try:
   with urlopen(Request(url,headers={'Accept':'application/json','User-Agent':'RegnAtlas/2.0'}),timeout=25) as resp:
    data=resp.read(5*1024*1024+1)
   if len(data)>5*1024*1024:return self.error(502,'Stationstjenestens svar er for stort')
   result=json.loads(data)
   if not isinstance(result.get('features'),list):return self.error(502,'Uventet svar fra stationstjenesten')
   file.write_bytes(data)
   self.send(200,data)
  except HTTPError as exc:self.error(502,'DMI stationer HTTP '+str(exc.code))
  except (URLError,TimeoutError,OSError,ValueError) as exc:self.error(502,'DMI stationer: '+str(exc))
 def log_message(self,fmt,*args):print(fmt%args,flush=True)
def smoke_test():
 try:
  station_url=DMI+'station/items?'+urlencode({'bbox':'11.5,54.8,12.3,55.4','limit':'200'})
  with urlopen(Request(station_url,headers={'User-Agent':'RegnAtlas/2.0'}),timeout=25) as resp: stations=json.loads(resp.read(3000000))
  fs=stations.get('features',[])
  climate=[f for f in fs if 'acc_precip' in (f.get('properties',{}).get('parameterId') or [])]
  print('DMI_SMOKE stations',len(fs),'rain_stations',len(climate),'example',json.dumps((climate or fs or [{}])[0].get('properties',{}).get('name')),flush=True)
 except Exception as exc:print('DMI_SMOKE_ERROR stations',str(exc),flush=True)
 # Verify the new server-side area-weighted analyzer with actual DMI observations.
 for mode in ('day','month'):
  try:
   import datetime as dt
   a=dt.date(2024,10,1)
   b=dt.date(2024,10,7) if mode=='day' else dt.date(2024,10,31)
   spec={'mode':mode,'days':7,'months':1}
   row=analysis_api.download(2024,a,b,spec,[11.93,54.99,11.98,55.04],threading.Event())
   print('REGNATLAS_BACKEND_SMOKE',mode,
         'usable',bool(row),'mm',row.get('total') if row else None,
         'cells',len(row['cells']) if row else 0,flush=True)
  except Exception as exc:print('REGNATLAS_BACKEND_SMOKE_ERROR',mode,repr(exc),flush=True)
 for mode in ('day','month'):
  params={'parameterId':'acc_precip','timeResolution':mode,'bbox':'11.8,54.9,12.1,55.2','datetime':'2024-10-01T00:00:00Z/2024-10-04T23:59:59Z' if mode=='day' else '2024-10-01T00:00:00Z/2024-10-31T23:59:59Z','limit':'15'}
  try:
   url=DMI+'10kmGridValue/items?'+urlencode(params)
   with urlopen(Request(url,headers={'User-Agent':'RegnAtlas/2.0','Accept':'application/json'}),timeout=25) as resp:
    result=json.loads(resp.read(500000))
   features=result.get('features',[])
   props=features[0].get('properties',{}) if features else {}
   print('DMI_SMOKE',mode,'features',len(features),'sample',json.dumps({'from':props.get('from'),'cellId':props.get('cellId'),'value':props.get('value'),'timeResolution':props.get('timeResolution')}),flush=True)
  except Exception as exc:print('DMI_SMOKE_ERROR',mode,str(exc),flush=True)

if __name__=='__main__':
 port=int(os.getenv('PORT','8765'))
 print('RegnAtlas lytter på port',port,flush=True)
 threading.Thread(target=smoke_test,daemon=True).start()
 ThreadingHTTPServer(('0.0.0.0',port),App).serve_forever()
