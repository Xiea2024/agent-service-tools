import type { ChatMessage, StreamEvent, Thread } from './api';

const STORAGE = 'supportpilot.static-demo.v1';
let authenticated = false;
interface Recording { title: string; messages: ChatMessage[]; updated: string; scenario?: number; feedback?: Record<string, boolean> }
let recordings: Record<string, Recording> = {};
let restored = false;
const scenarios = [
  {
    question: '接口返回 500，应该提供哪些信息来定位问题？',
    title: '接口 500 排查手册（演示资料）', slug: 'api-errors',
    excerpt: '记录请求方法、路径、时间、request_id、预期与实际状态码；使用脱敏最小请求复现。关联同一 request_id 的网关和应用日志，区分上游超时、业务异常与依赖故障。',
    answer: '先保留现场，再按同一条请求串联证据。\n\n1. **记录请求**：方法、路径、发生时间、状态码和 `request_id`。\n2. **提供最小复现**：脱敏请求参数，以及预期与实际结果。\n3. **关联日志**：用 `request_id` 对照 NGINX 和应用日志，查看异常栈。\n4. **核对近期变化**：部署版本、配置、数据库迁移与外部依赖。\n\n目前只有“返回 500”，不足以确定根因。请先补充失败接口和一段脱敏异常栈。',
    followup: '**研发交接报告 · 预录示例**\n\n- **现象**：接口返回 500，影响范围待确认。\n- **复现步骤**：使用脱敏的最小请求访问失败接口。\n- **证据**：请求时间、request_id、应用异常栈、近期发布记录。\n- **候选原因**：业务异常、依赖不可用或配置变化，尚未验证。\n- **下一步**：先关联同一请求的日志，再逐项验证依赖。',
  },
  {
    question: '应用升级依赖后启动失败，帮我梳理排查步骤。',
    title: '依赖升级与启动检查（演示资料）', slug: 'startup',
    excerpt: '保留首个异常与依赖锁文件，确认解释器和启动目录一致。在独立环境按锁文件安装，检查缺失配置项名称；对比最后正常版本，避免直接覆盖生产依赖或删除运行数据。',
    answer: '先找到**第一个异常**，避免只分析后续连锁报错。\n\n1. 保存完整启动命令、解释器版本和首个异常栈。\n2. 核对当前工作目录、虚拟环境与依赖锁文件。\n3. 在独立环境按锁文件重新安装，确认是否稳定复现。\n4. 检查缺失配置项的**名称**，不要粘贴密钥值。\n5. 对比最后正常提交，缩小到具体依赖或配置变更。\n\n这份预录案例还没有你的异常栈，因此不能确认是版本不兼容还是配置缺失。',
    followup: '**研发交接报告 · 预录示例**\n\n- **现象**：依赖升级后启动失败。\n- **环境**：解释器版本、系统和启动目录待补充。\n- **复现**：在独立环境使用相同锁文件安装后启动。\n- **待核对**：首个异常、变更的依赖、缺失配置项名称。\n- **建议**：对比最后正常版本；必要时走可恢复的版本回滚流程。',
  },
  {
    question: '请查知识库里与跨域请求失败有关的文档，给出排查建议和出处。',
    title: '跨域与 Cookie 排查（演示资料）', slug: 'cors',
    excerpt: 'Origin 包含协议、域名与端口。携带 Cookie 的跨域请求应显式允许可信 Origin，不能使用通配符；核对 OPTIONS 预检、允许的方法和请求头，以及 Cookie 的 Secure 与 SameSite 属性。',
    answer: '预录知识库命中了一条跨域排查资料。建议按浏览器实际请求核对：\n\n1. **确认 Origin**：协议、域名和端口都要匹配。\n2. **检查 OPTIONS**：预检是否成功，允许的方法和请求头是否覆盖实际请求。\n3. **核对 Cookie**：检查 `Secure`、`SameSite` 和浏览器是否实际发送 Cookie。\n4. **携带凭据时**：配置明确的可信来源，不能用 `Access-Control-Allow-Origin: *`。\n\n目前没有真实网络日志，不能把跨域失败直接归因于后端。可以继续核对浏览器 Network 面板中的预检状态码。',
    followup: '**研发交接报告 · 预录示例**\n\n- **现象**：浏览器提示跨域请求失败。\n- **需补证据**：实际 Origin、OPTIONS 状态码、响应头、Cookie 是否发送。\n- **候选原因**：来源未匹配、预检被拒绝或 Cookie 策略不符合预期。\n- **验证顺序**：先检查预检，再检查实际请求与凭据；不直接放开所有来源。',
  },
];

