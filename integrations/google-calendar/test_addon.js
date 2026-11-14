// Apps Script の外（Node）で、コードの作り方とハッシュが Yanua とそろっているかを確かめる。
// 使い方: node integrations/google-calendar/test_addon.js <コード>   → ハッシュを1行で出す
//         node integrations/google-calendar/test_addon.js --self      → 自己テスト
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const sandbox = {
  module: { exports: {} },
  Utilities: {
    DigestAlgorithm: { SHA_256: 'sha256' },
    Charset: { UTF_8: 'utf8' },
    // Apps Script と同じく、符号つきのバイト（-128〜127）の配列を返す
    computeDigest: (alg, text) => Array.from(crypto.createHash(alg).update(text, 'utf8').digest()).map(b => (b > 127 ? b - 256 : b)),
    getUuid: () => crypto.randomUUID(),
    formatDate: (d, tz, pattern) => {
      const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
        timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false
      }).formatToParts(d).map(p => [p.type, p.value]));
      return pattern === 'yyyy-MM-dd' ? `${parts.year}-${parts.month}-${parts.day}` : `${parts.hour}:${parts.minute}`;
    },
  },
  PropertiesService: { getScriptProperties: () => ({ getProperty: k => (k === 'INTERNAL_DOMAINS' ? 'example.co.jp' : null) }) },
  Session: { getActiveUser: () => ({ getEmail: () => 'takahashi.m@example.co.jp' }) },
};
sandbox.module.exports = {};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, 'Code.gs'), 'utf8'), sandbox);
const A = sandbox.module.exports;

if (process.argv[2] !== '--self') {
  console.log(A.hashCode(process.argv[2]));
  process.exit(0);
}

const assert = require('assert');
for (let i = 0; i < 200; i++) {
  const c = A.generateCode();
  assert.strictEqual(c.length, 8);
  assert.ok([...c].every(ch => A.ALPHABET.includes(ch)), c);
}
assert.strictEqual(A.normalizeCode('ｍｒｋｔ ７ｑ４ａ'), 'MRKT7Q4A');
assert.strictEqual(A.formatCode('mrkt7q4a'), 'MRKT-7Q4A');

const info = A.eventInfo_({
  id: 'ev1', status: 'confirmed', summary: '新商品の打ち合わせ',
  start: { dateTime: '2026-11-16T15:00:00+09:00' },
  organizer: { email: 'takahashi.m@example.co.jp', self: true },
  attendees: [
    { email: 'takahashi.m@example.co.jp', organizer: true, self: true },
    { email: 'room-5f-2@resource.calendar.google.com', displayName: '5階 第2会議室', resource: true },
    { email: 'kimura@abc-shoji.example', displayName: '木村 誠' },
    { email: 'sato.h@example.co.jp' },
    { email: 'declined@abc-shoji.example', responseStatus: 'declined' },
  ],
});
assert.strictEqual(info.date, '2026-11-16');
assert.strictEqual(info.time, '15:00');
assert.strictEqual(info.room, '5階 第2会議室');
assert.strictEqual(info.hasRoom, true);
assert.deepStrictEqual(info.guests.map(g => g.email), ['kimura@abc-shoji.example']);   // 社内と辞退者は除く
assert.strictEqual(A.eventInfo_({ id: 'x', start: { date: '2026-11-16' } }), null);      // 終日は対象外
const body = A.inviteBody_(info, { company: 'ABC商事', name: '木村 誠' }, 'MRKT7Q4A', 'takahashi.m@example.co.jp', '内線4100');
assert.ok(body.includes('MRKT-7Q4A') && body.includes('5階 第2会議室') && body.includes('内線4100'));
console.log('ok');
