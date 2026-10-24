"""Background DMI climate analyses. Phone only receives compact progress + results."""
import calendar, datetime as dt, hashlib, json, math, os, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError
ROOT=Path(__file__).resolve().parent
CACHE=ROOT/'.cache'/'analyses'
URL='https://opendataapi.dmi.dk/v2/climateData/collections/10kmGridValue/items'
lock=threading.RLock()
jobs={}
active=None
MAX_JOBS=15

def iso(d):return d.isoformat()
def date(v):return dt.date.fromisoformat(v)
def inc_months(d,num):
    y=(d.year*12+d.month-1+num)//12
    m=(d.year*12+d.month-1+num)%12+1
    return dt.date(y,m,1)
def aligned_dates(spec,year):
    a=date(spec['start'])
    if a.month==2 and a.day==29 and not calendar.isleap(year): a=a.replace(day=28)
    a=a.replace(year=year)
    if spec['mode']=='month':
        count=spec['months']
        b=inc_months(a,count)-dt.timedelta(days=1)
    else:
        b=a+dt.timedelta(days=spec['days']-1)
    return a,b
def candidates(spec):
    year=dt.date.today().year
    limit=dt.date.today()-dt.timedelta(days=14)
    selected=[]
    for y in range(year,1990,-1):
        try:a,b=aligned_dates(spec,y)
        except ValueError:continue
        if b<limit and a>=dt.date(1991,1,1):
            selected.append((y,a,b))
            if len(selected)==spec['years']:break
    return selected

def clip(poly,b):
    points=[list(pt[:2]) for pt in poly if len(pt)>=2]
    for dim,bound,keep_above in ((0,b[0],True),(0,b[2],False),(1,b[1],True),(1,b[3],False)):
        if not points:return []
        out=[]
        def inside(pt):return pt[dim]>=bound if keep_above else pt[dim]<=bound
        def cross(a,z):
            diff=z[dim]-a[dim]
            if abs(diff)<1e-12:return [bound,a[1]] if dim==0 else [a[0],bound]
            fraction=(bound-a[dim])/diff
            return [a[k]+fraction*(z[k]-a[k]) for k in range(2)]
        for index,c in enumerate(points):
            a=points[index-1]
            ai,ci=inside(a),inside(c)
            if ai and ci:out.append(c)
            elif ai and not ci:out.append(cross(a,c))
            elif not ai and ci:out.extend([cross(a,c),c])
        points=out
    return points

def area(poly):
    if len(poly)<3:return 0
    lat=sum(p[1] for p in poly)/len(poly)
    return abs(sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(poly,poly[1:]+poly[:1])))/2*max(.01,math.cos(math.radians(lat)))

def info(j,progress=None):
    with lock:
        if progress is not None:j['done']=progress
        j['modified']=time.time()

def download(year,a,z,spec,b,cancel):
    if cancel.is_set():return None
    b0=list(b)
    midx=(b[0]+b[2])/2;midy=(b[1]+b[3])/2
    west=min(b[0],midx-.1);south=min(b[1],midy-.1)
    east=max(b[2],midx+.1);north=max(b[3],midy+.1)
    bbox=','.join(f'{v:.5f}' for v in (west,south,east,north))
    # DMI interval filtering may include the next calendar day. Filter records below.
    start=(a-dt.timedelta(days=1)).isoformat()
    end=(z+dt.timedelta(days=1)).isoformat()
    query={'parameterId':'acc_precip','timeResolution':spec['mode'],
           'bbox':bbox,'datetime':start+'T00:00:00Z/'+end+'T23:59:59Z','limit':'3000'}
    expected=[]
    if spec['mode']=='month':
        expected=[inc_months(a,i).strftime('%Y-%m') for i in range(spec['months'])]
    else:
        expected=[(a+dt.timedelta(days=i)).isoformat() for i in range(spec['days'])]
    expect=set(expected)
    cells={}
    source=URL+'?'+urlencode(query)
    for page in range(20):
        if cancel.is_set():return None
        query['offset']=str(page*3000)
        link=URL+'?'+urlencode(query)
        req=Request(link,headers={'User-Agent':'RegnAtlas/3.0','Accept':'application/json'})
        try:
            with urlopen(req,timeout=35) as response:
                data=json.load(response)
        except HTTPError as err:
            raise RuntimeError('DMI HTTP '+str(err.code)) from err
        items=data.get('features',[])
        if not isinstance(items,list):raise RuntimeError('Uventet DMI-dataformat')
        for f in items:
            p=f.get('properties') or {}
            if p.get('parameterId')!='acc_precip' or p.get('timeResolution')!=spec['mode']:continue
            cid=p.get('cellId');val=p.get('value')
            if not cid or val is None:continue
            try:val=float(val)
            except (TypeError,ValueError):continue
            if not math.isfinite(val) or val<0:continue
            stamp=str(p.get('from') or '')
            key=stamp[:7] if spec['mode']=='month' else stamp[:10]
            if key not in expect:continue
            geom=f.get('geometry') or {}
            if geom.get('type')=='Polygon':ring=geom.get('coordinates',[[]])[0]
            elif geom.get('type')=='MultiPolygon':ring=geom.get('coordinates',[[[]]])[0][0]
            else:continue
            if not ring:continue
            cell=cells.get(cid)
            if cell is None:
                clipped=clip(ring,b)
                weight=area(clipped)
                center=(sum(pt[0] for pt in ring)/len(ring),sum(pt[1] for pt in ring)/len(ring))
                distance=math.hypot((center[0]-midx)*max(.4,math.cos(math.radians(midy))),center[1]-midy)
                cell={'id':cid,'w':weight,'dist':distance,'geom':geom,'days':{}}
                cells[cid]=cell
            cell['days'][key]=val
        if len(items)<3000:break
    else: raise RuntimeError('Det valgte område returnerer for mange dataposter')
    usable=[c for c in cells.values() if c['w']>1e-10]
    fallback=False
    if not usable and cells:
        nearest=min(cells.values(),key=lambda c:c['dist'])
        if nearest['dist']<.35:usable=[nearest];nearest['w']=1;fallback=True
    needed=len(expected) if spec['mode']=='month' else max(1,math.ceil(len(expected)*.90))
    usable=[c for c in usable if len(c['days'])>=needed]
    if not usable:return None
    totalweight=sum(c['w'] for c in usable)
    compact=[{'id':c['id'],'weight':c['w']/totalweight,
             'value':sum(c['days'].values()),'days':len(c['days'])} for c in usable]
    result={'year':year,'start':iso(a),'end':iso(z),
            'total':round(sum(c['value']*c['weight'] for c in compact),4),
            'cells':compact,'fallback':fallback,
            'source':source,'geometries':{c['id']:c['geom'] for c in usable}}
    return result

