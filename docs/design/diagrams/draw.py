"""Hand-authored SVG layouts. No Mermaid, Graphviz or automatic graph layout.
Coordinates and routes are hand-authored; full source labels are retained in edge titles.
Run from any directory: python -X utf8 docs/design/diagrams/draw.py
"""
from pathlib import Path
import re
from html import escape as esc

OUT = Path(__file__).resolve().parent
IN = OUT
PALETTE = {
    'blue': ('#2162B0', '#EBF3FF'), 'orange': ('#A9570D', '#FFF0DE'),
    'yellow': ('#896609', '#FFF7CF'), 'purple': ('#7043B5', '#F3EDFF'),
    'green': ('#17775B', '#E5F6ED'), 'slate': ('#435367', '#EAF0F5'),
    'red': ('#B63F4B', '#FDECEF'), 'teal': ('#137B85', '#E5F5F6'),
}
N = {}
def n(key, title, color='blue', detail='', english=None):
    N[key] = (title, key if english is None else english, color, detail)

for key,title,color in [
 ('intake','待受理','blue'),('waiting_dependency','等待依赖','yellow'),
 ('ready','就绪','blue'),('dispatching','派发中','orange'),('in_progress','执行中','orange'),
 ('waiting_user','等待用户','yellow'),('paused','暂停','yellow'),('verifying','主控核验','purple'),
 ('done','完成·待用户验收','green'),('accepted','已验收','green'),
 ('cancelled','已取消','red'),('archived','已归档','slate'),
 ('executing','执行','orange'),('reviewing','评审中','purple'),
 ('triaging','逐条处置','purple'),('revising','迭代修改','orange')]: n(key,title,color)
n('[*]','起点 / 终点','slate',english='')

def edges(name):
    result=[]
    source=(IN/(name+'.mmd')).read_text(encoding='utf-8')
    for line in source.splitlines():
        s=line.strip()
        if source.startswith('stateDiagram'):
            m=re.match(r'(\[\*\]|\w+) --> (\[\*\]|\w+)(?:: (.*))?$',s)
            if m: result.append((m[1],m[2],m[3] or '',False,False))
        elif source.startswith('sequenceDiagram'):
            m=re.match(r'(\w+)(-->>|->>)(\w+): (.*)',s)
            if m: result.append((m[1],m[3],m[4],m[2]=='-->>',False))
        elif re.search(r' --> | --- | \.-> ',s):
            src=re.match(r'\w+',s)[0]
            if ' -- ' in s:
                left,rest=s.split(' -- ',1); label,right=rest.split(' --> ',1); dashed=False; undirected=False
            elif ' -. ' in s:
                left,rest=s.split(' -. ',1); label,right=rest.split(' .-> ',1); dashed=True; undirected=False
            else:
                undirected=' --- ' in s
                left,right=s.split(' --- ' if undirected else ' --> ',1); label=''; dashed=False
            dst=re.match(r'\w+',right)[0]
            result.append((src,dst,label,dashed,undirected))
    return result

def wrap(text, width, size=15):
    # Deliberately conservative metrics for Segoe UI / Microsoft YaHei fallback.
    lines=[]
    for paragraph in str(text).split('\n'):
        cur=''; total=0
        for token in re.findall(r'[A-Za-z0-9_./+<>=-]+|[^A-Za-z0-9_./+<>=-]',paragraph):
            weight=sum(size*(1.03 if ord(c)>255 else .60) for c in token)
            if total+weight>width and cur and token not in '）)]}，。！？；：、?':
                lines.append(cur.rstrip());cur='';total=0
            cur+=token;total+=weight
        lines.append(cur.rstrip())
    return lines

