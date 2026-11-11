import { useState, type FormEvent } from 'react';
import App from './App';
import { demoLogin, demoLogout, isDemoLoggedIn, resetDemo } from './demo';
import './tenant.css';
import './demo.css';

export default function DemoWorkspace() {
  const [loggedIn, setLoggedIn] = useState(isDemoLoggedIn);
  const [generation, setGeneration] = useState(0);
  const [error, setError] = useState('');
  function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    try { demoLogin(String(fields.get('username')), String(fields.get('password'))); setLoggedIn(true); setError(''); }
    catch (e) { setError(e instanceof Error ? e.message : '无法进入演示'); }
  }
  if (!loggedIn) return <div className="tenant-login demo-login"><form onSubmit={login}>
    <span className="demo-pill">交互式产品演示</span><h1>体验 SupportPilot</h1>
    <p>从问题描述到文档证据，体验一次完整的研发排查流程。</p>
    <label>账号<input name="username" autoComplete="username" required defaultValue="guest" /></label>
    <label>密码<input name="password" type="password" autoComplete="current-password" required defaultValue="guest" /></label>
    <p>演示账号：<strong>guest / guest</strong></p>{error && <p role="alert">{error}</p>}
    <button>进入演示</button>
    <small>问答和检索结果均为预录示例，不连接服务器或模型。此入口不提供真实账号安全隔离，请勿输入个人密码或私有资料。</small>
  </form></div>;
  return <div className="demo-shell">
    <div className="demo-bar"><span><strong>静态演示</strong> · 预录问答与模拟流式输出 · 不调用模型</span>
      <button onClick={() => { resetDemo(); setGeneration(n => n + 1); }}>重置演示</button>
      <button onClick={() => { demoLogout(); setLoggedIn(false); }}>退出 guest</button>
    </div>
    <App key={generation} identity="static-demo-guest" demoMode />
  </div>;
}
