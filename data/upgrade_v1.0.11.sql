-- upgrade_v1.0.11: 股票代码后缀化（SH600036 → 600036.SH，幂等）
--   全系统唯一通用格式改为 QuantDB 后缀口径（指数同步后缀如 000300.SH）；
--   唯一例外 Qlib 小写 sh600036 仅内存转换、不落库，不在本补丁范围。
--
-- 幂等：只改严格匹配 ^(SH|SZ|BJ)[0-9]{6}$（大写）的值；后缀/小写/港美股/裸码
--   均不匹配，可随启动重复重放。本机已手工执行过迁移脚本的重放为 no-op。
-- 自动执行：backend/main_oss.py 的 _ensure_upgrade_scripts 每次启动扫描重放；
--   deploy/update.sh 第 4 步按 schema_migrations 去重执行。
-- 手工执行（可选）：
--   docker exec -i quantmind-db psql -U quantmind -d quantmind \
--     < data/upgrade_v1.0.11.sql
--
-- 注意：Redis 键（market:snapshot:/market:series:）不在本包内——远端行情库
--   为共享基础设施，只能加键拷贝不能删，且读端已多路兼容；如需补建后缀键，
--   在服务器上执行 backend/scripts/migrate_stock_code_to_suffix.py（先 --dry-run）。
-- ============================================================

BEGIN;

DO $$
DECLARE
  r RECORD;
  v RECORD;
  total_rows BIGINT := 0;
  total_values INT := 0;
BEGIN
  FOR r IN
    SELECT c.table_name, c.column_name
    FROM information_schema.columns c
    WHERE c.table_schema = 'public'
      AND c.column_name IN ('symbol', 'stock_code')
      AND c.data_type IN ('character varying', 'varchar', 'character', 'char', 'text')
      AND c.table_name NOT IN ('alembic_version', 'spatial_ref_sys', 'geography_columns', 'geometry_columns')
    ORDER BY c.table_name, c.column_name
  LOOP
    FOR v IN EXECUTE format(
      'SELECT DISTINCT %I AS old_v FROM %I WHERE %I ~ %L',
      r.column_name, r.table_name, r.column_name, '^(SH|SZ|BJ)[0-9]{6}$'
    ) LOOP
      EXECUTE format(
        'UPDATE %I SET %I = substring(%I FROM 3) || %L || substring(%I FROM 1 FOR 2) WHERE %I = %L',
        r.table_name, r.column_name, r.column_name, '.', r.column_name, r.column_name, v.old_v
      );
      GET DIAGNOSTICS total_rows = ROW_COUNT;
      total_values := total_values + 1;
      RAISE NOTICE 'upgrade_v1.0.11: %.% % → 后缀 (% 行)',
        r.table_name, r.column_name, v.old_v, total_rows;
    END LOOP;
  END LOOP;
  RAISE NOTICE 'upgrade_v1.0.11: 完成，共 % 个不同值转后缀', total_values;
END
$$;

COMMIT;
