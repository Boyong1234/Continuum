export interface Conversation { id: string; title: string; created_at: string; updated_at: string }
export interface Message { id: string; conversation_id: string; role: string; content: string; model: string | null; created_at: string; metadata: Record<string, unknown> }
export interface ModelConfig { id: string; provider: string; model: string }
export interface Persona { core: string; preferences: Record<string, unknown>; examples: string }
export interface Memory { version?: number; id: string; memory_type: string; content: string; confidence: number; status: string; source_message_ids: string[]; updated_at: string }
export interface StreamEvent { event: string; data: Record<string, unknown> }
export interface GenerationAttempt { id: string; user_message_id: string; status: string; model: string | null; metadata: Record<string, unknown> }
