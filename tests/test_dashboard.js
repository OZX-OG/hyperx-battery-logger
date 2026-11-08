process.env.TZ = 'America/New_York';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'battery_dashboard.html'), 'utf8');
const src = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));
const pick = (name) => {
  const m = new RegExp(`^(?:const|let|function) ${name}\\b[\\s\\S]*?(?=^(?:const|let|function|async function|document|window|load|setInterval|syncRanges)\\b)`, 'm').exec(src);
  if (!m) throw new Error(`${name} not found in the dashboard`);
  return m[0];
};
const names = ['H', 'GAP', 'MIN_SESSION', 'OFF_CHARGE', 'VJUMP', 'SURE', 'SETTLE', 'FORECAST_DAYS', 'LOW', 'pcOffset', 'tzOf',
  'shift', 'parseT', 'pc', 'unpc', 'build', 'fullVolts', 'forecast', 'dayKey', 'monthKey', 'selectionStats', 'forecastLine', 'fitRange'];
const ctx = vm.createContext({});
vm.runInContext(names.map(pick).join('\n') + '\nthis.api = { build, forecast, dayKey, parseT, selectionStats, forecastLine, fitRange };', ctx);
const { build, forecast, dayKey, selectionStats, forecastLine, fitRange } = ctx.api;

const MIN = 60e3, H = 60 * MIN;
const iso = (ms) => new Date(ms).toISOString().replace('.000Z', '+00:00');
const at = (s) => Date.parse(s + ':00Z');

function run(start, end, from, to, extra = {}) {
  const out = [];
  const a = at(start), b = at(end);
  for (let t = a; t <= b; t += 5 * MIN) {
    const pct = Math.round(from + (to - from) * (t - a) / (b - a || 1));
    out.push({ t: iso(t), state: 'connected', pct, mv: 3400 + pct * 8, ...extra });
  }
  return out;
}
const off = (t) => ({ t: iso(at(t)), state: 'disconnected' });
const model = (readings) => build({ live: { t: readings[readings.length - 1].t, state: 'disconnected' }, readings });
const plain = (x) => JSON.parse(JSON.stringify(x));
const hours = (m, day) => (m.byDay.get(day) || 0) / H;

test('a night with the PC off does not count as headset time', () => {
  const m = model([
    ...run('2026-11-04T20:00', '2026-11-04T23:40', 50, 48),
    off('2026-11-05T09:00'),
    ...run('2026-11-05T12:00', '2026-11-05T14:00', 48, 46),
  ]);
  assert.ok(Math.abs(hours(m, '2026-11-04') - 3 - 40 / 60) < 0.01);
  assert.ok(Math.abs(hours(m, '2026-11-05') - 2) < 0.01);
});

test('a short disconnect still counts up to the disconnect', () => {
  const m = model([...run('2026-11-04T10:00', '2026-11-04T11:00', 60, 59), off('2026-11-04T11:05')]);
  assert.ok(Math.abs(hours(m, '2026-11-04') - 65 / 60) < 0.01);
});

test('one discharge across restarts is one row, with each on-period counted', () => {
  const m = model([
    ...run('2026-11-01T09:00', '2026-11-01T10:30', 30, 100, { chg: 1 }),
    ...run('2026-11-01T10:35', '2026-11-01T18:00', 100, 92, { chg: 0 }),
    off('2026-11-01T18:05'),
    ...run('2026-11-02T09:00', '2026-11-02T20:00', 92, 81, { chg: 0 }),
    ...run('2026-11-03T10:00', '2026-11-03T15:00', 81, 75, { chg: 0 }),
  ]);
  assert.deepStrictEqual(plain(m.history.map(h => [h.dir, h.from, h.to])), [['charging', 30, 100], ['discharging', 100, 75]]);
  const d = m.history[1];
  const count = m.usage.filter(([a, b]) => b - a >= 10 * MIN && b > d.start && a < d.end).length;
  assert.strictEqual(count, 3);
  assert.strictEqual(m.charged, 70);
});

test('charging with the headset off shows up as its own row and counts as charged', () => {
  const m = model([
    ...run('2026-11-01T09:00', '2026-11-01T19:00', 60, 50),
    off('2026-11-01T19:05'),
    ...run('2026-11-02T09:00', '2026-11-02T12:00', 100, 97),
  ]);
  assert.deepStrictEqual(plain(m.history.map(h => [h.dir, !!h.off, h.from, h.to])),
    [['discharging', false, 60, 50], ['charging', true, 50, 100], ['discharging', false, 100, 97]]);
  assert.strictEqual(m.charged, 50);
});

test('a 1% bounce after a rest is not a charge', () => {
  const m = model([
    ...run('2026-11-01T09:00', '2026-11-01T19:00', 60, 50),
    ...run('2026-11-02T09:00', '2026-11-02T19:00', 51, 41),
  ]);
  assert.strictEqual(m.history.length, 1);
  assert.strictEqual(m.charged, 0);
});

test('drain uses all discharge time, not only long sessions', () => {
  const m = model([
    ...run('2026-11-01T09:00', '2026-11-01T19:00', 60, 50),
    ...run('2026-11-01T20:00', '2026-11-01T20:40', 50, 50),
  ]);
  assert.ok(Math.abs(m.drain - -10 / (10 + 40 / 60)) < 0.01);
});

