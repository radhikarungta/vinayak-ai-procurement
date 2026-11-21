-- Demo emails that "landed" later (procurement/cases/arrivals.py), so every server instance sees them.
create table if not exists public.arrivals (
  uid        text primary key,
  rec        jsonb not null,
  created_at timestamptz not null default now()
);
alter table public.arrivals enable row level security;
