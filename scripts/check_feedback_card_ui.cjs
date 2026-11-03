const {chromium}=require('C:/Users/hp/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const {execFileSync}=require('node:child_process');
const fs=require('node:fs');
const assert=require('node:assert/strict');
const html=execFileSync('C:/Brownberries/brownberries-cafe-operations/.venv/Scripts/python.exe',['-X','utf8','-c',`from flask import Flask,render_template
app=Flask(__name__,template_folder='templates')
with app.test_request_context('/'):
 print(render_template('cafe/feedback_card.html', table_name='T08',order_refs='D016, D017, D018',issued_at='03 Nov 2026, 03:45 PM',items=[dict(id=i,name=n) for i,n in enumerate(['Classic Cold Coffee','Cappuccino','Paneer Tikka Sandwich','Continental Vegetable Pizza'])]))`],{encoding:'utf8'});
(async()=>{const browser=await chromium.launch({headless:true});try{
 const page=await browser.newPage({viewport:{width:390,height:850}});
 await page.route('http://feedback.test/static/images/cafe-logo.png',r=>r.fulfill({contentType:'image/png',body:fs.readFileSync('static/images/cafe-logo.png')}));
 await page.route('http://feedback.test/card',r=>r.fulfill({contentType:'text/html',body:html}));
 await page.addInitScript(()=>{window.print=()=>{window.printCalls=(window.printCalls||0)+1;};});
 await page.goto('http://feedback.test/card');
 await page.getByRole('button',{name:'Print feedback card'}).click();
 await page.waitForFunction(()=>window.printCalls===1);
 assert.equal(await page.locator('tbody tr').count(),4);
 assert.equal(await page.locator('.circle').count(),20);
 await page.emulateMedia({media:'print'});
 await page.evaluate(()=>window.dispatchEvent(new Event('beforeprint')));
 const dim=await page.locator('.paper').evaluate(e=>({width:e.getBoundingClientRect().width,scroll:e.scrollWidth}));
 assert(Math.abs(dim.width-72*96/25.4)<1);assert(dim.scroll<=Math.ceil(dim.width));
 assert.match(await page.locator('#feedback-page-size').textContent(),/size: 80mm \d+mm/);
 await page.screenshot({path:'C:/Users/hp/Documents/Codex/feedback-card-preview.png',fullPage:true});
 await page.pdf({path:'C:/Users/hp/Documents/Codex/feedback-card-preview.pdf',preferCSSPageSize:true});
 console.log('PASS: 72mm content, 80mm page, 5 circles/item, image-loaded print, no clipping.');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
