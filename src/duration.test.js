import assert from 'node:assert/strict';
import { formatDuration } from './duration.js';

assert.equal(formatDuration(1), '1秒');
assert.equal(formatDuration(42), '42秒');
assert.equal(formatDuration(65), '1分钟5秒');
assert.equal(formatDuration(3600), '1小时');
assert.equal(formatDuration(0), '0秒');
assert.equal(formatDuration(null), '-');
