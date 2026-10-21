-- Kër Carto · catalogue foncier des lotissements et manifestations d'intérêt
-- Appliqué par Claude via la console SQL du projet (hors migrations drizzle).

create table if not exists public.subdivisions (
  id uuid primary key default gen_random_uuid(),
  program_id uuid not null unique references public.programs(id) on delete cascade,
  name text not null,
  maitre_ouvrage text,
  acte_ref text,
  acte_date date,
  legal_status text not null default 'approuvé'
    check (legal_status in ('approuvé','en cours d''approbation','suspendu – audit foncier','mainlevée en cours')),
  area_m2 numeric,
  parcel_count int,
  viabilisation jsonb not null default '{}'::jsonb,
  perimeter jsonb not null,
  minlon double precision, minlat double precision, maxlon double precision, maxlat double precision,
  lon double precision, lat double precision,
  plan_pdf_url text,
  contact_phone text,
  is_demo boolean not null default false,
  created_at timestamptz not null default now()
);

create table if not exists public.blocks (
  id uuid primary key default gen_random_uuid(),
  subdivision_id uuid not null references public.subdivisions(id) on delete cascade,
  num text not null,
  usage text not null default 'habitation' check (usage in ('habitation','équipement','espace vert','commerce','mixte')),
  geom jsonb not null
);
create index if not exists blocks_sub_idx on public.blocks(subdivision_id);

create table if not exists public.parcels (
  id uuid primary key default gen_random_uuid(),
  subdivision_id uuid not null references public.subdivisions(id) on delete cascade,
  block_id uuid references public.blocks(id) on delete set null,
  ref text not null,
  lot_num int,
  usage text not null default 'habitation' check (usage in ('habitation','commerce','équipement','mixte','espace vert')),
  area_m2 numeric not null,
  frontage_m numeric, depth_m numeric,
  price numeric, price_m2 numeric,
  status text not null check (status in ('disponible immédiatement','potentiellement disponible','disponible à terme','réservée','attribuée','non disponible')),
  raccordements jsonb not null default '{}'::jsonb,
  phase int not null default 1,
  lon double precision not null, lat double precision not null,
  geom jsonb not null,
  is_demo boolean not null default false,
  updated_at timestamptz not null default now()
);
create index if not exists parcels_sub_idx on public.parcels(subdivision_id);
create index if not exists parcels_status_idx on public.parcels(status);

create table if not exists public.networks (
  id uuid primary key default gen_random_uuid(),
  subdivision_id uuid not null references public.subdivisions(id) on delete cascade,
  kind text not null check (kind in ('voirie_principale','voirie_secondaire','eau','electricite_mt','electricite_bt','assainissement','fibre')),
  label text,
  etat text not null default 'réalisé' check (etat in ('réalisé','en cours','programmé')),
  geom jsonb not null
);
create index if not exists networks_sub_idx on public.networks(subdivision_id);

-- Gestionnaires (commune, agence, opérateur public) d'un programme
create table if not exists public.program_managers (
  user_id uuid not null references auth.users(id) on delete cascade,
  program_id uuid not null references public.programs(id) on delete cascade,
  role text not null default 'gestionnaire',
  created_at timestamptz not null default now(),
  primary key (user_id, program_id)
);

create or replace function public.is_program_manager(p_program uuid)
returns boolean language sql stable security definer set search_path = public as $$
  select exists (select 1 from program_managers where user_id = auth.uid() and program_id = p_program)
$$;

-- Manifestations d'intérêt
create sequence if not exists public.interest_ref_seq;

create table if not exists public.interests (
  id uuid primary key default gen_random_uuid(),
  reference text unique,
  target_type text not null check (target_type in ('parcel','program','offer')),
  parcel_id uuid references public.parcels(id) on delete set null,
  program_id uuid references public.programs(id) on delete set null,
  offer_id uuid references public.offers(id) on delete set null,
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  applicant jsonb not null,          -- {nom, telephone, email, type: particulier|entreprise|diaspora|cooperative}
  project jsonb not null default '{}'::jsonb, -- {usage, surface_batie, budget, financement}
  schedule text,
  network_needs text[] not null default '{}',
  message text,
  consent boolean not null check (consent = true),
  status text not null default 'reçue' check (status in ('reçue','en examen','réponse envoyée','close')),
  manager_note text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check ((target_type = 'parcel' and parcel_id is not null) or (target_type = 'program' and program_id is not null) or (target_type = 'offer' and offer_id is not null))
);
create index if not exists interests_user_idx on public.interests(user_id);
create index if not exists interests_program_idx on public.interests(program_id);

