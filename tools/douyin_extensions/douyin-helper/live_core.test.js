// live_core.test.js — node 直跑：node live_core.test.js（仓库 type:module，走 ESM）
//
// 覆盖：
//   1. parseCnCount 中文数量解析（万/亿/千分位/"+"封顶标记）
//   2. formatCount 展示规则（≥10万 永远展示 "10万+"，亿级照抄）
//   3. summarizeEnter 对真实抓包样本（tests/live_room_analysis/samples/dy_enter_3587.json）
//      的字段归一
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert';
import { fileURLToPath } from 'node:url';

await import('./live_core.js');
const core = globalThis.__dyDlhLiveCore;
assert.ok(core, 'live_core.js 未注册 globalThis.__dyDlhLiveCore');

const __dirname = path.dirname(fileURLToPath(import.meta.url));

let passed = 0;
function check(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`  ok  ${name}`);
  } catch (e) {
    console.error(`FAIL  ${name}\n      ${e.message}`);
    process.exitCode = 1;
  }
}

// ---------- parseCnCount ----------
check('parse: 纯整数 1234', () => {
  const p = core.parseCnCount('1234');
  assert.deepStrictEqual([p.ok, p.num, p.capped, p.scale], [true, 1234, false, '']);
});
check('parse: 千分位 12,340', () => {
  const p = core.parseCnCount('12,340');
  assert.strictEqual(p.num, 12340);
});
check('parse: 8.1万 → 81000', () => {
  const p = core.parseCnCount('8.1万');
  assert.deepStrictEqual([p.num, p.scale], [81000, '万']);
});
check('parse: 10万+ 标记 capped', () => {
  const p = core.parseCnCount('10万+');
  assert.deepStrictEqual([p.ok, p.num, p.capped], [true, 100000, true]);
});
check('parse: 3.4亿 → 3.4e8', () => {
  const p = core.parseCnCount('3.4亿');
  assert.strictEqual(p.num, 340000000);
});
check('parse: 非数字返回 ok=false', () => {
  assert.strictEqual(core.parseCnCount('在线').ok, false);
  assert.strictEqual(core.parseCnCount('').ok, false);
  assert.strictEqual(core.parseCnCount(null).ok, false);
});

// ---------- formatCount ----------
check('format: 10万+ → 10万+', () => {
  assert.strictEqual(core.formatCount(core.parseCnCount('10万+'), false), '10万+');
});
check('format: 具体数字 100234 达到阈值即封顶 10万+', () => {
  assert.strictEqual(core.formatCount(core.parseCnCount('100234'), false), '10万+');
});
check('format: 99999 以下展示千分位具体数字', () => {
  assert.strictEqual(core.formatCount(core.parseCnCount('98234'), false), '98,234');
});
check('format: 曾达到 10万（latched）后回落仍展示 10万+', () => {
  assert.strictEqual(core.formatCount(core.parseCnCount('999'), true), '10万+');
});
check('format: 8.1万 原样照抄', () => {
  assert.strictEqual(core.formatCount(core.parseCnCount('8.1万'), false), '8.1万');
});
check('format: 1.2万+ 原样照抄（未到 10万 的封顶文案）', () => {
  assert.strictEqual(core.formatCount(core.parseCnCount('1.2万+'), false), '1.2万+');
});
check('format: 亿级照抄页面文案', () => {
  assert.strictEqual(core.formatCount(core.parseCnCount('3.4亿'), false), '3.4亿');
  assert.strictEqual(core.formatCount(core.parseCnCount('1.2亿+'), false), '1.2亿+');
});
check('format: 解析失败返回 null', () => {
  assert.strictEqual(core.formatCount(core.parseCnCount('--'), false), null);
});

// ---------- summarizeEnter ----------
check('summarize: 空_payload 安全返回 ok=false', () => {
  assert.strictEqual(core.summarizeEnter(null).ok, false);
  assert.strictEqual(core.summarizeEnter({}).ok, false);
});
check('summarize: 构造最小 payload', () => {
  const s = core.summarizeEnter({
    data: {
      data: [{
        id_str: '7664058258907188031',
        status: 2,
        title: '测试房间',
        like_count: 62610,
        owner: { nickname: '测试主播' },
        stats: { user_count_str: '1218', total_user_str: '3万+', like_count: 0 },
        room_view_stats: { display_short: '1218' },
      }],
      user: { nickname: '测试主播' },
    },
  });
  assert.strictEqual(s.ok, true);
  assert.strictEqual(s.status, 2);
  assert.strictEqual(s.title, '测试房间');
  assert.strictEqual(s.anchor, '测试主播');
  assert.strictEqual(s.viewersText, '1218');
  assert.strictEqual(s.totalText, '3万+');
  assert.strictEqual(s.likeNum, 62610);
  assert.strictEqual(s.room_id_str, '7664058258907188031');
});

// ---------- 真实抓包样本回放 ----------
const samplePath = path.join(__dirname, '..', '..', '..', 'tests', 'live_room_analysis',
  'samples', 'dy_enter_3587.json');
if (fs.existsSync(samplePath)) {
  const sample = JSON.parse(fs.readFileSync(samplePath, 'utf-8'));
  const bodyText = sample.responseBody && sample.responseBody.text;
  check('真实样本: enter 响应归一（3587 抓包）', () => {
    assert.ok(bodyText, '样本缺 responseBody.text');
    const s = core.summarizeEnter(JSON.parse(bodyText));
    assert.strictEqual(s.ok, true);
    assert.strictEqual(s.status, 2);
    assert.strictEqual(s.anchor, '歌手刘筝');
    assert.ok(s.title.includes('乐器'), `title=${s.title}`);
    assert.strictEqual(s.viewersText, '1218');
    assert.strictEqual(s.totalText, '3万+');
    assert.strictEqual(s.likeNum, 62610);
    // 端到端：3万+ 不到 10万，展示原样；like 62610 → "62,610"
    assert.strictEqual(core.formatCount(core.parseCnCount(s.totalText), false), '3万+');
    assert.strictEqual(s.likeNum.toLocaleString('en-US'), '62,610');
  });
} else {
  console.log('  skip 真实样本（未找到 dy_enter_3587.json）');
}

console.log(`\n${passed} passed${process.exitCode ? '（有失败）' : ''}`);
