fn main() -> std::process::ExitCode {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    decision_browser_rust_cdp::native_cli::run(&args)
}
