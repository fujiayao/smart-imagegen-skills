# Security and privacy

- Do not publish API keys, auth.json, config.toml, cc-switch databases, DPAPI files, prompt snapshots, reference images, generated private images, or logs. The ignore rules cover common names, but cannot guarantee removal of every renamed or previously tracked secret.
- Keys must be supplied via the interactive hidden-input flow, an explicitly authorized local encrypted store on Windows, or secure environment configuration. Do not paste them into issue text, shell arguments, screenshots, prompts, or README examples.
- The runtime reads the selected gateway configuration only. It does not alter login state, automatically change providers, or reinterpret account OAuth tokens as API keys. A key cannot identify its service URL by itself.
- Runtime `.meta.json` files contain prompts and reference image paths even though they do not contain API keys. Treat them as private user data.
- `python scripts/check_release.py` inspects Git candidate files for common sensitive patterns. It is a heuristic check, not a proof of anonymity. Also review `git diff --cached` and the Git history before publication.
- If a credential has ever been committed or shared, revoke/rotate it; adding .gitignore does not remove it from history or from other copies.
- Do not send credentials or private images in public issues. Use a repository security advisory/private reporting channel if the maintainer enables one, and otherwise contact the maintainer through a privately agreed channel without including secrets.
- This code does not bypass image service safety policies, account permissions, quotas, or billing. No automatic provider switch is performed after an uncertain paid operation.
