import type { RefObject } from 'react';
import type { Message, GenerationAttempt } from '../types';
import { usageLabel, generationLabel } from '../services/stream.ts';
import { Markdown } from './Markdown';

const time = (date: string) => new Date(date).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });

export function MessageList({ messages, preview, busy, onRemember, endRef, attempts }: {
  messages: Message[]; preview: string; busy: boolean; onRemember: (id: string) => void; endRef: RefObject<HTMLDivElement | null>; attempts: GenerationAttempt[];
}) {
  const answered = new Set(messages.filter(m => m.role === 'assistant').map(m => m.metadata.reply_to));
  return <div className="message-list">
    {messages.map(m => <article key={m.id} className={'message ' + m.role}>
      <div className="message-heading"><strong>{m.role === 'user' ? '你' : 'Continuum'}</strong><time dateTime={m.created_at} title={new Date(m.created_at).toLocaleString()}>{time(m.created_at)}</time>{m.role === 'assistant' && <span className="model-tag">{m.model}</span>}<span className="saved-tag">已保存{m.role === 'user' && !busy && !answered.has(m.id) ? ' · 尚无完整回复' : ''}</span></div>
      <div className="prose"><Markdown>{m.content}</Markdown></div>
      {m.role === 'assistant' && <details className="muted small"><summary>生成详情</summary><span>完成 · {usageLabel(m.metadata.usage)}</span></details>}
      {m.role === 'user' && attempts.filter(a => a.user_message_id === m.id && a.status !== 'completed').map(a => <div key={a.id} className="muted small"><p>{a.status === 'running' && !busy ? '生成状态待核对' : generationLabel(a.status)}</p><details><summary>生成详情</summary><span>{usageLabel(a.metadata.usage)}</span></details></div>)}
      <button className="source-button" title={m.id} onClick={() => onRemember(m.id)}>引用为记忆来源</button>
    </article>)}
    {preview && <article className="message assistant streaming"><div className="message-heading"><strong>Continuum</strong><span className="model-tag">临时预览 · 未保存</span></div><div className="prose"><Markdown>{preview}</Markdown></div></article>}
    <div ref={endRef}/>
  </div>;
}
