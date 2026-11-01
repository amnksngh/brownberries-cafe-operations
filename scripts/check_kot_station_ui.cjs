// Disposable fixtures and mocked network only. Never prints or changes live data.
const {execFileSync}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'C:/Users/hp/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert=require('node:assert/strict');
const fixture=JSON.parse(execFileSync(process.env.PYTHON || 'C:/Brownberries/brownberries-cafe-operations/.venv/Scripts/python.exe',['-c',`
import sys,json
sys.path.insert(0,'tests')
from test_kot_printing import KitchenPrintQueueTests
from flask import render_template
t=KitchenPrintQueueTests();t.setUp()
try:
 token=t.ticket()
 with t.app.test_request_context('/'):
  print(json.dumps(dict(token=token,prompt=render_template('cafe/kot_prompt.html'))))
finally:t.tearDown()
`],{encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8',PYTHONWARNINGS:'ignore'}}));
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage(); let requests=0;
  await page.addInitScript(()=>{window.open=()=>{window.unwantedPrint=true;};});
  await page.route('http://fixture.test/**',async route=>{
   if(route.request().method()==='POST'){
    requests++;
    assert(route.request().headers()['x-csrf-token']);
    assert(route.request().url().endsWith(fixture.token));
    return route.fulfill({contentType:'application/json',body:JSON.stringify({job_id:1,status:'queued',message:'KOT #1 queued. Kitchen station offline; job retained in queue.'})});
   }
   return route.fulfill({contentType:'text/html',body:fixture.prompt});
  });
  await page.goto('http://fixture.test/?print_kot='+fixture.token);
  await page.click('#kot-no'); assert.equal(requests,0);
  await page.evaluate(()=>sessionStorage.clear());
  await page.goto('http://fixture.test/?print_kot='+fixture.token);
  await page.click('#kot-yes');
  await page.waitForFunction(()=>document.getElementById('kot-print-message').textContent.includes('queued'));
  assert.equal(requests,1); assert.equal(await page.evaluate(()=>!!window.unwantedPrint),false);
  assert(await page.isDisabled('#kot-yes'));
  console.log('PASS: remote KOT No sends nothing; Yes queues exact ticket with CSRF, no print dialog; offline status visible.');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