class SVG:
    def __init__(self,name,title,subtitle,height):
        self.name=name; self.title=title; self.height=height; self.parts=[]; self.nodes={}; self.used=[]; self.es=edges(name)
        self.rect(0,0,1200,height,'#F6F8FC',rx=0)
        self.rect(0,0,1200,9,'url(#'+name+'-header)',rx=0)
        self.text(44,42,'TASKSTEWARD  /  PRODUCT DESIGN',14,'#53657E',weight=600)
        self.text(44,87,title,32,'#152C46',weight=700)
        self.text(44,118,subtitle,16,'#53657E')
        self.legend(153)
    def rect(self,x,y,w,h,fill,stroke='none',rx=14,extra=''):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" {extra}/>')
    def text(self,x,y,t,size=15,color='#233C54',anchor='start',weight=400):
        self.parts.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" text-anchor="{anchor}" font-weight="{weight}">{esc(str(t))}</text>')
    def lines(self,x,y,t,width,size=15,color='#233C54',anchor='start',weight=400,leading=None):
        ls=wrap(t,width,size)
        for i,l in enumerate(ls): self.text(x,y+i*(leading or size*1.45),l,size,color,anchor,weight)
        return len(ls)*(leading or size*1.45)
    def legend(self,y):
        for x,key,label in [(44,'blue','规划 / 排队'),(200,'orange','派发 / 执行'),(360,'yellow','等待'),(471,'purple','评审'),(580,'green','完成 / 验收'),(740,'slate','终态 / 归档'),(900,'red','失败 / 取消')]:
            c,b=PALETTE[key]; self.rect(x,y-12,11,11,c,rx=3);self.text(x+19,y-1,label,14)
        self.path([(44,y+24),(96,y+24)],'blue',True)
        self.text(110,y+29,'正常流程 / 加粗实线',14)
        self.path([(345,y+24),(397,y+24)],'slate',False,True)
        self.text(411,y+29,'异常、恢复或返回 / 虚线',14)
        self.rect(748,y+16,11,11,PALETTE['teal'][0],rx=3)
        self.text(768,y+29,'数据 / 通知',14)
        self.path([(944,y+24),(984,y+24)],'slate',end=False)
        self.text(997,y+29,'无向关联',14)
    def section(self,y,title,note=''):
        self.text(44,y,title,21,'#152C46',weight=700)
        if note:self.text(44,y+27,note,15,'#53657E')
    def panel(self,x,y,w,h,title,color='blue',note=''):
        c,b=PALETTE[color];self.rect(x,y,w,h,b,c,18,extra='stroke-opacity=".28"')
        self.text(x+20,y+32,title,20,c,weight=700)
        if note:self.text(x+20,y+56,note,14,c)
    def node(self,key,x,y,w=270,h=78,title=None,detail=None,english=None,color=None,ref=False):
        tt,en,co,de=N[key];title=tt if title is None else title;detail=de if detail is None else detail;english=en if english is None else english;color=co if color is None else color
        c,b=PALETTE[color]
        box=f' data-box="{x},{y},{w},{h}"' if self.name=='task-state-machine' else ''
        self.parts.append(f'<g data-node="{esc(key)}"{box}>')
        self.rect(x,y,w,h,b,c,13,extra=f'filter="url(#{self.name}-shadow)" stroke-opacity=".40"')
        self.rect(x,y+13,4,h-26,c,rx=2)
        title_size=16 if w<300 else 18
        title_lines=wrap(title,w-30,title_size)
        for i,line in enumerate(title_lines):self.text(x+15,y+26+i*22,line,title_size,c,weight=650)
        cursor=y+49+(len(title_lines)-1)*22
        if english:
            cursor+=self.lines(x+15,cursor,english,w-30,14,c)-20.3
        if detail:self.lines(x+15,cursor+22,detail,w-30,14)
        self.parts.append('</g>')
        if not ref:self.nodes[key]=(x,y,w,h)
    def anchor(self,key,side='b',offset=0):
        x,y,w,h=self.nodes[key]
        return {'t':(x+w/2+offset,y),'b':(x+w/2+offset,y+h),'l':(x,y+h/2+offset),'r':(x+w,y+h/2+offset)}[side]
    def path(self,points,color='blue',main=False,dashed=False,end=True):
        c=PALETTE[color][0]
        d='M '+' L '.join(f'{x},{y}' for x,y in points)
        self.parts.append(f'<path d="{d}" fill="none" stroke="{c}" stroke-width="{3.2 if main else 1.7}" stroke-linecap="round" stroke-linejoin="round"'+(' stroke-dasharray="6 5"' if dashed else '')+(f' marker-end="url(#{self.name}-arrow-{color})"' if end else '')+'/>')
    def label(self,x,y,text,width=330,color='blue',size=15,anchor='middle'):
        if not text:return
        ls=wrap(text,width,size);h=len(ls)*(size*1.45)
        left=x-width/2 if anchor=='middle' else x-7
        self.rect(left,y-size-3,width,h+7,'#FFFFFF',rx=6)
        self.lines(x,y,text,width-14,size,PALETTE[color][0],anchor,600)
    def edge(self,index,points,label=None,at=None,width=330,color='blue',main=False,dashed=None):
        src,dst,raw,ds,ud=self.es[index];self.used.append(index)
        self.parts.append(f'<g data-edge="{index}" data-source="{esc(src)}" data-target="{esc(dst)}"><title>{esc(src+" → "+dst+": "+raw)}</title>')
        self.path(points,color,main,ds if dashed is None else dashed,not ud)
        if at:self.label(*at,raw if label is None else label,width,color)
        self.parts.append('</g>')
    def link(self,index,a='b',b='t',**kw):
        src,dst,*_=self.es[index];self.edge(index,[self.anchor(src,a),self.anchor(dst,b)],**kw)
    def dot(self,x,y,final=False):
        if final:self.parts.append(f'<circle cx="{x}" cy="{y}" r="9" fill="white" stroke="#435367" stroke-width="2"/>')
        self.parts.append(f'<circle cx="{x}" cy="{y}" r="5" fill="#435367"/>')
    def save(self):
        assert sorted(self.used)==list(range(len(self.es))),(self.name,'missing/duplicate',set(range(len(self.es)))-set(self.used),self.used)
        self.text(44,self.height-23,'SOURCE  '+self.name+'.mmd   ·   '+('每个状态仅一个节点；圆点表示汇流，交叉但无圆点的线不相连。' if self.name=='task-state-machine' else '每个节点仅绘制一次；圆点表示汇流，交叉但无圆点的线不相连。' if self.name in {'plugin-architecture','input-routing','review-loop','storage-and-context'} else '同名卡片表示同一节点；箭头方向与源图一致。' if self.name in {'architecture','end-to-end-sequence'} else '箭头方向与源图一致。'),13,'#64758A')
        defs=[f'<linearGradient id="{self.name}-header"><stop stop-color="#2162B0"/><stop offset=".38" stop-color="#7043B5"/><stop offset=".7" stop-color="#D79B2F"/><stop offset="1" stop-color="#17775B"/></linearGradient>',
              f'<filter id="{self.name}-shadow" x="-10%" y="-20%" width="120%" height="150%"><feDropShadow dx="0" dy="3" stdDeviation="3" flood-color="#19334F" flood-opacity=".08"/></filter>']
        for key,(c,b) in PALETTE.items():defs.append(f'<marker id="{self.name}-arrow-{key}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse" markerUnits="userSpaceOnUse"><path d="M 0 0 L 10 5 L 0 10 z" fill="{c}"/></marker>')
        svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="{self.height}" viewBox="0 0 1200 {self.height}" role="img" aria-labelledby="{self.name}-title" font-family="Segoe UI, Microsoft YaHei, sans-serif"><title id="{self.name}-title">{esc(self.title)}</title><defs>'+''.join(defs)+'</defs>'+''.join(self.parts)+'</svg>'
        (OUT/(self.name+'.svg')).write_text(svg,encoding='utf-8',newline='\n')

