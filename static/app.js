(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const categories = { homework: '作业通知', exam: '考试通知', course: '课程通知', activity: '活动通知' };
  const state = { tasks: [], user: null, csrf: '', category: 'all', search: '', status: 'all', sort: 'deadline', selectedId: null, editingId: null, deleteId: null, weekDate: null, loaded: false, favorites: new Set(), favoritesLoaded: false, favoritesLoading: false, favoritesBusy: false, favoritesError: '' };
  const zone = 'Asia/Shanghai';
  let refreshSequence = 0, toastTimer;
  let loginRole = 'user', authBusy = false, sessionRequest = null;
  let accountVersion = 0, favoritesSequence = 0, favoritesRequest = null;
  const attachmentLimit = 5, attachmentMaxBytes = 20 * 1024 * 1024;
  let editorFiles = [], editorAttachments = [], removedAttachmentIds = new Set(), editorBusy = false;
  let previewSequence = 0, previewRequest = null, previewObjectUrl = null;
  // Render user content as plain text to prevent stored HTML injection.
  function el(tag, cls, text) { const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=String(text);return n; }
  function icon(name) { const n=document.createElementNS('http://www.w3.org/2000/svg','svg');n.setAttribute('class','icon');n.setAttribute('aria-hidden','true');const u=document.createElementNS('http://www.w3.org/2000/svg','use');u.setAttribute('href','#i-'+name);n.append(u);return n; }
  const isAdmin = () => state.user?.role === 'admin';
  function formatDate(value) {return new Intl.DateTimeFormat('zh-CN',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(value));}
  function dateKey(value) {const p=new Intl.DateTimeFormat('en-CA',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date(value));const get=t=>p.find(v=>v.type===t).value;return get('year')+'-'+get('month')+'-'+get('day');}
  function localInput(value) {if(!value)return '';const p=new Intl.DateTimeFormat('en-GB',{timeZone:zone,hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date(value));return dateKey(value)+'T'+p.find(v=>v.type==='hour').value+':'+p.find(v=>v.type==='minute').value;}
  function deadlineState(task) {if(!task.deadline)return 'none';const diff=new Date(task.deadline)-Date.now();return diff<=0?'overdue':diff<=24*3600000?'soon':'upcoming';}
  function remaining(task) {if(!task.deadline)return '长期有效';const diff=new Date(task.deadline)-Date.now();if(diff<=0)return '已截止';const m=Math.floor(diff/60000),d=Math.floor(m/1440),h=Math.floor(m%1440/60);if(m<1)return '不足 1 分钟';if(d)return '剩余 '+d+' 天 '+h+' 小时';if(h)return '剩余 '+h+' 小时 '+m%60+' 分钟';return '剩余 '+m+' 分钟';}
  const countdown = task => el('span','countdown '+deadlineState(task),remaining(task));
  function badges(task) {const n=el('div','task-badges');n.append(el('span','tag tag-'+task.category,categories[task.category]));if(task.pinned){const p=el('span','tag tag-pin');p.append(icon('pin'),document.createTextNode('置顶'));n.append(p);}if(task.attachments?.length){const a=el('span','tag tag-attachment');a.append(icon('file'),document.createTextNode(task.attachments.length+' 个附件'));n.append(a);}return n;}
  function showError(id,message) {$(id).textContent=message||'';$(id).hidden=!message;}
  function toast(message) {clearTimeout(toastTimer);$('toastText').textContent=message;$('toast').hidden=false;toastTimer=setTimeout(()=>{$('toast').hidden=true;},4000);}
  function showDialog(id) {if(!$(id).open)$(id).showModal();}
  function closeDialog(id) {if(authBusy&&(id==='loginDialog'||id==='registerDialog'))return;if(editorBusy&&id==='editorDialog')return;$(id).close();}
  function clearFavorites(){
    favoritesSequence++;favoritesRequest=null;state.favorites=new Set();state.favoritesLoaded=false;
    state.favoritesLoading=false;state.favoritesBusy=false;state.favoritesError='';
  }
  function applySession(data){
    const changed=state.user?.id!==data.user?.id||state.csrf!==data.csrfToken;
    state.csrf=data.csrfToken;state.user=data.user;
    if(changed){accountVersion++;clearFavorites();}
    renderAccount();
    if(changed&&state.user)void refreshFavorites();
  }
  function refreshSession(){
    // Share the initial request so a slow guest response cannot replace a new login.
    if(!sessionRequest){
      const version=accountVersion;
      sessionRequest=(async()=>{
        let response;
        try{response=await fetch('/api/session',{headers:{Accept:'application/json'},credentials:'same-origin',cache:'no-store'});}
        catch{throw new Error('无法连接网站，请检查网络后重试。');}
        let data;
        try{data=await response.json();}catch{throw new Error('服务器响应异常，请稍后重试。');}
        if(!response.ok)throw new Error(data.error||'登录状态暂时无法获取，请稍后重试。');
        if(version===accountVersion)applySession(data);return data;
      })().finally(()=>{sessionRequest=null;});
    }
    return sessionRequest;
  }
  async function api(path,options={}) {
    const requestAccount=accountVersion;
    const multipart=typeof FormData!=='undefined'&&options.body instanceof FormData;
    const headers={Accept:'application/json',...(options.body&&!multipart?{'Content-Type':'application/json'}:{}),...(options.method&&options.method!=='GET'?{'X-CSRF-Token':state.csrf}:{})};
    let response,data;
    try{response=await fetch(path,{...options,headers,credentials:'same-origin',cache:'no-store'});}catch{throw new Error('无法连接网站，请检查网络后重试。');}
    try{data=await response.json();}catch{throw new Error('服务器响应异常，请稍后重试。');}
    if(!response.ok){
      if(response.status===401&&path!=='/api/login'&&requestAccount===accountVersion)applySession({user:null,csrfToken:state.csrf});
      if(response.status===403&&requestAccount===accountVersion){try{await refreshSession();}catch{/* Preserve original error. */}}
      const error=new Error(data.error||'操作失败，请稍后重试。');error.status=response.status;throw error;
    }
    return data;
  }
  function renderFavorites(){
    $('favoritesCount').hidden=!state.user||!state.favoritesLoaded;
    $('favoritesCount').textContent=state.loaded?state.tasks.filter(t=>state.favorites.has(t.id)).length:state.favorites.size;
    $('favoritesError').hidden=!state.user||!state.favoritesError;$('favoritesErrorText').textContent=state.favoritesError;
    $('retryFavoritesButton').disabled=state.favoritesLoading||state.favoritesBusy;
    if(state.loaded||state.category==='favorites')renderList();
    if($('detailDialog').open)updateDetail();
  }
  function refreshFavorites(){
    if(!state.user||state.favoritesBusy)return Promise.resolve();
    if(favoritesRequest?.version===accountVersion)return favoritesRequest.promise;
    const version=accountVersion,userId=state.user.id,seq=++favoritesSequence;
    state.favoritesLoading=true;state.favoritesError='';renderFavorites();
    const promise=(async()=>{
      try{
        const data=await api('/api/favorites');
        if(version!==accountVersion||seq!==favoritesSequence)return;
        if(data.userId!==userId){clearFavorites();renderFavorites();await refreshSession();return;}
        state.favorites=new Set(data.taskIds);state.favoritesLoaded=true;
      }catch(error){
        if(version===accountVersion&&seq===favoritesSequence)state.favoritesError='收藏同步失败：'+error.message;
      }finally{
        if(version===accountVersion&&seq===favoritesSequence){state.favoritesLoading=false;favoritesRequest=null;renderFavorites();}
      }
    })();
    favoritesRequest={version,promise};return promise;
  }
  function configureFavoriteButton(button,task){
    const saved=Boolean(state.user)&&state.favorites.has(task.id);
    const unavailable=Boolean(state.user)&&(!state.favoritesLoaded||state.favoritesLoading||state.favoritesBusy);
    const label=!state.user?'登录后收藏':state.favoritesLoading?'同步中…':!state.favoritesLoaded?'收藏暂不可用':saved?'取消收藏':'收藏';
    button.replaceChildren(icon('bookmark'),document.createTextNode(label));
    button.classList.toggle('is-favorited',saved);button.disabled=unavailable;
    button.setAttribute('aria-pressed',String(saved));button.setAttribute('aria-label',label+'：'+task.title);
    button.dataset.favoriteId=task.id;
  }
  async function toggleFavorite(taskId){
    if(!state.user){closeDialog('detailDialog');openLogin('user');toast('登录后即可收藏通知，在“我的收藏”中统一查看。');return;}
    if(authBusy||state.favoritesBusy||state.favoritesLoading)return;
    if(!state.favoritesLoaded){await refreshFavorites();return;}
    const version=accountVersion,userId=state.user.id,saved=state.favorites.has(taskId);
    // Serialize updates and invalidate older reads before changing private state.
    favoritesSequence++;state.favoritesBusy=true;renderFavorites();
    try{
      const data=await api('/api/favorites/'+taskId,{method:saved?'DELETE':'PUT',body:'{}'});
      if(version!==accountVersion)return;
      if(data.userId!==userId){clearFavorites();renderFavorites();await refreshSession();return;}
      if(data.favorited)state.favorites.add(taskId);else state.favorites.delete(taskId);
      state.favoritesError='';toast(data.favorited?'已加入我的收藏。':'已取消收藏。');
    }catch(error){
      if(version===accountVersion){toast(error.message);if(error.status===404){state.favorites.delete(taskId);void refreshTasks();}}
    }finally{
      if(version===accountVersion){state.favoritesBusy=false;renderFavorites();}
    }
  }
  async function refreshTasks() {
    const seq=++refreshSequence;$('syncStatus').textContent='正在同步…';
    try{const data=await api('/api/tasks');if(seq!==refreshSequence)return;state.tasks=data.tasks;state.loaded=true;$('loadError').hidden=true;$('syncStatus').textContent='已同步 · 每分钟更新';render();if(state.user)void refreshFavorites();}
    catch(error){if(seq!==refreshSequence)return;$('loadErrorText').textContent=error.message;$('loadError').hidden=false;$('syncStatus').textContent=state.loaded?'同步失败 · 显示上次内容':'连接失败';if(!state.loaded){if(state.category==='favorites')renderList();else $('taskList').replaceChildren(emptyState('暂时无法获取通知','点击重新加载，或检查电脑上的网站服务是否正在运行。'));$('recentPublished').replaceChildren(el('p','recent-empty','暂时无法获取最新通知，请重新加载。'));$('recentPublished').setAttribute('aria-busy','false');}}
    finally{if(seq===refreshSequence){$('taskList').setAttribute('aria-busy','false');renderWeekTasks();}}
  }
  function renderAccount() {
    const loggedIn=Boolean(state.user);
    $('userLoginButton').hidden=loggedIn;$('loginButton').hidden=loggedIn;$('logoutButton').hidden=!loggedIn;
    $('viewerBadge').parentElement.classList.toggle('is-signed-in',loggedIn);
    $('publishButton').hidden=!isAdmin();$('detailAdmin').hidden=!isAdmin();
    const label=loggedIn?state.user.username+' · '+(isAdmin()?'管理员':'普通用户'):'访客浏览';
    $('viewerText').textContent=label;$('viewerBadge').title=label;
    renderFavorites();
  }
  function filteredTasks() {
    const q=state.search.toLocaleLowerCase();
    return state.tasks.filter(t=>{if(state.category==='favorites'){if(!state.user||!state.favoritesLoaded||!state.favorites.has(t.id))return false;}else if(state.category!=='all'&&t.category!==state.category)return false;if(q&&!(t.title+' '+t.content).toLocaleLowerCase().includes(q))return false;const ds=deadlineState(t);if(state.status==='upcoming')return ds==='upcoming'||ds==='soon';return state.status==='all'||ds===state.status;}).sort((a,b)=>{
      if(a.pinned!==b.pinned)return a.pinned?-1:1;
      if(state.sort==='deadline'){const rank=t=>deadlineState(t)==='none'?1:deadlineState(t)==='overdue'?2:0;const r=rank(a)-rank(b);if(r)return r;if(a.deadline&&b.deadline){const delta=new Date(a.deadline)-new Date(b.deadline);if(delta)return rank(a)===2?-delta:delta;}}
      return new Date(b.createdAt)-new Date(a.createdAt)||b.id-a.id;
    });
  }
  function emptyState(title,message,actionText,action) {const n=el('div','empty-state'),sym=el('span','empty-symbol');sym.append(icon('bell'));n.append(sym,el('h3','',title),el('p','',message));if(action){const b=el('button','button button-light button-sm',actionText);b.type='button';b.addEventListener('click',action);n.append(b);}return n;}
  function renderList() {
    const tasks=filteredTasks(),focused=document.activeElement?.dataset?.taskId,favoriteFocus=document.activeElement?.dataset?.favoriteId;
    const favoritesView=state.category==='favorites';
    $('listHeading').textContent=favoritesView?'我的收藏':state.category==='all'?'全部事项':categories[state.category];
    $('resultCount').textContent=tasks.length;$('taskList').replaceChildren();
    if(favoritesView&&!state.user){$('resultCount').textContent='—';$('taskList').append(emptyState('登录后查看我的收藏','收藏跟随个人账号保存，只有你自己能查看和管理。','登录 / 注册',()=>openLogin('user')));return;}
    if(favoritesView&&!state.favoritesLoaded){
      $('resultCount').textContent='—';
      $('taskList').append(state.favoritesLoading?emptyState('正在加载我的收藏','请稍候，正在同步你的收藏记录。'):emptyState('暂时无法获取收藏','点击重试，或稍后重新打开我的收藏。','重试收藏',refreshFavorites));return;
    }
    if(favoritesView&&!state.loaded){$('resultCount').textContent='—';$('taskList').append(emptyState('正在获取收藏的通知','通知加载完成后，会在这里显示你的收藏。','重新加载',refreshTasks));return;}
    if(!tasks.length){
      if(favoritesView){
        const filtered=state.search||state.status!=='all';
        $('taskList').append(filtered?emptyState('没有找到符合条件的收藏','试试其他关键词，或清除筛选查看全部收藏。','清除筛选',()=>{state.search='';state.status='all';$('searchInput').value='';$('statusFilter').value='all';renderList();}):emptyState('还没有收藏的通知','点击通知卡片或详情中的“收藏”，把重要事项保存在这里。','浏览全部事项',resetFilters));
      }else{
        const filtered=state.category!=='all'||state.search||state.status!=='all';
        $('taskList').append(filtered?emptyState('没有找到相关事务','试试其他关键词，或清除筛选查看全部通知。','清除筛选',resetFilters):emptyState('班级的新安排，从这里开始','管理员发布通知后，大家就能在这里查看内容和截止时间。',isAdmin()?'发布第一条任务':'管理员登录',isAdmin()?()=>openEditor():state.user?null:()=>openLogin('admin')));
      }
      return;
    }
    for(const task of tasks){
      const card=el('article','task-card'),open=el('button','task-open');open.type='button';open.dataset.taskId=task.id;open.setAttribute('aria-label','查看详情：'+task.title);
      const head=el('div','task-card-head');head.append(badges(task),icon('arrow'));
      const footer=el('div','task-card-footer'),deadline=el('span','deadline-text');deadline.append(icon('clock'),document.createTextNode(task.deadline?formatDate(task.deadline)+' 截止':'无截止时间'));footer.append(deadline,countdown(task));
      open.append(head,el('h3','',task.title),el('p','task-excerpt',task.content),footer);open.addEventListener('click',()=>openDetail(task.id));card.append(open);
      const actions=el('div','card-actions'),favorite=el('button','text-button favorite-button');favorite.type='button';
      configureFavoriteButton(favorite,task);favorite.addEventListener('click',()=>toggleFavorite(task.id));actions.append(favorite);
      if(isAdmin()){const adminActions=el('div','card-admin-buttons');for(const [label,cls,action] of [['编辑','',()=>openEditor(task)],['删除','danger',()=>openDelete(task)]]){const b=el('button','text-button '+cls,label);b.type='button';b.addEventListener('click',action);adminActions.append(b);}actions.append(adminActions);}
      card.append(actions);
      $('taskList').append(card);
    }
    if(focused)Array.from($('taskList').querySelectorAll('.task-open')).find(n=>n.dataset.taskId===focused)?.focus({preventScroll:true});
    if(favoriteFocus){const button=Array.from($('taskList').querySelectorAll('[data-favorite-id]')).find(n=>n.dataset.favoriteId===favoriteFocus);if(button&&!button.disabled)button.focus({preventScroll:true});}
  }
  function renderOverview() {
    $('statTotal').textContent=state.tasks.length;
    $('statSoon').replaceChildren(document.createTextNode(String(state.tasks.filter(t=>deadlineState(t)==='soon').length)),el('span','stat-unit','项'));
    $('statOverdue').replaceChildren(document.createTextNode(String(state.tasks.filter(t=>deadlineState(t)==='overdue').length)),el('span','stat-unit','项'));
    document.querySelectorAll('[data-count]').forEach(n=>{n.textContent=state.tasks.filter(t=>n.dataset.count==='all'||t.category===n.dataset.count).length;});
    const upcoming=state.tasks.filter(t=>t.deadline&&new Date(t.deadline)>Date.now()).sort((a,b)=>new Date(a.deadline)-new Date(b.deadline));
    const area=$('nextDeadline');area.replaceChildren();
    if(upcoming.length){const task=upcoming[0],button=el('button','next-task'),link=el('span','next-link','查看任务详情');button.type='button';link.append(icon('arrow'));button.append(el('h3','',task.title),el('div','remaining',remaining(task).replace('剩余 ','')),el('span','next-time',formatDate(task.deadline)+' 截止'),link);button.addEventListener('click',()=>openDetail(task.id));area.append(button);}
    else{const n=el('div','deadline-empty');n.append(el('span','empty-dash','—'),el('p','','暂无待截止任务'),el('small','','有了新的安排，会在这里提醒你。'));area.append(n);}
  }
  function renderRecentPublished() {
    const now=Date.now(),cutoff=now-24*60*60*1000;
    const tasks=state.tasks.filter(task=>{
      const published=new Date(task.createdAt).getTime();
      return published>=cutoff&&published<=now;
    }).sort((a,b)=>new Date(b.createdAt)-new Date(a.createdAt)||b.id-a.id);
    const area=$('recentPublished'),focused=area.contains(document.activeElement)?document.activeElement.dataset.recentId:null;
    const scrollTop=area.scrollTop;
    $('recentCount').textContent=tasks.length;
    area.replaceChildren();area.setAttribute('aria-busy','false');
    if(!tasks.length){
      area.append(el('p','recent-empty','最近 24 小时暂无新发布，有新通知会显示在这里。'));
      return;
    }
    for(const task of tasks){
      const button=el('button','recent-task'),meta=el('span','recent-meta'),time=el('time','recent-time',formatDate(task.createdAt)+' 发布');
      button.type='button';button.dataset.recentId=task.id;button.setAttribute('aria-label','查看最新发布：'+task.title);
      time.dateTime=task.createdAt;
      meta.append(el('span','tag tag-'+task.category,categories[task.category]),icon('arrow'));
      button.append(meta,el('span','recent-title',task.title),time);
      button.addEventListener('click',()=>openDetail(task.id));area.append(button);
    }
    area.scrollTop=scrollTop;
    if(focused)Array.from(area.querySelectorAll('.recent-task')).find(n=>n.dataset.recentId===focused)?.focus({preventScroll:true});
  }
  function renderWeekTasks() {
    const area=$('weekTasks'),date=new Date(state.weekDate+'T12:00:00+08:00');
    const tasks=state.tasks.filter(t=>t.deadline&&dateKey(t.deadline)===state.weekDate)
      .sort((a,b)=>new Date(a.deadline)-new Date(b.deadline)||a.id-b.id);
    const scrollTop=area.dataset.date===state.weekDate?area.scrollTop:0;
    area.dataset.date=state.weekDate;
    $('weekDayHeading').textContent=new Intl.DateTimeFormat('zh-CN',{timeZone:zone,month:'numeric',day:'numeric',weekday:'short'}).format(date)+'截止';
    $('weekTaskCount').textContent=state.loaded?tasks.length+' 项':'—';
    area.replaceChildren();area.setAttribute('aria-busy',String(!state.loaded&&$('loadError').hidden));
    if(!state.loaded){
      area.append(el('p','week-empty',$('loadError').hidden?'正在加载当天任务…':'暂时无法获取任务，请重新加载。'));
      return;
    }
    if(!tasks.length){area.append(el('p','week-empty','当天暂无截止任务'));return;}
    const list=el('ul','week-task-list');
    for(const task of tasks){
      const item=el('li','week-task');
      item.append(el('span','tag tag-'+task.category,categories[task.category]),el('span','week-task-title',task.title));
      list.append(item);
    }
    area.append(list);area.scrollTop=scrollTop;
  }
  function renderWeek() {
    const now=new Date(),today=dateKey(now),chinaDate=new Date(today+'T12:00:00+08:00');
    const day=new Intl.DateTimeFormat('en-US',{timeZone:zone,weekday:'short'}).format(now),offset=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].indexOf(day);
    $('todayDate').textContent=new Intl.DateTimeFormat('zh-CN',{timeZone:zone,month:'long',day:'numeric',weekday:'long'}).format(now);
    const grid=$('weekGrid'),focused=grid.contains(document.activeElement)?document.activeElement.dataset.weekDate:null;
    const dates=Array.from({length:7},(_,i)=>dateKey(new Date(chinaDate.getTime()+(i-offset)*86400000)));
    if(!dates.includes(state.weekDate))state.weekDate=today;
    $('weekMonth').textContent=new Intl.DateTimeFormat('zh-CN',{timeZone:zone,year:'numeric',month:'long'}).format(now);grid.replaceChildren();
    const deadlines=new Set(state.tasks.filter(t=>t.deadline).map(t=>dateKey(t.deadline)));
    for(let i=0;i<7;i++){
      const key=dates[i],n=el('button','day'+(key===today?' today':'')+(key===state.weekDate?' selected':'')),num=el('span','day-num',Number(key.slice(-2)));
      n.type='button';n.dataset.weekDate=key;
      n.setAttribute('aria-label',key+(key===today?' 今天':'')+(deadlines.has(key)?' 有任务截止':'')+'，查看当天任务');
      n.setAttribute('aria-pressed',String(key===state.weekDate));n.setAttribute('aria-controls','weekTasks');
      if(key===today)n.setAttribute('aria-current','date');
      if(deadlines.has(key))num.append(el('span','calendar-dot'));
      n.append(el('span','',['一','二','三','四','五','六','日'][i]),num);
      n.addEventListener('click',()=>{state.weekDate=key;renderWeek();});grid.append(n);
    }
    renderWeekTasks();
    if(focused)Array.from(grid.children).find(n=>n.dataset.weekDate===focused)?.focus({preventScroll:true});
  }
  function render(){renderOverview();renderRecentPublished();renderFavorites();renderWeek();}
  function resetFilters(){state.category='all';state.search='';state.status='all';$('searchInput').value='';$('statusFilter').value='all';renderCategories();renderList();}
  function renderCategories(){document.querySelectorAll('[data-category]').forEach(n=>{const active=n.dataset.category===state.category;n.classList.toggle('active',active);if(active)n.setAttribute('aria-current','page');else n.removeAttribute('aria-current');});}
  function setAuthBusy(busy){
    authBusy=busy;
    for(const id of ['loginDialog','registerDialog'])$(id).querySelectorAll('button,input').forEach(n=>{n.disabled=busy;});
    $('loginForm').setAttribute('aria-busy',String(busy));$('registerForm').setAttribute('aria-busy',String(busy));
  }
  function setLoginRole(role){
    if(authBusy)return;
    loginRole=role==='admin'?'admin':'user';const admin=loginRole==='admin';
    $('loginTitle').textContent=admin?'管理员登录':'普通用户登录';$('loginEyebrow').textContent=admin?'ADMINISTRATOR':'CLASS MEMBER';
    $('loginDescription').textContent=admin?'登录后，发布和管理班级通知与任务。':'登录个人账号，收藏重要通知并管理自己的收藏。';
    $('loginUsername').placeholder=admin?'请输入管理员用户名':'请输入用户名';
    $('registerPrompt').hidden=admin;$('adminLoginHint').hidden=!admin;
    for(const [id,selected] of [['userLoginTab',!admin],['adminLoginTab',admin]]){$(id).classList.toggle('active',selected);$(id).setAttribute('aria-pressed',String(selected));}
    $('loginPassword').value='';showError('loginError','');
  }
  function openLogin(role='admin'){
    if(authBusy)return;
    closeDialog('registerDialog');setLoginRole(role);showDialog('loginDialog');$('loginUsername').focus();
  }
  function openRegister(){
    if(authBusy)return;
    $('registerForm').reset();$('registerUsername').value=$('loginUsername').value.trim();showError('registerError','');
    closeDialog('loginDialog');showDialog('registerDialog');$('registerUsername').focus();
  }
  function formatFileSize(size){return size<1024*1024?(size/1024).toFixed(1)+' KB':(size/(1024*1024)).toFixed(1)+' MB';}
  function attachmentInfo(file,pending=false){
    const info=el('div','attachment-info'),name=el('strong','attachment-name',file.name);
    info.append(name,el('span','attachment-meta',(file.extension||file.name.split('.').pop()).toUpperCase()+' · '+formatFileSize(file.size)+(pending?' · 待上传':'')));
    return info;
  }
  function renderEditorAttachments(){
    const list=$('editorAttachments');list.replaceChildren();
    for(const file of editorAttachments){
      const removed=removedAttachmentIds.has(file.id),row=el('li','attachment-item'+(removed?' attachment-removed':'')),action=el('button','text-button',removed?'撤销移除':'移除');
      action.type='button';action.disabled=editorBusy;action.setAttribute('aria-label',(removed?'撤销移除：':'移除附件：')+file.name);
      action.addEventListener('click',()=>{
        if(editorBusy)return;
        if(removed&&editorAttachments.length-removedAttachmentIds.size+editorFiles.length>=attachmentLimit){showError('taskError','每条通知最多保留 5 个附件，请先移除其他附件。');return;}
        if(removed)removedAttachmentIds.delete(file.id);else removedAttachmentIds.add(file.id);showError('taskError','');renderEditorAttachments();
      });
      row.append(icon('file'),attachmentInfo(file),action);list.append(row);
    }
    editorFiles.forEach((file,index)=>{
      const row=el('li','attachment-item'),action=el('button','text-button','移除');action.type='button';action.disabled=editorBusy;action.setAttribute('aria-label','移除待上传附件：'+file.name);
      action.addEventListener('click',()=>{if(editorBusy)return;editorFiles.splice(index,1);showError('taskError','');renderEditorAttachments();});
      row.append(icon('file'),attachmentInfo(file,true),action);list.append(row);
    });
    $('attachmentCount').textContent=(editorAttachments.length-removedAttachmentIds.size+editorFiles.length)+' / '+attachmentLimit;
    list.hidden=!list.children.length;
  }
  function chooseAttachments(){
    const picked=Array.from($('taskAttachments').files||[]);$('taskAttachments').value='';if(editorBusy)return;
    const additions=[];
    for(const file of picked){
      if(!/\.(pdf|doc|docx)$/i.test(file.name)){showError('taskError','只支持 PDF、DOC、DOCX 格式：'+file.name);return;}
      if(file.size===0||file.size>attachmentMaxBytes){showError('taskError','附件不能为空，且单个文件不能超过 20 MB：'+file.name);return;}
      if(![...editorFiles,...additions].some(f=>f.name===file.name&&f.size===file.size&&f.lastModified===file.lastModified))additions.push(file);
    }
    if(editorAttachments.length-removedAttachmentIds.size+editorFiles.length+additions.length>attachmentLimit){showError('taskError','每条通知最多添加 5 个附件，请减少选择的文件。');return;}
    editorFiles.push(...additions);showError('taskError','');renderEditorAttachments();
  }
  function renderDetailAttachments(task){
    const files=task.attachments||[],list=$('detailAttachmentList');list.replaceChildren();$('detailAttachments').hidden=!files.length;
    for(const file of files){
      const row=el('li','attachment-item'),actions=el('div','attachment-actions'),preview=el('button','button button-light button-sm','在线预览'),download=el('a','button button-light button-sm','下载');
      preview.type='button';preview.setAttribute('aria-label','在线预览：'+file.name);preview.addEventListener('click',()=>openAttachmentPreview(file));
      download.href=file.downloadUrl;download.setAttribute('download',file.name);download.setAttribute('aria-label','下载：'+file.name);
      actions.append(preview,download);row.append(icon('file'),attachmentInfo(file),actions);list.append(row);
    }
  }
  function releasePreview(){
    previewSequence++;if(previewRequest)previewRequest.abort();previewRequest=null;
    $('attachmentPreviewFrame').removeAttribute('src');
    if(previewObjectUrl)URL.revokeObjectURL(previewObjectUrl);previewObjectUrl=null;
  }
  async function openAttachmentPreview(file){
    releasePreview();const seq=previewSequence,controller=new AbortController();previewRequest=controller;
    $('attachmentPreviewTitle').textContent=file.name;$('attachmentPreviewMeta').textContent=file.extension.toUpperCase()+' · '+formatFileSize(file.size);
    $('attachmentPreviewDownload').href=file.downloadUrl;$('attachmentPreviewDownload').setAttribute('download',file.name);
    $('attachmentPreviewOpen').href=file.previewUrl;
    $('attachmentPreviewStatus').textContent='正在准备预览，Word 文档可能需要一些时间…';$('attachmentPreviewStatus').hidden=false;
    $('attachmentPreviewFrame').hidden=true;$('attachmentPreviewFrame').title='附件预览：'+file.name;showError('attachmentPreviewError','');showDialog('attachmentPreviewDialog');
    try{
      const response=await fetch(file.previewUrl,{credentials:'same-origin',cache:'no-store',signal:controller.signal});
      if(!response.ok){let message='预览暂时不可用，请下载原文件查看。';try{const data=await response.json();message=data.error||message;}catch{}throw new Error(message);}
      const blob=await response.blob();if(seq!==previewSequence)return;
      if(!['application/pdf','text/html'].includes(blob.type.split(';')[0]))throw new Error('该附件暂时无法在线预览，请下载原文件查看。');
      previewObjectUrl=URL.createObjectURL(blob);const frame=$('attachmentPreviewFrame');
      if(blob.type.startsWith('text/html'))frame.setAttribute('sandbox','');else frame.removeAttribute('sandbox');
      frame.src=previewObjectUrl;frame.hidden=false;$('attachmentPreviewStatus').hidden=true;
    }catch(error){if(seq!==previewSequence||error.name==='AbortError')return;$('attachmentPreviewStatus').hidden=true;showError('attachmentPreviewError',error.message||'预览失败，请下载原文件查看。');}
    finally{if(seq===previewSequence)previewRequest=null;}
  }
  function setEditorBusy(busy){
    editorBusy=busy;$('editorDialog').querySelectorAll('button,input,textarea,select').forEach(n=>{n.disabled=busy;});
    $('taskForm').setAttribute('aria-busy',String(busy));$('taskSubmit').textContent=busy?(editorFiles.length?'正在上传并保存…':'正在保存…'):(state.editingId?'保存修改':'发布任务');
  }
  function openEditor(task=null){
    if(editorBusy)return;editorFiles=[];editorAttachments=[...(task?.attachments||[])];removedAttachmentIds=new Set();renderEditorAttachments();
    if(!isAdmin()){openLogin();return;}state.editingId=task?.id??null;$('taskForm').reset();showError('taskError','');$('editorTitle').textContent=task?'编辑任务':'发布新任务';$('taskSubmit').textContent=task?'保存修改':'发布任务';
    $('taskTitle').value=task?.title??'';$('taskContent').value=task?.content??'';$('taskCategory').value=task?.category??'course';$('taskDeadline').value=localInput(task?.deadline);$('taskPinned').checked=task?.pinned??false;closeDialog('detailDialog');showDialog('editorDialog');
  }
  function openDetail(id){state.selectedId=id;updateDetail();if(state.tasks.some(t=>t.id===id))showDialog('detailDialog');}
  function updateDetail(){const task=state.tasks.find(t=>t.id===state.selectedId);if(!task){closeDialog('detailDialog');return;}$('detailTitle').textContent=task.title;$('detailBadges').replaceChildren(badges(task));$('detailMeta').textContent=task.author+' 发布 · '+formatDate(task.createdAt);$('detailDeadline').replaceChildren(icon('clock'),document.createTextNode(task.deadline?formatDate(task.deadline)+' 截止（北京时间）':'无截止时间'),countdown(task));$('detailContent').textContent=task.content;renderDetailAttachments(task);$('detailUpdated').textContent='更新于 '+formatDate(task.updatedAt);$('detailAdmin').hidden=!isAdmin();configureFavoriteButton($('detailFavorite'),task);}
  function openDelete(task){state.deleteId=task.id;showError('deleteError','');$('deleteDescription').textContent='“'+task.title+'”删除后将无法恢复。';closeDialog('detailDialog');showDialog('deleteDialog');}
  document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>closeDialog(b.dataset.close)));
  document.querySelectorAll('[data-category]').forEach(b=>b.addEventListener('click',()=>{state.category=b.dataset.category;if(state.category==='favorites'){state.search='';state.status='all';$('searchInput').value='';$('statusFilter').value='all';}renderCategories();renderList();if(state.category==='favorites'&&state.user)void refreshFavorites();}));
  $('searchInput').addEventListener('input',e=>{state.search=e.target.value.trim();renderList();});
  $('statusFilter').addEventListener('change',e=>{state.status=e.target.value;renderList();});
  $('sortSelect').addEventListener('change',e=>{state.sort=e.target.value;renderList();});
  $('userLoginButton').addEventListener('click',()=>openLogin('user'));$('loginButton').addEventListener('click',()=>openLogin('admin'));
  $('userLoginTab').addEventListener('click',()=>setLoginRole('user'));$('adminLoginTab').addEventListener('click',()=>setLoginRole('admin'));
  $('openRegisterButton').addEventListener('click',openRegister);$('backToLoginButton').addEventListener('click',()=>{$('loginUsername').value=$('registerUsername').value.trim();openLogin('user');});
  for(const id of ['loginDialog','registerDialog']){
    $(id).addEventListener('cancel',event=>{if(authBusy)event.preventDefault();});
    $(id).addEventListener('close',()=>{$(id).querySelectorAll('input[type="password"]').forEach(n=>{n.value='';});});
  }
  $('publishButton').addEventListener('click',()=>openEditor());$('retryButton').addEventListener('click',refreshTasks);$('refreshButton').addEventListener('click',refreshTasks);
  $('taskAttachments').addEventListener('change',chooseAttachments);
  $('editorDialog').addEventListener('cancel',event=>{if(editorBusy)event.preventDefault();});
  $('editorDialog').addEventListener('close',()=>{editorFiles=[];editorAttachments=[];removedAttachmentIds=new Set();$('taskAttachments').value='';});
  $('attachmentPreviewDialog').addEventListener('close',releasePreview);
  $('retryFavoritesButton').addEventListener('click',refreshFavorites);$('detailFavorite').addEventListener('click',()=>{if(state.selectedId!==null)void toggleFavorite(state.selectedId);});
  $('detailEdit').addEventListener('click',()=>{const t=state.tasks.find(t=>t.id===state.selectedId);if(t)openEditor(t);});$('detailDelete').addEventListener('click',()=>{const t=state.tasks.find(t=>t.id===state.selectedId);if(t)openDelete(t);});
  $('loginForm').addEventListener('submit',async e=>{
    e.preventDefault();if(authBusy)return;
    const payload={username:$('loginUsername').value.trim(),password:$('loginPassword').value,role:loginRole};
    setAuthBusy(true);$('loginSubmit').textContent='正在登录…';showError('loginError','');
    try{
      if(sessionRequest||!state.csrf)await refreshSession();
      const s=await api('/api/login',{method:'POST',body:JSON.stringify(payload)});
      applySession(s);$('loginPassword').value='';setAuthBusy(false);closeDialog('loginDialog');
      toast(isAdmin()?'管理员登录成功，可以发布和管理事务。':'登录成功，欢迎查看班级通知。');
    }catch(error){showError('loginError',error.message);}finally{setAuthBusy(false);$('loginSubmit').textContent='登录';}
  });
  $('registerForm').addEventListener('submit',async e=>{
    e.preventDefault();if(authBusy)return;showError('registerError','');
    const payload={username:$('registerUsername').value.trim(),password:$('registerPassword').value,passwordConfirm:$('registerPasswordConfirm').value};
    if(!payload.username||/\s/u.test(payload.username)){showError('registerError','用户名应为 1–64 个字符，且不能包含空白字符。');$('registerUsername').focus();return;}
    if(payload.password.length<8||payload.password.length>1024){showError('registerError','密码长度应为 8–1024 个字符。');$('registerPassword').focus();return;}
    if(payload.password!==payload.passwordConfirm){showError('registerError','两次输入的密码不一致。');$('registerPasswordConfirm').focus();return;}
    setAuthBusy(true);$('registerSubmit').textContent='正在注册…';
    try{
      if(sessionRequest||!state.csrf)await refreshSession();
      const s=await api('/api/register',{method:'POST',body:JSON.stringify(payload)});
      applySession(s);$('registerForm').reset();setAuthBusy(false);closeDialog('registerDialog');toast('注册成功，已登录普通用户账号。');
    }catch(error){showError('registerError',error.message);}finally{setAuthBusy(false);$('registerSubmit').textContent='注册并登录';}
  });
  $('logoutButton').addEventListener('click',async()=>{const b=$('logoutButton');b.disabled=true;try{if(sessionRequest)await sessionRequest;const s=await api('/api/logout',{method:'POST',body:'{}'});applySession(s);toast('已退出登录。');}catch(error){toast(error.message);}finally{b.disabled=false;}});
  $('taskForm').addEventListener('submit',async e=>{
    e.preventDefault();if(editorBusy)return;showError('taskError','');
    const raw=$('taskDeadline').value,editingId=state.editingId;
    const payload={title:$('taskTitle').value.trim(),content:$('taskContent').value.trim(),category:$('taskCategory').value,deadline:raw?raw+(raw.length===16?':00':'')+'+08:00':null,pinned:$('taskPinned').checked};
    let body=JSON.stringify(payload);
    if(editorFiles.length||removedAttachmentIds.size){
      if(editingId)payload.removedAttachmentIds=[...removedAttachmentIds];
      body=new FormData();body.append('payload',JSON.stringify(payload));for(const file of editorFiles)body.append('attachments',file);
    }
    setEditorBusy(true);
    try{
      await api(editingId?'/api/tasks/'+editingId:'/api/tasks',{method:editingId?'PATCH':'POST',body});
      setEditorBusy(false);closeDialog('editorDialog');resetFilters();toast(editingId?'任务和附件已更新。':'任务已发布，大家现在可以查看。');await refreshTasks();
    }catch(error){showError('taskError',error.message);}finally{setEditorBusy(false);}
  });
  $('confirmDelete').addEventListener('click',async()=>{if(state.deleteId===null)return;const b=$('confirmDelete');b.disabled=true;showError('deleteError','');try{await api('/api/tasks/'+state.deleteId,{method:'DELETE'});closeDialog('deleteDialog');state.deleteId=null;toast('任务已删除。');await refreshTasks();}catch(error){showError('deleteError',error.message);}finally{b.disabled=false;}});
  renderWeek();refreshSession().catch(()=>toast('登录状态暂时无法获取，仍可查看通知。'));refreshTasks();
  setInterval(()=>{if(!document.hidden&&state.loaded)render();},30000);
  setInterval(()=>{if(!document.hidden)refreshTasks();},60000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshTasks();});
})();
