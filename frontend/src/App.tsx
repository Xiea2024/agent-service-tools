import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import {
  ArrowDown, ArrowRight, ArrowUp, BookOpen, Check, ChevronDown, ChevronRight,
  CircleHelp, Command, Copy, ExternalLink, FileText, History, Layers3,
  LoaderCircle, Menu, MessageSquare, PanelRightClose, PanelRightOpen, Plus,
  RefreshCw, Search, ShieldCheck, Sparkles, Square, Terminal, X,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { AGENT, getHistory, listThreads, request, streamAnswer, type Metadata, type Thread } from './api';
import { evidenceFor, historyToTurns, safeUrl, toolsFor, type Turn } from './conversation';

function stored(key: string) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function remember(key: string, value: string) {
  try { localStorage.setItem(key, value); } catch { /* 禁用存储时仍可在本次页面内使用。 */ }
}
function browserUser() {
  const user = stored('supportpilot.user') || `web:${crypto.randomUUID()}`;
  remember('supportpilot.user', user);
  return user;
}
const messageOf = (error: unknown) => error instanceof TypeError ? '无法连接服务，请确认后端与网页代理已启动。'
  : error instanceof Error ? error.message : '操作失败，请稍后重试。';
const suggestions = [
  { icon: Terminal, title: '接口异常', text: '接口返回 500，应该提供哪些信息来定位问题？' },
  { icon: Layers3, title: '启动与配置', text: '应用升级依赖后启动失败，帮我梳理排查步骤。' },
  { icon: BookOpen, title: '查知识文档', text: '请查知识库里与跨域请求失败有关的文档，给出排查建议和出处。' },
];

function Markdown({ children }: { children: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
    a: ({ href, children }) => safeUrl(href || '')
      ? <a href={safeUrl(href!)} target="_blank" rel="noreferrer">{children}<ExternalLink size={12} /></a>
      : <span>{children}</span>,
    img: ({ alt }) => <span className="image-label">[图片：{alt || '外部图片'}]</span>,
  }}>{children}</ReactMarkdown>;
}

function CopyAnswer({ text }: { text: string }) {
  const [state, setState] = useState('');
  return <button className="text-button" onClick={() => {
    void navigator.clipboard.writeText(text).then(() => setState('已复制')).catch(() => setState('复制失败，请手动选择文本'));
  }} aria-label="复制回答">{state === '已复制' ? <Check size={13} /> : <Copy size={13} />}{state || '复制回答'}</button>;
}

function AnswerFeedback({ runId }: { runId: string }) {
  const [state, setState] = useState('');
  async function submit(helpful: boolean) {
    setState('正在提交');
    try {
      await request(`/runs/${runId}/feedback`, { method: 'POST', body: JSON.stringify({ helpful }) });
      setState('评价已保存');
    } catch (e) { setState(e instanceof Error ? e.message : '提交失败'); }
  }
  return <span>{state || <><button className="text-button" onClick={() => void submit(true)}>有帮助</button><button className="text-button" onClick={() => void submit(false)}>需改进</button></>}</span>;
}

