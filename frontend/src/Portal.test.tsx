import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import Portal from './Portal';

afterEach(cleanup);

describe('首页入口', () => {
  it('只展示工作台和评测两个入口', () => {
    render(<Portal />);
    const hrefs = screen.getAllByRole('link').map(link => link.getAttribute('href'));
    expect(hrefs).toContain('/supportpilot');
    expect(hrefs).toContain('/eval');
    expect(screen.getByRole('heading', { name: 'AGENTS TOOLS' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'SupportPilot' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: '评测与归因' })).toBeTruthy();
  });

  it('博客入口已经下线', () => {
    render(<Portal />);
    const hrefs = screen.getAllByRole('link').map(link => link.getAttribute('href') ?? '');
    expect(hrefs.some(href => href.startsWith('/blog'))).toBe(false);
  });

  it('不保留营销式文案', () => {
    const { container } = render(<Portal />);
    expect(container.textContent ?? '').not.toMatch(/有据可依|有迹可循|无限可能|小宇宙|奇思妙想/);
  });
});
