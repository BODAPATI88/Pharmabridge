PharmaBridge System Overview

Purpose

PharmaBridge is a medicine price aggregation and prescription processing platform.

Current deployment runs on a Kubernetes (k3s) cluster and consists of API, database, cache, object storage, and worker components.

---

Core Components

Gateway API

Responsibilities:

- Authentication
- Prescription management
- Search orchestration
- Operator workflows
- Health monitoring

Technology:

- FastAPI
- Uvicorn

---

PostgreSQL

Responsibilities:

- User records
- Prescriptions
- Search history
- Vendor pricing data

Technology:

- PostgreSQL 16

---

Redis

Responsibilities:

- OTP storage
- Queue coordination
- Session data
- Worker heartbeats

Technology:

- Redis 7

---

MinIO

Responsibilities:

- Prescription image storage
- Upload/download URLs
- Object lifecycle management

---

Workers

Current worker implementations:

- 1mg
- PharmEasy
- NetMeds

Responsibilities:

- Vendor search execution
- Result normalization
- Queue processing

---

High-Level Flow

Patient

→ Gateway API

→ Prescription Upload

→ MinIO Storage

→ Operator Review

→ Search Processing

→ Vendor Workers

→ Aggregated Results

→ Patient

---

Deployment Platform

Current platform:

- k3s Kubernetes
- Docker containers
- Traefik ingress
- Cloudflare DNS

---

Current Known Constraints

- Vendor worker reliability under review
- Automated integration testing not yet established
- Closed beta stage
- Human review remains mandatory for prescription approval

