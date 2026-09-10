#!/usr/bin/env python3
"""Probe WebArena reset servers and optionally exercise reset cycles.

Examples:
  scripts/webarena_reset_probe.py --status-only 34.1.2.3 35.4.5.6
  scripts/webarena_reset_probe.py --services gitlab --cycles 10 --verify-gitlab 34.1.2.3
  scripts/webarena_reset_probe.py --hosts-file hosts.txt --services gitlab --cycles 3
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def http_get(url: str, timeout: int) -> tuple[int | str, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            body = str(exc)
        return exc.code, body
    except Exception as exc:
        return "ERR", f"{type(exc).__name__}: {exc}"


def reset_base_url(host: str, reset_port: int) -> str:
    if "://" in host:
        return host.rstrip("/")
    return f"http://{host}:{reset_port}"


def service_host(host: str) -> str:
    if "://" not in host:
        return host.split(":", 1)[0]

    parsed = urllib.parse.urlparse(host)
    return parsed.hostname or host


def status(base_url: str, timeout: int) -> tuple[dict | None, str]:
    code, body = http_get(f"{base_url}/status", timeout)
    if code != 200:
        return None, f"status_http={code} body={body[:240]}"
    try:
        return json.loads(body), ""
    except json.JSONDecodeError as exc:
        return None, f"status_json={exc}"


def ready_count(data: dict, service: str) -> int:
    svc = data.get("services", {}).get(service)
    if not svc:
        return 0
    try:
        return int(svc.get("ready_count") or 0)
    except (TypeError, ValueError):
        return 0


def wait_for_standbys(
    base_url: str,
    services: list[str],
    timeout: int,
    poll: int,
    request_timeout: int,
) -> tuple[dict | None, str]:
    deadline = time.time() + timeout
    last_message = ""

    while time.time() < deadline:
        data, err = status(base_url, request_timeout)
        if data and all(ready_count(data, service) > 0 for service in services):
            return data, ""
        last_message = err or json.dumps(data or {}, sort_keys=True)[:500]
        time.sleep(poll)

    return None, f"timed out waiting for ready standby; last={last_message}"


def gitlab_explore_ok(host: str, timeout: int) -> tuple[bool, str]:
    code, body = http_get(f"http://{service_host(host)}:9001/explore", timeout)
    if code == 200:
        return True, "gitlab explore ok"
    return False, f"gitlab explore_http={code} body={body[:240]}"


def reset_once(
    host: str,
    services: list[str],
    reset_port: int,
    wait_timeout: int,
    poll: int,
    request_timeout: int,
    verify_gitlab: bool,
) -> tuple[bool, str]:
    base_url = reset_base_url(host, reset_port)
    before, err = wait_for_standbys(base_url, services, wait_timeout, poll, request_timeout)
    if not before:
        return False, err

    before_active = {
        service: before.get("services", {}).get(service, {}).get("active")
        for service in services
    }
    query = urllib.parse.urlencode({"services": ",".join(services)})
    code, body = http_get(f"{base_url}/reset?{query}", request_timeout)
    if code != 200:
        return False, f"reset_http={code} body={body[:240]}"

    after, err = status(base_url, request_timeout)
    if not after:
        return False, err

    transitions = []
    for service in services:
        svc = after.get("services", {}).get(service, {})
        active = svc.get("active")
        if active == before_active.get(service):
            return False, f"{service} active did not change: {active}"
        transitions.append(
            f"{service}:{before_active.get(service)}->{active} "
            f"ready={svc.get('ready_count')}/{svc.get('total')}"
        )

    if verify_gitlab and "gitlab" in services:
        ok, message = gitlab_explore_ok(host, request_timeout)
        if not ok:
            return False, message

    return True, "; ".join(transitions)


def summarize_status(host: str, reset_port: int, request_timeout: int) -> tuple[bool, str]:
    data, err = status(reset_base_url(host, reset_port), request_timeout)
    if not data:
        return False, err

    parts = [f"status={data.get('status')}"]
    for name, svc in sorted(data.get("services", {}).items()):
        parts.append(
            f"{name}:active={svc.get('active')} "
            f"ready={svc.get('ready_count')}/{svc.get('total')}"
        )
    return True, ", ".join(parts)


def probe_host(host: str, args: argparse.Namespace) -> tuple[str, bool, list[str]]:
    messages: list[str] = []

    if args.status_only:
        ok, message = summarize_status(host, args.reset_port, args.request_timeout)
        return host, ok, [message]

    ok = True
    for cycle in range(1, args.cycles + 1):
        passed, message = reset_once(
            host=host,
            services=args.services,
            reset_port=args.reset_port,
            wait_timeout=args.wait_timeout,
            poll=args.poll,
            request_timeout=args.request_timeout,
            verify_gitlab=args.verify_gitlab,
        )
        messages.append(f"cycle={cycle} {'ok' if passed else 'FAIL'} {message}")
        if not passed:
            ok = False
            break

    return host, ok, messages


def load_hosts(positional: list[str], hosts_file: str | None) -> list[str]:
    hosts = list(positional)
    if hosts_file:
        for line in pathlib.Path(hosts_file).read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                hosts.append(stripped)
    return sorted(dict.fromkeys(hosts))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("hosts", nargs="*", help="Host/IP or reset-server base URL.")
    parser.add_argument("--hosts-file", help="File containing one host/IP/URL per line.")
    parser.add_argument("--services", default="gitlab", help="Comma-separated services to reset.")
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--reset-port", type=int, default=7565)
    parser.add_argument("--wait-timeout", type=int, default=900)
    parser.add_argument("--poll", type=int, default=15)
    parser.add_argument("--request-timeout", type=int, default=120)
    parser.add_argument("--status-only", action="store_true")
    parser.add_argument("--verify-gitlab", action="store_true", help="Check http://HOST:9001/explore after GitLab resets.")
    args = parser.parse_args()

    args.services = [service.strip() for service in args.services.split(",") if service.strip()]
    hosts = load_hosts(args.hosts, args.hosts_file)
    if not hosts:
        parser.error("provide at least one host or --hosts-file")
    if not args.services:
        parser.error("--services must include at least one service")

    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = {pool.submit(probe_host, host, args): host for host in hosts}
        for future in concurrent.futures.as_completed(futures):
            host, ok, messages = future.result()
            print(f"{host}: {'ok' if ok else 'FAILED'}")
            for message in messages:
                print(f"  {message}")
            if not ok:
                failures.append(host)

    if failures:
        print("Failed hosts: " + ", ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
