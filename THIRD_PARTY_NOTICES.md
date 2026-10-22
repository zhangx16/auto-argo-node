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

## Panel protocol probe

The Docker build downloads the official sing-box v1.14.3 release for the build
architecture from https://github.com/SagerNet/sing-box and verifies the release
asset SHA-256 digest returned by GitHub. The image uses it only for bounded
Reality/Hysteria2 forwarding checks. The binary is not committed to this source
repository; upstream terms and licenses apply to downloaded components.

## UI references and typography

The management UI adapts the design language of shadcn/ui dashboard-01 and login-01 (new-york-v4): https://ui.shadcn.com/ . shadcn/ui is distributed under the MIT license: https://github.com/shadcn-ui/ui/blob/main/LICENSE.md . The locally bundled Geist Latin font subset is distributed under the SIL Open Font License 1.1; see `app/static/fonts/OFL.txt`.

## DB-IP Country Lite

Country inference uses DB-IP Country Lite 2026-10 (CC BY 4.0), downloaded and SHA-256 verified at image build. The web interface provides the required attribution link: https://db-ip.com . License: https://creativecommons.org/licenses/by/4.0/ . `maxminddb` reads this database with memory mapping in the panel process; no GeoIP database is installed on node servers.