def run_job(j,years):
    global active
    output=[];errors=[]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures={pool.submit(download,y,a,b,j['spec'],j['bbox'],j['cancel']):y for y,a,b in years}
            for task in as_completed(futures):
                if j['cancel'].is_set():break
                year=futures[task]
                try:
                    row=task.result()
                    if row:output.append(row)
                    else:errors.append(str(year)+': ikke tilstrækkelige data')
                except Exception as exc:
                    errors.append(str(year)+': '+str(exc)[:150])
                with lock:
                    j['done']+=1
                    j['complete']=len(output)
                    j['failed']=len(errors)
                    j['modified']=time.time()
        with lock:
            if j['cancel'].is_set():j['status']='cancelled'
            elif len(output)<3:j['status']='error';j['error']='Kun '+str(len(output))+' år med brugbare data. '+('; '.join(errors[:3]) or 'Prøv et mindre område.')
            else:
                geo={}
                for row in output:geo.update(row.pop('geometries',{}))
                output.sort(key=lambda r:r['year'])
                j['result']={'rows':output,'geometries':geo,'failed':len(errors),
                             'warnings':errors[:8],'total':len(years)}
                j['status']='complete'
    except Exception as exc:
        with lock:j['status']='error';j['error']=str(exc)[:300]
    finally:
        with lock:
            j['modified']=time.time()
            if active==j['id']:active=None

def start(spec,bbox):
    global active
    if spec.get('mode') not in ('day','month') or spec.get('years') not in (5,10,20,30):
        raise ValueError('Ugyldig periode eller historiklængde.')
    a=date(spec['start']);b=date(spec['end'])
    if b<a or (b-a).days>370 or a<dt.date.today() or a>dt.date.today()+dt.timedelta(days=1100):
        raise ValueError('Vælg en fremtidig periode på højst 12 måneder.')
    if spec['mode']=='month':
        spec['months']=int(spec.get('months',0))
        if not 1<=spec['months']<=12 or a.day!=1:raise ValueError('Ugyldige kalendermåneder.')
    else:
        spec['days']=int(spec.get('days',0))
        if not 1<=spec['days']<=120:raise ValueError('Vælg højst 120 dage.')
    try:
        west,south,east,north=map(float,bbox)
        if not 7<=west<east<=17 or not 54<=south<north<=59 or east-west>2 or north-south>1.5:
            raise ValueError('Vælg et mindre område i Danmark.')
    except (TypeError,ValueError) as exc:raise ValueError('Ugyldigt område.') from exc
    years=candidates(spec)
    if len(years)<3:raise ValueError('Ingen sammenlignelige historiske år.')
    with lock:
        if active and jobs.get(active,{}).get('status')=='running':
            raise RuntimeError('En analyse kører allerede. Afbryd den først.')
        if len(jobs)>=MAX_JOBS:
            for k in sorted(jobs,key=lambda x:jobs[x]['created'])[:-MAX_JOBS//2]:
                if jobs[k]['status']!='running':jobs.pop(k,None)
        token=uuid.uuid4().hex
        j={'id':token,'created':time.time(),'modified':time.time(),
           'status':'running','spec':spec,'bbox':[west,south,east,north],
           'done':0,'total':len(years),'complete':0,'failed':0,'result':None,
           'error':None,'cancel':threading.Event()}
        jobs[token]=j
        active=token
    threading.Thread(target=run_job,args=(j,years),daemon=True).start()
    return token

def get(jobid):
    with lock:
        j=jobs.get(jobid)
        if not j:raise KeyError('Analysen findes ikke længere.')
        response={k:j.get(k) for k in ('id','status','done','total','complete','failed','error')}
        if j['status']=='complete':response['result']=j['result']
        return response

def cancel(jobid):
    with lock:
        j=jobs.get(jobid)
        if j and j['status']=='running':
            j['cancel'].set()
            return True
        return False
