import { useEffect, useRef, useState, type FormEvent } from 'react';
import { request } from './api';

declare global {
  interface Window { turnstile?: { render: (element: HTMLElement, options: Record<string, unknown>) => string; remove: (id: string) => void } }
}

export default function Registration({ siteKey, close }: { siteKey: string; close: () => void }) {
  const [mode, setMode] = useState<'register' | 'verify' | 'forgot' | 'reset'>('register');
  const [human, setHuman] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [generation, setGeneration] = useState(0);
  const widget = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (mode !== 'register' && mode !== 'forgot') return;
    let id: string | undefined;
    let cancelled = false;
    const render = () => {
      if (!cancelled && widget.current && window.turnstile) id = window.turnstile.render(widget.current, {
        sitekey: siteKey, callback: (token: string) => setHuman(token),
        'expired-callback': () => setHuman(''), 'error-callback': () => setHuman(''),
      });
    };
    const script = document.createElement('script');
    if (window.turnstile) render();
    else { script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit'; script.onload = render; document.head.appendChild(script); }
    return () => { cancelled = true; if (id) window.turnstile?.remove(id); script.remove(); setHuman(''); };
  }, [siteKey, mode, generation]);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    const body: Record<string, unknown> = {};
    if (mode === 'register' || mode === 'forgot') { body.email = fields.get('email'); body.human_token = human; }
    if (mode === 'register' || mode === 'reset') body.password = fields.get('password');
    if (mode === 'verify' || mode === 'reset') body.token = fields.get('token');
    setBusy(true); setNotice('');
    try {
      const result = await request<{ status: string }>(`/auth/${mode}`, { method: 'POST', body: JSON.stringify(body) });
      setNotice(result.status);
      if (mode === 'register') setMode('verify');
      if (mode === 'forgot') setMode('reset');
    } catch (e) { setNotice(e instanceof Error ? e.message : '请求失败'); setGeneration(n => n + 1); }
    finally { setBusy(false); }
  }
  return <div className="tenant-login"><form onSubmit={submit}>
    <h1>{({ register: '注册普通账号', verify: '验证邮箱', forgot: '找回密码', reset: '重置密码' })[mode]}</h1>
    {(mode === 'register' || mode === 'forgot') && <label>邮箱<input name="email" type="email" required maxLength={254} /></label>}
    {(mode === 'verify' || mode === 'reset') && <label>邮件中的验证码<input name="token" required minLength={32} maxLength={128} autoComplete="one-time-code" /></label>}
    {(mode === 'register' || mode === 'reset') && <label>密码<input name="password" type="password" required minLength={10} maxLength={256} autoComplete="new-password" /></label>}
    <div ref={widget} />
    <button disabled={busy || ((mode === 'register' || mode === 'forgot') && !human)}>提交</button>
    {notice && <p role="status">{notice}</p>}
    <button type="button" onClick={() => { setMode('forgot'); setNotice(''); }}>找回密码</button>
    <button type="button" onClick={close}>返回登录</button>
  </form></div>;
}
