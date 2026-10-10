## Provider Matrix

Use this as the compact summary. This is setup guidance, not a runtime dependency list for evo itself.

| Provider | What evo uses at runtime | Setup / auth | Common config |
|---|---|---|---|
| `modal` | Modal Python SDK | If missing, install `evo-hq-cli[modal]` (or inject `modal` with `pipx`); then run `modal token new` | `app_name`, `gpu`, `region`, `timeout_seconds`, `health_timeout_seconds`, `apt_install`, `pip_install` |
| `e2b` | E2B Python SDK | If missing, install `evo-hq-cli[e2b]` (or inject `e2b` with `pipx`); then `export E2B_API_KEY=...` | `template`, `api_key`, `domain`, `root`, `timeout_seconds`, `health_timeout_seconds`, `allow_internet_access`, `secure` |
| `daytona` | Daytona Python SDK | If missing, install `evo-hq-cli[daytona]` (or inject `daytona` with `pipx`); then `export DAYTONA_API_KEY=...` | `api_key`, `api_url`, `target`, `timeout_seconds`, `health_timeout_seconds`, `ssh_host`, `ssh_port`, `ssh_token_ttl_minutes`, `sandbox_timeout_seconds` |
| `tenki` | Tenki Python SDK (`tenki`) | If missing, install `evo-hq-cli[tenki]` (or inject `tenki` with `pipx`); then `export TENKI_API_KEY=tk_...` | `auth_token` (or `api_key`), `workspace_id`, `base_url`, `name_prefix`, `image`, `snapshot_id`, `cpu_cores`, `memory_mb`, `disk_size_gb`, `idle_timeout_minutes`, `root`, `timeout_seconds`, `create_timeout_seconds`, `bootstrap_timeout_seconds`, `health_timeout_seconds` |
| `aws` | `boto3` | If missing, install `evo-hq-cli[aws]` (or inject `boto3` with `pipx`); then export AWS creds and region | `region`, `image_id`, `key_name`, `key`, `instance_type`, `subnet_id`, `security_group_ids`, `ssh_user`, `ssh_port`, `timeout_seconds`, `health_timeout_seconds`, `keep_warm` |
| `azure` | Azure Python SDK (`azure-identity`, `azure-mgmt-resource`, `azure-mgmt-network`, `azure-mgmt-compute`) | If missing, install `evo-hq-cli[azure]`; then use `az login` or Azure env creds, and provide subscription/resource-group config | `subscription_id`, `resource_group`, `location`, `vm_size`, `image`, `key`, `ssh_public_key`, `ssh_user`, `ssh_cidr`, `vnet_cidr`, `subnet_cidr`, `ssh_port`, `timeout_seconds`, `health_timeout_seconds`, `keep_warm` |
| `ssh` | local `ssh` transport | `ssh user@host` must work first; then add `-i` / `-p` if needed | `host`, `key`, `port`, `tunnel_port`, `keep_warm`, `health_timeout_seconds` |
| `manual` | existing remote workspace endpoint | no provisioning; only ask for URL/token if the user explicitly wants manual mode | `base_url`, `bearer_token`, `workspace_root`, `bundle_dir` |

Notes:
- `evo` runtime uses the provider SDK or transport listed in the second column.
- The `evo-hq-cli[<provider>]` extras are the preferred install path when the provider SDK is missing.
- Provider auth/setup is operator guidance. It is not the same thing as evo's runtime dependency surface.
- Common failures are usually one of: missing SDK import, missing auth state/env var, unreachable host/port, or provider-specific bootstrap mismatch.
- Incomplete provider setup usually surfaces on `evo new --remote <provider> ...`, because that is where remote allocation and bootstrap actually happen.
- For SSH-backed VM providers, also validate the guest assumptions:
  - the instance image has SSH enabled
  - the SSH user matches the image
  - the image architecture matches the selected instance type
  - the remote host can run evo's remote workspace runtime
