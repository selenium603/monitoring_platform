"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useProject } from "@/components/providers/ProjectProvider";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { getTraceAnalytics, type TraceAnalyticsParams } from "@/lib/api/traces";
import { getTraceScoreSummary } from "@/lib/api/evaluations";
import type {
  AnalyticsBucket,
  TokenCostBucket,
  TopModel,
} from "@/lib/api/types";
import { AnalyticsMetric, AnalyticsGranularity } from "@/lib/api/enums";
import { queryKeys } from "@/lib/query/keys";
import { LoadingState } from "@/components/common/LoadingState";
import { ErrorState } from "@/components/common/ErrorState";
import { EmptyState } from "@/components/common/EmptyState";
import { formatNumber, formatCost } from "@/lib/utils/format";
import {
  Select,
  SelectTrigger,
  SelectContent,
  SelectItem,
  SelectValue,
} from "@/components/ui/Select";
import { DateTimePicker } from "@/components/common/DateTimePicker";
import { extractErrorMessage } from "@/lib/api/client";
import { labelFor, metricLabel } from "@/lib/utils/labels";

function BarChart({
  data,
  labelKey,
  valueKey,
  maxValue,
}: {
  data: Record<string, unknown>[];
  labelKey: string;
  valueKey: string;
  maxValue: number;
}) {
  return (
    <div className="space-y-1">
      {data.map((item, i) => {
        const value = Number(item[valueKey]) || 0;
        const width = maxValue > 0 ? (value / maxValue) * 100 : 0;
        return (
          <div key={i} className="flex items-center gap-2 text-xs font-mono">
            <span className="w-24 truncate text-text-dim">
              {String(item[labelKey])}
            </span>
            <div className="flex-1 h-4 bg-bg border border-border">
              <div
                className="h-full bg-accent/20 border-r border-accent/40"
                style={{ width: `${Math.max(width, 1)}%` }}
              />
            </div>
            <span className="w-16 text-right text-text-dim">
              {formatNumber(value)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export default function AnalyticsPage() {
  const { currentProject } = useProject();
  const projectId = currentProject?.id ?? "";

  useDocumentTitle("分析");

  const [metric, setMetric] = useState<string>(AnalyticsMetric.volume);
  const [granularity, setGranularity] = useState<string>(
    AnalyticsGranularity.day,
  );
  const [startDate, setStartDate] = useState(() =>
    new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString().slice(0, 16),
  );
  const [endDate, setEndDate] = useState(() =>
    new Date(Date.now()).toISOString().slice(0, 16),
  );

  const analyticsParams: TraceAnalyticsParams = {
    metric: metric as TraceAnalyticsParams["metric"],
    granularity: granularity as TraceAnalyticsParams["granularity"],
    started_after: new Date(startDate).toISOString(),
    started_before: new Date(endDate).toISOString(),
  };

  const analyticsQuery = useQuery({
    queryKey: queryKeys.analytics.traces(
      projectId,
      analyticsParams as unknown as Record<string, unknown>,
    ),
    queryFn: () => getTraceAnalytics(analyticsParams),
    enabled: !!currentProject,
  });

  const scoresQuery = useQuery({
    queryKey: queryKeys.analytics.scores(projectId, {
      date_from: new Date(startDate).toISOString(),
      date_to: new Date(endDate).toISOString(),
    }),
    queryFn: () =>
      getTraceScoreSummary({
        date_from: new Date(startDate).toISOString(),
        date_to: new Date(endDate).toISOString(),
      }),
    enabled: !!currentProject,
  });

  if (!currentProject) {
    return (
      <EmptyState
        title="请选择项目"
        description="选择一个项目以查看分析数据。"
      />
    );
  }

  const analyticsData = analyticsQuery.data;
  const scoreSummary = scoresQuery.data ?? [];

  return (
    <div className="space-y-6 animate-fade-in">
      <h1 className="text-lg font-mono text-primary">分析</h1>

      <div className="flex flex-wrap items-center gap-3">
        <Select value={metric} onValueChange={setMetric}>
          <SelectTrigger className="w-36">
            <SelectValue placeholder="指标" />
          </SelectTrigger>
          <SelectContent>
            {Object.values(AnalyticsMetric).map((m) => (
              <SelectItem key={m} value={m}>
                {labelFor(m)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={granularity} onValueChange={setGranularity}>
          <SelectTrigger className="w-28">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {Object.values(AnalyticsGranularity).map((g) => (
              <SelectItem key={g} value={g}>
                {labelFor(g)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <div className="flex items-center gap-1.5">
          <span className="text-xs text-text-muted font-mono">从</span>
          <DateTimePicker
            value={startDate}
            onChange={setStartDate}
            placeholder="开始时间"
          />
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-xs text-text-muted font-mono">到</span>
          <DateTimePicker
            value={endDate}
            onChange={setEndDate}
            placeholder="结束时间"
          />
        </div>
      </div>

      {analyticsQuery.isPending ? (
        <LoadingState />
      ) : analyticsQuery.error ? (
        <ErrorState
          message={extractErrorMessage(analyticsQuery.error)}
          onRetry={() => analyticsQuery.refetch()}
        />
      ) : !analyticsData || (analyticsData as unknown[]).length === 0 ? (
        <EmptyState
          title="暂无分析数据"
          description="记录 Trace 后，分析数据会显示在这里。"
        />
      ) : (
        <div className="space-y-6">
          {metric === "models" ? (
            <div className="border-engraved bg-surface p-4">
              <h2 className="text-xs font-mono text-text-muted uppercase tracking-wider mb-3">
                常用模型
              </h2>
              <div className="border border-border overflow-x-auto">
                <table className="w-full text-xs font-mono">
                  <thead>
                    <tr className="border-b border-border bg-surface-hi">
                      <th className="text-left px-3 py-2 text-text-muted font-normal">
                        模型
                      </th>
                      <th className="text-left px-3 py-2 text-text-muted font-normal">
                        调用次数
                      </th>
                      <th className="text-left px-3 py-2 text-text-muted font-normal">
                        Token
                      </th>
                      <th className="text-left px-3 py-2 text-text-muted font-normal">
                        成本
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {(analyticsData as TopModel[]).map((m) => (
                      <tr key={m.model} className="border-b border-border">
                        <td className="px-3 py-2 text-text">{m.model}</td>
                        <td className="px-3 py-2 text-text-dim">
                          {formatNumber(m.call_count)}
                        </td>
                        <td className="px-3 py-2 text-text-dim">
                          {formatNumber(m.total_tokens)}
                        </td>
                        <td className="px-3 py-2 text-text-dim">
                          {formatCost(m.total_cost)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : metric === "tokens" || metric === "cost" ? (
            <div className="border-engraved bg-surface p-4">
              <h2 className="text-xs font-mono text-text-muted uppercase tracking-wider mb-3">
                {metric === "tokens" ? "Token 用量" : "成本"}趋势
              </h2>
              <BarChart
                data={analyticsData as unknown as Record<string, unknown>[]}
                labelKey="bucket"
                valueKey={metric === "tokens" ? "total_tokens" : "total_cost"}
                maxValue={Math.max(
                  ...(analyticsData as TokenCostBucket[]).map((b) =>
                    metric === "tokens" ? b.total_tokens : b.total_cost,
                  ),
                )}
              />
            </div>
          ) : (
            <div className="border-engraved bg-surface p-4">
              <h2 className="text-xs font-mono text-text-muted uppercase tracking-wider mb-3">
                Trace {labelFor(metric)}趋势
              </h2>
              <BarChart
                data={analyticsData as unknown as Record<string, unknown>[]}
                labelKey="bucket"
                valueKey={
                  metric === "latency" ? "avg_latency_ms" : "trace_count"
                }
                maxValue={Math.max(
                  ...(analyticsData as AnalyticsBucket[]).map((b) =>
                    metric === "latency"
                      ? (b.avg_latency_ms ?? 0)
                      : b.trace_count,
                  ),
                )}
              />
            </div>
          )}

          {scoreSummary.length > 0 && (
            <div className="border-engraved bg-surface p-4">
              <h2 className="text-xs font-mono text-text-muted uppercase tracking-wider mb-3">
                分数汇总
              </h2>
              <div className="border border-border overflow-x-auto">
                <table className="w-full text-xs font-mono">
                  <thead>
                    <tr className="border-b border-border bg-surface-hi">
                      <th className="text-left px-3 py-2 text-text-muted font-normal">
                        指标
                      </th>
                      <th className="text-left px-3 py-2 text-text-muted font-normal">
                        平均
                      </th>
                      <th className="text-left px-3 py-2 text-text-muted font-normal">
                        最小
                      </th>
                      <th className="text-left px-3 py-2 text-text-muted font-normal">
                        最大
                      </th>
                      <th className="text-left px-3 py-2 text-text-muted font-normal">
                        成功
                      </th>
                      <th className="text-left px-3 py-2 text-text-muted font-normal">
                        失败
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {scoreSummary.map((s) => (
                      <tr
                        key={s.metric_name}
                        className="border-b border-border"
                      >
                        <td className="px-3 py-2 text-text">
                          {metricLabel(s.metric_name)}
                        </td>
                        <td className="px-3 py-2 text-text-dim">
                          {s.avg_score?.toFixed(2) ?? "—"}
                        </td>
                        <td className="px-3 py-2 text-text-dim">
                          {s.min_score?.toFixed(2) ?? "—"}
                        </td>
                        <td className="px-3 py-2 text-text-dim">
                          {s.max_score?.toFixed(2) ?? "—"}
                        </td>
                        <td className="px-3 py-2 text-success">
                          {s.success_count}
                        </td>
                        <td className="px-3 py-2 text-error">
                          {s.failed_count}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
