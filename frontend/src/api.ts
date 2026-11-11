/** 对齐 FastAPI 的 ChatMessage / SSE 协议；网络分包不等于消息边界。 */
import { STATIC_DEMO } from './demo-mode';
import { demoRequest, demoStream } from './demo';
export const AGENT = 'dev-issue-agent';
let selectedTenant = '';
export function selectTenant(id: string) { selectedTenant = id; }
function tenantHeaders(): Record<string, string> {
  return selectedTenant ? { 'X-Tenant-ID': selectedTenant } : {};
}

export interface ToolCall {
  id: string | null;
  name: string;
  args: Record<string, unknown>;
}

export interface ChatMessage {
  type: 'human' | 'ai' | 'tool' | 'custom';
  content: string;
  tool_calls?: ToolCall[];
  tool_call_id?: string | null;
  run_id?: string | null;
}

export interface Thread {
  thread_id: string;
  title: string | null;
  updated_at: string | null;
}

export interface Metadata {
  agents: { key: string; description: string }[];
  models: string[];
  default_model: string;
}

export type StreamEvent =
  | { type: 'token'; content: string }
  | { type: 'message'; content: ChatMessage }
  | { type: 'error'; content: string };

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  if (STATIC_DEMO) return demoRequest<T>(path, init);
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...tenantHeaders(), ...init.headers },
    signal: init.signal ?? AbortSignal.timeout(12_000),
  });
  await checkResponse(response);
  return response.json() as Promise<T>;
}

async function checkResponse(response: Response) {
  if (response.ok) return;
  if (response.status === 401) {
    window.dispatchEvent(new Event('supportpilot:unauthorized'));
    throw new Error('身份认证失败，请重新登录；本地模式请检查 AUTH_SECRET 配置。');
  }
  if (response.status === 403) throw new Error('你没有当前团队或操作的权限。');
  if (response.status === 429) throw new Error('操作过于频繁、任务正在执行或今日额度已用完，请稍后重试。');
  if (response.status === 503) throw new Error('服务暂不可用，请稍后重试。');
  if (response.status === 409) throw new Error('操作发生冲突：会话可能仍在执行，或该回答已经评价。');
  if (response.status === 502 || response.status === 504) throw new Error('暂时无法连接后端，请确认 FastAPI 已启动。');
  throw new Error(`请求失败（HTTP ${response.status}），请检查后端日志。`);
}

/** 支持 CRLF、注释、多行 data、UTF-8 跨包，并要求后端明确发送 DONE。 */
export async function readSSE(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: StreamEvent) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let data: string[] = [];
  let done = false;
  function dispatch() {
    if (!data.length) return;
    const payload = data.join('\n');
    data = [];
    if (payload === '[DONE]') { done = true; return; }
    const event: unknown = JSON.parse(payload);
    if (!event || typeof event !== 'object' || !('type' in event) || !('content' in event)) {
      throw new Error('收到无法识别的流式消息。');
    }
    const item = event as StreamEvent;
    if ((item.type === 'token' || item.type === 'error') && typeof item.content === 'string') onEvent(item);
    else if (item.type === 'message' && item.content && typeof item.content.content === 'string'
      && ['human', 'ai', 'tool', 'custom'].includes(item.content.type)) onEvent(item);
    else throw new Error('收到无法识别的流式消息。');
  }
  try {
    while (!done) {
      const result = await reader.read();
      buffer += decoder.decode(result.value, { stream: !result.done });
      let end: number;
      while ((end = buffer.indexOf('\n')) !== -1 && !done) {
        const line = buffer.slice(0, end).replace(/\r$/, '');
        buffer = buffer.slice(end + 1);
        if (!line) dispatch();
        else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
      }
      if (result.done) {
        if (buffer.startsWith('data:')) data.push(buffer.slice(5).replace(/^ /, '').replace(/\r$/, ''));
        dispatch();
        break;
      }
    }
    if (!done) throw new Error('连接提前结束，回答可能不完整。请刷新历史确认结果后再继续。');
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

export async function streamAnswer(
  input: { message: string; thread_id: string; user_id: string; model?: string },
  signal: AbortSignal,
  onEvent: (event: StreamEvent) => void,
) {
  if (STATIC_DEMO) return demoStream(input, signal, onEvent);
  const response = await fetch(`/api/${AGENT}/stream`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...tenantHeaders() },
    body: JSON.stringify({ ...input, stream_tokens: true }), signal,
  });
  await checkResponse(response);
  if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) {
    throw new Error('后端没有返回预期的消息流，请检查代理配置。');
  }
  await readSSE(response.body, onEvent);
}

export const listThreads = (user: string) => request<{ threads: Thread[] }>(
  `/${AGENT}/threads?user_id=${encodeURIComponent(user)}&limit=100`,
);
export const getHistory = (thread: string, signal?: AbortSignal) => request<{ messages: ChatMessage[] }>(
  `/${AGENT}/history`, { method: 'POST', body: JSON.stringify({ thread_id: thread }), signal },
);
