import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import TenantWorkspace from './TenantWorkspace';

const api = vi.hoisted(() => ({ request: vi.fn(), selectTenant: vi.fn() }));
vi.mock('./api', () => api);
vi.mock('./App', () => ({ default: ({ identity, tenantMode }: { identity?: string; tenantMode?: boolean }) => <div>{tenantMode ? `用户:${identity}` : '本地工作台'}</div> }));
const profile = { user_id: 'user-a', email: 'a@example.com', tenant_id: 'team-a', tenants: [
  { id: 'team-a', name: '团队 A', role: 'admin' }, { id: 'team-b', name: '团队 B', role: 'member' },
] };

beforeEach(() => {
  api.request.mockImplementation(async (path: string) => {
    if (path === '/auth/config') return { enabled: true };
    if (path === '/auth/me') throw new Error('未登录');
    if (path === '/auth/login') return profile;
    if (path === '/tenants/members') return { members: [] };
    if (path === '/knowledge/documents') return { documents: [] };
    return {};
  });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it('登录后使用服务端身份，切换团队后隐藏管理员操作，退出后清除身份', async () => {
  render(<TenantWorkspace />);
  await screen.findByText('登录你的工作空间');
  fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'a@example.com' } });
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'test-password-123' } });
  fireEvent.click(screen.getByRole('button', { name: '登录' }));
  await screen.findByText('用户:user-a');
  expect(api.selectTenant).toHaveBeenLastCalledWith('team-a');
  fireEvent.click(screen.getByRole('button', { name: '团队与知识库' }));
  await screen.findByRole('button', { name: '添加成员' });
  await waitFor(() => expect(screen.getByLabelText('切换团队').hasAttribute('disabled')).toBe(false));
  fireEvent.change(screen.getByLabelText('切换团队'), { target: { value: 'team-b' } });
  expect(api.selectTenant).toHaveBeenLastCalledWith('team-b');
  fireEvent.click(screen.getByRole('button', { name: '团队与知识库' }));
  await screen.findByText('尚未导入文档。');
  expect(screen.queryByRole('button', { name: '添加成员' })).toBeNull();
  expect(screen.queryByLabelText('导入文档向量文件')).toBeNull();
  await waitFor(() => expect(screen.getByRole('button', { name: '退出登录' }).hasAttribute('disabled')).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '退出登录' }));
  await screen.findByText('登录你的工作空间');
  expect(api.selectTenant).toHaveBeenLastCalledWith('');
});

it('关闭多租户时保留原工作台', async () => {
  api.request.mockResolvedValue({ enabled: false });
  render(<TenantWorkspace />);
  await screen.findByText('本地工作台');
});

it('已有登录会话会恢复，认证过期后回到登录表单', async () => {
  api.request.mockImplementation(async (path: string) => path === '/auth/config' ? { enabled: true } : profile);
  render(<TenantWorkspace />);
  await screen.findByText('用户:user-a');
  fireEvent(window, new Event('supportpilot:unauthorized'));
  await screen.findByText('登录你的工作空间');
  expect(api.selectTenant).toHaveBeenLastCalledWith('');
});
