# Security Policy

## Scope

The reviewer control plane is not yet operational. Do not submit real secrets,
private repository contents, access tokens, credentials, or sensitive review
packets to this repository.

## Security properties

Future integrations must use least-privilege credentials, explicit repository
allowlists, read-only defaults, bounded concurrency, and durable redacted
receipts. Automatic approval, merge, deployment, thread resolution, and
credential exfiltration are prohibited.

## Reporting

Do not open a public issue for a suspected secret or exploitable integration
weakness. Contact the HUMMBL maintainers through the repository owner’s private
security channel and include only the minimum necessary evidence.
