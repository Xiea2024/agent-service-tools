import { describe, expect, it } from 'vitest';
import { readSSE, type StreamEvent } from './api';

function byteStream(text: string, size = 7) {
  const bytes = new TextEncoder().encode(text);
  return new ReadableStream<Uint8Array>({ start(controller) {
    for (let offset = 0; offset < bytes.length; offset += size) controller.enqueue(bytes.slice(offset, offset + size));
    controller.close();
  } });
}

describe('FastAPI SSE 协议', () => {
  it('中文 UTF-8 任意字节分包、CRLF 和多行 data 都能正确组装', async () => {
    const events: StreamEvent[] = [];
    await readSSE(byteStream(': keepalive\r\ndata: {"type":"token",\r\ndata: "content":"跨域异常"}\r\n\r\n'
      + 'data: {"type":"message","content":{"type":"ai","content":"完整回答"}}\r\n\r\n'
      + 'data: [DONE]\r\n\r\n', 1), event => events.push(event));
    expect(events).toEqual([
      { type: 'token', content: '跨域异常' },
      { type: 'message', content: { type: 'ai', content: '完整回答' } },
    ]);
  });

  it('连接中断不能误报为成功', async () => {
    await expect(readSSE(byteStream('data: {"type":"token","content":"部分回答"}\n\n'), () => {}))
      .rejects.toThrow('连接提前结束');
  });

  it('支持末尾无换行的 DONE', async () => {
    await expect(readSSE(byteStream('data: [DONE]'), () => {})).resolves.toBeUndefined();
  });

  it('拒绝不符合消息协议的数据', async () => {
    await expect(readSSE(byteStream('data: {"type":"token","content":12}\n\n'), () => {}))
      .rejects.toThrow('无法识别');
  });
});
