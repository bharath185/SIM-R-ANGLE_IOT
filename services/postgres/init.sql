-- Configuration key-value store
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Track operator login sessions


-- Roles table
CREATE TABLE IF NOT EXISTS roles (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) UNIQUE NOT NULL,
    description TEXT
);

-- User management tables
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    hashed_password TEXT NOT NULL,
    full_name VARCHAR(100),
    is_active BOOLEAN DEFAULT TRUE,
    role_id INTEGER REFERENCES roles(id),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shift_master (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    description TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS access_policies (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    resource TEXT NOT NULL,
    can_read BOOLEAN DEFAULT FALSE,
    can_write BOOLEAN DEFAULT FALSE,
    can_delete BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Log every step of MES/Trace processing
CREATE TABLE IF NOT EXISTS mes_trace_history (
    id SERIAL PRIMARY KEY,
    serial TEXT NOT NULL,
    step TEXT NOT NULL,  -- e.g., mes_pc, trace_pc, interlock, etc.
    response_json JSONB NOT NULL,
    ts TIMESTAMP DEFAULT NOW()
);

-- Final scan audit trail
CREATE TABLE IF NOT EXISTS scan_audit (
    id SERIAL PRIMARY KEY,
    serial TEXT NOT NULL,
    operator TEXT NOT NULL,
    result TEXT NOT NULL,  -- "pass", "fail", "scrap"
    ts TIMESTAMP DEFAULT NOW()
);

-- Generic error logs for troubleshooting
CREATE TABLE IF NOT EXISTS error_logs (
    id SERIAL PRIMARY KEY,
    context TEXT NOT NULL,          -- e.g., "scan", "login", etc.
    error_msg TEXT NOT NULL,
    details JSONB,
    ts TIMESTAMP DEFAULT NOW()
);

-- Initial config values (optional insert)
INSERT INTO config (key, value) VALUES
('MACHINE_ID', 'MACHINE_XYZ'),
('CBS_STREAM_NAME', 'line1'),
('OPERATOR_IDS', 'op1,op2,op3'),
('MES_OPERATOR_LOGIN_URL', 'http://mes-server/api/login'),
('MES_PROCESS_CONTROL_URL', 'http://mes-server/api/pc'),
('MES_UPLOAD_URL', 'http://mes-server/api/upload'),
('TRACE_PROXY_HOST', 'trace-proxy'),
('FAILURE_REASON_CODES', 'F001,F002'),
('NCM_REASON_CODES', 'NCM1,NCM2'),
('INFLUXDB_URL', 'http://influxdb:8086'),
('INFLUXDB_TOKEN', 'edgetoken'),
('INFLUXDB_ORG', 'EdgeOrg'),
('INFLUXDB_BUCKET', 'EdgeBucket'),
('KAFKA_BROKER', 'kafka:9092')
ON CONFLICT (key) DO NOTHING;

INSERT INTO roles (name, description) VALUES 
('admin', 'Administrator with full access'),
('engineer', 'Technical staff with system access'),
('operator', 'Machine operator with limited access')
ON CONFLICT (name) DO NOTHING;

-- Initial admin user (optional)
-- Password should be properly hashed in production
