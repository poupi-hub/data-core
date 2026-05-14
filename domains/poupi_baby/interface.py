from dataclasses import dataclass


@dataclass(frozen=True)
class PoupiBabyModule:
    name: str
    responsibility: str
    source_path: str


@dataclass(frozen=True)
class PoupiBabyEndpoint:
    method: str
    path: str
    module: str
    purpose: str
    auth_required: bool = True


DOMAIN_NAME = "poupi_baby"
DISPLAY_NAME = "Poupi Baby"
DESCRIPTION = "Migrated backend interface for Poupi price monitoring, scraping, alerts, billing, analytics, and admin operations."

MODULES: tuple[PoupiBabyModule, ...] = (
    PoupiBabyModule("auth", "JWT, Google auth, user profile, email confirmation, password changes.", "backend/src/auth"),
    PoupiBabyModule("products", "Product CRUD, URL ingestion, quota checks, canonical matching.", "backend/src/products"),
    PoupiBabyModule("offers", "Marketplace offers and current price records.", "backend/src/offers"),
    PoupiBabyModule("marketplaces", "Marketplace CRUD and source metadata.", "backend/src/marketplaces"),
    PoupiBabyModule("price_history", "Historical price reads and summaries.", "backend/src/price-history"),
    PoupiBabyModule("alerts", "User alerts and watchlists.", "backend/src/alerts"),
    PoupiBabyModule("crawler", "Scraper orchestration, queues, source health, metrics and retries.", "backend/src/crawler"),
    PoupiBabyModule("billing", "Checkout, subscriptions, cancellation and payment webhooks.", "backend/src/billing"),
    PoupiBabyModule("analytics", "Event tracking, funnels, active users, top products and time series.", "backend/src/analytics"),
    PoupiBabyModule("admin", "Admin dashboards, jobs, scraping controls, logs and settings.", "backend/src/admin"),
    PoupiBabyModule("deal_score", "Deal score calculation for offers and products.", "backend/src/deal-score"),
    PoupiBabyModule("review_intelligence", "Review analysis and trust score processing.", "backend/src/review-intelligence"),
    PoupiBabyModule("market_intelligence", "Trend analysis, upcoming promos and market pattern jobs.", "backend/src/market-intelligence"),
    PoupiBabyModule("ai_ops", "Incident detection, incident history and operational AI providers.", "backend/src/ai-ops"),
    PoupiBabyModule("worker", "Queue worker process for background jobs.", "worker/src"),
)

