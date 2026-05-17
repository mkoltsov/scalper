#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import html
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib import request
from urllib.error import HTTPError, URLError


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "deals.json"
DEFAULT_SECRETS = BASE_DIR / "secrets.json"
DEFAULT_STATE = BASE_DIR / "state.json"
DEFAULT_PUBLIC_DIR = BASE_DIR / "public"
DEFAULT_LOG_FILE = BASE_DIR / "cron.log"
LOGGER = logging.getLogger("scalper")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/124.0 Safari/537.36"
)
LOCAL_ONLY_DOMAINS = {
    "craigslist.org",
    "facebook.com",
    "marketplace.facebook.com",
    "nextdoor.com",
}
NO_US_SHIPPING_DOMAINS = {
    "jp.mercari.com",
}
ANTI_BOT_HTTP_ERROR_DOMAINS = {
    "ebay.com",
    "ebay.co.uk",
    "ebay.de",
    "ebay.ca",
}
BAD_STATUS_PATTERNS = [
    (r"\b(?:sold out|out of stock|currently unavailable|no longer available|not available|unavailable)\b", "unavailable"),
    (r"\b(?:listing|posting|item)\s+(?:has\s+been\s+)?(?:deleted|removed|expired|ended)\b", "expired"),
    (r"\b(?:this|the)\s+(?:item|listing|posting)\s+(?:has\s+been\s+)?sold\b", "sold"),
    (r"\b(?:was|has been|is)\s+sold\b", "sold"),
    (r"\bsold\s+on\b", "sold"),
    (r"\bended\b|\bauction\s+ended\b", "ended"),
    (r"\btemporarily\s+out\s+of\s+stock\b", "unavailable"),
    (r"\b404\s+error\b|\bpage\s+not\s+found\b", "not_found"),
]
NO_US_SHIPPING_PATTERNS = [
    (r"\blocal\s+pickup\s+only\b|\bpickup\s+only\b", "local_pickup_only"),
    (r"\bno\s+shipping\b", "no_shipping"),
    (
        r"\b(?:does\s+not|doesn't|will\s+not|won't|may\s+not)\s+ship\s+to\s+"
        r"(?:the\s+)?(?:us|u\.s\.|usa|united\s+states)\b",
        "no_us_shipping",
    ),
    (
        r"\bshipping\s+(?:is\s+)?(?:not\s+available|unavailable)\s+"
        r"(?:to|for)\s+(?:the\s+)?(?:us|u\.s\.|usa|united\s+states)\b",
        "no_us_shipping",
    ),
]


class Metrics:
    def __init__(self) -> None:
        self._meter_provider = None
        self._runs = None
        self._targets = None
        self._deals_found = None
        self._notifications_sent = None
        self._codex_errors = None
        self._codex_duration = None
        self.enabled = False

        if os.environ.get("DISABLE_OTEL", "").strip().lower() in {"1", "true", "yes"}:
            LOGGER.info("OpenTelemetry metrics disabled by DISABLE_OTEL")
            return

        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource

            os.environ.setdefault("OTEL_SERVICE_NAME", "scalper")
            os.environ.setdefault("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
            os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
            os.environ.setdefault("OTEL_METRICS_EXPORTER", "otlp")

            resource = Resource.create(
                {
                    "service.name": "scalper",
                    "deployment.environment": "cron",
                    "host.name": socket.gethostname(),
                }
            )
            reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(),
                export_interval_millis=60_000,
            )
            self._meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
            meter = self._meter_provider.get_meter("scalper")
            self._runs = meter.create_counter("scalper.runs", unit="{run}")
            self._targets = meter.create_counter("scalper.targets.checked", unit="{target}")
            self._deals_found = meter.create_counter("scalper.deals.found", unit="{deal}")
            self._notifications_sent = meter.create_counter("scalper.notifications.sent", unit="{notification}")
            self._codex_errors = meter.create_counter("scalper.codex.errors", unit="{error}")
            self._codex_duration = meter.create_histogram("scalper.codex.duration", unit="ms")
            self.enabled = True
        except Exception as exc:
            LOGGER.warning("OpenTelemetry metrics disabled: %s", exc)

    def add(self, name: str, value: int = 1, attrs: dict[str, Any] | None = None) -> None:
        instrument = getattr(self, f"_{name}", None)
        if instrument is not None:
            instrument.add(value, attrs or {})

    def record_duration(self, value_ms: float, attrs: dict[str, Any] | None = None) -> None:
        if self._codex_duration is not None:
            self._codex_duration.record(value_ms, attrs or {})

    def shutdown(self) -> None:
        if self._meter_provider is not None:
            self._meter_provider.force_flush()
            self._meter_provider.shutdown()


