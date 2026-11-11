import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import Registration from './Registration';

const api = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', () => api);
afterEach(() => { cleanup(); vi.clearAllMocks(); delete window.turnstile; });

it('人机验证通过后注册，提交验证码激活，注册请求不包含权限字段', async () => {
  window.turnstile = { render: vi.fn((_element, options) => { (options.callback as (token: string) => void)('human-test'); return 'widget'; }), remove: vi.fn() };
  api.request.mockResolvedValue({ status: '请检查邮箱' });
  const close = vi.fn();
  render(<Registration siteKey="site-test" close={close} />);
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'new@example.com' } });
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'test-password-123' } });
  await waitFor(() => expect(screen.getByRole('button', { name: '提交' }).hasAttribute('disabled')).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '提交' }));
  await screen.findByLabelText('邮件中的验证码');
  expect(JSON.parse(api.request.mock.calls[0][1].body)).toEqual({ email: 'new@example.com', password: 'test-password-123', human_token: 'human-test' });
  fireEvent.change(screen.getByLabelText('邮件中的验证码'), { target: { value: 'x'.repeat(43) } });
  fireEvent.click(screen.getByRole('button', { name: '提交' }));
  await waitFor(() => expect(api.request).toHaveBeenLastCalledWith('/auth/verify', expect.objectContaining({ method: 'POST' })));
  fireEvent.click(screen.getByRole('button', { name: '返回登录' }));
  expect(close).toHaveBeenCalledOnce();
});

it('未通过人机验证时禁止发起注册', () => {
  window.turnstile = { render: vi.fn(() => 'widget'), remove: vi.fn() };
  render(<Registration siteKey="site-test" close={() => {}} />);
  expect(screen.getByRole('button', { name: '提交' }).hasAttribute('disabled')).toBe(true);
  expect(api.request).not.toHaveBeenCalled();
});
