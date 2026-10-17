# Third-party components

## singbox-lite installer

- Project: https://github.com/0xdabiaoge/singbox-lite
- Pinned commit: `5486a730555ee96730791062aefd440a7b892751`
- File: `singbox.sh`
- SHA-256: `86687380b45b765f8a8d79e073d5c0972c7175497b995e4099c78091a0ae1cf4`

The upstream repository did not contain a license file when this project was prepared.
This repository does not redistribute the installer source. `scripts/fetch-installer.sh`
fetches that exact file from upstream and verifies its digest during the Docker build
(or explicit local development setup). The generated Docker image contains that dependency.
This notice does not grant rights to upstream code. Check upstream terms before redistributing
an image or any downloaded components.

The installer downloads sing-box, cloudflared, and supporting binaries at node installation
time. Those projects retain their respective licenses. Pinning the installer does not pin
all runtime binary versions or make installation independent of external services.

## Application dependencies

Python packages are listed with exact versions in `requirements.txt`. Their upstream
licenses continue to apply. No runtime credentials, production database, server backup,
or user node configuration is included in this repository.
