#!/bin/bash

OUTPUT="docs/evidence/latest-platform-audit.md"

mkdir -p docs/evidence

{
echo "# PharmaBridge Platform Audit"
echo
echo "Generated: $(date)"
echo

echo "## Git"
git branch --show-current
echo
git log --oneline -10
echo

echo "## Kubernetes Pods"
kubectl get pods -n pharmabridge -o wide
echo

echo "## Kubernetes Deployments"
kubectl get deployments -n pharmabridge
echo

echo "## Kubernetes Services"
kubectl get svc -n pharmabridge
echo

echo "## Ingress"
kubectl get ingress -n pharmabridge
echo

echo "## Gateway Image"
  kubectl get deployment gateway -n pharmabridge -o jsonpath='{.spec.template.spec.containers[0].image}'
echo
echo

echo "## Health"
kubectl port-forward -n pharmabridge svc/gateway 8080:8000 >/tmp/pf.log 2>&1 &
PF_PID=$!
sleep 5

curl -s http://localhost:8080/api/v1/health
echo
echo

echo "## Workers"
curl -s http://localhost:8080/api/v1/workers
echo
echo

echo "## Queue Depth"
curl -s http://localhost:8080/api/v1/queue-depth
echo
echo

kill $PF_PID 2>/dev/null

echo "## Docker Containers"
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo

echo "## Docker Images"
docker images
echo

echo "## Volumes"
docker volume ls
echo

echo "## Recent Kubernetes Events"
  kubectl get events -n pharmabridge --sort-by=.metadata.creationTimestamp | tail -20
} > "$OUTPUT"

echo "Audit generated:"
echo "$OUTPUT"

