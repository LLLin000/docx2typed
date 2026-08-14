//! Issue #61 embedded-asset integrity tests: the self-contained binary must
//! embed the pinned schema bundle, capability manifest, and Unicode vertical
//! catalog with hashes that match the frozen Python Reference bundle
//! records, and the `audit` operation must work from the embedded catalog
//! with no repo checkout (no `--catalog` override).

use std::path::PathBuf;

use docx2typed_app::embedded::{
    assets, verify, CAPABILITY_MANIFEST_JSON, SCHEMA_BUNDLE_JSON, UNICODE_CATALOG_HASH,
    UNICODE_CATALOG_VERSION, UNICODE_VERTICAL_CATALOG_JSON,
};
use docx2typed_app::{AuditArgs, Engine, Operation, OperationArgs, OperationContext, Outcome};
use docx2typed_protocol::{
    bytes_sha256, semantic_sha256, CAPABILITY_MANIFEST_SHA256, SCHEMA_BUNDLE_SHA256,
};

fn fixture() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../corpus/release/plain.docx")
}

#[test]
fn embedded_assets_match_the_frozen_bundle_records() {
    let table = verify().expect("every embedded asset matches its pinned hash");

    let by_name = |name: &str| {
        table
            .iter()
            .find(|a| a.name == name)
            .expect("asset present")
    };

    // Schema bundle: the Reference freeze pins the *semantic* identity
    // (reference/bundle-1/freeze.json identities.contract.schema_bundle).
    let schema = by_name("scripts/protocol_schema_bundle.json");
    assert_eq!(
        schema.semantic_sha256.as_deref(),
        Some(SCHEMA_BUNDLE_SHA256),
        "schema bundle semantic hash must equal the frozen record"
    );
    assert_eq!(
        bytes_sha256(SCHEMA_BUNDLE_JSON.as_bytes()),
        schema.raw_sha256
    );

    // Capability manifest: the Reference freeze pins the semantic identity
    // (identities.capability).
    let capability = by_name("capabilities/manifest.json");
    assert_eq!(
        capability.semantic_sha256.as_deref(),
        Some(CAPABILITY_MANIFEST_SHA256),
        "capability manifest semantic hash must equal the frozen record"
    );
    assert_eq!(
        bytes_sha256(CAPABILITY_MANIFEST_JSON.as_bytes()),
        capability.raw_sha256
    );

    // Unicode vertical catalog: pins its own catalog_hash + unicode_version.
    let catalog = by_name("scripts/unicode_vertical_catalog.json");
    assert!(catalog.match_pinned, "catalog self-hash must match");
    let value: serde_json::Value =
        serde_json::from_str(UNICODE_VERTICAL_CATALOG_JSON).expect("catalog is JSON");
    assert_eq!(value["catalog_hash"], UNICODE_CATALOG_HASH);
    assert_eq!(value["unicode_version"], UNICODE_CATALOG_VERSION);
    assert_eq!(
        bytes_sha256(UNICODE_VERTICAL_CATALOG_JSON.as_bytes()),
        catalog.raw_sha256
    );

    // Raw file identities recorded by the packaging pipeline must be stable
    // (these are the values the release bundle publishes in SHA256SUMS.txt).
    assert_eq!(
        schema.raw_sha256,
        "9b09b5c8405b7c05079b98e105f3c2813d7c0dbc270bcd34246e66ee01524eaf"
    );
    assert_eq!(
        capability.raw_sha256,
        "fbd26ce9b84e21a5deb05cc8e5c947ad89b144cd1d6df99b60e84cd3e6743a32"
    );
    assert_eq!(
        catalog.raw_sha256,
        "3e56f04c317b67e6405c89a33bae713bb318907a72cecd3023685d96521bb3ec"
    );
}

#[test]
fn embedded_bytes_are_self_consistent() {
    // The semantic hash recomputed from the embedded bytes must equal the
    // frozen constant that `engine_descriptor` reports - i.e. the binary
    // cannot drift from its own descriptor.
    let schema: serde_json::Value =
        serde_json::from_str(SCHEMA_BUNDLE_JSON).expect("schema bundle is JSON");
    assert_eq!(semantic_sha256(&schema), SCHEMA_BUNDLE_SHA256);

    let manifest: serde_json::Value =
        serde_json::from_str(CAPABILITY_MANIFEST_JSON).expect("capability manifest is JSON");
    assert_eq!(semantic_sha256(&manifest), CAPABILITY_MANIFEST_SHA256);

    // Exactly three assets, all pinned.
    assert_eq!(assets().len(), 3);
}

#[test]
fn audit_uses_the_embedded_catalog_without_repo_checkout() {
    // Run the audit operation with no --catalog override: the engine must
    // fall back to the embedded catalog (reported as path "embedded") and
    // succeed with no repo-relative file access.
    let engine = Engine::new();
    let outcome = engine
        .execute(
            Operation::Audit,
            OperationContext::new(docx2typed_protocol::new_operation_id()),
            OperationArgs::Audit(AuditArgs {
                source: fixture(),
                catalog_path: None,
            }),
        )
        .expect("engine runs");
    assert_eq!(outcome.outcome, Outcome::Success, "audit succeeds");
    let data = outcome.data;
    assert_eq!(data["catalog"]["path"], "embedded");
    assert_eq!(data["catalog"]["unicode_version"], UNICODE_CATALOG_VERSION);
    assert_eq!(data["catalog"]["catalog_hash"], UNICODE_CATALOG_HASH);
}
