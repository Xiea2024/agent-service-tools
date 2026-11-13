import { ArrowLeft, ArrowUpRight, Command, FlaskConical, Github } from 'lucide-react';
import { githubUrl } from './blog-api';
import './store.css';

interface Entry {
  href: string;
  name: string;
  kind: string;
  description?: string;
  tags: string[];
  icon: typeof Command;
}

const ENTRIES: Entry[] = [
  {
    href: '/supportpilot',
    name: 'SupportPilot',
    kind: '研发问题排查',
    description:
      '多轮对话加文档检索。回答引用实际命中的片段与来源链接，工具调用和证据可以逐轮展开核对。',
    tags: ['飞书 · 网页', '文档 RAG', '多轮会话'],
    icon: Command,
  },
  {
    href: '/eval',
    name: '评测与归因',
    kind: '回归与定位',
    tags: ['固定案例', '阶段归因', '回归门禁'],
    icon: FlaskConical,
  },
];

/** 旧页面共用的站点头；博客已下线，导航只剩工作台入口。 */
export function SiteHeader({ blog = false }: { blog?: boolean }) {
  return (
    <header className="site-header">
      <a className="site-brand" href="/" aria-label="返回首页">
        <span className="brand-orbit">✳</span>XIEA<span className="brand-period">.</span>
      </a>
      <nav aria-label="主导航">
        <a className={!blog ? 'current' : ''} href="/">首页</a>
        <a href="/supportpilot">SupportPilot <ArrowUpRight size={13} /></a>
        <a href="/eval">评测 <ArrowUpRight size={13} /></a>
      </nav>
      <a className="github-link" href={githubUrl} target="_blank" rel="noreferrer" aria-label="GitHub">
        <Github size={20} /><span>GitHub</span><ArrowUpRight size={13} />
      </a>
    </header>
  );
}

export function SiteFooter() {
  return (
    <footer className="site-footer">
      <span>© {new Date().getFullYear()} XIEA</span>
      <a href={githubUrl} target="_blank" rel="noreferrer" aria-label="访问 GitHub 主页"><Github size={17} /></a>
    </footer>
  );
}

export function BackHome() {
  return <a className="back-home" href="/"><ArrowLeft size={14} /> 首页</a>;
}

export default function Portal() {
  return (
    <div className="store">
      <header className="store-header">
        <div className="store-header-inner">
          <a className="store-brand" href="/" aria-label="XIEA 首页">
            <span className="store-mark">✳</span>XIEA
          </a>
          <nav className="store-nav" aria-label="主导航">
            <a href="https://github.com/Xiea2024/agent-service-tools" target="_blank" rel="noreferrer">
              <Github size={15} />源码
            </a>
            <a href={githubUrl} target="_blank" rel="noreferrer">
              GitHub <ArrowUpRight size={13} />
            </a>
          </nav>
        </div>
      </header>

      <main className="store-main">
        <h1 className="store-label">AGENTS TOOLS</h1>

        <div className="store-grid">
          {ENTRIES.map(({ href, name, kind, description, tags, icon: Icon }) => (
            <a className="store-card" href={href} key={href}>
              <div className="store-card-top">
                <span className="store-icon"><Icon size={19} /></span>
                <span className="store-card-title">
                  <h2>{name}</h2>
                  <span>{kind}</span>
                </span>
                <ArrowUpRight className="store-arrow" size={17} />
              </div>
              {description && <p>{description}</p>}
              <div className="store-tags">
                {tags.map(tag => <span className="store-tag" key={tag}>{tag}</span>)}
              </div>
            </a>
          ))}
        </div>
      </main>

      <footer className="store-foot">
        <div className="store-foot-inner">
          <span>© {new Date().getFullYear()} XIEA</span>
          <a href={githubUrl} target="_blank" rel="noreferrer">
            <Github size={14} />GitHub
          </a>
        </div>
      </footer>
    </div>
  );
}
