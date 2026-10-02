# Incident log

## 2026-03-14: gateway outage
For 47 minutes no request reached the platform. Cause: the gateway's TLS
certificate had expired. Fix: the certificate was renewed, and renewal is now
automated.

## 2026-05-02: slow job processing
Background jobs were delayed by up to two hours. Cause: one worker was stuck
on a malformed job. Fix: jobs now time out after 10 minutes.
