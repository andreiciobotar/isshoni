"""Record a short looping animation of each dashboard tab from the real data of this repository only.

    python3 tools/record.py [--out assets/tabs] [--scenes "dashboard hanami"] [--size 110x32] [--chrome PATH]

Frames come from `bin/isshoni-tui --snapshot ...` over a projects dir that holds only this repository's
directories, and are drawn by tools/strip.py. Nothing is rendered until every frame passed the privacy gate:
see "Privacy gate" in docs/isshoni.md. Environment: SIZE=WxH, SCENES="a b", HANAMI_AT=<duration>,
ALLOW="word word" (whole words that may appear although they are another repository's or task's name).
"""
import argparse,glob,json,os,re,shutil,subprocess,sys,tempfile,time
from datetime import datetime,timedelta

HERE=os.path.dirname(os.path.abspath(__file__))
ROOT=os.path.dirname(HERE)
sys.path.insert(0,HERE)
import strip

SIZE='110x32'
CEILING=1_200_000  # bytes per file
HANAMI_FRAMES=36
HANAMI_STEP='150ms'

# name -> (ms per frame, the argument lists of bin/isshoni-tui, one snapshot each; {replay} and {at} are Hanami's)
SCENES={
    'dashboard':(900,[['--snapshot','dashboard','--range','24h','--row',str(r)] for r in range(8)]
                     +[['--snapshot','dashboard','--range','24h','--row','1','--fold']]),
    'agents':(1200,[['--snapshot','agents','--range',r] for r in ('24h','7d','30d','all')]),
    'factory':(900,[['--snapshot','factory','--range','24h']]*4),
    'kaizen':(2000,[['--snapshot',v,'--range','7d'] for v in ('kaizen','kaizen-flow','kaizen-anatomy','kaizen-board')]),
    'hanami':(130,[['--snapshot','hanami','--replay','{replay}','--at','{at}','--frames',str(HANAMI_FRAMES),
                    '--frame-step',HANAMI_STEP]]),
}

def main_root():
    git=subprocess.check_output(['git','rev-parse','--path-format=absolute','--git-common-dir'],cwd=ROOT,text=True).strip()
    return os.path.dirname(git)

def encode(path):
    """The project directory name Claude Code gives a working directory."""
    return re.sub(r'[/.]','-',path)

def scoped_dirs(names,root):
    """The project directories of the repository at `root`: the checkout itself and its worktrees."""
    enc=encode(root)
    return sorted(n for n in names if n==enc or n.startswith(enc+'--claude-worktrees-'))

def store_refusal(factory_dir,root):
    """A message when a factory store file names a repository other than `root`, else None."""
    for path in sorted(glob.glob(os.path.join(factory_dir,'*.jsonl'))):
        with open(path,encoding='utf-8',errors='replace') as f:
            for line in f:
                try: rec=json.loads(line)
                except ValueError: continue
                if isinstance(rec,dict) and isinstance(rec.get('repo'),str) and rec['repo']!=root:
                    return 'record: the factory store holds other repositories; recording is refused.'
    return None

def deny_words(sessions,own_dirs):
    """Names of other repositories and of their tasks, from `isshoni sessions --json`, minus this repository's own."""
    own,other=set(),set()
    for s in sessions:
        (own if s.get('project_dir') in own_dirs else other).update(w for w in (s.get('repo'),s.get('task')) if w)
    return other-own

def strip_ansi(text): return re.sub(r'\x1b\[[0-9;?]*[A-Za-z]','',text)

def check_frame(frame,words,home,allow=()):
    """The first forbidden string found in an ANSI frame, else None."""
    text=strip_ansi(frame)
    for sub in (home,'/Users/','/home/'):
        if sub and sub in text: return sub
    for w in sorted(words):
        if w not in allow and re.search(r'(?<![A-Za-z0-9_-])%s(?![A-Za-z0-9_-])'%re.escape(w),text): return w
    return None

def hanami_at(events,speed=60.0,dt=0.05,lead=1.0,gap=3.0):
    """Seconds of replay time at which the last `merge` event is applied (as hanamiStep advances), or None."""
    merges=[i for i,e in enumerate(events) if e.get('kind')=='merge']
    if not merges: return None
    ts=[datetime.fromisoformat(e['ts']) for e in events]
    clock=ts[0]-timedelta(seconds=lead); ri=steps=0
    while ri<=merges[-1]:
        steps+=1
        clock+=timedelta(seconds=speed*dt)
        if ts[ri]-clock>timedelta(seconds=speed*gap): clock=ts[ri]-timedelta(seconds=speed*gap)
        while ri<len(ts) and ts[ri]<=clock: ri+=1
    return steps*dt

