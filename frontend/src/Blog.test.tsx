import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import Blog from './Blog';

const api = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', () => api);
beforeEach(() => {
  window.history.replaceState(null, '', '/blog/new');
  api.request.mockResolvedValue({ id: 'saved-post' });
});
afterEach(() => { cleanup(); vi.resetAllMocks(); window.history.replaceState(null, '', '/'); });

describe('博客写作', () => {
  it('首次保存草稿后复用文章 ID 更新并发布', async () => {
    render(<Blog editing />);
    fireEvent.change(screen.getByLabelText('文章标题'), { target: { value: '我的第一篇文章' } });
    fireEvent.change(screen.getByLabelText('文章正文'), { target: { value: '# 正文\n\n你好，世界。' } });
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));
    await screen.findByText('草稿已保存。');
    expect(api.request.mock.calls[0][0]).toBe('/blog/posts');
    expect(JSON.parse(api.request.mock.calls[0][1].body)).toMatchObject({ title: '我的第一篇文章', published: false });
    expect(window.location.pathname).toBe('/blog/saved-post/edit');
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: '保存并发布' }));
    await screen.findByText('文章已发布，首页会自动展示最近文章。');
    expect(api.request.mock.calls[1][0]).toBe('/blog/posts/saved-post');
    expect(api.request.mock.calls[1][1].method).toBe('PUT');
    expect(JSON.parse(api.request.mock.calls[1][1].body).published).toBe(true);
  });

  it('预览时空白正文无法保存，失败后保留输入', async () => {
    render(<Blog editing />);
    fireEvent.change(screen.getByLabelText('文章标题'), { target: { value: '保留我的输入' } });
    fireEvent.click(screen.getByRole('button', { name: '预览' }));
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));
    await screen.findByText('请填写文章标题和正文。');
    expect(api.request).not.toHaveBeenCalled();
    api.request.mockRejectedValue(new Error('保存失败'));
    fireEvent.change(screen.getByLabelText('文章正文'), { target: { value: '不能丢失的正文' } });
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe('保存失败'));
    expect((screen.getByLabelText('文章正文') as HTMLTextAreaElement).value).toBe('不能丢失的正文');
  });
});
