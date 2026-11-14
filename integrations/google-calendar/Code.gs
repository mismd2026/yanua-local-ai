/**
 * Yanua 受付コード（Googleカレンダーのアドオン / Apps Script）
 *
 * 会議室と社外のゲストを入れた予定に、ゲストごとのインビテーションコードを発行して、ゲストにメールで送る。
 * 発行した予定は、スプレッドシートに「コードのハッシュ」だけを残す。Yanua はこのシートを CSV で取り込む
 *（python -m yanua visit-import 書き出したCSV）。Yanua は社外に通信しないので、カレンダーと直接はつながない。
 *
 * 使い方は2つ:
 *   1. アドオン：予定を開くと右側に出る。社外のゲストごとに会社名・お名前を確かめて「発行」
 *   2. 自動発行：ホーム画面で有効にすると、1時間ごとに、会議室と社外のゲストがある自分の予定に自動で発行する
 *      （会社名はメールアドレスのドメイン、お名前はカレンダーの表示名）
 * どちらも、予定の日時・会議室が変わったら（リスケ）シートを更新し、予定の取り消しやゲストの削除では「取消」にする。
 *
 * コードの扱いは Yanua 側（yanua/visits.py）とそろえている:
 *   紛らわしい文字を除いた8文字 / シートにはハッシュだけ / ハッシュは「大文字にして空白・ハイフンを除いた」コードの SHA-256
 *
 * スクリプトのプロパティ:
 *   SHEET_ID          発行した予定を残すスプレッドシートのID（必須）
 *   INTERNAL_DOMAINS  社内のメールドメイン（カンマ区切り）。省略時は自分のメールアドレスのドメイン
 *   HOST_CONTACT      ゲストに見せてよい、自分の連絡先の既定値（例：内線4100）
 */

var ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'; // 0 O 1 I を除く 32 文字
var CODE_LEN = 8;
var TZ = 'Asia/Tokyo';
var SHEET_NAME = 'visits';
var HEADER = ['issued_at', 'event_id', 'date', 'time', 'room', 'host_email', 'host_contact',
              'visitor_company', 'visitor_name', 'visitor_email', 'code_hash', 'status'];
var LOOKAHEAD_DAYS = 60;

// ------------------------------------------------------------------ コード（Yanua とそろえる部分）

function normalizeCode(text) {
  return String(text || '').normalize('NFKC').replace(/[\s\-ー－_]/g, '').toUpperCase();
}

function formatCode(code) {
  var c = normalizeCode(code);
  return c.slice(0, 4) + '-' + c.slice(4);
}

function toHex_(bytes) {
  return bytes.map(function (b) { return ('0' + ((b + 256) % 256).toString(16)).slice(-2); }).join('');
}

function hashCode(code) {
  return toHex_(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, normalizeCode(code), Utilities.Charset.UTF_8));
}

/** 256 は 32 で割り切れるので、バイトを 32 で割った余りで偏りなく文字を選べる。乱数は UUID v4 から取る。 */
function generateCode() {
  var bytes = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, Utilities.getUuid() + Utilities.getUuid());
  var out = '';
  for (var i = 0; i < CODE_LEN; i++) out += ALPHABET[((bytes[i] + 256) % 256) % ALPHABET.length];
  return out;
}

// ------------------------------------------------------------------ 予定の読み取り

function internalDomains_() {
  var v = PropertiesService.getScriptProperties().getProperty('INTERNAL_DOMAINS');
  if (v) return v.split(',').map(function (d) { return d.trim().toLowerCase(); }).filter(String);
  return [Session.getActiveUser().getEmail().split('@')[1].toLowerCase()];
}

function isInternal_(email, domains) {
  var d = String(email || '').split('@')[1] || '';
  return domains.indexOf(d.toLowerCase()) >= 0;
}

