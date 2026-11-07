import { useEffect, useState, type FormEvent } from 'react';
import { ArrowLeft, ArrowUpRight, BookOpen, Check, LoaderCircle, PenLine, Save, Trash2 } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { request } from './api';
import { loadPosts, postDate, type Post } from './blog-api';
import { SiteFooter, SiteHeader } from './Portal';

export function BlogMarkdown({ content }: { content: string }) {
  return <div className="blog-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown></div>;
}

export default function Blog({ id, editing = false }: { id?: string; editing?: boolean }) {
  const [posts, setPosts] = useState<Post[]>([]);
  const [post, setPost] = useState<Post | null>(null);
  const [loading, setLoading] = useState(!editing || !!id);
  const [error, setError] = useState('');
  const [manage, setManage] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    if (editing && !id) { setLoading(false); return; }
    setLoading(true); setError('');
    const work = id ? request<Post>(`/blog/posts/${encodeURIComponent(id)}`, { signal: controller.signal }).then(setPost) : loadPosts(manage, 100, controller.signal).then(data => setPosts(data.posts));
    work.catch(error => { if (!controller.signal.aborted) setError(error instanceof Error ? error.message : '文章加载失败'); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [id, editing, manage]);
  return <div className="portal blog-shell"><SiteHeader blog /><main className="blog-main"><a className="blog-back" href={id || editing ? '/blog' : '/'}><ArrowLeft size={15} />{id || editing ? '返回全部文章' : '返回导航首页'}</a>
    {loading ? <div className="blog-empty" role="status"><LoaderCircle className="spin" />正在读取文章…</div> : error ? <div className="blog-empty" role="alert"><p>{error}</p><button className="portal-button" onClick={() => window.location.reload()}>重新加载</button></div> : editing ? <PostEditor initial={post} /> : id && post ? <article className="blog-article"><div className="article-meta">{postDate(post)} · {post.published ? '已发布' : '草稿'}</div><h1>{post.title}</h1>{post.summary && <p className="article-summary">{post.summary}</p>}<a className="blog-edit-link" href={`/blog/${post.id}/edit`}><PenLine size={14} /> 编辑文章</a><BlogMarkdown content={post.content || ''} /></article> : <><div className="blog-heading"><div><span className="portal-eyebrow">THE DIGITAL GARDEN</span><h1>随想与记录<span>✳</span></h1><p>把探索的过程，慢慢写成自己的答案。</p></div><a className="portal-button" href="/blog/new"><PenLine size={16} /> 写文章</a></div><div className="blog-toolbar"><span>{posts.length} 篇{manage ? '文章与草稿' : '已发布文章'}</span><button onClick={() => setManage(!manage)}>{manage ? '只看已发布' : '管理文章 / 草稿'}</button></div>{posts.length ? <div className="post-list">{posts.map((item, index) => <a href={`/blog/${item.id}${!item.published ? '/edit' : ''}`} key={item.id}><span className="post-index">{String(index + 1).padStart(2, '0')}</span><div><span className="article-meta">{postDate(item)}{!item.published && ' · 草稿'}</span><h2>{item.title}</h2><p>{item.summary || '点击阅读文章'}</p></div><ArrowUpRight size={22} /></a>)}</div> : <div className="blog-empty"><BookOpen size={34} /><h2>故事，从第一篇开始。</h2><p>这里还没有{manage ? '文章' : '已发布的文章'}，把此刻的想法写下来吧。</p><a className="portal-button" href="/blog/new"><PenLine size={16} /> 写第一篇文章</a></div>}</>}
  </main><SiteFooter /></div>;
}

function PostEditor({ initial }: { initial: Post | null }) {
  const [id, setId] = useState(initial?.id);
  const [title, setTitle] = useState(initial?.title || '');
  const [summary, setSummary] = useState(initial?.summary || '');
  const [content, setContent] = useState(initial?.content || '');
  const [published, setPublished] = useState(!!initial?.published);
  const [preview, setPreview] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [dirty, setDirty] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  useEffect(() => { const handler = (event: BeforeUnloadEvent) => { if (dirty) { event.preventDefault(); event.returnValue = ''; } }; window.addEventListener('beforeunload', handler); return () => window.removeEventListener('beforeunload', handler); }, [dirty]);
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(''); setMessage('');
    if (!title.trim() || !content.trim()) { setError('请填写文章标题和正文。'); setPreview(false); setBusy(false); return; }
    try { const saved = await request<Post>(`/blog/posts${id ? `/${id}` : ''}`, { method: id ? 'PUT' : 'POST', body: JSON.stringify({ title, summary, content, published }) }); setId(saved.id); setDirty(false); setMessage(published ? '文章已发布，首页会自动展示最近文章。' : '草稿已保存。'); window.history.replaceState(null, '', `/blog/${saved.id}/edit`); }
    catch (error) { setError(error instanceof Error ? error.message : '保存失败，请重试'); }
    finally { setBusy(false); }
  }
  async function remove() {
    setBusy(true); setError('');
    try { await request(`/blog/posts/${id}`, { method: 'DELETE' }); setDirty(false); window.location.assign('/blog'); }
    catch (error) { setError(error instanceof Error ? error.message : '删除失败'); setBusy(false); }
  }
  return <form className="post-editor" onSubmit={event => void save(event)} onChange={() => { setDirty(true); setMessage(''); }}><div className="blog-heading"><div><span className="portal-eyebrow">A NEW IDEA STARTS HERE</span><h1>{id ? '编辑文章' : '写一篇新文章'}<span>✎</span></h1></div><button className="portal-button" disabled={busy} type="submit">{busy ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />}{published ? '保存并发布' : '保存草稿'}</button></div>
    {error && <p className="editor-error" role="alert">{error}</p>}{message && <p className="editor-success" role="status"><Check size={16} />{message}{id && <a href={`/blog/${id}`}>查看文章 <ArrowUpRight size={13} /></a>}</p>}
    <fieldset disabled={busy}><label htmlFor="post-title">文章标题</label><input id="post-title" value={title} onChange={event => setTitle(event.target.value)} required maxLength={200} placeholder="给这个想法起个名字…" /><label htmlFor="post-summary">摘要 <span>选填 · 在文章列表中显示</span></label><textarea id="post-summary" value={summary} onChange={event => setSummary(event.target.value)} maxLength={500} rows={2} placeholder="用一两句话介绍这篇文章" /><div className="editor-tabs"><label htmlFor="post-content">文章正文</label><div><button type="button" className={!preview ? 'selected' : ''} onClick={() => setPreview(false)}>Markdown</button><button type="button" className={preview ? 'selected' : ''} onClick={() => setPreview(true)}>预览</button></div></div><textarea className="content-input" hidden={preview} id="post-content" value={content} onChange={event => setContent(event.target.value)} maxLength={200000} rows={18} placeholder={'# 从这里开始\n\n写下你的想法，支持 Markdown 格式。'} />{preview && <div className="editor-preview"><BlogMarkdown content={content || '还没有正文，切换到 Markdown 开始写作。'} /></div>}<label className="publish-toggle"><input type="checkbox" checked={published} onChange={event => setPublished(event.target.checked)} /><span>发布文章<small>发布后可在博客列表和首页星球导航中看到；取消勾选可退回草稿。</small></span></label></fieldset>
    {id && <div className="editor-delete">{confirmDelete ? <><span>确定永久删除这篇文章？</span><button type="button" disabled={busy} onClick={() => void remove()}>确定删除</button><button type="button" disabled={busy} onClick={() => setConfirmDelete(false)}>取消</button></> : <button type="button" onClick={() => setConfirmDelete(true)}><Trash2 size={14} /> 删除文章</button>}</div>}
  </form>;
}
