export interface ChatEvent { event: string; data: Record<string, unknown> }

export function usageLabel(value: unknown): string {
  const usage = value as Record<string, unknown> | null;
  if (!usage || usage.source !== 'api' || !['prompt_tokens', 'completion_tokens', 'total_tokens'].every(k => Number.isInteger(usage[k]) && Number(usage[k]) >= 0)) return 'Token 用量未知';
  return `Token · 输入 ${usage.prompt_tokens} / 输出 ${usage.completion_tokens} / 合计 ${usage.total_tokens}（API）`;
}

export function generationLabel(status: string): string {
  return ({ running: '生成中', completed: '完成', interrupted: '中断 · 临时回复未保存', cancelled: '已停止 · 临时回复未保存', truncated: '截断 · 临时回复未保存', failed: '失败 · 临时回复未保存' } as Record<string, string>)[status] || '状态未知';
}
type SavedMessage = { role: string; metadata: Record<string, unknown> };

export function replyPersistence(rows: readonly SavedMessage[], userId: string | null): 'confirmed' | 'missing' | 'unidentified' {
  if (!userId) return 'unidentified';
  return rows.some(row => row.role === 'assistant' && row.metadata.reply_to === userId) ? 'confirmed' : 'missing';
}

/** A delta is only a preview. Only a validated done acknowledges a committed reply. */
export async function consumeChatStream(body: ReadableStream<Uint8Array>, onEvent: (event: ChatEvent) => void): Promise<'done' | 'error'> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '', userId: string | null = null, ended = false;
  const invalid = () => new Error('流式数据无效；临时预览已清除，完整回复保存状态待核对。');
  try {
    for (;;) {
      const { value, done } = await reader.read();
      ended = done;
      buffer += decoder.decode(value, { stream: !done });
      let separator: RegExpExecArray | null;
      while ((separator = /\r?\n\r?\n/.exec(buffer)) !== null) {
        const frame = buffer.slice(0, separator.index);
        buffer = buffer.slice(separator.index + separator[0].length);
        const lines = frame.split(/\r?\n/);
        const event = lines.find(line => line.startsWith('event:'))?.slice(6).trim();
        if (!event || !['user_saved', 'delta', 'done', 'error', 'context'].includes(event)) continue;
        const json = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
        let data: Record<string, unknown>;
        try { data = JSON.parse(json); } catch { throw invalid(); }
        if (!data || typeof data !== 'object' || Array.isArray(data)) throw invalid();
        if (event === 'user_saved') {
          if (userId || typeof data.id !== 'string' || data.role !== 'user') throw invalid();
          userId = data.id;
        } else if (!userId) throw invalid();
        if (event === 'delta' && typeof data.content !== 'string') throw invalid();
        if (event === 'done') {
          const metadata = data.metadata as Record<string, unknown> | undefined;
          if (typeof data.id !== 'string' || data.role !== 'assistant' || typeof data.content !== 'string' || metadata?.reply_to !== userId) throw invalid();
        }
        if (event === 'error' && (data.user_message_id !== userId || data.reply_persisted !== false)) throw invalid();
        onEvent({ event, data });
        // A later TCP failure must not undo an acknowledged commit or known error.
        if (event === 'done' || event === 'error') return event;
      }
      if (done) break;
    }
    throw new Error('连接中断，临时预览已清除；完整回复保存状态待核对。');
  } finally {
    if (!ended) await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
