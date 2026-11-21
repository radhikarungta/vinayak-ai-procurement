-- One row per requirement email: its whole workflow state (decisions, replies, picks, quotes, agent runs).
-- Run once in the Supabase SQL editor. The app uses the service-role key from the server only.
create table if not exists public.case_state (
  uid        text primary key,
  state      jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

create or replace function public.case_state_touch() returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end $$;

drop trigger if exists case_state_touch on public.case_state;
create trigger case_state_touch before update on public.case_state
  for each row execute function public.case_state_touch();

-- No public access: row level security on, no policies. The service-role key bypasses it.
alter table public.case_state enable row level security;
