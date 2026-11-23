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

create index if not exists noethara_clips_job_id_idx on public.noethara_clips(job_id);
create index if not exists noethara_jobs_channel_idx on public.noethara_jobs(channel);

alter table public.noethara_jobs enable row level security;
alter table public.noethara_clips enable row level security;

-- The FastAPI backend uses the service_role key, which bypasses RLS.
-- No anon/client policies are created: the dashboard never queries these tables directly.
