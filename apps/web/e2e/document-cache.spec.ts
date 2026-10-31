import { chromium, test, expect } from '@playwright/test';

test('chat artifact metadata and image survive a browser restart without file API requests', async ({ baseURL }, testInfo) => {
  let metadata = 0, files = 0;
  const profile = testInfo.outputPath('artifact-profile');
  const portrait = Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="900" height="1600"><rect width="900" height="1600" fill="#f7f6f3"/><text x="40" y="80" font-size="40">Top of portrait</text><text x="40" y="1550" font-size="40">Bottom of portrait</text></svg>');
  for (let attempt = 0; attempt < 2; attempt++) {
    const context = await chromium.launchPersistentContext(profile, { headless: true, baseURL, viewport: { width: 1280, height: 850 } });
    try {
      await context.route('**/api/v1/**', async route => {
        const path = new URL(route.request().url()).pathname;
        if (path === '/api/v1/documents/cache-image') {
          metadata++;
          if (attempt) return route.abort();
          return route.fulfill({ json: { id: 'cache-image', name: 'cached.svg', mime_type: 'image/svg+xml', file_type: 'svg', file_size: portrait.length } });
        }
        if (path === '/api/v1/documents/cache-image/preview/content') {
          files++;
          if (attempt) return route.abort();
          return route.fulfill({ contentType: 'image/svg+xml', body: portrait });
        }
        return route.fulfill({ json: [] });
      });
      const page = await context.newPage();
      await page.goto('/e2e/fixtures/document-cache.html?artifact');
      const image = page.getByRole('img', { name: 'cached.svg', exact: true });
      await expect(image).toBeVisible();
      await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBeGreaterThan(0);
      await page.getByRole('button', { name: 'View full screen cached.svg', exact: true }).click();
      const dialog = page.getByRole('dialog', { name: 'cached.svg', exact: true });
      const fullImage = dialog.getByRole('img', { name: 'cached.svg' });
      await expect(fullImage).toBeVisible();
      const viewport = dialog.getByRole('region', { name: 'cached.svg' });
      await expect.poll(() => viewport.evaluate(element => element.scrollHeight - element.clientHeight)).toBeLessThanOrEqual(1);
      await expect.poll(() => fullImage.evaluate(element => element.getBoundingClientRect().height)).toBeLessThan(850);
      expect((await dialog.boundingBox())!.width).toBeGreaterThan(1280 * 0.9);
      await dialog.getByRole('button', { name: 'Actual size (100%)' }).click();
      await expect.poll(() => fullImage.evaluate(element => element.getBoundingClientRect().height)).toBe(1600);
      await dialog.getByRole('button', { name: 'Close', exact: true }).click();
    } finally { await context.close(); }
  }
  expect(metadata).toBe(1);
  expect(files).toBe(1);
});

test('a protected raw chat file stops after inactivity and recovers on retry', async ({ page }) => {
  await page.clock.install();
  let calls = 0;
  let release!: () => void;
  const blocked = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/v1/fs/cache-test/uploads/notes.txt*', async route => {
    expect(route.request().headers().authorization).toMatch(/^Bearer /);
    if (++calls === 1) {
      await blocked;
      await route.abort().catch(() => {});
    } else await route.fulfill({ contentType: 'text/plain', body: 'Recovered raw file.' });
  });
  await page.goto('/e2e/fixtures/document-cache.html?raw');
  await expect.poll(() => calls).toBe(1);
  await page.clock.fastForward(60_001);
  const retry = page.getByRole('button', { name: 'Retry', exact: true });
  try {
    await expect(retry).toBeVisible();
    await expect(page.getByText('File download stalled for 60 seconds. Please retry.', { exact: true })).toBeVisible();
  } finally { release(); }
  await retry.click();
  await expect(page.getByText('Recovered raw file.', { exact: true })).toBeVisible();
  await expect(retry).toBeHidden();
  expect(calls).toBe(2);
});

test('CSV retries metadata once, then opens from disk after a reload with APIs unavailable', async ({ page }) => {
  let metadata = 0, files = 0;
  let unavailable = false;
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/documents/cache-csv') {
      metadata++;
      if (metadata === 1 || unavailable) return route.fulfill({status:503,headers:{'Retry-After':'0.01'},json:{detail:'temporary'}});
      return route.fulfill({json:{id:'cache-csv',name:'cached.csv',file_type:'csv',mime_type:'text/csv',file_size:22,current_user_capabilities:['view','download']}});
    }
    if (path === '/api/v1/documents/cache-csv/preview/content') {
      files++;
      return route.fulfill({status:unavailable?503:200,contentType:'text/csv',body:'name,value\nCacheProof,7'});
    }
    return route.fulfill({json:[]});
  });
  await page.goto('/e2e/fixtures/document-cache.html');
  await expect(page.getByText('CacheProof',{exact:true})).toBeVisible();
  expect(metadata).toBe(2); expect(files).toBe(1);
  unavailable=true;
  await page.reload();
  await expect(page.getByText('CacheProof',{exact:true})).toBeVisible();
  expect(metadata).toBe(2); expect(files).toBe(1);
  // The application's legacy session reset must not delete persistent copies.
  await page.evaluate(()=> (window as any).cacheTestApi.documents.clearDownloadCache());
  await page.reload();
  await expect(page.getByText('CacheProof',{exact:true})).toBeVisible();
  expect(metadata).toBe(2); expect(files).toBe(1);
});

test('an exhausted first-load failure offers Retry and a recovered server opens without page refresh', async ({page})=>{
  let healthy=false, calls=0;
  await page.route('**/api/v1/**', async route=>{
    const path=new URL(route.request().url()).pathname;
    if(path==='/api/v1/documents/cache-csv'){
      calls++;
      return healthy ? route.fulfill({json:{id:'cache-csv',name:'recovered.csv',file_type:'csv',mime_type:'text/csv'}}) : route.fulfill({status:502,headers:{'Retry-After':'0.01'},json:{detail:'upstream'}});
    }
    if(path.endsWith('/preview/content')) return route.fulfill({contentType:'text/csv',body:'name,value\nRecovered,1'});
    return route.fulfill({json:[]});
  });
  await page.goto('/e2e/fixtures/document-cache.html');
  await expect(page.getByRole('button',{name:'Retry',exact:true})).toBeVisible();
  expect(calls).toBe(3);
  healthy=true;
  await page.getByRole('button',{name:'Retry',exact:true}).click();
  await expect(page.getByText('Recovered',{exact:true})).toBeVisible();
  expect(calls).toBe(4);
});

test('saving a document invalidates its cached metadata and preview',async({page})=>{
  let version=1, files=0, metadata=0;
  await page.route('**/api/v1/**',async route=>{
    const path=new URL(route.request().url()).pathname;
    if(route.request().method()==='PUT'){ version++; return route.fulfill({json:{saved:true}}); }
    if(path==='/api/v1/documents/cache-csv'){metadata++;return route.fulfill({json:{id:'cache-csv',name:'saved.csv',file_type:'csv',mime_type:'text/csv'}});}
    if(path.endsWith('/preview/content')){files++;return route.fulfill({contentType:'text/csv',body:`name,value\nVersion${version},${version}`});}
    return route.fulfill({json:[]});
  });
  await page.goto('/e2e/fixtures/document-cache.html');
  await expect(page.getByText('Version1',{exact:true})).toBeVisible();
  await page.evaluate(()=> (window as any).cacheTestApi.documents.saveContent('cache-csv','updated'));
  await page.reload();
  await expect(page.getByText('Version2',{exact:true})).toBeVisible();
  expect(files).toBe(2);expect(metadata).toBe(2);
});