class StateSVG(SVG):
    """Explicit hand-routed edges; bus branches retain individual source evidence."""
    def __init__(self, *args):
        super().__init__(*args)
        self.labels=[]

    def label(self,x,y,text,width=200,color='blue',size=13,anchor='middle'):
        if not text:return
        ls=wrap(text,width-12,size)
        h=len(ls)*19
        left=x-width/2 if anchor=='middle' else x-6
        self.labels.append(f'<g data-label-box="{left},{y-15},{width},{h+5}">')
        self.labels.append(f'<rect x="{left}" y="{y-15}" width="{width}" height="{h+5}" rx="4" fill="#F6F8FC"/>')
        for i,line in enumerate(ls):
            self.labels.append(f'<text x="{x}" y="{y+i*19}" font-size="{size}" fill="{PALETTE[color][0]}" text-anchor="{anchor}" font-weight="600">{esc(line)}</text>')
        self.labels.append('</g>')

    def transition(self,code,points,label,at,width=200,color='slate',main=False):
        found=[i for i,e in enumerate(self.es) if e[2].split(' ')[0]==code]
        assert len(found)==1,(code,found)
        i=found[0];a,b,raw,*_=self.es[i];self.used.append(i)
        self.parts.append(f'<g data-edge="{i}" data-source="{a}" data-target="{b}" data-label-ref="{self.name}-label-{i}"><title>{esc(raw)}</title>')
        self.path(points,color,main,not main)
        self.parts.append('</g>')
        self.labels.append(f'<g id="{self.name}-label-{i}">')
        self.label(*at,label,width,color)
        self.labels.append('</g>')

    def bus(self,code,branches,trunk,label,at,width,color):
        busid=self.name+'-bus-'+code
        self.parts.append(f'<g id="{busid}" data-bus="{code}">')
        self.path(trunk,color,dashed=True)
        self.parts.append('</g>')
        self.labels.append(f'<g id="{busid}-label">')
        self.label(*at,label,width,color)
        self.labels.append('</g>')
        indices=[i for i,e in enumerate(self.es) if e[2].split(' ')[0]==code]
        assert set(branches)=={self.es[i][0] for i in indices}
        for i in indices:
            a,b,raw,*_=self.es[i];points=branches[a];self.used.append(i)
            self.parts.append(f'<g data-edge="{i}" data-source="{a}" data-target="{b}" data-bus-ref="{busid}" data-label-ref="{busid}-label"><title>{esc(a+" → "+b+": "+raw)}</title>')
            self.path(points,color,dashed=True,end=False)
            x,y=points[-1]
            self.parts.append(f'<circle cx="{x}" cy="{y}" r="3" fill="{PALETTE[color][0]}"/>')
            self.parts.append('</g>')

    def save(self):
        self.parts.extend(self.labels)
        super().save()


def task():
    d=StateSVG('task-state-machine','任务状态机','主控全景  ·  纵向主路径 / 左侧等待与恢复（历史导入映射见 DN002 §3.4）',3170)
    for k,y in [('intake',390),('ready',690),('dispatching',900),('verifying',2250),('done',2500),('accepted',2780),('archived',2990)]:
        d.node(k,490,y,280,76)
    for k,y in [('waiting_dependency',670),('waiting_user',1340),('paused',2070),('cancelled',2780)]:
        d.node(k,90,y,220,80)
    d.parts.append('<g data-node="in_progress" data-box="420,1120,440,870">')
    d.panel(420,1120,440,870,'执行中 / in_progress','orange','worker 内部（主控不介入）')
    d.parts.append('</g>');d.nodes['in_progress']=(420,1120,440,870)
    for k,y in [('executing',1240),('reviewing',1410),('triaging',1580),('revising',1770)]:d.node(k,520,y,220,70)
    d.text(440,1940,'finalize 且终版已评审 → T13 提交',14,PALETTE['orange'][0])
    d.dot(630,300)
    d.transition('T01',[(630,305),(630,390)],'T01 登记任务',(704,353),145,'blue',True)
    main=[('T04',466,690,'规划完成·依赖满足'),('T07',766,900,'有名额·资源·租约（窗口外改为提案）'),('T08',976,1120,'子会话绑定 / 续用'),('T13',1990,2250,'提交·终版已评审'),('T18',2326,2500,'核验通过·主控验收'),('T22',2576,2780,'用户验收'),('T24',2856,2990,'满7天无变化')]
    for code,y1,y2,label in main:
        d.transition(code,[(630,y1),(630,y2)],code+' '+label,(747,2895 if code=='T24' else 810 if code=='T07' else (y1+y2)/2),214,'green' if code in {'T18','T22','T24'} else 'blue',True)
    d.transition('T02',[(540,390),(540,353),(490,353),(490,417)],'T02 澄清 / 答复',(410,338),172)
    d.transition('T03',[(490,440),(200,440),(200,670)],'T03 规划后依赖未满足',(320,539),205,'yellow',True)
    d.transition('T05',[(310,704),(490,704)],'T05 依赖齐备',(400,679),164,'blue',True)
    d.transition('T06',[(490,732),(310,732)],'T06 依赖失效',(201,808),210)
    d.transition('T09',[(770,935),(885,935),(885,720),(770,720)],'T09 未创建 / 提案过期',(754,870),238)
    d.transition('T10',[(490,936),(325,936),(325,2040),(200,2040),(200,2070)],'T10 现场冲突',(208,993),172)
    d.transition('T11',[(420,1362),(310,1362)],'T11 主会话判断后交给人类',(194,1290),230,'yellow')
    d.transition('T12',[(310,1403),(420,1403)],'T12 答复已记录',(194,1460),230)
    d.transition('T14',[(420,1900),(245,1900),(245,2070)],'T14 外部等待 / 失败·已停',(194,1870),230)
    d.transition('T15',[(420,1185),(395,1185),(395,745),(490,745)],'T15 会话丢失·已停',(194,1140),230)
    d.transition('T17',[(770,2272),(895,2272),(895,1955),(860,1955)],'T17 剩余工作 / 冲突 / 变更',(762,2220),230)
    d.transition('T19',[(490,2288),(200,2288),(200,2150)],'T19 集成 / 验证受阻',(202,2355),215)
    d.transition('T20',[(310,2094),(385,2094),(385,1960),(420,1960)],'T20 原会话恢复\n且有名额',(135,1992),180)
    d.transition('T21',[(310,2132),(365,2132),(365,718),(490,718)],'T21 恢复\n重新派发',(278,2205),114)
    d.transition('T27',[(285,2150),(285,2170),(465,2170),(465,2268),(490,2268)],'T27 恢复核验\n提交证据仍有效',(500,2127),210)
    d.transition('T23',[(770,2560),(915,2560),(915,450),(770,450)],'T23 退回 / 变更·revision+1',(770,628),250)
    d.transition('T26',[(200,2860),(200,3028),(490,3028)],'T26 退役',(314,3012),126,'slate',True)
    d.bus('T16',{
        'dispatching':[(490,952),(350,952)],'in_progress':[(420,1210),(350,1210)],
        'waiting_user':[(310,1382),(350,1382)],'verifying':[(490,2297),(350,2297)]},
        [(350,2297),(350,725),(310,725)],'T16 依赖失效·worker 已停',(195,1650),210,'yellow')
    d.bus('T25',{
        'intake':[(490,450),(40,450)],'waiting_dependency':[(90,710),(40,710)],
        'ready':[(490,751),(40,751)],'dispatching':[(490,960),(40,960)],
        'in_progress':[(420,1167),(40,1167)],'waiting_user':[(90,1400),(40,1400)],
        'paused':[(90,2120),(40,2120)],'verifying':[(490,2312),(40,2312)],'done':[(490,2540),(40,2540)]},
        [(40,450),(40,2820),(90,2820)],'T25 用户删除 / 关闭',(205,2657),224,'red')
    d.text(63,2593,'9 个来源汇入取消总线',13,PALETTE['red'][0])
    d.dot(630,1203)
    init=next(i for i,e in enumerate(d.es) if e[:3]==('[*]','executing',''))
    d.edge(init,[(630,1208),(630,1240)],color='orange',main=True)
    d.transition('E01',[(630,1310),(630,1410)],'E01 成果就绪·启动评审',(736,1351),178,'purple',True)
    d.transition('E02',[(520,1452),(500,1452),(500,1502),(554,1502),(554,1480)],'E02 失败重试',(550,1528),140,'purple')
    d.transition('E03',[(630,1480),(630,1580)],'E03 结构化意见',(719,1544),126,'purple',True)
    d.transition('E04',[(630,1650),(630,1770)],'E04 修复阻断项\n本周期未超轮次',(718,1692),128,'orange',True)
    d.transition('E05',[(740,1800),(790,1800),(790,1460),(740,1460)],'E05 新 HEAD\n增量评审',(676,1870),176,'purple')
    d.transition('E06',[(520,1615),(480,1615),(480,1275),(520,1275)],'E06\n自查遗漏',(464,1665),75,'orange')
    d.bus('E07',{'executing':[(740,1290),(810,1290),(810,1260)],'reviewing':[(740,1430),(835,1430)],'triaging':[(740,1600),(835,1600)],'revising':[(740,1820),(835,1820)]},
        [(835,1820),(835,1260),(740,1260)],'E07 需求变更·中止评审',(744,1220),188,'orange')
    d.dot(630,3110,True)
    final=next(i for i,e in enumerate(d.es) if e[:2]==('archived','[*]'))
    d.edge(final,[(630,3066),(630,3101)],color='slate',main=True)
    d.save()


