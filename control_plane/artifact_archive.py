"""Opt-in global, restart-safe private archival of completed job outputs.

No source deletion, GPU submissions or claims of scientific validation. A verified
archive means private full remote readback SHA-256, not merely upload success.
"""
from __future__ import annotations
import copy,csv,hashlib,io,json,os,re,shutil,tempfile,threading,time,zipfile
from pathlib import Path
from .datasets import _now,_safe_file
from .dataset_transfer import _account_cli
from .errors import ValidationError,ConflictError

TERMINAL={'verified','no_artifacts'}
SKIP_DIRS={'.git','.venv','venv','site-packages','__pycache__','node_modules','msd','msd_transport','data','wheels'}
SUFFIXES={'.pth','.pt','.ckpt','.h5','.hdf5','.npz','.npy','.bin','.safetensors','.json','.csv','.tsv','.txt','.log','.md','.sha256','.png','.pdf','.svg'}

def digest(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

class ArtifactArchiver:
    def __init__(self,service,cli_provider=None):
        self.service=service;self.root=service.data_dir/'artifact-archive';self.root.mkdir(exist_ok=True)
        self.path=self.root/'queue.json';self._lock=threading.RLock();self._stop=threading.Event();self._thread=None;self._active={}
        self._cli=cli_provider or (lambda aid:_account_cli(service,aid,max_text_chars=2000000))
        self.data=json.loads(self.path.read_text()) if self.path.exists() else {'config':{'enabled':False,'since':None,'include_job_ids':[],'account_ids':[]},'jobs':{},'restore':{}}
        self._save()

    def _save(self):
        if self._stop.is_set():return
        tmp=self.path.with_suffix('.tmp');tmp.write_text(json.dumps(self.data,indent=2),encoding='utf8')
        deadline=time.monotonic()+5
        while True:
            try:tmp.replace(self.path);break
            except PermissionError:
                if time.monotonic()>=deadline:raise
                time.sleep(.05)

    def _update(self,row,**kw):
        with self._lock:row.update(kw,updated_at=_now());self._save()

    def snapshot(self):
        with self._lock:
            return copy.deepcopy({'config':self.data['config'],'jobs':list(self.data['jobs'].values()),'active_workers':len(self._active),'restore_index':str(self.path),'local_cleanup':'never performed by this worker'})

    def configure(self,payload):
        self.service._reject_secrets(payload)
        with self._lock:
            config=self.data['config']
            enabled=payload.get('enabled',config['enabled'])
            if type(enabled) is not bool:raise ValidationError('enabled must be boolean')
            ids=payload.get('include_job_ids',[]);accounts=payload.get('account_ids',config['account_ids'])
            if not isinstance(ids,list) or not isinstance(accounts,list) or any(not isinstance(x,str) for x in ids+accounts):raise ValidationError('IDs must be lists of strings')
            for jid in ids:self.service.database.get_job(jid,event_limit=0)
            for aid in accounts:self.service.database.get_account(aid)
            config.update(enabled=enabled,account_ids=accounts,include_job_ids=sorted(set(config['include_job_ids']+ids)))
            if enabled and not config['since']:config['since']=_now()
            self._save()
        return self.snapshot()

    def start(self):
        if self._thread:return
        self._thread=threading.Thread(target=self._loop,name='kcp-auto-artifact-archive',daemon=True);self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:self._thread.join(2)

    def _loop(self):
        while not self._stop.is_set():
            try:self.tick()
            except Exception as exc:
                with self._lock:self.data['last_error']=type(exc).__name__;self._save()
            self._stop.wait(15)

    def tick(self):
        with self._lock:config=copy.deepcopy(self.data['config'])
        if not config['enabled']:return
        # Default is every future completed run, not an experiment allow-list.
        for job in self.service.database.list_jobs():
            if job['created_at']<(config['since'] or '') and job['id'] not in config['include_job_ids']:continue
            result=job.get('result') or {}
            output=result.get('output') or result.get('failure_output')
            if job['status'] not in {'succeeded','failed','cancelled'}:continue
            ready=bool(output) and not result.get('output_pending')
            if not ready and job['status']!='succeeded':continue
            with self._lock:
                if job['id'] not in self.data['jobs']:
                    self.data['jobs'][job['id']]={'job_id':job['id'],'source_account_id':job['account_id'],'kernel_slug':job['kernel_slug'],'job_state':job['status'],'output_dir':(output or {}).get('output_dir') or job['output_dir'],'output_ready':ready,'status':'pending' if ready else 'waiting_output','parts':[],'created_at':_now(),'scientific_validation':'not_assessed; archive integrity only'};self._save()
        with self._lock:
            for key,thread in list(self._active.items()):
                if not thread.is_alive():self._active.pop(key)
            busy={self.data['jobs'][key].get('account_id') for key in self._active}
            for row in self.data['jobs'].values():
                if len(self._active)>=2:break
                if row['status'] in TERMINAL or row['job_id'] in self._active or row.get('retry_after',0)>time.time():continue
                if row.get('account_id') in busy:continue
                # Reserve the account before launch, including recovery workers.
                if not row.get('account_id'):
                    operations=self.service.datasets.list_operations()
                    eligible=[a for a in self.service.database.list_accounts() if a['state']=='enabled' and a.get('credential_env_ref') and (not config['account_ids'] or a['id'] in config['account_ids']) and a['id'] not in busy and not any(o['account_id']==a['id'] and o['status'] not in {'succeeded','failed','interrupted'} for o in operations)]
                    eligible.sort(key=lambda a:a['id']!=row['source_account_id'])
                    if not eligible:continue
                    row['account_id']=eligible[0]['id'];self._save()
                busy.add(row['account_id'])
                thread=threading.Thread(target=self._run,args=(row,),name='kcp-archive-'+row['job_id'][-8:],daemon=True);self._active[row['job_id']]=thread;thread.start()

    def _plan(self,row):
        source=Path(row['output_dir']).resolve()
        if not source.is_relative_to(self.service.artifact_root.resolve()) or not source.is_dir():raise ValidationError('Output is not a collected KCP artifact directory')
        files=[]
        for directory,dirs,names in os.walk(source,followlinks=False):
            dirs[:]=[n for n in dirs if n.lower() not in SKIP_DIRS and not (Path(directory)/n).is_symlink()]
            for name in sorted(names):
                p=Path(directory)/name
                if p.suffix.lower() not in SUFFIXES:continue
                _safe_file(p);stat=p.stat()
                if stat.st_size==0:continue
                value=digest(p)
                files.append({'path':str(p),'relative_path':p.relative_to(source).as_posix(),'bytes':stat.st_size,'sha256':value,'name':value+'.bin'})
                if len(files)>10000:raise ValidationError('Artifact archive exceeds10000 files')
        if not files:self._update(row,status='no_artifacts');return
        # Same bytes within one run are uploaded once; retain every restore path.
        blobs={r['name']:r for r in files};parts=[];current=[];size=0
        for item in blobs.values():
            if current and size+item['bytes']>3*1024**3:parts.append(current);current=[];size=0
            current.append(item);size+=item['bytes']
        if current:parts.append(current)
        owner=self.service.database.get_account(row['account_id'])['kaggle_username']
        planned=[{'index':i,'dataset_ref':owner+'/kcp-artifacts-'+row['job_id'].removeprefix('job_')+'-'+str(i),'status':'pending','files':part,'verified':[]} for i,part in enumerate(parts,1)]
        self._update(row,files=files,parts=planned,status='archiving')

    def _stage(self,row,part):
        size=sum(f['bytes'] for f in part['files'])
        if shutil.disk_usage(self.service.allowed_source_root).free<20*1024**3+2*size:raise ValidationError('Waiting for disk reserve before archive staging/readback')
        stage=self.service.allowed_source_root/('_kcp_archive_'+row['job_id']+'_'+str(part['index']));stage.mkdir(exist_ok=True)
        for f in part['files']:
            source=Path(f['path']);_safe_file(source)
            if source.stat().st_size!=f['bytes'] or digest(source)!=f['sha256']:raise ValidationError('Artifact changed after frozen archive manifest')
            target=stage/f['name']
            if not target.exists():
                try:os.link(source,target)
                except OSError:shutil.copyfile(source,target)
            if digest(target)!=f['sha256']:raise ValidationError('Archive staging hash mismatch')
        manifest={'job_id':row['job_id'],'kernel_slug':row['kernel_slug'],'dataset_ref':part['dataset_ref'],'files':[f for f in row['files'] if f['name'] in {x['name'] for x in part['files']}],'local_eviction':False,'scientific_validation':row['scientific_validation']}
        p=stage/'artifact_manifest.json'
        if not p.exists():p.write_text(json.dumps(manifest,indent=2),encoding='utf8')
        expected=[{k:f[k] for k in ['name','bytes','sha256']} for f in part['files']]+[{'name':p.name,'bytes':p.stat().st_size,'sha256':digest(p)}]
        self._update(part,stage=str(stage),expected=expected)

    def _operation(self,row,part):
        key='auto-archive-'+row['job_id']+'-'+str(part['index'])
        found=[op for op in self.service.datasets.list_operations() if op['idempotency_key']==key]
        if found:
            op=found[0]
            if op['account_id']!=row['account_id'] or op['dataset_ref']!=part['dataset_ref']:raise ValidationError('Archive receipt identity mismatch')
            self._update(part,operation_id=op['id']);return op
        if part.get('upload_intent'):raise ValidationError('Archive upload intent has no reconciled operation; original retained')
        self._stage(row,part)
        selection=self.service.datasets.prepare({'source_dir':part['stage']})
        self._update(part,upload_intent=True,status='uploading')
        if self._stop.is_set():raise ConflictError('Archive paused before submission')
        try:
            op=self.service.datasets.submit({'kind':'upload','account_id':row['account_id'],'dataset_ref':part['dataset_ref'],'is_private':True,'readers':[],'license':'other','title':'Private run artifacts '+row['job_id'][-12:]+' '+str(part['index']),'selection_id':selection['selection_id'],'idempotency_key':key},'auto-artifact-archive')
        except ConflictError:
            self._update(part,upload_intent=False)
            raise
        self._update(part,operation_id=op['id']);return op

    def _listing(self,run,ref):
        found={};token=None;seen=set()
        for _ in range(100):
            cmd=['datasets','files',ref,'--csv','--page-size','1000']+(['--page-token',token] if token else [])
            raw=run(cmd,90);lines=raw.splitlines();start=next((i for i,line in enumerate(lines) if line.startswith('name,size')),None)
            if start is None:raise ValidationError('Invalid remote file listing')
            for f in csv.DictReader(io.StringIO('\n'.join(lines[start:]))):
                if f.get('name') and f.get('size') is not None:
                    if f['name'] in found:raise ValidationError('Duplicate remote file')
                    found[f['name']]=int(f['size'])
            match=re.search(r'^Next Page Token\s*=\s*(.+)$',raw,re.M)
            if not match:return found
            token=match[1].strip()
            if token in seen:raise ValidationError('Repeated page token')
            seen.add(token)
        raise ValidationError('Too many remote listing pages')

    def _remote_stamp(self,run,ref,metadata):
        stamp={k:metadata[k] for k in ['currentVersionNumber','versionNumber','lastUpdated','lastUpdatedDate'] if metadata.get(k) not in [None,'',0]}
        if stamp:return stamp
        # Kaggle's editable dataset-metadata.json normally omits the version.
        # The exact owned listing supplies lastUpdated, also used by Library
        # to invalidate stale readback reports after a remote version update.
        try:
            raw=run(['datasets','list','--mine','--csv','--search',ref.split('/')[1],'--page-size','100'],90)
            lines=raw.splitlines();start=next(i for i,line in enumerate(lines) if line.startswith('ref,'))
            for record in csv.DictReader(io.StringIO('\n'.join(lines[start:]))):
                if record.get('ref')==ref and record.get('lastUpdated'):return {'owned_lastUpdated':record['lastUpdated']}
        except (ValidationError,StopIteration):pass
        return {}

    def _verify(self,row,part,op):
        _,run=self._cli(row['account_id'])
        with tempfile.TemporaryDirectory(dir=self.root,prefix='verify-') as tmp:
            remote=self.service.datasets._remote_metadata(run,part['dataset_ref'],Path(tmp)/'meta')
            if remote['isPrivate'] is not True or remote.get('collaborators'):raise ValidationError('Archive is not private to the owning account')
            stamp=self._remote_stamp(run,part['dataset_ref'],remote)
            # Without a remote version marker, partial proofs cannot safely be
            # carried across restarts: redo readback instead of trusting stale bytes.
            if not stamp or part.get('remote_stamp')!=stamp:self._update(part,verified=[],remote_stamp=stamp)
            expected={f['name']:f for f in part['expected']}
            if self._listing(run,part['dataset_ref'])!={n:f['bytes'] for n,f in expected.items()}:raise ValidationError('Remote archive file set/size differs')
            self._update(part,status='verifying')
            for name,f in expected.items():
                if name in part['verified']:continue
                if self._stop.is_set():return False
                directory=Path(tmp)/'object';directory.mkdir(exist_ok=True)
                run(['datasets','download',part['dataset_ref'],'-f',name,'-p',str(directory),'-q'],3600)
                files=[p for p in directory.iterdir() if p.is_file()]
                if len(files)!=1:raise ValidationError('Unexpected readback layout')
                p=files[0];valid=p.stat().st_size==f['bytes'] and digest(p)==f['sha256']
                if not valid and zipfile.is_zipfile(p):
                    with zipfile.ZipFile(p) as z:
                        members=[x for x in z.infolist() if not x.is_dir()]
                        if len(members)==1 and Path(members[0].filename).name==name and members[0].file_size==f['bytes']:
                            with z.open(members[0]) as content:valid=hashlib.file_digest(content,'sha256').hexdigest()==f['sha256']
                if not valid:raise ValidationError('Remote archive SHA-256 mismatch')
                p.unlink();self._update(part,verified=part['verified']+[name])
            final=self.service.datasets._remote_metadata(run,part['dataset_ref'],Path(tmp)/'final-meta')
            final_stamp=self._remote_stamp(run,part['dataset_ref'],final)
            if final['isPrivate'] is not True or final.get('collaborators') or final_stamp!=stamp:
                self._update(part,verified=[])
                raise ValidationError('Remote archive version or visibility changed during readback')
        if {f['name']:(f['bytes'],f['sha256']) for f in op.get('files',[])}!={n:(f['bytes'],f['sha256']) for n,f in expected.items()}:raise ValidationError('Upload receipt SHA differs from archive manifest')
        if self._stop.is_set():return False
        self.service.dataset_library.register_verification({'account_id':row['account_id'],'dataset_ref':part['dataset_ref'],'operation_id':op['id'],'method':'full_remote_readback','files':part['expected'],'private_verified':True,'local_evicted':False,'local_eviction_pending':False},'auto-artifact-archive')
        with self._lock:
            for f in row['files']:
                if f['name'] in expected:self.data['restore'][f['path']]={'dataset_ref':part['dataset_ref'],'account_id':row['account_id'],'name':f['name'],'bytes':f['bytes'],'sha256':f['sha256'],'job_id':row['job_id']}
            part.update(status='verified',verified_at=_now());self._save()
        return True

    def _cleanup_stage(self,part):
        stage=Path(part['stage']).resolve()
        if not stage.is_relative_to(self.service.allowed_source_root.resolve()) or not stage.name.startswith('_kcp_archive_job_'):return
        # Only disposable staging links, never original job outputs.
        for f in part['files']:
            p=stage/f['name']
            if p.exists() and not p.is_symlink() and digest(p)==f['sha256']:p.unlink()

    def _run(self,row):
        try:
            if row.get('output_ready',True) is False:
                job=self.service.database.get_job(row['job_id'],event_limit=0)
                result=job.get('result') or {}
                if result.get('output_pending') or not result.get('output'):
                    # Reuse KCP's exact remote reconciliation/download path, not
                    # a new notebook submission. Handles app exit mid-download.
                    self.service._reconcile_recovered_jobs([row['job_id']],announce_restart=False)
                    job=self.service.database.get_job(row['job_id'],event_limit=0);result=job.get('result') or {}
                if result.get('output_pending') or not result.get('output'):raise ValidationError('Waiting for complete job output download; partial files are not archived')
                self._update(row,output_ready=True,output_dir=result['output'].get('output_dir') or job['output_dir'],status='pending')
            if not row['parts']:self._plan(row)
            if row['status']=='no_artifacts':return
            for part in row['parts']:
                if self._stop.is_set():return
                if part['status']=='verified':self._cleanup_stage(part);continue
                op=self._operation(row,part)
                if op['status'] in {'queued','running'}:self._update(row,status='uploading',retry_after=time.time()+15);return
                if op['status'] in {'interrupted','failed'} and op.get('create_started') is False:
                    self.service.datasets.resume_archive_upload(op['id']);self._update(row,status='resuming',retry_after=time.time()+15);return
                try:
                    if not self._verify(row,part,op):return
                    self._cleanup_stage(part)
                except Exception as exc:
                    # Only two exact404 metadata reads >=120s apart can reopen an
                    # interrupted create.403/timeouts never imply absence.
                    text=str(exc)
                    if op['status'] in {'interrupted','failed'} and '404' in text and ('GetDatasetMetadata' in text or 'Not Found' in text):
                        first=part.get('first_exact_404')
                        if first and time.time()-first>=120:
                            self.service.datasets.resume_archive_upload(op['id'],absence_confirmed=True);self._update(part,first_exact_404=None)
                        elif not first:self._update(part,first_exact_404=time.time())
                    raise
            self._update(row,status='verified',retry_after=0,error=None,verified_at=_now())
        except Exception as exc:
            message=str(exc)[:1000] if isinstance(exc,(ValidationError,ConflictError)) else 'Archive paused; local files retained ('+type(exc).__name__+')'
            self._update(row,status='waiting_reconciliation',error=message,retry_after=time.time()+120)
