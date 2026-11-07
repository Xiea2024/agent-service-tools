import type { ChatMessage, ToolCall } from './api';

export interface Turn {
  id: string;
  question: string;
  messages: ChatMessage[];
  draft: string;
  status: 'complete' | 'streaming' | 'error' | 'stopped';
  error?: string;
}

export interface Evidence {
  chunk_id: string;
  document_id: string;
  title: string;
  source_url: string;
  content: string;
  score?: number;
  truncated?: boolean;
}

export interface ToolResult {
  status: 'ok' | 'no_results' | 'error' | 'unknown';
  message: string;
  hits: Evidence[];
}

export function safeUrl(value: string): string | undefined {
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined;
  } catch { return undefined; }
}

export function parseToolResult(content: string): ToolResult {
  try {
    const data: unknown = JSON.parse(content);
    if (!data || typeof data !== 'object') throw new Error();
    const result = data as Record<string, unknown>;
    const hits = Array.isArray(result.hits) ? result.hits.filter((hit): hit is Evidence =>
      hit && typeof hit === 'object' && typeof hit.chunk_id === 'string'
      && typeof hit.title === 'string' && typeof hit.source_url === 'string'
      && typeof hit.content === 'string',
    ) : [];
    return {
      status: ['ok', 'no_results', 'error'].includes(String(result.status))
        ? result.status as ToolResult['status'] : 'unknown',
      message: typeof result.message === 'string' ? result.message : '工具已返回结果。', hits,
    };
  } catch { return { status: 'error', message: content || '工具没有返回可解析的结果。', hits: [] }; }
}

export function toolsFor(turn: Turn) {
  const calls = turn.messages.flatMap(message => message.tool_calls ?? []);
  const results = turn.messages.filter(message => message.type === 'tool');
  const items: { call?: ToolCall; result?: ToolResult; id: string }[] = calls.map((call, index) => {
    const match = call.id ? results.find(result => result.tool_call_id === call.id) : undefined;
    return { call, result: match ? parseToolResult(match.content) : undefined, id: call.id ?? `call-${index}` };
  });
  // 历史中即使缺少对应调用消息，也保留实际工具结果作为证据。
  results.forEach((message, index) => {
    if (!message.tool_call_id || !calls.some(call => call.id === message.tool_call_id)) {
      items.push({ result: parseToolResult(message.content), id: `result-${index}` });
    }
  });
  return items;
}

export function evidenceFor(turn: Turn): Evidence[] {
  return [...new Map(toolsFor(turn).flatMap(tool => tool.result?.hits ?? [])
    .map(hit => [hit.chunk_id, hit])).values()];
}

export function historyToTurns(messages: ChatMessage[]): Turn[] {
  const turns: Turn[] = [];
  for (const message of messages) {
    if (message.type === 'human') turns.push({
      id: `history-${turns.length}`, question: message.content,
      messages: [], draft: '', status: 'complete',
    });
    else if (turns.length) turns[turns.length - 1].messages.push(message);
  }
  return turns;
}