/** 予定から、受付に要る情報を取り出す。終日の予定や、時刻のない予定は対象外（null）。 */
function eventInfo_(ev) {
  if (!ev.start || !ev.start.dateTime) return null;
  var start = new Date(ev.start.dateTime);
  var domains = internalDomains_();
  var attendees = ev.attendees || [];
  var rooms = attendees.filter(function (a) { return a.resource && a.responseStatus !== 'declined'; })
                       .map(function (a) { return a.displayName || a.email; });
  var guests = attendees.filter(function (a) {
    return !a.resource && !isInternal_(a.email, domains) && a.responseStatus !== 'declined';
  });
  return {
    id: ev.id,
    cancelled: ev.status === 'cancelled',
    title: ev.summary || '',
    date: Utilities.formatDate(start, TZ, 'yyyy-MM-dd'),
    time: Utilities.formatDate(start, TZ, 'HH:mm'),
    room: rooms.join('、') || ev.location || '',
    hasRoom: rooms.length > 0,
    hostEmail: (ev.organizer && ev.organizer.self) ? Session.getActiveUser().getEmail() : (ev.organizer || {}).email,
    organizerIsMe: !!(ev.organizer && ev.organizer.self),
    guests: guests.map(function (a) {
      return { email: a.email.toLowerCase(), name: a.displayName || '', company: a.email.split('@')[1] };
    })
  };
}

function getEvent_(calendarId, eventId) {
  return Calendar.Events.get(calendarId, eventId);
}

// ------------------------------------------------------------------ シート

function sheet_() {
  var id = PropertiesService.getScriptProperties().getProperty('SHEET_ID');
  if (!id) throw new Error('スクリプトのプロパティ SHEET_ID を設定してください');
  var ss = SpreadsheetApp.openById(id);
  var sh = ss.getSheetByName(SHEET_NAME) || ss.insertSheet(SHEET_NAME);
  if (sh.getLastRow() === 0) {
    sh.appendRow(HEADER);
    sh.getRange('C:D').setNumberFormat('@'); // 日付・時刻を文字のまま残す（CSVで YYYY-MM-DD / HH:mm になるように）
  }
  return sh;
}

/** シートが日付や時刻に変えてしまった値も、文字にそろえて比べる */
function asText_(v, pattern) {
  return v instanceof Date ? Utilities.formatDate(v, TZ, pattern) : String(v);
}

/** その予定の行を { visitor_email: { row, values } } で返す（取消の行は除く） */
function rowsForEvent_(sh, eventId) {
  var out = {};
  var last = sh.getLastRow();
  if (last < 2) return out;
  var values = sh.getRange(2, 1, last - 1, HEADER.length).getValues();
  values.forEach(function (v, i) {
    if (v[1] === eventId && v[11] !== '取消') out[v[9]] = { row: i + 2, values: v };
  });
  return out;
}

function eventIdsInSheet_(sh) {
  var last = sh.getLastRow();
  if (last < 2) return [];
  var seen = {};
  sh.getRange(2, 1, last - 1, HEADER.length).getValues().forEach(function (v) {
    if (v[11] !== '取消') seen[v[1]] = true;
  });
  return Object.keys(seen);
}

// ------------------------------------------------------------------ 発行と同期

function inviteBody_(info, guest, code, hostName, hostContact) {
  return [
    (guest.company ? guest.company + ' ' : '') + (guest.name || '') + '様',
    '',
    'ご来訪の予定を承りました。',
    '日時：' + info.date + ' ' + info.time,
    '場所：' + (info.room || '受付でご案内します'),
    '担当：' + hostName,
    hostContact ? '連絡先：' + hostContact : null,
    '',
    '当日は受付で、次のインビテーションコードをご入力ください。',
    'インビテーションコード：' + formatCode(code),
    '（ご来訪の当日だけ使えます。ほかの方には共有しないでください）'
  ].filter(function (l) { return l !== null; }).join('\n');
}

/** 1人のゲストにコードを発行し、シートに残してメールで送る。コードはここでしか扱わない（保存しない）。 */
function issue_(sh, info, guest, hostContact) {
  var code = generateCode();
  sh.appendRow([new Date(), info.id, info.date, info.time, info.room, info.hostEmail, hostContact || '',
                guest.company, guest.name, guest.email, hashCode(code), '発行済み']);
  var me = Session.getActiveUser().getEmail();
  MailApp.sendEmail({
    to: guest.email,
    subject: '【ご来訪のご案内】' + info.date + ' ' + info.time + (info.title ? '　' + info.title : ''),
    body: inviteBody_(info, guest, code, me, hostContact),
    replyTo: me
  });
}

/**
 * 予定の今の状態にシートをそろえる（リスケ・会議室の変更・取り消し・ゲストの削除）。
 * 変わった行の数を返す。
 */
