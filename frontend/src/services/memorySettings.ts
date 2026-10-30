export type MemoryStatus = {
  supported: boolean; enabled: boolean; paused: boolean; provider?: string;
  cloud_authorized?: boolean; config?: Record<string, string | number>;
  quality_gate?: string;
  today?: { day_utc: string; requests: number; api_tokens: number; budget_tokens: number; unknown_requests: number; cost_upper_microusd: number };
  jobs?: Record<string, number>; segments?: Record<string, number>; last_processed_at?: string | null;
};

export function memoryStatusLabel(status: MemoryStatus): string {
  return !status.supported ? '需要隔离迁移至 v6' : !status.enabled ? '自动记忆未开启' : status.paused ? '已暂停 · 待处理任务保留' : '后台记忆已开启';
}

export function canEnableCloud(provider: string, authorize: boolean, alreadyAuthorized: boolean): boolean {
  return provider === 'mock' || authorize || alreadyAuthorized;
}

export function tokenLabel(today?: MemoryStatus['today']): string {
  if (!today) return '未知';
  return `${today.api_tokens} 个已返回 Token；${today.unknown_requests} 次用量未知，预算按预留上限占用`;
}
