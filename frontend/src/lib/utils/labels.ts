const LABELS: Record<string, string> = {
  COMPLETED: "已完成",
  OK: "正常",
  SUCCESS: "成功",
  ACTIVE: "运行中",
  RUNNING: "执行中",
  PENDING: "等待中",
  UNSET: "未设置",
  ERROR: "错误",
  FAILED: "失败",
  PAUSED: "已暂停",
  PAST_DUE: "逾期",
  CANCELED: "已取消",
  INCOMPLETE: "未完成",
  AUTOMATED: "自动评估",
  ANNOTATION: "人工标注",
  PROGRAMMATIC: "程序提交",
  NUMERIC: "数值",
  BOOLEAN: "布尔值",
  CATEGORICAL: "分类",
  TRACE: "Trace",
  SESSION: "Session",
  volume: "调用量",
  errors: "错误数",
  latency: "延迟",
  cost: "成本",
  tokens: "Token 用量",
  models: "模型",
  hour: "小时",
  day: "天",
  week: "周",
  every_6h: "每 6 小时",
  daily: "每天",
  weekly: "每周",
  custom: "自定义",
};

const METRIC_LABELS: Record<string, string> = {
  task_completion: "任务完成度",
  tool_correctness: "工具调用正确性",
  argument_correctness: "参数正确性",
  coherence: "连贯性",
  confidence: "置信度",
  loop_detection: "循环检测",
  plan_adherence: "计划遵循度",
  plan_quality: "计划质量",
  step_efficiency: "步骤效率",
  agent_reliability: "Agent 可靠性",
  agent_consistency: "Agent 一致性",
};

export function labelFor(value: string | null | undefined): string {
  if (!value) return "—";
  return LABELS[value] ?? value;
}

export function metricLabel(value: string): string {
  return METRIC_LABELS[value] ?? value;
}

export function sourceLabel(value: string): string {
  return labelFor(value);
}

export function dataTypeLabel(value: string): string {
  return labelFor(value);
}

export function cadenceLabel(value: string): string {
  if (!value) return "—";
  if (value.startsWith("cron:")) {
    return `自定义：${value.slice("cron:".length).trim()}`;
  }
  return labelFor(value);
}

export function filterLabel(value: string): string {
  const labels: Record<string, string> = {
    date_from: "开始时间",
    date_to: "结束时间",
    status: "状态",
    session_id: "Session ID",
    user_id: "用户 ID",
    tags: "标签",
    name: "名称",
    has_error: "包含错误",
    min_trace_count: "最少 Trace 数",
    trace_ids: "Trace ID",
    retry_of: "重试自",
    signal_weights: "信号权重",
  };
  return labels[value] ?? value;
}
