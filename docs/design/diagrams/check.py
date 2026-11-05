"""Standard-library validation: XML, visible transition labels, exact edge coverage,
node coverage, portable SVG restrictions, prefixed IDs and font-size floor.
Also compare DN002 §3.2 (T) and §3.3 (E) with the unified task state diagram.
"""
from pathlib import Path
import json
import re
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
INPUT = ROOT
NS = '{http://www.w3.org/2000/svg}'
CODE = r'\b(?:T\d{2}|E\d{2}|I\d{2})\b'
FLOW_LAYOUTS = {'plugin-architecture', 'input-routing', 'review-loop', 'storage-and-context'}

def source_edges(source):
    rows=[]
    for line in source.splitlines():
        s=line.strip()
        if source.startswith('stateDiagram'):
            m=re.fullmatch(r'(\[\*\]|\w+) --> (\[\*\]|\w+)(?:: (.*))?',s)
            if m:rows.append((m[1],m[2],m[3] or ''))
        elif source.startswith('sequenceDiagram'):
            m=re.fullmatch(r'(\w+)(-->>|->>)(\w+): (.*)',s)
            if m:rows.append((m[1],m[3],m[4]))
        else:
            m=re.search(r'(?: --> | --- | \.-> )(\w+)',s)
            if not m:continue
            src=re.match(r'\w+',s)[0]
            label=''
            if ' -- ' in s:label=s.split(' -- ',1)[1].split(' --> ',1)[0]
            if ' -. ' in s:label=s.split(' -. ',1)[1].split(' .-> ',1)[0]
            rows.append((src,m[1],label))
    return rows

def norm(s):return re.sub(r'\s+','',s)
def validate(path):
    name=path.stem;source=(INPUT/(name+'.mmd')).read_text(encoding='utf-8')
    root=ET.parse(path).getroot()
    assert root.tag==NS+'svg'
    assert len(root.attrib.get('viewBox','').split())==4
    assert root.attrib['font-family']=='Segoe UI, Microsoft YaHei, sans-serif'
    ids=[e.attrib['id'] for e in root.iter() if 'id' in e.attrib]
    assert len(set(ids))==len(ids),'duplicate IDs'
    assert all(s.startswith(name+'-') for s in ids),'unprefixed ID'
    assert all(e.tag not in {NS+'foreignObject',NS+'script',NS+'image'} for e in root.iter())
    for e in root.iter():
        for k,v in e.attrib.items():
            assert not k.lower().startswith('on'),'event handler'
            if k.endswith('href'):assert v.startswith('#'),'external reference'
            for ref in re.findall(r'url\(#([^)]*)\)',v):assert ref in ids,'unresolved reference'
        if e.tag==NS+'text':assert float(e.attrib['font-size'])>=13,'font too small'
    visible=' '.join(''.join(e.itertext()) for e in root.iter(NS+'text'))
    codes=set(re.findall(CODE,source));found=set(re.findall(CODE,visible))
    assert not codes-found,('missing visible transition IDs',codes-found)
    expected=source_edges(source)
    actual=[e for e in root.iter() if 'data-edge' in e.attrib]
    assert len(actual)==len(expected),'edge count'
    by_index={int(e.attrib['data-edge']):e for e in actual}
    assert len(by_index)==len(expected),'duplicate edge index'
    for i,(a,b,label) in enumerate(expected):
        e=by_index[i]
        assert (e.attrib['data-source'],e.attrib['data-target'])==(a,b),('edge endpoints',i)
        label_element=next((t for t in root.iter() if t.attrib.get('id')==e.attrib.get('data-label-ref')),e) if e.attrib.get('data-label-ref') else e
        texts=''.join(''.join(t.itertext()) for t in label_element.iter(NS+'text'))
        # Label must be visible, not just present in title/metadata.
        if name=='task-state-machine' and label:
            label_codes=edge_codes(label)
            assert label_codes,('missing transition ID',i,label)
            code=label_codes[0]
            assert code in texts and len(texts.strip())>len(code),('missing short label',i,label)
        else:
            assert norm(label) in norm(texts),('missing visible label',i,label)
        assert e.find(NS+'path') is not None,('missing path',i)
    expected_nodes={x for e in expected for x in e[:2] if x!='[*]'}
    expected_nodes.update(re.findall(r'^\s*state ".*?" as (\w+)',source,re.M))
    expected_nodes.update(re.findall(r'^\s*state (\w+) <<choice>>',source,re.M))
    if source.startswith('flowchart'):
        expected_nodes.update(re.findall(r'^\s*(\w+)[\[({]',source,re.M))
    actual_nodes={e.attrib['data-node'] for e in root.iter() if 'data-node' in e.attrib}
    assert expected_nodes<=actual_nodes,('missing nodes',expected_nodes-actual_nodes)
    if name=='task-state-machine':validate_state_geometry(root, expected, by_index, source)
    if name in FLOW_LAYOUTS:validate_flow_geometry(root, expected, by_index, source)
    # All visible text baselines remain within canvas, including tall layouts.
    width,height=map(float,root.attrib['viewBox'].split()[2:])
    for e in root.iter(NS+'text'):
        x=float(e.attrib['x']);y=float(e.attrib['y'])
        assert 0<=x<=width and 0<=y<=height,(name,'text outside viewBox',e.text,x,y)
    return {'file':path.name,'xml':'PASS','nodes':len(expected_nodes),'edges':len(expected),
            'unique_transition_ids':len(codes),'visible_labels':'PASS','ids_and_portability':'PASS',
            'viewBox':root.attrib['viewBox'],
            **({'node_labels':'PASS','edge_geometry':'PASS','label_clearance':'PASS','text_clearance':'PASS'} if name in FLOW_LAYOUTS else {}),
            **({'state_uniqueness':'PASS','edge_geometry_and_buses':'PASS','label_clearance':'PASS'} if name=='task-state-machine' else {})}

