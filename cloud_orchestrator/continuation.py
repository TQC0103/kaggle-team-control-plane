"""Pure, CPU-only source staging for already-approved DF63/DF65 chunks.

No remote APIs, ownership changes or scientific overrides.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import re

ALLOWED_FIELDS = {'name','start','stop','parent_job','checkpoint_sha256','parent_payload_filename'}


def literal(tree, name):
    nodes = [n for n in tree.body if isinstance(n,ast.Assign)
             and any(isinstance(t,ast.Name) and t.id==name for t in n.targets)]
    if len(nodes)!=1 or len(nodes[0].targets)!=1:
        raise ValueError('one exact literal assignment required')
    return ast.literal_eval(nodes[0].value)


def non_config(tree):
    t=copy.deepcopy(tree)
    t.body=[n for n in t.body if not (isinstance(n,ast.Assign)
            and any(isinstance(x,ast.Name) and x.id=='CFG' for x in n.targets))]
    return ast.dump(t)


def stage_child(source, native, *, name, stop, boundaries, payload_filename):
    tree=ast.parse(source)
    old=literal(tree,'CFG')
    signature=hashlib.sha256(source.encode()).hexdigest()
    checks=['full_state_RNG_history','source_split_PASS','frozen_ViT_PASS','best_weights_PASS','no_test']
    if (native.get('status')!='PASS' or any(native.get(k) is not True for k in checks)
            or native.get('source_sha256')!=signature
            or native.get('iteration')!=old['stop'] or native.get('seed')!=old['seed']
            or not re.fullmatch(r'[a-f0-9]{64}',str(native.get('checkpoint_sha256','')))
            or old['arm'] not in {'DF63','DF65'} or old['max_iterations']!=15000
            or old.get('no_test') is not True):
        raise ValueError('exact native parent required before source staging')
    if (not isinstance(boundaries,list) or sorted(set(boundaries))!=boundaries
            or boundaries[-1]!=15000 or old['stop'] not in boundaries
            or old['stop']==15000 or boundaries[boundaries.index(old['stop'])+1]!=stop):
        raise ValueError('only next predeclared boundary allowed')
    if (not re.fullmatch(r'[a-z0-9-]{5,80}',name)
            or not re.fullmatch(r'(?:[a-f0-9]{64}\.bin|resume_latest\.pth)',payload_filename)):
        raise ValueError('unsafe transport name')
    cfg=copy.deepcopy(old)
    cfg.update(name=name,start=old['stop'],stop=stop,parent_job=native['job_id'],
               checkpoint_sha256=native['checkpoint_sha256'],parent_payload_filename=payload_filename)
    for node in tree.body:
        if isinstance(node,ast.Assign) and isinstance(node.targets[0],ast.Name) and node.targets[0].id=='CFG':
            node.value=ast.parse(repr(cfg),mode='eval').body
    child=ast.unparse(tree)+'\n'
    after=ast.parse(child)
    if (non_config(ast.parse(source))!=non_config(after)
            or literal(ast.parse(source),'FILES')!=literal(after,'FILES')
            or {k for k in old.keys()|cfg.keys() if old.get(k)!=cfg.get(k)}-ALLOWED_FIELDS):
        raise ValueError('scientific code or configuration drift')
    compile(child,'run.py','exec')
    return dict(source=child,source_sha256=hashlib.sha256(child.encode()).hexdigest(),
                config=cfg,FILES_exact=True,non_CFG_AST_exact=True,
                changed_fields=sorted(k for k in old.keys()|cfg.keys() if old.get(k)!=cfg.get(k)))
