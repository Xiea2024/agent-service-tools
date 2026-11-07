import { describe, expect, it } from 'vitest';
import { evidenceFor, historyToTurns, parseToolResult, safeUrl, toolsFor } from './conversation';

const hit = { chunk_id: 'chunk-1', document_id: 'doc-1', title: '跨域排查',
  source_url: 'https://example.com/docs/cors', content: '核对允许的 Origin。', score: 0.82 };

describe('历史与证据', () => {
  it('按用户轮次恢复工具结果，证据不会串到其他轮次', () => {
    const turns = historyToTurns([
      { type: 'human', content: '查文档' },
      { type: 'ai', content: '', tool_calls: [{ id: 'call-1', name: 'search_feishu_documents', args: { query: '跨域' } }] },
      { type: 'tool', content: JSON.stringify({ status: 'ok', hits: [hit, hit] }), tool_call_id: 'call-1' },
      { type: 'ai', content: '根据文档核对 Origin。' },
      { type: 'human', content: '你好' }, { type: 'ai', content: '你好。' },
    ]);
    expect(turns).toHaveLength(2);
    expect(toolsFor(turns[0])[0].call?.args.query).toBe('跨域');
    expect(evidenceFor(turns[0])).toEqual([hit]);
    expect(evidenceFor(turns[1])).toEqual([]);
  });

  it('无结果与服务失败分别保留，损坏的结果不制造证据', () => {
    expect(parseToolResult('{"status":"no_results","hits":[]}').status).toBe('no_results');
    expect(parseToolResult('{"status":"error","message":"检索超时","hits":[]}').message).toBe('检索超时');
    expect(parseToolResult('工具执行失败').status).toBe('error');
    expect(parseToolResult('{"status":"ok","hits":[{}]}').hits).toEqual([]);
  });

  it('拒绝工具输出中的脚本与本地资源链接', () => {
    expect(safeUrl('javascript:alert(1)')).toBeUndefined();
    expect(safeUrl('file:///etc/passwd')).toBeUndefined();
    expect(safeUrl(hit.source_url)).toBe(hit.source_url);
  });
});
