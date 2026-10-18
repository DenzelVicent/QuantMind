import React from 'react';
import { clsx } from 'clsx';
import { Database } from 'lucide-react';
import type { QuantDBTrainingSource } from '../../features/admin/types';

interface Props {
  sources: QuantDBTrainingSource[];
  value: string;
  loading?: boolean;
  onChange: (id: string) => void;
  onManage?: () => void;
}

/**
 * 模型训练数据源三选一卡片（L1 / L2 / L1+L2 合并宽表）。
 * 点击即切换 factorSource，上层已有的 factorSource effect 会自动刷新
 * 特征清单、覆盖日期与目录版本，无需额外逻辑。
 */
export const TrainingDataSourceCards: React.FC<Props> = ({
  sources,
  value,
  loading = false,
  onChange,
  onManage,
}) => {
  if (loading && sources.length === 0) {
    return (
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {[0, 1, 2].map((i) => (
          <div key={i} className="rounded-2xl border border-slate-200 bg-slate-50 p-4 animate-pulse min-h-[118px]" />
        ))}
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center gap-2 mb-2.5">
        <Database size={15} className="text-indigo-500" />
        <span className="text-sm font-bold text-slate-800">因子数据源</span>
        <span className="text-[11px] text-slate-400">决定可用因子清单、覆盖起点与默认勾选</span>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {sources.map((item) => {
          const active = item.id === value;
          const selectable = item.trainable;
          const versionShort = item.catalog_version ? item.catalog_version.slice(0, 8) : null;
          return (
            <button
              key={item.id}
              type="button"
              disabled={!selectable}
              onClick={() => onChange(item.id)}
              className={clsx(
                'text-left rounded-2xl border p-4 transition-all min-h-[118px] flex flex-col gap-1',
                active
                  ? 'border-blue-500 ring-2 ring-blue-100 bg-blue-50/40 shadow-sm'
                  : 'border-slate-200 bg-white hover:border-blue-300 hover:shadow-sm',
                !selectable && 'opacity-70 cursor-not-allowed hover:border-slate-200 hover:shadow-none',
              )}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-bold text-slate-800">{item.name}</span>
                <span className="flex items-center gap-1.5 shrink-0">
                  {item.default && (
                    <span className="px-1.5 py-0.5 rounded-md bg-slate-100 text-slate-500 text-[10px] font-bold">默认</span>
                  )}
                  {active ? (
                    <span className="px-1.5 py-0.5 rounded-md bg-blue-600 text-white text-[10px] font-bold">当前</span>
                  ) : selectable ? null : (
                    <span className="px-1.5 py-0.5 rounded-md bg-amber-100 text-amber-700 text-[10px] font-bold">不可用</span>
                  )}
                  {active && item.published && (
                    <span className="px-1.5 py-0.5 rounded-md bg-blue-50 border border-blue-200 text-blue-600 text-[10px] font-bold">
                      平台默认
                    </span>
                  )}
                </span>
              </div>
              {item.min_date && item.max_date ? (
                <div className="text-[11px] text-slate-500">
                  覆盖：<span className="font-mono text-slate-700">{item.min_date} ~ {item.max_date}</span>
                </div>
              ) : (
                <div className="text-[11px] text-slate-400">覆盖：--</div>
              )}
              <div className="text-[11px] text-slate-500">
                目录因子 <span className="font-mono font-bold text-slate-700">{item.feature_count ?? 0}</span> 个
                · 默认勾选 <span className="font-mono font-bold text-slate-700">{item.default_selected_count ?? 0}</span> 个
              </div>
              <div className="text-[11px] text-slate-400 truncate">
                {versionShort ? (
                  <>版本 <span className="font-mono">{versionShort}…</span></>
                ) : (
                  <span className="text-amber-600">{item.reason || '尚未发布因子目录'}</span>
                )}
              </div>
              {!selectable && item.reason && versionShort && (
                <div className="text-[11px] text-amber-600 truncate">{item.reason}</div>
              )}
            </button>
          );
        })}
      </div>
      <div className="mt-2 text-[11px] text-slate-400">
        切换数据源后特征清单与覆盖日期将刷新
        {onManage && (
          <>
            {' · '}
            <button type="button" onClick={onManage} className="text-blue-600 hover:underline font-medium">
              前往数据发布管理 →
            </button>
          </>
        )}
      </div>
    </div>
  );
};
