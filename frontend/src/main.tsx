import { lazy, StrictMode, Suspense } from 'react';
import { createRoot } from 'react-dom/client';
import { STATIC_DEMO } from './demo-mode';
import './styles.css';

const Root = lazy(() => STATIC_DEMO ? import('./DemoWorkspace') : import('./Router'));
if (STATIC_DEMO) document.title = 'SupportPilot · 静态交互演示';
createRoot(document.getElementById('root')!).render(<StrictMode><Suspense fallback={<p>正在打开工作台…</p>}><Root /></Suspense></StrictMode>);
