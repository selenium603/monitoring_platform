"use client";

import Link from "next/link";
import { ListTree, Layers, Radio, Gauge } from "lucide-react";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { useProjectPath } from "@/hooks/useNavigation";

export default function EvaluationsPage() {
  const basePath = useProjectPath();

  useDocumentTitle("评估");

  const sections = [
    {
      title: "Trace 评估运行",
      description: "针对单条 Trace 执行评估指标",
      href: basePath + "/evaluations/trace-runs",
      icon: <ListTree className="h-5 w-5" />,
    },
    {
      title: "Session 评估运行",
      description: "针对整个 Session 执行评估指标",
      href: basePath + "/evaluations/session-runs",
      icon: <Layers className="h-5 w-5" />,
    },
    {
      title: "评估监控",
      description: "按周期自动执行评估",
      href: basePath + "/evaluations/monitors",
      icon: <Radio className="h-5 w-5" />,
    },
    {
      title: "Trace 分数",
      description: "浏览并筛选不同运行、指标和环境产生的 Trace 分数",
      href: basePath + "/evaluations/trace-scores",
      icon: <Gauge className="h-5 w-5" />,
    },
    {
      title: "Session 分数",
      description: "浏览并筛选不同运行和指标产生的 Session 分数",
      href: basePath + "/evaluations/session-scores",
      icon: <Gauge className="h-5 w-5" />,
    },
  ];

  return (
    <div className="space-y-6 animate-fade-in">
      <h1 className="text-lg font-mono text-primary">评估</h1>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {sections.map((s) => (
          <Link
            key={s.href}
            href={s.href}
            className="border-engraved bg-surface p-5 hover:bg-surface-hi transition-colors"
          >
            <div className="text-text-muted mb-3">{s.icon}</div>
            <h2 className="text-sm font-mono text-text mb-1">{s.title}</h2>
            <p className="text-xs text-text-dim">{s.description}</p>
          </Link>
        ))}
      </div>
    </div>
  );
}
