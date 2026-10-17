// Render and inspect disposable fixtures, without touching real staff records.
const {spawnSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const result = spawnSync(process.env.PYTHON || 'python', ['-c', `
import sys
sys.path.insert(0, 'tests')
from test_staff_proxy import StaffProxyTests
t=StaffProxyTests(); t.setUp()
try:
    r=t.client.get('/cafe/staff?section=payroll_summary&payroll_month=12&payroll_year=2026')
    assert r.status_code==200
    print(r.get_data(as_text=True))
finally:
    t.tearDown()
`], {encoding:'utf8', env:{...process.env,PYTHONIOENCODING:'utf-8'}, maxBuffer:4*1024*1024});
if(result.status) throw new Error(result.stderr);
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage();
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if(url.pathname.startsWith('/static/') && !url.pathname.includes('..')) {
        const file = path.join(process.cwd(), decodeURIComponent(url.pathname));
        if(fs.existsSync(file)) return route.fulfill({path:file});
      }
      return route.fulfill({contentType:'text/html',body:result.stdout});
    });
    await page.goto('http://fixture.local/cafe/staff');
    for(const width of [390,1280]) {
      await page.setViewportSize({width,height:900});
      if(await page.evaluate(() => document.documentElement.scrollWidth) > width) throw new Error('Viewport overflow');
    }
    const text = await page.locator('body').innerText();
    for(const expected of ['First Half','Second Half','16–31','21 Dec 2026','06 Jan 2027','Gross Estimate']) {
      if(!text.includes(expected)) throw new Error('Missing '+expected);
    }
    console.log('PASS: Payroll period labels, December rollover and mobile/desktop viewport.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
