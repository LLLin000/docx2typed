//! Contract/feature negotiation, mirroring `scripts/protocol.py`
//! (`negotiate`): raises a typed mismatch instead of panicking so the MCP
//! adapter can convert it into a failure Result envelope.

use serde_json::Value;

use crate::descriptor::{CONTRACT_RANGES, FEATURES, REQUIRED_FEATURES};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum NegotiationError {
    ContractIncompatible {
        contract: String,
        engine_range: Value,
        client_range: Value,
    },
    RequiredFeatureUnsupported {
        missing_features: Vec<String>,
    },
}

impl NegotiationError {
    pub fn code(&self) -> &'static str {
        match self {
            NegotiationError::ContractIncompatible { .. } => "contract-incompatible",
            NegotiationError::RequiredFeatureUnsupported { .. } => "required-feature-unsupported",
        }
    }

    pub fn message(&self) -> String {
        match self {
            NegotiationError::ContractIncompatible { contract, .. } => {
                format!("no compatible {contract} contract version")
            }
            NegotiationError::RequiredFeatureUnsupported { .. } => {
                "required features are unsupported".to_string()
            }
        }
    }

    pub fn details(&self) -> Value {
        match self {
            NegotiationError::ContractIncompatible {
                contract,
                engine_range,
                client_range,
            } => serde_json::json!({
                "contract": contract,
                "engine_range": engine_range,
                "client_range": client_range,
            }),
            NegotiationError::RequiredFeatureUnsupported { missing_features } => {
                serde_json::json!({ "missing_features": missing_features })
            }
        }
    }
}

/// Validate a client's declared contract ranges and feature support against
/// this engine. Absent client values mean "same as engine" (mirroring the
/// Python defaults).
pub fn negotiate(
    contract_ranges: Option<&Value>,
    supported_features: Option<&[String]>,
    required_features: Option<&[String]>,
) -> Result<(), NegotiationError> {
    if let Some(ranges) = contract_ranges {
        if let Some(object) = ranges.as_object() {
            for (name, client) in object {
                let engine = CONTRACT_RANGES
                    .iter()
                    .find(|(engine_name, _)| engine_name == name)
                    .map(|(_, range)| *range);
                let client_major = client.get("major").and_then(Value::as_i64);
                let client_min_minor = client.get("min_minor").and_then(Value::as_i64).unwrap_or(0);
                let client_max_minor = client
                    .get("max_minor")
                    .and_then(Value::as_i64)
                    .unwrap_or(client_min_minor);
                let compatible = match engine {
                    None => false,
                    Some(engine) => {
                        client_major == Some(engine.major)
                            && client_min_minor <= engine.max_minor
                            && client_max_minor >= engine.min_minor
                    }
                };
                if !compatible {
                    let engine_range = engine
                        .map(|range| {
                            serde_json::json!({
                                "major": range.major,
                                "min_minor": range.min_minor,
                                "max_minor": range.max_minor,
                            })
                        })
                        .unwrap_or(Value::Null);
                    return Err(NegotiationError::ContractIncompatible {
                        contract: name.clone(),
                        engine_range,
                        client_range: client.clone(),
                    });
                }
            }
        }
    }

    let engine_required: Vec<&str> = REQUIRED_FEATURES.to_vec();
    let engine_features: Vec<&str> = FEATURES.to_vec();
    let client_supported: Vec<String> = match supported_features {
        Some(list) => list.to_vec(),
        None => engine_required.iter().map(|f| f.to_string()).collect(),
    };
    let client_required: Vec<String> = required_features
        .map(|list| list.to_vec())
        .unwrap_or_default();

    let mut missing: Vec<String> = Vec::new();
    for feature in engine_required {
        if !client_supported.iter().any(|f| f == feature) {
            missing.push(feature.to_string());
        }
    }
    for feature in client_required {
        if !engine_features.iter().any(|f| *f == feature) {
            missing.push(feature);
        }
    }
    missing.sort();
    missing.dedup();
    if !missing.is_empty() {
        return Err(NegotiationError::RequiredFeatureUnsupported {
            missing_features: missing,
        });
    }
    Ok(())
}
