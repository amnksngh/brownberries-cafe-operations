// Isolated browser checks. window.open/print are mocked: no physical printing.
const {chromium} = require('C:/Users/hp/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const {execFileSync} = require('node:child_process');
const assert = require('node:assert/strict');
const python = 'C:/Brownberries/brownberries-cafe-operations/.venv/Scripts/python.exe';
function fixture(expression) {
  return execFileSync(python, ['-c', `import sys; sys.path.insert(0, 'tests')\nfrom test_thermal_receipt import ThermalReceiptTests\nfrom flask import render_template\nt=ThermalReceiptTests(); t.setUp()\nwith t.app.test_request_context('/'):\n print(${expression})`], {encoding:'utf8', env:{...process.env, PYTHONIOENCODING:'utf-8'}});
}
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage();
    const prompt = fixture("render_template('cafe/receipt_print_prompt.html')");
    await page.route('http://receipt.test/**', route => route.fulfill({contentType:'text/html', body:prompt}));
    await page.addInitScript(() => { window.open = url => {window.openedReceipt=url;}; });
    await page.goto('http://receipt.test/cashier?print_receipt=7');
    assert(await page.locator('dialog').isVisible());
    await page.getByRole('button', {name:'No', exact:true}).click();
    assert(!(await page.locator('dialog').isVisible()));
    assert.equal(await page.evaluate(() => window.openedReceipt), undefined);
    await page.reload();
    assert(!(await page.locator('dialog').isVisible()));
    await page.goto('http://receipt.test/cashier?print_receipt=8');
    await page.getByRole('button', {name:'Yes, print'}).click();
    assert.equal(await page.evaluate(() => window.openedReceipt), '/cafe/receipt/8?format=thermal&print=1');
    const receipt = fixture('t.render_receipt(thermal=True)');
    await page.setContent(receipt);
    await page.emulateMedia({media:'print'});
    await page.evaluate(() => window.dispatchEvent(new Event('beforeprint')));
    const sizes = await page.evaluate(() => ({
      width:document.querySelector('.receipt-paper').getBoundingClientRect().width,
      overflow:document.querySelector('.receipt-paper').scrollWidth,
      page:document.getElementById('thermal-page-size').textContent,
    }));
    assert(Math.abs(sizes.width - 72*96/25.4) < 1);
    assert(sizes.overflow <= Math.ceil(sizes.width));
    assert.match(sizes.page, /size: 80mm \d+mm/);
    console.log('PASS: No dismisses; refresh does not repeat; Yes opens exact receipt; thermal width and content-sized page verified.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode=1; });
