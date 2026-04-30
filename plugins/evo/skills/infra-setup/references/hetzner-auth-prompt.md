Make sure Hetzner Cloud credentials are available in the shell that launches evo.

If you use an environment variable:

```bash
export HCLOUD_TOKEN=...
```

If the server should boot with an injected SSH key, point evo at the key name
and the matching private key:

```bash
evo config backend remote --provider hetzner --provider-config \
  token=...,server_type=cpx11,image=ubuntu-24.04,ssh_key_name=my-key,key=/path/to/my-key.pem
```

Use the same server type, image, and SSH key that were used when creating the server.
