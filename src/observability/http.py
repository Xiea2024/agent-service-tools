"""Optional API metrics endpoint and bounded HTTP labels."""

from time import perf_counter

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Histogram, generate_latest
from starlette.responses import Response

REQUESTS = Counter("supportpilot_http_requests_total", "HTTP responses.", ["route", "status"])
DURATION = Histogram("supportpilot_http_duration_seconds", "HTTP response latency.", ["route"])


def install_metrics(app):
    """Expose aggregate metrics only; bind the API to the trusted host interface."""

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return Response(generate_latest(REGISTRY), headers={"Content-Type": CONTENT_TYPE_LATEST})

    @app.middleware("http")
    async def measure(request, call_next):
        if request.url.path == "/metrics":
            return await call_next(request)
        started, status = perf_counter(), 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            # Unmatched paths share one series; IDs and arbitrary URL paths never become labels.
            path = getattr(route, "path", "unmatched")
            REQUESTS.labels(path, str(status)).inc()
            DURATION.labels(path).observe(perf_counter() - started)