def save_webp(imgs,path,frame_ms):
    """Write `imgs` as one looping lossy animated WebP."""
    imgs[0].save(path,save_all=True,append_images=imgs[1:],lossless=False,quality=70,method=6,minimize_size=True,
                 duration=frame_ms,loop=0)

def split_frames(out): return [f.rstrip('\n') for f in re.split(r'^\f\n?',out,flags=re.M) if f.strip()]

def snapshot(args):
    return subprocess.run([os.path.join(ROOT,'bin','isshoni-tui')]+args,cwd=ROOT,capture_output=True,text=True,check=True).stdout

def hanami_fill(argl,projects,tmp):
    """Write the events file and fill {replay} and {at} (no --at at all when there is no merge to aim at)."""
    ev=subprocess.run([sys.executable,'-m','isshoni','events','--since','24h','--projects-dir',projects],cwd=ROOT,
                      capture_output=True,text=True,check=True).stdout
    path=os.path.join(tmp,'events.jsonl')
    with open(path,'w',encoding='utf-8') as f: f.write(ev)
    at=os.environ.get('HANAMI_AT')
    if not at:
        secs=hanami_at([json.loads(l) for l in ev.splitlines() if l.strip()])
        if secs is None: print('record: no merge event in the last 24h, hanami starts at the tool default.',file=sys.stderr)
        else: at='%.2fs'%max(0.0,secs-3)
    if not at: argl=argl[:argl.index('--at')]+argl[argl.index('--at')+2:]
    return [path if a=='{replay}' else at if a=='{at}' else a for a in argl]

def record_scene(name,projects,size,tmp):
    started=time.time(); frames=[]
    for argl in SCENES[name][1]:
        if name=='hanami': argl=hanami_fill(argl,projects,tmp)
        frames+=split_frames(snapshot(argl+['--projects-dir',projects,'--size',size]))
    return frames,time.time()-started

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--out',default='assets/tabs')
    ap.add_argument('--scenes',default=os.environ.get('SCENES',''),help='default: all')
    ap.add_argument('--size',default=os.environ.get('SIZE',SIZE))
    ap.add_argument('--chrome',default=os.environ.get('CHROME',''))
    args=ap.parse_args()
    names=args.scenes.split() or list(SCENES)
    for n in names:
        if n not in SCENES: sys.exit('record: unknown scene %s (have: %s)'%(n,' '.join(SCENES)))
    root=main_root()
    refusal=store_refusal(os.path.expanduser('~/.claude/factory'),root)
    if refusal: sys.exit(refusal)
    projects=os.path.expanduser('~/.claude/projects')
    own=scoped_dirs(os.listdir(projects),root)
    if not own: sys.exit('record: no project directory of this repository under %s.'%projects)
    sessions=json.loads(subprocess.run([os.path.join(ROOT,'bin','isshoni'),'sessions','--json'],cwd=ROOT,capture_output=True,
                                       text=True,check=True).stdout)['sessions']
    words=deny_words(sessions,set(own))
    allow=os.environ.get('ALLOW','').split()
    home=os.path.expanduser('~')
    tmp=tempfile.mkdtemp(prefix='isshoni-record-')
    try:
        scoped=os.path.join(tmp,'projects'); os.mkdir(scoped)
        for n in own: os.symlink(os.path.join(projects,n),os.path.join(scoped,n))
        scenes={}
        for n in names:
            scenes[n]=record_scene(n,scoped,args.size,tmp)
            print('record: %s: %d frames in %.1f s'%(n,len(scenes[n][0]),scenes[n][1]),file=sys.stderr)
        for n,(frames,_) in scenes.items():
            for i,f in enumerate(frames):
                hit=check_frame(f,words,home,allow)
                if hit: sys.exit('record: scene %s frame %d shows %r; nothing was written.'%(n,i,hit))
        os.makedirs(args.out,exist_ok=True)
        written=[]
        for n,(frames,_) in scenes.items():
            started=time.time(); work=os.path.join(tmp,'render-'+n)
            imgs=strip.render(frames,os.path.join(work,n),args.size,chrome=args.chrome)[1]
            if not imgs: sys.exit('record: Pillow is missing (pip install pillow).')
            dest=os.path.join(args.out,n+'.webp'); save_webp(imgs,dest,SCENES[n][0]); written.append(dest)
            print('record: %s rendered in %.1f s'%(n,time.time()-started),file=sys.stderr)
    finally:
        shutil.rmtree(tmp,ignore_errors=True)
    big=[p for p in written if os.path.getsize(p)>CEILING]
    for p in written: print('%s %d'%(p,os.path.getsize(p)))
    if big: sys.exit('record: over the %d byte ceiling: %s'%(CEILING,' '.join(big)))

if __name__=='__main__': main()
