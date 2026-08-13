# Security

## Reporting a vulnerability

Please report security issues privately through GitHub's private vulnerability
reporting feature. Do not open a public issue containing credentials, tokens,
account details, logs, or exploit instructions.

## Credential boundary

Kaggle tokens must never be committed to this repository. The Windows desktop
app stores them with user-scoped DPAPI encryption outside the checkout. Local
databases, artifacts, logs, `.env` files, installer output, and encrypted token
files are excluded by `.gitignore`.

The supplied control plane binds to loopback by default. Do not port-forward it
or expose it to an untrusted network without adding an authenticated reverse
proxy and reviewing the deployment boundary.
