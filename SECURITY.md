# Security Policy

## Supported versions

Security fixes are applied to the latest released version of AutoDoctor and the current `main` branch.

## Reporting a vulnerability

Please do not open a public issue containing credentials, Home Assistant tokens, private entity IDs, hostnames, IP addresses, logs with personal data, or exploit details for an unpatched vulnerability.

Prefer GitHub's private vulnerability reporting / Security Advisory flow when it is available for this repository. If it is not available, contact the repository owner privately through their GitHub profile before sharing sensitive details.

When reporting a problem, include only the minimum information needed to reproduce it and redact secrets from logs and configuration.

## Scope

Particularly useful reports include problems involving:

- Home Assistant or Supervisor authentication and API access
- ingress or dashboard access controls
- secret or personal-data disclosure
- unsafe automatic changes or destructive behaviour
- command, path, or request injection
- AI/MCP data exposure or unsafe tool use
- dependency or GitHub Actions supply-chain risks

AutoDoctor is designed to keep risky repairs approval-required. A finding that can bypass those safeguards is considered security-relevant.
