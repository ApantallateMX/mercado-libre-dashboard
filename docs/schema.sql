-- docs/schema.sql
--
-- Referencia de schema para las tablas del Manual de Usuario (§17a) y
-- Novedades (§17b). Este archivo NO se ejecuta en producción -- el schema
-- real y autoritativo lo crea init_db() en app/services/token_store.py con
-- CREATE TABLE IF NOT EXISTS (patrón real de este proyecto, ver
-- docs/developer-manual.md). Este .sql es un espejo de solo lectura para que
-- el schema quede documentado también en formato SQL estándar.
--
-- Ver docs/developer-manual.md § "Sistema (documentación y novedades)" para
-- el detalle de negocio de cada columna.

CREATE TABLE IF NOT EXISTS doc_categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    slug        TEXT UNIQUE NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS doc_pages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id  INTEGER NOT NULL REFERENCES doc_categories(id),
    title        TEXT NOT NULL,
    slug         TEXT UNIQUE NOT NULL,
    content      TEXT NOT NULL DEFAULT '',
    sort_order   INTEGER NOT NULL DEFAULT 0
);

-- Para referencia cruzada -- mismo patrón, viven en token_store.py:
--
-- CREATE TABLE IF NOT EXISTS changelog_entries (
--     id            INTEGER PRIMARY KEY AUTOINCREMENT,
--     version       TEXT NOT NULL,
--     release_date  TEXT NOT NULL,
--     title         TEXT NOT NULL,
--     content       TEXT NOT NULL,
--     category      TEXT NOT NULL DEFAULT 'improvement',
--     priority      INTEGER NOT NULL DEFAULT 0,
--     is_published  INTEGER NOT NULL DEFAULT 1,
--     published_at  TEXT NOT NULL DEFAULT '',
--     created_at    REAL NOT NULL DEFAULT 0
-- );
