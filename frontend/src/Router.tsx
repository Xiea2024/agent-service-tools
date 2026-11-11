import { lazy, Suspense, useEffect } from 'react';
import Portal, { BackHome } from './Portal';
import Blog from './Blog';
import './portal.css';

const SupportPilot = lazy(() => import('./TenantWorkspace'));

export default function Router() {
  const path = window.location.pathname.replace(/\/+$/, '') || '/';
  useEffect(() => { document.title = path.startsWith('/supportpilot') ? 'SupportPilot · 研发排查工作台' : path.startsWith('/blog') ? '随想与记录 · XIEA' : 'XIEA · 我的数字小宇宙'; }, [path]);
  if (path === '/supportpilot') return <Suspense fallback={<p>正在打开工作台…</p>}><BackHome /><SupportPilot /></Suspense>;
  if (path === '/blog') return <Blog />;
  if (path === '/blog/new') return <Blog editing />;
  const article = path.match(/^\/blog\/([^/]+)(\/edit)?$/);
  if (article) return <Blog id={article[1]} editing={!!article[2]} />;
  if (path !== '/') return <div className="portal not-found"><h1>这颗星球还未被发现。</h1><a href="/">返回导航首页 →</a></div>;
  return <Portal />;
}