# A code mentioned later in a label is a reference, not that edge's ID.
def edge_codes(label):
    match=re.match(r'('+CODE+r'(?:\s*/\s*'+CODE+r')*)',label)
    return re.findall(CODE,match[0]) if match else []

def state_model(source):
    aliases={};parents={};choices=set();stack=[]
    for line in source.splitlines():
        text=line.strip()
        if text=='}':
            if stack:stack.pop()
            continue
        match=re.match(r'state "(.*?)" as (\w+)(.*)',text)
        choice=re.match(r'state (\w+) <<choice>>',text)
        if match:
            display,key,tail=match.groups()
            aliases[key]=key
            aliases[norm(display)]=key
            # Chinese display name, with English ID and annotations removed.
            chinese=re.split(r'\b'+re.escape(key)+r'\b',display)[0].strip()
            if chinese:aliases[norm(chinese)]=key
            if stack:parents[key]=stack[-1]
            if tail.strip()=='{':stack.append(key)
        elif choice:
            key=choice[1];choices.add(key);aliases[key]=key
            if stack:parents[key]=stack[-1]
    for a,b,_ in source_edges(source):
        for key in (a,b):aliases[key]=key
    return aliases,parents,choices


def table_states(cell,model,terminal_states):
    aliases,parents,choices=model
    text=cell.replace('`','').replace('**','').strip()
    if text in {'（新建）','(新建)'}:return {'[*]'}
    if text.startswith(('任一','任意','任何')):
        # Prefer an explicit enumeration; otherwise expand the declared scope.
        listed=re.search(r'[（(](.*)[）)]',text)
        if listed:return table_states(listed[1],model,terminal_states)
        if '子状态' in text:
            return set(parents)-choices
        states=set(aliases.values())-choices-{'[*]'}
        if '非终态' in text:states-=terminal_states
        elif text not in {'任一状态','任意状态','任何状态'}:
            raise ValueError('无法确定通配状态范围：'+text)
        return states
    result=set()
    # Strip destination annotations before splitting (e.g. paused(reason=...)).
    text=re.sub(r'（.*?）|\(.*?\)','',text)
    for item in re.split(r'、|\s*/\s*',text):
        key=norm(item)
        if key in aliases:result.add(aliases[key]);continue
        english=[t for t in re.findall(r'[A-Za-z_][A-Za-z_0-9]*',item) if t in aliases]
        if len(english)==1:result.add(aliases[english[0]]);continue
        raise ValueError('无法识别状态：'+item)
    return result


