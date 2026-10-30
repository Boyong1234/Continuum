import test from 'node:test';
import assert from 'node:assert/strict';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

test('actual MessageList keeps usage in closed native details and status visible', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false, watch: null }, appType: 'custom', optimizeDeps: { noDiscovery: true, include: [] } });
  try {
    const { MessageList } = await server.ssrLoadModule('/src/components/MessageList.tsx');
    const usage = { source: 'api', prompt_tokens: 17, completion_tokens: 3, total_tokens: 20 };
    const html = renderToStaticMarkup(createElement(MessageList, {
      messages: [
        { id: 'u', role: 'user', content: '合成问题', created_at: '2026-10-28T00:00:00Z', metadata: {} },
        { id: 'a', role: 'assistant', content: '合成回复', model: 'mock', created_at: '2026-10-28T00:00:01Z', metadata: { usage, reply_to: 'u' } },
      ],
      attempts: [{ id: 'attempt', user_message_id: 'u', status: 'failed', metadata: { usage } }],
      busy: false, preview: '', onRemember() {}, endRef: { current: null },
    }));
    assert.match(html, /输入 17/);
    const details = [...html.matchAll(/<details\b([^>]*)>[\s\S]*?<\/details>/g)];
    assert.equal(details.length, 2);
    assert.ok(details.every(x => !/\bopen\b/.test(x[1])));
    const visible = html.replace(/<details\b[^>]*>[\s\S]*?<\/details>/g, '');
    assert.doesNotMatch(visible, /Token|输入 17|合计 20/);
    assert.match(visible, /生成失败|失败/);
    assert.match(visible, /已保存/);
  } finally {
    await server.close();
  }
});