@dataclass
class Deal:
    target_id: str
    title: str
    url: str
    source: str
    seller: str
    price_usd: float | None
    shipping_usd: float | None
    total_usd: float
    condition: str
    availability: str
    why_good: str
    confidence: float


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("SCALPER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(config_path: Path, secrets_path: Path | None) -> dict[str, Any]:
    config = load_json(config_path)
    if secrets_path and secrets_path.exists():
        config = deep_merge(config, load_json(secrets_path))
    return config


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"notified": {}, "last_run": None}
    try:
        return load_json(path)
    except json.JSONDecodeError:
        backup = path.with_suffix(f".corrupt-{int(time.time())}.json")
        path.rename(backup)
        LOGGER.warning("State file was corrupt; moved it to %s", backup)
        return {"notified": {}, "last_run": None}


def save_state(path: Path, state: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp_path.replace(path)


def build_prompt(target: dict[str, Any], max_results: int) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"""
You are a precise deal-finding automation. Use live web_search.

Current UTC date: {today}
Target id: {target["id"]}
Target name: {target["name"]}
Maximum total price: ${target["max_price_usd"]} USD, including shipping.
Usual market price estimate: ${target.get("usual_market_price_usd", "unknown")} USD.
Search intent:
{target["search_intent"]}

Acceptance criteria:
{json.dumps(target.get("acceptance_criteria", []), indent=2)}

Required model/text patterns. A listing must match at least one if this list is not empty:
{json.dumps(target.get("required_any_patterns", []), indent=2)}

Reject text patterns. Exclude a listing if it matches any of these:
{json.dumps(target.get("reject_patterns", []), indent=2)}

Suggested search queries:
{json.dumps(target.get("search_queries", []), indent=2)}

Return only strict JSON. Do not wrap it in Markdown. Do not include prose.
Schema:
{{
  "target_id": "{target["id"]}",
  "checked_at": "ISO-8601 timestamp",
      "deals": [
    {{
      "title": "listing title",
      "url": "direct listing URL",
      "source": "marketplace or site name",
      "seller": "seller if visible, otherwise empty string",
      "price_usd": 0.0,
      "shipping_usd": 0.0,
      "total_usd": 0.0,
      "condition": "condition text",
      "availability": "available",
      "listing_status": "available",
      "ships_to_us": true,
      "shipping_destination": "United States",
      "why_good": "short reason this matches the criteria",
      "confidence": 0.0
    }}
  ]
}}

Rules:
- Include at most {max_results} deals.
- Include only listings that satisfy every acceptance criterion.
- Open the exact direct listing URL before returning it. Exclude it if the URL does not load as a real listing page, returns not found, redirects to a search/home/error page, or says the listing was deleted/expired/ended.
- Use total_usd = item price plus shipping. If shipping is unknown, exclude the deal unless the item price alone is low enough that normal US shipping still keeps it below the limit.
- Include only listings that ship directly to the United States. Exclude local-pickup-only listings, Craigslist/Facebook Marketplace local listings, and listings that require a proxy/forwarder.
- Exclude sold, expired, ended, out-of-stock, unavailable, pending, auction-only without buy-it-now, and unclear listings.
- Never return sold listings, completed listings, availability examples, archived pages, cached listings, or historical price references.
- Exclude SEO pages or search result pages. A deal URL must be a buyable listing page.
- If there are no qualifying deals, return "deals": [].
""".strip()


def codex_command(codex_bin: str, output_path: Path, prompt: str) -> list[str]:
    return [
        codex_bin,
        "--search",
        "--ask-for-approval",
        "never",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--ephemeral",
        "--output-last-message",
        str(output_path),
        prompt,
    ]


def run_codex(target: dict[str, Any], config: dict[str, Any], metrics: Metrics) -> dict[str, Any]:
    codex_config = config.get("codex", {})
    codex_bin = codex_config.get("bin", "codex")
    timeout = int(codex_config.get("timeout_seconds", 900))
    max_results = int(codex_config.get("max_results_per_target", 5))
    prompt = build_prompt(target, max_results)
    attrs = {"scalper.target_id": target["id"]}
    start = time.monotonic()

    LOGGER.info("checking target=%s with Codex web_search", target["id"])
    with tempfile.TemporaryDirectory(prefix="scalper-codex-") as tmp_dir:
        output_path = Path(tmp_dir) / "last-message.json"
        process = subprocess.run(
            codex_command(codex_bin, output_path, prompt),
            cwd=BASE_DIR,
            env={**os.environ, "NO_COLOR": "1"},
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        output_text = output_path.read_text(encoding="utf-8") if output_path.exists() else process.stdout
    duration_ms = (time.monotonic() - start) * 1000
    metrics.record_duration(duration_ms, {**attrs, "scalper.codex.exit_code": process.returncode})

    if process.returncode != 0:
        metrics.add("codex_errors", attrs=attrs)
        raise RuntimeError(
            f"Codex failed for {target['id']} with exit code {process.returncode}: {process.stderr.strip()}"
        )

    return parse_codex_json(output_text)


def parse_codex_json(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        last: dict[str, Any] | None = None
        for match in re.finditer(r"{", text):
            try:
                candidate, _ = decoder.raw_decode(text[match.start() :])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                last = candidate
        if last is None:
            raise
        return last


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^0-9.]", "", str(value))
    if not cleaned:
        return None
    return float(cleaned)


def _deal_search_text(raw: dict[str, Any]) -> str:
    fields = [
        raw.get("title"),
        raw.get("url"),
        raw.get("source"),
        raw.get("seller"),
        raw.get("condition"),
        raw.get("availability"),
        raw.get("listing_status"),
        raw.get("status"),
        raw.get("shipping_destination"),
        raw.get("shipping_note"),
        raw.get("shipping"),
        raw.get("why_good"),
    ]
    return " ".join(str(field) for field in fields if field)


def _matches_text_filters(target: dict[str, Any], raw: dict[str, Any]) -> bool:
    text = _deal_search_text(raw)
    required_patterns = target.get("required_any_patterns") or []
    reject_patterns = target.get("reject_patterns") or []

    if required_patterns and not any(re.search(pattern, text, re.IGNORECASE) for pattern in required_patterns):
        LOGGER.info(
            "rejected deal target=%s reason=missing_required_model title=%s",
            target["id"],
            raw.get("title", ""),
        )
        return False

    for pattern in reject_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            LOGGER.info(
                "rejected deal target=%s reason=reject_pattern pattern=%s title=%s",
                target["id"],
                pattern,
                raw.get("title", ""),
            )
            return False

    return True


def _host_from_url(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _domain_matches(host: str, domains: set[str]) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _truthy_shipping_to_us(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value

    text = str(value).strip().lower()
    if text in {"true", "yes", "y", "1", "ships", "ships to us", "ships to united states"}:
        return True
    if text in {"false", "no", "n", "0"}:
        return False
    if any(re.search(pattern, text, re.IGNORECASE) for pattern, _ in NO_US_SHIPPING_PATTERNS):
        return False
    if re.search(r"\b(?:us|u\.s\.|usa|united states)\b", text, re.IGNORECASE):
        return True
    return None


def _pattern_reason(patterns: list[tuple[str, str]], text: str) -> str | None:
    for pattern, reason in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return reason
    return None


def _static_rejection_reason(target: dict[str, Any], raw: dict[str, Any]) -> str | None:
    url = str(raw.get("url") or "")
    host = _host_from_url(url)
    source = str(raw.get("source") or "").lower()

    if _domain_matches(host, LOCAL_ONLY_DOMAINS) or "craigslist" in source:
        return "local_only_no_us_shipping"
    if _domain_matches(host, NO_US_SHIPPING_DOMAINS):
        return "known_no_us_shipping_domain"

    status_text = " ".join(
        str(raw.get(field) or "")
        for field in ("availability", "listing_status", "status", "title", "condition", "why_good")
    ).strip()
    if status_text and status_text.lower() in {
        "sold",
        "sold out",
        "out of stock",
        "unavailable",
        "currently unavailable",
        "expired",
        "ended",
        "deleted",
        "removed",
        "pending",
    }:
        return "not_available"

    status_reason = _pattern_reason(
        BAD_STATUS_PATTERNS,
        " ".join(
            str(raw.get(field) or "")
            for field in ("availability", "listing_status", "status", "why_good")
        ),
    )
    if status_reason:
        return status_reason

    ships_to_us = _truthy_shipping_to_us(raw.get("ships_to_us"))
    if ships_to_us is False:
        return "no_us_shipping"

    shipping_reason = _pattern_reason(
        NO_US_SHIPPING_PATTERNS,
        " ".join(
            str(raw.get(field) or "")
            for field in (
                "shipping_destination",
                "shipping_note",
                "shipping",
                "availability",
                "listing_status",
                "why_good",
            )
        ),
    )
    if shipping_reason:
        return shipping_reason

    return None


def _page_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html).strip()


def validate_listing_url(deal: Deal, timeout: int = 20) -> str | None:
    host = _host_from_url(deal.url)
    req = request.Request(
        deal.url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status >= 400:
                return f"http_{status}"
            content_type = response.headers.get("Content-Type", "")
            body = response.read(600_000)
    except HTTPError as exc:
        if exc.code in {403, 429} and _domain_matches(host, ANTI_BOT_HTTP_ERROR_DOMAINS):
            LOGGER.info(
                "listing URL validation inconclusive target=%s reason=http_%s_antibot url=%s",
                deal.target_id,
                exc.code,
                deal.url,
            )
            return None
        return f"http_{exc.code}"
    except URLError as exc:
        return f"url_error:{exc.reason}"
    except Exception as exc:
        return f"url_error:{exc}"

    if "text/html" not in content_type.lower() and body:
        return None

    text = _page_text(body.decode("utf-8", errors="replace"))
    if not text:
        return "empty_page"

    status_reason = _pattern_reason(BAD_STATUS_PATTERNS, text)
    if status_reason:
        return status_reason

    shipping_reason = _pattern_reason(NO_US_SHIPPING_PATTERNS, text)
    if shipping_reason:
        return shipping_reason

    return None


def normalize_deal(target: dict[str, Any], raw: dict[str, Any]) -> Deal | None:
    url = str(raw.get("url") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not url or not title:
        return None
    if not _matches_text_filters(target, raw):
        return None
    static_rejection = _static_rejection_reason(target, raw)
    if static_rejection:
        LOGGER.info(
            "rejected deal target=%s reason=%s title=%s url=%s",
            target["id"],
            static_rejection,
            title,
            url,
        )
        return None

    price = to_float(raw.get("price_usd"))
    shipping = to_float(raw.get("shipping_usd"))
    total = to_float(raw.get("total_usd"))
    if total is None:
        if price is None:
            return None
        total = price + (shipping or 0)

    max_price = float(target["max_price_usd"])
    if total >= max_price:
        return None

    availability = str(raw.get("availability") or "").strip() or "available"
    if availability.lower() not in {"available", "in stock", "buyable"}:
        return None

    confidence = to_float(raw.get("confidence"))
    return Deal(
        target_id=target["id"],
        title=title,
        url=url,
        source=str(raw.get("source") or "").strip(),
        seller=str(raw.get("seller") or "").strip(),
        price_usd=price,
        shipping_usd=shipping,
        total_usd=total,
        condition=str(raw.get("condition") or "").strip(),
        availability=availability,
        why_good=str(raw.get("why_good") or "").strip(),
        confidence=confidence if confidence is not None else 0.0,
    )


def deal_key(deal: Deal) -> str:
    normalized_url = re.sub(r"[?#].*$", "", deal.url).rstrip("/")
    key = f"{deal.target_id}|{normalized_url}|{deal.total_usd:.2f}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def ntfy_url(config: dict[str, Any]) -> str:
    ntfy = config.get("ntfy", {})
    base_url = str(ntfy.get("base_url", "https://ntfy.sh")).rstrip("/")
    topic = str(ntfy["topic"]).strip("/")
    return f"{base_url}/{topic}"


def notifications_enabled(config: dict[str, Any]) -> bool:
    return bool(str(config.get("ntfy", {}).get("topic", "")).strip())


def http_header_value(value: Any) -> str:
    text = re.sub(r"[\r\n]+", " ", str(value)).strip()
    return text.encode("latin-1", errors="replace").decode("latin-1")


def send_ntfy(config: dict[str, Any], deal: Deal) -> None:
    ntfy = config.get("ntfy", {})
    title = f"Scalper deal: {deal.title[:80]}"
    lines = [
        f"{deal.title}",
        f"Total: ${deal.total_usd:.2f}",
        f"Source: {deal.source or 'unknown'}",
        f"Condition: {deal.condition or 'unknown'}",
        f"Why: {deal.why_good or 'matches configured deal criteria'}",
        deal.url,
    ]
    payload = "\n".join(lines).encode("utf-8")
    req = request.Request(
        ntfy_url(config),
        data=payload,
        method="POST",
        headers={
            "Title": http_header_value(title),
            "Priority": http_header_value(ntfy.get("priority", "4")),
            "Tags": http_header_value(ntfy.get("tags", "moneybag")),
            "Click": http_header_value(deal.url),
        },
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            if response.status >= 300:
                raise RuntimeError(f"ntfy returned HTTP {response.status}")
    except URLError as exc:
        raise RuntimeError(f"ntfy request failed: {exc}") from exc


def prune_state(state: dict[str, Any], keep: int = 500) -> None:
    notified = state.setdefault("notified", {})
    if len(notified) <= keep:
        return
    ordered = sorted(notified.items(), key=lambda item: item[1].get("notified_at", ""))
    state["notified"] = dict(ordered[-keep:])


def deal_to_json(deal: Deal) -> dict[str, Any]:
    return {
        "target_id": deal.target_id,
        "title": deal.title,
        "url": deal.url,
        "source": deal.source,
        "seller": deal.seller,
        "price_usd": deal.price_usd,
        "shipping_usd": deal.shipping_usd,
        "total_usd": deal.total_usd,
        "condition": deal.condition,
        "availability": deal.availability,
        "why_good": deal.why_good,
        "confidence": deal.confidence,
    }


def sanitize_log_text(text: str) -> str:
    text = re.sub(r"cfut_[A-Za-z0-9_=-]+", "cfut_[redacted]", text)
    text = re.sub(r"(https://ntfy\.sh/)[A-Za-z0-9_.-]+", r"\1[redacted]", text)
    text = re.sub(r"(Authorization:\s*(?:Bearer|token)\s+)[A-Za-z0-9_.=-]+", r"\1[redacted]", text, flags=re.IGNORECASE)
    return text


def read_log_tail(log_path: Path, lines: int = 240) -> list[str]:
    if not log_path.exists():
        return []
    try:
        raw_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [f"Could not read log file: {exc}"]
    return [sanitize_log_text(line) for line in raw_lines[-lines:]]


def write_pages(public_dir: Path, payload: dict[str, Any]) -> None:
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / "results.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (public_dir / "logs.txt").write_text(
        "\n".join(payload.get("logs", [])) + "\n",
        encoding="utf-8",
    )
    write_app_js(public_dir)

    generated_at = payload.get("finished_at") or payload.get("started_at")
    cards = []
    for target in payload.get("targets", []):
        deals = target.get("deals", [])
        if deals:
            items = "\n".join(
                f"""
                <article class="deal">
                  <div class="meta">{html.escape(deal.get("source") or "unknown")} · ${float(deal.get("total_usd") or 0):.2f}</div>
                  <h2><a href="{html.escape(deal.get("url") or "#")}" rel="nofollow noopener">{html.escape(deal.get("title") or "Untitled listing")}</a></h2>
                  <p>{html.escape(deal.get("why_good") or "Matches configured criteria.")}</p>
                </article>
                """
                for deal in deals
            )
        else:
            items = '<p class="empty">No available qualifying listings found in the latest run.</p>'
        cards.append(
            f"""
            <section class="target">
              <div class="target-head">
                <h1>{html.escape(target.get("name") or target.get("id") or "Target")}</h1>
                <span>{len(deals)} live match{"es" if len(deals) != 1 else ""}</span>
              </div>
              {items}
            </section>
            """
        )

    log_lines = payload.get("logs", [])
    if log_lines:
        logs_html = html.escape("\n".join(log_lines[-120:]))
    else:
        logs_html = "No logs available yet."

    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Scalper Results</title>
  <style>
    :root {{ color-scheme: dark; --bg:#090b10; --panel:#111722; --line:#253044; --text:#eef3ff; --muted:#97a3b8; --accent:#74d3ff; --danger:#ff8b8b; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font:16px/1.55 system-ui,-apple-system,Segoe UI,sans-serif; background:radial-gradient(circle at 20% 0,#172236 0,#090b10 42rem); color:var(--text); }}
    main {{ width:min(1040px, calc(100% - 32px)); margin:0 auto; padding:48px 0 72px; }}
    header {{ margin-bottom:32px; }}
    .eyebrow {{ color:var(--accent); font-size:13px; text-transform:uppercase; letter-spacing:.08em; }}
    h1,h2,p {{ margin-top:0; }}
    header h1 {{ font-size:clamp(34px,6vw,68px); line-height:1; margin:10px 0 16px; }}
    header p,.empty,.meta,.target-head span {{ color:var(--muted); }}
    nav {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:22px; }}
    nav a,.button,button {{ border:1px solid var(--line); background:#162033; color:var(--text); border-radius:6px; padding:9px 12px; font:inherit; text-decoration:none; cursor:pointer; }}
    nav a:hover,.button:hover,button:hover {{ border-color:var(--accent); color:var(--accent); }}
    .target {{ border:1px solid var(--line); background:color-mix(in srgb, var(--panel) 86%, transparent); border-radius:8px; padding:22px; margin:18px 0; box-shadow:0 24px 60px rgba(0,0,0,.24); }}
    .target-head {{ display:flex; gap:16px; align-items:baseline; justify-content:space-between; border-bottom:1px solid var(--line); padding-bottom:14px; margin-bottom:14px; }}
    .target-head h1 {{ font-size:22px; line-height:1.2; margin:0; }}
    .deal {{ padding:16px 0; border-top:1px solid rgba(255,255,255,.07); }}
    .deal:first-of-type {{ border-top:0; }}
    .deal h2 {{ font-size:20px; line-height:1.25; margin:4px 0 8px; }}
    .tool-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }}
    label {{ display:block; color:var(--muted); font-size:13px; margin-bottom:5px; }}
    input,textarea {{ width:100%; border:1px solid var(--line); background:#090f19; color:var(--text); border-radius:6px; padding:10px 11px; font:inherit; }}
    textarea {{ min-height:92px; resize:vertical; }}
    .wide {{ grid-column:1 / -1; }}
    .actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }}
    pre {{ margin:0; white-space:pre-wrap; overflow:auto; border:1px solid var(--line); background:#070b12; color:#dbe7ff; border-radius:8px; padding:16px; max-height:440px; }}
    .output {{ margin-top:16px; }}
    .error {{ color:var(--danger); }}
    a {{ color:var(--text); text-decoration-color:var(--accent); text-underline-offset:4px; }}
    a:hover {{ color:var(--accent); }}
    @media (max-width:640px) {{ main {{ width:min(100% - 20px, 1040px); padding-top:28px; }} .target {{ padding:16px; }} .target-head {{ display:block; }} .tool-grid {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <div class="eyebrow">Live deal monitor</div>
      <h1>Scalper Results</h1>
      <p>Latest run: {html.escape(str(generated_at or "unknown"))}. Results include only listings that passed availability, shipping, price, and direct-listing checks.</p>
      <nav>
        <a href="#results">Results</a>
        <a href="#add-target">Add target</a>
        <a href="#logs">Logs</a>
        <a href="results.json">JSON</a>
        <a href="logs.txt">Raw logs</a>
      </nav>
    </header>
    <section id="results">
      {''.join(cards)}
    </section>
    <section class="target" id="add-target">
      <div class="target-head">
        <h1>Add target</h1>
        <span>builds JSON for deals.json</span>
      </div>
      <form id="target-form" class="tool-grid">
        <div>
          <label for="target-id">ID</label>
          <input id="target-id" name="id" placeholder="sony-wh-1000xm6" required>
        </div>
        <div>
          <label for="target-price">Max total price USD</label>
          <input id="target-price" name="max_price_usd" type="number" min="1" step="1" placeholder="150" required>
        </div>
        <div class="wide">
          <label for="target-name">Name</label>
          <input id="target-name" name="name" placeholder="Sony WH-1000XM6 headphones" required>
        </div>
        <div class="wide">
          <label for="target-intent">Search intent</label>
          <textarea id="target-intent" name="search_intent" placeholder="Find currently available used or open-box..." required></textarea>
        </div>
        <div>
          <label for="target-required">Required regex patterns, one per line</label>
          <textarea id="target-required" name="required_any_patterns"></textarea>
        </div>
        <div>
          <label for="target-reject">Reject regex patterns, one per line</label>
          <textarea id="target-reject" name="reject_patterns"></textarea>
        </div>
        <div class="wide">
          <label for="target-queries">Search queries, one per line</label>
          <textarea id="target-queries" name="search_queries" required></textarea>
        </div>
      </form>
      <div class="actions">
        <button type="button" id="build-target">Build JSON</button>
        <button type="button" id="copy-target">Copy JSON</button>
        <a class="button" id="issue-link" href="https://github.com/mkoltsov/scalper/issues/new" rel="noopener">Open GitHub issue</a>
      </div>
      <p id="target-message" class="empty"></p>
      <pre class="output" id="target-output"></pre>
    </section>
    <section class="target" id="logs">
      <div class="target-head">
        <h1>Logs</h1>
        <span>{len(log_lines)} lines published</span>
      </div>
      <pre>{logs_html}</pre>
    </section>
  </main>
  <script src="app.js"></script>
</body>
</html>
"""
    (public_dir / "index.html").write_text(page, encoding="utf-8")


def write_app_js(public_dir: Path) -> None:
    script = r"""const form = document.querySelector("#target-form");
const buildButton = document.querySelector("#build-target");
const copyButton = document.querySelector("#copy-target");
const output = document.querySelector("#target-output");
const message = document.querySelector("#target-message");
const issueLink = document.querySelector("#issue-link");

function lines(value) {
  return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

function slug(value) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function buildTarget() {
  const data = new FormData(form);
  const name = String(data.get("name") || "").trim();
  const id = slug(String(data.get("id") || name));
  const price = Number(data.get("max_price_usd"));
  const queries = lines(String(data.get("search_queries") || ""));
  const intent = String(data.get("search_intent") || "").trim();

  if (!id || !name || !price || !intent || queries.length === 0) {
    throw new Error("Fill ID, name, max price, search intent, and at least one search query.");
  }

  const target = {
    id,
    name,
    max_price_usd: price,
    search_intent: intent,
    acceptance_criteria: [
      "The listing clearly matches the requested item.",
      `The total price including shipping is less than ${price} USD.`,
      "It is currently available to buy.",
      "The listing URL opens as an active listing page.",
      "It ships directly to the United States; local pickup only is not acceptable."
    ],
    search_queries: queries
  };

  const required = lines(String(data.get("required_any_patterns") || ""));
  const reject = lines(String(data.get("reject_patterns") || ""));
  if (required.length) target.required_any_patterns = required;
  if (reject.length) target.reject_patterns = reject;
  return target;
}

function refresh() {
  try {
    const target = buildTarget();
    const json = JSON.stringify(target, null, 2);
    output.textContent = json;
    message.textContent = "Add this object to deals.json under targets.";
    message.className = "empty";
    const body = [
      "Please add this scalper target to deals.json:",
      "",
      "```json",
      json,
      "```"
    ].join("\n");
    issueLink.href = "https://github.com/mkoltsov/scalper/issues/new?title=" +
      encodeURIComponent("Add scalper target: " + target.name) +
      "&body=" + encodeURIComponent(body);
  } catch (error) {
    output.textContent = "";
    message.textContent = error.message;
    message.className = "error";
    issueLink.href = "https://github.com/mkoltsov/scalper/issues/new";
  }
}

buildButton?.addEventListener("click", refresh);
form?.addEventListener("input", () => {
  if (output.textContent) refresh();
});
copyButton?.addEventListener("click", async () => {
  refresh();
  if (!output.textContent) return;
  await navigator.clipboard.writeText(output.textContent);
  message.textContent = "Copied target JSON.";
  message.className = "empty";
});
"""
    (public_dir / "app.js").write_text(script, encoding="utf-8")


def publish_pages(public_dir: Path) -> None:
    subprocess.run(["git", "fetch", "origin", "main"], cwd=BASE_DIR, check=True)
    subprocess.run(["git", "rebase", "--autostash", "origin/main"], cwd=BASE_DIR, check=True)
    subprocess.run(["git", "add", str(public_dir)], cwd=BASE_DIR, check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=BASE_DIR)
    if diff.returncode == 0:
        LOGGER.info("pages unchanged; nothing to publish")
        return
    message = f"Update scalper results {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    subprocess.run(["git", "commit", "-m", message], cwd=BASE_DIR, check=True)
    subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=BASE_DIR, check=True)

    with tempfile.TemporaryDirectory(prefix="scalper-pages-") as tmp_dir:
        pages_repo = Path(tmp_dir)
        for path in public_dir.iterdir():
            target = pages_repo / path.name
            if path.is_dir():
                shutil.copytree(path, target)
            else:
                shutil.copy2(path, target)
        subprocess.run(["git", "init", "-b", "gh-pages"], cwd=pages_repo, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "add", "."], cwd=pages_repo, check=True)
        subprocess.run(["git", "commit", "-m", message], cwd=pages_repo, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "remote", "add", "origin", "git@github.com:mkoltsov/scalper.git"], cwd=pages_repo, check=True)
        subprocess.run(["git", "push", "-f", "origin", "gh-pages"], cwd=pages_repo, check=True)


def run(
    config_path: Path,
    secrets_path: Path | None,
    state_path: Path,
    public_dir: Path,
    log_file: Path,
    dry_run: bool,
    publish: bool,
    targets_filter: set[str] | None,
) -> int:
    config = load_config(config_path, secrets_path)
    targets = config.get("targets", [])
    if targets_filter:
        targets = [target for target in targets if target.get("id") in targets_filter]
    if not targets:
        raise RuntimeError("No targets configured or selected")

    state = load_state(state_path)
    metrics = Metrics()
    started_at = datetime.now(timezone.utc).isoformat()
    pages_payload: dict[str, Any] = {
        "started_at": started_at,
        "finished_at": None,
        "targets": [],
        "logs": read_log_tail(log_file),
    }
    exit_code = 0

    try:
        metrics.add("runs", attrs={"scalper.status": "started"})
        for target in targets:
            target_attrs = {"scalper.target_id": target["id"]}
            metrics.add("targets", attrs=target_attrs)
            try:
                result = run_codex(target, config, metrics)
            except subprocess.TimeoutExpired:
                exit_code = 1
                metrics.add("codex_errors", attrs=target_attrs)
                LOGGER.exception("Codex timed out for target=%s", target["id"])
                continue
            except Exception:
                exit_code = 1
                metrics.add("codex_errors", attrs=target_attrs)
                LOGGER.exception("Codex search failed for target=%s", target["id"])
                continue

            raw_deals = result.get("deals", [])
            candidate_deals = [
                deal for deal in (normalize_deal(target, raw) for raw in raw_deals) if deal is not None
            ]
            deals: list[Deal] = []
            for deal in candidate_deals:
                link_rejection = validate_listing_url(deal)
                if link_rejection:
                    LOGGER.info(
                        "rejected deal target=%s reason=%s title=%s url=%s",
                        deal.target_id,
                        link_rejection,
                        deal.title,
                        deal.url,
                    )
                    continue
                deals.append(deal)
            LOGGER.info("target=%s qualifying_deals=%s", target["id"], len(deals))
            metrics.add("deals_found", len(deals), target_attrs)
            pages_payload["targets"].append(
                {
                    "id": target["id"],
                    "name": target.get("name", target["id"]),
                    "max_price_usd": target.get("max_price_usd"),
                    "deals": [deal_to_json(deal) for deal in deals],
                }
            )

            for deal in deals:
                key = deal_key(deal)
                if key in state.setdefault("notified", {}):
                    LOGGER.info("already notified target=%s total=%.2f url=%s", deal.target_id, deal.total_usd, deal.url)
                    continue
                LOGGER.info("new deal target=%s total=%.2f url=%s", deal.target_id, deal.total_usd, deal.url)
                if not dry_run:
                    if notifications_enabled(config):
                        send_ntfy(config, deal)
                        metrics.add("notifications_sent", attrs=target_attrs)
                    else:
                        LOGGER.info("ntfy disabled: no ntfy.topic configured")
                    state["notified"][key] = {
                        "target_id": deal.target_id,
                        "title": deal.title,
                        "url": deal.url,
                        "total_usd": deal.total_usd,
                        "notified_at": datetime.now(timezone.utc).isoformat(),
                    }
                else:
                    LOGGER.info("dry-run: would notify %s", deal.url)

        state["last_run"] = {
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "dry_run": dry_run,
            "exit_code": exit_code,
        }
        pages_payload["finished_at"] = state["last_run"]["finished_at"]
        pages_payload["exit_code"] = exit_code
        write_pages(public_dir, pages_payload)
        prune_state(state)
        if not dry_run:
            save_state(state_path, state)
            if publish:
                publish_pages(public_dir)
        metrics.add("runs", attrs={"scalper.status": "ok" if exit_code == 0 else "error"})
        return exit_code
    finally:
        metrics.shutdown()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find configured deals using Codex web_search and notify via ntfy.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--public-dir", type=Path, default=DEFAULT_PUBLIC_DIR)
    parser.add_argument("--log-file", type=Path, default=DEFAULT_LOG_FILE)
    parser.add_argument("--target", action="append", help="Only run one target id. Can be repeated.")
    parser.add_argument("--dry-run", action="store_true", help="Run searches but do not send ntfy or persist state.")
    parser.add_argument("--publish-pages", action="store_true", help="Commit and push generated public results.")
    parser.add_argument("--validate-config", action="store_true", help="Validate JSON config and exit without searching.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    configure_logging()
    args = parse_args(argv)
    if args.validate_config:
        config = load_config(args.config, args.secrets)
        if not config.get("targets"):
            raise RuntimeError("Config has no targets")
        if notifications_enabled(config):
            LOGGER.info("config ok targets=%s ntfy_url=%s", len(config["targets"]), ntfy_url(config))
        else:
            LOGGER.info("config ok targets=%s ntfy=disabled", len(config["targets"]))
        return 0
    return run(
        args.config,
        args.secrets,
        args.state,
        args.public_dir,
        args.log_file,
        args.dry_run,
        args.publish_pages,
        set(args.target) if args.target else None,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
