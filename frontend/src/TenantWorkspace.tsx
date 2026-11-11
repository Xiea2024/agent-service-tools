import { useEffect, useState, type FormEvent } from 'react';
import App from './App';
import { request, selectTenant } from './api';
import { safeUrl } from './conversation';
import './tenant.css';
import Registration from './Registration';

interface Team { id: string; name: string; role: 'admin' | 'member' }
interface Profile { platform_admin?: boolean; user_id: string; email: string; tenant_id: string; tenants: Team[] }
interface Member { id: string; email: string; role: string }
interface Document { document_id: string; title: string; source_url: string; chunk_count: number }
const message = (e: unknown) => e instanceof Error ? e.message : '操作失败，请稍后重试。';

export default function TenantWorkspace() {
  const [signup, setSignup] = useState(false);
  const [authConfig, setAuthConfig] = useState({ public_demo: false, registration_enabled: false, turnstile_site_key: '' });
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const [panel, setPanel] = useState(false);
  const [members, setMembers] = useState<Member[]>([]);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [notice, setNotice] = useState('');
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    let mounted = true;
    selectTenant('');
    setError('');
    void request<{ enabled: boolean; public_demo: boolean; registration_enabled: boolean; turnstile_site_key: string }>('/auth/config').then(async config => {
      if (!mounted) return;
      setEnabled(config.enabled); setAuthConfig(config);
      if (config.enabled) {
        try {
          const user = await request<Profile>('/auth/me');
          if (mounted) { selectTenant(user.tenant_id); setProfile(user); }
        } catch { /* 未登录时显示登录表单。 */ }
      }
    }).catch(e => { if (mounted) setError(message(e)); });
    const unauthorized = () => { setProfile(null); selectTenant(''); setPanel(false); };
    window.addEventListener('supportpilot:unauthorized', unauthorized);
    return () => { mounted = false; window.removeEventListener('supportpilot:unauthorized', unauthorized); };
  }, [retry]);

  async function act(work: () => Promise<void>) {
    setPending(true); setError(''); setNotice('');
    try { await work(); } catch (e) { setError(message(e)); }
    finally { setPending(false); }
  }
  async function loadPanel() {
    const [people, docs] = await Promise.all([
      (authConfig.public_demo && !profile?.platform_admin && profile?.tenants.find(t => t.id === profile.tenant_id)?.role !== 'admin') ? Promise.resolve({ members: [] }) : request<{ members: Member[] }>('/tenants/members'),
      request<{ documents: Document[] }>('/knowledge/documents'),
    ]);
    setMembers(people.members); setDocuments(docs.documents);
  }
  function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    void act(async () => {
      const user = await request<Profile>('/auth/login', { method: 'POST', body: JSON.stringify({ email: fields.get('email'), password: fields.get('password') }) });
      selectTenant(user.tenant_id); setProfile(user);
    });
  }

  if (enabled === false) return <App />;
  if (enabled === null) return <div className="tenant-login"><h1>SupportPilot</h1><p>{error || '正在连接工作台…'}</p>{error && <button onClick={() => setRetry(n => n + 1)}>重新连接</button>}</div>;
  if (!profile && signup) return <Registration siteKey={authConfig.turnstile_site_key} close={() => setSignup(false)} />;
  if (!profile) return <div className="tenant-login"><form onSubmit={login}><span>SupportPilot · 团队工作台</span><h1>登录你的工作空间</h1><p>使用已激活的账号登录。</p><label>邮箱<input name="email" type="email" autoComplete="username" required maxLength={254} /></label><label>密码<input name="password" type="password" autoComplete="current-password" required maxLength={256} /></label>{error && <p role="alert">{error}</p>}<button disabled={pending}>{pending ? '正在登录…' : '登录'}</button>{authConfig.registration_enabled && <button type="button" onClick={() => setSignup(true)}>注册 / 找回密码</button>}</form></div>;
  const team = profile.tenants.find(t => t.id === profile.tenant_id)!;

  return <div className="tenant-shell">
    <div className="tenant-bar"><strong>团队空间</strong><select aria-label="切换团队" value={profile.tenant_id} disabled={pending} onChange={event => {
      const id = event.target.value;
      selectTenant(id); setProfile({ ...profile, tenant_id: id }); setPanel(false); setMembers([]); setDocuments([]); setError(''); setNotice('');
    }}>{profile.tenants.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}</select><span>{team.role === 'admin' ? '管理员' : '成员'} · {profile.email}</span><button disabled={pending} onClick={() => { setPanel(!panel); if (!panel) void act(loadPanel); }}>团队与知识库</button><button disabled={pending} onClick={() => void act(async () => {
      await request('/auth/logout', { method: 'POST' }); setProfile(null); selectTenant(''); setPanel(false);
    })}>退出登录</button></div>
    {error && <div className="tenant-alert" role="alert">{error}<button onClick={() => setError('')}>关闭</button></div>}
    {panel && <section className="tenant-management" aria-label="团队与知识库管理">
      <button className="tenant-close" onClick={() => setPanel(false)}>关闭</button><h2>{team.name}</h2><p>会话与回答仅本人可见，知识库由团队共享。</p>
      {(!authConfig.public_demo || profile.platform_admin) && <form onSubmit={event => {
        event.preventDefault(); const form = event.currentTarget; const fields = new FormData(form);
        void act(async () => {
          const created = await request<Team>('/tenants', { method: 'POST', body: JSON.stringify({ name: fields.get('name') }) });
          selectTenant(created.id); setProfile({ ...profile, tenant_id: created.id, tenants: [...profile.tenants, created] }); setMembers([]); setDocuments([]); setPanel(false);
        });
      }}><h3>创建团队</h3><input name="name" aria-label="新团队名称" placeholder="新团队名称" required maxLength={80} /><button disabled={pending}>创建并切换</button></form>}
      <h3>团队成员</h3><ul>{members.map(m => <li key={m.id}>{m.email} · {m.role === 'admin' ? '管理员' : '成员'}</li>)}</ul>
      {team.role === 'admin' && <form onSubmit={event => {
        event.preventDefault(); const form = event.currentTarget; const fields = new FormData(form);
        void act(async () => {
          await request('/tenants/members', { method: 'POST', body: JSON.stringify({ email: fields.get('email'), password: fields.get('password') || null, role: fields.get('role') }) });
          form.reset(); await loadPanel(); setNotice('成员已加入。已有账号的密码保持原值。');
        });
      }}><label>成员邮箱<input name="email" type="email" required maxLength={254} /></label>{!authConfig.public_demo && <label>新账号初始密码<input name="password" type="password" autoComplete="new-password" minLength={10} maxLength={256} placeholder="已有账号可留空" /></label>}<select name="role" aria-label="成员角色"><option value="member">成员</option>{(!authConfig.public_demo || profile.platform_admin) && <option value="admin">管理员</option>}</select><button disabled={pending}>添加成员</button></form>}
      <h3>团队知识库</h3><ul>{documents.map(doc => <li key={doc.document_id}>{safeUrl(doc.source_url) ? <a href={safeUrl(doc.source_url)} target="_blank" rel="noreferrer">{doc.title}</a> : doc.title} · {doc.chunk_count} 个片段</li>)}</ul>{!documents.length && <p>尚未导入文档。</p>}
      {team.role === 'admin' && <label>导入文档向量文件（vectors.json，最多 10 MB）<input aria-label="导入文档向量文件" type="file" accept=".json" disabled={pending} onChange={event => {
        const file = event.target.files?.[0]; event.target.value = '';
        if (file) void act(async () => {
          if (file.size > 10 * 1024 * 1024) throw new Error('文件不能超过 10 MB');
          await request('/knowledge/documents/import', { method: 'POST', body: await file.text(), signal: AbortSignal.timeout(120_000) });
          await loadPanel(); setNotice('文档入库与读回验证完成。');
        });
      }} /></label>}
      {notice && <p role="status">{notice}</p>}
    </section>}
    <App key={`${profile.user_id}:${profile.tenant_id}`} identity={profile.user_id} tenantMode />
  </div>;
}
