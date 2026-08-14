//! Installed `docx2typed` binary: concrete CLI and MCP(stdout) adapters
//! over the one synchronous Engine (issue #55 slice). Adapters parse and
//! validate transport DTOs, invoke the Engine, and serialize the common
//! `docx2typed-result-1` envelope; they never reassemble Engine order.

mod cli;
mod mcp;

use docx2typed_app::Operation;

fn main() {
    // Windows' default main-thread stack is 1 MiB; the extraction pipeline
    // (zip member reads + hashing) needs more headroom in debug builds, so
    // run the real entry point on a spawned thread with an explicit stack.
    let handle = std::thread::Builder::new()
        .stack_size(16 * 1024 * 1024)
        .spawn(run_main)
        .expect("spawn entry thread");
    let exit_code = handle.join().unwrap_or(1);
    std::process::exit(exit_code);
}

fn run_main() -> i32 {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    // `--json` may appear anywhere in the invocation (Python semantics).
    let json_mode = argv.iter().any(|arg| arg == "--json");
    let argv: Vec<String> = argv.into_iter().filter(|arg| arg != "--json").collect();
    let build_commit = std::env::var("DOCX2TYPED_BUILD_COMMIT").unwrap_or_default();
    dispatch(&argv, json_mode, &build_commit)
}

fn dispatch(argv: &[String], json_mode: bool, build_commit: &str) -> i32 {
    if argv.is_empty() {
        if json_mode {
            cli::print_invocation_failure("", "no command given", argv);
        } else {
            eprintln!("usage: docx2typed <command> [--json] [options]");
        }
        return 1;
    }
    if argv == ["--version"] {
        let descriptor = docx2typed_protocol::engine_descriptor(build_commit);
        if json_mode {
            println!(
                "{}",
                serde_json::to_string(&descriptor).expect("descriptor serializes")
            );
        } else {
            println!(
                "{} {} ({})",
                descriptor.name, descriptor.version, descriptor.build_commit
            );
        }
        return 0;
    }
    match argv[0].as_str() {
        "extract" => cli::run(Operation::Extract, &argv[1..], json_mode, build_commit),
        "build" => cli::run(Operation::Build, &argv[1..], json_mode, build_commit),
        "verify" => cli::run(Operation::Verify, &argv[1..], json_mode, build_commit),
        "mcp" => mcp::run(build_commit),
        other => {
            if json_mode {
                cli::print_invocation_failure(
                    other,
                    &format!("no Protocol-major-1 --json contract for command: {other}"),
                    argv,
                );
            } else {
                eprintln!("Unknown command: {other}");
            }
            1
        }
    }
}