def review():
    d=FlowSVG('review-loop','worker 自评审闭环',
              'Review loop  ·  评审异常汇入主会话判断；迭代与终版补审均受本周期预算约束',3120)
    for key,y,h,color in [('A',260,85,'orange'),('B',420,95,'purple'),
            ('C',610,130,'purple'),('D',900,100,'purple'),('E',1090,120,'purple'),
            ('F',1300,100,'purple'),('G',2130,100,'green'),
            ('L',2330,90,'green'),('M',2710,100,'green'),('S',2940,85,'purple')]:
        d.card(key,390,y,380,h,color)
    for key,y,h,color in [('R',630,105,'yellow'),('U',860,90,'yellow'),
            ('KD',1440,90,'yellow'),('K',1680,110,'yellow'),
            ('OJ',1900,100,'yellow'),('W',2190,85,'yellow')]:
        d.card(key,55,y,285,h,color)
    for key,y,h,color in [('X',1310,120,'yellow'),('H',1690,95,'yellow'),
            ('I',1910,100,'orange'),('J',2120,105,'purple'),
            ('Q',2350,95,'yellow'),('N',2560,90,'purple')]:
        d.card(key,860,y,285,h,color)
    for a,b in [('A','B'),('B','C'),('D','E'),('E','F'),('G','L'),('M','S')]:
        d.route(a,b,color='purple' if b!='L' else 'green',main=True)
    d.route('C','D',at=(630,820),width=70,color='purple',main=True)
    d.route('C','R',[(390,655),(340,655)],at=(292,600),width=150,color='yellow')
    d.route('R','C',[(197,630),(197,580),(440,580),(440,610)],
            at=(280,554),width=65,color='purple')
    d.route('R','U',at=(245,811),width=60,color='yellow')
    d.route('U','OJ',[(340,905),(360,905),(360,1930),(340,1930)],color='yellow')
    d.route('F','G',at=(629,1550),width=60,color='green',main=True)
    d.route('F','X',[(770,1340),(860,1340)],at=(815,1318),width=55,color='yellow')
    d.route('X','KD',[(1000,1430),(1000,1485),(340,1485)],
            at=(812,1460),width=55,color='yellow')
    d.route('KD','OJ',[(340,1500),(375,1500),(375,1960),(340,1960)],color='yellow')
    d.route('X','H',at=(1050,1600),width=60,color='yellow')
    d.route('H','I',[(1080,1785),(1080,1910)],at=(1115,1865),width=50,color='orange',main=True)
    d.route('I','J',color='purple',main=True)
    d.route('J','C',[(1145,2175),(1170,2175),(1170,650),(770,650)],color='purple')
    d.route('H','K',[(860,1740),(340,1740)],at=(809,1715),width=55,color='yellow')
    d.route('K','OJ',color='yellow',main=True)
    d.route('OJ','I',[(180,1900),(180,1850),(1000,1850),(1000,1910)],
            at=(655,1825),width=140,color='orange',main=True)
    d.route('OJ','H',[(340,1980),(840,1980),(840,1770),(860,1770)],
            at=(650,1950),width=140,color='orange')
    d.route('OJ','G',[(197,2000),(197,2090),(450,2090),(450,2130)],
            at=(395,2060),width=240,color='green',main=True)
    d.route('OJ','W',[(120,2000),(120,2190)],at=(197,2160),width=120,color='yellow',main=True)
    d.route('L','M',at=(632,2590),width=60,color='green',main=True)
    d.route('L','Q',[(770,2375),(860,2375)],at=(850,2318),width=60,color='yellow')
    d.route('Q','K',[(860,2410),(805,2410),(805,1760),(340,1760)],
            at=(809,2475),width=55,color='yellow')
    d.route('Q','N',at=(1050,2510),width=60,color='purple',main=True)
    d.route('N','C',[(1145,2605),(1190,2605),(1190,690),(770,690)],color='purple')
    d.text(55,2400,'主会话判断期间保持 in_progress',14,PALETTE['yellow'][0])
    d.text(55,2430,'只有交给人类才经 T11 等待用户',14,PALETTE['yellow'][0])
    d.save()