-- Référence, programme rattaché, limite de 5 par jour et par utilisateur
create or replace function public.interests_before_insert()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  if (select count(*) from interests where user_id = new.user_id and created_at > now() - interval '24 hours') >= 5 then
    raise exception 'Limite atteinte : 5 manifestations d''intérêt par jour.';
  end if;
  new.reference := 'KC-MI-' || to_char(now(), 'YYYY') || '-' || lpad(nextval('interest_ref_seq')::text, 6, '0');
  if new.target_type = 'parcel' then
    select s.program_id into new.program_id from parcels p join subdivisions s on s.id = p.subdivision_id where p.id = new.parcel_id;
  end if;
  new.status := 'reçue';
  new.manager_note := null;
  return new;
end $$;
drop trigger if exists interests_bi on public.interests;
create trigger interests_bi before insert on public.interests for each row execute function public.interests_before_insert();

create or replace function public.touch_updated_at()
returns trigger language plpgsql as $$ begin new.updated_at := now(); return new; end $$;
drop trigger if exists interests_bu on public.interests;
create trigger interests_bu before update on public.interests for each row execute function public.touch_updated_at();
drop trigger if exists parcels_bu on public.parcels;
create trigger parcels_bu before update on public.parcels for each row execute function public.touch_updated_at();

-- Les demandeurs ne modifient que le message ; les gestionnaires le statut et la note
create or replace function public.interests_guard_update()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  if public.is_program_manager(old.program_id) then
    new.applicant := old.applicant; new.project := old.project; new.user_id := old.user_id;
    new.reference := old.reference; new.target_type := old.target_type; new.parcel_id := old.parcel_id;
    new.program_id := old.program_id; new.offer_id := old.offer_id; new.consent := old.consent;
  else
    new.status := old.status; new.manager_note := old.manager_note; new.reference := old.reference;
    new.user_id := old.user_id; new.program_id := old.program_id; new.parcel_id := old.parcel_id;
  end if;
  return new;
end $$;
drop trigger if exists interests_guard on public.interests;
create trigger interests_guard before update on public.interests for each row execute function public.interests_guard_update();

-- RLS
alter table public.subdivisions enable row level security;
alter table public.blocks enable row level security;
alter table public.parcels enable row level security;
alter table public.networks enable row level security;
alter table public.program_managers enable row level security;
alter table public.interests enable row level security;

drop policy if exists "lecture publique" on public.subdivisions;
create policy "lecture publique" on public.subdivisions for select using (true);
drop policy if exists "lecture publique" on public.blocks;
create policy "lecture publique" on public.blocks for select using (true);
drop policy if exists "lecture publique" on public.parcels;
create policy "lecture publique" on public.parcels for select using (true);
drop policy if exists "lecture publique" on public.networks;
create policy "lecture publique" on public.networks for select using (true);

drop policy if exists "gestionnaire modifie ses parcelles" on public.parcels;
create policy "gestionnaire modifie ses parcelles" on public.parcels for update to authenticated
  using (exists (select 1 from subdivisions s where s.id = subdivision_id and public.is_program_manager(s.program_id)))
  with check (exists (select 1 from subdivisions s where s.id = subdivision_id and public.is_program_manager(s.program_id)));

drop policy if exists "voir ses rôles" on public.program_managers;
create policy "voir ses rôles" on public.program_managers for select to authenticated using (user_id = auth.uid());

drop policy if exists "créer sa demande" on public.interests;
create policy "créer sa demande" on public.interests for insert to authenticated with check (user_id = auth.uid() and consent);
drop policy if exists "lire ses demandes ou celles de ses programmes" on public.interests;
create policy "lire ses demandes ou celles de ses programmes" on public.interests for select to authenticated
  using (user_id = auth.uid() or public.is_program_manager(program_id));
drop policy if exists "modifier ses demandes ou celles de ses programmes" on public.interests;
create policy "modifier ses demandes ou celles de ses programmes" on public.interests for update to authenticated
  using (user_id = auth.uid() or public.is_program_manager(program_id))
  with check (user_id = auth.uid() or public.is_program_manager(program_id));

-- Statistiques par lotissement
create or replace function public.subdivision_stats(p_sub uuid)
returns table(status text, n bigint, surface numeric) language sql stable as $$
  select status, count(*), sum(area_m2) from parcels where subdivision_id = p_sub group by status
$$;

grant select on public.subdivisions, public.blocks, public.parcels, public.networks to anon, authenticated;
grant update on public.parcels to authenticated;
grant select, insert, update on public.interests to authenticated;
grant select on public.program_managers to authenticated;
grant usage on sequence public.interest_ref_seq to authenticated;
grant execute on function public.subdivision_stats(uuid) to anon, authenticated;
grant execute on function public.is_program_manager(uuid) to authenticated;
