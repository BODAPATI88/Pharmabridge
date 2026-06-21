Docker Legacy Inventory

Status

Legacy Docker Compose environment remains active alongside Kubernetes.

Inventory collected on: 2026-06-21

---

Docker Compose Stack

pharmabridge

Status:

running (7)

Configuration:

/home/ravi/projects/pharmabridge-src/pharmabridge/docker-compose.yml

---

Running Containers

Gateway

Container: pharmabridge-gateway
Image: pharmabridge-gateway:dev
Ports: 8000:8000
Status: Healthy

Validation Container

Container: pb-test
Image: pharmabridge-gateway:fixed
Ports: 8001:8000
Status: Healthy
Purpose: Temporary validation of fix-storage image

PostgreSQL

Container: pharmabridge-postgres
Image: postgres:16-alpine
Ports: 5432:5432
Status: Healthy

Redis

Container: pharmabridge-redis
Image: redis:7.2-alpine
Ports: 6379:6379
Status: Healthy

MinIO

Container: pharmabridge-minio
Image: quay.io/minio/minio
Ports: 9000-9001
Status: Running

Workers

pharmabridge-worker-1mg
pharmabridge-worker-pharmeasy
pharmabridge-worker-netmeds

Status:

Running

---

Docker Volumes

pharmabridge_postgres_data
pharmabridge_redis_data
pharmabridge_minio_data

---

Images

bodapati88/pharmabridge-gateway:fix-storage
bodapati88/pharmabridge-gateway:rel-5d44422
pharmabridge-gateway:dev
pharmabridge-gateway:fixed
pharmabridge-worker:dev
postgres:16-alpine
redis:7.2-alpine
quay.io/minio/minio

---

Port Usage

8000 -> legacy docker gateway
8001 -> validation container
5432 -> docker postgres
6379 -> docker redis
9000 -> docker minio api
9001 -> docker minio console

---

Findings

Kubernetes and Docker Compose are both running PharmaBridge workloads simultaneously.

This causes:

- Port conflicts
- Duplicate services
- Operational ambiguity
- Troubleshooting complexity

Example:

kubectl port-forward svc/gateway 8000:8000

fails because Docker already owns port 8000.

---

Decision Reference

See:

ADR-003 Platform Source of Truth

Kubernetes is the production platform.

Docker Compose is retained temporarily for development, validation, and rollback purposes until a formal retirement plan is approved.
