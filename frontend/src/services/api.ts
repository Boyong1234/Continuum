import type { StreamEvent } from '../types';
import { consumeChatStream } from './stream.ts';

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch('/api' + path, { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers } });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.message || `请求失败 (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export async function streamChat(id: string, content: string, config_id: string, onEvent: (event: StreamEvent) => void, request_id: string, signal?: AbortSignal) {
  const response = await fetch(`/api/conversations/${id}/chat`, { method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content, config_id, request_id }) });
  if (!response.ok || !response.body) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.message || `请求失败 (${response.status})`);
  }
  return consumeChatStream(response.body, onEvent);
}