def validate_dn002(document=None,sources=None):
    """Only the numbered T (§3.2) and E (§3.3) tables are canonical.

    §3.4 import mapping is not part of the state diagram; the unnumbered
    attempt attributes in §6 are intentionally outside scope.
    """
    if document is None:document=(ROOT.parent/'DN002-task-state-machine.md').read_text(encoding='utf-8')
    if sources is None:sources={'task-state-machine':(INPUT/'task-state-machine.mmd').read_text(encoding='utf-8')}
    source=sources['task-state-machine'];model=state_model(source)
    actual={};errors=[]
    for a,b,label in source_edges(source):
        for code in edge_codes(label):
            pairs=actual.setdefault(code,set())
            if (a,b) in pairs:errors.append(f'{code}：图源重复迁移 {a} → {b}')
            pairs.add((a,b))
    rows={};header=[];section=None
    for number,line in enumerate(document.splitlines(),1):
        if line.startswith('#'):
            heading=re.match(r'### (3\.[23])(?:\s|$)',line)
            section=heading[1] if heading else None
            header=[]
        if section is None or not line.lstrip().startswith('|'):continue
        cells=[c.strip() for c in re.split(r'(?<!\\)\|',line.strip())[1:-1]]
        if not cells:continue
        if cells[0]=='ID':header=cells;continue
        raw=cells[0].strip('` ')
        if re.fullmatch(r'[: -]+',raw):continue
        try:
            if not re.fullmatch(CODE,raw):raise ValueError('不支持的迁移编号 '+raw)
            if raw in rows:raise ValueError('重复编号 '+raw)
            expected_prefix={'3.2':'T','3.3':'E'}[section]
            if not raw.startswith(expected_prefix):raise ValueError('编号不属于 §'+section)
            rows[raw]={'line':number}
            from_cell=cells[header.index('从') if '从' in header else header.index('来源记录')]
            to_cell=cells[header.index('到') if '到' in header else header.index('进入状态')]
            starts=table_states(from_cell,model,set())
            ends=table_states(to_cell,model,set())
            rows[raw]['pairs']={(a,b) for a in starts for b in ends}
        except (ValueError,IndexError) as exc:errors.append(f'DN002:{number} {raw}：{exc}')
    for code,row in rows.items():
        if 'pairs' not in row:continue
        found=actual.get(code,set());expected=row['pairs']
        where=f'DN002:{row["line"]} {code} / task-state-machine.mmd'
        for a,b in sorted(expected-found):errors.append(where+f'：表格有 {a} → {b}，图源缺失')
        for a,b in sorted(found-expected):errors.append(where+f'：图源有 {a} → {b}，表格没有')
    for code,pairs in sorted(actual.items()):
        if code not in rows:
            for a,b in sorted(pairs):errors.append(f'{code}：图源 {a} → {b} 未列入 DN002 迁移表')
    for prefix in 'TE':
        if not any(code.startswith(prefix) for code in rows):errors.append(f'DN002：未解析到 {prefix} 迁移表')
    return {'status':'FAIL' if errors else 'PASS','file':'../DN002-task-state-machine.md',
            'transition_ids':len(rows),'exact_ids':len(rows),'canonical_edges':sum(len(p) for p in actual.values()),
            'notes':['只校验 §3.2 T、§3.3 E；全部对应 task-state-machine.mmd。§3.4 导入映射不在状态图中。'],
            'differences':errors}


def points(path):
    return [tuple(map(float,p.split(','))) for p in re.findall(r'(-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?)',path.attrib['d'])]


def on_segment(p,a,b):
    return (abs((p[0]-a[0])*(b[1]-a[1])-(p[1]-a[1])*(b[0]-a[0]))<.01
            and min(a[0],b[0])-.01<=p[0]<=max(a[0],b[0])+.01
            and min(a[1],b[1])-.01<=p[1]<=max(a[1],b[1])+.01)


