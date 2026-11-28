//! Recovery of legacy stage barriers using authenticated evidence across boots.
//! Inspection never executes retained code or changes the retained runtime.
use super::{Result, STATE, checksum, open_lease, sync_directory};
use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::cell::Cell;
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Component, Path, PathBuf};

const LEASE: &str = ".decision-browser-install.lease";
const RESIDUAL: &str = ".decision-browser-stage-cleanup-unknown.json";
const IDLE: &[u8] = b"{\"owner\":\"decision-browser-lease-v1\"}\n";
const POISON: &[u8] = b"{\"blocked\":\"stage-cleanup-unknown\"}\n";
const MAX_FILE: u64 = 2 * 1024 * 1024;

struct Unique(Value);
impl<'de> Deserialize<'de> for Unique {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        struct JsonVisitor;
        impl<'de> Visitor<'de> for JsonVisitor {
            type Value = Unique;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("JSON without duplicate keys")
            }
            fn visit_map<M: MapAccess<'de>>(
                self,
                mut map: M,
            ) -> std::result::Result<Unique, M::Error> {
                let mut object = serde_json::Map::new();
                while let Some((key, value)) = map.next_entry::<String, Unique>()? {
                    if object.insert(key, value.0).is_some() {
                        return Err(de::Error::custom("duplicate key"));
                    }
                }
                Ok(Unique(Value::Object(object)))
            }
            fn visit_seq<S: SeqAccess<'de>>(
                self,
                mut seq: S,
            ) -> std::result::Result<Unique, S::Error> {
                let mut items = Vec::new();
                while let Some(value) = seq.next_element::<Unique>()? {
                    items.push(value.0);
                }
                Ok(Unique(Value::Array(items)))
            }
            fn visit_bool<E: de::Error>(self, value: bool) -> std::result::Result<Unique, E> {
                Ok(Unique(json!(value)))
            }
            fn visit_i64<E: de::Error>(self, value: i64) -> std::result::Result<Unique, E> {
                Ok(Unique(json!(value)))
            }
            fn visit_u64<E: de::Error>(self, value: u64) -> std::result::Result<Unique, E> {
                Ok(Unique(json!(value)))
            }
            fn visit_f64<E: de::Error>(self, value: f64) -> std::result::Result<Unique, E> {
                Ok(Unique(json!(value)))
            }
            fn visit_str<E: de::Error>(self, value: &str) -> std::result::Result<Unique, E> {
                Ok(Unique(json!(value)))
            }
            fn visit_string<E: de::Error>(self, value: String) -> std::result::Result<Unique, E> {
                Ok(Unique(json!(value)))
            }
            fn visit_unit<E: de::Error>(self) -> std::result::Result<Unique, E> {
                Ok(Unique(Value::Null))
            }
            fn visit_none<E: de::Error>(self) -> std::result::Result<Unique, E> {
                self.visit_unit()
            }
        }
        deserializer.deserialize_any(JsonVisitor)
    }
}

fn parse(bytes: &[u8]) -> Result<Value> {
    serde_json::from_slice::<Unique>(bytes)
        .map(|v| v.0)
        .map_err(|_| "invalid_recovery_evidence".into())
}

// Match the existing witness encoding, including ASCII escapes, so previously
// recorded evidence can be checked with the original trusted account key.
fn encoded(value: &Value, compact: bool) -> Vec<u8> {
    fn append(value: &Value, compact: bool, out: &mut String) {
        match value {
            Value::Array(items) => {
                out.push('[');
                for (index, value) in items.iter().enumerate() {
                    if index != 0 {
                        out.push_str(if compact { "," } else { ", " });
                    }
                    append(value, compact, out);
                }
                out.push(']');
            }
            Value::Object(items) => {
                out.push('{');
                for (index, (key, value)) in items.iter().enumerate() {
                    if index != 0 {
                        out.push_str(if compact { "," } else { ", " });
                    }
                    append(&json!(key), compact, out);
                    out.push_str(if compact { ":" } else { ": " });
                    append(value, compact, out);
                }
                out.push('}');
            }
            Value::String(value) => {
                let quoted = serde_json::to_string(value).expect("string encoding");
                for character in quoted.chars() {
                    if character.is_ascii() {
                        out.push(character);
                    } else {
                        let mut units = [0; 2];
                        for unit in character.encode_utf16(&mut units) {
                            use std::fmt::Write;
                            write!(out, "\\u{unit:04x}").expect("string write");
                        }
                    }
                }
            }
            _ => out.push_str(&value.to_string()),
        }
    }
    let mut result = String::new();
    append(value, compact, &mut result);
    result.into_bytes()
}

fn signature(value: &Value, key: &[u8]) -> String {
    let mut body = value.clone();
    body.as_object_mut().expect("witness object").remove("mac");
    let mut inner = [0x36u8; 64];
    let mut outer = [0x5cu8; 64];
    for (index, byte) in key.iter().enumerate() {
        inner[index] ^= byte;
        outer[index] ^= byte;
    }
    let mut digest = Sha256::new();
    digest.update(inner);
    digest.update(encoded(&body, true));
    let first = digest.finalize();
    let mut digest = Sha256::new();
    digest.update(outer);
    digest.update(first);
    digest
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn same_mac(left: &str, right: &str) -> bool {
    left.len() == right.len()
        && left
            .bytes()
            .zip(right.bytes())
            .fold(0, |result, (a, b)| result | (a ^ b))
            == 0
}

fn hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

#[cfg(unix)]
fn metadata(info: &fs::Metadata, links: bool) -> Value {
    use std::os::unix::fs::MetadataExt;
    let mut items = vec![
        json!(info.dev()),
        json!(info.ino()),
        json!(info.mode()),
        json!(info.uid()),
    ];
    if links {
        items.push(json!(info.nlink()));
    }
    items.extend([
        json!(info.len()),
        json!(info.mtime() * 1_000_000_000 + info.mtime_nsec()),
    ]);
    if links {
        items.push(json!(info.ctime() * 1_000_000_000 + info.ctime_nsec()));
    }
    json!(items)
}

#[cfg(unix)]
fn validate_directory(path: &Path, private: bool) -> Result<File> {
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    let directory = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_DIRECTORY)
        .open(path)
        .map_err(|_| "unsafe_recovery_directory")?;
    let info = directory.metadata()?;
    if info.uid() != unsafe { libc::geteuid() }
        || info.mode() & if private { 0o077 } else { 0o022 } != 0
    {
        return Err("unsafe_recovery_directory".into());
    }
    Ok(directory)
}

#[cfg(unix)]
fn project_directory(project: &Path) -> Result<File> {
    if !project.is_absolute()
        || project
            .components()
            .any(|c| matches!(c, Component::ParentDir))
        || project.canonicalize().map_err(|_| "unsafe_project")? != project
    {
        return Err("unsafe_project".into());
    }
    validate_directory(project, false)
}

#[cfg(unix)]
fn revalidate_project(project: &Path, original: &File) -> Result<()> {
    use std::os::unix::fs::MetadataExt;
    let current = project_directory(project)?.metadata()?;
    let original = original.metadata()?;
    if (current.dev(), current.ino()) != (original.dev(), original.ino()) {
        return Err("recovery_evidence_changed".into());
    }
    Ok(())
}

#[cfg(unix)]
fn secure_read(path: &Path, limit: u64, private: bool) -> Result<Option<(Vec<u8>, Value)>> {
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    let mut file = match OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
    {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err("unsafe_recovery_evidence".into()),
    };
    let before = file.metadata()?;
    if !before.is_file()
        || before.nlink() != 1
        || before.uid() != unsafe { libc::geteuid() }
        || before.mode() & if private { 0o077 } else { 0o022 } != 0
        || before.len() > limit
    {
        return Err("unsafe_recovery_evidence".into());
    }
    let mut bytes = Vec::new();
    (&mut file).take(limit + 1).read_to_end(&mut bytes)?;
    let after = file.metadata()?;
    let present = fs::symlink_metadata(path)?;
    if bytes.len() as u64 > limit
        || metadata(&before, true) != metadata(&after, true)
        || metadata(&after, true) != metadata(&present, true)
    {
        return Err("recovery_evidence_changed".into());
    }
    Ok(Some((
        bytes.clone(),
        json!({"identity":metadata(&after, false),"sha256":checksum(&bytes)}),
    )))
}

#[cfg(unix)]
fn write_exclusive(path: &Path, data: &[u8]) -> Result<()> {
    use std::os::unix::fs::OpenOptionsExt;
    if data.len() as u64 > MAX_FILE {
        return Err("recovery_evidence_too_large".into());
    }
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    file.write_all(data)?;
    file.sync_all()?;
    sync_directory(path.parent().ok_or("unsafe_recovery_evidence")?)
}

