import assert from 'node:assert/strict';
import {
  buildDownloadInputText,
  mergeDownloadPreviews,
  removeDownloadPreviewItem,
} from './downloadPreviewState.js';

const first = {
  items: [
    { index: 1, url: 'https://www.douyin.com/video/111', selected: true, title: 'first' },
  ],
};
const second = {
  items: [
    { index: 1, url: 'https://www.douyin.com/video/222', selected: true, title: 'second' },
  ],
};
const duplicateFirst = {
  items: [
    { index: 1, url: 'https://www.douyin.com/video/111', selected: true, title: 'first updated' },
  ],
};

const merged = mergeDownloadPreviews(first, second, new Set(['https://www.douyin.com/video/111']));
assert.deepEqual(merged.preview.items.map((item) => item.url), [
  'https://www.douyin.com/video/111',
  'https://www.douyin.com/video/222',
]);
assert.deepEqual(merged.preview.items.map((item) => item.index), [1, 2]);
assert.deepEqual(Array.from(merged.selectedUrls).sort(), [
  'https://www.douyin.com/video/111',
  'https://www.douyin.com/video/222',
]);

const updated = mergeDownloadPreviews(merged.preview, duplicateFirst, merged.selectedUrls);
assert.deepEqual(updated.preview.items.map((item) => item.url), [
  'https://www.douyin.com/video/111',
  'https://www.douyin.com/video/222',
]);
assert.equal(updated.preview.items[0].title, 'first updated');

const removed = removeDownloadPreviewItem(
  updated.preview,
  updated.selectedUrls,
  { 'https://www.douyin.com/video/111': [1, 2] },
  'https://www.douyin.com/video/111',
);
assert.deepEqual(removed.preview.items.map((item) => item.url), ['https://www.douyin.com/video/222']);
assert.deepEqual(Array.from(removed.selectedUrls), ['https://www.douyin.com/video/222']);
assert.deepEqual(removed.selectedMedia, {});

assert.equal(
  buildDownloadInputText('ignored when preview exists', removed.preview),
  'https://www.douyin.com/video/222',
);
const empty = removeDownloadPreviewItem(removed.preview, removed.selectedUrls, removed.selectedMedia, 'https://www.douyin.com/video/222');
assert.deepEqual(empty.preview.items, []);
assert.equal(buildDownloadInputText('stale text should not submit', empty.preview), '');
assert.equal(buildDownloadInputText('raw text', null), 'raw text');
