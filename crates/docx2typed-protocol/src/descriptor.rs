//! Engine descriptor + contract/feature negotiation, mirroring
//! `scripts/protocol.py` (`engine_descriptor`/`CONTRACT_RANGES`/`FEATURES`/
//! `negotiate`).

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::{
    CAPABILITY_MANIFEST_SCHEMA, CAPABILITY_MANIFEST_SHA256, ENGINE_DESCRIPTOR_SCHEMA, ENGINE_NAME,
    SCHEMA_BUNDLE_SCHEMA, SCHEMA_BUNDLE_SHA256,
};

/// Frozen contract ranges (major 1, minor 0 only).
pub const CONTRACT_RANGES: [(&str, ContractRange); 5] = [
    (
        "cli",
        ContractRange {
            major: 1,
            min_minor: 0,
            max_minor: 0,
        },
    ),
    (
        "mcp",
        ContractRange {
            major: 1,
            min_minor: 0,
            max_minor: 0,
        },
    ),
    (
        "result",
        ContractRange {
            major: 1,
            min_minor: 0,
            max_minor: 0,
        },
    ),
    (
        "evidence",
        ContractRange {
            major: 1,
            min_minor: 0,
            max_minor: 0,
        },
    ),
    (
        "workdir",
        ContractRange {
            major: 1,
            min_minor: 0,
            max_minor: 0,
        },
    ),
];

pub const FEATURES: [&str; 3] = ["hybrid-fidelity", "locked-structure", "typed-mode"];
pub const REQUIRED_FEATURES: [&str; 3] = FEATURES;

/// The frozen finite-command set. This slice implements extract/build/verify;
/// the remaining finite commands land with their operations in #56+.
pub const PROTOCOL_COMMANDS: [&str; 3] = ["extract", "build", "verify"];
pub const PROTOCOL_TOOLS: [&str; 2] = ["engine_info", "workdir_open"];

/// Version declared in the descriptor. Python's fallback package version is
/// `0.1.0rc1` (its own `_package_version()`); the Rust descriptor mirrors it
/// verbatim for negotiation parity even though Cargo's semver is
/// `0.1.0-rc1`.
pub const PACKAGE_VERSION: &str = "0.1.0rc1";

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct ContractRange {
    pub major: i64,
    pub min_minor: i64,
    pub max_minor: i64,
}

#[derive(Clone, Debug, Serialize)]
pub struct EngineDescriptor {
    pub schema: &'static str,
    pub name: &'static str,
    pub version: &'static str,
    pub build_commit: String,
    pub target: String,
    pub contracts: Value,
    pub schema_bundle: Value,
    pub capability_manifest: Value,
    pub commands: Value,
    pub tools: Vec<&'static str>,
    pub features: Vec<&'static str>,
    pub required_features: Vec<&'static str>,
}

/// Build the Protocol-major-1 engine descriptor. `build_commit` comes from
/// `$DOCX2TYPED_BUILD_COMMIT` at call time (mirroring the Python contract:
/// the descriptor is never cached so an injected commit is always observed).
pub fn engine_descriptor(build_commit: &str) -> EngineDescriptor {
    let target = format!("{}-{}-rust", std::env::consts::OS, std::env::consts::ARCH);
    EngineDescriptor {
        schema: ENGINE_DESCRIPTOR_SCHEMA,
        name: ENGINE_NAME,
        version: PACKAGE_VERSION,
        build_commit: if build_commit.is_empty() {
            "unknown".to_string()
        } else {
            build_commit.to_string()
        },
        target,
        contracts: contracts_json(),
        schema_bundle: serde_json::json!({
            "schema": SCHEMA_BUNDLE_SCHEMA,
            "sha256": SCHEMA_BUNDLE_SHA256,
        }),
        capability_manifest: serde_json::json!({
            "schema": CAPABILITY_MANIFEST_SCHEMA,
            "sha256": CAPABILITY_MANIFEST_SHA256,
        }),
        commands: serde_json::json!({
            "finite": PROTOCOL_COMMANDS,
            "launchers": ["mcp"],
        }),
        tools: PROTOCOL_TOOLS.to_vec(),
        features: FEATURES.to_vec(),
        required_features: REQUIRED_FEATURES.to_vec(),
    }
}

fn contracts_json() -> Value {
    let mut map = serde_json::Map::new();
    for (name, range) in CONTRACT_RANGES {
        map.insert(
            name.to_string(),
            serde_json::json!({
                "major": range.major,
                "min_minor": range.min_minor,
                "max_minor": range.max_minor,
            }),
        );
    }
    Value::Object(map)
}
