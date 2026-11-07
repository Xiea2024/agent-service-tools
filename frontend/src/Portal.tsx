import { useEffect, useState, type CSSProperties } from 'react';
import { ArrowDown, ArrowLeft, ArrowRight, ArrowUpRight, BookOpen, Command, Github, PenLine, Sparkles, Star } from 'lucide-react';
import { githubUrl, loadPosts, postDate, type Post } from './blog-api';

export function SiteHeader({ blog = false }: { blog?: boolean }) {
  return <header className="site-header"><a className="site-brand" href="/" aria-label="返回导航首页"><span className="brand-orbit">✳</span>XIEA<span className="brand-period">.</span></a><nav aria-label="主导航"><a className={!blog ? 'current' : ''} href="/">导航首页</a><a className={blog ? 'current' : ''} href="/blog">博客</a><a href="/supportpilot">SupportPilot <ArrowUpRight size={13} /></a></nav><a className="github-link" href={githubUrl} target="_blank" rel="noreferrer" aria-label="GitHub 个人主页"><Github size={20} /><span>GitHub</span><ArrowUpRight size={13} /></a></header>;
}

export function SiteFooter() {
  return <footer className="site-footer"><span>© {new Date().getFullYear()} XIEA · 保持好奇，持续创造。</span><span><i /> A little corner of the internet</span><a href={githubUrl} target="_blank" rel="noreferrer" aria-label="访问 GitHub 主页"><Github size={17} /></a></footer>;
}

export function PlanetNav({ posts }: { posts: Post[] }) {
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const open = hovered || focused;
  return <div className={`planet-nav ${open ? 'is-open' : ''}`} data-testid="planet-nav" onPointerLeave={() => setHovered(false)} onFocus={() => setFocused(true)} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false); }} onKeyDown={event => { if (event.key === 'Escape') { setHovered(false); setFocused(false); } }}>
    <div className="orbit orbit-one" aria-hidden="true" /><div className="orbit orbit-two" aria-hidden="true" />
    <span className="space-spark spark-one" aria-hidden="true">✦</span><span className="space-spark spark-two" aria-hidden="true">✧</span><span className="space-spark spark-three" aria-hidden="true">+</span><span className="tiny-moon" aria-hidden="true" />
    <span className="orbit-label">A SMALL PLANET OF IDEAS</span>
    <a className="planet-core" href="/blog" onPointerEnter={event => { if (event.pointerType !== 'touch') setHovered(true); }} aria-label="全部文章" aria-expanded={open} aria-controls="planet-articles">
      <span className="planet-crater crater-one" /><span className="planet-crater crater-two" /><span className="planet-crater crater-three" />
      <span className="planet-face" aria-hidden="true"><i /><i /><b /></span><span className="planet-title">全部文章</span><span className="planet-subtitle">EXPLORE THE BLOG <ArrowUpRight size={12} /></span><span className="planet-ring" aria-hidden="true" />
    </a>
    <div id="planet-articles" className="planet-articles" inert={!open}>
      {posts.slice(0, 5).map((post, index) => { const angle = (-90 + index * 72) * Math.PI / 180; return <a key={post.id} className={`article-satellite satellite-${index}`} href={`/blog/${post.id}`} style={{ '--x': `${Math.cos(angle) * 225}px`, '--y': `${Math.sin(angle) * 205}px`, '--delay': `${index * 35}ms` } as CSSProperties}><span className="satellite-number">0{index + 1}<Star size={12} /></span><strong>{post.title}</strong><span className="satellite-date">{postDate(post)} <ArrowUpRight size={13} /></span></a>; })}
      {!posts.length && <a className="planet-empty" href="/blog/new"><PenLine size={15} /> 这颗星球，等你的第一篇文章 <ArrowUpRight size={14} /></a>}
    </div>
    <span className="planet-hint"><span /> 悬停星球，发现最近文章</span>
  </div>;
}

export default function Portal() {
  const [posts, setPosts] = useState<Post[]>([]);
  const [status, setStatus] = useState('正在读取最近文章…');
  useEffect(() => { const controller = new AbortController(); loadPosts(false, 5, controller.signal).then(data => { setPosts(data.posts); setStatus('还没有文章，故事即将开始。'); }).catch(() => { if (!controller.signal.aborted) setStatus('文章暂时无法加载，请前往博客重试。'); }); return () => controller.abort(); }, []);
  return <div className="portal"><SiteHeader /><main className="portal-main"><section className="portal-hero"><div className="hero-copy"><span className="portal-eyebrow"><span /> HELLO, WORLD. I'M XIEA</span><h1>想法在这里<span>着陆<span className="heading-star">✳</span></span></h1><p>写一点代码，记一些想法。<br />这是我的数字小宇宙，欢迎随意探索。</p><a className="explore-link" href="#destinations">选一个目的地 <ArrowDown size={16} /></a><span className="handwritten">Stay curious, keep building.</span></div><div className="planet-scene"><PlanetNav posts={posts} /></div></section>
    <section className="mobile-recent" aria-labelledby="recent-title"><h2 id="recent-title">最近文章 <span>RECENT NOTES</span></h2>{posts.length ? posts.map(post => <a key={post.id} href={`/blog/${post.id}`}><span>{post.title}</span><ArrowUpRight size={16} /></a>) : <p>{status}</p>}</section>
    <section className="destinations" id="destinations" aria-labelledby="destinations-title"><div className="destinations-heading"><h2 id="destinations-title">我的目的地 <span>THE DIRECTORY</span></h2><span>两个入口，无限可能 <Sparkles size={13} /></span></div><div className="destination-grid"><a href="/supportpilot" className="destination-card support-destination"><div className="destination-top"><span className="destination-icon"><Command size={25} /></span><span className="destination-tag">AI WORKSPACE</span><ArrowUpRight className="destination-arrow" size={23} /></div><h3>SupportPilot<span>研发排查工作台</span></h3><p>把问题说清楚，让排查有据可依。<br />对话、检索与文档证据，在一个工作台汇合。</p><span className="destination-cta">进入工作台 <ArrowRight size={16} /></span><span className="card-decoration" aria-hidden="true">⌘</span></a><a href="/blog" className="destination-card blog-destination"><div className="destination-top"><span className="destination-icon"><BookOpen size={25} /></span><span className="destination-tag">DIGITAL GARDEN</span><ArrowUpRight className="destination-arrow" size={23} /></div><h3>随想与记录<span>我的个人博客</span></h3><p>关于技术、实践，以及一路上的奇思妙想。<br />把探索的过程，慢慢写成自己的答案。</p><span className="destination-cta">翻开博客 <ArrowRight size={16} /></span><span className="card-decoration" aria-hidden="true">✳</span></a></div></section>
    <div className="portal-note"><Star size={14} /><span>不必一次抵达所有地方，选一颗感兴趣的星球就好。</span><span className="note-line" /></div></main><SiteFooter /></div>;
}

export function BackHome() { return <a className="back-home" href="/"><ArrowLeft size={14} /> 导航首页</a>; }
