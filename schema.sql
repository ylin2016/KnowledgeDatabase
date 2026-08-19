-- KnowledgeDatabase — property onboarding sheets -> Neon Postgres
--
-- Shape mirrors the source workbook: one workbook per property, whose
-- "Property Info" tab holds one COLUMN per listing (Main / Child 1 / Child 2).
-- So a property has many listings, and nearly every attribute is per-listing.
--
-- The Summary tab is derived from the other tabs and is deliberately not stored.

create extension if not exists pgcrypto;
create extension if not exists pg_trgm;

-- ---------------------------------------------------------------- owners

create table if not exists owners (
    owner_id    bigserial primary key,
    full_name   text not null,
    email       text,
    phone       text,
    comm_method text,          -- "Preferred Communication Method" row 1 (Wechat, SMS, ...)
    comm_handle text,          -- row 2 of the same label: the actual wechat id / handle
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- Owners are deduped on name+email; email is missing often enough that it
-- cannot carry the constraint alone.
create unique index if not exists owners_identity_idx
    on owners (lower(full_name), coalesce(lower(email), ''));

-- ------------------------------------------------------------ properties

create table if not exists properties (
    property_id        bigserial primary key,
    nickname           text not null unique,   -- "Seattle 10057"
    address            text,
    property_type      text,
    nwmls_id           text,

    -- provenance: which Drive workbook this row was built from
    source_file_id     text,
    source_file_name   text,
    source_modified_at timestamptz,
    synced_at          timestamptz,

    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now()
);

create table if not exists property_owners (
    property_id bigint not null references properties (property_id) on delete cascade,
    owner_id    bigint not null references owners (owner_id) on delete cascade,
    ordinal     int    not null,   -- 1 = "Owner", 2 = "Owner 2", ...
    primary key (property_id, owner_id)
);

-- -------------------------------------------------------------- listings

create table if not exists listings (
    listing_id     bigserial primary key,
    property_id    bigint not null references properties (property_id) on delete cascade,
    ordinal        int    not null,   -- 0 = Main listing, 1..n = Child listing n
    role           text   not null check (role in ('main', 'child')),

    nickname       text,              -- "Seattle 10057 Lower"
    title          text,              -- public listing title
    rental_type    text,              -- STR / MTR / LTR
    address        text,
    property_type  text,
    account_link   text,
    listing_3d_link text,
    house_manual_link text,

    square_footage numeric,
    bedrooms       numeric,
    bathrooms      numeric,
    sleeps         int,

    cleaning_fee   numeric(10, 2),    -- charged to guest
    cleaning_cost  numeric(10, 2),    -- paid out to cleaner

    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),

    unique (property_id, ordinal)
);

-- Exactly one main listing per property.
create unique index if not exists listings_one_main_idx
    on listings (property_id) where role = 'main';

-- --------------------------------------------------- per-listing attributes

-- Everything in the Property Info tab that is not promoted to a listings
-- column. Long free text, and the template gains fields over time, so this
-- stays a narrow table rather than 60 more columns.
create table if not exists listing_attrs (
    listing_id  bigint not null references listings (listing_id) on delete cascade,
    field       text   not null,   -- normalized full label, the join key
    category    text,              -- "Maintenance"
    subcategory text,              -- "WiFi"
    label       text,              -- "Internet name"
    value       text,
    updated_at  timestamptz not null default now(),
    primary key (listing_id, field)
);

create index if not exists listing_attrs_category_idx
    on listing_attrs (category, subcategory);

-- Trigram index so "which houses mention a circuit breaker problem" is a
-- cheap query rather than a full scan.
create index if not exists listing_attrs_value_trgm_idx
    on listing_attrs using gin (value gin_trgm_ops);

-- --------------------------------------------------------------- secrets

-- Door codes, WiFi passwords, utility portal logins. Encrypted with
-- pgp_sym_encrypt() under SECRETS_KEY, which lives in .env and is NEVER
-- stored in this database — a dump of Neon alone does not expose these.
create table if not exists listing_secrets (
    listing_id  bigint not null references listings (listing_id) on delete cascade,
    field       text   not null,
    category    text,
    subcategory text,
    label       text,
    value_enc   bytea  not null,
    updated_at  timestamptz not null default now(),
    primary key (listing_id, field)
);

-- ----------------------------------------------------------------- views

-- Rebuilds the workbook's read-only Summary tab from stored data, so the
-- derived tab never has to be trusted or imported.
create or replace view listing_summary as
select
    p.nickname       as property_nickname,
    p.address        as property_address,
    p.property_type,
    l.ordinal,
    l.role,
    l.nickname       as listing_nickname,
    l.title          as listing_title,
    l.rental_type,
    l.bedrooms,
    l.bathrooms,
    l.sleeps,
    l.cleaning_fee,
    p.synced_at
from properties p
join listings l using (property_id)
order by p.nickname, l.ordinal;
