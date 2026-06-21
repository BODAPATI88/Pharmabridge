# ADR-004: Environment Strategy

## Status

Accepted

## Date

2026-06-21

## Context

PharmaBridge currently has two runnable environments:

1. Kubernetes cluster (k3s)
2. Docker Compose environment

Platform audits performed on 2026-06-21 confirmed both environments exist on the same infrastructure.

Kubernetes currently hosts:

- Gateway
- PostgreSQL
- Redis
- Ingress

Docker Compose still contains:

- Gateway
- Workers
- PostgreSQL
- Redis
- MinIO

This creates potential confusion regarding:

- Production source of truth
- Operational troubleshooting
- Disaster recovery procedures
- Release validation

## Decision

Kubernetes is the primary platform.

All future deployment validation, operational reviews, health checks, and release verification will be based on Kubernetes.

Docker Compose is retained temporarily for:

- Local testing
- Recovery testing
- Historical reference

Docker Compose is not considered the primary runtime environment.

## Consequences

### Positive

- Single operational source of truth
- Consistent audit process
- Reduced deployment ambiguity
- Simplified incident response

### Negative

- Compose environment may drift from Kubernetes over time
- Additional review required before eventual retirement

## Future Review

Review after:

- Beta release completion
- Worker strategy finalization
- Production readiness review

Potential future ADR:

ADR-005 Docker Retirement Strategy
