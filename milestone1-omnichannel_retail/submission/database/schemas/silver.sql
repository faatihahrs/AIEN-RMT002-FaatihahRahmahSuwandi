CREATE SCHEMA IF NOT EXISTS silver;

CREATE TABLE IF NOT EXISTS silver.customers (
    customer_id TEXT PRIMARY KEY,
    city_id TEXT NOT NULL,
    created_at_utc TIMESTAMPTZ NOT NULL,
    customer_segment TEXT NOT NULL,
    source_row_id TEXT NOT NULL UNIQUE,
    bronze_record_id BIGINT NOT NULL UNIQUE,
    pipeline_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS silver.customer_profiles (
    source_row_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    city_id TEXT NOT NULL,
    customer_segment TEXT NOT NULL,
    valid_from_utc TIMESTAMPTZ NOT NULL,
    valid_to_utc TIMESTAMPTZ,
    bronze_record_id BIGINT NOT NULL UNIQUE,
    pipeline_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS silver.customer_addresses (
    address_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    city_id TEXT NOT NULL,
    valid_from_utc TIMESTAMPTZ NOT NULL,
    valid_to_utc TIMESTAMPTZ,
    source_row_id TEXT NOT NULL UNIQUE,
    bronze_record_id BIGINT NOT NULL UNIQUE,
    pipeline_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS silver.products (
    product_id TEXT PRIMARY KEY,
    product_name TEXT NOT NULL,
    sku TEXT NOT NULL UNIQUE,
    unit_price NUMERIC(14,2) NOT NULL,
    source_row_id TEXT NOT NULL UNIQUE,
    bronze_record_id BIGINT NOT NULL UNIQUE,
    pipeline_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS silver.product_categories (
    source_row_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    category_id TEXT NOT NULL,
    category_name TEXT NOT NULL,
    valid_from_utc TIMESTAMPTZ NOT NULL,
    valid_to_utc TIMESTAMPTZ,
    bronze_record_id BIGINT NOT NULL UNIQUE,
    pipeline_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS silver.stores (
    store_id TEXT PRIMARY KEY,
    store_name TEXT NOT NULL,
    city_id TEXT NOT NULL,
    location_type TEXT NOT NULL,
    source_row_id TEXT NOT NULL UNIQUE,
    bronze_record_id BIGINT NOT NULL UNIQUE,
    pipeline_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS silver.sales_channels (
    channel_id TEXT PRIMARY KEY,
    channel_name TEXT NOT NULL UNIQUE,
    source_row_id TEXT NOT NULL UNIQUE,
    bronze_record_id BIGINT NOT NULL UNIQUE,
    pipeline_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS silver.promotions (
    promotion_id TEXT PRIMARY KEY,
    promotion_code TEXT NOT NULL,
    promotion_type TEXT NOT NULL,
    discount_rate NUMERIC(14,4) NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    source_row_id TEXT NOT NULL UNIQUE,
    bronze_record_id BIGINT NOT NULL UNIQUE,
    pipeline_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS silver.orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    ordered_at_utc TIMESTAMPTZ NOT NULL,
    updated_at_utc TIMESTAMPTZ NOT NULL,
    sales_channel TEXT NOT NULL,
    store_id TEXT,
    shipping_revenue NUMERIC(14,2) NOT NULL,
    order_status TEXT NOT NULL,
    source_row_id TEXT NOT NULL,
    bronze_record_id BIGINT NOT NULL UNIQUE,
    pipeline_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS silver.order_items (
    order_item_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price NUMERIC(14,2) NOT NULL CHECK (unit_price >= 0),
    item_discount_amount NUMERIC(14,2) NOT NULL
        CHECK (item_discount_amount >= 0),
    source_row_id TEXT NOT NULL UNIQUE,
    bronze_record_id BIGINT NOT NULL UNIQUE,
    pipeline_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS silver.order_promotions (
    order_id TEXT NOT NULL,
    promotion_id TEXT NOT NULL,
    discount_amount NUMERIC(14,2) NOT NULL
        CHECK (discount_amount >= 0),
    source_row_id TEXT NOT NULL UNIQUE,
    bronze_record_id BIGINT NOT NULL UNIQUE,
    pipeline_run_id TEXT NOT NULL,
    PRIMARY KEY (order_id, promotion_id)
);

CREATE TABLE IF NOT EXISTS silver.payment_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    occurred_at_utc TIMESTAMPTZ NOT NULL,
    ingested_at_utc TIMESTAMPTZ NOT NULL,
    payment_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    amount NUMERIC(14,2) NOT NULL CHECK (amount >= 0),
    bronze_record_id BIGINT NOT NULL UNIQUE,
    pipeline_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS silver.refund_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    occurred_at_utc TIMESTAMPTZ NOT NULL,
    ingested_at_utc TIMESTAMPTZ NOT NULL,
    refund_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    amount NUMERIC(14,2) NOT NULL CHECK (amount >= 0),
    bronze_record_id BIGINT NOT NULL UNIQUE,
    pipeline_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS silver.return_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    occurred_at_utc TIMESTAMPTZ NOT NULL,
    ingested_at_utc TIMESTAMPTZ NOT NULL,
    return_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    bronze_record_id BIGINT NOT NULL UNIQUE,
    pipeline_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS silver.support_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    occurred_at_utc TIMESTAMPTZ NOT NULL,
    ingested_at_utc TIMESTAMPTZ NOT NULL,
    ticket_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    order_id TEXT,
    reason TEXT,
    bronze_record_id BIGINT NOT NULL UNIQUE,
    pipeline_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS silver.web_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    occurred_at_utc TIMESTAMPTZ NOT NULL,
    ingested_at_utc TIMESTAMPTZ NOT NULL,
    session_id TEXT NOT NULL,
    customer_id TEXT,
    order_id TEXT,
    channel TEXT NOT NULL,
    campaign_id TEXT,
    bronze_record_id BIGINT NOT NULL UNIQUE,
    pipeline_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS silver.inventory_snapshots (
    product_id TEXT NOT NULL,
    location_id TEXT NOT NULL,
    snapshot_date DATE NOT NULL,
    available_quantity INTEGER NOT NULL,
    reserved_quantity INTEGER NOT NULL,
    unit_cost NUMERIC(14,2) NOT NULL,
    bronze_record_id BIGINT NOT NULL UNIQUE,
    pipeline_run_id TEXT NOT NULL,
    PRIMARY KEY (product_id, location_id, snapshot_date)
);

CREATE TABLE IF NOT EXISTS silver.campaign_spend (
    spend_date DATE NOT NULL,
    campaign_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    spend_amount NUMERIC(14,2) NOT NULL CHECK (spend_amount >= 0),
    bronze_record_id BIGINT NOT NULL UNIQUE,
    pipeline_run_id TEXT NOT NULL,
    PRIMARY KEY (spend_date, campaign_id, channel)
);

CREATE TABLE IF NOT EXISTS silver.city_reference (
    city_id TEXT PRIMARY KEY,
    city_name TEXT NOT NULL,
    country TEXT NOT NULL,
    bronze_record_id BIGINT NOT NULL UNIQUE,
    pipeline_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS silver.rejected_records (
    rejection_id BIGSERIAL PRIMARY KEY,
    bronze_record_id BIGINT NOT NULL,
    source_file TEXT NOT NULL,
    source_record_id TEXT,
    rejection_reason TEXT NOT NULL,
    rejection_details JSONB NOT NULL DEFAULT '{}'::JSONB,
    pipeline_run_id TEXT NOT NULL,
    rejected_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (bronze_record_id, rejection_reason)
);

CREATE INDEX IF NOT EXISTS idx_silver_profiles_temporal
    ON silver.customer_profiles(customer_id, valid_from_utc, valid_to_utc);

CREATE INDEX IF NOT EXISTS idx_silver_categories_temporal
    ON silver.product_categories(product_id, valid_from_utc, valid_to_utc);

CREATE INDEX IF NOT EXISTS idx_silver_orders_customer_date
    ON silver.orders(customer_id, ordered_at_utc);

CREATE INDEX IF NOT EXISTS idx_silver_items_order
    ON silver.order_items(order_id);

CREATE INDEX IF NOT EXISTS idx_silver_payments_order
    ON silver.payment_events(order_id, occurred_at_utc);

CREATE INDEX IF NOT EXISTS idx_silver_refunds_order
    ON silver.refund_events(order_id, occurred_at_utc);

CREATE INDEX IF NOT EXISTS idx_silver_returns_order
    ON silver.return_events(order_id, occurred_at_utc);

CREATE INDEX IF NOT EXISTS idx_silver_support_customer
    ON silver.support_events(customer_id, occurred_at_utc);

CREATE INDEX IF NOT EXISTS idx_silver_web_campaign
    ON silver.web_events(campaign_id, channel, occurred_at_utc);

CREATE INDEX IF NOT EXISTS idx_silver_rejected_source
    ON silver.rejected_records(source_file, rejection_reason);
