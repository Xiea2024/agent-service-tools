import { lazy, Suspense, useEffect } from 'react';
import Portal, { BackHome } from './Portal';
import './portal.css';

const SupportPilot = lazy(() => import('./TenantWorkspace'));
const EvalPage = lazy(() => import('./EvalPage'));

/** 博客暂时下线：旧链接直接回首页，不留 404。 */
function Redirect({ to }: { to: string }) {
  useEffect(() => { window.location.replace(to); }, [to]);
  return <p>正在跳转…</p>;
}

export default function Router() {
  const path = window.location.pathname.replace(/\/+$/, '') || '/';
  useEffect(() => { document.title = path.startsWith('/supportpilot') ? 'SupportPilot · 研发排查工作台' : path.startsWith('/eval') ? 'SupportPilot · 评测与归因' : 'XIEA'; }, [path]);
  if (path === '/supportpilot') return <Suspense fallback={<p>正在打开工作台…</p>}><BackHome /><SupportPilot /></Suspense>;
  if (path === '/eval') return <Suspense fallback={<p>正在打开评测页…</p>}><EvalPage /></Suspense>;
  if (path === '/blog' || path.startsWith('/blog/')) return <Redirect to="/" />;
  if (path !== '/') return <div className="portal not-found"><h1>页面不存在。</h1><a href="/">返回首页 →</a></div>;
  return <Portal />;
}
