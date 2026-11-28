use std::env;
use std::fs::{File, Metadata};
use std::io::{self, Read};
use std::path::Path;
use std::process::ExitCode;

const MAX_POLICY_BYTES: u64 = 65_536;
const HELP: &str = concat!(
    "Decision Browser\n\nUsage: decision-browser <command>\n\nCommands:\n",
    "  serve [--admission standard_form|task_scope]   Serve MCP over stdio\n",
    "  setup [codex|claude|both]                       Register a client, prompting if omitted in a terminal\n",
    "  install codex|claude|both                      Register the native server\n",
    "  upgrade [codex|claude|both]                     Update a project registration\n",
    "  uninstall [codex|claude|both]                   Remove a project registration\n",
    "  configure <provider>                           Save a provider preset\n",
    "  doctor | install-status | recover-install      Inspect or recover an installation\n",
    "  probe-browser [--existing]                      Verify CDP on an owned local fixture\n",
    "  --help | --version                             Print command or release information\n",
    "\nUse --project PATH for project installation, diagnostics or recovery.",
);

fn absent<T>(result: io::Result<T>) -> io::Result<Option<T>> {
    match result {
        Ok(value) => Ok(Some(value)),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

#[cfg(unix)]
fn trusted(metadata: &Metadata, directory: bool) -> bool {
    use std::os::unix::fs::MetadataExt;

    // SAFETY: getuid has no arguments or memory effects.
    let own_user = unsafe { libc::getuid() };
    (if directory {
        metadata.is_dir()
    } else {
        metadata.is_file() && metadata.nlink() == 1
    }) && metadata.uid() == own_user
        && metadata.mode() & 0o022 == 0
}

#[cfg(not(unix))]
fn trusted(metadata: &Metadata, directory: bool) -> bool {
    if directory {
        metadata.is_dir()
    } else {
        metadata.is_file()
    }
}

#[cfg(unix)]
fn same_file(first: &Metadata, second: &Metadata) -> bool {
    use std::os::unix::fs::MetadataExt;
    (first.dev(), first.ino()) == (second.dev(), second.ino())
}

#[cfg(not(unix))]
fn same_file(first: &Metadata, second: &Metadata) -> bool {
    first
        .created()
        .ok()
        .is_some_and(|created| second.created().ok() == Some(created))
}

fn open_policy(path: &Path) -> io::Result<File> {
    let mut options = File::options();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    options.open(path)
}

fn read_project_policy(root: &Path) -> Result<Option<toml::Value>, &'static str> {
    let directory = root.join(".codex");
    let path = directory.join("config.toml");
    let unreadable = |_| "project_policy_unreadable";
    let Some(parent) = absent(directory.symlink_metadata()).map_err(unreadable)? else {
        return Ok(None);
    };
    if !trusted(&parent, true) {
        return Err("project_policy_unreadable");
    }
    let Some(before) = absent(path.symlink_metadata()).map_err(unreadable)? else {
        return Ok(None);
    };
    if !trusted(&before, false) {
        return Err("project_policy_unreadable");
    }
    let file = open_policy(&path).map_err(unreadable)?;
    let opened = file.metadata().map_err(unreadable)?;
    if !trusted(&opened, false) || !same_file(&before, &opened) {
        return Err("project_policy_unreadable");
    }
    let mut bytes = Vec::new();
    file.take(MAX_POLICY_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(unreadable)?;
    let after = path.symlink_metadata().map_err(unreadable)?;
    let parent_after = directory.symlink_metadata().map_err(unreadable)?;
    if bytes.len() as u64 > MAX_POLICY_BYTES
        || !trusted(&after, false)
        || !same_file(&opened, &after)
        || !trusted(&parent_after, true)
        || !same_file(&parent, &parent_after)
    {
        return Err("project_policy_unreadable");
    }
    let text = std::str::from_utf8(&bytes).map_err(|_| "project_policy_unreadable")?;
    toml::from_str(text)
        .map(Some)
        .map_err(|_| "project_policy_invalid")
}

fn mode(value: &str) -> Result<&'static str, &'static str> {
    match value {
        "standard_form" => Ok("standard_form"),
        "task_scope" => Ok("task_scope"),
        _ => Err("project_policy_invalid"),
    }
}

fn matching_path(value: Option<&str>, expected: &Path) -> bool {
    value
        .and_then(|value| Path::new(value).canonicalize().ok())
        .is_some_and(|path| path == expected)
}

fn project_mode(
    root: &Path,
    executable: &Path,
) -> Result<(&'static str, &'static str), &'static str> {
    let default = ("standard_form", "default_standard_form");
    let Some(policy) = read_project_policy(root)? else {
        return Ok(default);
    };
    let Some(server) = policy
        .get("mcp_servers")
        .and_then(|servers| servers.get("decision_browser"))
        .and_then(toml::Value::as_table)
    else {
        return Ok(default);
    };
    if !matching_path(
        server.get("command").and_then(toml::Value::as_str),
        executable,
    ) || !matching_path(server.get("cwd").and_then(toml::Value::as_str), root)
    {
        return Ok(default);
    }
    let Some(args) = server.get("args").and_then(toml::Value::as_array) else {
        return Err("project_policy_invalid");
    };
    if args.first().and_then(toml::Value::as_str) != Some("serve") {
        return Ok(default);
    }
    match args.as_slice() {
        [_] => Ok(("standard_form", "project_launcher")),
        [_, flag, value] if flag.as_str() == Some("--admission") => Ok((
            mode(value.as_str().ok_or("project_policy_invalid")?)?,
            "project_launcher",
        )),
        _ => Err("project_policy_invalid"),
    }
}

fn serve_mode(args: &[String]) -> Result<(&'static str, &'static str), &'static str> {
    match args {
        [] => {
            let root = env::current_dir()
                .and_then(|path| path.canonicalize())
                .map_err(|_| "project_policy_unreadable")?;
            let executable = env::current_exe()
                .and_then(|path| path.canonicalize())
                .map_err(|_| "project_policy_unreadable")?;
            project_mode(&root, &executable)
        }
        [flag, value] if flag == "--admission" => Ok((mode(value)?, "explicit_cli")),
        [flag] if flag.starts_with("--admission=") => {
            Ok((mode(&flag["--admission=".len()..])?, "explicit_cli"))
        }
        _ => Err("invalid serve arguments"),
    }
}

