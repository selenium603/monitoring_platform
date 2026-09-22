"use client";

import type { MonitorResponse } from "@/lib/api/types";
import { StatusBadge } from "@/components/common/StatusBadge";
import { Badge } from "@/components/ui/Badge";
import { formatRelativeTime } from "@/lib/utils/format";
import { cadenceLabel, labelFor, metricLabel } from "@/lib/utils/labels";

interface MonitorTableProps {
  monitors: MonitorResponse[];
  onSelect?: (monitor: MonitorResponse) => void;
}

export function MonitorTable({ monitors, onSelect }: MonitorTableProps) {
  return (
    <div className="border border-border">
      <table className="w-full text-xs font-mono">
        <thead className="sticky top-0 z-10 bg-surface-hi">
          <tr className="border-b border-border">
            <th className="text-left px-3 py-2 text-text-muted font-normal">
              名称 / ID
            </th>
            <th className="text-left px-3 py-2 text-text-muted font-normal">
              状态
            </th>
            <th className="text-left px-3 py-2 text-text-muted font-normal">
              目标
            </th>
            <th className="text-left px-3 py-2 text-text-muted font-normal">
              周期
            </th>
            <th className="text-left px-3 py-2 text-text-muted font-normal">
              指标
            </th>
            <th className="text-left px-3 py-2 text-text-muted font-normal">
              采样比例
            </th>
            <th className="text-left px-3 py-2 text-text-muted font-normal">
              上次运行
            </th>
            <th className="text-left px-3 py-2 text-text-muted font-normal">
              下次运行
            </th>
          </tr>
        </thead>
        <tbody>
          {monitors.map((m) => (
            <tr
              key={m.id}
              className="border-b border-border hover:bg-surface-hi transition-colors cursor-pointer"
              onClick={() => onSelect?.(m)}
            >
              <td className="px-3 py-2 max-w-[220px] truncate text-text">
                {m.name || m.id.slice(0, 8)}
                <span className="ml-2 text-[10px] text-text-muted" title={m.id}>
                  {m.id.slice(0, 8)}
                </span>
              </td>
              <td className="px-3 py-2">
                <StatusBadge status={m.status} />
              </td>
              <td className="px-3 py-2 text-text-dim">
                {labelFor(m.target_type)}
              </td>
              <td
                className="px-3 py-2 text-text-dim max-w-[180px] truncate"
                title={m.cadence}
              >
                {cadenceLabel(m.cadence)}
              </td>
              <td className="px-3 py-2">
                <div className="flex gap-1 flex-wrap">
                  {m.metric_names.slice(0, 3).map((name) => (
                    <Badge key={name} variant="info">
                      {metricLabel(name)}
                    </Badge>
                  ))}
                  {m.metric_names.length > 3 && (
                    <span className="text-[10px] text-text-muted self-center">
                      +{m.metric_names.length - 3}
                    </span>
                  )}
                </div>
              </td>
              <td className="px-3 py-2 text-text-dim">
                {formatSamplingRate(m.sampling_rate)}
              </td>
              <td className="px-3 py-2 text-text-dim">
                {m.last_run_at ? (
                  formatRelativeTime(m.last_run_at)
                ) : (
                  <span className="text-text-muted">—</span>
                )}
              </td>
              <td className="px-3 py-2 text-text-dim">
                {m.next_run_at ? (
                  formatRelativeTime(m.next_run_at)
                ) : (
                  <span className="text-text-muted">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function formatSamplingRate(rate: number): string {
  if (rate == null || Number.isNaN(rate)) return "—";
  if (rate >= 1) return "100%";
  return `${Math.round(rate * 100)}%`;
}
