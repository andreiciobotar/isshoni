"""Turn ANSI frames on stdin (separated by a line that is exactly a form feed) into a PNG contact strip and a GIF.

    tools/strip.py --out shots/NAME --size WxH [--cols N] [--frame-ms 120] [--chrome PATH]

Writes NAME.html, NAME.png (one Chrome screenshot of all frames in a grid) and NAME.gif (the frames cropped
out of that screenshot). Without Pillow only the PNG is written.
"""
import argparse,os,re,subprocess,sys
import ansi2html as a2h

CELL_W=9   # px per cell column, the pitch `make screenshot` already assumes
PAD=8      # the pre's padding, around each frame
GAP=6      # between frames, and around the grid

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--out',required=True,help='path without extension')
    ap.add_argument('--size',required=True,help='WxH of one frame, in cells')
    ap.add_argument('--cols',type=int,default=4,help='frames per grid row')
    ap.add_argument('--frame-ms',type=int,default=120)
    ap.add_argument('--chrome',default=os.environ.get('CHROME',''))
    args=ap.parse_args()
    w,h=map(int,args.size.split('x'))
    frames=[f.rstrip('\n') for f in re.split(r'^\f\n?',sys.stdin.read(),flags=re.M)]
    frames=[f for f in frames if f]
    if not frames: sys.exit('strip: no frames on stdin')
    cols=min(args.cols,len(frames)); rows=-(-len(frames)//cols)
    fw=w*CELL_W+2*PAD; fh=h*a2h.LINE_PX+2*PAD
    boxes=[(GAP+i%cols*(fw+GAP),GAP+i//cols*(fh+GAP)) for i in range(len(frames))]
    cells=''.join('<div style="position:absolute;left:%dpx;top:%dpx;width:%dpx;height:%dpx;overflow:hidden">'
                  '<pre style="%s">%s</pre></div>'%(x,y,fw,fh,a2h.PRE_STYLE,a2h.to_html(f)) for (x,y),f in zip(boxes,frames))
    width=GAP+cols*(fw+GAP); height=GAP+rows*(fh+GAP)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)),exist_ok=True)
    with open(args.out+'.html','w',encoding='utf-8') as fh_:
        fh_.write('<html><body style="margin:0;background:%s"><div style="position:relative;width:%dpx;height:%dpx">%s</div></body></html>'
                  %(a2h.BG,width,height,cells))
    if not args.chrome: sys.exit('strip: Chrome or Chromium is missing. Install one, or pass --chrome PATH.')
    subprocess.run([args.chrome,'--headless','--disable-gpu','--hide-scrollbars','--window-size=%d,%d'%(width,height),
                    '--screenshot='+os.path.abspath(args.out+'.png'),'file://'+os.path.abspath(args.out+'.html')],
                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
    try:
        from PIL import Image
    except ImportError:
        print('strip: Pillow is missing, wrote the PNG only (pip install pillow for the GIF).',file=sys.stderr)
        print(args.out+'.png'); return
    shot=Image.open(args.out+'.png').convert('RGB')
    if shot.size[1]<height: sys.exit('strip: Chrome cut the screenshot to %dx%d, wanted %dx%d'%(*shot.size,width,height))
    gif=[shot.crop((x,y,x+fw,y+fh)).convert('P',palette=Image.ADAPTIVE) for x,y in boxes]
    gif[0].save(args.out+'.gif',save_all=True,append_images=gif[1:],duration=args.frame_ms,loop=0)
    print(args.out+'.png'); print(args.out+'.gif')

if __name__=='__main__': main()
