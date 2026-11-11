// One names-only diagnostic, NOT an interactive-web acceptance/exit gate.
// upstream checked: deepseek-harness@b830ec950faba0a46dda564be3e2d854afed94f1:
// packages/host/plugin-inventory/{README.md,src/index.ts}; group rows are absent,
// and appReady is not a barrier for remote MCP connection/optional fibers.
import fs from 'node:fs';
import path from 'node:path';
export const name='northstar-native-startup';
export const inject=['loader','appReady'];
const freeze=map=>Object.freeze(Object.fromEntries(Object.entries(map).map(([key,ids])=>[key,Object.freeze([...ids])])));
const seven={context7:['mcp-context7'],exa:['mcp-exa'],deepwiki:['mcp-deepwiki'],
  'paper-search':['mcp-paper-search'],'brooks-review':['brooks-lint-skills'],
  'reference-checker':['reference-checker'],
  'session-search':['optional-session-search','optional-session-query-sqlite','optional-tool-session-query']};
// Keep the legacy export/default as web, including both upstream bundle leaves.
// billion-context@daa082ead8a8ead60311e29ad4627bff0f569fc1:dsh.bundle.patch.yml:21-25.
export const providers=freeze({...seven,
  'billion-context':['bili-native','bili-compaction-gate'],
});
const profileProviders=Object.freeze({web:providers,headless:freeze(seven),worker:providers,tui:freeze({...seven,openviking:['mcp-openviking']}),
  // LEAD #809/6630686131: same two pinned billion-context leaves as web.
  ws:freeze({...providers,weknora:['weknora']})});
