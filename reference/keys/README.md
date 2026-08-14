# Reference bundle signing scheme (issue #54)

The Reference bundle's semantic identity — the canonical JSON of
`reference/bundle-<N>/manifest.json` excluding the `signing.signature`
field — is bound by a **detached Ed25519 signature** produced with the
`openssl` CLI (toolchain-only, no Python dependencies, deterministic per
RFC 8032).

## Keys

| Key | Private key location (never committed) | Public key (committed) | Role |
|---|---|---|---|
| Dev key | `~/.docx2typed/keys/dev-signing.key` (`--init-dev-key` provisions it) | `dev-signing-pub.pem` | Development / CI reproducibility runs. Clearly marked; **not** a release signature. |
| Operator key | `~/.docx2typed/keys/release-signing.key` or `$DOCX2TYPED_RELEASE_KEY` | `release-signing-pub.pem` (added by the release operator) | Release signing. Only this key authorizes a public Reference release. |

The key role recorded in each bundle is decided by which committed public
key the signing key material matches.  An unregistered key is refused — a
signature must always be verifiable, never faked.

## Verify a bundle

```bash
python -m scripts.release_bundle --verify reference/bundle-<N>
```

This recomputes the signed payload from the published manifest and verifies
the detached signature with the committed public key, then audits the
archived inputs, the two runs' Semantic roots, per-run artifact hashes, and
the freeze record.

## Release signing requirement

The bundle produced by a local run is signed with the **dev key** and is a
reproducibility artifact, not a public release.  Before a public release the
operator must:

1. install the operator private key (keystore or `DOCX2TYPED_RELEASE_KEY`),
2. commit its public key as `release-signing-pub.pem`,
3. re-run the release; the bundle then records `key_role: operator`.

A signature is never synthesized: if no key is available the release fails
closed with instructions.
