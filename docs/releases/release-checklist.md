PharmaBridge Release Checklist

Purpose

This checklist must be completed before every release, deployment, or production change.

---

Pre-Deployment

Source Control

- [ ] Working tree clean
- [ ] Correct branch checked out
- [ ] Latest changes committed
- [ ] Changes reviewed
- [ ] Release notes prepared

Build

- [ ] Docker image builds successfully
- [ ] No build errors
- [ ] Image tagged correctly
- [ ] Version recorded

Example:

docker build -t bodapati88/pharmabridge-gateway:<tag> .

Image Registry

- [ ] Image pushed successfully
- [ ] Image visible in Docker Hub
- [ ] Tag verified

Example:

docker push bodapati88/pharmabridge-gateway:<tag>

---

Deployment

Kubernetes

- [ ] Deployment manifest updated
- [ ] Correct image tag specified
- [ ] imagePullPolicy verified
- [ ] Secrets unchanged or validated
- [ ] ConfigMaps validated

Verification:

kubectl get deployment gateway -n pharmabridge

Rollout

- [ ] Rollout started
- [ ] Rollout completed successfully

Verification:

kubectl rollout status deployment/gateway -n pharmabridge

---

Post-Deployment Validation

Pods

- [ ] Gateway running
- [ ] Postgres running
- [ ] Redis running

Verification:

kubectl get pods -n pharmabridge

Logs

- [ ] No startup exceptions
- [ ] Redis connected
- [ ] PostgreSQL connected

Verification:

kubectl logs deployment/gateway -n pharmabridge --tail=100

Expected:

redis_ok
postgres_ok
Application startup complete

Health Endpoint

- [ ] Health endpoint returns 200

Verification:

curl http://localhost:8080/api/v1/health

OpenAPI

- [ ] OpenAPI endpoint loads

Verification:

curl http://localhost:8080/openapi.json

Ingress

- [ ] Ingress exists
- [ ] DNS resolves
- [ ] Endpoint reachable

Verification:

kubectl get ingress -n pharmabridge

---

Documentation

- [ ] Release history updated
- [ ] Review notes updated
- [ ] ADRs updated if required
- [ ] Backlog updated if required

Files:

- docs/releases/release-history.md
- docs/reviews/
- docs/decisions/
- docs/releases/engineering-backlog.md

---

Release Approval

Deployment approved only when:

- [ ] Rollout successful
- [ ] Pods healthy
- [ ] Health endpoint healthy
- [ ] OpenAPI accessible
- [ ] No critical errors in logs

Status:

- [ ] APPROVED
- [ ] REJECTED

Reviewer:

---

Date:

---
