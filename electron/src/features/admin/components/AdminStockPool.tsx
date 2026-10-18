/**
 * 后台管理 - 全局股票池（v2：TXT 即事实源，保存即生效）
 *
 * 股票池是回测 / 训练 / 推理 / 模拟盘 / 实盘共用的唯一事实源。
 * 每个池的成员是一个前缀式一行一个的 TXT（/data/stock_pool/<code>.txt）。
 * 本页负责「写」：建池、上传解析导入、维护成员（保存即生效，无发布步骤）。
 * 读侧统一由后端 PoolResolver 提供（本页「解析调试」可直接验证）。
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
    Alert,
    Button,
    Card,
    Col,
    Descriptions,
    Divider,
    Drawer,
    Empty,
    Form,
    Input,
    Modal,
    Popconfirm,
    Row,
    Select,
    Space,
    Statistic,
    Switch,
    Table,
    Tabs,
    Tag,
    Tooltip,
    Typography,
    Upload,
    message,
} from 'antd';
import {
    CloudUploadOutlined,
    DeleteOutlined,
    ExperimentOutlined,
    ExportOutlined,
    FileSearchOutlined,
    InboxOutlined,
    PlusOutlined,
    ReloadOutlined,
    SearchOutlined,
    WarningOutlined,
} from '@ant-design/icons';
import stockPoolService, {
    ParseReport,
    ParseRow,
    PoolMeta,
    PoolResolveResult,
    StockPool,
} from '../services/stockPoolService';

const { Text, Paragraph } = Typography;

const MATCH_TYPE_LABEL: Record<string, string> = {
    code: '纯代码',
    symbol: '后缀式',
    prefix: '前缀式',
    exchange_fixed: '交易所已纠正',
    name: '名称',
    name_loose: '名称(去ST)',
    none: '—',
};

const PARSE_STATUS_META: Record<string, { color: string; label: string }> = {
    matched: { color: 'green', label: '已匹配' },
    not_in_index: { color: 'orange', label: '不在索引' },
    unrecognized: { color: 'red', label: '无法识别' },
};

const TARGET_TYPE_LABEL: Record<string, string> = {
    strategy: '策略',
    training: '模型',
    simulation: '模拟盘账户',
    live: '实盘配置',
    factor: '因子',
    backtest: '回测（一次性）',
    inference: '推理（一次性）',
};

const POOL_TYPE_LABEL: Record<string, string> = {
    system_index: '指数成分',
    static: '静态维护',
    imported: '文件导入',
};

const STATUS_COLOR: Record<string, string> = {
    active: 'green',
    archived: 'default',
};

const SOURCE_LABEL: Record<string, string> = {
    pool: '库内池',
    builtin: '内置池',
    inline: '内联列表',
    file: '本地文件',
    all: '不过滤（全市场）',
    missing: '未找到',
    unsupported: '需上游解析',
    unresolved: '解析失败',
};

interface PoolFilters {
    market?: string;
    pool_type?: string;
    status?: string;
    keyword?: string;
}

const AdminStockPool: React.FC = () => {
    const [meta, setMeta] = useState<PoolMeta | null>(null);
    const [loading, setLoading] = useState(false);
    const [items, setItems] = useState<StockPool[]>([]);
    const [total, setTotal] = useState(0);
    const [page, setPage] = useState(1);
    const [pageSize, setPageSize] = useState(20);

    const [filters, setFilters] = useState<PoolFilters>({});
    const patchFilters = (patch: PoolFilters) => {
        setPage(1);
        setFilters({ ...filters, ...patch });
    };

    // 新建
    const [createOpen, setCreateOpen] = useState(false);
    const [createForm] = Form.useForm();

    // 详情 / 成员
    const [detail, setDetail] = useState<StockPool | null>(null);
    const [detailOpen, setDetailOpen] = useState(false);
    const [members, setMembers] = useState<string[]>([]);
    const [memberLoading, setMemberLoading] = useState(false);
    const [memberDraft, setMemberDraft] = useState('');
    const [savingMembers, setSavingMembers] = useState(false);

    // 解析调试
    const [resolveOpen, setResolveOpen] = useState(false);
    const [resolveRef, setResolveRef] = useState('pool:csi300');
    const [resolveResult, setResolveResult] = useState<PoolResolveResult | null>(null);
    const [resolving, setResolving] = useState(false);

    // 健康
    const [health, setHealth] = useState<any>(null);

    // 上传解析导入
    const [parseFileName, setParseFileName] = useState<string>('');
    const [parseBase64, setParseBase64] = useState<string>('');
    const [parseText, setParseText] = useState<string>('');
    const [parseFmt, setParseFmt] = useState<'auto' | 'csv' | 'txt'>('auto');
    const [parseHasHeader, setParseHasHeader] = useState(true);
    const [parseColumn, setParseColumn] = useState<string>('');
    const [parseRunning, setParseRunning] = useState(false);
    const [report, setReport] = useState<ParseReport | null>(null);
    const [selectedRows, setSelectedRows] = useState<number[]>([]);
    const [poolCode, setPoolCode] = useState('');
    const [poolName, setPoolName] = useState('');
    const [poolDesc, setPoolDesc] = useState('');
    const [creating, setCreating] = useState(false);

    // 引用
    const [usages, setUsages] = useState<any[]>([]);
    const [usagesLoading, setUsagesLoading] = useState(false);
    const [bindingTargetType, setBindingTargetType] = useState('strategy');
    const [bindingTargetId, setBindingTargetId] = useState('');
    const [bindingBusy, setBindingBusy] = useState(false);

    const loadMeta = useCallback(async () => {
        try {
            setMeta(await stockPoolService.getMeta());
        } catch (e: any) {
            message.error(`加载元信息失败: ${e?.message || e}`);
        }
    }, []);

    const loadPools = useCallback(async () => {
        setLoading(true);
        try {
            const res = await stockPoolService.listPools({
                ...filters,
                limit: pageSize,
                offset: (page - 1) * pageSize,
            });
            setItems(res.items || []);
            setTotal(res.total || 0);
        } catch (e: any) {
            message.error(`加载股票池失败: ${e?.message || e}`);
        } finally {
            setLoading(false);
        }
    }, [filters, page, pageSize]);

    const loadHealth = useCallback(async () => {
        try {
            setHealth(await stockPoolService.health());
        } catch {
            setHealth(null);
        }
    }, []);

    useEffect(() => {
        loadMeta();
    }, [loadMeta]);

    useEffect(() => {
        loadPools();
    }, [loadPools]);

    const handleCreate = async () => {
        try {
            const values = await createForm.validateFields();
            await stockPoolService.createPool({
                code: values.code,
                name: values.name,
                description: values.description,
                market: values.market,
                pool_type: values.pool_type,
                scope: 'global',
            });
            message.success('股票池已创建，请「成员」维护成分；保存即生效。');
            setCreateOpen(false);
            createForm.resetFields();
            loadPools();
        } catch (e: any) {
            if (e?.errorFields) return;
            message.error(`创建失败: ${e?.response?.data?.detail || e?.message || e}`);
        }
    };

    const loadMembers = useCallback(async (poolId: string) => {
        setMemberLoading(true);
        try {
            const res = await stockPoolService.getMembers(poolId);
            const list = res.symbols || [];
            setMembers(list);
            setMemberDraft(list.join('\n'));
        } catch (e: any) {
            message.error(`加载成员失败: ${e?.response?.data?.detail || e?.message || e}`);
        } finally {
            setMemberLoading(false);
        }
    }, []);

    const openDetail = async (pool: StockPool) => {
        try {
            const data = await stockPoolService.getPool(pool.pool_id);
            setDetail(data);
            setDetailOpen(true);
            setBindingTargetId('');
            void loadMembers(pool.pool_id);
            void loadUsages(pool.pool_id);
        } catch (e: any) {
            message.error(`加载详情失败: ${e?.response?.data?.detail || e?.message || e}`);
        }
    };

    const refreshDetail = async (poolId: string) => {
        const refreshed = await stockPoolService.getPool(poolId);
        setDetail(refreshed);
    };

    const handleSaveMembers = async () => {
        if (!detail) return;
        const symbols = memberDraft
            .split(/[\n,;]+/)
            .map((s) => s.trim())
            .filter((s) => s && !s.startsWith('#'));
        if (!symbols.length) {
            message.warning('成员为空；如确要清空请谨慎（空池会让消费方显式失败）');
            return;
        }
        setSavingMembers(true);
        try {
            const res = await stockPoolService.saveMembers(detail.pool_id, { symbols });
            message.success(
                `已保存并生效：${res.symbol_count} 只` +
                    (res.rejected ? `，${res.rejected} 条被拒` : '') +
                    (res.duplicates ? `，去重 ${res.duplicates} 条` : ''),
            );
            await loadMembers(detail.pool_id);
            await refreshDetail(detail.pool_id);
            loadPools();
        } catch (e: any) {
            message.error(`保存失败: ${e?.response?.data?.detail || e?.message || e}`);
        } finally {
            setSavingMembers(false);
        }
    };

    const handleRefreshBuiltin = async (pool: StockPool) => {
        try {
            const res = await stockPoolService.refreshPool(pool.pool_id);
            message.success(`已刷新成分：${res.symbol_count} 只`);
            await refreshDetail(pool.pool_id);
            if (detailOpen && detail?.pool_id === pool.pool_id) void loadMembers(pool.pool_id);
            loadPools();
        } catch (e: any) {
            message.error(`刷新失败: ${e?.response?.data?.detail || e?.message || e}`);
        }
    };

    const handleArchive = async (pool: StockPool) => {
        try {
            await stockPoolService.archivePool(pool.pool_id);
            message.success('已归档');
            loadPools();
        } catch (e: any) {
            message.error(`归档失败: ${e?.response?.data?.detail || e?.message || e}`);
        }
    };

    const handleDelete = async (pool: StockPool) => {
        try {
            await stockPoolService.deletePool(pool.pool_id);
            message.success('已删除');
            loadPools();
        } catch (e: any) {
            message.error(`删除失败: ${e?.response?.data?.detail || e?.message || e}`);
        }
    };

    const handleResolve = async () => {
        setResolving(true);
        try {
            setResolveResult(await stockPoolService.resolve(resolveRef));
        } catch (e: any) {
            message.error(`解析失败: ${e?.response?.data?.detail || e?.message || e}`);
        } finally {
            setResolving(false);
        }
    };

    // 引用管理
    const loadUsages = async (poolId: string) => {
        setUsagesLoading(true);
        try {
            const res = await stockPoolService.usages(poolId);
            setUsages(res.items || []);
        } catch (e: any) {
            message.error(`加载引用失败: ${e?.response?.data?.detail || e?.message || e}`);
        } finally {
            setUsagesLoading(false);
        }
    };

    const handleBind = async (poolId: string) => {
        if (!bindingTargetId.trim()) {
            message.warning('请填写目标 ID');
            return;
        }
        setBindingBusy(true);
        try {
            await stockPoolService.bindPool(poolId, {
                target_type: bindingTargetType,
                target_id: bindingTargetId.trim(),
            });
            message.success('引用已登记（该池将被保护，不可删除）');
            setBindingTargetId('');
            await loadUsages(poolId);
        } catch (e: any) {
            message.error(`登记失败: ${e?.response?.data?.detail || e?.message || e}`);
        } finally {
            setBindingBusy(false);
        }
    };

    const handleUnbind = async (poolId: string, targetType: string, targetId: string) => {
        try {
            await stockPoolService.unbindPool(poolId, targetType, targetId);
            message.success('引用已解除');
            await loadUsages(poolId);
        } catch (e: any) {
            message.error(`解除失败: ${e?.response?.data?.detail || e?.message || e}`);
        }
    };

    const handleReconcile = async () => {
        try {
            const res = await stockPoolService.reconcileBindings(true);
            message.info(
                `引用回填预览：扫描 ${res.scanned} 条模型记录，可绑定 ${res.bound}，` +
                    `无法解析 ${res.unresolved}${res.unresolved_samples?.length ? `（示例 ${res.unresolved_samples.slice(0, 3).join(', ')}）` : ''}`,
            );
        } catch (e: any) {
            message.error(`回填失败: ${e?.response?.data?.detail || e?.message || e}`);
        }
    };

    // 上传解析导入
    const fileToBase64 = (file: File): Promise<string> =>
        new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => {
                const bytes = new Uint8Array(reader.result as ArrayBuffer);
                let binary = '';
                const CHUNK = 0x8000;
                for (let i = 0; i < bytes.length; i += CHUNK) {
                    binary += String.fromCharCode.apply(
                        null,
                        Array.from(bytes.subarray(i, i + CHUNK)) as unknown as number[],
                    );
                }
                resolve(btoa(binary));
            };
            reader.onerror = () => reject(reader.error);
            reader.readAsArrayBuffer(file);
        });

    const handleBeforeUpload = async (file: File) => {
        try {
            const b64 = await fileToBase64(file);
            setParseBase64(b64);
            setParseFileName(file.name);
            setParseText('');
            const ext = (file.name.split('.').pop() || '').toLowerCase();
            if (ext === 'csv' || ext === 'txt') setParseFmt(ext as 'csv' | 'txt');
            setReport(null);
            setSelectedRows([]);
        } catch (e: any) {
            message.error(`读取文件失败: ${e?.message || e}`);
        }
        return false;
    };

    const handleRunParse = async () => {
        if (!parseBase64 && !parseText.trim()) {
            message.warning('请先选择文件或粘贴内容');
            return;
        }
        setParseRunning(true);
        try {
            const res = await stockPoolService.parsePoolFile({
                content_base64: parseBase64 || undefined,
                content_text: parseBase64 ? undefined : parseText,
                filename: parseFileName || undefined,
                fmt: parseFmt === 'auto' ? undefined : parseFmt,
                has_header: parseHasHeader,
                column: parseColumn.trim() || undefined,
                // 后端明细上限 20000；A 股全市场约 5000+ 只，必须一次取全，否则勾选数会被截断
                row_limit: 20000,
            });
            setReport(res);
            setSelectedRows(
                res.rows.filter((r) => r.status === 'matched' && !r.duplicate).map((r) => r.row_index),
            );
            if (res.truncated) {
                message.warning(
                    `文件行数超出明细展示上限，仅展示前 ${res.rows.length} 行，勾选数以展示为准`,
                );
            }
            if (!poolName && parseFileName) {
                setPoolName(parseFileName.replace(/\.[^.]+$/, '').slice(0, 60));
            }
            message.success(
                `解析完成：匹配 ${res.summary.matched} / 重复 ${res.summary.duplicates} / 未匹配 ${res.summary.unmatched}`,
            );
        } catch (e: any) {
            message.error(`解析失败: ${e?.response?.data?.detail || e?.message || e}`);
        } finally {
            setParseRunning(false);
        }
    };

    const handleCreateFromParse = async () => {
        if (!report) return;
        if (!poolCode.trim() || !poolName.trim()) {
            message.warning('请填写股票池代码与名称');
            return;
        }
        // 与后端 schemas.py 保持一致：code 直接用作 TXT 文件名，仅限 ASCII 安全字符
        if (!/^[A-Za-z0-9_-]+$/.test(poolCode.trim())) {
            message.warning('池代码仅限英文字母、数字、下划线与短横线');
            return;
        }
        if (selectedRows.length === 0) {
            message.warning('请至少保留一只股票');
            return;
        }
        setCreating(true);
        try {
            const byRow = new Map<number, ParseRow>();
            report.rows.forEach((r) => byRow.set(r.row_index, r));
            const symbols = selectedRows
                .map((idx) => byRow.get(idx))
                .filter((r): r is ParseRow => !!r && r.status === 'matched' && !!r.api_symbol)
                .map((r) => r.api_symbol as string);

            const res = await stockPoolService.createPoolFromMembers({
                code: poolCode.trim(),
                name: poolName.trim(),
                description: poolDesc.trim() || undefined,
                market: 'CN',
                pool_type: 'imported',
                symbols,
            });
            message.success(
                `股票池 ${res.pool.code} 已创建并可立即使用：${res.accepted} 只` +
                    (res.rejected ? `，${res.rejected} 条被拒` : ''),
            );
            setReport(null);
            setSelectedRows([]);
            setParseBase64('');
            setParseText('');
            setParseFileName('');
            setPoolCode('');
            setPoolName('');
            setPoolDesc('');
            loadPools();
        } catch (e: any) {
            message.error(`建池失败: ${e?.response?.data?.detail || e?.message || e}`);
        } finally {
            setCreating(false);
        }
    };

    const columns = useMemo(
        () => [
            {
                title: '代码',
                dataIndex: 'code',
                width: 150,
                align: 'center' as const,
                render: (code: string, row: StockPool) => (
                    <Space size={4} style={{ justifyContent: 'center', width: '100%' }}>
                        <Text strong>{code}</Text>
                        {row.is_system && <Tag color="blue" style={{ margin: 0 }}>内置</Tag>}
                    </Space>
                ),
            },
            { title: '名称', dataIndex: 'name', width: 150, align: 'center' as const },
            { title: '市场', dataIndex: 'market', width: 70, align: 'center' as const },
            {
                title: '类型',
                dataIndex: 'pool_type',
                width: 110,
                align: 'center' as const,
                render: (t: string) => POOL_TYPE_LABEL[t] || t,
            },
            {
                title: '状态',
                dataIndex: 'status',
                width: 90,
                align: 'center' as const,
                render: (s: string) => (
                    <Tag color={STATUS_COLOR[s] || 'default'} style={{ margin: 0 }}>
                        {s}
                    </Tag>
                ),
            },
            {
                title: '成员数',
                dataIndex: 'symbol_count',
                width: 90,
                align: 'center' as const,
                render: (n: number) => (n > 0 ? n : <Text type="secondary">0</Text>),
            },
            {
                title: '校验和',
                dataIndex: 'checksum',
                width: 130,
                align: 'center' as const,
                render: (v: string) => (v ? <Text code>{v.slice(0, 10)}</Text> : '—'),
            },
            {
                title: '操作',
                key: 'actions',
                align: 'left' as const,
                render: (_: unknown, row: StockPool) => (
                    <Space size={4} wrap style={{ justifyContent: 'flex-start', width: '100%' }}>
                        <Button size="small" onClick={() => openDetail(row)}>
                            成员 / 引用
                        </Button>
                        {row.is_system && (
                            <Tooltip title="从 QuantDB 指数权重重新拉取成分并覆盖 TXT">
                                <Button
                                    size="small"
                                    icon={<ReloadOutlined />}
                                    onClick={() => handleRefreshBuiltin(row)}
                                />
                            </Tooltip>
                        )}
                        {!row.is_system && (
                            <>
                                <Popconfirm title="归档该池？" onConfirm={() => handleArchive(row)} okText="归档">
                                    <Button size="small" icon={<WarningOutlined />} />
                                </Popconfirm>
                                <Popconfirm
                                    title="彻底删除（含成员 TXT）？"
                                    onConfirm={() => handleDelete(row)}
                                    okText="删除"
                                >
                                    <Button size="small" danger icon={<DeleteOutlined />} />
                                </Popconfirm>
                            </>
                        )}
                    </Space>
                ),
            },
        ],
        // eslint-disable-next-line react-hooks/exhaustive-deps
        [items, detailOpen, detail],
    );

    /** 引用明细 */
    const usageColumns = [
        {
            title: '类型',
            dataIndex: 'target_type',
            width: 110,
            align: 'center' as const,
            render: (t: string) => TARGET_TYPE_LABEL[t] || t,
        },
        { title: '目标 ID', dataIndex: 'target_id', ellipsis: true, align: 'center' as const },
        { title: '模式', dataIndex: 'mode', width: 90, align: 'center' as const },
        { title: '优先级', dataIndex: 'priority', width: 80, align: 'center' as const },
        {
            title: '操作',
            key: 'op',
            width: 90,
            align: 'center' as const,
            render: (_: unknown, row: any) => (
                <Popconfirm
                    title="解除该引用？"
                    description="解除后该池可能变为可删除。"
                    onConfirm={() => handleUnbind(detail?.pool_id || '', row.target_type, row.target_id)}
                    okText="解除"
                >
                    <Button size="small" danger icon={<DeleteOutlined />} />
                </Popconfirm>
            ),
        },
    ];

    /** 上传解析报告明细 */
    const parseColumns = [
        { title: '行号', dataIndex: 'row_index', width: 64, align: 'center' as const },
        { title: '原文', dataIndex: 'raw', ellipsis: true, align: 'center' as const },
        {
            title: '命中单元',
            dataIndex: 'token',
            width: 120,
            align: 'center' as const,
            render: (v: string) => <Text code>{v || '—'}</Text>,
        },
        {
            title: '状态',
            dataIndex: 'status',
            width: 120,
            align: 'center' as const,
            render: (s: string, row: ParseRow) => {
                const m = PARSE_STATUS_META[s] || { color: 'default', label: s };
                return (
                    <Space size={4}>
                        <Tag color={m.color}>{m.label}</Tag>
                        {row.duplicate && <Tag color="blue">重复</Tag>}
                    </Space>
                );
            },
        },
        {
            title: '匹配方式',
            dataIndex: 'match_type',
            width: 116,
            align: 'center' as const,
            render: (t: string) =>
                t === 'exchange_fixed' ? (
                    <Tooltip title="文件里的交易所前缀有误，已按 6 位代码纠正">
                        <Tag color="gold" style={{ margin: 0 }}>
                            {MATCH_TYPE_LABEL[t] || t}
                        </Tag>
                    </Tooltip>
                ) : (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                        {MATCH_TYPE_LABEL[t] || t}
                    </Text>
                ),
        },
        {
            title: '代码',
            dataIndex: 'api_symbol',
            width: 110,
            align: 'center' as const,
            render: (v: string) => v || '—',
        },
        {
            title: '名称',
            dataIndex: 'name',
            width: 130,
            align: 'center' as const,
            render: (v: string) => v || '—',
        },
    ];

    return (
        <div style={{ width: '100%', maxWidth: 1240, margin: '0 auto', padding: '20px 24px 40px' }}>
            <Card
                style={{ borderRadius: 12, boxShadow: '0 1px 3px rgba(15,23,42,0.06)' }}
                styles={{ body: { padding: '20px 24px 24px' } }}
            >
                {/* 顶部左对齐：标题 + 操作区 */}
                <div
                    style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'flex-start',
                        flexWrap: 'wrap',
                        gap: 12,
                        marginBottom: 8,
                    }}
                >
                    <div style={{ textAlign: 'left' }}>
                        <div style={{ fontSize: 18, fontWeight: 800 }}>全局股票池</div>
                        <div style={{ marginTop: 4, fontSize: 12, color: '#94a3b8' }}>
                            回测 / 训练 / 推理 / 模拟盘 / 实盘共用的唯一事实源 · 保存即生效
                        </div>
                    </div>
                    <Space wrap style={{ justifyContent: 'flex-end' }}>
                        <Button icon={<ExperimentOutlined />} onClick={() => setResolveOpen(true)}>
                            解析调试
                        </Button>
                        <Button icon={<CloudUploadOutlined />} onClick={handleReconcile}>
                            引用回填预览
                        </Button>
                        <Button icon={<SearchOutlined />} onClick={loadHealth}>
                            健康检查
                        </Button>
                        <Button icon={<ReloadOutlined />} onClick={loadPools}>
                            刷新
                        </Button>
                        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
                            新建股票池
                        </Button>
                    </Space>
                </div>
                <Tabs
                    defaultActiveKey="list"
                    items={[
                        {
                            key: 'list',
                            label: '股票池列表',
                            children: (
                                <>
                                    <Alert
                                        type="info"
                                        showIcon
                                        style={{ marginBottom: 12, textAlign: 'left' }}
                                        message="股票池是回测 / 训练 / 推理 / 模拟盘 / 实盘共用的唯一事实源"
                                        description={
                                            <span>
                                                每个池的成分就是一个 TXT（前缀式一行一个，如 <Text code>SH600036</Text>），
                                                存于服务器 <Text code>{meta?.pool_txt_dir || '/data/stock_pool'}</Text>，
                                                回测引擎等模块可直接读取。<b>编辑成员保存后立即生效，无需发布</b>；
                                                内置指数池由系统每日自动刷新成分。
                                            </span>
                                        }
                                    />

                                    {health && (
                                        <Alert
                                            type={health.unhealthy > 0 ? 'warning' : 'success'}
                                            showIcon
                                            style={{ marginBottom: 12 }}
                                            message={`健康检查：共 ${health.total} 个池，${health.unhealthy} 个有告警`}
                                            description={
                                                health.unhealthy > 0 ? (
                                                    <Space direction="vertical" size={2}>
                                                        {(health.items || [])
                                                            .filter((i: any) => i.warnings?.length)
                                                            .slice(0, 6)
                                                            .map((i: any) => (
                                                                <Text key={i.pool_id} type="secondary" style={{ fontSize: 12 }}>
                                                                    <Text code>{i.code}</Text>：{i.warnings.join('；')}
                                                                </Text>
                                                            ))}
                                                    </Space>
                                                ) : null
                                            }
                                        />
                                    )}

                                    <Space
                                        wrap
                                        align="center"
                                        size={8}
                                        style={{
                                            marginBottom: 12,
                                            width: '100%',
                                            justifyContent: 'flex-start',
                                        }}
                                    >
                                        <Select
                                            allowClear
                                            placeholder="市场"
                                            style={{ width: 120 }}
                                            value={filters.market}
                                            onChange={(v) => patchFilters({ market: v })}
                                            options={(meta?.markets || ['CN', 'HK', 'US']).map((m) => ({ value: m, label: m }))}
                                        />
                                        <Select
                                            allowClear
                                            placeholder="类型"
                                            style={{ width: 150 }}
                                            value={filters.pool_type}
                                            onChange={(v) => patchFilters({ pool_type: v })}
                                            options={(meta?.pool_types || []).map((t) => ({ value: t, label: POOL_TYPE_LABEL[t] || t }))}
                                        />
                                        <Select
                                            allowClear
                                            placeholder="状态"
                                            style={{ width: 120 }}
                                            value={filters.status}
                                            onChange={(v) => patchFilters({ status: v })}
                                            options={(meta?.statuses || []).map((s) => ({ value: s, label: s }))}
                                        />
                                        <Input
                                            className="stock-pool-search"
                                            placeholder="代码 / 名称（回车搜索）"
                                            style={{ width: 220 }}
                                            allowClear
                                            defaultValue={filters.keyword}
                                            key={filters.keyword || 'empty'}
                                            suffix={<SearchOutlined style={{ color: '#bfbfbf' }} />}
                                            onPressEnter={(e) =>
                                                patchFilters({ keyword: e.currentTarget.value || undefined })
                                            }
                                            onChange={(e) => {
                                                if (!e.target.value) patchFilters({ keyword: undefined });
                                            }}
                                        />
                                    </Space>

                                    <Table
                                        rowKey="pool_id"
                                        size="small"
                                        loading={loading}
                                        columns={columns as any}
                                        dataSource={items}
                                        className="stock-pool-table"
                                        style={{ width: '100%' }}
                                        tableLayout="fixed"
                                        pagination={{
                                            current: page,
                                            pageSize,
                                            total,
                                            showSizeChanger: true,
                                            position: ['bottomRight'],
                                            onChange: (p, ps) => {
                                                setPage(p);
                                                setPageSize(ps);
                                            },
                                        }}
                                    />
                                </>
                            ),
                        },
                        {
                            key: 'parse',
                            label: '上传解析导入',
                            children: (
                                <>
                                    <Alert
                                        type="info"
                                        showIcon
                                        style={{ marginBottom: 12, textAlign: 'left' }}
                                        message="上传 CSV / TXT，自动与 data/stocks/stocks_index.json 对比后生成股票池"
                                        description={
                                            <span>
                                                代码支持 <Text code>600519</Text> / <Text code>SH600519</Text> /{' '}
                                                <Text code>600519.SH</Text> / <Text code>sh600519</Text>；
                                                也可直接写中文简称（<Text code>贵州茅台</Text>、<Text code>万 科Ａ</Text> 均可）。
                                                代码与名称可混排、列顺序随意。交易所写错会按代码自动纠正。
                                                <b> 解析只出报告，确认后才落库；建完立即可被各功能消费。</b>
                                            </span>
                                        }
                                    />

                                    {/* 步骤 1：目标股票池命名（置顶，先定名再解析） */}
                                    <Card
                                        size="small"
                                        style={{ marginBottom: 16 }}
                                        title={<div style={{ textAlign: 'center' }}>1. 目标股票池命名</div>}
                                    >
                                        <Row gutter={12} align="middle">
                                            <Col span={6}>
                                                <Input
                                                    addonBefore="代码"
                                                    value={poolCode}
                                                    maxLength={64}
                                                    onChange={(e) =>
                                                        setPoolCode(e.target.value.replace(/[^A-Za-z0-9_-]/g, ''))
                                                    }
                                                    placeholder="my_pool"
                                                />
                                            </Col>
                                            <Col span={6}>
                                                <Input
                                                    addonBefore="名称"
                                                    value={poolName}
                                                    onChange={(e) => setPoolName(e.target.value)}
                                                    placeholder="我的自选池"
                                                />
                                            </Col>
                                            <Col span={7}>
                                                <Input
                                                    addonBefore="描述"
                                                    value={poolDesc}
                                                    onChange={(e) => setPoolDesc(e.target.value)}
                                                    placeholder="来源、维护方式、用途（可选）"
                                                />
                                            </Col>
                                            <Col span={5}>
                                                <Tooltip
                                                    title={
                                                        !report
                                                            ? '先完成下方第 4 步解析，再一键生成'
                                                            : selectedRows.length === 0
                                                              ? '请在解析报告中勾选至少一只股票'
                                                              : `生成 ${poolCode.trim() || '新股票池'}（${selectedRows.length} 只）`
                                                    }
                                                >
                                                    <Button
                                                        type="primary"
                                                        block
                                                        icon={<PlusOutlined />}
                                                        loading={creating}
                                                        onClick={handleCreateFromParse}
                                                    >
                                                        生成股票池
                                                        {report ? `（${selectedRows.length} 只）` : ''}
                                                    </Button>
                                                </Tooltip>
                                            </Col>
                                        </Row>
                                    </Card>

                                    <Row gutter={16}>
                                        <Col span={13}>
                                            <Card
                                                size="small"
                                                title={<div style={{ textAlign: 'center' }}>2. 选择文件</div>}
                                            >
                                                <Upload.Dragger
                                                    accept=".csv,.txt,.tsv"
                                                    showUploadList={false}
                                                    beforeUpload={handleBeforeUpload}
                                                    disabled={parseRunning}
                                                >
                                                    <p className="ant-upload-drag-icon">
                                                        <InboxOutlined />
                                                    </p>
                                                    <p className="ant-upload-text">点击或拖拽 CSV / TXT 文件到此处</p>
                                                    <p className="ant-upload-hint">支持 UTF-8 / GBK（Excel 直接另存的 CSV 也能读）</p>
                                                </Upload.Dragger>
                                                {parseFileName && (
                                                    <div style={{ marginTop: 8 }}>
                                                        <Tag color="blue">{parseFileName}</Tag>
                                                        <Button
                                                            size="small"
                                                            type="link"
                                                            onClick={() => {
                                                                setParseBase64('');
                                                                setParseFileName('');
                                                                setReport(null);
                                                            }}
                                                        >
                                                            清除
                                                        </Button>
                                                    </div>
                                                )}

                                                <Divider plain style={{ margin: '12px 0' }}>
                                                    或直接粘贴内容
                                                </Divider>
                                                <Input.TextArea
                                                    rows={5}
                                                    value={parseText}
                                                    onChange={(e) => {
                                                        setParseText(e.target.value);
                                                        if (e.target.value) {
                                                            setParseBase64('');
                                                            setParseFileName('');
                                                        }
                                                    }}
                                                    placeholder={'600519.SH\n贵州茅台\n600036.SH\n300750.SZ,宁德时代'}
                                                />
                                            </Card>
                                        </Col>

                                        <Col span={11}>
                                            <Card
                                                size="small"
                                                title={<div style={{ textAlign: 'center' }}>3. 解析选项</div>}
                                            >
                                                <Space direction="vertical" style={{ width: '100%' }} size={12}>
                                                    <div>
                                                        <Text type="secondary">文件格式</Text>
                                                        <Select
                                                            style={{ width: '100%', marginTop: 4 }}
                                                            value={parseFmt}
                                                            onChange={setParseFmt}
                                                            options={[
                                                                { value: 'auto', label: '自动识别（推荐）' },
                                                                { value: 'csv', label: 'CSV / 逗号分隔' },
                                                                { value: 'txt', label: 'TXT / 每行一个' },
                                                            ]}
                                                        />
                                                    </div>
                                                    <div>
                                                        <Text type="secondary">CSV 首行为表头</Text>
                                                        <div style={{ marginTop: 4 }}>
                                                            <Switch checked={parseHasHeader} onChange={setParseHasHeader} />
                                                            <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                                                                开启后自动跳过表头行
                                                            </Text>
                                                        </div>
                                                    </div>
                                                    <div>
                                                        <Text type="secondary">强制指定列（可选）</Text>
                                                        <Input
                                                            style={{ marginTop: 4 }}
                                                            value={parseColumn}
                                                            onChange={(e) => setParseColumn(e.target.value)}
                                                            placeholder="列名（如 code）或列序号（如 0）"
                                                        />
                                                        <Text type="secondary" style={{ fontSize: 12 }}>
                                                            留空则逐行扫描所有列，自动找出股票
                                                        </Text>
                                                    </div>
                                                    <Button
                                                        type="primary"
                                                        block
                                                        icon={<FileSearchOutlined />}
                                                        loading={parseRunning}
                                                        onClick={handleRunParse}
                                                    >
                                                        开始解析
                                                    </Button>
                                                </Space>
                                            </Card>
                                        </Col>
                                    </Row>

                                    {report && (
                                        <>
                                            <Divider orientation="left">4. 解析报告</Divider>
                                            <Row gutter={12} style={{ marginBottom: 12 }}>
                                                <Col span={4}>
                                                    <Statistic title="文件行数" value={report.summary.total} />
                                                </Col>
                                                <Col span={4}>
                                                    <Statistic
                                                        title="匹配（唯一）"
                                                        value={report.summary.matched}
                                                        valueStyle={{ color: '#3f8600' }}
                                                    />
                                                </Col>
                                                <Col span={4}>
                                                    <Statistic
                                                        title="重复行"
                                                        value={report.summary.duplicates}
                                                        valueStyle={{ color: '#1677ff' }}
                                                    />
                                                </Col>
                                                <Col span={4}>
                                                    <Statistic
                                                        title="未匹配"
                                                        value={report.summary.unmatched}
                                                        valueStyle={{ color: report.summary.unmatched ? '#cf1322' : undefined }}
                                                    />
                                                </Col>
                                                <Col span={4}>
                                                    <Statistic
                                                        title="其中不在索引"
                                                        value={report.summary.not_in_index}
                                                        valueStyle={{ color: '#d46b08' }}
                                                    />
                                                </Col>
                                                <Col span={4}>
                                                    <Statistic title="勾选保留" value={selectedRows.length} />
                                                </Col>
                                            </Row>

                                            {report.warnings.length > 0 && (
                                                <Alert
                                                    type="warning"
                                                    showIcon
                                                    style={{ marginBottom: 12 }}
                                                    message="解析告警"
                                                    description={
                                                        <Space direction="vertical" size={2}>
                                                            {report.warnings.slice(0, 8).map((w, i) => (
                                                                <Text key={i} style={{ fontSize: 12 }}>
                                                                    {w}
                                                                </Text>
                                                            ))}
                                                        </Space>
                                                    }
                                                />
                                            )}

                                            <Text type="secondary" style={{ fontSize: 12 }}>
                                                索引：{report.index_source || '未找到'}（{report.index_size} 条） · 编码{' '}
                                                {report.encoding || 'utf-8'}
                                                {report.truncated ? ' · 明细已截断展示' : ''}
                                            </Text>

                                            <Table
                                                rowKey="row_index"
                                                size="small"
                                                style={{ marginTop: 8 }}
                                                columns={parseColumns as any}
                                                dataSource={report.rows}
                                                rowSelection={{
                                                    selectedRowKeys: selectedRows,
                                                    onChange: (keys) => setSelectedRows(keys as number[]),
                                                    getCheckboxProps: (row: ParseRow) => ({
                                                        disabled: row.status !== 'matched',
                                                    }),
                                                }}
                                                pagination={{ pageSize: 50, showSizeChanger: true }}
                                            />

                                        </>
                                    )}
                                </>
                            ),
                        },
                    ]}
                />
            </Card>

            {/* 新建 — 统一样式 */}
            <Modal
                title={
                    <div className="flex items-center gap-2">
                        <div className="w-8 h-8 rounded-xl bg-blue-50 border border-blue-100 flex items-center justify-center text-blue-600">
                            <PlusOutlined />
                        </div>
                        <span className="font-black text-slate-800">新建股票池</span>
                    </div>
                }
                open={createOpen}
                onOk={handleCreate}
                onCancel={() => setCreateOpen(false)}
                okText="创建"
                okButtonProps={{ className: 'rounded-xl font-bold' }}
                cancelButtonProps={{ className: 'rounded-xl' }}
                centered
                destroyOnClose
                styles={{
                    content: { borderRadius: 24, padding: 0, overflow: 'hidden' },
                    header: { padding: '16px 24px', margin: 0, borderBottom: '1px solid #f1f5f9' },
                    body: { padding: 24 },
                    footer: { padding: '12px 24px', borderTop: '1px solid #f1f5f9' },
                }}
            >
                <Form form={createForm} layout="vertical" initialValues={{ market: 'CN', pool_type: 'static' }}>
                    <Form.Item
                        name="code"
                        label="代码"
                        rules={[
                            { required: true, message: '请输入代码' },
                            { pattern: /^[A-Za-z0-9_-]+$/, message: '仅允许字母、数字、下划线与短横线' },
                        ]}
                        extra="各功能引用标识（也用作服务器上的 TXT 文件名），如 my_quality_pool"
                    >
                        <Input placeholder="my_quality_pool" />
                    </Form.Item>
                    <Form.Item name="name" label="名称" rules={[{ required: true, message: '请输入名称' }]}>
                        <Input placeholder="优质成长池" />
                    </Form.Item>
                    <Row gutter={12}>
                        <Col span={12}>
                            <Form.Item name="market" label="市场" rules={[{ required: true }]}>
                                <Select options={(meta?.markets || ['CN']).map((m) => ({ value: m, label: m }))} />
                            </Form.Item>
                        </Col>
                        <Col span={12}>
                            <Form.Item name="pool_type" label="类型" rules={[{ required: true }]}>
                                <Select
                                    options={(meta?.pool_types || [])
                                        .filter((t) => t !== 'system_index')
                                        .map((t) => ({ value: t, label: POOL_TYPE_LABEL[t] || t }))}
                                />
                            </Form.Item>
                        </Col>
                    </Row>
                    <Form.Item name="description" label="描述">
                        <Input.TextArea rows={3} placeholder="用途、维护方式、数据来源" />
                    </Form.Item>
                </Form>
            </Modal>

            {/* 成员 / 引用 */}
            <Drawer
                title={detail ? `${detail.code} · ${detail.name}` : '股票池'}
                width={640}
                className="stock-pool-drawer"
                open={detailOpen}
                closable={false}
                onClose={() => setDetailOpen(false)}
                destroyOnClose
            >
                {detail && (
                    <>
                        <Row gutter={12} style={{ marginBottom: 12 }}>
                            <Col span={12}>
                                <Statistic title="成员数" value={detail.symbol_count} />
                            </Col>
                            <Col span={12}>
                                <Statistic title="状态" value={detail.status} />
                            </Col>
                        </Row>

                        <div
                            style={{
                                marginBottom: 12,
                                padding: '8px 12px',
                                background: '#f8fafc',
                                border: '1px solid #f1f5f9',
                                borderRadius: 8,
                            }}
                        >
                            <Text type="secondary" style={{ fontSize: 12 }}>
                                TXT 文件
                            </Text>
                            <div>
                                <Text code style={{ fontSize: 12, wordBreak: 'break-all' }}>
                                    {detail.file_path || '（尚未生成）'}
                                </Text>
                            </div>
                        </div>

                        <Descriptions size="small" column={1} bordered style={{ marginBottom: 12 }}>
                            <Descriptions.Item label="pool_id">{detail.pool_id}</Descriptions.Item>
                            <Descriptions.Item label="市场 / 类型">
                                {detail.market} / {POOL_TYPE_LABEL[detail.pool_type] || detail.pool_type}
                            </Descriptions.Item>
                            <Descriptions.Item label="校验和">{detail.checksum || '—'}</Descriptions.Item>
                            <Descriptions.Item label="来源">
                                {detail.source_kind || '—'} {detail.source_ref ? `(${detail.source_ref})` : ''}
                            </Descriptions.Item>
                        </Descriptions>

                        {detail.is_system && (
                            <Alert
                                type="info"
                                showIcon
                                style={{ marginBottom: 12 }}
                                message="内置指数池：成分由 QuantDB 指数权重每日自动刷新，不可手工编辑；如需自定义请新建 imported 池。"
                                action={
                                    <Button
                                        size="small"
                                        icon={<ReloadOutlined />}
                                        onClick={() => handleRefreshBuiltin(detail)}
                                    >
                                        立即刷新
                                    </Button>
                                }
                            />
                        )}

                        <Divider orientation="left">成员（后缀式，一行一个）</Divider>
                        <Input.TextArea
                            rows={10}
                            value={memberDraft}
                            onChange={(e) => setMemberDraft(e.target.value)}
                            disabled={detail.is_system}
                            placeholder={'600036.SH\n000001.SZ\n600519.SH'}
                            style={{ fontFamily: 'monospace' }}
                        />
                        <Space style={{ marginTop: 8 }} wrap>
                            <Button
                                type="primary"
                                icon={<CloudUploadOutlined />}
                                loading={savingMembers}
                                disabled={detail.is_system}
                                onClick={handleSaveMembers}
                            >
                                保存成员（立即生效）
                            </Button>
                            <Button
                                icon={<ExportOutlined />}
                                href={stockPoolService.membersExportUrl(detail.pool_id)}
                                target="_blank"
                            >
                                导出 TXT
                            </Button>
                            <Button
                                icon={<ReloadOutlined />}
                                loading={memberLoading}
                                onClick={() => loadMembers(detail.pool_id)}
                            >
                                重新加载
                            </Button>
                            <Text type="secondary" style={{ fontSize: 12 }}>
                                共 {members.length} 只 · 逗号/分号也可分隔 · 保存后回测 / 训练 / 推理立即读到
                            </Text>
                        </Space>

                        <Divider orientation="left">引用（被引用后不可删除）</Divider>
                        <Paragraph type="secondary" style={{ fontSize: 12 }}>
                            只登记<b>长生命周期</b>引用（策略 / 模型 / 模拟盘账户 / 实盘配置）。
                            回测与推理是一次性运行，不登记为 binding —— 它们的池校验和记在各自结果里。
                        </Paragraph>
                        <Space wrap style={{ marginBottom: 8, width: '100%' }}>
                            <Select
                                style={{ width: 120 }}
                                value={bindingTargetType}
                                onChange={setBindingTargetType}
                                options={[
                                    { value: 'strategy', label: '策略' },
                                    { value: 'training', label: '模型' },
                                    { value: 'simulation', label: '模拟盘账户' },
                                    { value: 'live', label: '实盘配置' },
                                    { value: 'factor', label: '因子' },
                                ]}
                            />
                            <Input
                                style={{ flex: 1, minWidth: 160 }}
                                value={bindingTargetId}
                                onChange={(e) => setBindingTargetId(e.target.value)}
                                placeholder="目标 ID（如策略/模型 ID）"
                            />
                            <Button icon={<PlusOutlined />} loading={bindingBusy} onClick={() => handleBind(detail.pool_id)}>
                                登记引用
                            </Button>
                        </Space>
                        <Table
                            rowKey={(r: any) => `${r.target_type}:${r.target_id}`}
                            size="small"
                            loading={usagesLoading}
                            columns={usageColumns as any}
                            dataSource={usages}
                            pagination={false}
                            locale={{ emptyText: <Empty description="暂无引用登记" /> }}
                        />
                    </>
                )}
            </Drawer>

            {/* 解析调试 — 统一样式：与数据管理弹窗一致 */}
            <Modal
                title={
                    <div className="flex items-center gap-2">
                        <div className="w-8 h-8 rounded-xl bg-indigo-50 border border-indigo-100 flex items-center justify-center text-indigo-600">
                            <ExperimentOutlined />
                        </div>
                        <span className="font-black text-slate-800">股票池解析调试</span>
                    </div>
                }
                open={resolveOpen}
                onCancel={() => setResolveOpen(false)}
                footer={null}
                width={760}
                centered
                destroyOnClose
                styles={{
                    content: { borderRadius: 24, padding: 0, overflow: 'hidden' },
                    header: { padding: '16px 24px', margin: 0, borderBottom: '1px solid #f1f5f9' },
                    body: { padding: 24 },
                }}
            >
                <div className="bg-slate-50 border border-slate-100 rounded-xl px-3 py-2.5 mb-4">
                    <Text type="secondary" className="text-xs leading-relaxed">
                        验证各功能实际会拿到什么成分。支持 <Text code className="text-xs">pool:code</Text>、裸内置名{' '}
                        <Text code className="text-xs">csi300</Text>、<Text code className="text-xs">list:SH600036,SZ000001</Text>、
                        <Text code className="text-xs">file:/path/x.txt</Text>、<Text code className="text-xs">all</Text>。
                    </Text>
                </div>
                <Space.Compact style={{ width: '100%' }}>
                    <Input
                        value={resolveRef}
                        onChange={(e) => setResolveRef(e.target.value)}
                        placeholder="pool:csi300"
                        onPressEnter={handleResolve}
                        className="rounded-l-xl"
                        style={{ height: 40, fontSize: 13 }}
                    />
                    <Button
                        type="primary"
                        loading={resolving}
                        onClick={handleResolve}
                        className="rounded-r-xl font-bold"
                        style={{ height: 40, padding: '0 20px' }}
                    >
                        解析
                    </Button>
                </Space.Compact>

                {resolveResult && (
                    <div className="mt-4 space-y-3">
                        <Descriptions
                            size="small"
                            column={2}
                            bordered
                            styles={{ label: { background: '#f8fafc', fontSize: 12 }, content: { fontSize: 12 } }}
                            className="rounded-xl overflow-hidden"
                        >
                            <Descriptions.Item label="来源">
                                {SOURCE_LABEL[resolveResult.source] || resolveResult.source}
                            </Descriptions.Item>
                            <Descriptions.Item label="池">{resolveResult.code}</Descriptions.Item>
                            <Descriptions.Item label="成分数">{resolveResult.symbol_count}</Descriptions.Item>
                            <Descriptions.Item label="市场 / 口径">{resolveResult.market}</Descriptions.Item>
                            <Descriptions.Item label="不过滤">{String(resolveResult.unfiltered)}</Descriptions.Item>
                            <Descriptions.Item label="校验和">{resolveResult.checksum || '—'}</Descriptions.Item>
                        </Descriptions>
                        {resolveResult.warnings?.length > 0 && (
                            <Alert
                                type="warning"
                                showIcon
                                className="rounded-xl"
                                message="解析告警"
                                description={
                                    <Space direction="vertical" size={2}>
                                        {resolveResult.warnings.map((w, i) => (
                                            <Text key={i} className="text-xs">
                                                {w}
                                            </Text>
                                        ))}
                                    </Space>
                                }
                            />
                        )}
                        <div className="bg-slate-50 border border-slate-100 rounded-xl px-3 py-2.5">
                            <Text type="secondary" className="text-xs">样本（API 前缀式）：</Text>
                            <Paragraph className="text-xs mt-1 mb-0 break-all">
                                {(resolveResult.sample || []).join(', ') || '（空）'}
                            </Paragraph>
                        </div>
                    </div>
                )}
            </Modal>
        </div>
    );
};

export default AdminStockPool;
