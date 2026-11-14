"""Convert truecolor ANSI text to HTML. Run as a filter (stdin to stdout) for `make screenshot`; tools/strip.py imports it."""
import re,sys,html,unicodedata

LINE_PX=17
PRE_STYLE='margin:0;padding:8px;font:14px/%dpx Menlo,monospace;color:#ECEAF7;display:inline-block'%LINE_PX
BG='#12111C'

def one_cell(c):
    # characters the fallback font draws wider than one cell: braille, block elements and the scene's glyphs
    return '⠀'<=c<='⣿' or '▀'<=c<='▟' or c in '✿❀✾✽∘•◉✦✶◇◆○●■▲━┃'

def cell(c):
    # pinned to a fixed box so the line keeps one height and the next cell starts where the grid says
    if unicodedata.east_asian_width(c) in 'WF': w=2
    elif one_cell(c): w=1
    else: return html.escape(c)
    return ('<span style="display:inline-block;vertical-align:top;overflow:hidden;width:%dch;height:%dpx;'
            'line-height:%dpx;text-align:center">%s</span>')%(w,LINE_PX,LINE_PX,html.escape(c))

def to_html(data):
    """The <span> markup for one ANSI frame; colour state starts fresh."""
    out=[];st={}
    def span(t):
        if not t: return
        css=[]
        if 'fg' in st: css.append('color:rgb(%s)'%st['fg'])
        if 'bg' in st: css.append('background:rgb(%s)'%st['bg'])
        if st.get('b'): css.append('font-weight:bold')
        if st.get('f'): css.append('opacity:.6')
        if st.get('i'): css.append('font-style:italic')
        if st.get('u'): css.append('text-decoration:underline')
        if 'bg' in st:  # a background fills its whole cell row, not just the font's box: no seams between lines
            css.append('display:inline-block;vertical-align:top;height:%dpx'%LINE_PX)
        # one span per line, so a block-level span never holds a line break
        out.append('\n'.join('<span style="%s">%s</span>'%(';'.join(css),''.join(cell(c) for c in part)) if part else ''
                             for part in t.split('\n')))
    pos=0
    for m in re.finditer(r'\x1b\[([0-9;]*)m',data):
        span(data[pos:m.start()]); pos=m.end()
        p=[int(x) if x else 0 for x in m.group(1).split(';')] if m.group(1) else [0]
        i=0
        while i<len(p):
            c=p[i]
            if c==0: st.clear()
            elif c==1: st['b']=1
            elif c==2: st['f']=1
            elif c==3: st['i']=1
            elif c==4: st['u']=1
            elif c==22: st.pop('b',0);st.pop('f',0)
            elif c in(38,48) and i+4<len(p) and p[i+1]==2:
                st['fg' if c==38 else 'bg']='%d,%d,%d'%tuple(p[i+2:i+5]); i+=4
            elif c==39: st.pop('fg',0)
            elif c==49: st.pop('bg',0)
            i+=1
    span(data[pos:])
    return ''.join(out)

if __name__=='__main__':
    print('<html><body style="margin:0;background:%s"><pre style="%s">%s</pre></body></html>'%(BG,PRE_STYLE,to_html(sys.stdin.read())))
