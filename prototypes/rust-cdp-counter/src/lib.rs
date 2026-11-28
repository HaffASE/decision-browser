pub mod browser;
pub mod matrix_fixtures;
pub mod native_cli;
pub mod native_install;
pub mod product_mcp;
pub mod provider;
pub mod task_runner;
pub mod tool_contracts;

fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}
