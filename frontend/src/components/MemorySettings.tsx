import { useEffect, useState } from 'react';
import { api } from '../services/api';
import { memoryStatusLabel, tokenLabel, canEnableCloud } from '../services/memorySettings';
import type { MemoryStatus } from '../services/memorySettings';
import type { Message } from '../types';

export function MemorySettings({messages=[]}: {messages?: Message[]}) {
  const [status,setStatus]=useState<MemoryStatus | null>(null);
  const [config,setConfig]=useState<Record<string,string|number>>({});
  const [enabled,setEnabled]=useState(false);
  const [authorize,setAuthorize]=useState(false);
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const [episodes,setEpisodes]=useState<Array<{id:string;title:string;content:string;state:string;source_message_ids:string[]}>>([]);
  const [correcting,setCorrecting]=useState('');
  const [source,setSource]=useState('');
  useEffect(()=>{
    let mounted=true;
    async function read(first=false) {
      try {const value=await api<MemoryStatus>('/memory-runtime');if(mounted){setStatus(value);if(first){setConfig(value.config || {});setEnabled(value.enabled);}}}
      catch(e){if(mounted)setError(String(e));}
    }
    void read(true);const timer=setInterval(()=>void read(),5000);
    return ()=>{mounted=false;clearInterval(timer);};
  },[]);
  async function save() {
    setBusy(true);setError('');
    try {setStatus(await api<MemoryStatus>('/memory-runtime/configure',{method:'POST',body:JSON.stringify({config,enabled,authorize_cloud:authorize})}));setAuthorize(false);}
    catch(e){setError(String(e));}finally{setBusy(false);}
  }
  async function pause() {
    setBusy(true);try{setStatus(await api<MemoryStatus>('/memory-runtime/pause',{method:'POST',body:JSON.stringify({paused:!status?.paused})}));}
    catch(e){setError(String(e));}finally{setBusy(false);}
  }
  async function readEpisodes() {try {setEpisodes(await api('/memory-runtime/episodes'));}catch(e){setError(String(e));}}
  async function episodeAction(id:string,correct=false) {
    try {
      await api(correct ? `/memory-runtime/episodes/${id}/correct` : `/memory-runtime/suppress/episodic/${id}`,{method:'POST',...(correct?{body:JSON.stringify({source_message_id:source})}:{})});
      setCorrecting('');setSource('');await readEpisodes();
    } catch(e){setError(String(e));}
  }
  if(!status)return <p>正在读取后台状态…{error}</p>;
  return <div className="memory-settings">
    <p role="status">{memoryStatusLabel(status)}</p>
    {!status.supported ? <p className="muted">当前数据集保持原样。请先按运维文档创建并验证 v6 隔离副本；此处不会自动升级。</p> : <>
      <p className="muted small">后台处理需要独立 Worker 进程。关闭页面不会停止 Worker。首次开启只处理之后的新对话。</p>
      <dl><dt>提取供应商</dt><dd>{status.provider}</dd><dt>今日请求预留（UTC 日）</dt><dd>{status.today?.requests ?? '未知'}</dd><dt>实际用量</dt><dd>{tokenLabel(status.today)}</dd><dt>预算占用 Token</dt><dd>{status.today?.budget_tokens ?? '未知'}</dd><dt>保守费用上界</dt><dd>${((status.today?.cost_upper_microusd || 0)/1000000).toFixed(4)} USD（不是账单）</dd><dt>任务状态</dt><dd>{Object.entries(status.jobs || {}).map(([key,n])=>`${key}: ${n}`).join(' · ') || '无任务'}</dd><dt>片段状态</dt><dd>{Object.entries(status.segments || {}).map(([key,n])=>`${key}: ${n}`).join(' · ') || '无片段'}</dd><dt>最近处理</dt><dd>{status.last_processed_at ? new Date(status.last_processed_at).toLocaleString() : '尚无'}</dd></dl>
      <label><input type="checkbox" checked={enabled} onChange={e=>setEnabled(e.target.checked)}/>开启自动记忆</label>
      <label>提取配置<select value={String(config.provider || 'mock')} onChange={e=>{setConfig({...config,provider:e.target.value});setAuthorize(false);}}><option value="mock">离线 Mock</option><option value="deepseek">DeepSeek</option></select></label>
      {['daily_requests','daily_tokens','daily_microusd','idle_seconds'].map(key=><label key={key}>{({daily_requests:'每日请求上限',daily_tokens:'每日 Token 上限',daily_microusd:'每日费用上限（百万分之一美元）',idle_seconds:'空闲合并窗口（秒）'} as Record<string,string>)[key]}<input type="number" min={0} value={Number(config[key] || 0)} onChange={e=>setConfig({...config,[key]:Number(e.target.value)})}/></label>)}
      {config.provider==='deepseek' && <label><input type="checkbox" checked={authorize} onChange={e=>setAuthorize(e.target.checked)}/>一次授权该提取配置处理启用后的新对话，允许额外 API 消耗；不会上传启用前全部历史。</label>}
      <div className="search"><button disabled={busy || enabled && !canEnableCloud(String(config.provider),authorize,Boolean(status.cloud_authorized && config.provider===status.provider))} onClick={()=>void save()}>保存配置</button><button disabled={busy || !status.enabled} onClick={()=>void pause()}>{status.paused?'继续处理':'暂停处理'}</button></div>
      <p className="muted small">记忆面板可纠正、归档或停止使用及处理派生信息。停止使用不是物理删除：原文、不可变审计和已有备份仍保留。Persona 不会自动修改。</p>
      <p className="muted small">本地质量门禁：{status.quality_gate || '未知'}；固定样本通过不等于全部真实对话都准确。</p>
      <button onClick={()=>void readEpisodes()}>查看近期事件记忆</button>
      {episodes.map(e=><article key={e.id}><h3>{e.title} · {e.state}</h3><p>{e.content}</p><details><summary>原始来源</summary>{e.source_message_ids.map(id=><code key={id}>{id}<br/></code>)}</details><button onClick={()=>{setCorrecting(e.id);setSource('');}}>纠正（追加修订）</button><button onClick={()=>void episodeAction(e.id)}>停止使用及处理</button>{correcting===e.id && <div><p className="small muted">选择当前会话中较新的用户纠正原文；旧事件仍保留。</p><select aria-label="事件纠正来源" value={source} onChange={event=>setSource(event.target.value)}><option value="">选择新的用户消息</option>{messages.filter(m=>m.role==='user').map(m=><option key={m.id} value={m.id}>{m.content.slice(0,60)}</option>)}</select><button disabled={!source} onClick={()=>void episodeAction(e.id,true)}>保存追加修订</button></div>}</article>)}
    </>}
    {error && <p className="error" role="alert">{error}</p>}
  </div>;
}
