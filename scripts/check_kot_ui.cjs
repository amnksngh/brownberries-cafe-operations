// Disposable fixture; printing is mocked, never sent to a real printer.
const {execFileSync}=require('node:child_process');
const {chromium}=require('C:/Users/hp/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert=require('node:assert/strict');
const fixture=JSON.parse(execFileSync('C:/Brownberries/brownberries-cafe-operations/.venv/Scripts/python.exe',['-c',`
import sys,json
sys.path.insert(0,'tests')
from test_kot import KitchenTicketTests
from app.models import CafeKitchenTicket
from flask import render_template
t=KitchenTicketTests();t.setUp()
try:
 t.approve(t.ids)
 with t.app.test_request_context('/'):
  ticket=CafeKitchenTicket.query.one()
  print(json.dumps(dict(token=ticket.token,prompt=render_template('cafe/kot_prompt.html'),html=render_template('cafe/kot.html',kot=json.loads(ticket.contents_json)))))
finally:t.tearDown()
`],{encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8',PYTHONWARNINGS:'ignore'}}));
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage();
  await page.addInitScript(()=>{window.open=url=>window.opened=url;window.print=()=>window.printed=true;});
  await page.route('http://fixture.test/**',r=>r.fulfill({contentType:'text/html',body:fixture.prompt}));
  await page.goto('http://fixture.test/?print_kot='+fixture.token);
  assert(await page.locator('dialog').isVisible());
  await page.getByRole('button',{name:'No',exact:true}).click();
  assert(!(await page.locator('dialog').isVisible()));
  await page.reload();assert(!(await page.locator('dialog').isVisible()));
  const second=await browser.newPage();
  await second.addInitScript(()=>{window.open=url=>window.opened=url;});
  await second.route('http://fixture.test/**',r=>r.fulfill({contentType:'text/html',body:fixture.prompt}));
  await second.goto('http://fixture.test/?print_kot='+fixture.token);
  await second.getByRole('button',{name:'Yes, print KOT'}).click();
  assert((await second.evaluate(()=>window.opened)).includes('/kot/'+fixture.token));
  await page.setContent(fixture.html);
  await page.emulateMedia({media:'print'});
  await page.evaluate(()=>window.dispatchEvent(new Event('beforeprint')));
  assert(await page.getByText('KITCHEN ORDER TICKET',{exact:true}).isVisible());
  const size=await page.locator('.ticket').evaluate(e=>({width:e.clientWidth,scroll:e.scrollWidth}));
  assert(size.scroll<=size.width);
  assert(fixture.html.includes('@page{size:portrait;margin:0}'));
  assert(!fixture.html.includes('size:80mm'));
  console.log('PASS: KOT Yes/No, no repeated prompt, exact ticket, print width.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
