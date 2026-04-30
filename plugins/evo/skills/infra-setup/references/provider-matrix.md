## Provider Matrix

Use this as the compact summary. This is setup guidance, not a runtime dependency list for evo itself.

| Provider | What evo uses at runtime | Setup / auth | Common config |
|---|---|---|---|
| `modal` | Modal Python SDK | If missing, install `evo-hq-cli[modal]` (or inject `modal` with `pipx`); then run `modal token new` | `app_name`, `gpu`, `region`, `timeout_seconds`, `health_timeout_seconds`, `apt_install`, `pip_install` |
| `e2b` | E2B Python SDK | If missing, install `evo-hq-cli[e2b]` (or inject `e2b` with `pipx`); then `export E2B_API_KEY=...` | `template`, `api_key`, `domain`, `root`, `timeout_seconds`, `health_timeout_seconds`, `allow_internet_access`, `secure` |
| `daytona` | Daytona Python SDK | If missing, install `evo-hq-cli[daytona]` (or inject `daytona` with `pipx`); then `export DAYTONA_API_KEY=...` | `api_key`, `api_url`, `target`, `timeout_seconds`, `health_timeout_seconds`, `ssh_host`, `ssh_port`, `ssh_token_ttl_minutes`, `sandbox_timeout_seconds` |
| `aws` | `boto3` | If missing, install `evo-hq-cli[aws]` (or inject `boto3` with `pipx`); then export AWS creds and region | `region`, `image_id`, `key_name`, `key`, `instance_type`, `subnet_id`, `security_group_ids`, `ssh_user`, `ssh_port`, `timeout_seconds`, `health_timeout_seconds`, `keep_warm` |
| `cloudflare` | Cloudflare Sandbox bridge over HTTP (`requests`) | Deploy the bridge Worker, export `SANDBOX_API_URL` and `SANDBOX_API_KEY` | `api_url`, `api_key`, `workspace_root`, `timeout_seconds`, `health_timeout_seconds` |
| `hetzner` | `hcloud` | If missing, install `evo-hq-cli[hetzner]` (or inject `hcloud` with `pipx`); then `export HCLOUD_TOKEN=...` | `token`, `server_type`, `image`, `location`, `ssh_key_name`, `key`, `ssh_user`, `ssh_port`, `timeout_seconds`, `health_timeout_seconds`, `keep_warm` |
| `ssh` | local `ssh` transport | `ssh user@host` must work first; then add `-i` / `-p` if needed | `host`, `key`, `port`, `tunnel_port`, `keep_warm`, `health_timeout_seconds` |
| `manual` | existing sandbox-agent URL/token | no provisioning; only ask for URL/token if the user explicitly wants manual mode | `base_url`, `bearer_token`, `workspace_root`, `bundle_dir` |

Notes:
- `evo` runtime uses the provider SDK or transport listed in the second column.
- The `evo-hq-cli[<provider>]` extras are the preferred install path when the provider SDK is missing.
- Provider auth/setup is operator guidance. It is not the same thing as evo's runtime dependency surface.
- Common failures are usually one of: missing SDK import, missing auth state/env var, or unreachable host/port.