function sync_(sh, info) {
  var rows = rowsForEvent_(sh, info.id);
  var current = {};
  info.guests.forEach(function (g) { current[g.email] = true; });
  var changed = 0;
  Object.keys(rows).forEach(function (email) {
    var r = rows[email];
    if (info.cancelled || !current[email]) {
      sh.getRange(r.row, 12).setValue('取消');
      changed++;
    } else if (asText_(r.values[2], 'yyyy-MM-dd') !== info.date || asText_(r.values[3], 'HH:mm') !== info.time ||
               String(r.values[4]) !== info.room) {
      sh.getRange(r.row, 3, 1, 3).setValues([[info.date, info.time, info.room]]);
      changed++;
    }
  });
  return changed;
}

// ------------------------------------------------------------------ 自動発行（時間ごとの実行）

function autoIssueAll() {
  var sh = sheet_();
  var now = new Date();
  var res = Calendar.Events.list('primary', {
    timeMin: now.toISOString(),
    timeMax: new Date(now.getTime() + LOOKAHEAD_DAYS * 86400000).toISOString(),
    singleEvents: true, showDeleted: true, maxResults: 250
  });
  var contact = PropertiesService.getScriptProperties().getProperty('HOST_CONTACT') || '';
  var seen = {};
  (res.items || []).forEach(function (ev) {
    seen[ev.id] = true;
    var info = eventInfo_(ev);
    if (!info) return;
    sync_(sh, info);
    if (info.cancelled || !info.organizerIsMe || !info.hasRoom) return;
    var issued = rowsForEvent_(sh, info.id);
    info.guests.forEach(function (g) {
      if (!issued[g.email]) issue_(sh, info, { email: g.email, company: g.company, name: g.name || g.email.split('@')[0] }, contact);
    });
  });
  // 期間内の一覧に出てこない（消された）予定は、取消にする
  eventIdsInSheet_(sh).forEach(function (id) {
    if (seen[id]) return;
    try {
      var info = eventInfo_(getEvent_('primary', id));
      if (info) sync_(sh, info);
    } catch (err) {
      sync_(sh, { id: id, cancelled: true, guests: [] });
    }
  });
}

function enableAuto_() {
  disableAuto_();
  ScriptApp.newTrigger('autoIssueAll').timeBased().everyHours(1).create();
}

function disableAuto_() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'autoIssueAll') ScriptApp.deleteTrigger(t);
  });
}

function autoEnabled_() {
  return ScriptApp.getProjectTriggers().some(function (t) { return t.getHandlerFunction() === 'autoIssueAll'; });
}

// ------------------------------------------------------------------ アドオンの画面

function onHome(e) {
  var section = CardService.newCardSection()
    .addWidget(CardService.newTextParagraph().setText(
      '会議室と社外のゲストを入れた予定を開くと、ゲストごとにインビテーションコードを発行できます。' +
      'コードはゲストにメールで送り、シートにはハッシュだけを残します。'))
    .addWidget(CardService.newDecoratedText().setTopLabel('自動発行（1時間ごと）')
      .setText(autoEnabled_() ? '有効' : '無効'))
    .addWidget(CardService.newTextButton().setText(autoEnabled_() ? '自動発行を止める' : '自動発行を有効にする')
      .setOnClickAction(CardService.newAction().setFunctionName('onToggleAuto')));
  return CardService.newCardBuilder().setHeader(CardService.newCardHeader().setTitle('Yanua 受付コード'))
    .addSection(section).build();
}

function onToggleAuto(e) {
  if (autoEnabled_()) disableAuto_(); else enableAuto_();
  return CardService.newActionResponseBuilder().setNavigation(CardService.newNavigation().updateCard(onHome(e)))
    .setNotification(CardService.newNotification().setText(autoEnabled_() ? '自動発行を有効にしました' : '自動発行を止めました'))
    .build();
}

function message_(text) {
  return CardService.newCardBuilder().setHeader(CardService.newCardHeader().setTitle('Yanua 受付コード'))
    .addSection(CardService.newCardSection().addWidget(CardService.newTextParagraph().setText(text))).build();
}

