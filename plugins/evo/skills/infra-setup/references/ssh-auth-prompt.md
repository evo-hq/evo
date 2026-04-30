Verify SSH access with exactly the same endpoint evo will use:

```bash
ssh user@host
```

If the host uses a non-default key or port, include them explicitly:

```bash
ssh -i /path/to/key -p 2222 user@host
```

Once that succeeds, rerun:

```bash
evo config backend remote --provider ssh --provider-config host=user@host,port=2222,key=/path/to/key
```
