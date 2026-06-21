Health Observability Plan

Objective

Improve operational visibility and health reporting for PharmaBridge.

Background

Current platform status:

- Gateway running successfully
- PostgreSQL running successfully
- Redis running successfully
- Kubernetes deployment stable
- Worker deployments currently scaled down
- Health endpoint available

The next engineering cycle focuses on observability rather than new features.

Scope

Health Endpoints

Review:

- /api/v1/health
- readiness checks
- liveness checks

Validate:

- PostgreSQL reporting
- Redis reporting
- worker reporting
- dependency reporting

Worker Observability

Review:

- worker heartbeat reporting
- worker alive status
- queue visibility
- worker metrics

Validate:

- worker status accuracy
- queue depth visibility
- failure reporting

Operational Metrics

Review and document:

- Redis health
- PostgreSQL health
- Gateway health
- Worker health
- Queue depth

Out Of Scope

- Vendor integrations
- OCR implementation
- UI redesign
- Order workflow changes
- Infrastructure migration
- Docker retirement

Deliverables

1. Health endpoint assessment
2. Worker observability assessment
3. Metrics assessment
4. Recommended improvements
5. Updated documentation

Success Criteria

- Platform health visible from a single endpoint
- Dependency failures identifiable
- Worker status visible
- Troubleshooting time reduced
- Documentation updated

Branch

feature/health-observability

Status

Planning
