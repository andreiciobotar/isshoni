"""Turn ANSI frames on stdin (separated by a line that is exactly a form feed) into a PNG contact strip, a GIF and a WebP.

    tools/strip.py --out shots/NAME --size WxH [--cols N] [--frame-ms 120] [--chrome PATH]

Writes NAME.html and NAME.png (one Chrome screenshot of all frames in a grid; NAME-1.png, NAME-2.png... when the
grid is too tall for one screenshot), and NAME.gif and NAME.webp (lossless), the frames cropped out of the
screenshots. Without Pillow only the PNG is written. tools/record.py imports render().
"""
import argparse,os,re,subprocess,sys
import ansi2html as a2h

CELL_W=9      # px per cell column, the pitch `make screenshot` already assumes
PAD=8         # the pre's padding, around each frame
GAP=6         # between frames, and around the grid
MAX_SHOT_H=8000  # px of one screenshot; a taller grid is split in several

def render(frames,out,size,cols=4,frame_ms=120,chrome=os.environ.get('CHROME','')):
    """Draw ANSI `frames` in Chrome and write out.html, out.png, out.gif and out.webp; returns the paths written."""
    if not chrome: sys.exit('strip: Chrome or Chromium is missing. Install one, or pass --chrome PATH.')
    w,h=map(int,size.split('x'))
    cols=min(cols,len(frames))
    fw=w*CELL_W+2*PAD; fh=h*a2h.LINE_PX+2*PAD
    per_shot=cols*max(1,(MAX_SHOT_H-GAP)//(fh+GAP))
    batches=[frames[i:i+per_shot] for i in range(0,len(frames),per_shot)]
    os.makedirs(os.path.dirname(os.path.abspath(out)),exist_ok=True)
    pngs=[];crops=[]  # crops: (png, boxes in it)
    for n,batch in enumerate(batches,1):
        name=out if len(batches)==1 else '%s-%d'%(out,n)
        rows=-(-len(batch)//cols)
        boxes=[(GAP+i%cols*(fw+GAP),GAP+i//cols*(fh+GAP)) for i in range(len(batch))]
        cells=''.join('<div style="position:absolute;left:%dpx;top:%dpx;width:%dpx;height:%dpx;overflow:hidden">'
                      '<pre style="%s">%s</pre></div>'%(x,y,fw,fh,a2h.PRE_STYLE,a2h.to_html(f)) for (x,y),f in zip(boxes,batch))
        width=GAP+cols*(fw+GAP); height=GAP+rows*(fh+GAP)
        with open(name+'.html','w',encoding='utf-8') as f_:
            f_.write('<html><body style="margin:0;background:%s"><div style="position:relative;width:%dpx;height:%dpx">%s</div></body></html>'
                     %(a2h.BG,width,height,cells))
        subprocess.run([chrome,'--headless','--disable-gpu','--hide-scrollbars','--window-size=%d,%d'%(width,height),
                        '--screenshot='+os.path.abspath(name+'.png'),'file://'+os.path.abspath(name+'.html')],
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
        pngs.append(name+'.png'); crops.append((name+'.png',boxes,height))
    try:
        from PIL import Image
    except ImportError:
        print('strip: Pillow is missing, wrote the PNG only (pip install pillow for the GIF and WebP).',file=sys.stderr)
        return pngs
    imgs=[]
    for png,boxes,height in crops:
        shot=Image.open(png).convert('RGB')
        if shot.size[1]<height: sys.exit('strip: Chrome cut the screenshot to %dx%d, wanted height %d'%(*shot.size,height))
        imgs+=[shot.crop((x,y,x+fw,y+fh)) for x,y in boxes]
    gif=[i.convert('P',palette=Image.ADAPTIVE) for i in imgs]
    gif[0].save(out+'.gif',save_all=True,append_images=gif[1:],duration=frame_ms,loop=0)
    imgs[0].save(out+'.webp',save_all=True,append_images=imgs[1:],duration=frame_ms,loop=0,lossless=True)
    return pngs+[out+'.gif',out+'.webp']

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--out',required=True,help='path without extension')
    ap.add_argument('--size',required=True,help='WxH of one frame, in cells')
    ap.add_argument('--cols',type=int,default=4,help='frames per grid row')
    ap.add_argument('--frame-ms',type=int,default=120)
    ap.add_argument('--chrome',default=os.environ.get('CHROME',''))
    args=ap.parse_args()
    frames=[f.rstrip('\n') for f in re.split(r'^\f\n?',sys.stdin.read(),flags=re.M)]
    frames=[f for f in frames if f]
    if not frames: sys.exit('strip: no frames on stdin')
    for p in render(frames,args.out,args.size,args.cols,args.frame_ms,args.chrome): print(p)

if __name__=='__main__': main()