def classification():
    for k,t,c,dt,en in [
     ('S','源任务记录','blue','完整审计后的最终状态','orchestrator-agent'),
     ('R','已有 retirement 或 archived？','slate','','R · retirement check'),
     ('A','已归档 · 只读','slate','保留原 status 与退役原因','archived'),
     ('C','status == cancelled？','red','','C · cancellation check'),
     ('CR','已取消 · 只读','red','','cancelled'),
     ('D','status == done？','green','','D · completion check'),
     ('UA','用户验收有效且 revision 一致？','green','有效 user_acceptance 且 revision 一致','UA · acceptance check'),
     ('AC','已验收 · 只读','green','满7天后按新系统规则归档','accepted'),
     ('DN','完成 · 可 resume','green','待用户验收','done'),
     ('P','status == pending / ready？','blue','','P · planning check'),
     ('PN','待受理 · 可 resume','blue','保留依赖与规划理由','intake'),
     ('PA','status == paused / failed？','yellow','','PA · pause check'),
     ('PP','暂停 · 可 resume','yellow','保留 owner / next_action / resume_condition\nfailed 归入类别 failure','paused'),
     ('X','活动状态','orange','starting/running/waiting_user/review/\nintegrating/conflict','X · active states'),
     ('XH','暂停交接','yellow','类别 = import_handover\n须核实旧 Codex/Copilot worker 已停','paused'),
     ('RS','resume 资料包','blue','任务书 + brief + 已有报告 + 评审 + 产出/PR + 待答问题','RS · resume package')]:n(k,t,c,dt,en)
    d=SVG('import-classification','历史导入：分类决策','Import classification  ·  先判断退役与只读状态，再整理可恢复的任务现场',2160)
    d.node('S',66,255,450,92)
    for k,y in [('R',410),('C',565),('D',720),('P',1185),('PA',1340)]:d.node(k,66,y,450,88)
    for k,y in [('A',410),('CR',565),('UA',720),('PN',1185),('PP',1340)]:d.node(k,692,y,450,135 if k=='PP' else 108)
    d.node('AC',770,918,372,104);d.node('DN',332,918,372,104)
    d.node('X',66,1520,450,123);d.node('XH',692,1520,450,123)
    d.node('RS',250,1970,710,116)
    for idx in [0,2,4,10,12]:
        a,b,*_=d.es[idx];p=d.anchor(a);q=d.anchor(b,'t');d.edge(idx,[p,q],at=(339,(p[1]+q[1])/2+5),width=70,main=True,color=N[b][2])
    for idx in [1,3,5,9,11,13]:d.link(idx,'r','l',at=(603,d.anchor(d.es[idx][0],'r')[1]-15),width=90,main=True,color=N[d.es[idx][1]][2])
    d.edge(6,[d.anchor('UA','b',70),(987,883),(956,883),(956,918)],at=(1020,882),width=60,color='green',main=True)
    d.edge(7,[d.anchor('UA','b',-70),(847,858),(518,858),(518,918)],at=(617,883),width=60,color='green')
    d.edge(8,[d.anchor('D','b'),(291,1185)],at=(198,1114),width=60,main=True)
    # Resume rows explicitly name each source; no hidden fan-in behind decision branches.
    d.section(1750,'可恢复状态汇入同一个资料包','只读的 archived / cancelled / accepted 没有通往 resume 资料包的边。')
    for j,(idx,key) in enumerate([(14,'DN'),(15,'PN'),(16,'PP'),(17,'XH')]):
        x=44+j*283;d.node(key,x,1800,262,100,ref=True,detail='',title={'DN':'完成·待用户验收','PN':'待受理','PP':'暂停','XH':'暂停交接'}[key])
        endpoint=(335+j*177,1970)
        d.edge(idx,[(x+131,1900),(x+131,1932), (endpoint[0],1932),endpoint],color='blue',main=True)
    d.save()

