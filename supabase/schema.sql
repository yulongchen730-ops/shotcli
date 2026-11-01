-- ============================================================================
-- 投篮分析知识库 → Supabase 表结构
-- 项目: spb-huph0sp3akaoi0jn (public schema，沿用 xhs_* 的 snake_case 风格)
-- 用法: 复制整段到 Supabase Dashboard → SQL Editor → Run（幂等，可重复执行）
-- ============================================================================

-- 1) 训练场次（一段视频 = 一行）
create table if not exists public.shooting_sessions (
  tag              text primary key,          -- 与 data/tags.json 的 tag 一致（a..x）
  session_date     date not null,             -- 标签日期（9月13日/9月20日 疑似同场，见知识库）
  label            text,                      -- 标题，如「9月20日 · 全场（13 球）」
  video            text,                      -- 视频文件名
  fps              numeric,                   -- 采样帧率
  resolution       text,                      -- 如 1920×1080
  duration_s       numeric,                   -- 时长（秒）
  scanned_range    text,                      -- 扫描范围，如 0:00–1:34.2
  camera           text,                      -- 机位（侧后方 / 另一侧 / 侧面90° …）
  distance         text,                      -- 罚球 / 中距 / 三分（可空）
  shots_identified integer,                   -- 识别到的出手次数
  shots_measured   integer,                   -- 逐帧测算次数
  report_path      text,                      -- 报告文件名
  hand             text not null default 'right',
  height_cm        numeric,
  created_at       timestamptz not null default now()
);

-- 2) 逐球指标（一次出手 = 一行，13 项指标 + 评分/误检标记）
create table if not exists public.shooting_shots (
  session_tag                 text not null references public.shooting_sessions(tag) on delete cascade,
  seq                         integer not null,       -- 第几球（按时间序）
  t_rel                       numeric,                -- 出手时刻（秒）
  knee_angle_min              numeric,                -- 屈膝角(最低)          °
  knee_angle_release          numeric,                -- 出手时膝角            °
  elbow_angle_release_shooting numeric,               -- 出手时肘角(投篮手)    °
  elbow_angle_release_guide   numeric,                -- 出手时肘角(辅助手)    °
  elbow_angle_gather_shooting numeric,                -- 收球肘角(投篮手)      °
  elbow_angle_gather_guide    numeric,                -- 收球肘角(辅助手)      °
  forearm_angle               numeric,                -- 出手小臂仰角          °
  release_height_ratio        numeric,                -- 出手点离地高度        ×身高
  vertical_jump_ratio         numeric,                -- 垂直弹跳              ×身高
  knee_to_release_s           numeric,                -- 屈膝到出手            s
  knee_ext_to_release_s       numeric,                -- 膝伸展到出手          s
  body_drift_ratio            numeric,                -- 身体位移              ×身高（有符号，取决于机位侧）
  landing_drift_ratio         numeric,                -- 落地位移              ×身高（有符号）
  score                       numeric,                -- 综合评分（旧口径不可跨期比较）
  rank                        integer,                -- 当段内排名
  is_outlier                  boolean not null default false,  -- 误检（出手点<1.10 或弹跳<0.09）
  primary key (session_tag, seq)
);

create index if not exists shooting_shots_session_idx on public.shooting_shots(session_tag);

-- 3) 场次聚合视图（查趋势用）
create or replace view public.shooting_session_agg as
select
  s.tag,
  s.session_date,
  s.label,
  s.camera,
  count(sh.seq)                                   as n_shots,
  round(avg(sh.knee_angle_min)::numeric, 1)       as knee_angle_min_avg,
  round(stddev(sh.knee_angle_min)::numeric, 2)    as knee_angle_min_sd,
  round(avg(sh.forearm_angle)::numeric, 1)        as forearm_angle_avg,
  round(avg(sh.knee_to_release_s)::numeric, 2)    as tempo_avg,
  round(avg(sh.release_height_ratio)::numeric, 3) as release_height_avg,
  round(avg(sh.vertical_jump_ratio)::numeric, 3)  as jump_avg
from public.shooting_sessions s
left join public.shooting_shots sh on sh.session_tag = s.tag
group by s.tag, s.session_date, s.label, s.camera;

-- 4) 行级安全：service_role 自动绕过；anon 只读（将来做网页/图表用）
alter table public.shooting_sessions enable row level security;
alter table public.shooting_shots    enable row level security;

drop policy if exists "anon read sessions" on public.shooting_sessions;
drop policy if exists "anon read shots"    on public.shooting_shots;
create policy "anon read sessions" on public.shooting_sessions for select to anon using (true);
create policy "anon read shots"    on public.shooting_shots    for select to anon using (true);

-- 授权（新表默认不授予 anon/authenticated，这里补上 SELECT）
grant usage on schema public to anon, authenticated;
grant select on public.shooting_sessions, public.shooting_shots, public.shooting_session_agg to anon, authenticated;
