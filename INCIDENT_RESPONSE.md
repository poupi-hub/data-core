# Incident Response

## Severity Levels

| Severity | Definition |
| --- | --- |
| SEV1 | Data loss risk, exposed database, production unavailable, trading/runtime safety risk |
| SEV2 | Major feature degraded, queues stalled, scrape/data freshness broken |
| SEV3 | Partial degradation with workaround |
| SEV4 | Documentation, stale dashboard, non-urgent cleanup |

## First 10 Minutes

1. Freeze deploys unless the incident is caused by a bad deploy and rollback is already known safe.
2. Capture evidence:

```bash
ssh poupi "date -Is; docker ps; docker ps --filter health=unhealthy; ss -tulpn"
ssh poupi "curl -fsS http://127.0.0.1:9090/-/healthy || true"
ssh poupi "curl -fsS http://127.0.0.1:9090/api/v1/targets | jq -r '.data.activeTargets[] | [.labels.job,.health,.lastError] | @tsv' || true"
```

3. Check recent logs:

```bash
ssh poupi "docker logs --tail 200 <container>"
```

4. Check storage and memory:

```bash
ssh poupi "df -h; free -h; docker stats --no-stream"
```

## Do Not

- Do not delete volumes.
- Do not run migrations manually without backup and approval.
- Do not change thresholds or strategy code to silence symptoms.
- Do not paste secrets into tickets or chat.

## Common Incidents

### Public Database Exposure

Classification: SEV1.

Actions:

- Confirm listener with `ss -tulpn`.
- Confirm Compose source.
- Restrict firewall or remove host port binding.
- Rotate affected credentials if exposure was internet-accessible.
- Audit logs where available.

### Prometheus Target Down

Classification: SEV2 unless user impact is confirmed.

Actions:

- Check target DNS/network.
- Confirm container/service name.
- Fix Prometheus target or attach service to monitoring network.
- Avoid deleting stale target without documenting reason.

### Queue Or Scheduler Stalled

Classification: SEV2.

Actions:

- Check worker/scheduler logs.
- Check Redis health and auth.
- Check job dead letters or backlog metrics.
- Restart only after capturing logs.

### Suspected Bad Deploy

Classification: SEV1/SEV2 depending impact.

Actions:

- Identify current image/commit.
- Compare previous deploy.
- Roll back via Coolify or compose image tag.
- Validate health and metrics.
