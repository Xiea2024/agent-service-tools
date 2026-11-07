import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { StreamEvent } from './api';
import App from './App';

const api = vi.hoisted(() => ({ request: vi.fn(), listThreads: vi.fn(), getHistory: vi.fn(), streamAnswer: vi.fn() }));
vi.mock('./api', () => ({ AGENT: 'dev-issue-agent', ...api }));

beforeEach(() => {
  localStorage.clear();
  vi.stubGlobal('matchMedia', () => ({ matches: true }));
  Element.prototype.scrollTo = vi.fn();
  api.request.mockResolvedValue({ agents: [{ key: 'dev-issue-agent' }], models: ['test-model'], default_model: 'test-model' });
  api.listThreads.mockResolvedValue({ threads: [{ thread_id: 'existing-thread', title: '历史接口异常', updated_at: null }] });
  api.getHistory.mockResolvedValue({ messages: [
    { type: 'human', content: '历史问题' },
    { type: 'ai', content: '', tool_calls: [{ id: 'old-call', name: 'search_feishu_documents', args: { query: '历史检索' } }] },
    { type: 'tool', tool_call_id: 'old-call', content: JSON.stringify({ status: 'ok', hits: [{
      chunk_id: 'old-chunk', title: '历史排查文档', content: '历史证据正文', source_url: 'https://example.com/history', document_id: 'old-doc',
    }] }) },
    { type: 'ai', content: '历史回答' },
  ] });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

async function openApp() {
  render(<App />);
  await screen.findByText('服务已连接');
}

describe('工作台完整交互', () => {
  it('显示流式文字，最终消息不重复，真实工具证据可打开', async () => {
    let emit: (event: StreamEvent) => void = () => {};
    let complete: () => void = () => {};
    api.streamAnswer.mockImplementation((_input, _signal, onEvent) => {
      emit = onEvent;
      return new Promise<void>(resolve => { complete = resolve; });
    });
    await openApp();
    fireEvent.change(screen.getByLabelText('描述你的问题'), { target: { value: '请查跨域文档' } });
    fireEvent.click(screen.getByLabelText('发送问题'));
    await waitFor(() => expect(api.streamAnswer).toHaveBeenCalledTimes(1));
    const { act } = await import('@testing-library/react');
    await act(async () => { emit({ type: 'token', content: '正在分析跨域' }); });
    expect(screen.getByText('正在分析跨域')).toBeTruthy();
    await act(async () => {
      emit({ type: 'message', content: { type: 'ai', content: '', tool_calls: [{ name: 'search_feishu_documents', id: 'call-1', args: { query: '跨域' } }] } });
      emit({ type: 'message', content: { type: 'tool', tool_call_id: 'call-1', content: JSON.stringify({ status: 'ok', message: '返回相关片段', hits: [{
        chunk_id: 'chunk-1', document_id: 'doc-1', title: '跨域配置指南', content: '检查服务端 Origin 配置。', source_url: 'https://example.com/cors', score: 0.8,
      }] }) } });
      emit({ type: 'token', content: '建议核对 Origin。' });
      emit({ type: 'message', content: { type: 'ai', content: '建议核对 Origin。', run_id: 'run-123' } });
      complete();
    });
    expect(screen.queryByText('正在分析跨域')).toBeNull();
    expect(screen.getAllByText('建议核对 Origin。')).toHaveLength(1);
    expect(screen.getByText('跨域配置指南')).toBeTruthy();
    expect(screen.getByRole('link', { name: '查看原文' }).getAttribute('href')).toBe('https://example.com/cors');
    expect(api.streamAnswer.mock.calls[0][0]).toMatchObject({ message: '请查跨域文档', model: 'test-model' });
  });

  it('从后端恢复历史和证据，继续发送时复用原线程', async () => {
    api.streamAnswer.mockResolvedValue(undefined);
    await openApp();
    fireEvent.click(screen.getByText('历史接口异常'));
    await screen.findByText('历史回答');
    expect(screen.getByText('历史证据正文')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('描述你的问题'), { target: { value: '继续排查' } });
    fireEvent.click(screen.getByLabelText('发送问题'));
    await waitFor(() => expect(api.streamAnswer.mock.calls[0][0].thread_id).toBe('existing-thread'));
  });

  it('断流错误可见，保留部分回答且不会自动重发', async () => {
    api.streamAnswer.mockImplementation(async (_input, _signal, emit) => {
      emit({ type: 'token', content: '部分结果' }); throw new Error('连接提前结束');
    });
    await openApp();
    fireEvent.change(screen.getByLabelText('描述你的问题'), { target: { value: '接口异常' } });
    fireEvent.click(screen.getByLabelText('发送问题'));
    await screen.findByText('连接提前结束');
    expect(screen.getByText('部分结果')).toBeTruthy();
    expect(api.streamAnswer).toHaveBeenCalledTimes(1);
  });

  it('认证失败时显示原因并阻止发送', async () => {
    api.request.mockRejectedValue(new Error('后端认证失败'));
    render(<App />);
    await screen.findByText('后端认证失败');
    expect(screen.getByLabelText('发送问题').hasAttribute('disabled')).toBe(true);
  });
});
