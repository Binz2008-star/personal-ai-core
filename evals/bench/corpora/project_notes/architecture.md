# Architecture

The platform has three services:

- gateway: terminates TLS and routes requests. Listens on port 9443.
- api: business logic. Listens on port 8443, reachable only from the gateway.
- worker: processes background jobs from the queue. Exposes no port.