def architecture():
    for k,t,c,dt,en in [
     ('U','用户','blue','','U · user'),
     ('O','主会话','blue','cwd = 控制根\nskill: orchestrate','orchestrator'),
     ('W1','worker 子会话','orange','cwd = 任务 worktree','T241-a1'),
     ('W2','worker 子会话','orange','cwd = 研究目录','T242-a1'),
     ('MCP','tsw MCP server','purple','按 cwd 解析角色·按角色暴露工具','MCP'),
     ('HOOKS','hooks','purple','SessionStart / PreToolUse / Stop','HOOKS'),
     ('SK','skills','purple','orchestrate / worker / self-review','SK'),
     ('L','事件账本','blue','ledger/events/*.jsonl','L · ledger'),
     ('PJ','.local 投影','teal','state.sqlite（可重建）','PJ · projection'),
     ('CFG','统一配置','blue','config/task-steward.json','CFG · configuration'),
     ('RD','渲染器','green','STATUS.html / ARCHIVE.html','RD · renderer'),
     ('IMP','导入器','blue','orchestrator-agent 历史','IMP · importer'),
     ('CX','Codex reviewer','purple','gpt-6-astra · medium · read-only','CX'),
     ('GH','GitHub / ADO','blue','PR · CI','GH'),
     ('TM','Teams self chat','teal','Teams MCP 工具','TM')]:n(k,t,c,dt,en)
    d=SVG('architecture','产品架构：会话、插件与事实账本','Architecture  ·  Claude app 承载会话，tsw 统一处理工具与事件事实',2920)
    d.node('U',44,237,240,70)
    d.panel(44,350,1112,366,'Claude Code 桌面 app','blue')
    d.node('O',79,419,318,115);d.node('W1',440,568,318,105);d.node('W2',801,568,318,105)
    d.edge(0,[d.anchor('U','b'),(164,419)],at=(293,333),width=210,main=True)
    d.edge(1,[d.anchor('U','r'),(592,272),(592,568)],at=(771,302),width=330,color='orange')
    d.edge(2,[d.anchor('O','b'),(238,554),(473,554),(473,568)],at=(268,583),width=322,color='orange',main=True)
    d.edge(3,[d.anchor('O','r'),(948,476),(948,568)],at=(923,452),width=345,color='orange',main=True)
    d.panel(44,768,1112,260,'TaskSteward 插件（用户级安装）','purple')
    d.node('HOOKS',78,860,318,123);d.node('MCP',442,860,318,123);d.node('SK',804,860,318,123)
    d.edge(9,[d.anchor('HOOKS','r'),d.anchor('MCP','l')],color='purple')
    # Undirected role/tool associations are explicit, labeled in the section below.
    d.panel(44,1088,1112,502,'tsw core（Python 标准库）','blue')
    d.node('L',445,1180,318,105);d.node('PJ',445,1320,318,105);d.node('RD',445,1460,318,105)
    d.node('CFG',78,1180,318,105);d.node('IMP',804,1180,318,105)
    d.edge(10,[d.anchor('MCP'),(601,1180)],color='blue',main=True)
    d.link(11,color='teal',main=True);d.link(12,color='green',main=True)
    d.edge(13,[d.anchor('CFG','t'),(237,1055),(520,1055),(520,983)],color='blue')
    d.edge(14,[d.anchor('IMP','l'),d.anchor('L','r')],color='blue',main=True)
    d.section(1650,'会话到工具的关联','无箭头实线沿用源图的关联关系；skills 在源图中属于插件分组，没有独立连线。')
    for j,idx in enumerate([6,7,8]):
        x=44+j*379;y=1700;a,b,*_=d.es[idx]
        d.node(a,x,y,352,112,ref=True)
        d.node(b,x,y+153,352,106,ref=True)
        d.edge(idx,[(x+176,y+112),(x+176,y+153)],color='purple')
    d.section(2025,'外部协作与通知','评审由 worker 发起；完成、失败、待答通知由主会话发送。')
    for j,idx in enumerate([15,16,17]):
        x=44+j*379;y=2073;a,b,*_=d.es[idx]
        d.node(a,x,y,352,112,ref=True);d.node(b,x,y+209,352,125,ref=True)
        d.edge(idx,[(x+176,y+112),(x+176,y+209)],at=(x+176,y+155),width=330,color=N[b][2],main=True)
    d.section(2490,'worker 唤醒主会话','reportable 事件主动发消息；平台在全部子会话空闲时提供兜底。')
    for j,idx in enumerate([4,5]):
        y=2540+j*168
        d.node('W1',79,y,318,105,ref=True)
        d.node('O',801,y,318,105,ref=True)
        d.edge(idx,[(397,y+53),(801,y+53)],at=(599,y+21),width=390,
               color='teal',main=idx==4,dashed=idx==5)
    d.save()

def sequence():
    for k,t,c,en in [('U','用户','blue','user'),('O','主会话','blue','orchestrator'),('C','tsw','teal','MCP + 账本'),('A','Claude app','slate','app'),('W','worker 子会话','orange','worker'),('X','Codex reviewer','purple','reviewer')]:n(k,t,c,english=en)
    d=SVG('end-to-end-sequence','端到端协作时序','End-to-end sequence  ·  从新任务、评审与主控核验，到用户验收当前 revision',3150)
    xs={'U':130,'O':318,'C':506,'A':694,'W':882,'X':1070}
    for k,x in xs.items():
        c,b=PALETTE[N[k][2]];d.rect(x-80,324,160,2750,b,rx=0,extra='opacity=".42"')
        d.node(k,x-85,236,170,72)
        d.path([(x,313),(x,3078)],'slate',False,True,False)
    ys={};y=370
    bands={0:'01  登记、规划与启动',8:'02  执行与两轮评审',15:'03  回传与主控核验',19:'ALT  ·  仍有剩余工作',21:'ELSE  ·  核验通过'}
    for i,(a,b,label,ds,ud) in enumerate(d.es):
        if i in bands:
            d.rect(44,y-20,1112,36,'#E6EDF5',rx=6);d.text(58,y+4,bands[i],16,'#233C54',weight=700);y+=66
        ys[i]=y
        x1,x2=xs[a],xs[b];co=N[a][2]
        if a==b:
            route=[(x1,y+22),(x1+70,y+22),(x1+70,y+49),(x1,y+49)]
        else:route=[(x1,y+30),(x2,y+30)]
        # Place long messages above arrows across available lane space, preserving endpoints.
        width=min(940,max(abs(x1-x2)-18,600 if len(label)>40 else 410))
        mid=(x1+x2)/2 if a!=b else x1+180
        mid=max(85+width/2,min(1135-width/2,mid))
        d.edge(i,route,at=(mid,y-7),width=width,color=co,main=not ds,dashed=ds,label=f'{i+1:02d}  {label}')
        y+=91 if len(wrap(label,width-14,15))<2 else 108
    alt_top=ys[19]-86
    d.rect(44,alt_top,1112,y-alt_top-35,'none','#9C86BF',12,extra='stroke-width="1.5"')
    d.save()



class FlowSVG(StateSVG):
    """Manual flow layouts; source text is authoritative, labels clear all routes."""
    def __init__(self, *args):
        super().__init__(*args)
        source=(IN/(self.name+'.mmd')).read_text(encoding='utf-8')
        self.source_labels={key:label.strip('"').replace('<br/>','\n')
            for line in source.splitlines() if not line.strip().startswith('subgraph ')
            for key,label in re.findall(r'\b(\w+)[\[({]+(.+?)[\])}]+(?=\s*(?:-->|$))',line)}

    def card(self,key,x,y,w,h,color='blue',size=16):
        raw=self.source_labels[key]
        title,*detail=raw.split('\n')
        c,b=PALETTE[color]
        self.parts.append(f'<g data-node="{key}" data-box="{x},{y},{w},{h}">')
        self.rect(x,y,w,h,b,c,13,extra=f'filter="url(#{self.name}-shadow)" stroke-opacity=".40"')
        self.rect(x,y+13,4,h-26,c,rx=2)
        cursor=y+26
        for text,font,weight in [(title,size,650)]+[(line,14,400) for line in detail]:
            for line in wrap(text,w-30,font):
                assert cursor+5<=y+h,(key,'card text overflows',line)
                self.text(x+15,cursor,line,font,c,weight=weight)
                cursor+=font*1.45
        self.parts.append('</g>')
        self.nodes[key]=(x,y,w,h)

    def route(self,a,b,points=None,match=None,**kw):
        indices=[i for i,e in enumerate(self.es) if e[:2]==(a,b) and (match is None or e[2]==match)]
        assert len(indices)==1,(a,b,indices)
        self.edge(indices[0],points or [self.anchor(a),self.anchor(b,'t')],**kw)

    def edge(self,index,points,**kw):
        # Deferred labels retain an explicit edge reference for coverage checks.
        start=len(self.parts)
        self.labels.append(f'<g id="{self.name}-label-{index}">')
        super().edge(index,points,**kw)
        self.labels.append('</g>')
        self.parts[start]=self.parts[start].replace(' data-edge=',f' data-label-ref="{self.name}-label-{index}" data-edge=',1)