def segment_hits_box(p,q,box):
    """Liang–Barsky clipping against the interior (border contact is allowed)."""
    x,y,w,h=box;epsilon=.2
    lo,hi=0.,1.
    for start,delta,lower,upper in [(p[0],q[0]-p[0],x+epsilon,x+w-epsilon),
                                   (p[1],q[1]-p[1],y+epsilon,y+h-epsilon)]:
        if abs(delta)<1e-9:
            if not lower<start<upper:return False
        else:
            t1,t2=sorted(((lower-start)/delta,(upper-start)/delta))
            lo=max(lo,t1);hi=min(hi,t2)
    return lo<hi


def boxes_overlap(a,b):
    x,y,w,h=a;u,v,c,d=b
    return max(x,u)<min(x+w,u+c) and max(y,v)<min(y+h,v+d)


def validate_state_geometry(root,expected,by_index,source):
    elements=[e for e in root.iter() if 'data-node' in e.attrib]
    keys=[e.attrib['data-node'] for e in elements]
    assert len(keys)==len(set(keys)),'task states must be drawn exactly once'
    boxes={e.attrib['data-node']:tuple(map(float,e.attrib['data-box'].split(','))) for e in elements}
    assert set(boxes)=={k for a,b,_ in expected for k in (a,b) if k!='[*]'},'extra or missing state'
    by_id={e.attrib['id']:e for e in root.iter() if 'id' in e.attrib}
    radii={}
    for element in elements:
        key=element.attrib['data-node'];x,y,w,h=boxes[key]
        rect=element.find(NS+'rect')
        assert rect is not None,('missing visible state',key)
        assert tuple(float(rect.attrib[k]) for k in ('x','y','width','height'))==boxes[key],('state box differs from drawing',key)
        radii[key]=float(rect.attrib.get('rx',0))
    def boundary(p,key):
        x,y,w,h=boxes[key]
        radius=radii[key]
        return any(on_segment(p,a,b) for a,b in [((x+radius,y),(x+w-radius,y)),
            ((x+w,y+radius),(x+w,y+h-radius)),((x+w-radius,y+h),(x+radius,y+h)),
            ((x,y+h-radius),(x,y+radius))])
    bus_sources={}
    main_ids={'T04','T07','T08','T13','T18','T22','T24'}
    for i,(a,b,label) in enumerate(expected):
        edge=by_index[i];path=edge.find(NS+'path');route=points(path)
        assert len(route)>=2,('empty route',i)
        if a!='[*]':assert boundary(route[0],a),('route does not leave source',i,a,route[0])
        target_route=route
        full_route=route
        if 'data-bus-ref' in edge.attrib:
            bus=by_id[edge.attrib['data-bus-ref']];trunk=bus.find(NS+'path');target_route=points(trunk)
            assert any(on_segment(route[-1],p,q) for p,q in zip(target_route,target_route[1:])),('bus branch disconnected',i)
            # Follow the trunk from the junction to validate the complete self-loop.
            junction=next(j for j,(p,q) in enumerate(zip(target_route,target_route[1:]))
                          if on_segment(route[-1],p,q))
            full_route=route+target_route[junction+1:]
            assert 'marker-end' in trunk.attrib,('bus missing arrow',i)
            assert 'marker-end' not in path.attrib,('branch arrow should be on trunk',i)
            bus_sources.setdefault(bus.attrib['data-bus'],set()).add(a)
        else:assert 'marker-end' in path.attrib,('missing arrow',i)
        if b!='[*]':assert boundary(target_route[-1],b),('route does not enter target',i,b,target_route[-1])
        code=edge_codes(label)
        if code and code[0] in main_ids:
            assert float(path.attrib['stroke-width'])>=3 and 'stroke-dasharray' not in path.attrib,('main path not bold solid',code)
        if a==b:
            assert len(set(full_route))>=4 and full_route[0]!=full_route[-1],('self-loop missing',i)
    for code in ('T25','T16','E07'):
        wanted={a for a,b,label in expected if edge_codes(label)==[code]}
        assert bus_sources.get(code)==wanted,('bus source coverage',code)
    aliases,parents,choices=state_model(source)
    assert {k for k,v in parents.items() if v=='in_progress'}=={'executing','reviewing','triaging','revising'}
    x,y,w,h=boxes['in_progress']
    for key,parent in parents.items():
        if parent!='in_progress':continue
        a,b,c,d=boxes[key]
        assert x<a<a+c<x+w and y<b<b+d<y+h,('child outside composite',key)
    assert root.attrib['width']=='1200','canvas width'
    labels=[(''.join(e.itertext()),tuple(map(float,e.attrib['data-label-box'].split(','))))
            for e in root.iter() if 'data-label-box' in e.attrib]
    paths=[(e.attrib.get('data-edge',e.attrib.get('data-bus')),points(e.find(NS+'path')))
           for e in root.iter() if 'data-edge' in e.attrib or 'data-bus' in e.attrib]
    collisions=[]
    for i,(label,box) in enumerate(labels):
        for other,other_box in labels[i+1:]:
            if boxes_overlap(box,other_box):collisions.append(f'labels overlap: {label} / {other}')
        for key,node_box in boxes.items():
            if key!='in_progress' and boxes_overlap(box,node_box):collisions.append(f'label overlaps node: {label} / {key}')
        for key,route in paths:
            if any(segment_hits_box(a,b,box) for a,b in zip(route,route[1:])):
                collisions.append(f'label overlaps route: {label} / {key}')
    for key,route in paths:
        for node,box in boxes.items():
            # Child transitions legitimately lie inside the composite state.
            if node=='in_progress':continue
            if any(segment_hits_box(a,b,box) for a,b in zip(route,route[1:])):
                collisions.append(f'route crosses node: {key} / {node}')
    assert not collisions,'\n'.join(collisions)




