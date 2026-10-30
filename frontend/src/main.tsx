import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { api, streamChat } from './services/api';
import { replyPersistence } from './services/stream.ts';
import { Markdown } from './components/Markdown';
import { MessageList } from './components/MessageList';
import { MemorySettings } from './components/MemorySettings';
import type { Conversation, Message, ModelConfig, Persona, Memory, GenerationAttempt } from './types';
import './style.css';

const time = (date: string) => new Date(date).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
const memoryTypes: Record<string, string> = { user_statement: '用户明确陈述', user_confirmed: '用户确认', model_inference: '模型推测', outdated: '已失效信息' };

function App() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [active, setActive] = useState('');
  const [messages, setMessages] = useState<Message[]>([]);
  const [configs, setConfigs] = useState<ModelConfig[]>([]);
  const [config, setConfig] = useState('mock');
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [preview, setPreview] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [panel, setPanel] = useState<'persona' | 'memory' | 'settings' | null>(null);
  const [persona, setPersona] = useState<Persona | null>(null);
  const [memories, setMemories] = useState<Memory[]>([]);
  const [query, setQuery] = useState('');
  const [memoryText, setMemoryText] = useState('');
  const [memoryType, setMemoryType] = useState('user_statement');
  const [source, setSource] = useState('');
  const [editingMemory,setEditingMemory]=useState<Memory | null>(null);
  const [sidebar, setSidebar] = useState(false);
  const end = useRef<HTMLDivElement>(null);
  const view = useRef('');
  const sendLock = useRef(false);
  const abort = useRef<AbortController | null>(null);
  const [attempts, setAttempts] = useState<GenerationAttempt[]>([]);

  async function refresh() { setConversations(await api<Conversation[]>('/conversations')); }
  async function load(id: string) {
    view.current = id; setActive(id); setSidebar(false); setLoading(true); setMessages([]); setAttempts([]); setError(''); setNotice('');
    try { const [rows, states] = await Promise.all([api<Message[]>(`/conversations/${id}/messages`), api<GenerationAttempt[]>(`/conversations/${id}/attempts`)]); if (view.current === id) { setMessages(rows); setAttempts(states); } }
    catch (e) { if (view.current === id) setError(String(e)); }
    finally { if (view.current === id) setLoading(false); }
  }
  useEffect(() => { void Promise.all([api<ModelConfig[]>('/configs'), api<Conversation[]>('/conversations')]).then(([models, rows]) => {
    setConfigs(models); setConversations(rows); if (rows[0]) void load(rows[0].id);
  }).catch(e => setError(String(e))); }, []);
  useEffect(() => { end.current?.scrollIntoView({ behavior: 'instant' }); }, [messages, preview]);

  async function newConversation() {
    if (busy) return;
    try { const c = await api<Conversation>('/conversations', { method: 'POST', body: JSON.stringify({}) }); await refresh(); await load(c.id); }
    catch (e) { setError(String(e)); }
  }
  async function send(event: React.FormEvent) {
    event.preventDefault(); if (sendLock.current || busy || loading || !input.trim()) return;
    sendLock.current = true; abort.current = new AbortController();
    const content = input; setBusy(true); setError(''); setNotice(''); setPreview('');
    let id = active, userId: string | null = null, outcome: 'done' | 'error' | 'unknown' = 'unknown';
    try {
      if (!id) { const c = await api<Conversation>('/conversations', { method: 'POST', body: '{}' }); id = c.id; setActive(id); view.current = id; }
      outcome = await streamChat(id, content, config, ({ event, data }) => {
        if (event === 'user_saved') { userId = String(data.id); setInput(''); setMessages(old => [...old, data as unknown as Message]); }
        if (event === 'delta') setPreview(old => old + String(data.content));
        if (event === 'context') setNotice('本次使用最近对话窗口，部分较早历史或记忆未进入模型上下文；全部原文仍保存在本机。');
        if (event === 'done') { setMessages(old => [...old, data as unknown as Message]); setPreview(''); }
        if (event === 'error') { setError(`${data.message} [${data.code}]`); setPreview(''); if (data.attempt_status_persisted === false) setNotice('生成状态写入未确认；请恢复数据库连接后重新打开会话核对。'); }
      }, crypto.randomUUID(), abort.current.signal);
    } catch (e) { setError(String(e)); setPreview(''); setNotice('请求中断，正在核对历史中的保存状态…'); }
    finally {
      if (id) {
        try {
          const rows = await api<Message[]>(`/conversations/${id}/messages`); setMessages(rows);
          setAttempts(await api<GenerationAttempt[]>(`/conversations/${id}/attempts`));
          if (userId) setInput('');
          if (outcome === 'unknown') {
            const persisted = replyPersistence(rows, userId);
            if (persisted === 'confirmed') { setError(''); setNotice('连接中断，但已从历史确认完整回复已保存。'); }
            else if (persisted === 'missing') setNotice('用户消息已保存；历史中尚未发现完整回复。临时预览已清除，请稍后刷新再决定是否重发。');
            else setNotice('发送结果未确认，草稿已保留。请核对历史，避免重复发送。');
          }
        } catch {
          setNotice(outcome === 'done' ? '完整回复已确认保存，但历史刷新失败，请恢复连接后重新打开会话。' : outcome === 'error' ? '用户消息已保存，临时回复未保存；历史刷新失败，请恢复连接后重查。' : '无法读取历史，保存状态仍未知。临时预览已清除，请恢复连接后核对，避免重复发送。');
        }
        try { await refresh(); } catch { /* History reconciliation status remains authoritative. */ }
      }
      setPreview('');
      setBusy(false);
      sendLock.current = false; abort.current = null;
    }
  }
  async function stopGeneration() {
    if (!active || !busy) return;
    try { await api(`/conversations/${active}/cancel`, { method: 'POST' }); }
    catch { abort.current?.abort(); }
  }
  async function showPanel(kind: 'persona' | 'memory' | 'settings') {
    setPanel(kind);
    try { if (kind === 'persona') setPersona(await api<Persona>('/persona')); else if (kind === 'memory') setMemories(await api<Memory[]>('/memories?include_archived=true')); }
    catch (e) { setError(String(e)); }
  }
  async function searchMemory() {
    try { setMemories(await api<Memory[]>('/memories?include_archived=true&q=' + encodeURIComponent(query))); } catch (e) { setError(String(e)); }
  }
  async function saveMemory(event: React.FormEvent) {
    event.preventDefault();
    try { await api(editingMemory ? `/memories/${editingMemory.id}` : '/memories', { method: editingMemory ? 'PATCH' : 'POST', body: JSON.stringify(editingMemory ? {content:memoryText,source_message_ids:[source],expected_version:editingMemory.version} : { memory_type: memoryType, content: memoryText, source_message_ids: [source], confidence: memoryType === 'model_inference' ? 0.5 : 1 }) }); setEditingMemory(null); setMemoryText(''); await searchMemory(); }
    catch (e) { setError(String(e)); }
  }
  async function archiveMemory(id: string) {
    try { await api(`/memories/${id}/archive`, { method: 'POST' }); await searchMemory(); } catch (e) { setError(String(e)); }
  }
  async function suppressMemory(id: string) {
    try { const result=await api<{physically_deleted:boolean}>(`/memory-runtime/suppress/semantic/${id}`,{method:'POST'}); setNotice(result.physically_deleted ? '已删除' : '已停止使用及来源处理；原文、审计和备份仍保留。'); await searchMemory(); } catch(e) { setError(String(e)); }
  }
  const selected = conversations.find(c => c.id === active);
  const currentModel = configs.find(c => c.id === config);
  return <div className="app">
    {sidebar && <button className="scrim" aria-label="关闭导航" onClick={() => setSidebar(false)}/>}
    <aside className={sidebar ? 'sidebar open' : 'sidebar'}>
      <div className="brand"><span className="brand-mark">c<span>·</span></span><div>Continuum<small>记忆让对话延续</small></div></div>
      <button className="new-button" onClick={() => void newConversation()} disabled={busy}>＋ 新建会话 <span>↗</span></button>
      {active && <a className="mobile-export" href={`/api/exports/conversations/${active}.md`} download>↓ 导出当前会话 Markdown</a>}
      <div className="section-label">会话记录 <span>{conversations.length}</span></div>
      <nav aria-label="历史会话">{conversations.map(c => <button key={c.id} className={'conversation ' + (c.id === active ? 'active' : '')} disabled={busy} onClick={() => void load(c.id)}><span>{c.title}</span><small>{time(c.updated_at)}</small></button>)}{!conversations.length && <p className="muted small">第一段对话，从这里开始。</p>}</nav>
      <div className="sidebar-bottom"><button onClick={() => void showPanel('memory')}>◇ 长期记忆</button><button onClick={() => void showPanel('persona')}>☷ 交流原则</button><button onClick={() => void showPanel('settings')}>⚙ 后台记忆设置</button><a href="/api/exports/conversations.jsonl" download>↓ 导出全部对话</a><div className="local"><i/> 数据保存在本机 <span>v0.2 P1</span></div></div>
    </aside>
    {panel && error && <div className="modal-error error" role="alert">{error}<button aria-label="关闭错误提示" onClick={() => setError('')}>×</button></div>}
    <main>
      <header><div className="header-title"><button className="mobile-menu" aria-label="打开导航" onClick={() => setSidebar(true)}>☰</button><div><h1>{selected?.title || '新的对话'}</h1><span>个人空间 / 对话</span></div></div><div className="header-actions"><select aria-label="模型配置" disabled={busy} value={config} onChange={e => setConfig(e.target.value)}>{configs.map(c => <option key={c.id} value={c.id}>{c.provider === 'mock' ? '离线模拟' : c.provider} · {c.model}</option>)}</select>{active && <a title="导出当前会话为 Markdown" href={`/api/exports/conversations/${active}.md`} download>导出 ↗</a>}</div></header>
      <section className="chat" aria-label="聊天消息">
        {!messages.length && !loading && <div className="welcome"><div className="eyebrow">YOUR PERSONAL CONTINUITY</div><h2>从此刻，接着聊。</h2><p>想法、经历，或只是今天发生的小事。<br/>在自己的空间里，让对话慢慢积累。</p><div className="welcome-note"><span>◇</span><div>保留每一段原始对话<small>记忆有来源，模型可更换，数据由你掌握。</small></div></div></div>}
        {loading && <p className="muted">正在读取本地记录…</p>}
        <MessageList messages={messages} preview={preview} busy={busy} attempts={attempts} endRef={end} onRemember={id => { setSource(id); void showPanel('memory'); }}/>
      </section>
      {notice && <div className="persistence-notice" role="status">{notice}</div>}
      <div className="composer-area">{error && <div className="error" role="alert">{error}<button aria-label="关闭错误" onClick={() => setError('')}>×</button></div>}<form className="composer" onSubmit={e => void send(e)}><textarea aria-label="消息" placeholder="写下你想说的…" value={input} maxLength={100000} disabled={busy || loading} onChange={e => setInput(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); if (!busy) e.currentTarget.form?.requestSubmit(); } }}/><div className="composer-bottom"><span>{busy ? '正在生成，完整回复将在结束后保存…' : 'Enter 发送 · Shift + Enter 换行'}</span>{busy && <button type="button" onClick={() => void stopGeneration()}>停止</button>}<button className="send-button" aria-label="发送消息" disabled={busy || loading || !input.trim()} type="submit">{busy ? '…' : '↑'}</button></div></form><p className="footnote">{currentModel?.provider === 'mock' ? '当前为离线 Mock，不调用真实模型。' : '对话上下文仅发送至当前选定的模型服务。'} 原始记录始终保存在本机。</p></div>
    </main>
    {panel && <div className="modal-backdrop" onClick={() => setPanel(null)}><section className="modal" role="dialog" aria-modal="true" aria-label={panel === 'persona' ? '交流原则' : panel === 'settings' ? '后台记忆设置' : '长期记忆'} onClick={e => e.stopPropagation()}><div className="modal-heading"><h2>{panel === 'persona' ? '交流原则' : panel === 'settings' ? '后台记忆设置' : '长期记忆'}</h2><button aria-label="关闭面板" onClick={() => setPanel(null)}>×</button></div>{panel === 'settings' ? <MemorySettings messages={messages}/> : panel === 'persona' ? <><p className="muted small">本地文件管理。模型无法修改基础原则。</p><div className="prose"><Markdown>{persona?.core || '读取中…'}</Markdown></div><h3>用户偏好</h3><pre>{JSON.stringify(persona?.preferences, null, 2)}</pre><h3>历史互动样例</h3><div className="prose"><Markdown>{persona?.examples || ''}</Markdown></div></> : <><p className="muted small">人工整理，有原始来源。推测与失效信息不会作为事实注入上下文。</p><div className="search"><input aria-label="搜索记忆" placeholder="搜索记忆…" value={query} onChange={e => setQuery(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') void searchMemory(); }}/><button onClick={() => void searchMemory()}>搜索</button><a href="/api/exports/memories.json" download>导出 JSON</a></div><form className="memory-form" onSubmit={e => void saveMemory(e)}><h3>{editingMemory ? '纠正记忆（须选择新的用户原文来源）' : '添加记忆'}</h3>{editingMemory && <button type="button" onClick={()=>{setEditingMemory(null);setMemoryText('');}}>取消纠正</button>}<textarea aria-label="记忆内容" required placeholder="需要长期保留的信息" value={memoryText} onChange={e => setMemoryText(e.target.value)}/><label>信息类型<select value={memoryType} onChange={e => setMemoryType(e.target.value)}>{Object.entries(memoryTypes).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label><label>原始消息来源<select aria-label="记忆来源" required value={source} onChange={e => setSource(e.target.value)}><option value="">从当前会话选择一条消息</option>{messages.map(m => <option key={m.id} value={m.id}>{m.role} · {m.content.slice(0, 45)}</option>)}</select></label><p className="small muted">“用户确认”须对应用户明确确认的原文；系统仅校验来源存在与角色，不自动判断语义。</p><button className="primary" type="submit" disabled={!memoryText.trim() || !source}>保存记忆</button></form><div className="memory-list">{memories.map(m => <article key={m.id}><div className="memory-meta">{memoryTypes[m.memory_type]} · {({ active: '有效', tentative: '待证实', superseded: '已替代', archived: '已归档' } as Record<string,string>)[m.status] || '未知'} · 置信度 {m.confidence}</div><p>{m.content}</p><details><summary>来源 ID · {m.source_message_ids.length} 条</summary>{m.source_message_ids.map(id => <code key={id}>{id}<br/></code>)}</details>{m.status !== 'archived' && <button onClick={() => void archiveMemory(m.id)}>归档</button>}<button onClick={()=>{setEditingMemory(m);setMemoryText(m.content);setSource('');}}>纠正</button><button onClick={()=>void suppressMemory(m.id)}>停止使用及处理</button></article>)}{!memories.length && <p className="muted">暂无匹配的记忆。</p>}</div></>}</section></div>}
  </div>;
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);


