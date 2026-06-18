import { getOpenAI } from '@/lib/openai'
import type { AIProvider, AIMessage, AIResponse, BotToolCall, ToolDefinition } from '@/types/bot'

interface CallAIParams {
  provider: AIProvider
  model: string
  systemPrompt: string
  messages: AIMessage[]
  tools: ToolDefinition[]
}

/** Вызов AI с поддержкой OpenAI и Anthropic */
export async function callAI(params: CallAIParams): Promise<AIResponse> {
  if (params.provider === 'anthropic') {
    return callAnthropic(params)
  }
  return callOpenAI(params)
}

// ─── OPENAI ─────────────────────────────────────────────────────────────────

function toOpenAITools(tools: ToolDefinition[]) {
  return tools.map(t => ({
    type: 'function' as const,
    function: {
      name: t.name,
      description: t.description,
      parameters: t.parameters,
    },
  }))
}

function toOpenAIMessages(messages: AIMessage[]): Array<Record<string, unknown>> {
  return messages.map(msg => {
    if (msg.role === 'tool') {
      return { role: 'tool', content: msg.content, tool_call_id: msg.toolCallId }
    }
    if (msg.role === 'assistant' && msg.toolCalls && msg.toolCalls.length > 0) {
      return {
        role: 'assistant',
        content: msg.content ?? null,
        tool_calls: msg.toolCalls.map(tc => ({
          id: tc.id,
          type: 'function',
          function: { name: tc.name, arguments: tc.argsJson },
        })),
      }
    }
    return { role: msg.role, content: msg.content ?? '' }
  })
}

async function callOpenAI(params: CallAIParams): Promise<AIResponse> {
  const openai = getOpenAI()

  const messages: Array<Record<string, unknown>> = [
    { role: 'system', content: params.systemPrompt },
    ...toOpenAIMessages(params.messages),
  ]

  const response = await openai.chat.completions.create({
    model: params.model || 'gpt-4o-mini',
    messages: messages as unknown as Parameters<typeof openai.chat.completions.create>[0]['messages'],
    tools: params.tools.length > 0 ? toOpenAITools(params.tools) : undefined,
    temperature: 0.7,
    max_tokens: 1000,
  })

  const choice = response.choices[0]
  const promptTokens = response.usage?.prompt_tokens || 0
  const completionTokens = response.usage?.completion_tokens || 0
  const tokensUsed = response.usage?.total_tokens || promptTokens + completionTokens

  if (choice.message.tool_calls && choice.message.tool_calls.length > 0) {
    const toolCalls: BotToolCall[] = choice.message.tool_calls.map(tc => {
      const tcAny = tc as unknown as Record<string, unknown>
      const fn = (tcAny.function || tcAny) as { name: string; arguments?: string }
      return {
        id: (tcAny.id || '') as string,
        name: fn.name,
        args: JSON.parse(fn.arguments || '{}'),
      }
    })
    return {
      text: choice.message.content || undefined,
      toolCalls,
      tokensUsed,
      promptTokens,
      completionTokens,
    }
  }

  return {
    text: choice.message.content || '',
    tokensUsed,
    promptTokens,
    completionTokens,
  }
}

// ─── ANTHROPIC ──────────────────────────────────────────────────────────────

function toAnthropicTools(tools: ToolDefinition[]) {
  return tools.map(t => ({
    name: t.name,
    description: t.description,
    input_schema: t.parameters,
  }))
}

function toAnthropicMessages(messages: AIMessage[]): Array<Record<string, unknown>> {
  return messages.map(msg => {
    if (msg.role === 'tool') {
      return {
        role: 'user',
        content: [{
          type: 'tool_result',
          tool_use_id: msg.toolCallId,
          content: msg.content,
        }],
      }
    }
    if (msg.role === 'assistant' && msg.toolCalls && msg.toolCalls.length > 0) {
      const blocks: Array<Record<string, unknown>> = []
      if (msg.content) blocks.push({ type: 'text', text: msg.content })
      for (const tc of msg.toolCalls) {
        blocks.push({
          type: 'tool_use',
          id: tc.id,
          name: tc.name,
          input: JSON.parse(tc.argsJson || '{}'),
        })
      }
      return { role: 'assistant', content: blocks }
    }
    return { role: msg.role, content: msg.content ?? '' }
  })
}

async function callAnthropic(params: CallAIParams): Promise<AIResponse> {
  // Динамический импорт чтобы не ломать если SDK не установлен
  const { default: Anthropic } = await import('@anthropic-ai/sdk')
  const client = new Anthropic()

  const messages = toAnthropicMessages(params.messages)

  const response = await client.messages.create({
    model: params.model || 'claude-sonnet-4-20250514',
    system: params.systemPrompt,
    messages: messages as unknown as Parameters<typeof client.messages.create>[0]['messages'],
    tools: params.tools.length > 0 ? toAnthropicTools(params.tools) as Parameters<typeof client.messages.create>[0]['tools'] : undefined,
    max_tokens: 1000,
    temperature: 0.7,
  })

  const promptTokens = response.usage?.input_tokens || 0
  const completionTokens = response.usage?.output_tokens || 0
  const tokensUsed = promptTokens + completionTokens

  const toolUseBlocks = response.content.filter(b => b.type === 'tool_use')
  if (toolUseBlocks.length > 0) {
    const toolCalls: BotToolCall[] = toolUseBlocks.map(b => {
      if (b.type !== 'tool_use') throw new Error('Unexpected block type')
      return {
        id: b.id,
        name: b.name,
        args: (b.input || {}) as Record<string, unknown>,
      }
    })
    const textBlock = response.content.find(b => b.type === 'text')
    return {
      text: textBlock && textBlock.type === 'text' ? textBlock.text : undefined,
      toolCalls,
      tokensUsed,
      promptTokens,
      completionTokens,
    }
  }

  const textBlock = response.content.find(b => b.type === 'text')
  return {
    text: textBlock && textBlock.type === 'text' ? textBlock.text : '',
    tokensUsed,
    promptTokens,
    completionTokens,
  }
}
