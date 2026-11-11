import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { demoLogin, demoLogout, demoRequest, demoStream, resetDemo } from './demo';
import type { ChatMessage, StreamEvent } from './api';

beforeEach(() => { sessionStorage.clear(); demoLogout(); resetDemo(); vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllEnvs(); window.history.replaceState({}, '', '/'); });

it('guest 入口拒绝其他账号，退出后不能读取演示历史', async () => {
  expect(() => demoLogin('admin', 'guest')).toThrow();
  await expect(demoRequest('/info')).rejects.toThrow();
  demoLogin('guest', 'guest');
  const list = await demoRequest<{ threads: unknown[] }>('/dev-issue-agent/threads?limit=100');
  expect(list.threads).toHaveLength(3);
  demoLogout();
  await expect(demoRequest('/info')).rejects.toThrow();
});

it('预录检索、流式回答、引用和后续交接报告可以还原为历史', async () => {
  demoLogin('guest', 'guest');
  const events: StreamEvent[] = [];
  const input = { message: '请查跨域文档', thread_id: 'new-thread' };
  const work = demoStream(input, new AbortController().signal, event => events.push(event));
  await vi.runAllTimersAsync(); await work;
  expect(events.some(e => e.type === 'token')).toBe(true);
  expect(events.some(e => e.type === 'message' && e.content.type === 'tool')).toBe(true);
  const result = await demoRequest<{ messages: ChatMessage[] }>('/dev-issue-agent/history', { body: JSON.stringify({ thread_id: input.thread_id }) });
  expect(result.messages.at(-1)?.content).toContain('demo-docs/cors.html');
  expect(result.messages.at(-1)?.content).toContain('未调用模型');
  const followup = demoStream({ ...input, message: '生成交接报告' }, new AbortController().signal, () => {});
  await vi.runAllTimersAsync(); await followup;
  const history = await demoRequest<{ messages: ChatMessage[] }>('/dev-issue-agent/history', { body: JSON.stringify({ thread_id: input.thread_id }) });
  expect(history.messages.at(-1)?.content).toContain('浏览器提示跨域请求失败');
});

it('停止输出会中断模拟流并保存已有片段，未知接口不走网络', async () => {
  demoLogin('guest', 'guest');
  const controller = new AbortController();
  const promise = demoStream({ message: '接口500', thread_id: 'stopped' }, controller.signal, event => {
    if (event.type === 'token') controller.abort();
  });
  const rejected = expect(promise).rejects.toMatchObject({ name: 'AbortError' });
  await vi.runAllTimersAsync(); await rejected;
  const history = await demoRequest<{ messages: ChatMessage[] }>('/dev-issue-agent/history', { body: JSON.stringify({ thread_id: 'stopped' }) });
  expect(history.messages.at(-1)?.content).toContain('模拟输出已停止');
  await expect(demoRequest('/auth/register')).rejects.toThrow('不提供此操作');
});

it('页面刷新后恢复标签页历史，仓库子路径下的引用保持可访问', async () => {
  vi.stubEnv('BASE_URL', './');
  window.history.replaceState({}, '', '/agent-service-tools/');
  demoLogin('guest', 'guest'); resetDemo();
  vi.resetModules();
  const refreshed = await import('./demo');
  expect(refreshed.isDemoLoggedIn()).toBe(true);
  const result = await refreshed.demoRequest<{ messages: ChatMessage[] }>('/dev-issue-agent/history', { body: JSON.stringify({ thread_id: 'demo-sample-2' }) });
  expect(result.messages.at(-1)?.content).toContain('/agent-service-tools/demo-docs/cors.html');
  window.history.replaceState({}, '', '/');
});
