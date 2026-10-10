# E-Commerce Demo API Server

A FastAPI API server for a demo e-commerce store backed by IBM Db2 LUW 11.5. It is built to be
observed: every request and query produces Prometheus metrics and structured JSON logs, and Db2
failures map to distinct, machine-readable error responses.

- Python 3.11, FastAPI + uvicorn, synchronous endpoints (FastAPI's thread pool provides concurrency)
- `ibm_db` driver with a custom connection pool (`app/db/pool.py`)
- `prometheus_client` metrics at `GET /metrics`
- JSON logs on stdout, one access line per request, `X-Request-ID` on every log line and response

## Layout

```
app/
  main.py            FastAPI app, request middleware, exception handlers
  config.py          settings from environment variables
  logging_setup.py   JSON logging, per-request context (request id, timings)
  metrics.py         HTTP metrics
  errors.py          ApiError (4xx responses raised by routes)
  db/pool.py         Db2Pool + pool metrics
  db/queries.py      all SQL, one function per query, query metrics
  db/errors.py       SQLCODE / SQLSTATE / reason code parsing
  routes/            products, orders, customers, health
sql/schema.sql       COMMERCE schema
sql/seed.py          demo data generator
tests/               pytest suite (ibm_db is replaced by tests/fake_ibm_db.py)
deploy/apiserver.service   systemd unit
```

## Configuration

All settings come from environment variables (see `.env.example`).

| Variable | Default | Meaning |
|---|---|---|
| `DB2_HOST` | `localhost` | Db2 server host |
| `DB2_PORT` | `50000` | Db2 server port |
| `DB2_DATABASE` | `COMMERCE` | database name |
| `DB2_USER` | `db2inst1` | user |
| `DB2_PASSWORD` | *(empty)* | password |
| `POOL_MIN_SIZE` | `2` | connections opened at startup |
| `POOL_MAX_SIZE` | `20` | maximum connections checked out at once |
| `POOL_ACQUIRE_TIMEOUT` | `0.005` | seconds to wait for a connection before returning 503. Kept at 5 ms so that when the pool is exhausted the 503s take about as long as normal requests (p95 ≈ 10 ms) and do not move the latency percentiles |
| `APP_PORT` | `8000` | HTTP listen port |
| `LOG_LEVEL` | `INFO` | `DEBUG` also logs each physical connection open/close |
| `LOG_FILE` | *(empty)* | write logs to this file instead of stdout (the systemd unit sets `/var/log/apiserver/apiserver.log`) |

The pool sets the Db2 client application name to `apiserver` on every connection, so its
connections show up as `CLIENT_APPLNAME = 'apiserver'` in `MON_GET_CONNECTION`, `db2top`, and
similar tools.

## Database setup

1. Create the database (as the instance owner):

   ```sh
   db2 create database COMMERCE
   ```

2. Create the schema, either with the CLP:

   ```sh
   db2 connect to COMMERCE
   db2 -tvf sql/schema.sql
   ```

   or let `seed.py` do it; it applies `schema.sql` when the tables do not exist.

3. Load demo data (1,000 customers; 200 products in 8 categories; inventory; about 200,000
   orders over the last 12 months with 1–4 items each):

   ```sh
   pip install -r requirements.txt
   export $(grep -v '^#' .env | xargs)   # or set DB2_* some other way
   python sql/seed.py            # creates tables if needed; loads only if the tables are empty
   python sql/seed.py --reset    # drops all tables, recreates them and reloads
   ```

   Options: `--customers N`, `--orders N`, `--random-seed N`. With the defaults it takes about
   15 seconds against a local Db2 and finishes by running RUNSTATS on the tables.

4. Lock timeouts. Db2's default `LOCKTIMEOUT` is `-1` (wait forever), which means lock contention
   shows up as hung requests rather than SQL0911N. To get `-911` errors, set a finite timeout and
   reactivate the database:

   ```sh
   db2 update db cfg for COMMERCE using LOCKTIMEOUT 10
   db2 force applications all && db2 deactivate db COMMERCE && db2 activate db COMMERCE
   ```

### Throwaway Db2 for local testing

```sh
docker run -d --name db2 --privileged -p 50000:50000 \
  -e LICENSE=accept -e DB2INST1_PASSWORD=changeme -e DBNAME=COMMERCE \
  icr.io/db2_community/db2:11.5.9.0
docker logs -f db2   # wait for "Setup has completed."
```

## Running

### Local

```sh
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit
export $(grep -v '^#' .env | xargs)
python -m app.main
```

If `import ibm_db` fails with `libcrypt.so.1: cannot open shared object file`, the host has no
legacy libcrypt, which the bundled Db2 CLI driver needs. Install it:
Fedora/RHEL `sudo dnf install libxcrypt-compat`; Debian/Ubuntu `sudo apt-get install libcrypt1`.

### Docker

The `ibm_db` wheel bundles the Db2 CLI driver, and the image build fails if `import ibm_db` fails.

```sh
docker build -t apiserver .
docker run --rm -p 8000:8000 --env-file .env apiserver
# seed from the same image:
docker run --rm --env-file .env apiserver python sql/seed.py
```

### systemd

```bash
sudo deploy/install.sh          # installs to /opt/apiserver, enables + starts the service
sudoedit /opt/apiserver/.env    # the unit's EnvironmentFile
sudo systemctl restart apiserver
tail -f /var/log/apiserver/apiserver.log   # follow application logs
journalctl -u apiserver -f                 # startup errors / crash tracebacks
```

Re-run `sudo deploy/install.sh` after pulling changes to sync code and restart.

For log rotation, copy `deploy/apiserver.logrotate` to `/etc/logrotate.d/apiserver` (daily, 14 days kept).

## API

| Method & path | Description |
|---|---|
| `GET /api/products?category=&limit=20&offset=0` | products joined with INVENTORY (`in_stock`) |
| `GET /api/products/{id}` | one product with stock |
| `GET /api/categories` | categories with product and in-stock counts |
| `GET /api/customers?limit=50` | customers with order counts (for the "log in as" picker) |
| `POST /api/orders` | place an order (one transaction; see below) |
| `GET /api/orders?customer_id=&limit=20` | order history with items; without `customer_id`, the most recent orders overall |
| `GET /healthz` | liveness, no DB access |
| `GET /readyz` | runs `SELECT 1 FROM SYSIBM.SYSDUMMY1` through the pool |
| `GET /metrics` | Prometheus metrics |

`POST /api/orders` body: `{"customer_id": 1, "items": [{"product_id": 3, "quantity": 2}]}`.
In one transaction it checks that the customer exists, locks each INVENTORY row
(`SELECT ... FOR UPDATE WITH RS`, in product_id order), checks stock, inserts ORDERS and
ORDER_ITEMS, decrements INVENTORY and commits. Duplicate product ids in `items` are merged.
New orders get status `PLACED`. Returns 201 with the order.

### Error responses

Every error body is JSON with `error` and `request_id`.

| Status | `error` | When |
|---|---|---|
| 404 | `product_not_found`, `customer_not_found`, `not_found` | unknown id or path |
| 409 | `insufficient_stock` | an order line exceeds stock; `items` lists `requested`/`available` |
| 422 | `validation_error` | bad query or body; `details` holds the validation errors |
| 503 | `db_pool_timeout` | no connection available within `POOL_ACQUIRE_TIMEOUT` |
| 503 | `db_lock_timeout` | SQL0911N (SQLCODE -911); includes `sqlcode`, `reason` (68 = lock timeout, 2 = deadlock) and `query` |
| 500 | `db_error` | any other Db2 error; includes `sqlcode`, `sqlstate`, `query` |
| 500 | `internal_error` | unhandled exception |
| 503 | *(readyz)* `not_ready` | `/readyz` could not reach Db2 |

Each Db2 error is logged once at ERROR level by the code that hit it (logger
`apiserver.db.queries` or `apiserver.db.pool`), with `query`, `sqlcode`, `sqlstate`, `reason`,
`request_id` and the full traceback in `exc_info`. If a connection fails with a communication
error (SQL30081N, SQL30108N, SQL1224N, or SQLSTATE class 08), it is closed instead of being
returned to the pool.

## Logs

Logs are JSON, one object per line, on stdout (or in `LOG_FILE` when set). Every line has `timestamp`, `level`, `logger`,
`message` and `request_id` (`null` outside a request). The request id comes from the incoming
`X-Request-ID` header when it is present and well-formed (up to 128 characters from
`[A-Za-z0-9._:-]`); otherwise one is generated. It is echoed back in the `X-Request-ID` response
header.

Access log line (logger `apiserver.access`):

```json
{"timestamp": "2026-09-30T21:02:43+0000", "level": "INFO", "logger": "apiserver.access",
 "message": "request completed", "request_id": "lock-demo2", "method": "POST",
 "route": "/api/orders", "status": 503, "duration_ms": 5017.887,
 "pool_acquire_ms": 0.037, "db_ms": 5013.829}
```

`pool_acquire_ms` is the time spent waiting for pool connections, and `db_ms` is the total time
spent in Db2 calls (prepare, execute, fetch, commit, rollback) during the request.

## Metrics

HTTP (`app/metrics.py`). `route` is the route template (for example `/api/products/{id}`), or
`unmatched` for unknown paths.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `apiserver_http_requests_total` | counter | `method`, `route`, `status` | completed HTTP requests |
| `apiserver_http_request_duration_seconds` | histogram | `method`, `route` | request latency, from middleware entry to response |

Connection pool (`app/db/pool.py`):

| Metric | Type | Meaning |
|---|---|---|
| `apiserver_db_pool_in_use` | gauge | connections currently checked out |
| `apiserver_db_pool_idle` | gauge | idle connections held by the pool |
| `apiserver_db_pool_waiters` | gauge | threads currently waiting in `acquire()` |
| `apiserver_db_pool_max_size` | gauge | configured `POOL_MAX_SIZE` (static) |
| `apiserver_db_pool_acquire_seconds` | histogram | time spent in `acquire()`, including timeouts (buckets 1ms–5s) |
| `apiserver_db_pool_connections_created_total` | counter | physical Db2 connections opened |
| `apiserver_db_pool_connections_closed_total` | counter | physical Db2 connections closed |
| `apiserver_db_pool_acquire_timeouts_total` | counter | acquires that gave up after `POOL_ACQUIRE_TIMEOUT` (each one is a 503) |

Queries (`app/db/queries.py`). `query` is the name of the function in `queries.py`.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `apiserver_db_query_seconds` | histogram | `query` | prepare + execute + fetch time per statement; `commit` and `rollback` are also recorded |
| `apiserver_db_errors_total` | counter | `query`, `sqlcode` | Db2 errors by query and SQLCODE (`unknown` if it could not be parsed) |

Query names: `ping`, `list_categories`, `list_products`, `list_products_by_category`,
`get_product`, `list_customers`, `customer_exists`, `lock_inventory`, `get_product_prices`,
`insert_order`, `insert_order_item`, `decrement_inventory`, `list_orders_for_customer`,
`list_recent_orders`, `get_order_items`, `commit`, `rollback`.

The standard `prometheus_client` process and Python GC metrics are exported too.

## Example calls

```sh
B=http://localhost:8000

curl -s $B/healthz
curl -s $B/readyz
curl -s "$B/api/categories"
curl -s "$B/api/products?category=Electronics&limit=5"
curl -s "$B/api/products?limit=10&offset=20"
curl -s "$B/api/products/42"
curl -s "$B/api/customers?limit=10"
curl -s "$B/api/orders?customer_id=1&limit=5"
curl -s "$B/api/orders?limit=5"

curl -s -X POST $B/api/orders \
  -H 'Content-Type: application/json' -H 'X-Request-ID: demo-order-1' \
  -d '{"customer_id": 1, "items": [{"product_id": 3, "quantity": 1}, {"product_id": 7, "quantity": 2}]}'

curl -s $B/metrics | grep '^apiserver_'
```

## Tests

```sh
pip install -r requirements-dev.txt
python -m pytest
```

The tests replace `ibm_db` with `tests/fake_ibm_db.py`, so they need neither a database nor the
Db2 driver.
