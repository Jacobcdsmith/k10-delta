-- DELTA Supabase schema
-- Run this once in your Supabase SQL editor (https://supabase.com/dashboard)

create table if not exists delta_soul (
  agent_id   text primary key,
  identity   text,
  boot_count int  default 0,
  data       jsonb,
  updated_at timestamptz default now()
);

create table if not exists delta_goals (
  id         text  not null,
  agent_id   text  not null,
  text       text,
  status     text,
  priority   int   default 0,
  data       jsonb,
  created_at timestamptz,
  updated_at timestamptz default now(),
  primary key (agent_id, id)
);

create table if not exists delta_episodes (
  id          uuid default gen_random_uuid() primary key,
  agent_id    text not null,
  ts          timestamptz,
  summary     text,
  data        jsonb,
  inserted_at timestamptz default now()
);

create index if not exists delta_episodes_ts  on delta_episodes (agent_id, ts desc);
create index if not exists delta_goals_status on delta_goals (agent_id, status);

-- Enable RLS if you want per-user isolation (optional)
-- alter table delta_soul     enable row level security;
-- alter table delta_goals    enable row level security;
-- alter table delta_episodes enable row level security;