const workstationWorkerProviders=freeze({...providers,weknora:['weknora']});
export function providersForProfile(profile='web',{workerWeKnora=false}={}){
  if(typeof profile!=='string'||!Object.hasOwn(profileProviders,profile))throw new Error('unsupported startup profile');
  // The installer supplies this only after #804's marked-workstation boundary.
  // Selection adds required observation; it never asserts that a provider is active.
  if(typeof workerWeKnora!=='boolean'||workerWeKnora&&profile!=='worker')throw new Error('unsupported worker knowledge selection');
  return workerWeKnora?workstationWorkerProviders:profileProviders[profile];
}
const phases=['pending','loading','active','failed','unloading',null];
export function providerStatus(inventory,entries,profile='web',options){
  // Loader Entry.id prefixes the configured id with colon-separated tree ids.
  // Remote snapshots omit groups; diagnostics retain the known group as such.
  return Object.fromEntries(Object.entries(providersForProfile(profile,options)).map(([provider,ids])=>[provider,ids.map(id=>{
    const actual=entries?entries.filter(entry=>entry.options.id===id):
      (inventory?.entries||[]).filter(row=>row.entryId===id||row.entryId?.endsWith(':'+id));
    const entryId=actual.length===1?(entries?actual[0].id:actual[0].entryId):null;
    const rows=(inventory?.entries||[]).filter(row=>entryId!==null&&row.entryId===entryId),row=rows.length===1?rows[0]:null;
    return {id,entryId,enabled:row?row.enabled===true:entries&&actual.length===1?!actual[0].disabled:null,
      phase:phases.includes(row?.fiberPhase)?row.fiberPhase:null,required:id!=='optional-session-search'};
  })]));
}
export function nonActive(status){
  return Object.entries(status).flatMap(([provider,rows])=>rows.filter(row=>row.required&&(!row.enabled||row.phase!=='active'))
    .map(row=>`${provider}/${row.id} enabled=${row.enabled} phase=${row.phase}`));
}
const officialBackend='DOCUMENTED-NON-USE';
const tuiOverlayIds=[...Object.keys(seven),'openviking','billion-context'];
const matchesId=(row,id)=>row.entryId===id||typeof row.entryId==='string'&&row.entryId.endsWith(':'+id);
// Names/config policy only; backend wording follows policy PR #894, never a
// login/readiness assertion. Disabled OAuth must be observed, not assumed.
export function tuiStartupContract(inventory,policy={}){
  const overlays=tuiOverlayIds.filter(id=>policy.overlays?.includes(id));
  const status=providerStatus(inventory,undefined,'tui');
  if(overlays.includes('billion-context'))status['billion-context']=providerStatus(inventory,undefined,'web')['billion-context'];
  const auth=(inventory?.entries||[]).filter(row=>matchesId(row,'dsh-tui-auth'));
  const hooks=(inventory?.entries||[]).filter(row=>matchesId(row,'orca-agent-hooks'));
  const forbidden=['openviking','mcp-openresearch','openresearch','weknora'];
  const leaks=(inventory?.entries||[]).filter(row=>row.enabled===true&&(
    forbidden.some(id=>matchesId(row,id))||!overlays.includes('openviking')&&matchesId(row,'mcp-openviking')||
    !overlays.includes('billion-context')&&['bili-native','bili-compaction-gate'].some(id=>matchesId(row,id))));
  const openvikingRows=(inventory?.entries||[]).filter(row=>matchesId(row,'mcp-openviking'));
  const openvikingStatus=status.openviking[0];
  const openvikingReason=!overlays.includes('openviking')?'selected overlay missing':
    openvikingRows.length===0?'inventory row missing':openvikingRows.length!==1?'inventory rows duplicate':
    openvikingStatus.enabled===true&&openvikingStatus.phase==='active'?null:
    `mcp-openviking enabled=${openvikingStatus.enabled} phase=${openvikingStatus.phase}`;
  const openviking=openvikingReason?{status:'HOLD',reason:openvikingReason}:{status:'active'};
  const holds=['openresearch','weknora','billion-context','orca-hooks'].flatMap(id=>{
    const rows=policy.holds?.filter(row=>row.id===id)||[];
    return rows.length===1?[{id,reason:typeof rows[0].reason==='string'?rows[0].reason.slice(0,2048):'unknown'}]:[];
  });
  if(openvikingReason)holds.push({id:'openviking',reason:openvikingReason});
  return {schemaVersion:1,profile:'tui',overlays,providers:status,holds,openviking,
    requestRetry:{mode:'normal',executor:'single inherited llm-retry'},
    dshTuiAuth:auth.length===1&&auth[0].enabled===false&&auth[0].fiberPhase===null?'disabled':'UNVERIFIED',
    backends:{claude:officialBackend,codex:officialBackend},
    orcaHooks:policy.orcaHooks==='attached'?
      hooks.length===1&&hooks[0].enabled===true&&hooks[0].fiberPhase==='active'?'attached':'UNVERIFIED':'HOLD',
    forbiddenActive:leaks.length};
}
export function validateTuiStartup(value){
  if(value?.schemaVersion!==1||value.profile!=='tui'||!Array.isArray(value.overlays)||new Set(value.overlays).size!==value.overlays.length||
    ![...Object.keys(seven),'openviking'].every(id=>value.overlays.includes(id))||value.overlays.some(id=>!tuiOverlayIds.includes(id))||
    !Array.isArray(value.holds)||!['openresearch','weknora'].every(id=>value.holds.some(row=>row.id===id&&typeof row.reason==='string'))||
    value.openviking?.status!=='active'||value.holds.some(row=>row.id==='openviking')||
    value.requestRetry?.mode!=='normal'||value.requestRetry.executor!=='single inherited llm-retry'||value.dshTuiAuth!=='disabled'||
    value.backends?.claude!==officialBackend||value.backends?.codex!==officialBackend||value.forbiddenActive!==0||
    !['attached','HOLD'].includes(value.orcaHooks)||value.orcaHooks==='HOLD'&&!value.holds.some(row=>row.id==='orca-hooks'))return false;
  const map={...profileProviders.tui,...(value.overlays.includes('billion-context')?{'billion-context':['bili-native','bili-compaction-gate']}:{})};
  if(!value.overlays.includes('billion-context')&&!value.holds.some(row=>row.id==='billion-context'))return false;
  return Object.keys(value.providers||{}).length===Object.keys(map).length&&Object.entries(map).every(([name,ids])=>
    Array.isArray(value.providers[name])&&value.providers[name].length===ids.length&&ids.every(id=>{
      const rows=value.providers[name].filter(row=>row.id===id);
      return rows.length===1&&(id==='optional-session-search'||rows[0].enabled===true&&rows[0].phase==='active'&&typeof rows[0].entryId==='string');
    }));
}
// Reuse acceptance's private directory/exclusive 0600 receipt convention.
// No raw runtime output or authentication URL enters this names-only file.
export function startupLogPath(home,pid=process.pid){
  return path.join(fs.realpathSync(home),'.northstar/native-state/dsh/logs','native-startup-'+pid+'.log');
}
function logWebDiagnostic(line){
  const home=fs.realpathSync(process.env.HOME),file=startupLogPath(home),directory=path.dirname(file);
  for(let current=directory;current!==home;current=path.dirname(current)){
    const stat=fs.lstatSync(current,{throwIfNoEntry:false});
    if(stat&&(!stat.isDirectory()||stat.isSymbolicLink()||stat.uid!==process.getuid()))throw new Error('unsafe log directory');
  }
  fs.mkdirSync(directory,{recursive:true,mode:0o700});
  if(fs.statSync(directory).mode&0o077)throw new Error('log directory must be private');
  const fd=fs.openSync(file,'wx',0o600);
  try{fs.writeSync(fd,line);}finally{fs.closeSync(fd);}
}
export function apply(ctx,config){
  const profile=config.profile===undefined?'web':config.profile;
  const options={workerWeKnora:config.workerWeKnora};
  providersForProfile(profile,options); // Unknown names never silently become web.
  const headless=profile==='headless'||profile==='worker',tui=profile==='tui';
  const startedAt=new Date().toISOString();let emitted=false;
  const emit=async(webCtx)=>{
    if(emitted)return;emitted=true;
    try{
      let inventory,entries=[],unavailable=false;
      try{
        const {readPluginInventory}=await import(config.inventoryModule);
        inventory=await readPluginInventory(ctx);entries=[...ctx.loader.entries()];
      }catch{unavailable=true;}
      const tuiReceipt=tui?tuiStartupContract(inventory,config.tui):null;
      const status=tui?tuiReceipt.providers:providerStatus(inventory,entries,profile,options),warnings=nonActive(status).map(row=>'WARN required provider not active: '+row);
      if(tui&&tuiReceipt.openviking.status==='HOLD')warnings.push('HOLD openviking: '+tuiReceipt.openviking.reason);
      if(tui&&tuiReceipt.dshTuiAuth!=='disabled')warnings.push('WARN dsh-tui-auth not observed disabled');
      if(tui&&tuiReceipt.forbiddenActive)warnings.push('WARN forbidden native provider active');
      if(unavailable)warnings.unshift('WARN inventory unavailable');
      const webServer={host:null,port:null};
      if(!headless&&!tui){
        try{webServer.host=['127.0.0.1','0.0.0.0','::1','::'].includes(webCtx.webServer.host)?webCtx.webServer.host:null;
          const port=webCtx.webServer.port;webServer.port=Number.isInteger(port)&&port>0&&port<=65535?port:null;}catch{}
        if(webServer.host!=='127.0.0.1'||webServer.port===null)warnings.push('WARN webServer loopback binding unavailable or not 127.0.0.1');
      }
      // Preserve final-answer stdout for headless; no listener or poller added.
      const line='northstar-dsh-startup '+JSON.stringify({schemaVersion:2,pid:process.pid,profile,startedAt,
        ...(!headless&&!tui?{webServer}:{}),providers:status,warnings,...(tui?{tui:tuiReceipt}:{})})+'\n';
      if(profile==='web'||tui){
        // appReady is not a remote-connection barrier: pending/loading facts
        // belong in the receipt, while actual failed fibers stay visible even
        // when the log sink refuses or cannot write.
        for(const warning of warnings.filter(row=>row.includes('phase=failed')||row.includes('unavailable')))
          process.stderr.write('dsh: '+warning+'\n');
        logWebDiagnostic(line);
      }else (headless?process.stderr:process.stdout).write(line);
    }catch{process.stderr.write('northstar-dsh-startup WARN: diagnostic unavailable; runtime left running\n');}
  };
  // rc.5 agent/src/index.ts:552 awaits serial observers before queued work.
  // rc.5 packages/host/webserver/src/index.ts:326-333,515-523: port exists
  // after Service.init binds; vendor/cordis/src/registry.ts:300-308 inject
  // waits for ACTIVE (fiber.ts:597-600), unlike a sibling property read
  // without inject (reflect.ts:144-166). Adopt the scoped callback pattern
  // in packages/bundle/web-app/src/index.ts:208-219; keep headless independent.
  if(headless)ctx.on('agent/created',emit);
  else if(tui)ctx.effect(()=>ctx.appReady.onReady(()=>{void emit();}));
  else{
    ctx.inject(['webServer'],webCtx=>{
      webCtx.effect(()=>webCtx.appReady.onReady(()=>{void emit(webCtx);}));
    });
    // A missing/failed server never activates the injection. Only call it
    // missing after Loader settles, using Cordis's documented strict get.
    ctx.effect(()=>ctx.appReady.onReady(()=>{
      const missing=()=>{if(ctx.get('webServer')===undefined)void emit();};
      void ctx.loader.await().then(missing,missing);
    }));
  }
}
