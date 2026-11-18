const assert=require('node:assert/strict'),fs=require('fs'),vm=require('vm'),crypto=require('node:crypto');
const root=__dirname+'/../.work/test/decoded/assets/js/';
function environment(fail=false){
 const calls=[],files={},timers=[];
 const c={console,Promise,Map,TextDecoder,TextEncoder,Uint8Array,AbortController,crypto:crypto.webcrypto,
  setTimeout:(fn,ms)=>{const t=setTimeout(fn,ms);t.unref();timers.push(t);return t;},clearTimeout,
  pref:()=> 'true',Store:{get:()=>({}),set:()=>{}},ICON:{star:'*'},
  Cast:{norm:s=>s.toLowerCase(),config(){},keyNow:()=> 'old-secret',_key:'old-secret'},
  SInfo:{MIN_TMDB_MIN:15},Dual:{origin:x=>({provider:x.provider||'one',id:String(x.stream_id||x.series_id).replace('L2_','')})},
  genreList:x=>x,siQuery:q=>({q}),document:{body:{contains:()=>true}},$$:()=>[],
  fetch:async url=>{calls.push(url);assert(!url.includes('api.themoviedb.org')); if(fail||!files[url.split('/').pop()])throw Error('offline');
   let b=Buffer.from(files[url.split('/').pop()]);return{ok:true,headers:{get:()=>String(b.length)},arrayBuffer:async()=>b.buffer.slice(b.byteOffset,b.byteOffset+b.byteLength)};}
 };c.window=c;vm.createContext(c);vm.runInContext(fs.readFileSync(root+'tmdb.js','utf8'),c);vm.runInContext(fs.readFileSync(root+'central-meta.js','utf8'),c);
 function fixture(){
  let meta={'movie:2':{id:2,type:'movie',title:'Dune',original:'Dune',year:'2021',genres:['Sci-Fi'],va:8.2,vc:200,cast:[]},
    'movie:3':{id:3,type:'movie',title:'Dune',original:'Dune',year:'1984',genres:['Sci-Fi'],va:6.2,vc:200,cast:[]}};
  let maps={id:{'one:movie:1':'movie:2','premiumtv:movie:1':'movie:3'},title:{'movie|dune':{'movie:2':'2021','movie:3':'1984'}},meta};
  let manifest={version:1,maps:{}};
  for(const[kind,map]of Object.entries(maps)){
    let count=kind==='meta'?1024:256,groups={};for(const[k,v]of Object.entries(map)){let b=c.CentralMeta.bucket(k,count);(groups[b]??={})[k]=v;}
    let chunks={};for(const[b,items]of Object.entries(groups)){let raw=JSON.stringify({version:1,items}),hash=crypto.createHash('sha256').update(raw).digest('hex'),file=kind+'-'+b+'-'+hash.slice(0,16)+'.json';files[file]=raw;chunks[b]={file,bytes:Buffer.byteLength(raw),sha256:hash};}
    manifest.maps[kind]={count,chunks};
  }files['manifest.json']=JSON.stringify(manifest);
 }fixture();return{c,calls,files,timers};
}
(async()=>{
 let {c,calls,files,timers}=environment();const C=c.CentralMeta;
 assert.equal(C.provider({rating:'8,3'}),8.3);assert.equal(C.provider({rating_5based:'4.5'}),9);
 for(const x of ['0','-1','11','8abc','NaN',''])assert.equal(C.provider({rating:x}),null);
 assert.equal(c.Cast.keyNow(),'');assert.equal(c.Tmdb.key(),'');assert.equal(await c.Tmdb.req('/search/movie'),null);assert.equal(calls.length,0);
 const [a,b]=await Promise.all([C.lookup('movie',{name:'Dune',year:'2021',stream_id:1}),C.lookup('movie',{name:'Dune',year:'2021',stream_id:1})]);
 assert.equal(a.id,2);assert.equal(b.id,2);assert.equal(calls.filter(x=>x.endsWith('manifest.json')).length,1);
 assert.equal(new Set(calls).size,calls.length,'in-flight requests deduplicated');
 let n=calls.length;await C.lookup('movie',{name:'Dune',year:'2021',stream_id:1});assert.equal(calls.length,n,'memory cache');
 assert.equal((await C.lookup('movie',{name:'Dune',year:'1984',stream_id:1,provider:'premiumtv'})).id,3,'same provider ID does not cross lines');
 assert.equal(await C.lookup('movie',{name:'Other',year:'2021',stream_id:1}),null,'reused ID wrong title');
 assert.equal(await C.byTitle('movie','Dune',''),null,'unknown-year remake ambiguous');
 assert.equal(await C.byTitle('movie','Dune','2000'),null,'wrong year');
 assert.equal((await C.byTitle('movie','(DE-) -6- Dune (2021) -1080p','')).id,2,'provider prefixes and quality tags');
 assert.equal(await C.lookup('movie',{name:'Dune',year:'2021',stream_id:1,tmdb_id:3}),null,'conflicting explicit TMDB ID');
 assert.equal(C.valid({id:2,type:'movie',title:'Dune',year:'2021'},'series',{q:'Dune',year:'2021'}),false);
 const bad=environment();Object.keys(bad.files).filter(x=>x.startsWith('meta-')).forEach(x=>bad.files[x]='{"version":1,"items":{}}');
 assert.equal(await bad.c.CentralMeta.byTitle('movie','Dune','2021'),null,'tampered digest rejected');
 const off=environment(true);assert.equal(await off.c.CentralMeta.lookup('movie',{name:'Dune',year:'2021',stream_id:1}),null,'offline is nonfatal');
 assert.equal(off.c.CentralMeta.provider({rating:'7'}),7,'provider fallback independent of network');
 for(const x of [...timers,...bad.timers,...off.timers])clearTimeout(x);
 console.log('PASS central metadata: identity, remakes, two providers, rating normalization, no TMDB calls, digest, memory/in-flight cache, offline fallback');
})().catch(e=>{console.error(e);process.exitCode=1;});
