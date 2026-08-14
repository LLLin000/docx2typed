//! Independent verifier (issue #55 slice): read-only, verifies a built
//! output package against the workdir's immutable template package. The
//! request carries only immutable byte sources (workdir + output paths) plus
//! a profile; the verifier never receives Core AST, build plans, or
//! mutation state, and never writes. It implements its own package walking
//! and hashing (zip + sha2) rather than calling Core's — per issue #36.
//!
//! The no-op verification is the strongest one available: every output part
//! must be byte-identical (SHA-256) to the template part, and the whole
//! output file must equal the template file (copy-if-unchanged contract).

use std::collections::BTreeMap;
use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};

use docx2typed_protocol::{bytes_sha256, file_sha256};
use zip::ZipArchive;

#[derive(Clone, Debug)]
pub struct VerificationRequest {
    /// Immutable input: the typed workdir whose `_template.docx` is the
    /// authoritative package baseline.
    pub workdir: PathBuf,
    /// Immutable input: the built output package under verification.
    pub output: PathBuf,
    /// Check profile name (S / L / X); the slice records it in evidence.
    pub profile: String,
}

#[derive(Clone, Debug)]
pub struct VerificationCheck {
    pub name: String,
    pub status: String, // pass | fail | not-applicable
    #[allow(dead_code)]
    pub detail: Option<String>,
}

/// Canonical evidence payload produced by the verifier. The App engine wraps
/// this into a `docx2typed-run-evidence-1` record.
#[derive(Clone, Debug)]
pub struct VerificationEvidence {
    pub verdict: String, // pass | fail
    pub checks: Vec<VerificationCheck>,
    pub output_sha256: String,
    pub template_sha256: String,
    pub parts_identical: bool,
    pub profile: String,
}

pub struct IndependentVerifier;

impl Default for IndependentVerifier {
    fn default() -> Self {
        Self::new()
    }
}

impl IndependentVerifier {
    pub fn new() -> Self {
        IndependentVerifier
    }

    pub fn verify(&self, request: &VerificationRequest) -> VerificationEvidence {
        let mut checks: Vec<VerificationCheck> = Vec::new();
        let template = request.workdir.join("_template.docx");
        let template_sha256 = file_sha256(&template).unwrap_or_default();
        let output_sha256 = file_sha256(&request.output).unwrap_or_default();

        // Check 1: output package opens as a zip.
        let output_parts = match package_parts(&request.output) {
            Some(parts) => {
                checks.push(VerificationCheck {
                    name: "package-openable".to_string(),
                    status: "pass".to_string(),
                    detail: Some(format!("{} parts", parts.len())),
                });
                parts
            }
            None => {
                checks.push(VerificationCheck {
                    name: "package-openable".to_string(),
                    status: "fail".to_string(),
                    detail: Some("output docx unreadable".to_string()),
                });
                BTreeMap::new()
            }
        };

        // Check 2: output parts byte-identical to the template parts.
        let template_parts = package_parts(&template).unwrap_or_default();
        let mut changed: Vec<String> = Vec::new();
        let mut added: Vec<String> = Vec::new();
        let mut removed: Vec<String> = Vec::new();
        for (name, hash) in &template_parts {
            match output_parts.get(name) {
                Some(output_hash) if output_hash == hash => {}
                Some(_) => changed.push(name.clone()),
                None => removed.push(name.clone()),
            }
        }
        for name in output_parts.keys() {
            if !template_parts.contains_key(name) {
                added.push(name.clone());
            }
        }
        let parts_identical = changed.is_empty() && added.is_empty() && removed.is_empty();
        let parts_status = if parts_identical { "pass" } else { "fail" };
        checks.push(VerificationCheck {
            name: "parts-match-template".to_string(),
            status: parts_status.to_string(),
            detail: Some(format!(
                "changed={} added={} removed={}",
                changed.len(),
                added.len(),
                removed.len()
            )),
        });

        // Check 3: whole-file byte identity (copy-if-unchanged).
        let whole_identical = !output_sha256.is_empty() && output_sha256 == template_sha256;
        let whole_status = if whole_identical { "pass" } else { "fail" };
        checks.push(VerificationCheck {
            name: "output-identical-to-template".to_string(),
            status: whole_status.to_string(),
            detail: Some(format!(
                "output={} template={}",
                short(&output_sha256),
                short(&template_sha256)
            )),
        });

        let verdict = if checks.iter().all(|check| check.status == "pass") {
            "pass"
        } else {
            "fail"
        };
        VerificationEvidence {
            verdict: verdict.to_string(),
            checks,
            output_sha256,
            template_sha256,
            parts_identical: parts_identical && whole_identical,
            profile: request.profile.clone(),
        }
    }
}

fn package_parts(path: &Path) -> Option<BTreeMap<String, String>> {
    let file = File::open(path).ok()?;
    let mut archive = ZipArchive::new(file).ok()?;
    let mut parts = BTreeMap::new();
    for index in 0..archive.len() {
        let mut member = archive.by_index(index).ok()?;
        let name = member.name().to_string();
        let mut buf = Vec::new();
        member.read_to_end(&mut buf).ok()?;
        parts.insert(name, bytes_sha256(&buf));
    }
    Some(parts)
}

fn short(hash: &str) -> String {
    if hash.len() >= 12 {
        hash[..12].to_string()
    } else {
        hash.to_string()
    }
}
