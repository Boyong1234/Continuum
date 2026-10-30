import { test } from 'node:test';
import assert from 'node:assert/strict';
import { consumeChatStream, replyPersistence, usageLabel, generationLabel } from './stream.ts';

const encoder = new TextEncoder();

test('usage labels distinguish real API counts, estimates and unknown', () => {
  assert.match(usageLabel({ source: 'api', prompt_tokens: 10, completion_tokens: 2, total_tokens: 12 }), /输入 10.*合计 12（API）/);
  for (const value of [null, {}, { source:'local_estimate', total_tokens:99 }, { source:'api', prompt_tokens:true, completion_tokens:2, total_tokens:3 }]) assert.equal(usageLabel(value), 'Token 用量未知');
  assert.match(generationLabel('truncated'), /截断.*未保存/);
  assert.match(generationLabel('interrupted'), /中断.*未保存/);
});

test('context window notice is delivered without losing persistence acknowledgement', async () => {
  const events = [];
  assert.equal(await consumeChatStream(body([frame('user_saved', user), frame('context', { history_omitted:true }), frame('done',reply)]), e => events.push(e)), 'done');
  assert.deepEqual(events.map(e=>e.event), ['user_saved','context','done']);
});
const user = { id: 'u1', role: 'user', content: '你好' };
const reply = { id: 'a1', role: 'assistant', content: '完整回复', metadata: { reply_to: 'u1' } };
const frame = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
function body(parts) {
  let index = 0;
  return new ReadableStream({ pull(controller) {
    if (index === parts.length) { controller.close(); return; }
    const part = parts[index++];
    if (part instanceof Error) controller.error(part);
    else controller.enqueue(typeof part === 'string' ? encoder.encode(part) : part);
  }});
}

test('UTF-8 and CRLF split across chunks preserve the done acknowledgement', async () => {
  const text = (frame('user_saved', user) + frame('delta', { content: '你好' }) + frame('done', reply)).replaceAll('\n', '\r\n');
  const parts = [...encoder.encode(text)].map(byte => new Uint8Array([byte]));
  const events = [];
  assert.equal(await consumeChatStream(body(parts), event => events.push(event)), 'done');
  assert.deepEqual(events.map(e => e.event), ['user_saved', 'delta', 'done']);
  assert.equal(events[1].data.content, '你好');
});

test('EOF after partial output cannot become a saved reply', async () => {
  const events = [];
  await assert.rejects(consumeChatStream(body([frame('user_saved', user), frame('delta', { content: 'partial' })]), e => events.push(e)), /状态待核对/);
  assert.deepEqual(events.map(e => e.event), ['user_saved', 'delta']);
});

test('upstream error is a known unpersisted reply, not success', async () => {
  const error = { code: 'network_error', message: '中断', user_message_id: 'u1', reply_persisted: false };
  assert.equal(await consumeChatStream(body([frame('user_saved', user), frame('delta', { content: 'partial' }), frame('error', error)]), () => {}), 'error');
});

test('read failure cannot turn an unacknowledged preview into success', async () => {
  await assert.rejects(consumeChatStream(body([frame('user_saved', user), new Error('network interrupted')]), () => {}), /network interrupted/);
});

test('done is sufficient even if the transport fails afterwards', async () => {
  assert.equal(await consumeChatStream(body([frame('user_saved', user) + frame('done', reply), new Error('lost TCP close')]), () => {}), 'done');
});

test('wrong reply correlation, missing user acknowledgement and malformed data are rejected', async () => {
  for (const frames of [
    [frame('done', reply)],
    [frame('user_saved', user), frame('done', { ...reply, metadata: { reply_to: 'other' } })],
    [frame('user_saved', user), 'event: done\ndata: [invalid]\n\n'],
    [frame('user_saved', user), frame('error', { user_message_id: 'other', reply_persisted: false })],
  ]) await assert.rejects(consumeChatStream(body(frames), () => {}), /状态待核对/);
});

test('lost done is reconciled by reply_to, never by identical content', () => {
  assert.equal(replyPersistence([reply], 'u1'), 'confirmed');
  assert.equal(replyPersistence([{ ...reply, metadata: { reply_to: 'different' } }], 'u1'), 'missing');
  assert.equal(replyPersistence([reply], null), 'unidentified');
  assert.equal(replyPersistence([], 'u1'), 'missing');
});
