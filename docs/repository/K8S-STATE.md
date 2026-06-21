=== K8S PODS ===
NAME                       READY   STATUS    RESTARTS         AGE   IP            NODE          NOMINATED NODE   READINESS GATES
gateway-5fbcd8d474-psspt   1/1     Running   17 (6h48m ago)   9d    10.42.1.250   k8s-worker1   <none>           <none>
postgres-9cf64c688-mlrtj   1/1     Running   17 (6h48m ago)   9d    10.42.1.254   k8s-worker1   <none>           <none>
redis-8f5bbf5d-sdxw4       1/1     Running   17 (6h48m ago)   9d    10.42.1.247   k8s-worker1   <none>           <none>

=== K8S SERVICES ===
NAME       TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)    AGE
gateway    ClusterIP   10.43.109.55    <none>        8000/TCP   12d
postgres   ClusterIP   10.43.79.206    <none>        5432/TCP   12d
redis      ClusterIP   10.43.230.197   <none>        6379/TCP   12d

=== K8S INGRESS ===
NAME                   CLASS     HOSTS                                                   ADDRESS          PORTS   AGE
pharmabridge-ingress   traefik   pharmabridge.bvrinfra.in,pharmabridge-ops.bvrinfra.in   192.168.29.200   80      9d
