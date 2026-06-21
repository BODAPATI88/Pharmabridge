ADR-003: Platform Source of Truth

Status

Accepted

Date

2026-06-21

Context

PharmaBridge currently exists in two deployment models:

1. Docker Compose
2. Kubernetes

Both environments are running simultaneously on the same infrastructure.

Evidence:

- Docker Compose stack "pharmabridge" is running.
- Kubernetes namespace "pharmabridge" is running.
- Docker gateway occupies host port 8000.
- Kubernetes gateway is running as a Deployment and exposed through Service and Ingress.

Running both platforms creates:

- Port conflicts
- Operational confusion
- Deployment ambiguity
- Troubleshooting complexity
- Configuration drift risk

Decision

Kubernetes is the official deployment platform for PharmaBridge Beta and future production environments.

Docker Compose remains available only for:

- Local development
- Temporary testing
- Emergency troubleshooting

Docker Compose is not considered the source of truth for production operations.

Consequences

Positive:

- Single deployment target
- Simplified operations
- Clear release process
- Reduced troubleshooting effort
- Consistent documentation

Negative:

- Existing Docker deployment must eventually be archived
- Migration validation is required before retirement

Evidence

2026-06-21 verification:

Gateway:

- Running in Kubernetes
- Image: bodapati88/pharmabridge-gateway:fix-storage

Dependencies:

- PostgreSQL healthy
- Redis healthy

Application:

- Health endpoint operational
- OpenAPI endpoint operational

Owner

BVR Group Ltd

Review Date

After Beta completion.
