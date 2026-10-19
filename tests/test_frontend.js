'use strict';

// Run with: node --test tests/test_frontend.js
// Execute the shipped script and real template without a browser or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const { File, Blob } = require('node:buffer');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');
const template = fs.readFileSync(path.join(root, 'templates/index.html'), 'utf8');
const settle = async () => { for (let n = 0; n < 4; n++) await new Promise(setImmediate); };
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };

class Element {
  constructor(tag, document) {
    this.tagName = tag.toLowerCase(); this.document = document; this.children = [];
    this.attributes = {}; this.events = {}; this.parentElement = null;
    this.dataset = new Proxy({}, { set: (o, k, v) => {
      o[k] = String(v);
      this.attributes['data-' + k.replace(/[A-Z]/g, c => '-' + c.toLowerCase())] = String(v);
      return true;
    } });
    this.className = ''; this.value = ''; this.hidden = false; this.disabled = false;
    this.open = false; this.scrollTop = 0; this._text = '';
    this.classList = {
      toggle: (name, force) => {
        const names = new Set(this.className.split(/\s+/).filter(Boolean));
        const add = force === undefined ? !names.has(name) : force;
        if (add) names.add(name); else names.delete(name);
        this.className = [...names].join(' '); return add;
      },
    };
  }
  get textContent() { return this._text + this.children.map(n => n.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  append(...nodes) { for (const node of nodes) { node.parentElement = this; this.children.push(node); } }
  replaceChildren(...nodes) { this._text = ''; this.children = []; this.append(...nodes); }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === 'class') this.className = String(value);
    if (name === 'value') this.value = String(value);
    if (name === 'hidden') this.hidden = true;
    if (name === 'disabled') this.disabled = true;
    if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  matches(selector) {
    if (selector.startsWith('.')) return this.className.split(/\s+/).includes(selector.slice(1));
    const match = selector.match(/^([\w-]+)?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/);
    if (!match) throw new Error('Unsupported test selector: ' + selector);
    return (!match[1] || this.tagName === match[1]) && (!match[2] ||
      (this.getAttribute(match[2]) !== null && (match[3] === undefined || this.getAttribute(match[2]) === match[3])));
  }
  querySelectorAll(selector) {
    const found = [], selectors = selector.split(',');
    const visit = n => { for (const child of n.children) { if (selectors.some(s => child.matches(s))) found.push(child); visit(child); } };
    visit(this); return found;
  }
  contains(node) { return node === this || this.children.some(child => child.contains(node)); }
  addEventListener(name, fn) { (this.events[name] ??= []).push(fn); }
  async dispatch(name) {
    if (name === 'click' && this.disabled) return;
    const event = { target: this, preventDefault() {} };
    await Promise.all((this.events[name] ?? []).map(fn => fn(event)));
    await settle();
  }
  focus() { this.document.activeElement = this; }
  showModal() { this.open = true; }
  close() { this.open = false; for (const fn of this.events.close ?? []) fn({ target: this }); }
  reset() { this.querySelectorAll('input,textarea').forEach(n => { n.value = ''; }); }
}

function createDocument() {
  const document = new Element('document'); document.document = document;
  document.activeElement = null; document.hidden = false;
  document.createElement = tag => new Element(tag, document);
  document.createElementNS = (_, tag) => document.createElement(tag);
  document.createTextNode = text => { const n = document.createElement('#text'); n.textContent = text; return n; };
  document.getElementById = id => document.querySelectorAll('[id]').find(n => n.getAttribute('id') === id) ?? null;
  // Only basic tree/attributes are needed; application rendering still uses the original script.
  const stack = [document], voidTags = new Set(['meta', 'link', 'input', 'br', 'hr', 'img']);
  for (const token of template.match(/<!--[^]*?-->|<![^>]*>|<[^>]+>|[^<]+/g)) {
    if (token.startsWith('<!')) continue;
    if (token.startsWith('</')) { stack.pop(); continue; }
    if (!token.startsWith('<')) { stack.at(-1).append(document.createTextNode(token)); continue; }
    const tag = token.match(/^<([\w-]+)/)?.[1];
    if (!tag) continue;
    const node = document.createElement(tag);
    const attrs = token.slice(tag.length + 1).replace(/\/?\s*>$/, '');
    for (const match of attrs.matchAll(/([\w:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) {
      node.setAttribute(match[1], match[2] ?? match[3] ?? match[4] ?? '');
    }
    stack.at(-1).append(node);
    if (!voidTags.has(tag) && !token.endsWith('/>')) stack.push(node);
  }
  return document;
}

const userA = { id: 11, username: 'student-a', role: 'user' };
const userB = { id: 22, username: 'student-b', role: 'user' };
const admin = { id: 33, username: 'class-admin', role: 'admin' };
const tasks = [1, 2].map(id => ({ id, title: '通知 ' + id, content: '内容 ' + id, category: 'course',
  author: 'teacher', pinned: false, deadline: null, createdAt: '2026-10-19T01:00:00Z', updatedAt: '2026-10-19T01:00:00Z' }));

async function boot(initialUser = null, taskData = tasks) {
  const document = createDocument(), calls = [], reads = [], taskWrites = [], previewReads = [], revokedUrls = [];
  const server = { user: initialUser, csrf: 'initial-token', tasks: structuredClone(taskData), favorites: new Map([[11, new Set([1])], [22, new Set([2])], [33, new Set()]]) };
  const response = (data, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
  const fetch = async (url, options = {}) => {
    const method = options.method ?? 'GET'; calls.push({ url, method, options });
    if (url === '/api/session') return response({ user: server.user, csrfToken: server.csrf });
    if (url === '/api/tasks' && method === 'GET') return response({ tasks: server.tasks });
    if (/^\/api\/tasks(?:\/\d+)?$/.test(url) && ['POST', 'PATCH'].includes(method)) {
      assert.equal(server.user.role, 'admin'); assert.equal(options.headers['X-CSRF-Token'], server.csrf);
      if (taskWrites.length) return taskWrites.shift()(options);
      const multipart = options.body instanceof FormData;
      const payload = JSON.parse(multipart ? options.body.get('payload') : options.body);
      if (multipart) assert.equal(options.headers['Content-Type'], undefined);
      const existing = method === 'PATCH' ? server.tasks.find(t => t.id === Number(url.split('/').pop())) : null;
      const removed = payload.removedAttachmentIds || []; delete payload.removedAttachmentIds;
      const files = multipart ? options.body.getAll('attachments') : [];
      const attachments = [...(existing?.attachments || []).filter(a => !removed.includes(a.id)), ...files.map((f,i) => ({ id:100+i, name:f.name, size:f.size, extension:f.name.split('.').pop(), previewUrl:'/api/attachments/'+(100+i)+'/preview', downloadUrl:'/api/attachments/'+(100+i)+'/download' }))];
      const task = { ...tasks[0], ...existing, ...payload, id:existing?.id || 3, attachments };
      server.tasks = existing ? server.tasks.map(t => t.id === task.id ? task : t) : [...server.tasks, task];
      return response({ task }, existing ? 200 : 201);
    }
    if (/^\/api\/attachments\/\d+\/preview$/.test(url)) {
      if (previewReads.length) return previewReads.shift()(options);
      return { ...response({}), blob: async () => new Blob(['%PDF-1.7 fixture'], { type:'application/pdf' }) };
    }
    if (url === '/api/login') {
      server.user = JSON.parse(options.body).username === userA.username ? userA : userB;
      server.csrf = 'login-' + server.user.id;
      return response({ user: server.user, csrfToken: server.csrf });
    }
    if (url === '/api/logout') { server.user = null; server.csrf = 'logged-out'; return response({ user: null, csrfToken: server.csrf }); }
    if (url === '/api/favorites') {
      if (reads.length) return reads.shift()();
      assert.ok(server.user, 'Guests must not request private favorites');
      return response({ userId: server.user.id, taskIds: [...server.favorites.get(server.user.id)] });
    }
    const match = url.match(/^\/api\/favorites\/(\d+)$/);
    if (match) {
      assert.ok(server.user); assert.equal(options.headers['X-CSRF-Token'], server.csrf);
      const saved = server.favorites.get(server.user.id), id = Number(match[1]);
      if (method === 'PUT') saved.add(id); else if (method === 'DELETE') saved.delete(id); else assert.fail('Unexpected mutation method');
      return response({ userId: server.user.id, taskId: id, favorited: saved.has(id) });
    }
    throw new Error('Unexpected request: ' + method + ' ' + url);
  };
  let blobSequence = 0;
  const URL = { createObjectURL: () => 'blob:test-' + (++blobSequence), revokeObjectURL: value => revokedUrls.push(value) };
  vm.runInNewContext(source, { document, fetch, Intl, Date, FormData, AbortController, URL, setTimeout: () => 1, clearTimeout() {}, setInterval() {} }, { filename: 'static/app.js' });
  await settle();
  const $ = id => { const n = document.getElementById(id); assert.ok(n, 'Template element exists: ' + id); return n; };
  const category = name => document.querySelectorAll('[data-category]').find(n => n.dataset.category === name);
  const favorite = id => $('taskList').querySelectorAll('.favorite-button').find(n => n.dataset.favoriteId === String(id));
  const card = id => $('taskList').querySelectorAll('.task-open').find(n => n.dataset.taskId === String(id));
  const login = async user => {
    await $('userLoginButton').dispatch('click'); $('loginUsername').value = user.username; $('loginPassword').value = 'test-only-password';
    await $('loginForm').dispatch('submit');
  };
  return { $, category, favorite, card, login, calls, reads, response, server, taskWrites, previewReads, revokedUrls };
}

test('public notices remain visible; favorites requires ordinary-user login', async () => {
  const ui = await boot();
  assert.ok(ui.card(1)); assert.ok(ui.card(2));
  assert.equal(ui.calls.filter(c => c.url === '/api/favorites').length, 0);
  assert.equal(ui.$('favoritesCount').hidden, true);
  await ui.category('favorites').dispatch('click');
  assert.match(ui.$('taskList').textContent, /登录后查看我的收藏/);
  assert.equal(ui.$('resultCount').textContent, '—');
  await ui.$('taskList').querySelectorAll('button')[0].dispatch('click');
  assert.equal(ui.$('loginDialog').open, true);
  assert.equal(ui.$('loginTitle').textContent, '普通用户登录');
  await ui.category('all').dispatch('click');
  await ui.card(1).dispatch('click');
  await ui.$('detailFavorite').dispatch('click');
  assert.equal(ui.$('detailDialog').open, false);
  assert.equal(ui.calls.filter(c => c.url.startsWith('/api/favorites')).length, 0);
});

test('login loads personal favorites; card and detail additions/removals keep count and list in sync', async () => {
  const ui = await boot(); await ui.login(userA);
  assert.equal(ui.$('favoritesCount').textContent, '1');
  assert.equal(ui.favorite(1).getAttribute('aria-pressed'), 'true');
  assert.equal(ui.favorite(2).getAttribute('aria-pressed'), 'false');
  await ui.category('favorites').dispatch('click');
  assert.ok(ui.card(1)); assert.equal(ui.card(2), undefined);
  await ui.favorite(1).dispatch('click');
  assert.equal(ui.$('favoritesCount').textContent, '0');
  assert.match(ui.$('taskList').textContent, /还没有收藏的通知/);
  await ui.category('all').dispatch('click'); await ui.card(2).dispatch('click');
  await ui.$('detailFavorite').dispatch('click');
  assert.equal(ui.$('detailFavorite').getAttribute('aria-pressed'), 'true');
  assert.equal(ui.favorite(2).getAttribute('aria-pressed'), 'true');
  assert.equal(ui.$('favoritesCount').textContent, '1');
  await ui.category('favorites').dispatch('click');
  assert.ok(ui.card(2)); assert.equal(ui.card(1), undefined);
  await ui.$('detailFavorite').dispatch('click');
  assert.equal(ui.$('detailFavorite').getAttribute('aria-pressed'), 'false');
  assert.equal(ui.$('favoritesCount').textContent, '0');
  assert.equal(ui.$('resultCount').textContent, '0');
  assert.deepEqual(ui.calls.filter(c => /\/favorites\//.test(c.url)).map(c => [c.method, c.url]), [
    ['DELETE', '/api/favorites/1'], ['PUT', '/api/favorites/2'], ['DELETE', '/api/favorites/2'],
  ]);
});

test('logout removes private favorites from list, detail and navigation', async () => {
  const ui = await boot(userA); await ui.category('favorites').dispatch('click');
  await ui.card(1).dispatch('click'); await ui.$('logoutButton').dispatch('click');
  assert.equal(ui.$('favoritesCount').hidden, true);
  assert.match(ui.$('taskList').textContent, /登录后查看我的收藏/);
  assert.equal(ui.$('detailFavorite').getAttribute('aria-pressed'), 'false');
  assert.match(ui.$('detailFavorite').textContent, /登录后收藏/);
  await ui.category('all').dispatch('click');
  assert.ok(ui.card(1)); assert.ok(ui.card(2));
});

test('a delayed favorites response cannot restore the old account after logout or switching accounts', async () => {
  const ui = await boot(userA), oldRead = deferred();
  ui.reads.push(() => oldRead.promise);
  await ui.category('favorites').dispatch('click');
  await ui.$('logoutButton').dispatch('click');
  await ui.login(userB);
  assert.ok(ui.card(2)); assert.equal(ui.card(1), undefined);
  oldRead.resolve(ui.response({ userId: userA.id, taskIds: [1] })); await settle();
  assert.ok(ui.card(2)); assert.equal(ui.card(1), undefined);
  assert.equal(ui.$('favoritesCount').textContent, '1');
  assert.match(ui.$('viewerText').textContent, /student-b/);
});

test('an old unauthorized favorites response cannot log out a newly signed-in account', async () => {
  const ui = await boot(userA), oldRead = deferred();
  ui.reads.push(() => oldRead.promise);
  await ui.category('favorites').dispatch('click');
  await ui.$('logoutButton').dispatch('click'); await ui.login(userB);
  oldRead.resolve(ui.response({ error: '请先登录' }, 401)); await settle();
  assert.match(ui.$('viewerText').textContent, /student-b/);
  assert.equal(ui.$('logoutButton').hidden, false);
  assert.equal(ui.$('favoritesError').hidden, true);
  assert.ok(ui.card(2)); assert.equal(ui.card(1), undefined);
});

test('failed favorites loading preserves public notices and can be retried', async () => {
  const ui = await boot();
  ui.reads.push(() => ui.response({ error: '暂时不可用' }, 503));
  await ui.login(userA);
  assert.ok(ui.card(1)); assert.ok(ui.card(2));
  assert.equal(ui.$('favoritesError').hidden, false);
  assert.match(ui.$('favoritesErrorText').textContent, /暂时不可用/);
  assert.equal(ui.favorite(1).disabled, true);
  await ui.$('retryFavoritesButton').dispatch('click');
  assert.equal(ui.$('favoritesError').hidden, true);
  assert.equal(ui.$('favoritesCount').textContent, '1');
  assert.equal(ui.favorite(1).disabled, false);
  assert.equal(ui.favorite(1).getAttribute('aria-pressed'), 'true');
  await ui.category('favorites').dispatch('click');
  assert.ok(ui.card(1)); assert.equal(ui.card(2), undefined);
});

const attachment = (id, name = '课程资料.pdf') => ({ id, name, size:2048, extension:name.split('.').pop(), previewUrl:'/api/attachments/'+id+'/preview', downloadUrl:'/api/attachments/'+id+'/download' });
const fillTask = ui => { ui.$('taskTitle').value='带附件的通知'; ui.$('taskContent').value='请查看附件'; ui.$('taskPinned').checked=false; };
const choose = async (ui, files) => { ui.$('taskAttachments').files=files; await ui.$('taskAttachments').dispatch('change'); };

test('admin uploads selected files with multipart and CSRF; normal JSON publishing still works', async () => {
  const ui = await boot(admin); await ui.$('publishButton').dispatch('click'); fillTask(ui);
  await choose(ui, [new File(['%PDF-1.7'], '中文资料.pdf')]);
  assert.match(ui.$('editorAttachments').textContent, /中文资料.pdf.*待上传/);
  assert.equal(ui.$('attachmentCount').textContent, '1 / 5');
  await ui.$('taskForm').dispatch('submit');
  assert.equal(ui.$('editorDialog').open, false);
  const request=ui.calls.find(c=>c.method==='POST'&&c.url==='/api/tasks');
  assert.ok(request.options.body instanceof FormData); assert.equal(request.options.headers['Content-Type'], undefined);
  assert.equal(request.options.body.getAll('attachments')[0].name, '中文资料.pdf');
  assert.equal(ui.server.tasks.at(-1).attachments.length, 1);
  await ui.$('publishButton').dispatch('click'); fillTask(ui); await ui.$('taskForm').dispatch('submit');
  assert.equal(ui.calls.filter(c=>c.method==='POST'&&c.url==='/api/tasks').at(-1).options.headers['Content-Type'], 'application/json');
});

test('file type, empty files, individual size and total count are validated before upload', async () => {
  const ui=await boot(admin); await ui.$('publishButton').dispatch('click');
  for(const [files,error] of [
    [[{name:'script.exe',size:100}],/只支持/],
    [[{name:'empty.pdf',size:0}],/不能为空/],
    [[{name:'large.pdf',size:20*1024*1024+1}],/20 MB/],
    [Array.from({length:6},(_,i)=>({name:i+'.docx',size:100})),/最多添加 5/],
  ]) { await choose(ui,files); assert.match(ui.$('taskError').textContent,error); assert.equal(ui.$('attachmentCount').textContent,'0 / 5'); }
  assert.equal(ui.calls.filter(c=>c.method==='POST'&&c.url==='/api/tasks').length,0);
});

test('editing stages attachment removal, supports undo, and commits removals with new files', async () => {
  const ui=await boot(admin,[{...tasks[0],attachments:[attachment(7)]}]);
  await ui.card(1).dispatch('click'); await ui.$('detailEdit').dispatch('click');
  assert.match(ui.$('editorAttachments').textContent,/课程资料.pdf/);
  await ui.$('editorAttachments').querySelectorAll('button')[0].dispatch('click');
  assert.equal(ui.$('attachmentCount').textContent,'0 / 5'); assert.match(ui.$('editorAttachments').textContent,/撤销移除/);
  await ui.$('editorAttachments').querySelectorAll('button')[0].dispatch('click');
  assert.equal(ui.$('attachmentCount').textContent,'1 / 5');
  await ui.$('editorAttachments').querySelectorAll('button')[0].dispatch('click');
  await choose(ui,[new File(['word fixture'],'新资料.docx')]);
  await ui.$('taskForm').dispatch('submit');
  const request=ui.calls.find(c=>c.method==='PATCH');
  assert.deepEqual(JSON.parse(request.options.body.get('payload')).removedAttachmentIds,[7]);
  assert.equal(ui.server.tasks[0].attachments.length,1);assert.equal(ui.server.tasks[0].attachments[0].name,'新资料.docx');
});

test('failed upload preserves selected files and editor contents; successful retry clears them', async () => {
  const ui=await boot(admin);await ui.$('publishButton').dispatch('click');fillTask(ui);
  await choose(ui,[new File(['pdf'],'保持.pdf')]);
  ui.taskWrites.push(()=>ui.response({error:'文件格式校验失败'},400));
  await ui.$('taskForm').dispatch('submit');
  assert.equal(ui.$('editorDialog').open,true);assert.equal(ui.$('taskTitle').value,'带附件的通知');
  assert.match(ui.$('editorAttachments').textContent,/保持.pdf/);assert.match(ui.$('taskError').textContent,/文件格式/);
  assert.equal(ui.$('taskSubmit').disabled,false);
  await ui.$('taskForm').dispatch('submit');assert.equal(ui.$('editorDialog').open,false);
  await ui.$('publishButton').dispatch('click');assert.equal(ui.$('attachmentCount').textContent,'0 / 5');
});

test('editor cannot close or submit twice while a file is being uploaded', async () => {
  const ui=await boot(admin), write=deferred();await ui.$('publishButton').dispatch('click');fillTask(ui);
  await choose(ui,[new File(['pdf'],'保持.pdf')]);ui.taskWrites.push(()=>write.promise);
  const submit=ui.$('taskForm').dispatch('submit');await settle();
  assert.equal(ui.$('taskAttachments').disabled,true);assert.match(ui.$('taskSubmit').textContent,/上传/);
  await ui.$('taskForm').dispatch('submit');
  await ui.$('editorDialog').querySelectorAll('[data-close]')[0].dispatch('click');
  assert.equal(ui.$('editorDialog').open,true);assert.equal(ui.calls.filter(c=>c.method==='POST'&&c.url==='/api/tasks').length,1);
  write.resolve(ui.response({error:'请重试'},503));await submit;assert.equal(ui.$('taskAttachments').disabled,false);
});

test('guests can preview and download attachments, and preview object URLs are cleaned on close', async () => {
  const ui=await boot(null,[{...tasks[0],attachments:[attachment(7,'<img onerror=alert(1)>.pdf')]}]);
  await ui.card(1).dispatch('click');assert.equal(ui.$('detailAttachments').hidden,false);
  const list=ui.$('detailAttachmentList');assert.equal(list.querySelectorAll('img').length,0);
  assert.equal(list.querySelectorAll('a')[0].href,'/api/attachments/7/download');
  await list.querySelectorAll('button')[0].dispatch('click');
  assert.equal(ui.$('attachmentPreviewDialog').open,true);assert.equal(ui.$('attachmentPreviewFrame').hidden,false);
  assert.equal(ui.$('attachmentPreviewFrame').src,'blob:test-1');
  assert.equal(ui.$('attachmentPreviewDownload').href,'/api/attachments/7/download');
  await ui.$('attachmentPreviewDialog').querySelectorAll('[data-close]')[0].dispatch('click');
  assert.deepEqual(ui.revokedUrls,['blob:test-1']);
});

test('preview failure retains original download and a late preview cannot reopen a closed dialog', async () => {
  const ui=await boot(null,[{...tasks[0],attachments:[attachment(8,'说明.docx')]}]);await ui.card(1).dispatch('click');
  ui.previewReads.push(()=>ui.response({error:'转换暂时不可用，请下载原文件'},503));
  await ui.$('detailAttachmentList').querySelectorAll('button')[0].dispatch('click');
  assert.match(ui.$('attachmentPreviewError').textContent,/转换暂时不可用/);assert.equal(ui.$('attachmentPreviewDownload').href,'/api/attachments/8/download');
  await ui.$('attachmentPreviewDialog').querySelectorAll('[data-close]')[0].dispatch('click');
  const read=deferred();ui.previewReads.push(()=>read.promise);
  const open=ui.$('detailAttachmentList').querySelectorAll('button')[0].dispatch('click');await settle();
  await ui.$('attachmentPreviewDialog').querySelectorAll('[data-close]')[0].dispatch('click');
  read.resolve({...ui.response({}),blob:async()=>new Blob(['<p>preview</p>'],{type:'text/html'})});await open;
  assert.equal(ui.$('attachmentPreviewDialog').open,false);assert.equal(ui.$('attachmentPreviewFrame').hidden,true);
});
