Make sure AWS credentials are available in the shell that launches evo.

If you use environment variables:

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-east-1
```

If the EC2 instance uses a key pair, point evo at the matching private key:

```bash
evo config backend remote --provider aws --provider-config \
  region=us-east-1,image_id=ami-...,key_name=my-key,key=/path/to/my-key.pem
```

Use the same region and key pair you used when creating the instance.