#[cfg(unix)]
fn child_name(path: &Path) -> Result<std::ffi::CString> {
    use std::os::unix::ffi::OsStrExt;
    std::ffi::CString::new(
        path.file_name()
            .ok_or("unsafe_recovery_evidence")?
            .as_bytes(),
    )
    .map_err(|_| "unsafe_recovery_evidence".into())
}

#[cfg(unix)]
fn write_project_evidence(directory: &File, path: &Path, bytes: &[u8]) -> Result<()> {
    use std::os::fd::{AsRawFd, FromRawFd};
    if bytes.len() as u64 > MAX_FILE {
        return Err("recovery_evidence_too_large".into());
    }
    let name = child_name(path)?;
    let descriptor = unsafe {
        libc::openat(
            directory.as_raw_fd(),
            name.as_ptr(),
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            0o600,
        )
    };
    if descriptor < 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let mut file = unsafe { File::from_raw_fd(descriptor) };
    file.write_all(bytes)?;
    file.sync_all()?;
    directory.sync_all()?;
    Ok(())
}

#[cfg(unix)]
fn retire_project_evidence(directory: &File, path: &Path) -> Result<()> {
    use std::os::fd::AsRawFd;
    let name = child_name(path)?;
    if unsafe { libc::unlinkat(directory.as_raw_fd(), name.as_ptr(), 0) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    directory.sync_all()?;
    Ok(())
}

fn residual(bytes: &[u8]) -> Result<Value> {
    let value = parse(bytes)?;
    let object = value.as_object().ok_or("invalid_residual")?;
    let names = ["schema", "owner", "attempt", "pid", "new_session", "reason"];
    let reasons = [
        "stage_failed_uncertainty",
        "stage_timeout_uncertainty",
        "stage_interrupted_uncertainty",
        "stage_cleanup_unknown_uncertainty",
        "stage_start_failed_uncertainty",
        "quiescence_unknown",
    ];
    if object.len() != names.len()
        || names.iter().any(|name| !object.contains_key(*name))
        || value["schema"].as_u64() != Some(1)
        || value["owner"] != "decision-browser-installer-stage-v1"
        || !value["attempt"].as_str().is_some_and(|s| hex(s, 32))
        || !(value["pid"].is_null() || value["pid"].as_u64().is_some_and(|n| n > 0))
        || value["new_session"] != true
        || !value["reason"]
            .as_str()
            .is_some_and(|r| reasons.contains(&r))
    {
        return Err("invalid_residual".into());
    }
    Ok(value)
}

fn report(status: &str, reason: &str, recovery: &str, changed: bool) -> Value {
    let next = match reason {
        "clear" => "The installation barrier is clear.",
        "recovered" => {
            "The verified barrier was cleared. Retained runtimes are available for inspection."
        }
        "reboot_witness_required" => {
            "Run recover-install --apply to record this barrier for comparison after a reboot."
        }
        "reboot_required" => {
            "Reboot this machine, then inspect install-status and run recover-install --apply again."
        }
        "recovery_ready" => {
            "Stop clients using this project, then run recover-install --apply to recheck and recover."
        }
        "active_setup" => {
            "Wait for the active installer to finish, then inspect install-status again."
        }
        "retained_resources_in_use" => {
            "Close clients using the retained runtime, then inspect install-status again."
        }
        _ => {
            "Preserve the installation barrier and retained evidence until the reported uncertainty is resolved."
        }
    };
    json!({"schema":1,"status":status,"reason":reason,"recovery":recovery,"changed":changed,"next_action":next})
}

trait Evidence {
    fn boot(&self) -> Result<Value>;
    fn snapshot(&self, project: &Path) -> Result<Value>;
    fn unused(&self, project: &Path) -> Result<()>;
    fn key(&self, project: &Path, create: bool) -> Result<Vec<u8>>;
}

struct SystemEvidence;
impl Evidence for SystemEvidence {
    fn boot(&self) -> Result<Value> {
        boot_identity()
    }
    fn snapshot(&self, project: &Path) -> Result<Value> {
        snapshot(project)
    }
    fn unused(&self, project: &Path) -> Result<()> {
        check_unused(project)
    }
    fn key(&self, project: &Path, create: bool) -> Result<Vec<u8>> {
        trusted_key(project, create)
    }
}

/// Return a fixed-code report. An uncertain inspection keeps every barrier.
pub(super) fn inspect(project: &Path, apply: bool) -> Value {
    let project = system_project_path(project);
    let mutation = Cell::new(false);
    match inspect_inner(&project, apply, &SystemEvidence, &mutation) {
        Ok(value) => value,
        Err(error) => {
            let reason = error.to_string();
            let known = [
                "unsafe_project",
                "unsafe_recovery_directory",
                "unsafe_recovery_evidence",
                "recovery_evidence_changed",
                "missing_lease",
                "additional_stage_lease",
                "legacy_install_lock",
                "active_setup",
                "stage_lifecycle_uncertain",
                "unrecognized_lease",
                "invalid_residual",
                "invalid_recovery_evidence",
                "missing_residual",
                "ambiguous_recovery_evidence",
                "missing_recovery_witness",
                "trusted_witness_unavailable",
                "trusted_witness_overlaps_project",
                "unsafe_trusted_witness",
                "boot_identity_unavailable",
                "platform_inspection_unavailable",
                "process_scope_unavailable",
                "filesystem_unverified",
                "runtime_referenced",
                "registration_unreadable",
                "unsafe_registration",
                "unsafe_retained_state",
                "retained_state_changed",
                "retained_state_too_large",
                "unrecognized_retained_state",
                "legacy_lock_present",
                "retained_resources_in_use",
                "inspection_incomplete",
                "inspection_unavailable",
            ];
            let mut value = report(
                "blocked",
                if known.contains(&reason.as_str()) {
                    &reason
                } else {
                    "recovery_inspection_failed"
                },
                "unavailable",
                false,
            );
            if mutation.get() {
                value["changed"] = Value::Null;
            }
            value
        }
    }
}

fn system_project_path(project: &Path) -> PathBuf {
    #[cfg(target_os = "macos")]
    for (alias, actual) in [
        ("/var", "/private/var"),
        ("/tmp", "/private/tmp"),
        ("/etc", "/private/etc"),
    ] {
        if let Ok(suffix) = project.strip_prefix(alias) {
            return Path::new(actual).join(suffix);
        }
    }
    project.to_path_buf()
}

pub(super) fn relevant(project: &Path) -> bool {
    [LEASE, RESIDUAL, ".decision-browser-stage.lease"]
        .iter()
        .any(|name| fs::symlink_metadata(project.join(name)).is_ok())
        || fs::symlink_metadata(project.join(STATE).join("owner.json")).is_ok()
}

#[cfg(not(unix))]
fn inspect_inner(_: &Path, _: bool, _: &impl Evidence, _: &Cell<bool>) -> Result<Value> {
    Err("platform_inspection_unavailable".into())
}

#[cfg(unix)]
fn inspect_inner(
    project: &Path,
    apply: bool,
    evidence: &impl Evidence,
    mutation: &Cell<bool>,
) -> Result<Value> {
    let directory = project_directory(project)?;
    if fs::symlink_metadata(project.join(".decision-browser-stage.lease")).is_ok() {
        return Err("additional_stage_lease".into());
    }
    for name in ["install.lock", "prepare.lock"] {
        if fs::symlink_metadata(project.join(STATE).join(name)).is_ok() {
            return Err("legacy_install_lock".into());
        }
    }
    let native_path = project.join(".decision-browser-install.native.lease");
    let _native_lease = if fs::symlink_metadata(&native_path).is_ok() {
        let file = open_lease(&native_path, false).map_err(|_| "unsafe_recovery_evidence")?;
        file.try_lock().map_err(|_| "active_setup")?;
        Some(file)
    } else {
        None
    };
    for path in [
        project.join(".decision-browser-stage.lease"),
        project.join(STATE).join("install.lock"),
        project.join(STATE).join("prepare.lock"),
    ] {
        if fs::symlink_metadata(&path).is_ok() {
            return Err("additional_stage_lease".into());
        }
    }
    let lease_path = project.join(LEASE);
    if fs::symlink_metadata(&lease_path).is_err() {
        if fs::symlink_metadata(project.join(RESIDUAL)).is_ok()
            || fs::symlink_metadata(project.join(STATE)).is_ok()
        {
            return Err("missing_lease".into());
        }
        return Ok(report("clear", "clear", "unavailable", false));
    }
    let mut lease_file = open_lease(&lease_path, false).map_err(|_| "unsafe_recovery_evidence")?;
    lease_file.try_lock().map_err(|_| "active_setup")?;
    let lease = secure_read(&lease_path, 128, true)?.ok_or("missing_lease")?;
    if metadata(&lease_file.metadata()?, false) != lease.1["identity"] {
        return Err("recovery_evidence_changed".into());
    }
    let current = secure_read(&project.join(RESIDUAL), MAX_FILE, true)?;
    if lease.0 == IDLE && current.is_none() {
        return Ok(report("clear", "clear", "unavailable", false));
    }
    if lease.0 == b"{\"active\":\"stage-lifecycle-v1\"}\n" {
        return Err("stage_lifecycle_uncertain".into());
    }
    if lease.0 != POISON {
        return Err("unrecognized_lease".into());
    }
    let (raw, identity, archived) = if let Some((bytes, identity)) = current {
        residual(&bytes)?;
        (bytes, identity, false)
    } else {
        let mut candidates = Vec::new();
        for entry in fs::read_dir(project)? {
            let entry = entry?;
            let name = entry.file_name();
            let name = name.to_str().ok_or("invalid_recovery_evidence")?;
            if let Some(attempt) = name
                .strip_prefix(".decision-browser-recovered.")
                .and_then(|s| s.strip_suffix(".json"))
                && hex(attempt, 32)
            {
                if candidates.len() >= 128 {
                    return Err("ambiguous_recovery_evidence".into());
                }
                let archive =
                    secure_read(&entry.path(), MAX_FILE, true)?.ok_or("missing_residual")?;
                let r = residual(&archive.0)?;
                let witness = secure_read(
                    &project.join(format!(".decision-browser-recovery.{attempt}.json")),
                    MAX_FILE,
                    true,
                )?;
                if let Some((bytes, _)) = witness {
                    let value = parse(&bytes)?;
                    if r["attempt"] == attempt
                        && value["lease"] == lease.1
                        && value["residual"]["sha256"] == archive.1["sha256"]
                    {
                        candidates.push(archive);
                    }
                }
            }
        }
        if candidates.len() != 1 {
            return Err("missing_residual".into());
        }
        let (bytes, identity) = candidates.remove(0);
        (bytes, identity, true)
    };
    let r = residual(&raw)?;
    let attempt = r["attempt"].as_str().ok_or("invalid_residual")?;
    let witness_path = project.join(format!(".decision-browser-recovery.{attempt}.json"));
    let archive_path = project.join(format!(".decision-browser-recovered.{attempt}.json"));
    let snapshot = evidence.snapshot(project)?;
    let boot = evidence.boot()?;
    if !["host", "boot"]
        .iter()
        .all(|key| boot[*key].as_str().is_some_and(|s| hex(s, 64)))
    {
        return Err("boot_identity_unavailable".into());
    }
    let witness_record = secure_read(&witness_path, MAX_FILE, true)?;
    if witness_record.is_none() {
        if archived {
            return Err("missing_recovery_witness".into());
        }
        evidence.unused(project)?;
        if !apply {
            return Ok(report(
                "blocked",
                "reboot_witness_required",
                "record_reboot_witness",
                false,
            ));
        }
        evidence.unused(project)?;
        verify_state(
            project,
            &directory,
            &lease_file,
            &lease,
            &raw,
            &identity,
            false,
        )?;
        if evidence.snapshot(project)? != snapshot || evidence.boot()? != boot {
            return Err("recovery_evidence_changed".into());
        }
        let mut witness = json!({"schema":1,"owner":"decision-browser-recovery-v1","attempt":attempt,"host":boot["host"],"boot":boot["boot"],"lease":lease.1,"residual":identity,"snapshot":snapshot});
        mutation.set(true);
        witness["mac"] = json!(signature(&witness, &evidence.key(project, true)?));
        verify_state(
            project,
            &directory,
            &lease_file,
            &lease,
            &raw,
            &identity,
            false,
        )?;
        write_project_evidence(&directory, &witness_path, &encoded(&witness, false))?;
        return Ok(report(
            "blocked",
            "reboot_required",
            "reboot_required",
            true,
        ));
    }
    let witness_record = witness_record.expect("checked witness");
    let witness = parse(&witness_record.0)?;
    check_witness(
        &witness,
        attempt,
        &lease.1,
        &identity,
        &snapshot,
        &boot,
        archived,
        &evidence.key(project, false)?,
    )?;
    if witness["boot"] == boot["boot"] {
        return Ok(report(
            "blocked",
            "reboot_required",
            "reboot_required",
            false,
        ));
    }
    evidence.unused(project)?;
    if !apply {
        return Ok(report("blocked", "recovery_ready", "ready", false));
    }
    evidence.unused(project)?;
    if evidence.snapshot(project)? != snapshot || evidence.boot()? != boot {
        return Err("recovery_evidence_changed".into());
    }
    verify_state(
        project,
        &directory,
        &lease_file,
        &lease,
        &raw,
        &identity,
        archived,
    )?;
    if secure_read(&witness_path, MAX_FILE, true)?.as_ref() != Some(&witness_record) {
        return Err("recovery_evidence_changed".into());
    }
    if let Some(existing) = secure_read(&archive_path, MAX_FILE, true)? {
        if existing.0 != raw {
            return Err("recovery_evidence_changed".into());
        }
    } else {
        if archived {
            return Err("missing_residual".into());
        }
        mutation.set(true);
        write_project_evidence(&directory, &archive_path, &raw)?;
    }
    verify_state(
        project,
        &directory,
        &lease_file,
        &lease,
        &raw,
        &identity,
        archived,
    )?;
    if !archived {
        mutation.set(true);
        retire_project_evidence(&directory, &project.join(RESIDUAL))?;
    }
    mutation.set(true);
    let commit = (|| -> Result<()> {
        lease_file.seek(SeekFrom::Start(0))?;
        lease_file.write_all(IDLE)?;
        lease_file.set_len(IDLE.len() as u64)?;
        lease_file.sync_all()?;
        if secure_read(&lease_path, 128, true)?
            .ok_or("missing_lease")?
            .0
            != IDLE
        {
            return Err("recovery_write_failed".into());
        }
        Ok(())
    })();
    if commit.is_err() {
        lease_file.seek(SeekFrom::Start(0))?;
        lease_file.write_all(POISON)?;
        lease_file.set_len(POISON.len() as u64)?;
        lease_file.sync_all()?;
        return commit.map(|_| Value::Null);
    }
    Ok(report("recovered", "recovered", "complete", true))
}

#[cfg(unix)]
fn verify_state(
    project: &Path,
    directory: &File,
    lease_file: &File,
    lease: &(Vec<u8>, Value),
    raw: &[u8],
    identity: &Value,
    archived: bool,
) -> Result<()> {
    revalidate_project(project, directory)?;
    if secure_read(&project.join(LEASE), 128, true)?.as_ref() != Some(lease)
        || metadata(&lease_file.metadata()?, false) != lease.1["identity"]
    {
        return Err("recovery_evidence_changed".into());
    }
    if !archived
        && secure_read(&project.join(RESIDUAL), MAX_FILE, true)?.as_ref()
            != Some(&(raw.to_vec(), identity.clone()))
    {
        return Err("recovery_evidence_changed".into());
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn check_witness(
    w: &Value,
    attempt: &str,
    lease: &Value,
    residual: &Value,
    snapshot: &Value,
    boot: &Value,
    archived: bool,
    key: &[u8],
) -> Result<()> {
    if key.len() != 32
        || w.as_object().is_none_or(|o| o.len() != 9)
        || w["schema"].as_u64() != Some(1)
        || w["owner"] != "decision-browser-recovery-v1"
        || w["attempt"] != attempt
        || w["lease"] != *lease
        || w["snapshot"] != *snapshot
        || w["host"] != boot["host"]
        || !w["boot"].as_str().is_some_and(|s| hex(s, 64))
        || w["residual"]["sha256"] != residual["sha256"]
        || !archived && w["residual"] != *residual
        || !w["mac"]
            .as_str()
            .is_some_and(|s| same_mac(s, &signature(w, key)))
    {
        return Err("recovery_evidence_changed".into());
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn capture_with_pid(program: &str, args: &[&str]) -> Result<(Vec<u8>, u32)> {
    use std::process::{Command, Stdio};
    use std::time::{Duration, Instant};
    let mut child = Command::new(program)
        .args(args)
        .env_clear()
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|_| "inspection_unavailable")?;
    let pid = child.id();
    let stdout = child.stdout.take().ok_or("inspection_unavailable")?;
    let stderr = child.stderr.take().ok_or("inspection_unavailable")?;
    let read = |stream: Box<dyn Read + Send>| {
        std::thread::spawn(move || {
            let mut bytes = Vec::new();
            stream
                .take(64 * 1024 * 1024 + 1)
                .read_to_end(&mut bytes)
                .map(|_| bytes)
        })
    };
    let output = read(Box::new(stdout));
    let errors = read(Box::new(stderr));
    let deadline = Instant::now() + Duration::from_secs(20);
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break status;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            let _ = output.join();
            let _ = errors.join();
            return Err("inspection_unavailable".into());
        }
        std::thread::sleep(Duration::from_millis(20));
    };
    let output = output.join().map_err(|_| "inspection_incomplete")??;
    let errors = errors.join().map_err(|_| "inspection_incomplete")??;
    if !status.success() || !errors.is_empty() || output.len() > 64 * 1024 * 1024 {
        return Err("inspection_incomplete".into());
    }
    Ok((output, pid))
}

#[cfg(target_os = "macos")]
fn capture(program: &str, args: &[&str]) -> Result<Vec<u8>> {
    Ok(capture_with_pid(program, args)?.0)
}

fn uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(index, c)| {
            if [8, 13, 18, 23].contains(&index) {
                c == b'-'
            } else {
                c.is_ascii_hexdigit()
            }
        })
}

