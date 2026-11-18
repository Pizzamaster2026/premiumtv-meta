/* Test6: customer devices never call the TMDB API. Public GitHub cache only.
 * Provider-ID matches are checked against the current item's identity; no
 * category changes, no list/info scans, no dependency of login on this cache.
 */
(function () {
  'use strict';
  var BASE = 'https://raw.githubusercontent.com/Pizzamaster2026/premiumtv-meta/main/data/client-v1/';
  var DAY = 86400000, mem = new Map(), pending = {}, queue = [], busy = 0, manifestPromise;
  var dbPromise, writing = Promise.resolve(), memBytes = 0, MAX_BYTES = 8 * 1024 * 1024;
  function norm(s) { return String(s || '').toLowerCase().replace(/ß/g, 'ss').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').replace(/[^a-z0-9]+/g, ' ').trim(); }
  function bucket(s, n) { var h = 5381; for (var i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0; return String(h % n); }
  function remember(k, r) {
    if (mem.has(k)) { memBytes -= mem.get(k).bytes; mem.delete(k); }
    mem.set(k, r); memBytes += r.bytes;
    while (memBytes > MAX_BYTES && mem.size > 1) { var old = mem.keys().next().value; memBytes -= mem.get(old).bytes; mem.delete(old); }
  }
  function db() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise(function (resolve) {
      var timer = setTimeout(function () { resolve(null); }, 2000);
      try {
        var r = indexedDB.open('ptv-central-meta-v1', 1);
        r.onupgradeneeded = function () { r.result.createObjectStore('files', { keyPath:'key' }); };
        r.onsuccess = function () { clearTimeout(timer); resolve(r.result); };
        r.onerror = r.onblocked = function () { clearTimeout(timer); resolve(null); };
      } catch (e) { clearTimeout(timer); resolve(null); }
    }); return dbPromise;
  }
  function read(k) {
    if (mem.has(k)) { var r = mem.get(k); mem.delete(k); mem.set(k, r); return Promise.resolve(r); }
    return db().then(function (d) { return new Promise(function (resolve) {
      if (!d) return resolve(null);
      var timer = setTimeout(function () { resolve(null); }, 2000);
      try { var q = d.transaction('files').objectStore('files').get(k);
        q.onsuccess = function () { clearTimeout(timer); var r = q.result; if (r) remember(k,r); resolve(r || null); };
        q.onerror = function () { clearTimeout(timer); resolve(null); };
      } catch (e) { clearTimeout(timer); resolve(null); }
    }); });
  }
  function write(k, value, bytes) {
    var r = { key:k, value:value, bytes:bytes, time:Date.now() }; remember(k,r);
    writing = writing.catch(function () {}).then(function () { return db(); }).then(function (d) {
      return new Promise(function (resolve) {
        if (!d) return resolve();
        try {
          var tx = d.transaction('files','readwrite'), s = tx.objectStore('files'); s.put(r);
          var timer = setTimeout(function () { try { tx.abort(); } catch (e) {} resolve(); }, 3000);
          tx.oncomplete = tx.onerror = tx.onabort = function () { clearTimeout(timer); resolve(); };
          var q = s.getAll(); q.onsuccess = function () {
            var rows = q.result.sort(function(a,b) { return b.time-a.time; }), sum = 0;
            rows.forEach(function (x) { sum += x.bytes || 0; if (sum > MAX_BYTES) s.delete(x.key); });
          };
        } catch (e) { resolve(); }
      });
    });
  }
  function network(file, limit, digest) {
    return new Promise(function (resolve) {
      if (queue.length >= 64) return resolve(null);
      queue.push({file:file, limit:limit, digest:digest, resolve:resolve}); pump();
    });
  }
  function pump() {
    if (busy >= 2 || !queue.length) return;
    var task = queue.shift(); busy++;
    var controller = new AbortController(), timer = setTimeout(function () { controller.abort(); }, 8000);
    fetch(BASE + task.file, {signal:controller.signal, credentials:'omit', redirect:'error', cache:'no-cache'})
      .then(async function (r) {
        if (!r.ok || +(r.headers.get('content-length') || 0) > task.limit) throw Error('cache response');
        var bytes;
        if (r.body && r.body.getReader) {
          var reader = r.body.getReader(), parts = [], total = 0;
          while (true) { var p = await reader.read(); if (p.done) break; total += p.value.length;
            if (total > task.limit) { controller.abort(); throw Error('size'); } parts.push(p.value); }
          bytes = new Uint8Array(total); var offset = 0; parts.forEach(function (p) { bytes.set(p,offset); offset += p.length; });
        } else { bytes = new Uint8Array(await r.arrayBuffer()); if (bytes.length > task.limit) throw Error('size'); }
        if (task.digest) {
          var hash = new Uint8Array(await crypto.subtle.digest('SHA-256',bytes));
          var hex = Array.from(hash).map(function (n) { return n.toString(16).padStart(2,'0'); }).join('');
          if (hex !== task.digest) throw Error('digest');
        }
        var value = JSON.parse(new TextDecoder().decode(bytes)); return {value:value,bytes:bytes.length};
      }).then(task.resolve, function () { task.resolve(null); }).finally(function () { clearTimeout(timer); busy--; pump(); });
  }
  function manifest() {
    if (manifestPromise) return manifestPromise;
    manifestPromise = read('manifest').then(function (old) {
      if (old && Date.now()-old.time < 6*3600000) return old.value;
      return network('manifest.json',512*1024).then(function (r) {
        if (r && r.value.version === 1 && r.value.maps && r.value.maps.meta && r.value.maps.id && r.value.maps.title) {
          write('manifest',r.value,r.bytes); return r.value;
        }
        return old && Date.now()-old.time < 30*DAY ? old.value : null;
      });
    });
    // Failed manifests are retried on the next screen after a five-minute cooldown.
    setTimeout(function () { manifestPromise = null; }, 5*60000);
    return manifestPromise;
  }
  function chunk(kind,key) {
    return manifest().then(function (m) {
      if (!m) return null;
      var map = m.maps[kind]; if (!map || !Number.isInteger(map.count) || map.count < 1 || map.count > 2048) return null;
      var item = map.chunks[bucket(key,map.count)];
      if (!item || !/^(id|title|meta)-\d+-[a-f0-9]{16}\.json$/.test(item.file) || !/^[a-f0-9]{64}$/.test(item.sha256) || item.bytes > 2*1024*1024) return null;
      if (pending[item.file]) return pending[item.file];
      var job = read(item.file).then(function (old) {
        if (old) return old.value;
        return network(item.file,2*1024*1024,item.sha256).then(function (r) {
          if (!r || r.value.version !== 1 || !r.value.items) return null;
          write(item.file,r.value,r.bytes); return r.value;
        });
      });
      pending[item.file] = job;
      job.then(function () { delete pending[item.file]; }); return job;
    }).then(function (r) { return r && r.items ? r.items[key] || null : null; }).catch(function () { return null; });
  }
  function clean(name,year) {
    var s=String(name||'');
    for(var i=0;i<6;i++){
      var old=s;
      s=s.replace(/^\s*\(\s*(?:DE|EN|GER|ENG|DEU|US|FR|IT|ES|TR|AT|CH|NL|PL|RU|UA|GR|MULTI)\s*-?\s*\)\s*/i,'');
      s=s.replace(/^\s*-?\d{1,4}\s*[-–.:|)/]+\s+/,'');
      if(s===old)break;
    }
    s=s.replace(/(?:^|[\s.\-])(?:Multi(?:[\s-]?Sub(?:s|bed)?)?|Multi(?:[\s-]?Audio)?|Multi(?:[\s-]?Lang(?:uage)?)?|WEB-?DL|WEBRip|Blu-?Ray|REMUX|TRUEHD)(?=\s|$|[|\-–.])/gi,' ');
    s=s.replace(/(?:^|[\s.\-])(?:1080p|720p|2160p|3D)(?=\s|$|[|\-–.])/g,' ');
    return Tmdb.clean(s,year);
  }
  function yearOK(a,b) { return /^\d{4}$/.test(String(a)) && /^\d{4}$/.test(String(b)) && Math.abs(+a-+b)<=1; }
  function convert(e) {
    if (!e || !e.id || !['movie','series'].includes(e.type) || !e.title) return null;
    return {id:+e.id, type:e.type==='series'?'tv':'movie', va:rating(e.va,10)||0,vc:+e.vc||0,
      ov:String(e.overview||''), pp:/^\/[\w.-]+\.(jpg|jpeg|png|webp)$/.test(e.poster||'')?e.poster:'',
      names:Array.isArray(e.genres)?e.genres:[], g:[], rt:+e.runtime||0, y:e.year, yv:true,
      cast:(e.cast||[]).map(function(c){return c.name;}), credits:e.cast||[], t:Date.now(), central:true};
  }
  function valid(e,type,c,x) {
    if (!e || e.type !== type) return false;
    var known = c.year;
    if (known && !yearOK(known,e.year)) return false;
    var id = x && String(x.tmdb_id || x.tmdb || '');
    if (id && /^[1-9][0-9]*$/.test(id)) return +id === +e.id;
    return !!norm(c.q) && [e.title,e.original].some(function(t){return norm(t)===norm(c.q);});
  }
  var matched = new Map();
  function memo(type,c,e) { var k = type+'|'+norm(c.q)+'|'+c.year; matched.set(k,e); if (matched.size>1500) matched.delete(matched.keys().next().value); return e; }
  function byTitle(type,name,year) {
    var c = clean(name,year), q = norm(c.q); if (!q) return Promise.resolve(null);
    return chunk('title',type+'|'+q).then(function (refs) {
      var keys = Object.keys(refs||{}).filter(function(k){return !c.year || yearOK(c.year,refs[k]);});
      if (keys.length !== 1) return null;
      return chunk('meta',keys[0]).then(function (e) {
        // This exact alias was revalidated by the exporter, including umlaut variants.
        return e && e.type===type && (!c.year||yearOK(c.year,e.year)) ? memo(type,c,convert(e)) : null;
      });
    });
  }
  function lookup(type,x,info) {
    var year = (info && (info.releasedate||info.release_date||info.releaseDate)) || x.year || x.releaseDate;
    var c = clean(x.name,year), origin = Dual.origin(x,type==='series'?'series':'vod');
    var key = origin.provider+':'+type+':'+origin.id, explicit=String(x.tmdb_id||x.tmdb||'');
    var initial=/^[1-9][0-9]*$/.test(explicit) ? chunk('meta',(type==='series'?'tv:':'movie:')+explicit) : chunk('id',key).then(function (ref) { return ref ? chunk('meta',ref):null; });
    return initial.then(function (e) {
      // Reused provider IDs, renamed titles and conflicting years cannot silently borrow metadata.
      if (valid(e,type,c,x) && (c.year || /^[1-9][0-9]*$/.test(String(x.tmdb_id||x.tmdb||'')))) return memo(type,c,convert(e));
      return byTitle(type,x.name,year).then(function(hit){return /^[1-9][0-9]*$/.test(explicit)&&hit&&hit.id!==+explicit?null:hit;});
    }).catch(function () { return null; });
  }
  function rating(v,max) { if (!/^[0-9]+(?:[.,][0-9]+)?$/.test(String(v).trim())) return null;
    var n=Number(String(v).replace(',','.'));return n>0&&n<=max?n:null; }
  function provider(x) { x=x||{};var n=rating(x.rating_5based,5);return n!==null?n*2:rating(x.rating,10); }
  function badge(n,source) { return '<span class="'+(source==='TMDB'?'tmdbr':'provider-rating')+'" title="'+source+'">'+ICON.star+'<b>'+Tmdb.fmt(n)+'/10</b><em> '+source+'</em></span>'; }
  function providerBadge(x) { var n=provider(x); return !Tmdb.ratingsOn()||n===null?'':badge(n,'Anbieter'); }
  function detailProvider(info,x) { return providerBadge(provider(info)!==null?info:x); }
  window.CentralMeta = {lookup:lookup, byTitle:byTitle, norm:norm, bucket:bucket, valid:valid, provider:provider, providerBadge:providerBadge, detailProvider:detailProvider};

  // Disable every original API path, including actor photos, EPG and episode stills.
  // Static image.tmdb.org artwork remains ordinary image loading, not API calls.
  Cast.config=function(){this._key='';}; Cast.keyNow=function(){return '';}; Cast._key='';
  Tmdb.key=function(){return '';}; Tmdb.on=function(){return true;};
  Tmdb.req=function(){return Promise.resolve(null);}; Tmdb.pump=function(){};
  Tmdb._q=[]; Tmdb._m={};
  Tmdb.match=byTitle;
  Tmdb.cachedFor=function(type,name,year){var c=clean(name,year);return matched.get(type+'|'+norm(c.q)+'|'+c.year)||null;};
  var originalGenres=Tmdb.genres;
  Tmdb.genres=function(type,e){return e&&Array.isArray(e.names)?genreList(e.names):originalGenres.call(this,type,e);};
  Tmdb.runtime=function(e){return Promise.resolve(e&&e.rt||0);};
  Tmdb.plotEn=function(){return Promise.resolve('');};
  Tmdb.badge=function(e){return this.hasRating(e)?badge(e.va,'TMDB'):'';};
  Tmdb.credits=function(type,e){var out={};(e&&e.credits||[]).forEach(function(c){
    if(c.name)out[Cast.norm(c.name)]={id:c.id||0,u:/^\/[\w.-]+\.(jpg|jpeg|png|webp)$/.test(c.photo||'')?'https://image.tmdb.org/t/p/w185'+c.photo:''};
  });return Promise.resolve(out);};
  Tmdb.card=function(c){
    var type=c.dataset.open,x=type==='series'?Data.findSeries(c.dataset.id):Data.findVod(c.dataset.id);
    if(!x)return; var image=c.querySelector('.pimg')||c;
    if(Tmdb.ratingsOn()&&!c.querySelector('.provider-rating')&&provider(x)!==null)image.insertAdjacentHTML('beforeend',providerBadge(x));
    lookup(type,x).then(function(e){
      if(!e||!document.body.contains(c))return;
      var gt=c.querySelector('.gtags'); if(gt&&!infoGenres(type,x).length&&e.names.length)gt.innerHTML=genreTags(type,x,Tmdb.genres(type,e));
      if(Tmdb.ratingsOn()&&provider(x)===null&&!c.querySelector('.tmdbr')&&Tmdb.hasRating(e))image.insertAdjacentHTML('beforeend',Tmdb.badge(e));
      var img=c.querySelector('img.pi,img.lpost'); if((!img||img.classList.contains('broken'))&&e.pp){
        if(img)img.remove();image.insertAdjacentHTML('afterbegin','<img class="pi tmdbp" src="'+esc(Tmdb.poster(e,'w185'))+'" alt="" onerror="this.remove()">');
      }
    });
  };
  Tmdb.detail=function(type,x,info,el){
    lookup(type,x,info).then(function(e){
      if(!e||!document.body.contains(el.meta))return;
      if(Tmdb.ratingsOn()&&!el.meta.querySelector('.provider-rating,.tmdbr')&&Tmdb.hasRating(e))el.meta.insertAdjacentHTML('beforeend',Tmdb.badge(e));
      if(!infoGenres(type,x).length&&e.names.length){
        var holder=type==='movie'?document.querySelector('.chips'):el.meta;
        if(holder&&!holder.querySelector('.central-genres'))holder.insertAdjacentHTML('beforeend','<span class="central-genres">'+Tmdb.genres(type,e).map(function(g){return '<span class="chipg">'+esc(g)+'</span>';}).join(' ')+'</span>');
      }
      if(el.plot&&!String(el.plotText||'').trim()&&e.ov)el.plot.textContent=e.ov;
      if(!el.hasPoster&&e.pp&&el.setPoster)el.setPoster(Tmdb.poster(e));
      if(el.setRuntime&&e.rt)el.setRuntime(e.rt);
      if(el.cast)Tmdb.credits(type,e).then(function(m){if(document.body.contains(el.cast))Cast.applyCredits(el.cast,m);});
      if(el.onMatch)el.onMatch(e);
    });
  };
  SInfo.tmdb=function(title,dur){
    if(dur&&dur<this.MIN_TMDB_MIN)return Promise.resolve(null);
    var q=siQuery(title);return byTitle('movie',q.q,'').then(function(e){return e||byTitle('series',q.q,'');});
  };
  var strip=Tmdb.strip;Tmdb.strip=function(){strip.call(this);$$('.provider-rating').forEach(function(b){b.remove();});};
})();