test('forecast spreads the remaining hours over your daily use', () => {
  const m = model([
    ...run('2026-11-01T08:00', '2026-11-01T18:00', 80, 70),
    ...run('2026-11-02T08:00', '2026-11-02T18:00', 70, 60),
    ...run('2026-11-03T08:00', '2026-11-03T18:00', 60, 50),
  ]);
  const now = at('2026-11-04T08:00');
  const fc = forecast(m, now);
  assert.ok(Math.abs(fc.useMs / H - 50) < 0.5);
  assert.ok(Math.abs(fc.perDay / H - 10) < 0.01);
  assert.strictEqual(fc.days, 3);
  assert.ok(Math.abs((fc.until - now) / (24 * H) - 5) < 0.1);
});

test('no forecast while charging', () => {
  const m = model(run('2026-11-01T08:00', '2026-11-01T10:00', 50, 80, { chg: 1 }));
  assert.strictEqual(forecast(m, at('2026-11-02T08:00')), null);
});

test('days follow the PC time zone from the log, not the browser', () => {
  const m = model(run('2026-11-04T23:00', '2026-11-04T23:55', 50, 50));
  assert.strictEqual(dayKey(at('2026-11-04T23:30')), '2026-11-04');
  assert.ok(hours(m, '2026-11-04') > 0.9);
});

test('voltage after a full charge is read once it has settled', () => {
  const m = model([
    ...run('2026-11-01T08:00', '2026-11-01T10:00', 70, 100, { chg: 1 }),
    { t: iso(at('2026-11-01T10:02')), state: 'connected', pct: 100, mv: 4185, chg: 0 },
    { t: iso(at('2026-11-01T10:15')), state: 'connected', pct: 100, mv: 4112, chg: 0 },
    ...run('2026-11-01T10:20', '2026-11-01T12:00', 99, 98, { chg: 0 }),
  ]);
  assert.deepStrictEqual(plain(m.fullVolts.map(v => v.mv)), [4112]);
});

test('a selected span reports on-time, battery used and drain for just that span', () => {
  const m = model([
    ...run('2026-11-01T08:00', '2026-11-01T18:00', 80, 70),
    off('2026-11-01T18:05'),
    ...run('2026-11-02T08:00', '2026-11-02T18:00', 70, 50),
  ]);
  const s = selectionStats(m, at('2026-11-02T08:00'), at('2026-11-02T13:00'));
  assert.strictEqual(s.first.pct, 70);
  assert.strictEqual(s.last.pct, 60);
  assert.strictEqual(s.disPct, 10);
  assert.ok(Math.abs(s.onMs / H - 5) < 0.01);
  assert.ok(Math.abs(s.drain - 2) < 0.01);
  assert.strictEqual(s.chgPct, 0);
  assert.strictEqual(s.charge, null);
});

test('a selection across the night counts only time the headset was on', () => {
  const m = model([
    ...run('2026-11-01T08:00', '2026-11-01T18:00', 80, 70),
    off('2026-11-01T18:05'),
    ...run('2026-11-02T08:00', '2026-11-02T18:00', 70, 50),
  ]);
  const s = selectionStats(m, at('2026-11-01T16:00'), at('2026-11-02T10:00'));
  assert.ok(Math.abs(s.onMs / H - (2 + 5 / 60 + 2)) < 0.01);
  assert.strictEqual(s.sessions, 2);
  assert.strictEqual(s.disPct, 2 + 4);
  assert.ok(Math.abs(s.drain - 6 / 4) < 0.01);
});

test('a selection over a charge reports charging speed separately', () => {
  const m = model([
    ...run('2026-11-01T08:00', '2026-11-01T10:00', 40, 30, { chg: 0 }),
    ...run('2026-11-01T10:05', '2026-11-01T12:05', 30, 90, { chg: 1 }),
  ]);
  const s = selectionStats(m, at('2026-11-01T08:00'), at('2026-11-01T12:05'));
  assert.strictEqual(s.disPct, 10);
  assert.ok(Math.abs(s.charge - 60 / (125 / 60)) < 0.01);
  assert.strictEqual(s.max, 90);
  assert.strictEqual(s.min, 30);
});

test('forecast line runs from the last reading to empty, crossing 20% on the way', () => {
  const m = model([
    ...run('2026-11-01T08:00', '2026-11-01T18:00', 80, 70),
    ...run('2026-11-02T08:00', '2026-11-02T18:00', 70, 60),
    ...run('2026-11-03T08:00', '2026-11-03T18:00', 60, 50),
  ]);
  const now = at('2026-11-04T08:00');
  const fl = forecastLine(m, now);
  assert.strictEqual(fl.from.pct, 50);
  assert.strictEqual(fl.empty, forecast(m, now).until);
  // linear from 50% at the last reading to 0% at empty: 20% is 3/5 of the way
  assert.ok(Math.abs((fl.low - fl.from.t) / (fl.empty - fl.from.t) - 0.6) < 1e-9);
});

test('no forecast line while charging, and no 20% mark once below it', () => {
  assert.strictEqual(forecastLine(model(run('2026-11-01T08:00', '2026-11-01T10:00', 50, 80, { chg: 1 })), at('2026-11-02T08:00')), null);
  const m = model(run('2026-11-01T08:00', '2026-11-01T18:00', 25, 15));
  assert.strictEqual(forecastLine(m, at('2026-11-01T18:00')).low, null);
});

test('clicking a session picks the smallest range that shows it', () => {
  const now = at('2026-11-08T12:00');
  assert.strictEqual(fitRange(now - 5 * H, now), 1);
  assert.strictEqual(fitRange(now - 3 * 24 * H, now), 7);
  assert.strictEqual(fitRange(now - 20 * 24 * H, now), 30);
  assert.strictEqual(fitRange(now - 60 * 24 * H, now), 0);
});
