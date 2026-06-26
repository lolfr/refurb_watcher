-- version: 0.1.0
-- refurb_watcher schema MariaDB
-- MariaDB backend only: apply once to your database
-- (the default SQLite backend auto-creates these tables).

-- Produits actuellement listés (snapshot courant)
CREATE TABLE IF NOT EXISTS refurb_snapshot (
    part_number   VARCHAR(64)  NOT NULL,
    category      VARCHAR(64)  NOT NULL,
    title         VARCHAR(512) NOT NULL,
    price_cents   INT          NOT NULL,
    list_price_cents INT       NULL,
    savings_cents INT          NULL,
    currency      CHAR(3)      NOT NULL DEFAULT 'EUR',
    url           VARCHAR(1024) NOT NULL,
    ram_gb        INT          NULL,
    storage_gb    INT          NULL,
    chip          VARCHAR(64)  NULL,
    cpu_cores     INT          NULL,
    gpu_cores     INT          NULL,
    raw_specs     TEXT         NULL,
    first_seen    DATETIME     NOT NULL,
    last_seen     DATETIME     NOT NULL,
    PRIMARY KEY (part_number),
    INDEX idx_category (category),
    INDEX idx_last_seen (last_seen)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Historique des apparitions (chaque NOUVELLE apparition = 1 ligne)
-- Utile pour l'analyse : combien de temps un produit reste dispo, fréquence d'apparition, etc.
CREATE TABLE IF NOT EXISTS refurb_events (
    id            BIGINT       NOT NULL AUTO_INCREMENT,
    part_number   VARCHAR(64)  NOT NULL,
    event_type    ENUM('appeared', 'disappeared', 'price_changed') NOT NULL,
    seen_at       DATETIME     NOT NULL,
    price_cents   INT          NULL,
    title         VARCHAR(512) NULL,
    notified      TINYINT(1)   NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    INDEX idx_part (part_number),
    INDEX idx_seen (seen_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Santé du scraper (un poll = 1 ligne)
CREATE TABLE IF NOT EXISTS refurb_polls (
    id            BIGINT       NOT NULL AUTO_INCREMENT,
    polled_at     DATETIME     NOT NULL,
    url           VARCHAR(1024) NOT NULL,
    http_status   INT          NULL,
    products_found INT         NULL,
    error         TEXT         NULL,
    duration_ms   INT          NULL,
    PRIMARY KEY (id),
    INDEX idx_polled (polled_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