def plugin_architecture():
    d=FlowSVG('plugin-architecture','插件层架构：核心、接口与宿主',
              'Plugin architecture  ·  四类插件；评审协议经会话适配器调用一次性评审会话',1930)
    d.parts.append('<g data-node="HOSTS" data-box="60,240,1080,220" data-container="true">')
    d.panel(60,240,1080,220,'01  宿主中的模型会话','blue')
    d.parts.append('</g>');d.nodes['HOSTS']=(60,240,1080,220)
    d.card('O',290,325,275,90)
    d.card('W',600,325,230,90,'orange')
    d.card('R',865,325,250,90,'purple')
    d.panel(60,525,1080,650,'02  tsw core（与宿主无关）','purple')
    d.card('MCP',270,585,810,100,'purple')
    d.card('RV',270,810,240,100,'purple')
    d.card('TR',555,810,250,100,'yellow')
    d.card('SM',880,810,210,100,'blue')
    d.card('OPS',555,1010,280,100,'orange')
    d.panel(44,1290,1112,200,'03  插件接口（plugin_api v1）','purple')
    d.panel(44,1540,1112,230,'04  插件实现（示例）','slate')
    for key,x,w,color in [('1',70,240,'blue'),('2',350,280,'orange'),
            ('3',675,230,'teal'),('4',950,180,'teal')]:
        d.card('P'+key,x,1350,w,105,color)
        d.card('I'+key,x,1600,w,140,color,size=14)
        route=[(190,1455),(190,1510),(34,1510),(34,1585),(190,1585),(190,1600)] if key=='1' else None
        d.route('P'+key,'I'+key,route,color=color)
    d.card('DATA',750,1805,390,85,'teal')
    d.route('O','MCP',[(430,415),(430,585)],match='tsw 工具',
            at=(350,490),width=120,main=True)
    d.route('W','MCP',[(715,415),(715,585)],at=(789,490),width=120,main=True)
    d.route('O','MCP',[(530,415),(530,585)],match='receipt',
            at=(594,490),width=100,color='teal',main=True)
    d.route('MCP','SM',[(985,685),(985,810)],color='blue',main=True)
    d.route('MCP','RV',[(390,685),(390,810)],color='purple',main=True)
    d.route('MCP','TR',[(680,685),(680,810)],color='yellow',main=True)
    d.route('TR','OPS',[(680,910),(680,1010)],color='orange',main=True)
    d.route('SM','OPS',[(985,910),(985,955),(785,955),(785,1010)],color='orange',main=True)
    d.route('OPS','O',[(555,1060),(45,1060),(45,370),(290,370)],
            at=(150,747),width=150,color='orange',main=True)
    d.route('OPS','P2',[(600,1110),(600,1350)],color='orange',main=True)
    d.route('OPS','P3',[(785,1110),(785,1350)],color='teal',main=True)
    d.route('RV','P2',[(420,910),(420,1350)],
            at=(260,1210),width=235,color='purple',main=True)
    d.route('P2','R',[(490,1350),(490,1250),(1175,1250),(1175,370),(1115,370)],color='purple',main=True)
    d.route('SM','P4',[(1050,910),(1050,1350)],color='teal')
    d.route('P4','DATA',[(1130,1400),(1168,1400),(1168,1847),(1140,1847)],color='teal',main=True)
    d.route('P1','HOSTS',[(70,1390),(22,1390),(22,290),(60,290)],
            at=(160,1118),width=240,color='slate')
    d.text(60,1795,'P1 虚线：生成宿主配置；交叉但无圆点的线不相连。',14,'#53657E')
    d.save()


