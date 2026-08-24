import assert from 'node:assert/strict';
import {
  canonicalDetailUrl,
  canDownload,
  extractAwemeIds,
  getSettingsWithFallback,
  parseSecUid,
} from './popup_core.js';

async function run() {
  // Settings fallback
  const settings = await getSettingsWithFallback(async () => {
    const error = new Error('Not Found');
    error.status = 404;
    throw error;
  });
  assert.deepEqual(settings, { download_output_dir: '', wrap_download_folder: false });

  // extractAwemeIds across scenarios
  assert.deepEqual(
    extractAwemeIds('https://www.douyin.com/video/7652964976545565988'),
    ['7652964976545565988'],
  );
  assert.deepEqual(
    extractAwemeIds('https://www.douyin.com/note/7652964976545565988?from_tab_name=main'),
    ['7652964976545565988'],
  );
  assert.deepEqual(
    extractAwemeIds(
      'https://www.douyin.com/user/MS4w?from_tab_name=main&modal_id=7652964976545565988&vid=7627686789402103076',
    ),
    ['7652964976545565988', '7627686789402103076'],
  );
  assert.deepEqual(
    extractAwemeIds('https://www.douyin.com/jingxuan?modal_id=7627686789402103076'),
    ['7627686789402103076'],
  );
  assert.deepEqual(
    extractAwemeIds('https://www.douyin.com/jingxuan/search/xxx?modal_id=7627686789402103076'),
    ['7627686789402103076'],
  );
  assert.deepEqual(
    extractAwemeIds('https://www.douyin.com/user/self?from_tab_name=main&modal_id=7627686789402103076'),
    ['7627686789402103076'],
  );

  // canonicalDetailUrl collapses all the above
  assert.equal(
    canonicalDetailUrl('https://www.douyin.com/user/MS4w?modal_id=7652964976545565988'),
    'https://www.douyin.com/video/7652964976545565988',
  );
  assert.equal(
    canonicalDetailUrl('https://www.douyin.com/jingxuan/search/xxx?modal_id=7627686789402103076'),
    'https://www.douyin.com/video/7627686789402103076',
  );
  assert.equal(
    canonicalDetailUrl('https://v.douyin.com/iJ5Rq2xY/'),
    'https://v.douyin.com/iJ5Rq2xY/',
  );

  // canDownload across scenarios
  assert.equal(canDownload('https://www.douyin.com/video/7652964976545565988'), true);
  assert.equal(canDownload('https://www.douyin.com/note/7652964976545565988'), true);
  assert.equal(canDownload('https://v.douyin.com/iJ5Rq2xY/'), true);
  assert.equal(canDownload('https://www.douyin.com/user/MS4w?modal_id=7652964976545565988'), true);
  assert.equal(canDownload('https://www.douyin.com/jingxuan?modal_id=7627686789402103076'), true);
  assert.equal(canDownload('https://www.douyin.com/jingxuan/search/xxx?modal_id=7627686789402103076'), true);
  assert.equal(canDownload('https://www.douyin.com/user/self?modal_id=7627686789402103076'), true);
  assert.equal(canDownload('https://www.douyin.com/user/MS4w'), false);

  // parseSecUid
  assert.equal(parseSecUid('https://www.douyin.com/user/MS4w?from_tab_name=main'), 'MS4w');
  assert.equal(parseSecUid('https://www.douyin.com/video/1'), '');
  assert.equal(parseSecUid('not a url'), '');
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
