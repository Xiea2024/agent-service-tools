import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PlanetNav } from './Portal';
import type { Post } from './blog-api';

// jsdom does not implement PointerEvent; MouseEvent preserves relatedTarget.
beforeEach(() => vi.stubGlobal('PointerEvent', MouseEvent));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const posts: Post[] = Array.from({ length: 6 }, (_, index) => ({ id: `post-${index}`, title: `文章 ${index}`, summary: '', published: true, created_at: '2026-11-06T00:00:00Z', updated_at: '2026-11-06T00:00:00Z', published_at: '2026-11-06T00:00:00Z' }));

describe('星球文章导航', () => {
  it('从圆心移动到文章保持展开，离开整个区域后收起', () => {
    render(<PlanetNav posts={posts} />);
    const region = screen.getByTestId('planet-nav');
    const core = screen.getByRole('link', { name: '全部文章' });
    expect(core.getAttribute('href')).toBe('/blog');
    expect(core.getAttribute('aria-expanded')).toBe('false');
    fireEvent.pointerEnter(core, { pointerType: 'mouse' });
    expect(core.getAttribute('aria-expanded')).toBe('true');
    fireEvent.pointerLeave(core, { relatedTarget: region });
    fireEvent.pointerEnter(screen.getByText('文章 0'));
    expect(core.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText('文章 0').closest('a')?.getAttribute('href')).toBe('/blog/post-0');
    expect(screen.queryByText('文章 5')).toBeNull();
    fireEvent.pointerLeave(region);
    expect(core.getAttribute('aria-expanded')).toBe('false');
  });

  it('键盘焦点移向外圈仍展开，焦点离开后收起', () => {
    render(<PlanetNav posts={posts} />);
    const core = screen.getByRole('link', { name: '全部文章' });
    const article = screen.getByText('文章 0').closest('a')!;
    fireEvent.focus(core);
    fireEvent.blur(core, { relatedTarget: article });
    fireEvent.focus(article);
    expect(core.getAttribute('aria-expanded')).toBe('true');
    fireEvent.blur(article, { relatedTarget: null });
    expect(core.getAttribute('aria-expanded')).toBe('false');
  });

  it('空博客提供写作入口', () => {
    render(<PlanetNav posts={[]} />);
    expect(screen.getByText(/等你的第一篇文章/).closest('a')?.getAttribute('href')).toBe('/blog/new');
  });
});