fn boot_identity() -> Result<Value> {
    #[cfg(target_os = "macos")]
    let (host, boot) = {
        let raw = capture("/usr/sbin/sysctl", &["-n", "kern.bootsessionuuid"])?;
        let boot = std::str::from_utf8(&raw)
            .map_err(|_| "boot_identity_unavailable")?
            .trim()
            .to_owned();
        let platform = capture("/usr/sbin/ioreg", &["-rd1", "-c", "IOPlatformExpertDevice"])?;
        let text = std::str::from_utf8(&platform).map_err(|_| "boot_identity_unavailable")?;
        let host = text
            .lines()
            .find_map(|line| {
                let (_, rest) = line.split_once("\"IOPlatformUUID\"")?;
                let (_, value) = rest.split_once('=')?;
                Some(value.trim().trim_matches('"').to_owned())
            })
            .filter(|s| uuid(s))
            .ok_or("boot_identity_unavailable")?;
        (host, boot)
    };
    #[cfg(target_os = "linux")]
    let (host, boot) = {
        use std::os::unix::fs::MetadataExt;
        if fs::read_link("/proc/self/ns/pid")? != Path::new("pid:[4026531836]")
            || fs::read_link("/proc/self/ns/user")? != Path::new("user:[4026531837]")
            || fs::read_to_string("/proc/self/uid_map")?
                .split_whitespace()
                .collect::<Vec<_>>()
                != ["0", "0", "4294967295"]
        {
            return Err("process_scope_unavailable".into());
        }
        let info = fs::metadata("/etc/machine-id")?;
        if !info.is_file() || info.uid() != 0 || info.mode() & 0o022 != 0 {
            return Err("boot_identity_unavailable".into());
        }
        let host = fs::read_to_string("/etc/machine-id")?.trim().to_owned();
        if !hex(&host, 32) {
            return Err("boot_identity_unavailable".into());
        }
        (
            host,
            fs::read_to_string("/proc/sys/kernel/random/boot_id")?
                .trim()
                .to_owned(),
        )
    };
    #[cfg(any(target_os = "macos", target_os = "linux"))]
    {
        if !uuid(&boot) {
            return Err("boot_identity_unavailable".into());
        }
        Ok(
            json!({"host":checksum(host.to_lowercase().as_bytes()),"boot":checksum(boot.to_lowercase().as_bytes())}),
        )
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    Err("platform_inspection_unavailable".into())
}

#[cfg(unix)]
fn trusted_key(project: &Path, create: bool) -> Result<Vec<u8>> {
    use std::ffi::CStr;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    let mut entry = std::mem::MaybeUninit::<libc::passwd>::uninit();
    let mut result = std::ptr::null_mut();
    let mut buffer = vec![0u8; 65536];
    let status = unsafe {
        libc::getpwuid_r(
            libc::geteuid(),
            entry.as_mut_ptr(),
            buffer.as_mut_ptr().cast(),
            buffer.len(),
            &mut result,
        )
    };
    if status != 0 || result.is_null() {
        return Err("trusted_witness_unavailable".into());
    }
    let entry = unsafe { entry.assume_init() };
    if entry.pw_dir.is_null() {
        return Err("trusted_witness_unavailable".into());
    }
    let home = Path::new(std::ffi::OsStr::from_bytes(
        unsafe { CStr::from_ptr(entry.pw_dir) }.to_bytes(),
    ));
    let anchor = home.join(".local/state/decision-browser/recovery");
    if anchor.starts_with(project) || project.starts_with(&anchor) {
        return Err("trusted_witness_overlaps_project".into());
    }
    project_directory(home)?;
    let mut path = home.to_path_buf();
    for part in [".local", "state", "decision-browser", "recovery"] {
        path.push(part);
        if create {
            match fs::create_dir(&path) {
                Ok(()) => {
                    fs::set_permissions(&path, fs::Permissions::from_mode(0o700))?;
                    sync_directory(path.parent().ok_or("unsafe_trusted_witness")?)?;
                }
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
                Err(_) => return Err("trusted_witness_unavailable".into()),
            }
        }
        let info = fs::symlink_metadata(&path).map_err(|_| "trusted_witness_unavailable")?;
        if !info.is_dir()
            || info.uid() != unsafe { libc::geteuid() }
            || info.mode() & 0o022 != 0
            || part == "recovery" && info.mode() & 0o077 != 0
        {
            return Err("unsafe_trusted_witness".into());
        }
        validate_directory(&path, part == "recovery")?;
    }
    let key_path = path.join("key");
    let mut key = secure_read(&key_path, 32, true)?;
    if key.is_none() && create {
        let mut bytes = [0u8; 32];
        File::open("/dev/urandom")?.read_exact(&mut bytes)?;
        match write_exclusive(&key_path, &bytes) {
            Ok(()) => {}
            Err(_) if key_path.exists() => {}
            Err(error) => return Err(error),
        }
        key = secure_read(&key_path, 32, true)?;
    }
    key.map(|item| item.0)
        .filter(|bytes| bytes.len() == 32)
        .ok_or_else(|| "trusted_witness_unavailable".into())
}

#[cfg(not(unix))]
fn trusted_key(_: &Path, _: bool) -> Result<Vec<u8>> {
    Err("platform_inspection_unavailable".into())
}

#[cfg(unix)]
fn local_filesystem(project: &Path) -> Result<()> {
    let mut mounts = Vec::new();
    #[cfg(target_os = "macos")]
    {
        let raw = capture("/sbin/mount", &[])?;
        for line in std::str::from_utf8(&raw)
            .map_err(|_| "filesystem_unverified")?
            .lines()
        {
            let (source, rest) = line.split_once(" on ").ok_or("filesystem_unverified")?;
            let (target, flags) = rest.rsplit_once(" (").ok_or("filesystem_unverified")?;
            let flags = flags.strip_suffix(')').ok_or("filesystem_unverified")?;
            let target = PathBuf::from(target);
            if target.starts_with(project.join(STATE)) {
                return Err("filesystem_unverified".into());
            }
            if project.starts_with(&target) {
                let flags = flags.split(", ").collect::<Vec<_>>();
                mounts.push((
                    target.components().count(),
                    source.starts_with("/dev/disk")
                        && flags.contains(&"local")
                        && matches!(flags.first(), Some(&"apfs" | &"hfs")),
                ));
            }
        }
    }
    #[cfg(target_os = "linux")]
    {
        for line in fs::read_to_string("/proc/self/mountinfo")?.lines() {
            let (before, after) = line.split_once(" - ").ok_or("filesystem_unverified")?;
            let fields = before.split_whitespace().collect::<Vec<_>>();
            let suffix = after.split_whitespace().collect::<Vec<_>>();
            if fields.len() < 6 || suffix.len() < 3 {
                return Err("filesystem_unverified".into());
            }
            let target = PathBuf::from(
                fields[4]
                    .replace("\\040", " ")
                    .replace("\\011", "\t")
                    .replace("\\012", "\n")
                    .replace("\\134", "\\"),
            );
            if target.starts_with(project.join(STATE)) {
                return Err("filesystem_unverified".into());
            }
            if target == Path::new("/proc")
                && fields[5]
                    .split(',')
                    .chain(suffix[2].split(','))
                    .any(|s| s.starts_with("hidepid=") && s != "hidepid=0")
            {
                return Err("process_scope_unavailable".into());
            }
            if project.starts_with(&target) {
                mounts.push((
                    target.components().count(),
                    fields[3] == "/"
                        && ["ext2", "ext3", "ext4", "xfs", "btrfs", "tmpfs"].contains(&suffix[0]),
                ));
            }
        }
    }
    if mounts
        .into_iter()
        .max_by_key(|item| item.0)
        .is_none_or(|item| !item.1)
    {
        return Err("filesystem_unverified".into());
    }
    Ok(())
}

#[cfg(unix)]
fn references(project: &Path) -> Result<Value> {
    let mut rows = Vec::new();
    for relative in [
        ".codex/config.toml",
        ".mcp.json",
        ".decision-browser-install/manifest.json",
        ".decision-browser-install/native-manifest.json",
    ] {
        let path = super::checked(project, relative).map_err(|_| "unsafe_registration")?;
        let Some((bytes, _)) = secure_read(&path, 1024 * 1024, false)? else {
            // Preserve the historical reference digest for existing witnesses.
            if !relative.ends_with("native-manifest.json") {
                rows.push(json!([relative, Value::Null]));
            }
            continue;
        };
        if relative.ends_with("manifest.json") {
            return Err("runtime_referenced".into());
        }
        let value: Value = if relative.ends_with(".toml") {
            let text = std::str::from_utf8(&bytes).map_err(|_| "registration_unreadable")?;
            serde_json::to_value(
                toml::from_str::<toml::Value>(text).map_err(|_| "registration_unreadable")?,
            )?
        } else {
            parse(&bytes).map_err(|_| "registration_unreadable")?
        };
        if !value.is_object() {
            return Err("registration_unreadable".into());
        }
        let key = if relative.ends_with(".toml") {
            "mcp_servers"
        } else {
            "mcpServers"
        };
        if let Some(servers) = value.get(key) {
            let servers = servers.as_object().ok_or("registration_unreadable")?;
            for server in servers.values() {
                let allowed = [
                    "url",
                    "type",
                    "http_headers",
                    "env_http_headers",
                    "bearer_token_env_var",
                    "enabled",
                    "required",
                    "startup_timeout_sec",
                    "tool_timeout_sec",
                    "enabled_tools",
                    "disabled_tools",
                ];
                let valid = server
                    .as_object()
                    .is_some_and(|s| s.keys().all(|key| allowed.contains(&key.as_str())))
                    && matches!(
                        server.get("type").and_then(Value::as_str).unwrap_or("http"),
                        "http" | "sse"
                    )
                    && server["url"]
                        .as_str()
                        .and_then(|s| reqwest::Url::parse(s).ok())
                        .is_some_and(|url| {
                            matches!(url.scheme(), "http" | "https") && url.host_str().is_some()
                        });
                if !valid {
                    return Err("runtime_referenced".into());
                }
            }
        }
        let serialized = value.to_string();
        let mut decoded = serialized.clone();
        for _ in 0..3 {
            decoded = percent_decode(&decoded);
        }
        if decoded.contains(STATE) {
            return Err("runtime_referenced".into());
        }
        rows.push(json!([
            relative,
            metadata(&fs::symlink_metadata(path)?, true),
            checksum(&bytes)
        ]));
    }
    Ok(json!(rows))
}

fn percent_decode(text: &str) -> String {
    let mut bytes = Vec::new();
    let input = text.as_bytes();
    let mut index = 0;
    while index < input.len() {
        if input[index] == b'%'
            && index + 2 < input.len()
            && let Ok(hex) = std::str::from_utf8(&input[index + 1..index + 3])
            && let Ok(byte) = u8::from_str_radix(hex, 16)
        {
            bytes.push(byte);
            index += 3;
            continue;
        }
        bytes.push(input[index]);
        index += 1;
    }
    String::from_utf8_lossy(&bytes).into_owned()
}

#[cfg(unix)]
fn inventory(project: &Path, content: bool) -> Result<(Value, BTreeSet<(u64, u64)>)> {
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::MetadataExt;
    let root = project.join(STATE);
    let info = fs::symlink_metadata(&root).map_err(|_| "unsafe_retained_state")?;
    if !info.is_dir()
        || info.uid() != unsafe { libc::geteuid() }
        || info.mode() & 0o077 != 0
        || info.dev() != fs::metadata(project)?.dev()
    {
        return Err("unsafe_retained_state".into());
    }
    if secure_read(&root.join("owner.json"), 256, true)?
        .ok_or("unrecognized_retained_state")?
        .0
        != b"{\"owner\":\"decision-browser-installer-v1\"}\n"
    {
        return Err("unrecognized_retained_state".into());
    }
    let device = info.dev();
    let mut pending = vec![root.clone()];
    let mut rows = Vec::new();
    let mut identities = BTreeSet::new();
    let mut total = 0;
    while let Some(path) = pending.pop() {
        let info = fs::symlink_metadata(&path)?;
        if info.uid() != unsafe { libc::geteuid() } || info.dev() != device {
            return Err("unsafe_retained_state".into());
        }
        let name = path
            .file_name()
            .and_then(|s| s.to_str())
            .ok_or("unsafe_retained_state")?;
        if ["install.lock", "prepare.lock"].contains(&name) {
            return Err("legacy_lock_present".into());
        }
        let relative = path
            .strip_prefix(project)?
            .to_str()
            .ok_or("unsafe_retained_state")?;
        let meta = metadata(&info, true);
        if info.file_type().is_symlink() {
            let interpreter = name == "python"
                || name == "python3"
                || name
                    .strip_prefix("python3.")
                    .is_some_and(|v| !v.is_empty() && v.bytes().all(|c| c.is_ascii_digit()));
            if !interpreter
                || path.parent().and_then(Path::file_name) != Some(std::ffi::OsStr::new("bin"))
                || path
                    .parent()
                    .and_then(Path::parent)
                    .and_then(Path::file_name)
                    != Some(std::ffi::OsStr::new("venv"))
            {
                return Err("unsafe_retained_state".into());
            }
            let target = path.canonicalize().map_err(|_| "unsafe_retained_state")?;
            let info = fs::metadata(&target)?;
            if target.starts_with(&root)
                || !info.is_file()
                || ![0, unsafe { libc::geteuid() }].contains(&info.uid())
                || info.mode() & 0o022 != 0
            {
                return Err("unsafe_retained_state".into());
            }
            rows.push(json!([
                relative,
                meta,
                "symlink",
                checksum(fs::read_link(&path)?.as_os_str().as_bytes())
            ]));
        } else if info.is_dir() {
            if info.mode() & 0o022 != 0 {
                return Err("unsafe_retained_state".into());
            }
            identities.insert((info.dev(), info.ino()));
            rows.push(json!([relative, meta, "directory"]));
            let mut entries = fs::read_dir(&path)?
                .map(|entry| entry.map(|e| e.path()))
                .collect::<std::io::Result<Vec<_>>>()?;
            entries.sort();
            pending.extend(entries.into_iter().rev());
        } else if info.is_file() {
            if info.nlink() != 1 || info.mode() & 0o022 != 0 && name != ".lock" {
                return Err("unsafe_retained_state".into());
            }
            if info.len() > 512 * 1024 * 1024 {
                return Err("retained_state_too_large".into());
            }
            total += info.len();
            let digest = if content {
                Some(super::file_checksum(&path)?)
            } else {
                None
            };
            if metadata(&fs::symlink_metadata(&path)?, true) != meta {
                return Err("retained_state_changed".into());
            }
            rows.push(json!([relative, meta, digest]));
            identities.insert((info.dev(), info.ino()));
        } else {
            return Err("unsafe_retained_state".into());
        }
        if rows.len() > 100_000 || total > 2 * 1024 * 1024 * 1024u64 {
            return Err("retained_state_too_large".into());
        }
    }
    Ok((json!(rows), identities))
}

#[cfg(unix)]
fn snapshot(project: &Path) -> Result<Value> {
    project_directory(project)?;
    local_filesystem(project)?;
    snapshot_files(project)
}

#[cfg(unix)]
fn snapshot_files(project: &Path) -> Result<Value> {
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::MetadataExt;
    if fs::symlink_metadata(project.join(".decision-browser-stage.lease")).is_ok() {
        return Err("additional_stage_lease".into());
    }
    let refs = references(project)?;
    let (rows, _) = inventory(project, true)?;
    let info = fs::metadata(project)?;
    Ok(
        json!({"project":[checksum(project.as_os_str().as_bytes()),info.dev(),info.ino(),info.uid()],"references":checksum(&encoded(&refs,false)),"inventory":checksum(&encoded(&rows,false))}),
    )
}

#[cfg(not(unix))]
fn snapshot(_: &Path) -> Result<Value> {
    Err("platform_inspection_unavailable".into())
}

fn shell_arguments(command: &str) -> Result<Vec<String>> {
    if command.contains('\0') {
        return Ok(command.split('\0').map(str::to_owned).collect());
    }
    let mut arguments = Vec::new();
    let mut current = String::new();
    let mut quote = None;
    let mut escaped = false;
    for character in command.chars() {
        if escaped {
            current.push(character);
            escaped = false;
            continue;
        }
        if character == '\\' && quote != Some('\'') {
            escaped = true;
            continue;
        }
        if quote == Some(character) {
            quote = None;
            continue;
        }
        if quote.is_none() && matches!(character, '\'' | '"') {
            quote = Some(character);
            continue;
        }
        if quote.is_none() && character.is_whitespace() {
            if !current.is_empty() {
                arguments.push(std::mem::take(&mut current));
            }
        } else {
            current.push(character);
        }
    }
    if escaped || quote.is_some() {
        return Err("inspection_incomplete".into());
    }
    if !current.is_empty() {
        arguments.push(current);
    }
    Ok(arguments)
}

fn normalized(path: &Path) -> std::path::PathBuf {
    let mut result = std::path::PathBuf::new();
    for component in path.components() {
        match component {
            Component::ParentDir => {
                result.pop();
            }
            Component::CurDir => {}
            _ => result.push(component.as_os_str()),
        }
    }
    result
}

fn mentioned(command: &str, project: &Path, cwd: &Path) -> Result<bool> {
    let retained = project.join(STATE);
    if command.contains(retained.to_str().ok_or("inspection_incomplete")?) {
        return Ok(true);
    }
    if !command.contains(STATE) {
        return Ok(false);
    }
    if cwd.starts_with(project) {
        return Ok(true);
    }
    for argument in shell_arguments(command)? {
        if argument.contains(STATE) {
            let value = argument
                .split_once('=')
                .map_or(argument.as_str(), |(_, v)| v);
            if normalized(&cwd.join(value)).starts_with(&retained) {
                return Ok(true);
            }
        }
    }
    Ok(false)
}

#[cfg(target_os = "macos")]
fn processes() -> Result<BTreeSet<u32>> {
    let (output, inspector) = capture_with_pid("/bin/ps", &["-axo", "uid=,pid="])?;
    let text = std::str::from_utf8(&output).map_err(|_| "inspection_incomplete")?;
    let mut result = BTreeSet::new();
    for line in text.lines() {
        let fields = line.split_whitespace().collect::<Vec<_>>();
        if fields.len() != 2 {
            return Err("inspection_incomplete".into());
        }
        let uid = fields[0]
            .parse::<i64>()
            .map_err(|_| "inspection_incomplete")?;
        let pid = fields[1]
            .parse::<u32>()
            .map_err(|_| "inspection_incomplete")?;
        if uid == unsafe { libc::geteuid() } as i64 && pid != std::process::id() && pid != inspector
        {
            result.insert(pid);
        }
    }
    Ok(result)
}

#[cfg(target_os = "macos")]
fn darwin_unused(project: &Path, identities: &BTreeSet<(u64, u64)>) -> Result<()> {
    #[derive(Default)]
    struct Resource {
        pid: Option<u32>,
        device: Option<u64>,
        inode: Option<u64>,
        path: String,
        descriptor: String,
        kind: String,
    }
    fn check(
        resource: &Resource,
        project: &Path,
        identities: &BTreeSet<(u64, u64)>,
        working: &mut BTreeMap<u32, std::path::PathBuf>,
    ) -> Result<()> {
        if resource.pid == Some(std::process::id()) || resource.pid.is_none() {
            return Ok(());
        }
        if let (Some(device), Some(inode)) = (resource.device, resource.inode)
            && identities.contains(&(device, inode))
        {
            return Err("retained_resources_in_use".into());
        }
        if Path::new(&resource.path).starts_with(project.join(STATE)) {
            return Err("retained_resources_in_use".into());
        }
        if (["cwd", "txt"].contains(&resource.descriptor.as_str())
            || ["REG", "DIR"].contains(&resource.kind.as_str()))
            && (resource.device.is_none() || resource.inode.is_none())
        {
            return Err("inspection_incomplete".into());
        }
        if resource.descriptor == "cwd" {
            working.insert(
                resource.pid.expect("checked pid"),
                std::path::PathBuf::from(&resource.path),
            );
        }
        Ok(())
    }
    let expected = processes()?;
    let uid = unsafe { libc::geteuid() }.to_string();
    let bytes = capture("/usr/sbin/lsof", &["-nP", "-a", "-u", &uid, "-F0pufDtin"])?;
    let mut resource = Resource::default();
    let mut working = BTreeMap::new();
    let mut observed = BTreeSet::new();
    for field in bytes.split(|byte| *byte == 0) {
        let field = field.strip_prefix(b"\n").unwrap_or(field);
        if field.is_empty() {
            continue;
        }
        let value = std::str::from_utf8(&field[1..]).map_err(|_| "inspection_incomplete")?;
        if matches!(field[0], b'p' | b'f') {
            check(&resource, project, identities, &mut working)?;
            let pid = resource.pid;
            resource = Resource {
                pid,
                ..Resource::default()
            };
        }
        match field[0] {
            b'p' => {
                let pid = value.parse::<u32>().map_err(|_| "inspection_incomplete")?;
                resource.pid = Some(pid);
                observed.insert(pid);
            }
            b'f' => resource.descriptor = value.to_owned(),
            b't' => resource.kind = value.to_owned(),
            b'D' => {
                resource.device = Some(
                    u64::from_str_radix(value.trim_start_matches("0x"), 16)
                        .map_err(|_| "inspection_incomplete")?,
                )
            }
            b'i' => resource.inode = Some(value.parse().map_err(|_| "inspection_incomplete")?),
            b'n' => resource.path = value.to_owned(),
            _ => {}
        }
    }
    check(&resource, project, identities, &mut working)?;
    let alive = processes()?;
    if alive
        .iter()
        .any(|pid| !observed.contains(pid) || !working.contains_key(pid))
    {
        return Err("inspection_incomplete".into());
    }
    let inspected = expected.union(&alive).copied().collect::<Vec<_>>();
    for batch in inspected.chunks(100) {
        let ids = batch
            .iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>()
            .join(",");
        let bytes = capture("/bin/ps", &["-ww", "-p", &ids, "-o", "pid=,command="])?;
        let text = std::str::from_utf8(&bytes).map_err(|_| "inspection_incomplete")?;
        for line in text.lines() {
            let line = line.trim_start();
            let (pid, command) = line
                .split_once(char::is_whitespace)
                .ok_or("inspection_incomplete")?;
            let pid = pid.parse::<u32>().map_err(|_| "inspection_incomplete")?;
            if let Some(cwd) = working.get(&pid)
                && mentioned(command, project, cwd)?
            {
                return Err("retained_resources_in_use".into());
            }
        }
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn linux_processes() -> Result<BTreeMap<std::path::PathBuf, String>> {
    let mut result = BTreeMap::new();
    for entry in fs::read_dir("/proc")? {
        let path = entry?.path();
        let Some(pid) = path
            .file_name()
            .and_then(|s| s.to_str())
            .and_then(|s| s.parse::<u32>().ok())
        else {
            continue;
        };
        if pid == std::process::id() {
            continue;
        }
        let read = (|| -> Result<Option<String>> {
            let status = fs::read_to_string(path.join("status"))?;
            let fields = status
                .lines()
                .find_map(|s| s.strip_prefix("Uid:"))
                .ok_or("inspection_incomplete")?
                .split_whitespace()
                .map(str::parse::<u32>)
                .collect::<std::result::Result<Vec<_>, _>>()
                .map_err(|_| "inspection_incomplete")?;
            if fields.len() != 4 {
                return Err("inspection_incomplete".into());
            }
            if !fields.contains(&unsafe { libc::geteuid() }) {
                return Ok(None);
            }
            let stat = fs::read_to_string(path.join("stat"))?;
            let fields = stat
                .rsplit_once(')')
                .ok_or("inspection_incomplete")?
                .1
                .split_whitespace()
                .collect::<Vec<_>>();
            Ok(Some(
                fields.get(19).ok_or("inspection_incomplete")?.to_string(),
            ))
        })();
        match read {
            Ok(Some(start)) => {
                result.insert(path, start);
            }
            Ok(None) => {}
            Err(_) if !path.exists() => {}
            Err(_) => return Err("inspection_incomplete".into()),
        }
    }
    Ok(result)
}

#[cfg(target_os = "linux")]
fn linux_unused(project: &Path, identities: &BTreeSet<(u64, u64)>) -> Result<()> {
    use std::os::unix::fs::MetadataExt;
    let processes = linux_processes()?;
    for path in processes.keys() {
        let inspect = (|| -> Result<()> {
            let cwd = path.join("cwd").canonicalize()?;
            let command = fs::read(path.join("cmdline"))?;
            if mentioned(
                std::str::from_utf8(&command).map_err(|_| "inspection_incomplete")?,
                project,
                &cwd,
            )? {
                return Err("retained_resources_in_use".into());
            }
            let mut resources = vec![path.join("cwd"), path.join("exe")];
            resources.extend(
                fs::read_dir(path.join("fd"))?
                    .map(|entry| entry.map(|e| e.path()))
                    .collect::<std::io::Result<Vec<_>>>()?,
            );
            for resource in resources {
                match fs::metadata(resource) {
                    Ok(info) if identities.contains(&(info.dev(), info.ino())) => {
                        return Err("retained_resources_in_use".into());
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                    Err(_) => return Err("inspection_incomplete".into()),
                    _ => {}
                }
            }
            for line in fs::read_to_string(path.join("maps"))?.lines() {
                let fields = line.split_whitespace().collect::<Vec<_>>();
                if fields.len() < 5 {
                    return Err("inspection_incomplete".into());
                }
                let (major, minor) = fields[3].split_once(':').ok_or("inspection_incomplete")?;
                let major = u64::from_str_radix(major, 16).map_err(|_| "inspection_incomplete")?;
                let minor = u64::from_str_radix(minor, 16).map_err(|_| "inspection_incomplete")?;
                let device = libc::makedev(major as u32, minor as u32);
                if identities.contains(&(
                    device,
                    fields[4]
                        .parse::<u64>()
                        .map_err(|_| "inspection_incomplete")?,
                )) {
                    return Err("retained_resources_in_use".into());
                }
            }
            Ok(())
        })();
        if let Err(error) = inspect
            && path.exists()
        {
            return Err(error);
        }
    }
    if linux_processes()?
        .iter()
        .any(|(pid, start)| processes.get(pid) != Some(start))
    {
        return Err("inspection_incomplete".into());
    }
    Ok(())
}

fn check_unused(project: &Path) -> Result<()> {
    #[cfg(unix)]
    let (_, identities) = inventory(project, false)?;
    #[cfg(target_os = "macos")]
    return darwin_unused(project, &identities);
    #[cfg(target_os = "linux")]
    return linux_unused(project, &identities);
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        let _ = project;
        Err("platform_inspection_unavailable".into())
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::cell::{Cell, RefCell};
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    fn inspect_with(project: &Path, apply: bool, evidence: &impl Evidence) -> Result<Value> {
        inspect_inner(project, apply, evidence, &Cell::new(false))
    }

    struct Project(PathBuf);
    impl Project {
        fn new() -> Self {
            let path = std::env::temp_dir().canonicalize().unwrap().join(format!(
                "native-recovery-{}",
                super::super::nonce().unwrap()
            ));
            fs::create_dir(&path).unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
            Self(path)
        }
        fn blocked(&self) {
            self.write(LEASE, POISON);
            self.write(RESIDUAL,&encoded(&json!({"schema":1,"owner":"decision-browser-installer-stage-v1","attempt":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","pid":987654321,"new_session":true,"reason":"stage_failed_uncertainty"}),false));
            self.write(
                ".decision-browser-install/owner.json",
                b"{\"owner\":\"decision-browser-installer-v1\"}\n",
            );
            self.write(
                ".decision-browser-install/runtimes/attempt/retained",
                b"retained bytes",
            );
            self.write(".codex/config.toml", b"model = 'private-canary'\n");
            self.write(".mcp.json", b"{\"mcpServers\":{},\"custom\":\"kept\"}");
        }
        fn write(&self, relative: &str, bytes: &[u8]) {
            let path = self.0.join(relative);
            super::super::write_atomic(&path, Some(bytes)).unwrap();
        }
        fn witness(&self) -> PathBuf {
            self.0
                .join(".decision-browser-recovery.bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.json")
        }
        fn archive(&self) -> PathBuf {
            self.0
                .join(".decision-browser-recovered.bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.json")
        }
    }
    impl Drop for Project {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    struct FakeEvidence {
        boot: Cell<u8>,
        key: RefCell<Option<Vec<u8>>>,
        in_use: Cell<bool>,
        changed: Cell<bool>,
        scans: Cell<usize>,
    }
    impl FakeEvidence {
        fn new() -> Self {
            Self {
                boot: Cell::new(1),
                key: RefCell::new(None),
                in_use: Cell::new(false),
                changed: Cell::new(false),
                scans: Cell::new(0),
            }
        }
    }
    impl Evidence for FakeEvidence {
        fn boot(&self) -> Result<Value> {
            Ok(
                json!({"host":"a".repeat(64),"boot":if self.boot.get()==1 {"b".repeat(64)}else{"c".repeat(64)}}),
            )
        }
        fn snapshot(&self, project: &Path) -> Result<Value> {
            let mut value = snapshot_files(project)?;
            let scans = self.scans.get() + 1;
            self.scans.set(scans);
            if self.changed.get() && scans.is_multiple_of(2) {
                value["inventory"] = json!("changed");
            }
            Ok(value)
        }
        fn unused(&self, _: &Path) -> Result<()> {
            if self.in_use.get() {
                Err("retained_resources_in_use".into())
            } else {
                Ok(())
            }
        }
        fn key(&self, _: &Path, create: bool) -> Result<Vec<u8>> {
            if self.key.borrow().is_none() && create {
                *self.key.borrow_mut() = Some(vec![0x12; 32]);
            }
            self.key
                .borrow()
                .clone()
                .ok_or_else(|| "trusted_witness_unavailable".into())
        }
    }
    fn inspect_test(project: &Project, apply: bool, evidence: &FakeEvidence) -> Value {
        inspect_with(&project.0, apply, evidence).unwrap()
    }

    #[test]
    fn empty_preview_is_read_only() {
        let project = Project::new();
        let evidence = FakeEvidence::new();
        assert_eq!(inspect_test(&project, false, &evidence)["status"], "clear");
        assert_eq!(fs::read_dir(&project.0).unwrap().count(), 0);
        assert!(evidence.key.borrow().is_none());
    }
    #[test]
    fn witness_needs_a_later_boot_and_preserves_lease_inode() {
        let project = Project::new();
        project.blocked();
        let evidence = FakeEvidence::new();
        let lease = project.0.join(LEASE);
        let inode = fs::metadata(&lease).unwrap().ino();
        assert_eq!(
            inspect_test(&project, false, &evidence)["reason"],
            "reboot_witness_required"
        );
        assert!(!project.witness().exists());
        assert_eq!(
            inspect_test(&project, true, &evidence)["reason"],
            "reboot_required"
        );
        assert_eq!(inspect_test(&project, true, &evidence)["changed"], false);
        evidence.boot.set(2);
        assert_eq!(
            inspect_test(&project, false, &evidence)["reason"],
            "recovery_ready"
        );
        assert_eq!(
            inspect_test(&project, true, &evidence)["status"],
            "recovered"
        );
        assert_eq!(fs::metadata(&lease).unwrap().ino(), inode);
        assert_eq!(fs::read(lease).unwrap(), IDLE);
        assert!(!project.0.join(RESIDUAL).exists());
        assert!(project.archive().exists());
        assert_eq!(
            fs::read(
                project
                    .0
                    .join(".decision-browser-install/runtimes/attempt/retained")
            )
            .unwrap(),
            b"retained bytes"
        );
    }
    #[test]
    fn changed_inventory_or_registration_rejects_later_boot() {
        for relative in [
            ".decision-browser-install/runtimes/attempt/retained",
            ".codex/config.toml",
        ] {
            let project = Project::new();
            project.blocked();
            let evidence = FakeEvidence::new();
            inspect_test(&project, true, &evidence);
            evidence.boot.set(2);
            project.write(relative, b"changed after witness");
            assert!(inspect_with(&project.0, true, &evidence).is_err());
            assert_eq!(fs::read(project.0.join(LEASE)).unwrap(), POISON);
        }
    }
    #[test]
    fn forged_boot_and_missing_key_cannot_authorize_recovery() {
        let project = Project::new();
        project.blocked();
        let evidence = FakeEvidence::new();
        inspect_test(&project, true, &evidence);
        let mut witness = parse(&fs::read(project.witness()).unwrap()).unwrap();
        witness["boot"] = json!("d".repeat(64));
        project.write(
            project.witness().file_name().unwrap().to_str().unwrap(),
            &encoded(&witness, false),
        );
        assert!(inspect_with(&project.0, true, &evidence).is_err());
        *evidence.key.borrow_mut() = None;
        assert!(inspect_with(&project.0, true, &evidence).is_err());
        assert!(evidence.key.borrow().is_none());
        assert_eq!(fs::read(project.0.join(LEASE)).unwrap(), POISON);
    }
    #[test]
    fn archived_residual_can_resume_after_interrupted_retirement() {
        let project = Project::new();
        project.blocked();
        let evidence = FakeEvidence::new();
        inspect_test(&project, true, &evidence);
        evidence.boot.set(2);
        let raw = fs::read(project.0.join(RESIDUAL)).unwrap();
        write_exclusive(&project.archive(), &raw).unwrap();
        fs::remove_file(project.0.join(RESIDUAL)).unwrap();
        assert_eq!(
            inspect_test(&project, true, &evidence)["status"],
            "recovered"
        );
    }
    #[test]
    fn active_or_aliased_leases_remain_blocked() {
        let project = Project::new();
        project.blocked();
        let evidence = FakeEvidence::new();
        let lease = open_lease(&project.0.join(LEASE), false).unwrap();
        lease.try_lock().unwrap();
        assert_eq!(
            inspect_with(&project.0, true, &evidence)
                .unwrap_err()
                .to_string(),
            "active_setup"
        );
        lease.unlock().unwrap();
        fs::hard_link(project.0.join(LEASE), project.0.join("other")).unwrap();
        assert!(inspect_with(&project.0, true, &evidence).is_err());
        assert_eq!(fs::read(project.0.join("other")).unwrap(), POISON);
    }
    #[test]
    fn fifo_and_symlink_evidence_are_rejected_without_reading() {
        use std::os::unix::ffi::OsStrExt;
        for fifo in [false, true] {
            let project = Project::new();
            project.blocked();
            let evidence = FakeEvidence::new();
            let path = project.0.join(RESIDUAL);
            fs::remove_file(&path).unwrap();
            if fifo {
                let path = std::ffi::CString::new(path.as_os_str().as_bytes()).unwrap();
                assert_eq!(unsafe { libc::mkfifo(path.as_ptr(), 0o600) }, 0);
            } else {
                std::os::unix::fs::symlink(project.0.join(LEASE), path).unwrap();
            }
            assert!(inspect_with(&project.0, true, &evidence).is_err());
            assert_eq!(fs::read(project.0.join(LEASE)).unwrap(), POISON);
        }
    }
    #[test]
    fn active_resources_and_concurrent_change_prevent_witness() {
        let project = Project::new();
        project.blocked();
        let evidence = FakeEvidence::new();
        evidence.in_use.set(true);
        assert!(inspect_with(&project.0, true, &evidence).is_err());
        assert!(!project.witness().exists());
        evidence.in_use.set(false);
        evidence.scans.set(0);
        evidence.changed.set(true);
        assert!(inspect_with(&project.0, true, &evidence).is_err());
        assert!(!project.witness().exists());
    }
    #[test]
    fn command_registration_and_extra_stage_state_prevent_recovery() {
        for (path, bytes) in [
            (
                ".mcp.json",
                b"{\"mcpServers\":{\"other\":{\"command\":\"wrapper\"}}}".as_slice(),
            ),
            (".decision-browser-stage.lease", b"{}".as_slice()),
            (
                ".decision-browser-install/install.lock",
                b"unknown".as_slice(),
            ),
        ] {
            let project = Project::new();
            project.blocked();
            let evidence = FakeEvidence::new();
            project.write(path, bytes);
            assert!(inspect_with(&project.0, true, &evidence).is_err());
            assert!(!project.witness().exists());
        }
    }
    #[test]
    fn registration_snapshots_never_include_contents() {
        let project = Project::new();
        project.blocked();
        let first = snapshot_files(&project.0).unwrap();
        assert_eq!(first, snapshot_files(&project.0).unwrap());
        assert!(!first.to_string().contains("private-canary"));
        project.write(".codex/config.toml", b"model='changed'");
        assert_ne!(first, snapshot_files(&project.0).unwrap());
    }
    #[test]
    fn retained_aliases_and_wrong_owner_are_refused() {
        let project = Project::new();
        project.blocked();
        fs::hard_link(
            project.0.join(".decision-browser-install/owner.json"),
            project.0.join(".decision-browser-install/alias"),
        )
        .unwrap();
        assert!(inventory(&project.0, true).is_err());
    }
    #[test]
    fn json_and_authentication_match_legacy_canonical_encoding() {
        assert!(parse(br#"{"schema":1,"schema":2}"#).is_err());
        assert_eq!(
            encoded(&json!({"z":[1,"é"],"a":true}), true),
            br#"{"a":true,"z":[1,"\u00e9"]}"#
        );
        assert_eq!(
            encoded(&json!({"z":[1,"é"],"a":true}), false),
            br#"{"a": true, "z": [1, "\u00e9"]}"#
        );
        let witness = json!({"attempt":"b".repeat(32),"schema":1});
        let first = signature(&witness, &[1; 32]);
        assert_eq!(first.len(), 64);
        let mut with_mac = witness.clone();
        with_mac["mac"] = json!("ignored");
        assert_eq!(signature(&with_mac, &[1; 32]), first);
        assert!(!same_mac(&first, &signature(&witness, &[2; 32])));
    }
    #[test]
    fn process_reference_resolution_includes_relative_paths() {
        let project = Path::new("/local/project");
        assert!(
            mentioned(
                "server .decision-browser-install/runtimes/server",
                project,
                project
            )
            .unwrap()
        );
        assert!(
            mentioned(
                "server ../project/.decision-browser-install/server",
                project,
                Path::new("/local/other")
            )
            .unwrap()
        );
        assert!(
            !mentioned(
                "server .decision-browser-install/server",
                project,
                Path::new("/local/other")
            )
            .unwrap()
        );
        assert!(
            mentioned(
                "server '/local/project/.decision-browser-install/server'",
                project,
                Path::new("/")
            )
            .unwrap()
        );
    }
    #[test]
    fn project_alias_and_host_changes_preserve_barrier() {
        let project = Project::new();
        project.blocked();
        let evidence = FakeEvidence::new();
        let alias = Project::new();
        let path = alias.0.join("alias");
        std::os::unix::fs::symlink(&project.0, &path).unwrap();
        assert!(inspect_with(&path, true, &evidence).is_err());
        inspect_test(&project, true, &evidence);
        let mut witness = parse(&fs::read(project.witness()).unwrap()).unwrap();
        witness["host"] = json!("f".repeat(64));
        witness["mac"] = json!(signature(&witness, &[0x12; 32]));
        project.write(
            project.witness().file_name().unwrap().to_str().unwrap(),
            &encoded(&witness, false),
        );
        assert!(inspect_with(&project.0, true, &evidence).is_err());
        assert_eq!(fs::read(project.0.join(LEASE)).unwrap(), POISON);
    }

    #[test]
    fn evidence_writes_and_retirement_stay_on_the_opened_project() {
        let project = Project::new();
        project.write(RESIDUAL, b"original");
        let directory = project_directory(&project.0).unwrap();
        let moved = project.0.with_extension("moved");
        fs::rename(&project.0, &moved).unwrap();
        fs::create_dir(&project.0).unwrap();
        project.write(RESIDUAL, b"replacement");
        assert!(revalidate_project(&project.0, &directory).is_err());
        write_project_evidence(&directory, &project.0.join("new-evidence"), b"anchored").unwrap();
        retire_project_evidence(&directory, &project.0.join(RESIDUAL)).unwrap();
        assert_eq!(fs::read(project.0.join(RESIDUAL)).unwrap(), b"replacement");
        assert!(!project.0.join("new-evidence").exists());
        assert_eq!(fs::read(moved.join("new-evidence")).unwrap(), b"anchored");
        assert!(!moved.join(RESIDUAL).exists());
        fs::remove_dir_all(moved).unwrap();
    }

    #[test]
    fn idle_primary_lease_does_not_override_an_additional_barrier() {
        for name in [
            ".decision-browser-stage.lease",
            ".decision-browser-install/install.lock",
            ".decision-browser-install/prepare.lock",
        ] {
            let project = Project::new();
            project.write(LEASE, IDLE);
            project.write(name, b"retained lock");
            let evidence = FakeEvidence::new();
            assert!(inspect_with(&project.0, true, &evidence).is_err());
            assert_eq!(fs::read(project.0.join(LEASE)).unwrap(), IDLE);
            assert_eq!(fs::read(project.0.join(name)).unwrap(), b"retained lock");
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn system_temporary_directory_alias_is_accepted_for_preview() {
        let project = Project::new();
        let alias = if let Ok(suffix) = project.0.strip_prefix("/private/var") {
            Path::new("/var").join(suffix)
        } else if let Ok(suffix) = project.0.strip_prefix("/private/tmp") {
            Path::new("/tmp").join(suffix)
        } else {
            return;
        };
        assert_eq!(inspect(&alias, false)["status"], "clear");
        assert_eq!(fs::read_dir(&project.0).unwrap().count(), 0);
    }
}
