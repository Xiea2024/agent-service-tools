/** 评测页面的接口封装；后端返回的字段与 evaluation.run_evaluation 的 JSONL 对齐。 */
import { request } from './api';

export interface EvalCase {
  id: string;
  title: string;
  category: string;
  turns: number;
}

export interface EvalCaseList {
  suite_id: string;
  version: string;
  cases: EvalCase[];
}

export interface EvalAttribution {
  stage: string;
  reason: string;
  failed_checks: string[];
  variant: string | null;
}

export interface EvalRow {
  case_id: string;
  title: string;
  turn_index: number;
  status: 'passed' | 'failed' | 'error';
  duration_seconds: number;
  run_id: string | null;
  answer: string | null;
  attribution: EvalAttribution | null;
  failed_checks: string[];
}

export interface EvalRun {
  job_id: string;
  status: 'queued' | 'running' | 'succeeded' | 'failed';
  run_dir: string | null;
  total_cases: number;
  total_turns: number;
  recorded_turns: number;
  passed_cases: number | null;
  stage_failures: Record<string, number> | null;
  requested_model: string | null;
  started_at: string;
  finished_at: string | null;
  error: string | null;
  rows: EvalRow[];
}

export interface EvalStage {
  stage: string;
  outcome: string;
  duration_seconds: number;
  variant: string;
  decisions: Record<string, unknown>;
}

export function fetchEvalCases() {
  return request<EvalCaseList>('/eval/cases');
}

export function fetchEvalRuns() {
  return request<{ jobs: EvalRun[] }>('/eval/runs');
}

export function fetchEvalRun(jobId: string) {
  return request<EvalRun>(`/eval/runs/${jobId}`);
}

export function startEval(input: { case_ids: string[]; model?: string; timeout?: number }) {
  return request<EvalRun>('/eval/runs', { method: 'POST', body: JSON.stringify(input) });
}

/** 阶段记录与指标同名，失败时能直接落到具体一步。 */
export function fetchStages(runId: string) {
  return request<{ run_id: string; stages: EvalStage[] }>(`/runs/${runId}/stages`);
}

export const TERMINAL = new Set<EvalRun['status']>(['succeeded', 'failed']);

export const STAGE_LABEL: Record<string, string> = {
  route: '委派',
  memory: '记忆',
  retrieve: '检索',
  evidence: '引用',
  generate: '生成',
  verify: '自检',
  none: '通过',
  unattributed: '无法归因',
};