function source(index: number) { return new URL(`${import.meta.env.BASE_URL}demo-docs/${scenarios[index].slug}.html`, window.location.href).href; }
function save() { try { sessionStorage.setItem(STORAGE, JSON.stringify(recordings)); } catch { /* Storage is optional. */ } }
function restore() {
  if (restored) return;
  restored = true;
  try {
    const data = JSON.parse(sessionStorage.getItem(STORAGE) || '{}');
    recordings = data && typeof data === 'object' && !Array.isArray(data) && Object.values(data).every(row => row && typeof row === 'object' && 'messages' in row && Array.isArray(row.messages) && 'title' in row && typeof row.title === 'string' && 'updated' in row && typeof row.updated === 'string') ? data : {};
  } catch { recordings = {}; }
  if (!Object.keys(recordings).length) resetDemo();
}
function requireGuest() { if (!isDemoLoggedIn()) throw new Error('请使用 guest / guest 进入静态演示。'); restore(); }
export function isDemoLoggedIn() {
  try { return authenticated || sessionStorage.getItem(`${STORAGE}.guest`) === 'true'; } catch { return authenticated; }
}
export function demoLogin(username: string, password: string) {
  if (username !== 'guest' || password !== 'guest') throw new Error('演示账号和密码均为 guest。');
  authenticated = true;
  try { sessionStorage.setItem(`${STORAGE}.guest`, 'true'); } catch { /* In-memory session works. */ }
  restore();
}
export function demoLogout() {
  authenticated = false;
  try { sessionStorage.removeItem(`${STORAGE}.guest`); } catch { /* Optional storage. */ }
}
function messages(index: number, answer: string, runId: string): ChatMessage[] {
  const item = scenarios[index];
  const call = `${runId}-search`;
  return [
    { type: 'ai', content: '', tool_calls: [{ id: call, name: 'search_feishu_documents', args: { query: item.question } }] },
    { type: 'tool', tool_call_id: call, content: JSON.stringify({ status: 'ok', message: '预录检索结果：1 条演示文档，未执行真实检索。', hits: [{
      chunk_id: `demo-chunk-${index}`, document_id: `demo-doc-${index}`, title: item.title, content: item.excerpt, source_url: source(index), score: 0.873,
    }] }) },
    { type: 'ai', content: `${answer}\n\n参考：[${item.title}](${source(index)})\n\n*静态演示：以上为预录内容，未调用模型或真实知识库。*`, run_id: runId },
  ];
}
export function resetDemo() {
  recordings = Object.fromEntries(scenarios.map((item, index) => [`demo-sample-${index}`, {
    title: item.question, updated: new Date().toISOString(), scenario: index,
    messages: [{ type: 'human' as const, content: item.question }, ...messages(index, item.answer, `demo-run-${index}`)],
  }]));
  save();
}
export async function demoRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  requireGuest();
  init.signal?.throwIfAborted();
  const body = typeof init.body === 'string' ? JSON.parse(init.body) : {};
  let result: unknown;
  if (path === '/info') result = { agents: [{ key: 'dev-issue-agent', description: '预录研发排查演示' }], models: ['预录问答 · 无模型调用'], default_model: '预录问答 · 无模型调用' };
  else if (path.startsWith('/dev-issue-agent/threads?')) result = { threads: Object.entries(recordings).map(([id, row]): Thread => ({ thread_id: id, title: row.title, updated_at: row.updated })).sort((a, b) => b.updated_at!.localeCompare(a.updated_at!)) };
  else if (path === '/dev-issue-agent/history') {
    if (!recordings[body.thread_id]) throw new Error('演示会话不存在。');
    result = { messages: recordings[body.thread_id].messages };
  } else if (/^\/runs\/[^/]+\/feedback$/.test(path)) {
    const run = path.split('/')[2];
    const record = Object.values(recordings).find(row => row.messages.some(m => m.run_id === run));
    if (!record) throw new Error('演示记录不存在。');
    (record.feedback ??= {})[run] = Boolean(body.helpful); save(); result = { status: 'ok' };
  } else throw new Error('静态演示不提供此操作。');
  return structuredClone(result) as T;
}
function pause(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    signal.throwIfAborted();
    const abort = () => { clearTimeout(timer); reject(new DOMException('演示已停止', 'AbortError')); };
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, ms);
    signal.addEventListener('abort', abort, { once: true });
  });
}
export async function demoStream(input: { message: string; thread_id: string }, signal: AbortSignal, onEvent: (event: StreamEvent) => void) {
  requireGuest(); signal.throwIfAborted();
  const previous = recordings[input.thread_id];
  const followup = /报告|交接|总结|继续/.test(input.message) && previous?.scenario != null;
  const index = followup ? previous.scenario! : /跨域|cors|cookie|预检/i.test(input.message) ? 2 : /启动|依赖|升级|配置/.test(input.message) ? 1 : /500|接口|异常|日志/.test(input.message) ? 0 : -1;
  const row = recordings[input.thread_id] ??= { title: input.message.slice(0, 60), updated: '', messages: [] };
  row.updated = new Date().toISOString(); row.messages.push({ type: 'human', content: input.message });
  if (index >= 0) row.scenario = index;
  const run = `demo-${crypto.randomUUID()}`;
  const sequence = index < 0 ? [{ type: 'ai' as const, run_id: run, content: '这是静态预录演示，暂不支持任意问题的实时推理。\n\n可以体验以下三类问题：**接口 500**、**依赖升级后启动失败**、**跨域 / Cookie**。完成一个案例后，可以输入“生成交接报告”体验预录的多轮流程。' }] : messages(index, followup ? scenarios[index].followup : scenarios[index].answer, run);
  let draft = '';
  try {
    for (const message of sequence) {
      await pause(message.type === 'tool' ? 400 : 160, signal);
      if (message.type === 'ai' && message.content) {
        for (let i = 0; i < message.content.length; i += 16) {
          await pause(24, signal);
          const part = message.content.slice(i, i + 16); draft += part;
          onEvent({ type: 'token', content: part });
        }
      }
      row.messages.push(message); save(); onEvent({ type: 'message', content: message });
      if (message.type === 'ai' && message.content) draft = '';
    }
  } catch (error) {
    if (draft) row.messages.push({ type: 'ai', content: `${draft}\n\n*模拟输出已停止。*` });
    throw error;
  } finally { save(); }
}