def validate_flow_geometry(root,expected,by_index,source):
    """Check source coverage and hand-authored flow layout bounds."""
    assert root.attrib['width']=='1200','canvas width'
    elements=[e for e in root.iter() if 'data-node' in e.attrib]
    keys=[e.attrib['data-node'] for e in elements]
    assert len(keys)==len(set(keys)),'flow nodes must be drawn exactly once'
    nodes={e.attrib['data-node']:e for e in elements}
    boxes={key:tuple(map(float,e.attrib['data-box'].split(','))) for key,e in nodes.items()}
    containers={key for key,e in nodes.items() if e.attrib.get('data-container')=='true'}
    for line in source.splitlines():
        if line.strip().startswith('subgraph '):continue
        for key,label in re.findall(r'\b(\w+)[\[({]+(.+?)[\])}]+(?=\s*(?:-->|$))',line):
            raw=label.strip('"').replace('<br/>','')
            visible=''.join(''.join(t.itertext()) for t in nodes[key].iter(NS+'text'))
            assert norm(raw) in norm(visible),('missing visible node text',key,raw)
    routes=[]
    for i,(a,b,label) in enumerate(expected):
        path=by_index[i].find(NS+'path');route=points(path)
        assert len(route)>=2,('empty route',i)
        for key,p in [(a,route[0]),(b,route[-1])]:
            x,y,w,h=boxes[key]
            assert any(on_segment(p,u,v) for u,v in [((x,y),(x+w,y)),((x+w,y),(x+w,y+h)),
                ((x+w,y+h),(x,y+h)),((x,y+h),(x,y))]),('disconnected route',i,key,p)
        source_line=next(line for line in source.splitlines()
                         if re.match(r'^\s*'+re.escape(a)+r'\b',line)
                         and re.search(r'(?: --> | --- | \.-> )'+re.escape(b)+r'\b',line))
        assert ('stroke-dasharray' in path.attrib)==(' .-> ' in source_line),('dashed relationship',i)
        assert ('marker-end' in path.attrib)==(' --- ' not in source_line),('arrow direction',i)
        routes.append((i,route))
    labels=[(''.join(e.itertext()),tuple(map(float,e.attrib['data-label-box'].split(','))))
            for e in root.iter() if 'data-label-box' in e.attrib]
    collisions=[]
    for i,(label,box) in enumerate(labels):
        for other,other_box in labels[i+1:]:
            if boxes_overlap(box,other_box):collisions.append(f'labels overlap: {label} / {other}')
        for key,node_box in boxes.items():
            if key not in containers and boxes_overlap(box,node_box):
                collisions.append(f'label overlaps node: {label} / {key}')
        for key,route in routes:
            if any(segment_hits_box(a,b,box) for a,b in zip(route,route[1:])):
                collisions.append(f'label overlaps route: {label} / {key}')
    for i,key in enumerate(keys):
        if key in containers:continue
        for other in keys[i+1:]:
            if other not in containers and boxes_overlap(boxes[key],boxes[other]):
                collisions.append(f'nodes overlap: {key} / {other}')
        for index,route in routes:
            if any(segment_hits_box(a,b,boxes[key]) for a,b in zip(route,route[1:])):
                collisions.append(f'route crosses node: {index} / {key}')
    # Conservative font bounds also cover section titles and standalone notes.
    def text_box(element):
        size=float(element.attrib['font-size'])
        text=''.join(element.itertext())
        width=sum(size*(1.03 if ord(char)>255 else .60) for char in text)
        x=float(element.attrib['x']);y=float(element.attrib['y'])
        anchor=element.attrib.get('text-anchor','start')
        if anchor=='middle':x-=width/2
        elif anchor=='end':x-=width
        return x,y-size,width,size*1.2

    texts=[(''.join(e.itertext()),text_box(e)) for e in root.iter(NS+'text')]
    canvas_width,canvas_height=map(float,root.attrib['viewBox'].split()[2:])
    for i,(text,box) in enumerate(texts):
        x,y,w,h=box
        if not (0<=x and 0<=y and x+w<=canvas_width and y+h<=canvas_height):
            collisions.append(f'text outside canvas: {text}')
        for other,other_box in texts[i+1:]:
            if boxes_overlap(box,other_box):collisions.append(f'texts overlap: {text} / {other}')
        for index,route in routes:
            if any(segment_hits_box(a,b,box) for a,b in zip(route,route[1:])):
                collisions.append(f'text overlaps route: {text} / {index}')
    for key,element in nodes.items():
        if key in containers:continue
        x,y,w,h=boxes[key]
        for text in element.iter(NS+'text'):
            a,b,c,d=text_box(text)
            if not (x+8<=a and y+8<=b and a+c<=x+w-8 and b+d<=y+h-8):
                collisions.append(f'text outside node: {key} / {text.text}')
    for element in root.iter():
        if 'data-label-box' not in element.attrib:continue
        x,y,w,h=map(float,element.attrib['data-label-box'].split(','))
        for text in element.iter(NS+'text'):
            a,b,c,d=text_box(text)
            if not (x<=a and y<=b and a+c<=x+w and b+d<=y+h):
                collisions.append(f'text outside label: {text.text}')
    assert not collisions,'\n'.join(collisions)


def main():
    expected_names={p.stem for p in INPUT.glob('*.mmd')}
    actual_names={p.stem for p in ROOT.glob('*.svg')}
    results=[];errors=[]
    for name in sorted(expected_names|actual_names):
        if name not in expected_names or name not in actual_names:
            errors.append(name+': missing matching SVG or Mermaid source');continue
        try:results.append(validate(ROOT/(name+'.svg')))
        except (AssertionError,ET.ParseError,KeyError,ValueError) as exc:
            errors.append(name+': '+str(exc))
    dn002=validate_dn002()
    payload={'status':'FAIL' if errors or dn002['differences'] else 'PASS',
             'svg_coverage':{'status':'FAIL' if errors else 'PASS','differences':errors},
             'files':len(results),'total_edges':sum(x['edges'] for x in results),
             'results':results,'dn002_consistency':dn002}
    (ROOT/'validation.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps(payload,ensure_ascii=False,indent=2))
    return 0 if payload['status']=='PASS' else 1

if __name__=='__main__':
    sys.exit(main())
