use std::error::Error;
use std::fs;
use std::path::{Path, PathBuf};

type Result<T> = std::result::Result<T, Box<dyn Error>>;

fn stable_version(version: &str) -> bool {
    let parts = version.split('.').collect::<Vec<_>>();
    parts.len() == 3
        && parts.iter().all(|part| {
            !part.is_empty()
                && part.bytes().all(|byte| byte.is_ascii_digit())
                && (part.len() == 1 || !part.starts_with('0'))
        })
}

fn calendar_date(date: &str) -> bool {
    if date.len() != 10 || date.as_bytes()[4] != b'-' || date.as_bytes()[7] != b'-' {
        return false;
    }
    if !date
        .bytes()
        .enumerate()
        .all(|(index, byte)| index == 4 || index == 7 || byte.is_ascii_digit())
    {
        return false;
    }
    let year = date[..4].parse::<u32>().unwrap_or(0);
    let month = date[5..7].parse::<usize>().unwrap_or(0);
    let day = date[8..].parse::<u32>().unwrap_or(0);
    let leap = year.is_multiple_of(4) && (!year.is_multiple_of(100) || year.is_multiple_of(400));
    let days = [
        0,
        31,
        if leap { 29 } else { 28 },
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ];
    year > 0 && month > 0 && month < days.len() && day > 0 && day <= days[month]
}

fn check(root: &Path, manifest_path: &Path, tag: Option<&str>) -> Result<String> {
    let manifest: toml::Value = toml::from_str(&fs::read_to_string(manifest_path)?)?;
    let package = manifest
        .get("package")
        .ok_or("native package metadata is missing")?;
    let name = package
        .get("name")
        .and_then(toml::Value::as_str)
        .ok_or("native package name is missing")?;
    let version = package
        .get("version")
        .and_then(toml::Value::as_str)
        .ok_or("native package version is missing")?;
    if !stable_version(version) {
        return Err("native release version must be three canonical numeric components".into());
    }
    let license = package
        .get("license")
        .and_then(toml::Value::as_str)
        .unwrap_or("");
    if license != "MIT" || !fs::read_to_string(root.join("LICENSE"))?.starts_with("MIT License\n") {
        return Err("native package and root LICENSE must declare the reviewed MIT license".into());
    }
    let lock_path = manifest_path
        .parent()
        .ok_or("manifest has no parent directory")?
        .join("Cargo.lock");
    let lock: toml::Value = toml::from_str(&fs::read_to_string(lock_path)?)?;
    let entries = lock
        .get("package")
        .and_then(toml::Value::as_array)
        .ok_or("Cargo.lock package array is missing")?;
    let matching = entries
        .iter()
        .filter(|entry| {
            entry.get("name").and_then(toml::Value::as_str) == Some(name)
                && entry.get("source").is_none()
        })
        .collect::<Vec<_>>();
    if matching.len() != 1
        || matching[0].get("version").and_then(toml::Value::as_str) != Some(version)
    {
        return Err("native package version and Cargo.lock disagree".into());
    }
    if let Some(tag) = tag {
        if tag != format!("v{version}") {
            return Err("release tag must exactly equal v<package.version>".into());
        }
        let prefix = format!("## [{version}] - ");
        let changelog = fs::read_to_string(root.join("CHANGELOG.md"))?;
        let entries = changelog
            .lines()
            .filter_map(|line| line.strip_prefix(&prefix))
            .collect::<Vec<_>>();
        if entries.len() != 1 || !calendar_date(entries[0]) {
            return Err(
                "release needs one dated CHANGELOG entry with a valid calendar date".into(),
            );
        }
    }
    Ok(version.to_owned())
}

fn main() -> Result<()> {
    let mut manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("Cargo.toml");
    let mut root = None;
    let mut tag = None;
    let mut args = std::env::args().skip(1);
    while let Some(argument) = args.next() {
        match argument.as_str() {
            "--manifest-path" => {
                manifest = PathBuf::from(args.next().ok_or("--manifest-path requires a path")?)
            }
            "--root" => root = Some(PathBuf::from(args.next().ok_or("--root requires a path")?)),
            "--tag" => tag = Some(args.next().ok_or("--tag requires an exact version tag")?),
            "--help" | "-h" => {
                println!(
                    "Usage: check-native-release [--manifest-path Cargo.toml] [--root REPOSITORY] [--tag vVERSION]"
                );
                return Ok(());
            }
            _ => return Err("unknown native release check argument".into()),
        }
    }
    let manifest = manifest.canonicalize()?;
    let root = root
        .or_else(|| {
            manifest
                .ancestors()
                .find(|path| path.join("LICENSE").is_file() && path.join("CHANGELOG.md").is_file())
                .map(Path::to_path_buf)
        })
        .ok_or("repository root was not found")?;
    println!("Version OK: {}", check(&root, &manifest, tag.as_deref())?);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let nonce = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let root = std::env::temp_dir().join(format!(
                "native-release-check-{}-{nonce}",
                std::process::id()
            ));
            fs::create_dir(&root).unwrap();
            fs::write(
                root.join("Cargo.toml"),
                "[package]\nname='decision-browser'\nversion='0.1.4'\nlicense='MIT'\n",
            )
            .unwrap();
            fs::write(
                root.join("Cargo.lock"),
                "version=4\n[[package]]\nname='decision-browser'\nversion='0.1.4'\n",
            )
            .unwrap();
            fs::write(root.join("LICENSE"), "MIT License\nReviewed license text\n").unwrap();
            fs::write(root.join("CHANGELOG.md"), "## [0.1.4] - 2026-11-28\n").unwrap();
            Self(root)
        }
        fn check(&self, tag: Option<&str>) -> Result<String> {
            check(&self.0, &self.0.join("Cargo.toml"), tag)
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn consistent_native_package_passes_exact_tag() {
        let fixture = Fixture::new();
        assert_eq!(fixture.check(Some("v0.1.4")).unwrap(), "0.1.4");
        assert!(fixture.check(Some("v0.1.3")).is_err());
    }
    #[test]
    fn changed_lock_and_missing_license_are_rejected() {
        let fixture = Fixture::new();
        fs::write(
            fixture.0.join("Cargo.lock"),
            "[[package]]\nname='decision-browser'\nversion='0.1.3'\n",
        )
        .unwrap();
        assert!(fixture.check(None).is_err());
        fs::remove_file(fixture.0.join("LICENSE")).unwrap();
        assert!(fixture.check(None).is_err());
    }
    #[test]
    fn tag_requires_a_real_date_and_unique_entry() {
        let fixture = Fixture::new();
        fs::write(fixture.0.join("CHANGELOG.md"), "## [0.1.4] - 2026-02-29\n").unwrap();
        assert!(fixture.check(None).is_ok());
        assert!(fixture.check(Some("v0.1.4")).is_err());
        fs::write(
            fixture.0.join("CHANGELOG.md"),
            "## [0.1.4] - 2026-11-28\n## [0.1.4] - 2026-11-28\n",
        )
        .unwrap();
        assert!(fixture.check(Some("v0.1.4")).is_err());
        assert!(calendar_date("2024-02-29"));
        assert!(!calendar_date("2026-13-01"));
    }
}
