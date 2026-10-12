// Disposable fixture only: no production or local-preview data is accessed.
const {spawnSync} = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const python = process.env.PYTHON || 'python';
const source = `
from tempfile import TemporaryDirectory
from app import create_app
from app.extensions import db
from app.models import User
with TemporaryDirectory() as directory:
    app=create_app(instance_path=directory,initialize_legacy_leaves=False)
    with app.app_context():
        user=User(full_name='Fixture Cashier',email='fixture@test',password_hash='x',role='cashier',active=True)
        db.session.add(user);db.session.commit();uid=user.id
    client=app.test_client()
    with client.session_transaction() as session: session['user_id']=uid
    response=client.get('/cafe/cash-counter/control')
    assert response.status_code==200
    print(response.get_data(as_text=True))
    with app.app_context(): db.session.remove();db.engine.dispose()
`;
(async()=>{
 const rendered=spawnSync(python,['-c',source],{encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'},maxBuffer:4*1024*1024});
 if(rendered.status!==0) throw new Error(rendered.stderr);
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage();
  await page.route('**/*',route=>{
   const url=new URL(route.request().url());
   if(url.pathname.startsWith('/static/') && !url.pathname.includes('..')) {
    const file=path.join(process.cwd(),decodeURIComponent(url.pathname));
    if(fs.existsSync(file)) return route.fulfill({path:file});
   }
   return route.fulfill({status:200,contentType:'text/html',body:rendered.stdout});
  });
  await page.goto('http://fixture.local/cafe/cash-counter/control');
  for(const width of [390,1280]) {
   await page.setViewportSize({width,height:900});
   const actual=await page.evaluate(()=>document.documentElement.scrollWidth);
   if(actual>width) throw new Error('Viewport overflow '+width+' -> '+actual);
  }
  const countForm=page.locator('form').filter({has:page.getByRole('button',{name:'Save count snapshot'})});
  if(await countForm.locator('[name=action]').count()!==1) throw new Error('Ambiguous action controls');
  await countForm.locator('[name=count_note_100]').fill('3');
  if(await countForm.locator('output').textContent()!=='300.00') throw new Error('Count total not updated');
  await countForm.locator('[name=action]').selectOption('open',{timeout:3000});
  const action=await countForm.evaluate(form=>new FormData(form).get('action'));
  if(action!=='open') throw new Error('Wrong submitted action');
  await page.setViewportSize({width:390,height:900});
  if(process.env.CASH_UI_SCREENSHOT) await page.screenshot({path:process.env.CASH_UI_SCREENSHOT,fullPage:true});
  console.log('Cash control UI passed: 390px and 1280px; form action and denomination totals.');
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