def input_routing():
    d=FlowSVG('input-routing','主会话判断：决定与人类渠道',
              'Input routing  ·  唯一硬边界是已有授权；其余综合判断，决定写入记录',2640)
    for key,x in [('Q1',50),('Q2',425),('Q3',800)]:
        d.card(key,x,255,350,115,'orange')
    d.panel(50,435,650,545,'01  主会话判断','yellow')
    d.card('J',70,495,610,70,'yellow')
    d.card('A',70,655,610,115,'yellow')
    d.card('F',70,845,610,110,'yellow')
    d.card('D',70,1060,150,90,'yellow')
    for key,y,h in [('R1',1060,100),('R2',1270,130),('R3',1500,80),
            ('R4',1690,100),('R5',1910,80)]:
        d.card(key,320,y,360,h,'green')
    d.card('DR',320,2120,360,100,'teal')
    d.card('W1',320,2300,360,85,'green')
    d.panel(800,1030,360,1530,'02  人类渠道','yellow')
    d.card('ESC',820,1090,320,105,'orange')
    d.card('CH',820,1280,320,85,'teal')
    for key,y in [('S1',1450),('S2',1640),('S3',1830)]:
        d.card(key,850,y,270,115,'teal')
    d.card('V',820,2100,320,85,'teal')
    d.card('RR',820,2280,320,105,'green')
    d.card('C',820,2430,320,100,'slate')
    d.route('Q1','J',[(225,370),(225,410),(300,410),(300,495)],main=True)
    d.route('Q2','J',[(600,370),(600,390),(375,390),(375,495)],main=True)
    d.route('Q3','J',[(975,370),(975,425),(550,425),(550,495)],main=True)
    d.route('J','A',at=(530,612),width=255,color='yellow',main=True)
    d.route('J','ESC',[(680,530),(780,530),(780,1120),(820,1120)],
            at=(953,510),width=195,color='orange',main=True)
    d.route('A','ESC',[(680,712),(750,712),(750,1150),(820,1150)],
            at=(714,688),width=45,color='orange',main=True)
    d.route('A','F',at=(420,812),width=65,color='yellow',main=True)
    d.route('F','D',[(145,955),(145,1060)],color='yellow',main=True)
    d.route('D','R1',[(220,1090),(320,1090)],at=(280,1068),width=60,color='green',main=True)
    for key,y in [('R2',1310),('R3',1540),('R4',1730),('R5',1950)]:
        d.route('D',key,[(175,1150),(175,y),(320,y)],
                at=(257,y-26),width=110,color='green',main=True)
    d.route('D','ESC',[(220,1130),(245,1130),(245,1010),(1125,1010),(1125,1090)],
            at=(955,985),width=150,color='orange',main=True)
    # The five autonomous decisions share a collector beside the cards.
    for key,y in [('R1',1110),('R2',1335),('R3',1540),('R4',1740),('R5',1950)]:
        d.route(key,'DR',[(680,y),(715,y),(715,2165),(680,2165)],color='teal',main=True)
    for x,ys,color in [(175,[1310,1540,1730],'green'),(715,[1335,1540,1740,1950],'teal')]:
        for y in ys:d.parts.append(f'<circle cx="{x}" cy="{y}" r="3" fill="{PALETTE[color][0]}"/>')
    d.route('DR','W1',color='green',main=True)
    d.route('ESC','CH',color='orange',main=True)
    d.route('CH','S1',[(985,1365),(985,1450)],color='teal',main=True)
    d.route('CH','S2',[(820,1310),(800,1310),(800,1695),(850,1695)],color='teal')
    d.route('CH','S3',[(820,1340),(775,1340),(775,1885),(850,1885)],color='teal')
    d.route('S1','V',[(1120,1505),(1175,1505),(1175,2125),(1140,2125)],color='teal')
    d.route('S2','V',[(1120,1695),(1155,1695),(1155,2160),(1140,2160)],color='teal')
    d.route('S3','V',[(985,1945),(985,2100)],color='teal',main=True)
    d.route('V','RR',color='green',main=True)
    d.route('RR','C',color='slate',main=True)
    d.text(70,2450,'判断边界',20,'#152C46',weight=700)
    d.lines(70,2485,'主会话决定不替代用户验收。\n只有交给人类才经 T11 进入 waiting_user。\n超时不是答复；迟到答复不覆盖已生效答复。',630,15,leading=28)
    d.save()

def storage_and_context():
    d=FlowSVG('storage-and-context','存储分层与任务上下文',
              'Storage & context  ·  写入归约、检查点恢复与分层检索（DN007）',1880)
    d.panel(100,225,1050,180,'写入方（都经 tsw 写入口）','blue')
    d.card('O',140,290,250,90)
    d.card('W',460,290,380,90,'orange')
    d.card('H',900,290,240,90,'yellow')
    d.card('G',460,470,380,90,'purple')

    # L0 is the authoritative tier; saturated red distinguishes its heading.
    c,b=PALETTE['red']
    d.rect(100,600,1050,220,b,c,18)
    d.rect(115,612,380,40,c,rx=8)
    d.text(130,639,'L0  事件账本 · 唯一事实 · 备份',20,'#FFFFFF',weight=700)
    d.card('IM',140,690,250,90,'red')
    d.card('EV',460,690,380,90,'red')
    d.card('RAW',900,690,240,90,'red')

    d.panel(100,890,760,340,'L1  检查点与投影 · 可重建','teal')
    d.card('CP',140,1000,250,90,'teal')
    d.card('ST',460,1000,380,120,'teal')
    d.panel(890,960,260,180,'系统读取方 · 只读 L1','blue')
    d.card('GUARD',900,1010,240,70)

    d.panel(100,1240,1050,250,'L2  检索索引 · 可重建','teal')
    d.card('CARD',140,1370,250,90,'teal')
    d.card('AL',500,1370,250,90,'teal')
    d.card('FTS',900,1370,240,90,'teal')
    d.panel(100,1500,1050,340,'读取方','blue')
    d.card('CTX',340,1560,500,110,'blue')
    d.card('BRIEF',460,1750,380,70,'orange')

    d.route('O','G',[(265,380),(265,440),(550,440),(550,470)],main=True)
    d.route('W','G',[(650,380),(650,470)],main=True)
    d.route('H','G',[(1020,380),(1020,440),(750,440),(750,470)],main=True)
    d.route('G','EV',[(650,560),(650,690)],
            at=(730,612),width=100,color='red',main=True)
    # The gate-to-projection lane stays outside the authoritative event card.
    d.route('G','ST',[(840,515),(870,515),(870,1080),(840,1080)],
            at=(1000,867),width=180,color='purple',main=True)
    d.route('EV','CP',[(550,780),(550,960),(265,960),(265,1000)],
            at=(470,854),width=100,color='slate')
    d.route('CP','ST',[(265,1090),(265,1170),(550,1170),(550,1120)],
            at=(410,1143),width=240,color='slate')
    d.route('IM','EV',[(390,735),(460,735)],
            at=(420,672),width=100,color='slate')
    d.route('ST','CARD',[(650,1120),(650,1280),(265,1280),(265,1370)],
            at=(400,1320),width=100,color='teal',main=True)
    # Raw material uses the right margin to bypass the L1-only system reader.
    d.route('RAW','FTS',[(1140,735),(1170,735),(1170,1330),(1020,1330),(1020,1370)],
            at=(1050,1210),width=110,color='teal',main=True)
    d.route('ST','AL',[(780,1120),(780,1310),(620,1310),(620,1370)],color='teal',main=True)
    d.route('ST','GUARD',[(840,1045),(900,1045)],color='blue',main=True)
    d.route('CARD','CTX',[(265,1460),(265,1540),(520,1540),(520,1560)],color='teal',main=True)
    d.route('AL','CTX',[(620,1460),(620,1560)],color='teal',main=True)
    d.route('FTS','CTX',[(1020,1460),(1020,1600),(840,1600)],color='teal',main=True)
    d.route('CTX','BRIEF',[(590,1670),(590,1750)],color='blue',main=True)
    # One return lane supplies the original main-session node, without copies.
    d.route('CTX','O',[(340,1615),(70,1615),(70,335),(140,335)],
            at=(205,1585),width=215,color='blue',main=True)
    d.save()


if __name__=='__main__':
    task();review();classification();architecture();sequence()
    plugin_architecture();input_routing();storage_and_context()
    print('Wrote 8 hand-authored SVG diagrams.')
