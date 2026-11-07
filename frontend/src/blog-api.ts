import { request } from './api';

export const githubUrl = 'https://github.com/Xiea2024';
export interface Post {
  id: string;
  title: string;
  summary: string;
  content?: string;
  published: boolean | number;
  created_at: string;
  updated_at: string;
  published_at: string | null;
}
export const loadPosts = (drafts = false, limit = 100, signal?: AbortSignal) =>
  request<{ posts: Post[] }>(`/blog/posts?drafts=${drafts}&limit=${limit}`, { signal });
export const postDate = (post: Post) => new Date(post.published_at || post.created_at).toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' });
