//! Issue #60: secured review collaboration lane for the Rust tracer.
//!
//! - `security`: the issue #31 transport-security primitives (capability,
//!   Host allowlist, origin gates, throttle, security headers).
//! - `queue`: the file-backed review event queue (`.review/inbox`).
//! - `collab`: the document/session state machine, settlement, and the
//!   external-write guard.

pub mod collab;
pub mod draft;
pub mod queue;
pub mod security;
pub mod server;

pub use collab::{CollaborationError, PATCH_SCHEMA, SNAPSHOT_SCHEMA};
