// Disposable database only; no live or preview inventory is changed.
const {spawnSync}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const source=`
import json
from tempfile import TemporaryDirectory
from uuid import uuid4
from app import create_app
from app.extensions import db
from app.models import User, ReusableInventoryAsset
from app.asset_lifecycle import execute
with TemporaryDirectory() as folder:
 app=create_app(instance_path=folder,initialize_legacy_leaves=False)
 with app.app_context():
  user=User(full_name='Fixture Admin',email='fixture@test',role='admin',active=True,password_hash='x')
  asset=ReusableInventoryAsset(name='Cappuccino cups',purchased_quantity=100,current_quantity=90,average_unit_price=50,area_scope='cafe')
  db.session.add_all([user,asset]);db.session.commit();uid=user.id;aid=asset.id
  execute(asset,dict(action='initialize',store='60',reason='Opening count'),user,uuid4().hex);db.session.commit()
 client=app.test_client()
 with client.session_transaction() as s:s['user_id']=uid
 urls=['/cafe/inventory/reusable-lifecycle?asset_id='+str(aid),'/cafe/cash-counter/control','/cafe/inventory?section=reusable_assets']
 pages={}
 for url in urls:
  response=client.get(url);assert response.status_code==200
  pages[url]=response.get_data(as_text=True)
 print(json.dumps(pages))
 with app.app_context():db.session.remove();db.engine.dispose()
`;
(async()=>{
 const result=spawnSync(process.env.PYTHON||'python',['-c',source],{encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'},maxBuffer:8*1024*1024});
 if(result.status!==0)throw Error(result.stderr);
 const pages=JSON.parse(result.stdout);
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage();
  await page.route('**/*',route=>{
   const u=new URL(route.request().url());
   if(u.pathname.startsWith('/static/')&&!u.pathname.includes('..')){
    const file=path.join(process.cwd(),decodeURIComponent(u.pathname));
    if(fs.existsSync(file))return route.fulfill({path:file});
   }
   return route.fulfill({status:200,contentType:'text/html',body:pages[u.pathname+u.search]||''});
  });
  for(const url of Object.keys(pages)){
   await page.goto('http://fixture.local'+url);
   for(const width of [390,1280]){
    await page.setViewportSize({width,height:900});
    const actual=await page.evaluate(()=>document.documentElement.scrollWidth);
    if(actual>width)throw Error(url+' overflows '+width+' to '+actual);
   }
   if(url.includes('reusable-lifecycle')){
    const form=page.locator('form').filter({has:page.getByRole('button',{name:'Move stock',exact:true})});
    await form.locator('[name=action]').selectOption('return');
    if(await form.evaluate(f=>new FormData(f).get('action'))!=='return')throw Error('Ambiguous movement action');
    if(process.env.ASSET_UI_SCREENSHOT)await page.screenshot({path:process.env.ASSET_UI_SCREENSHOT,fullPage:true});
   }
   if(url.includes('cash-counter')){
    const form=page.locator('form').filter({has:page.locator('[name=confirmed_adjustment]')});
    if(await form.count()!==1)throw Error('Missing admin reconciliation form');
   }
  }
  console.log('Asset lifecycle, existing inventory and admin cash control: 390/1280 px passed; movement actions verified.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
