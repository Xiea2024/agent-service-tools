import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import DemoWorkspace from './DemoWorkspace';
import { demoLogout, resetDemo } from './demo';

vi.mock('./demo-mode', () => ({ STATIC_DEMO: true }));
const noNetwork = vi.fn(() => { throw new Error('Static demo must not call fetch'); });
beforeEach(() => {
  sessionStorage.clear(); demoLogout(); resetDemo();
  vi.stubGlobal('fetch', noNetwork);
  vi.stubGlobal('matchMedia', () => ({ matches: true }));
  Element.prototype.scrollTo = vi.fn();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.clearAllMocks(); });

it('完整静态流程无需网络：登录、历史证据、新问答、反馈和退出', async () => {
  render(<DemoWorkspace />);
  fireEvent.click(screen.getByRole('button', { name: '进入演示' }));
  await screen.findByText('演示已就绪');
  fireEvent.click(within(screen.getByRole('navigation', { name: '历史会话' })).getByRole('button', { name: /请查知识库里与跨域请求失败/ }));
  await screen.findByText('跨域与 Cookie 排查（演示资料）', { selector: 'h3' });
  expect(screen.getByRole('link', { name: '查看原文' }).getAttribute('href')).toContain('demo-docs/cors.html');
  fireEvent.click(screen.getByRole('button', { name: /发起新的排查/ }));
  fireEvent.change(screen.getByLabelText('描述你的问题'), { target: { value: '接口返回500' } });
  fireEvent.click(screen.getByLabelText('发送问题'));
  await screen.findByRole('button', { name: '有帮助' }, { timeout: 8000 });
  fireEvent.click(screen.getByRole('button', { name: '有帮助' }));
  await screen.findByText('评价已保存');
  expect(noNetwork).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '退出 guest' }));
  await screen.findByRole('button', { name: '进入演示' });
  await waitFor(() => expect(screen.queryByLabelText('描述你的问题')).toBeNull());
}, 12000);