fn probe_browser(args: &[String]) -> Result<(), String> {
    use crate::browser::{MatrixPage, run_existing_task_scenario, run_task_scenario};
    use serde_json::json;
    use std::time::Duration;

    let existing = match args {
        [] => false,
        [flag] if flag == "--existing" => true,
        _ => return Err("expected probe-browser [--existing]".to_owned()),
    };
    let started = std::time::Instant::now();
    let fixture = crate::matrix_fixtures::start("click").map_err(|_| "fixture_unavailable")?;
    let runner = if existing {
        run_existing_task_scenario
    } else {
        run_task_scenario
    };
    runner(
        &fixture.url,
        Duration::from_secs(30),
        &|| Ok(()),
        |page: &mut MatrixPage| {
            let state = page.snapshot()?;
            let target = state["choices"]
                .as_array()
                .and_then(|choices| choices.iter().find(|choice| choice["label"] == "Activate"))
                .and_then(|choice| choice["id"].as_str())
                .ok_or("fixture control unavailable")?;
            page.act("click", target, None)?;
            let all_checks = fixture
                .checks
                .as_array()
                .ok_or("fixture checks unavailable")?;
            for check in all_checks {
                if !page.read_check(check)? {
                    return Err("fixture check failed".into());
                }
            }
            if !fixture.verify()? {
                return Err("fixture effect unverified".into());
            }
            Ok(())
        },
    )
    .map_err(|_| "browser_probe_failed")?;
    println!(
        "{}",
        json!({"status":"verified","mode":if existing {"existing_chrome"} else {"isolated"},"elapsed_ms":started.elapsed().as_millis()})
    );
    Ok(())
}

pub fn run(args: &[String]) -> ExitCode {
    let result = match args {
        [flag] if flag == "--help" || flag == "-h" => {
            println!("{HELP}");
            Ok(())
        }
        [flag] if flag == "--version" || flag == "-V" => {
            println!("decision-browser {}", env!("CARGO_PKG_VERSION"));
            Ok(())
        }
        [command, tail @ ..] if command == "serve" => match serve_mode(tail) {
            Ok((admission, source)) => {
                eprintln!("admission_policy_source: {source}");
                crate::product_mcp::serve(admission).map_err(|_| "stdio_server_failed".to_owned())
            }
            Err(error) => Err(error.to_owned()),
        },
        [command, tail @ ..] if command == "probe-browser" => probe_browser(tail),
        [command, ..]
            if matches!(
                command.as_str(),
                "setup"
                    | "install"
                    | "upgrade"
                    | "uninstall"
                    | "configure"
                    | "doctor"
                    | "install-status"
                    | "recover-install"
            ) =>
        {
            crate::native_install::dispatch(args).map_err(|error| error.to_string())
        }
        _ => {
            eprintln!("{HELP}");
            Err("missing or unknown command".to_owned())
        }
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("error: {error}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn explicit_policy_keeps_a_strict_enum() {
        assert_eq!(
            serve_mode(&["--admission".to_owned(), "task_scope".to_owned()]),
            Ok(("task_scope", "explicit_cli"))
        );
        assert!(serve_mode(&["--admission=unknown".to_owned()]).is_err());
        assert!(serve_mode(&["--admission".to_owned()]).is_err());
    }
}
