'use strict';
// Run with Playwright installed: node tests/test_attachments_browser.js
// Uses temporary test data; never touches live notices or accounts.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const python = process.env.TEST_PYTHON || path.join(root, '.venv/bin/python');

(async () => {
  const server = spawn(python, ['-u', 'tests/browser_fixture_server.py'], { cwd:root, stdio:['ignore','pipe','pipe'] });
  let errors = '', browser;
  server.stderr.on('data', chunk => { errors += chunk; });
  try {
    const info = await new Promise((resolve, reject) => {
      let output=''; const timer=setTimeout(()=>reject(new Error('Test server timed out: '+errors)),15000);
      server.stdout.on('data', chunk=>{ output+=chunk; if(output.includes('\n')){clearTimeout(timer);resolve(JSON.parse(output.split('\n')[0]));} });
      server.once('exit', code=>{clearTimeout(timer);reject(new Error('Test server exited '+code+': '+errors));});
    });
    const base='http://127.0.0.1:'+info.port;
    browser=await chromium.launch({headless:true,...(process.env.TEST_BROWSER_EXECUTABLE?{executablePath:process.env.TEST_BROWSER_EXECUTABLE}:{})});
    const context=await browser.newContext({viewport:{width:1365,height:1000}}),page=await context.newPage(),pageErrors=[];
    page.on('pageerror', error=>pageErrors.push(error.message));
    await page.goto(base);await page.locator('#loginButton').click();
    await page.locator('#loginUsername').fill('browser-admin');await page.locator('#loginPassword').fill('browser-test-password');await page.locator('#loginSubmit').click();
    await page.locator('#publishButton').waitFor({state:'visible'});await page.locator('#publishButton').click();
    await page.locator('#taskTitle').fill('实验报告提交说明');await page.locator('#taskContent').fill('请阅读附件中的要求，并按时完成实验报告。');
    await page.locator('#taskAttachments').setInputFiles([path.join(info.fixtures,'实验要求.pdf'),path.join(info.fixtures,'课程说明.docx')]);
    assert.equal(await page.locator('#attachmentCount').textContent(),'2 / 5');
    const artifactDir=path.join(root,'tests/artifacts/attachments');await fs.mkdir(artifactDir,{recursive:true});
    await page.locator('#editorDialog').screenshot({path:path.join(artifactDir,'editor-desktop.png')});
    await page.locator('#taskSubmit').click();await page.locator('#editorDialog').waitFor({state:'hidden'});
    await page.locator('.task-open').click();
    assert.equal(await page.locator('#detailAttachmentList .attachment-item').count(),2);
    await page.locator('#detailDialog').screenshot({path:path.join(artifactDir,'detail-desktop.png')});
    for(const name of ['实验要求.pdf','课程说明.docx']){
      await page.getByRole('button',{name:'在线预览：'+name,exact:true}).click();
      await page.locator('#attachmentPreviewFrame').waitFor({state:'visible',timeout:90000});
      const contentType=await page.locator('#attachmentPreviewFrame').evaluate(async frame=>(await fetch(frame.src)).headers.get('content-type'));
      assert.ok(/application\/pdf|text\/html/.test(contentType),contentType);
      if(contentType.includes('html')){
        assert.equal(await page.locator('#attachmentPreviewFrame').getAttribute('sandbox'),'');
        assert.match(await page.frameLocator('#attachmentPreviewFrame').locator('body').textContent(),/课程附件预览测试/);
      }
      // Native PDF viewers paint after the iframe becomes visible.
      await new Promise(resolve=>setTimeout(resolve,1500));
      await page.locator('#attachmentPreviewDialog').screenshot({path:path.join(artifactDir,name.endsWith('.pdf')?'preview-pdf.png':'preview-word.png')});
      await page.getByRole('button',{name:'关闭附件预览'}).click();
    }
    const downloadEvent=page.waitForEvent('download');await page.getByRole('link',{name:'下载：实验要求.pdf',exact:true}).click();
    const download=await downloadEvent;assert.equal(download.suggestedFilename(),'实验要求.pdf');
    assert.deepEqual(await fs.readFile(await download.path()),await fs.readFile(path.join(info.fixtures,'实验要求.pdf')));
    await page.locator('#detailEdit').click();await page.getByRole('button',{name:'移除附件：实验要求.pdf',exact:true}).click();
    assert.equal(await page.locator('#attachmentCount').textContent(),'1 / 5');await page.locator('#taskSubmit').click();await page.locator('#editorDialog').waitFor({state:'hidden'});
    await page.locator('.task-open').click();assert.equal(await page.locator('#detailAttachmentList .attachment-item').count(),1);
    await page.getByRole('button',{name:'关闭任务详情',exact:true}).click();await page.locator('#logoutButton').click();await page.locator('#userLoginButton').waitFor({state:'visible'});
    await page.setViewportSize({width:390,height:844});await page.locator('.task-open').click();
    assert.equal(await page.locator('#detailAttachmentList .attachment-item').count(),1);
    assert.equal(await page.locator('#detailDialog').evaluate(n=>n.scrollWidth>n.clientWidth),false);
    await page.locator('#detailDialog').screenshot({path:path.join(artifactDir,'detail-mobile.png')});
    await page.getByRole('button',{name:'在线预览：课程说明.docx',exact:true}).click();await page.locator('#attachmentPreviewFrame').waitFor({state:'visible',timeout:90000});
    assert.equal(await page.locator('#attachmentPreviewDialog').evaluate(n=>n.scrollWidth>n.clientWidth),false);
    await new Promise(resolve=>setTimeout(resolve,1500));
    await page.locator('#attachmentPreviewDialog').screenshot({path:path.join(artifactDir,'preview-mobile.png')});
    assert.deepEqual(pageErrors,[]);
    console.log(JSON.stringify({ok:true,checks:['admin multi-file publish','PDF online preview','DOCX online preview','original-byte download','edit attachment removal','guest preview','390px mobile layout','no browser errors'],screenshots:artifactDir}));
  } finally {
    if(browser)await browser.close();server.kill('SIGINT');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
