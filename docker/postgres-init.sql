-- Runs once, when the local Postgres volume is first created. Dev/test only.
-- Supabase owns the real auth schema; this is the minimum our foreign keys need.
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE IF NOT EXISTS auth.users (
    id           uuid PRIMARY KEY,
    email        varchar(255),
    banned_until timestamptz
);

-- Separate database for pytest, so tests never touch dev data (or Supabase).
CREATE DATABASE veda_test;
\connect veda_test
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE IF NOT EXISTS auth.users (
    id           uuid PRIMARY KEY,
    email        varchar(255),
    banned_until timestamptz
);
