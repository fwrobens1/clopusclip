-- NoetharaClipz cloud storage schema.
-- Uses dedicated table names so it does not touch an existing clipping schema
-- already present in the Supabase project. Run once in the SQL editor or via
-- the Supabase Management API.

create table if not exists public.noethara_jobs (
  id text primary key,
  channel text,
  title text,
  source_url text,
  goal text,
  status text default 'queued',
  stage text,
  progress integer default 0,
  duration double precision,
  clips_count integer default 0,
  settings jsonb default '{}'::jsonb,
  notes jsonb default '{}'::jsonb,
  parent_job text,
  thumbnail text,
  source_info jsonb default '{}'::jsonb,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create table if not exists public.noethara_clips (
  id bigserial primary key,
  job_id text not null references public.noethara_jobs(id) on delete cascade,
  idx integer not null,
  title text,
  reason text,
  start_time double precision,
  end_time double precision,
  duration double precision,
  score double precision,
  render_status text default 'pending',
  storage_path text,
  mime_type text default 'video/mp4',
  size_bytes bigint,
  width integer,
  height integer,
  hook text,
  hooks jsonb default '[]'::jsonb,
  focus jsonb default '{}'::jsonb,
  note text,
  error text,
  encoder text,
  created_at timestamptz default now(),
  unique (job_id, idx)
);

alter table public.noethara_clips
  add column if not exists score_reason text,
  add column if not exists hook_title text,
  add column if not exists transcript_excerpt text,
  add column if not exists tracks jsonb default '[]'::jsonb,
  add column if not exists config jsonb default '{}'::jsonb,
  add column if not exists source_start_seconds double precision,
  add column if not exists source_end_seconds double precision;

create table if not exists public.noethara_live_sessions (
  id text primary key,
  job_id text references public.noethara_jobs(id) on delete set null,
  provider text,
  src_url text,
  state text default 'live',
  phase text default 'clipping',
  pass_count integer default 0,
  clipped_from_seconds double precision default 0,
  clipped_until_seconds double precision default 0,
  next_pass_at timestamptz,
  stop_reason text,
  target_clips integer default 10,
  max_clips integer default 60,
  max_duration_seconds integer default 43200,
  first_pass_seconds integer default 900,
  pass_window_seconds integer default 1800,
  curation_prompt text,
  settings jsonb default '{}'::jsonb,
  last_error text,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create table if not exists public.noethara_publications (
  id bigserial primary key,
  uid text unique,
  job_id text not null references public.noethara_jobs(id) on delete cascade,
  clip_idx integer not null,
  platform text not null,
  social_account text,
  caption text,
  hashtags jsonb default '[]'::jsonb,
  scheduled_at timestamptz,
  status text default 'scheduled',
  post_id text,
  error text,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  unique (job_id, clip_idx, platform, scheduled_at)
);

alter table public.noethara_live_sessions
  add column if not exists recording_path text,
  add column if not exists started_at_epoch double precision,
  add column if not exists stop_requested boolean default false;

alter table public.noethara_publications
  add column if not exists uid text unique,
  add column if not exists approved boolean default false;

create table if not exists public.noethara_tool_calls (
  id bigserial primary key,
  tool text not null,
  args jsonb default '{}'::jsonb,
  ok boolean default true,
  error text,
  user_email text,
  created_at timestamptz default now()
);

create index if not exists noethara_clips_job_id_idx on public.noethara_clips(job_id);
create index if not exists noethara_jobs_channel_idx on public.noethara_jobs(channel);
create index if not exists noethara_live_sessions_state_idx on public.noethara_live_sessions(state);
create index if not exists noethara_publications_status_idx on public.noethara_publications(status);
create index if not exists noethara_tool_calls_tool_idx on public.noethara_tool_calls(tool);

alter table public.noethara_jobs enable row level security;
alter table public.noethara_clips enable row level security;
alter table public.noethara_live_sessions enable row level security;
alter table public.noethara_publications enable row level security;
alter table public.noethara_tool_calls enable row level security;

-- The FastAPI backend uses the service_role key, which bypasses RLS.
-- No anon/client policies are created: the dashboard never queries these tables directly.
