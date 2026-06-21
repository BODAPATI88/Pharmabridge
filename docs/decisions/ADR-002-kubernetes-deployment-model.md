ADR-002 Kubernetes Deployment Model

Status: Accepted

Date: 2026-06-21

Context

PharmaBridge requires a deployment platform that supports:

- Multiple services
- Future scaling
- Infrastructure learning goals
- Low operating cost

Decision

Deploy PharmaBridge on k3s Kubernetes using containerized services.

Rationale

Why Kubernetes

Benefits:

- Standard deployment model
- Service discovery
- Rolling deployments
- Future scalability

Why k3s

Benefits:

- Lightweight
- Suitable for small infrastructure
- Minimal resource consumption
- Easy operational management

Why Docker Images

Benefits:

- Repeatable deployments
- Environment consistency
- Versioned releases

Why Human Review First

Prescription validation remains a human-controlled workflow.

Automation may assist operators but does not replace operator approval.

Consequences

Positive:

- Production-like deployment environment
- Clear upgrade path
- Portable workloads

Negative:

- Additional operational complexity
- Higher learning curve than Docker Compose

Future Review

Review when:

- Cloud migration begins
- Production launch planning starts
- Infrastructure scale exceeds current cluster capacity
