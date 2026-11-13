/** 评测工作台：选案例 → 启动 → 轮询进度 → 逐轮归因 → 展开该轮的阶段记录。 */
import { useEffect, useMemo, useState } from 'react';
import {
  STAGE_LABEL,
  TERMINAL,
  type EvalCase,
  type EvalRow,
  type EvalRun,
  type EvalStage,
  fetchEvalCases,
  fetchEvalRun,
  fetchEvalRuns,
  fetchStages,
  startEval,
} from './eval-api';
import './eval.css';

const STATUS_TEXT: Record<EvalRow['status'], string> = {
  passed: '通过',
  failed: '未通过',
  error: '执行错误',
};
const STATUS_CLASS: Record<EvalRow['status'], string> = {
  passed: 'ok',
  failed: 'bad',
  error: 'bad',
};

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function stageClass(stage: string | undefined): string {
  if (!stage) return 'mute';
  if (stage === 'none') return 'ok';
  if (stage === 'unattributed') return 'warn';
  return 'bad';
}

function decisionsText(decisions: Record<string, unknown>): string {
  const parts = Object.entries(decisions).map(([key, value]) => `${key}=${String(value)}`);
  return parts.join(' · ');
}

export default function EvalPage() {
  const [cases, setCases] = useState<EvalCase[]>([]);
  const [suite, setSuite] = useState('');
  const [selected, setSelected] = useState<string[]>([]);
  const [job, setJob] = useState<EvalRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [openRunId, setOpenRunId] = useState('');
  const [stages, setStages] = useState<Record<string, EvalStage[]>>({});

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const list = await fetchEvalCases();
        if (!alive) return;
        setCases(list.cases);
        setSuite(`${list.suite_id} · ${list.version}`);
        setSelected(list.cases.map((item) => item.id));
      } catch (cause) {
        if (alive) setError(describe(cause));
      }
      try {
        const recent = await fetchEvalRuns();
        if (alive && recent.jobs.length > 0) setJob(recent.jobs[0]);
      } catch {
        // 没有历史任务是正常状态，不当作错误提示。
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  // 轮询：每次拿到新对象就会重新安排下一次，进入终态后自然停止。
  useEffect(() => {
    if (!job || TERMINAL.has(job.status)) return undefined;
    const timer = window.setTimeout(() => {
      void fetchEvalRun(job.job_id).then(setJob).catch(() => undefined);
    }, 1200);
    return () => window.clearTimeout(timer);
  }, [job]);

  const running = busy || (job !== null && !TERMINAL.has(job.status));
  const selectedText = useMemo(
    () => `${selected.length}/${cases.length}`,
    [selected.length, cases.length],
  );

  function toggleCase(id: string) {
    setSelected((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]));
  }

  async function run() {
    setBusy(true);
    setError('');
    setOpenRunId('');
    try {
      setJob(await startEval({ case_ids: selected }));
    } catch (cause) {
      setError(describe(cause));
    } finally {
      setBusy(false);
    }
  }

  async function openStages(row: EvalRow) {
    const runId = row.run_id;
    if (!runId) return;
    if (openRunId === runId) {
      setOpenRunId('');
      return;
    }
    setOpenRunId(runId);
    if (stages[runId]) return;
    try {
      const body = await fetchStages(runId);
      setStages((prev) => ({ ...prev, [runId]: body.stages }));
    } catch {
      setStages((prev) => ({ ...prev, [runId]: [] }));
    }
  }

  const rows = job?.rows ?? [];
  const done = job ? `${job.recorded_turns}/${job.total_turns}` : '0/0';
  const passRate =
    job && job.passed_cases !== null ? `${job.passed_cases}/${job.total_cases}` : '—';
  const failures = Object.entries(job?.stage_failures ?? {}).sort((a, b) => b[1] - a[1]);
  const worst = failures.length > 0 ? failures[0][1] : 0;

  return (
    <div className="eval-page">
      <header className="eval-head">
        <div>
          <h1>评测与归因</h1>
          <p className="eval-sub">
            与 CLI、飞书后台共用同一个 run_evaluation，跑的是同一个 dev-issue-agent。失败按
            route → retrieve → evidence → generate → verify 归因到最早出错的一步。
          </p>
        </div>
        <div className="eval-actions">
          <a href="/supportpilot">← 返回工作台</a>
        </div>
      </header>

      <div className="eval-grid">
        <section className="eval-card">
          <h2>1 · 选择案例</h2>
          <div className="eval-cases">
            {cases.map((item) => (
              <label key={item.id}>
                <input
                  type="checkbox"
                  checked={selected.includes(item.id)}
                  onChange={() => toggleCase(item.id)}
                  disabled={running}
                />
                <span>
                  <span className="eval-case-id">{item.id}</span>
                  <br />
                  <span className="eval-case-title">
                    {item.title} · {item.turns} 轮
                  </span>
                </span>
              </label>
            ))}
            {cases.length === 0 && <p className="eval-empty">正在读取案例集…</p>}
          </div>
          <div className="eval-actions">
            <button type="button" onClick={() => setSelected(cases.map((item) => item.id))} disabled={running}>
              全选
            </button>
            <button type="button" onClick={() => setSelected([])} disabled={running}>
              清空
            </button>
          </div>
          <p className="eval-sub">{suite} · 已选 {selectedText}</p>
          <div className="eval-actions">
            <button
              type="button"
              className="primary"
              onClick={() => void run()}
              disabled={running || selected.length === 0}
            >
              {running ? '评测执行中…' : '运行评测'}
            </button>
          </div>
          {error && <p className="eval-error">{error}</p>}
          {job?.error && <p className="eval-error">任务失败：{job.error}</p>}
        </section>

        <section className="eval-card">
          <h2>2 · 逐轮结果（点击一行看该轮的阶段记录）</h2>
          {rows.length === 0 && <p className="eval-empty">还没有结果。启动一次评测后，这里逐轮出现。</p>}
          {rows.length > 0 && (
            <table className="eval-rows">
              <thead>
                <tr>
                  <th>案例</th>
                  <th>轮</th>
                  <th>状态</th>
                  <th>归因</th>
                  <th>耗时</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const key = `${row.case_id}-${row.turn_index}`;
                  const stage = row.attribution?.stage;
                  return [
                    <tr
                      key={key}
                      className="eval-row"
                      onClick={() => void openStages(row)}
                      title={row.run_id ? `run_id ${row.run_id}` : '本轮没有 run_id'}
                    >
                      <td>
                        <span className="eval-case-id">{row.case_id}</span>
                        <div className="eval-reason">{row.title}</div>
                      </td>
                      <td>{row.turn_index}</td>
                      <td>
                        <span className={`eval-chip ${STATUS_CLASS[row.status]}`}>
                          {STATUS_TEXT[row.status]}
                        </span>
                      </td>
                      <td>
                        <span className={`eval-chip ${stageClass(stage)}`}>
                          {STAGE_LABEL[stage ?? ''] ?? stage ?? '未归因'}
                        </span>
                        {row.attribution?.reason && (
                          <div className="eval-reason">{row.attribution.reason}</div>
                        )}
                      </td>
                      <td className="eval-mono">{row.duration_seconds.toFixed(1)}s</td>
                    </tr>,
                    openRunId === row.run_id && row.run_id ? (
                      <tr key={`${key}-stages`}>
                        <td colSpan={5}>
                          <div className="eval-stages">
                            {(stages[row.run_id] ?? []).length === 0 && (
                              <p className="eval-empty">
                                这一轮没有阶段记录（服务可能重启过，记录只在内存里保留最近 200 次运行）。
                              </p>
                            )}
                            {(stages[row.run_id] ?? []).map((item, index) => (
                              <div className="eval-stage-line" key={`${item.stage}-${index}`}>
                                <span className={`eval-chip ${stageClass(item.stage)}`}>
                                  {STAGE_LABEL[item.stage] ?? item.stage}
                                </span>
                                <span>{item.outcome}</span>
                                <span className="eval-decisions">{decisionsText(item.decisions)}</span>
                                <span className="eval-mono">{item.duration_seconds.toFixed(2)}s</span>
                              </div>
                            ))}
                          </div>
                        </td>
                      </tr>
                    ) : null,
                  ];
                })}
              </tbody>
            </table>
          )}
        </section>

        <section className="eval-card">
          <h2>3 · 汇总</h2>
          <div className="eval-metric">
            <span>状态</span>
            <span className={`eval-chip ${job?.status === 'succeeded' ? 'ok' : job?.status === 'failed' ? 'bad' : 'mute'}`}>
              {job?.status ?? '未开始'}
            </span>
          </div>
          <div className="eval-metric">
            <span>规则通过案例</span>
            <strong>{passRate}</strong>
          </div>
          <div className="eval-metric">
            <span>已记录轮次</span>
            <strong>{done}</strong>
          </div>
          <div className="eval-bar" aria-hidden="true">
            <span
              style={{
                width: job && job.total_turns > 0 ? `${(job.recorded_turns / job.total_turns) * 100}%` : '0%',
                background: '#10693a',
              }}
            />
          </div>
          <p className="eval-sub">
            规则通过率不等于答案正确率；rubric 仍待人工复核，JSONL 里是 pending。
          </p>

          <h2 style={{ marginTop: 18 }}>失败阶段分布</h2>
          {failures.length === 0 && <p className="eval-empty">还没有失败轮次。</p>}
          {failures.map(([stage, count]) => (
            <div key={stage} style={{ marginBottom: 8 }}>
              <div className="eval-metric">
                <span>{STAGE_LABEL[stage] ?? stage}</span>
                <span className="eval-mono">{count}</span>
              </div>
              <div className="eval-bar" aria-hidden="true">
                <span style={{ width: worst > 0 ? `${(count / worst) * 100}%` : '0%' }} />
              </div>
            </div>
          ))}
          {job?.run_dir && <p className="eval-sub">结果目录：{job.run_dir}</p>}
          {job?.requested_model && <p className="eval-sub">请求模型：{job.requested_model}</p>}
          {job?.started_at && <p className="eval-sub">开始：{job.started_at}</p>}
        </section>
      </div>
    </div>
  );
}
