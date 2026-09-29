-- upgrade_v1.0.9: qm_model_inference_runs 增加 pool_id 物理列
-- 背景：股票池推理与全市场推理同天并存时，历史列表按 (data_trade_date) 去重只留一行
-- （signals_count 大的胜出），池 run 永远被全市场行盖住；且池 run 曾跨 run 删除同日
-- 全市场信号。新逻辑按 (data_trade_date, pool_id) 去重展示，各 run 信号互不删除。
-- 幂等：IF NOT EXISTS，可随主初始化重复重放（ON_ERROR_STOP=0）。

ALTER TABLE qm_model_inference_runs
  ADD COLUMN IF NOT EXISTS pool_id TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_qm_model_inference_runs_pool
  ON qm_model_inference_runs (tenant_id, user_id, model_id, pool_id, data_trade_date DESC);

-- 存量回填：request_json / result_json 里带 pool_id 的行补到物理列
-- （此前版本未写 JSON 时此语句为空操作）。
UPDATE qm_model_inference_runs
   SET pool_id = COALESCE(
     NULLIF(request_json->>'pool_id', ''),
     NULLIF(result_json->>'pool_id', ''),
     ''
   )
 WHERE COALESCE(pool_id, '') = ''
   AND (
     request_json->>'pool_id' IS NOT NULL
     OR result_json->>'pool_id' IS NOT NULL
   );