function eventCard_(calendarId, eventId) {
  var info = eventInfo_(getEvent_(calendarId, eventId));
  if (!info) return message_('時刻のある予定だけが対象です（終日の予定には発行できません）。');
  if (!info.guests.length) return message_('社外のゲストがいない予定です。ゲストを追加して保存してから、開き直してください。');

  var issued = rowsForEvent_(sheet_(), info.id);
  var head = CardService.newCardSection()
    .addWidget(CardService.newDecoratedText().setTopLabel('日時').setText(info.date + ' ' + info.time))
    .addWidget(CardService.newDecoratedText().setTopLabel('会議室')
      .setText(info.room || '未設定（会議室を追加すると、ゲストの案内に入ります）'));
  var card = CardService.newCardBuilder().setHeader(CardService.newCardHeader().setTitle('Yanua 受付コード'))
    .addSection(head);

  var pending = 0;
  info.guests.forEach(function (g, i) {
    var sec = CardService.newCardSection().setHeader(g.email);
    if (issued[g.email]) {
      var v = issued[g.email].values;
      sec.addWidget(CardService.newTextParagraph().setText(
        '発行済み（' + asText_(v[2], 'yyyy-MM-dd') + ' ' + asText_(v[3], 'HH:mm') + '）'));
    } else {
      pending++;
      sec.addWidget(CardService.newTextInput().setFieldName('company_' + i).setTitle('会社名').setValue(g.company))
         .addWidget(CardService.newTextInput().setFieldName('name_' + i).setTitle('お名前').setValue(g.name));
    }
    card.addSection(sec);
  });

  var actions = CardService.newCardSection();
  if (pending) {
    actions.addWidget(CardService.newTextInput().setFieldName('contact').setTitle('ゲストに見せてよい連絡先（任意）')
      .setValue(PropertiesService.getScriptProperties().getProperty('HOST_CONTACT') || ''));
    actions.addWidget(CardService.newTextButton().setText('コードを発行してメールで送る')
      .setOnClickAction(CardService.newAction().setFunctionName('onIssue')
        .setParameters({ calendarId: calendarId, eventId: eventId })));
  }
  actions.addWidget(CardService.newTextButton().setText('日時・会議室の変更をシートに反映')
    .setOnClickAction(CardService.newAction().setFunctionName('onSync')
      .setParameters({ calendarId: calendarId, eventId: eventId })));
  return card.addSection(actions).build();
}

function onEventOpen(e) {
  var ev = e.calendar || {};
  if (!ev.id) return message_('予定を保存してから、開き直してください。');
  return eventCard_(ev.calendarId, ev.id);
}

function input_(e, name) {
  var f = ((e.commonEventObject || {}).formInputs || {})[name];
  return f ? String(f.stringInputs.value[0] || '').trim() : '';
}

function onIssue(e) {
  var p = e.commonEventObject.parameters;
  var sh = sheet_();
  var info = eventInfo_(getEvent_(p.calendarId, p.eventId));
  var issued = rowsForEvent_(sh, info.id);
  var contact = input_(e, 'contact');
  var count = 0;
  info.guests.forEach(function (g, i) {
    if (issued[g.email]) return;
    issue_(sh, info, { email: g.email, company: input_(e, 'company_' + i) || g.company,
                       name: input_(e, 'name_' + i) || g.name }, contact);
    count++;
  });
  return CardService.newActionResponseBuilder()
    .setNavigation(CardService.newNavigation().updateCard(eventCard_(p.calendarId, p.eventId)))
    .setNotification(CardService.newNotification().setText(count + '人にコードを送りました')).build();
}

function onSync(e) {
  var p = e.commonEventObject.parameters;
  var changed = sync_(sheet_(), eventInfo_(getEvent_(p.calendarId, p.eventId)));
  return CardService.newActionResponseBuilder()
    .setNavigation(CardService.newNavigation().updateCard(eventCard_(p.calendarId, p.eventId)))
    .setNotification(CardService.newNotification().setText(changed ? changed + '件を更新しました' : '変更はありません'))
    .build();
}

// Node でのテスト用（Apps Script では module は未定義なので何もしない）
if (typeof module !== 'undefined') {
  module.exports = { normalizeCode: normalizeCode, formatCode: formatCode, hashCode: hashCode, generateCode: generateCode,
                     eventInfo_: eventInfo_, inviteBody_: inviteBody_, sync_: sync_, ALPHABET: ALPHABET, HEADER: HEADER };
}
