// Типы для встроенного AI-бота

export interface DoctorSpecialization {
  id: number
  doctorLogin: string
  doctorName?: string
  canTreat: string[]
  preferredDiagnoses: string[]
  cannotTreat: string[]
  description: string
  createdAt?: string
  updatedAt?: string
}

export type AIProvider = 'openai' | 'anthropic'

export interface BotToolCall {
  id: string
  name: string
  args: Record<string, unknown>
}

// Сообщения для AI диалога с нативной поддержкой tool_calls
export type AIMessage =
  | { role: 'user'; content: string }
  | {
      role: 'assistant'
      content: string | null
      toolCalls?: Array<{ id: string; name: string; argsJson: string }>
    }
  | { role: 'tool'; content: string; toolCallId: string }

export type BotActionType =
  | 'started'
  | 'guard_blocked'
  | 'anamnesis_flagged'      // NEW: separate из guard_blocked для метрик
  | 'ai_call'
  | 'tool_call'
  | 'response_sent'
  | 'error'
  | 'completed'

export interface BotActionLog {
  id: number
  conversationId: number
  leadId: number | null
  actionType: BotActionType
  actionDetail: string
  aiInput: string
  aiOutput: string
  toolName: string
  toolArgs: Record<string, unknown>
  toolResult: string
  tokensUsed: number
  durationMs: number
  errorMessage: string
  createdAt: string
}

export interface AIResponse {
  text?: string
  toolCalls?: BotToolCall[]
  tokensUsed?: number
  promptTokens?: number
  completionTokens?: number
}

// Единый формат определения инструмента для AI
export interface ToolDefinition {
  name: string
  description: string
  parameters: Record<string, unknown>
}

// Контекст для выполнения инструментов бота
export interface BotContext {
  conversationId: number
  leadId: number | null
  externalId: string
  patientName: string
  phone: string
}