ENDPOINTS: tuple[PoupiBabyEndpoint, ...] = (
    PoupiBabyEndpoint("GET", "/", "app", "Root service response.", auth_required=False),
    PoupiBabyEndpoint("GET", "/healthz", "health", "Backend health check.", auth_required=False),
    PoupiBabyEndpoint("POST", "/auth/signup", "auth", "Create user account.", auth_required=False),
    PoupiBabyEndpoint("POST", "/auth/login", "auth", "Login with credentials.", auth_required=False),
    PoupiBabyEndpoint("POST", "/auth/google", "auth", "Google login.", auth_required=False),
    PoupiBabyEndpoint("POST", "/auth/sync", "auth", "Sync external auth user.", auth_required=False),
    PoupiBabyEndpoint("GET", "/auth/me", "auth", "Current user.", auth_required=True),
    PoupiBabyEndpoint("GET", "/auth/profile", "auth", "Current user profile.", auth_required=True),
    PoupiBabyEndpoint("PATCH", "/auth/profile", "auth", "Update profile.", auth_required=True),
    PoupiBabyEndpoint("PATCH", "/auth/password", "auth", "Update password.", auth_required=True),
    PoupiBabyEndpoint("POST", "/auth/email-confirmation/request", "auth", "Request email confirmation.", auth_required=True),
    PoupiBabyEndpoint("POST", "/auth/email-confirmation/confirm", "auth", "Confirm email code.", auth_required=True),
    PoupiBabyEndpoint("POST", "/products/by-url", "products", "Create or monitor product from URL.", auth_required=True),
    PoupiBabyEndpoint("GET", "/products", "products", "List products.", auth_required=True),
    PoupiBabyEndpoint("GET", "/products/quota", "products", "Get user product quota.", auth_required=True),
    PoupiBabyEndpoint("GET", "/products/:id", "products", "Get product details.", auth_required=True),
    PoupiBabyEndpoint("POST", "/products", "products", "Create product.", auth_required=True),
    PoupiBabyEndpoint("PATCH", "/products/:id", "products", "Update product.", auth_required=True),
    PoupiBabyEndpoint("DELETE", "/products/:id", "products", "Delete product.", auth_required=True),
    PoupiBabyEndpoint("POST", "/offers", "offers", "Create offer.", auth_required=True),
    PoupiBabyEndpoint("GET", "/offers", "offers", "List offers.", auth_required=True),
    PoupiBabyEndpoint("GET", "/offers/:id", "offers", "Get offer.", auth_required=True),
    PoupiBabyEndpoint("PATCH", "/offers/:id", "offers", "Update offer.", auth_required=True),
    PoupiBabyEndpoint("DELETE", "/offers/:id", "offers", "Delete offer.", auth_required=True),
    PoupiBabyEndpoint("GET", "/crawler/scrape", "crawler", "Scrape arbitrary URL.", auth_required=True),
    PoupiBabyEndpoint("GET", "/crawler/amazon", "crawler", "Amazon scrape helper.", auth_required=True),
    PoupiBabyEndpoint("GET", "/crawler/sync/:offerId", "crawler", "Sync one offer.", auth_required=True),
    PoupiBabyEndpoint("POST", "/crawler/sync", "crawler", "Sync offers in batch.", auth_required=True),
    PoupiBabyEndpoint("POST", "/crawler/sync/:offerId", "crawler", "Queue sync for one offer.", auth_required=True),
    PoupiBabyEndpoint("GET", "/crawler/health", "crawler", "Scraper health overview.", auth_required=True),
    PoupiBabyEndpoint("GET", "/crawler/queue/stats", "crawler", "Queue stats.", auth_required=True),
    PoupiBabyEndpoint("GET", "/crawler/queue/failed", "crawler", "Failed queue jobs.", auth_required=True),
    PoupiBabyEndpoint("POST", "/crawler/queue/retry", "crawler", "Retry failed jobs.", auth_required=True),
    PoupiBabyEndpoint("POST", "/crawler/queue/pause", "crawler", "Pause crawler queue.", auth_required=True),
    PoupiBabyEndpoint("POST", "/crawler/queue/resume", "crawler", "Resume crawler queue.", auth_required=True),
    PoupiBabyEndpoint("POST", "/billing/checkout", "billing", "Start checkout.", auth_required=True),
    PoupiBabyEndpoint("GET", "/billing/status", "billing", "Subscription status.", auth_required=True),
    PoupiBabyEndpoint("POST", "/billing/cancel", "billing", "Cancel subscription.", auth_required=True),
    PoupiBabyEndpoint("POST", "/billing/webhook/mercadopago", "billing", "MercadoPago webhook.", auth_required=False),
    PoupiBabyEndpoint("POST", "/billing/webhook/stripe", "billing", "Stripe webhook.", auth_required=False),
    PoupiBabyEndpoint("POST", "/analytics/track", "analytics", "Track analytics event.", auth_required=False),
    PoupiBabyEndpoint("GET", "/analytics/event-counts", "analytics", "Event counts.", auth_required=True),
    PoupiBabyEndpoint("GET", "/analytics/active-users", "analytics", "Active users.", auth_required=True),
    PoupiBabyEndpoint("GET", "/analytics/top-products", "analytics", "Top products.", auth_required=True),
    PoupiBabyEndpoint("GET", "/analytics/funnel", "analytics", "Funnel report.", auth_required=True),
    PoupiBabyEndpoint("GET", "/analytics/time-series", "analytics", "Time-series report.", auth_required=True),
    PoupiBabyEndpoint("GET", "/admin/overview", "admin", "Admin overview.", auth_required=True),
    PoupiBabyEndpoint("GET", "/admin/scraping", "admin", "Admin scraping status.", auth_required=True),
    PoupiBabyEndpoint("POST", "/admin/scraping/retry-failed", "admin", "Retry failed scraping jobs.", auth_required=True),
    PoupiBabyEndpoint("POST", "/admin/scraping/pause", "admin", "Pause scraping.", auth_required=True),
    PoupiBabyEndpoint("POST", "/admin/scraping/resume", "admin", "Resume scraping.", auth_required=True),
    PoupiBabyEndpoint("GET", "/admin/jobs", "admin", "Admin job status.", auth_required=True),
    PoupiBabyEndpoint("GET", "/admin/logs", "admin", "Admin logs.", auth_required=True),
)


def get_interface_summary() -> dict:
    return {
        "name": DOMAIN_NAME,
        "display_name": DISPLAY_NAME,
        "description": DESCRIPTION,
        "runtime": "nestjs/typescript",
        "repo": "Projetos/poupi-baby",
        "backend_path": "backend/src",
        "worker_path": "worker/src",
        "modules": len(MODULES),
        "endpoints": len(ENDPOINTS),
    }


def list_modules() -> list[dict]:
    return [module.__dict__ for module in MODULES]


def list_endpoints() -> list[dict]:
    return [endpoint.__dict__ for endpoint in ENDPOINTS]

