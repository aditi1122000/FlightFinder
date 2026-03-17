-- Run this in Supabase SQL Editor to create the table for conversation persistence.
-- Table: one row per message (user or assistant). turn_index pairs user + assistant per turn.

create table if not exists conversation_messages (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null,
  user_name text,
  turn_index integer not null default 1,
  role text not null check (role in ('user', 'assistant')),
  content text not null,
  slots jsonb,
  created_at timestamptz not null default now()
);


create index if not exists idx_conversation_messages_conversation_id

create index if not exists idx_conversation_messages_turn
  on conversation_messages (conversation_id, turn_index);

-- alter table conversation_messages add column if not exists turn_index integer not null default 1;
-- alter table conversation_messages add column if not exists user_name text;

-- Optional: index for listing messages by user (for summarisation agent)
create index if not exists idx_conversation_messages_user_name_created
  on conversation_messages (user_name, created_at)
  where user_name is not null;

-- User summaries (intent profiling): one row per user_name, updated by summarisation agent
create table if not exists user_summaries (
  user_name text primary key,
  summary_text text not null default '',
  last_summarised_at timestamptz,
  updated_at timestamptz not null default now()
);

-- Optional: RLS (Row Level Security) - enable if you use anon key and want per-user isolation
-- alter table conversation_messages enable row level security;
-- create policy "Users can manage own messages" on conversation_messages
--   for all using (auth.uid()::text = conversation_id::text);