export default function App({ identity, tenantMode = false, demoMode = false }: { identity?: string; tenantMode?: boolean; demoMode?: boolean } = {}) {
  const [user] = useState(() => identity || browserUser());
  const [metadata, setMetadata] = useState<Metadata | null>(null);
  const [model, setModel] = useState('');
  const [threads, setThreads] = useState<Thread[]>([]);
  const [threadId, setThreadId] = useState(() => crypto.randomUUID() as string);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState('');
  const [search, setSearch] = useState('');
  const [busy, setBusy] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [connecting, setConnecting] = useState(true);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const [evidenceOpen, setEvidenceOpen] = useState(() => window.matchMedia('(min-width: 941px)').matches);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [showHelp, setShowHelp] = useState(false);
  const [bootKey, setBootKey] = useState(0);
  const active = useRef<AbortController | null>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const [scrolledUp, setScrolledUp] = useState(false);
  const composer = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    let mounted = true;
    const controller = new AbortController();
    setConnecting(true);
    setError('');
    async function connect() {
      try {
        const info = await request<Metadata>('/info', { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(12_000)]) });
        if (!info.agents.some(agent => agent.key === AGENT)) throw new Error('后端未注册 dev-issue-agent，请检查启动的项目版本。');
        const result = await listThreads(user);
        if (!mounted) return;
        setMetadata(info);
        setModel(previous => info.models.includes(previous) ? previous : info.default_model);
        setConnected(true);
        setThreads(result.threads);
      } catch (cause) {
        if (mounted) { setConnected(false); setError(messageOf(cause)); }
      } finally { if (mounted) setConnecting(false); }
    }
    void connect();
    return () => { mounted = false; controller.abort(); };
  }, [user, bootKey]);

  useEffect(() => () => { active.current?.abort(); }, []);
  useEffect(() => {
    if (follow.current) scroll.current?.scrollTo({ top: scroll.current.scrollHeight, behavior: 'instant' });
  }, [turns, historyLoading]);
  useEffect(() => {
    if (composer.current) {
      composer.current.style.height = 'auto';
      composer.current.style.height = `${Math.min(composer.current.scrollHeight, 150)}px`;
    }
  }, [input]);

  async function refreshThreads() {
    const result = await listThreads(user);
    setThreads(result.threads);
  }

  function newChat() {
    if (active.current) return;
    setThreadId(crypto.randomUUID());
    setTurns([]); setSelected(null); setInput(''); setError(''); setNotice('');
    setSidebarOpen(false); follow.current = true; setScrolledUp(false);
    composer.current?.focus();
  }

  async function loadThread(id: string) {
    if (active.current) return;
    const controller = new AbortController();
    active.current = controller;
    setHistoryLoading(true); setError(''); setNotice(''); setSidebarOpen(false);
    try {
      const result = await getHistory(id, AbortSignal.any([controller.signal, AbortSignal.timeout(15_000)]));
      const restored = historyToTurns(result.messages);
      setThreadId(id); setTurns(restored); setSelected(null);
      follow.current = true; setScrolledUp(false);
    } catch (cause) { if (!controller.signal.aborted) setError(messageOf(cause)); }
    finally { active.current = null; setHistoryLoading(false); }
  }

  async function send(event?: FormEvent) {
    event?.preventDefault();
    const question = input.trim();
    if (!question || active.current || !connected) return;
    const controller = new AbortController();
    active.current = controller;
    const id = crypto.randomUUID();
    setTurns(previous => [...previous, { id, question, messages: [], draft: '', status: 'streaming' }]);
    setInput(''); setBusy(true); setError(''); setNotice(''); setSelected(null);
    follow.current = true; setScrolledUp(false);
    const update = (change: (turn: Turn) => Turn) => setTurns(previous => previous.map(turn => turn.id === id ? change(turn) : turn));
    // 后端单轮预算 65 秒；独立看门狗避免网络静默挂起。
    let timedOut = false;
    const timeout = window.setTimeout(() => { timedOut = true; controller.abort(); }, 110_000);
    try {
      await streamAnswer({ message: question, thread_id: threadId, user_id: user, model: model || undefined }, controller.signal, event => {
        if (event.type === 'error') throw new Error(event.content);
        if (event.type === 'token') update(turn => ({ ...turn, draft: turn.draft + event.content }));
        if (event.type === 'message') update(turn => ({
          ...turn, messages: [...turn.messages, event.content],
          // 完整 AI 消息替换当前文本草稿，避免 token 和最终消息重复显示。
          draft: event.content.type === 'ai' ? '' : turn.draft,
        }));
      });
      update(turn => ({ ...turn, status: 'complete' }));
    } catch (cause) {
      const stopped = controller.signal.aborted && !timedOut;
      update(turn => ({ ...turn, status: stopped ? 'stopped' : 'error', error: stopped
        ? demoMode ? '模拟输出已停止，可刷新历史查看已生成的片段。' : '已停止接收。后端可能仍在执行，请稍后刷新历史确认结果。'
        : timedOut ? '等待回答超时。后端可能仍在执行，请稍后刷新历史确认结果。' : messageOf(cause) }));
    } finally {
      clearTimeout(timeout);
      active.current = null; setBusy(false);
      // 即使回答中断，已写入 checkpoint 的线程也应该出现在历史列表。
      void refreshThreads().catch(() => setNotice('会话列表刷新失败，已显示的回答仍然保留。'));
    }
  }

  function onComposerKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) {
      event.preventDefault(); void send();
    }
  }

  const evidenceTurn = turns.find(turn => turn.id === selected) ?? turns.at(-1);
  const evidence = evidenceTurn ? evidenceFor(evidenceTurn) : [];
  const tools = evidenceTurn ? toolsFor(evidenceTurn) : [];
  const currentTitle = turns[0]?.question || '新的排查';
  const locked = busy || historyLoading;
  const visibleThreads = threads.filter(thread => (thread.title || '未命名会话').toLowerCase().includes(search.toLowerCase()));
  const latest = turns.at(-1);
  const activity = latest && toolsFor(latest).some(tool => !tool.result) ? '正在检索文档' : '正在组织回答';

  return <div className={`workspace ${evidenceOpen ? '' : 'without-evidence'}`}>
    {sidebarOpen && <button className="drawer-backdrop" aria-label="关闭会话列表" onClick={() => setSidebarOpen(false)} />}
    <aside className={`sidebar ${sidebarOpen ? 'sidebar-visible' : ''}`}>
      <a className="brand" href={demoMode ? import.meta.env.BASE_URL : "/"} aria-label="SupportPilot 首页"><span className="brand-icon"><Command size={21} /></span><span>SupportPilot<small>研发排查工作台</small></span></a>
      <button className="new-chat" disabled={locked} onClick={newChat}><Plus size={17} />发起新的排查<span>↗</span></button>
      <div className="nav-label"><History size={14} />历史会话<button className="icon-button" title="刷新会话列表" aria-label="刷新会话列表" disabled={locked || connecting} onClick={() => void refreshThreads().catch(cause => setError(messageOf(cause)))}><RefreshCw size={13} /></button></div>
      <label className="search-box"><Search size={14} /><input aria-label="搜索历史会话" placeholder="搜索会话" value={search} onChange={event => setSearch(event.target.value)} /><kbd>⌕</kbd></label>
      <nav className="thread-list" aria-label="历史会话">
        {connecting ? <p className="sidebar-hint">正在加载会话…</p> : !visibleThreads.length ? <p className="sidebar-hint">{search ? '没有匹配的会话' : '每一次排查，都会留有记录。\n发送问题后在这里继续。'}</p> : <>
          <span className="list-caption">最近的排查 · {visibleThreads.length}</span>
          {visibleThreads.map(thread => <button key={thread.thread_id} className={`thread ${thread.thread_id === threadId ? 'active' : ''}`} disabled={locked} onClick={() => void loadThread(thread.thread_id)}>
            <MessageSquare size={15} /><span><strong>{thread.title || '未命名会话'}</strong><small>{thread.updated_at ? new Date(thread.updated_at).toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' }) : '历史会话'}</small></span><ChevronRight size={13} />
          </button>)}
        </>}
      </nav>
      <div className="sidebar-bottom">
        <div className="scope-note"><ShieldCheck size={16} /><span>从证据出发，逐步定位<small>文档检索 · 多轮上下文</small></span></div>
        <button className="profile" onClick={() => setShowHelp(true)}><span className="avatar">研</span><span>{tenantMode ? '团队工作空间' : '个人工作空间'}<small>{tenantMode ? '本人会话 · 团队知识库' : '此浏览器的独立会话'}</small></span><CircleHelp size={16} /></button>
      </div>
    </aside>

    <main className="main-panel">
      <header className="topbar">
        <button className="icon-button mobile-menu" aria-label="打开会话列表" onClick={() => setSidebarOpen(true)}><Menu size={20} /></button>
        <div className="breadcrumb"><span>工作台</span><ChevronRight size={13} /><strong>研发问题排查</strong></div>
        <div className="header-actions"><span className={`connection ${connected ? 'online' : ''}`}><i />{connecting ? '连接中' : connected ? demoMode ? '演示已就绪' : '服务已连接' : '服务未连接'}</span><span className="header-divider" /><button className="icon-button" aria-label={evidenceOpen ? '收起证据面板' : '打开证据面板'} title="证据面板" onClick={() => setEvidenceOpen(!evidenceOpen)}>{evidenceOpen ? <PanelRightClose size={19} /> : <PanelRightOpen size={19} />}</button></div>
      </header>
      <div className="conversation-bar"><span className="conversation-title"><MessageSquare size={15} />{currentTitle}</span><button className="text-button" disabled={locked || !turns.length} onClick={() => void loadThread(threadId)}><RefreshCw size={13} />刷新历史</button></div>
      {error && <div className="banner error" role="alert"><span>{error}</span>{!connected ? <button disabled={connecting} onClick={() => setBootKey(key => key + 1)}>重新连接</button> : <button aria-label="关闭错误提示" onClick={() => setError('')}><X size={15} /></button>}</div>}
      {notice && <div className="banner" role="status">{notice}<button aria-label="关闭提示" onClick={() => setNotice('')}><X size={15} /></button></div>}
      <div className="chat-scroll" ref={scroll} onScroll={() => {
        if (!scroll.current) return;
        const nearBottom = scroll.current.scrollHeight - scroll.current.scrollTop - scroll.current.clientHeight < 100;
        follow.current = nearBottom; setScrolledUp(!nearBottom);
      }}>
        {historyLoading ? <div className="loading-history"><LoaderCircle className="spin" size={22} />正在读取会话历史…</div> : !turns.length ? <div className="welcome">
          <span className="welcome-mark"><Command size={31} /></span>
          <span className="eyebrow">YOUR DEBUGGING COMPANION</span>
          <h1>把问题说清楚，<br />让排查<span>有据可依。</span></h1>
          <p>梳理异常，查找文档，串联线索。<br />从一个问题开始，一起找到下一步。</p>
          <div className="suggestions">{suggestions.map(({ icon: Icon, title, text }) => <button key={title} disabled={locked} onClick={() => { setInput(text); composer.current?.focus(); }}><Icon size={19} /><strong>{title}</strong><span>{text}</span><ArrowRight className="suggestion-arrow" size={15} /></button>)}</div>
          <div className="welcome-foot"><span /><span>让事实成为每一步建议的起点</span><span /></div>
        </div> : <div className="messages">
          <div className="session-date"><span />本次排查<span /></div>
          {turns.map((turn, index) => {
            const answer = turn.messages.filter(message => message.type === 'ai' && message.content.trim()).map(message => message.content).join('\n\n');
            const turnTools = toolsFor(turn);
            const hitCount = evidenceFor(turn).length;
            const runId = turn.messages.find(message => message.run_id)?.run_id;
            return <section className="turn" key={turn.id}>
              <div className="question-row"><div className="question"><span>你 · 问题 {String(index + 1).padStart(2, '0')}</span><p>{turn.question}</p></div><span className="user-avatar">研</span></div>
              <div className="answer-row"><span className="assistant-avatar"><Command size={17} /></span><div className="answer-body">
                <div className="answer-label">SupportPilot <span>研发助手</span></div>
                {turnTools.length > 0 && <button className="tool-summary" onClick={() => { setSelected(turn.id); setEvidenceOpen(true); }}><Search size={13} />{turn.status === 'streaming' && turnTools.some(tool => !tool.result) ? '正在查找相关文档' : `工具调用 ${turnTools.length} 次 · ${hitCount} 条文档证据`}<ChevronRight size={13} /></button>}
                {turn.messages.filter(message => message.type === 'ai' && message.content.trim()).map((message, i) => <div className="markdown" key={i}><Markdown>{message.content}</Markdown></div>)}
                {turn.draft && <div className="markdown streaming-text"><Markdown>{turn.draft}</Markdown></div>}
                {turn.status === 'streaming' && <div className="stream-status" role="status"><span className="pulse-dots"><i /><i /><i /></span>{activity}</div>}
                {turn.error && <div className={`turn-error ${turn.status === 'stopped' ? 'stopped' : ''}`} role="alert">{turn.error}</div>}
                {turn.status === 'complete' && !answer && !turn.draft && <div className="turn-error">本轮没有返回文字回答，请刷新历史确认结果。</div>}
                {turn.status !== 'streaming' && (answer || turn.draft) && <div className="answer-footer"><CopyAnswer text={[answer, turn.draft].filter(Boolean).join('\n\n')} />{runId && <span title={runId}>运行 {runId.slice(0, 8)}</span>}{(tenantMode || demoMode) && runId && turn.status === 'complete' && <AnswerFeedback runId={runId} />}</div>}
              </div></div>
            </section>;
          })}
        </div>}
      </div>
      {scrolledUp && <button className="scroll-bottom" onClick={() => { follow.current = true; scroll.current?.scrollTo({ top: scroll.current.scrollHeight, behavior: 'smooth' }); }}><ArrowDown size={15} />最新消息</button>}
      <div className="composer-wrap"><form className="composer" onSubmit={event => void send(event)}>
        <textarea ref={composer} rows={2} aria-label="描述你的问题" placeholder="描述遇到的问题，也可以粘贴报错与日志…" value={input} disabled={historyLoading} maxLength={20000} onChange={event => setInput(event.target.value)} onKeyDown={onComposerKey} />
        <div className="composer-toolbar"><label className="model-select"><Sparkles size={14} /><select aria-label="选择模型" value={model} disabled={locked || !metadata} onChange={event => setModel(event.target.value)}>{!metadata && <option value="">默认模型</option>}{metadata?.models.map(name => <option key={name} value={name}>{name}</option>)}</select><ChevronDown size={12} /></label><div className="send-controls"><span>Enter 发送 · Shift + Enter 换行</span>{busy ? <button className="send-button stop-button" type="button" aria-label="停止接收" title="停止接收回答" onClick={() => active.current?.abort()}><Square size={15} fill="currentColor" /></button> : <button className="send-button" type="submit" aria-label="发送问题" disabled={!input.trim() || !connected || locked}><ArrowUp size={19} /></button>}</div></div>
      </form><p className="composer-note"><ShieldCheck size={11} />建议需结合实际环境验证，请勿发送密钥和未脱敏日志。</p></div>
    </main>

    {evidenceOpen && <aside className="evidence-panel">
      <div className="evidence-header"><div><BookOpen size={17} /><strong>证据与来源</strong></div><span className="count-badge">{evidence.length}</span><button className="icon-button evidence-mobile-close" aria-label="关闭证据面板" onClick={() => setEvidenceOpen(false)}><X size={18} /></button></div>
      <div className="evidence-scroll">
        <div className="evidence-intro"><span className="section-kicker">CONTEXT, NOT GUESSWORK</span><p>查看本轮检索到的资料，<br />让每一条建议都有迹可循。</p></div>
        {evidenceTurn && <div className="evidence-selection"><span>当前问题</span><p>{evidenceTurn.question}</p><small>点击回答中的工具记录可查看其他轮次。</small></div>}
        <div className="section-title"><span>工具记录</span><span>{tools.length ? `${tools.length} 次调用` : '等待调用'}</span></div>
        {tools.length ? <div className="tool-list">{tools.map(tool => <div className="tool-record" key={tool.id}><span className={`tool-state ${tool.result?.status || (evidenceTurn?.status === 'streaming' ? 'pending' : 'unknown')}`}>{!tool.result && evidenceTurn?.status === 'streaming' ? <LoaderCircle size={13} className="spin" /> : tool.result?.status === 'ok' ? <Check size={13} /> : <Search size={13} />}</span><div><strong>{tool.call?.name === 'search_feishu_documents' ? demoMode ? '文档检索（预录）' : '搜索飞书文档' : tool.call?.name || '文档检索结果'}</strong>{tool.call?.args.query != null && <p>{String(tool.call.args.query)}</p>}<small>{tool.result?.message || (evidenceTurn?.status === 'streaming' ? '正在等待结果…' : '未收到工具结果')}</small></div></div>)}</div> : <div className="quiet-card"><Search size={17} /><p>{evidenceTurn ? '本轮尚无工具记录。普通问答可能无需检索。' : '需要文档依据时，助手会检索已导入的知识库。'}</p></div>}
        <div className="section-title source-title"><span>文档片段</span><span>{evidence.length} 条证据</span></div>
        {evidence.length ? evidence.map((hit, index) => <article className="evidence-card" key={hit.chunk_id}>
          <div className="source-number"><span><FileText size={12} />来源 {String(index + 1).padStart(2, '0')}</span>{typeof hit.score === 'number' && Number.isFinite(hit.score) && <small title="向量相似度，不代表答案正确率">相似度 {hit.score.toFixed(3)}</small>}</div>
          <h3>{hit.title}</h3><p className="source-excerpt">{hit.content}</p>{hit.truncated && <small className="truncated">正文已由后端截断</small>}
          <div className="source-footer"><span title={hit.chunk_id}>片段 {hit.chunk_id.slice(0, 8)}</span>{safeUrl(hit.source_url) ? <a href={safeUrl(hit.source_url)} target="_blank" rel="noreferrer">查看原文<ExternalLink size={12} /></a> : <span>来源链接不可用</span>}</div>
        </article>) : <div className="empty-evidence"><span className="empty-document"><FileText size={30} /><Search size={17} /></span><strong>线索，会在这里汇集</strong><p>检索命中的文档片段与来源链接<br />将在这里展示。</p></div>}
      </div>
      <div className="evidence-foot"><ShieldCheck size={14} /><span>资料用于辅助判断，不能单独证明故障根因。</span></div>
    </aside>}

    {showHelp && <div className="modal-backdrop" onClick={() => setShowHelp(false)}><section role="dialog" aria-modal="true" aria-labelledby="help-title" className="help-modal" onClick={event => event.stopPropagation()}><button autoFocus className="icon-button modal-close" aria-label="关闭使用说明" onClick={() => setShowHelp(false)}><X size={20} /></button><span className="welcome-mark"><Command size={26} /></span><h2 id="help-title">你的研发排查工作台</h2><p>输入问题开始排查，左侧继续历史对话，右侧核对文档证据。</p><p>{demoMode ? '演示会话和反馈仅保存在当前标签页的浏览器存储中，不会发送到服务器。所有回答与引用均为预录示例。' : tenantMode ? '会话按团队和登录账号保存，仅本人可见；知识库由当前团队共享。切换团队后会打开对应的独立工作空间。' : '此浏览器保存匿名用户编号，会话内容由后端 checkpoint 保存。清除浏览器数据后，历史列表将使用新身份。'}</p><label className="identity-label">当前网页身份<code>{user}</code></label><p className="muted">{demoMode ? '请从首页三个预设案例开始，或继续左侧的示例会话。' : tenantMode ? '团队管理员可在顶部管理成员和导入知识库。' : '当前为本地工作台，共享 API 认证不提供完整的多用户权限隔离。'}</p><button className="primary-button" onClick={() => setShowHelp(false)}>开始排查<ArrowRight size={16} /></button></section></div>}
  </div>;
}
