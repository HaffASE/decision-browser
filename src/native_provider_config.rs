use super::{Provider, ProviderResult, failure};
use serde::de::{self, MapAccess, Visitor};
use serde::{Deserialize, Deserializer};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::fmt;
use std::fs::{self, File};
use std::io::Read;
use std::path::{Component, Path, PathBuf};

type Environment = BTreeMap<String, String>;

pub(super) struct Settings {
    pub provider: Provider,
    pub key: Option<String>,
    pub endpoint: String,
    pub model: String,
    pub max_choices: usize,
    pub text: Option<TextSettings>,
}

pub(super) struct TextSettings {
    pub endpoint: String,
    pub model: String,
    pub key: Option<String>,
}

pub(super) fn environment() -> ProviderResult<Environment> {
    std::env::vars_os()
        .filter(|(name, _)| {
            name.to_string_lossy().starts_with("DECISION_BROWSER_")
                || [
                    "OPENAI_API_KEY",
                    "TYPESAFE_API_KEY",
                    "OPENCODE_API_KEY",
                    "OPENROUTER_API_KEY",
                    "HOME",
                ]
                .contains(&name.to_string_lossy().as_ref())
        })
        .map(|(name, value)| {
            Ok((
                name.into_string()
                    .map_err(|_| failure("invalid_provider_config"))?,
                value
                    .into_string()
                    .map_err(|_| failure("invalid_provider_config"))?,
            ))
        })
        .collect()
}

pub(super) fn resolve(env: &Environment) -> ProviderResult<Settings> {
    let profile = if env.contains_key("DECISION_BROWSER_PROVIDER")
        || env.contains_key("DECISION_BROWSER_ENDPOINT")
    {
        None
    } else {
        load_profile(env)?
    };
    settings(env, profile.as_ref())
}

pub(super) fn preset_endpoint(provider: Provider, endpoint: &str) -> bool {
    let configured = reqwest::Url::parse(endpoint).ok();
    let preset = provider
        .route()
        .ok()
        .and_then(|route| reqwest::Url::parse(route.0).ok());
    configured
        .zip(preset)
        .is_some_and(|(configured, preset)| configured.origin() == preset.origin())
}

pub(super) fn settings_for(provider: Provider) -> ProviderResult<Settings> {
    let env = environment()?;
    let resolved = resolve(&env)?;
    if resolved.provider.name() == provider.name() {
        return Ok(resolved);
    }
    let mut selected = Environment::new();
    selected.insert(
        "DECISION_BROWSER_PROVIDER".to_owned(),
        provider.name().to_owned(),
    );
    if let Some(key) = env.get(provider_key_name(provider)) {
        selected.insert(provider_key_name(provider).to_owned(), key.clone());
    }
    settings(&selected, None)
}

pub(super) fn settings(
    env: &Environment,
    profile: Option<&Environment>,
) -> ProviderResult<Settings> {
    let mut effective = profile.cloned().unwrap_or_default();
    effective.extend(env.clone());
    let provider = Provider::from_name(
        effective
            .get("DECISION_BROWSER_PROVIDER")
            .map_or("openai", String::as_str),
    )?;
    if matches!(provider, Provider::Fixture) {
        return Err(failure("invalid_provider_config"));
    }
    let endpoint = effective
        .get("DECISION_BROWSER_ENDPOINT")
        .map_or(provider.route()?.0, String::as_str);
    validate_endpoint(endpoint, matches!(provider, Provider::Local))?;
    if !matches!(provider, Provider::Local)
        && reqwest::Url::parse(endpoint)
            .map_err(|_| failure("invalid_provider_config"))?
            .scheme()
            != "https"
    {
        return Err(failure("invalid_provider_config"));
    }
    let model = effective
        .get("DECISION_BROWSER_MODEL")
        .map_or(provider.model(), String::as_str);
    validate_model(model)?;
    let max_choices = effective
        .get("DECISION_BROWSER_MAX_CHOICES")
        .map(|value| {
            if !value.bytes().all(|ch| ch.is_ascii_digit()) {
                return Err(failure("invalid_provider_config"));
            }
            value
                .parse::<usize>()
                .map_err(|_| failure("invalid_provider_config"))
        })
        .transpose()?
        .unwrap_or(provider.max_choices());
    if !(2..=255).contains(&max_choices)
        || matches!(provider, Provider::Luna)
            && (endpoint != provider.route()?.0
                || model != provider.model()
                || max_choices != provider.max_choices())
    {
        return Err(failure("invalid_provider_config"));
    }
    let preset_origin = reqwest::Url::parse(provider.route()?.0)
        .map_err(|_| failure("invalid_provider_config"))?
        .origin();
    let named_key = !env.contains_key("DECISION_BROWSER_API_KEY")
        && (env.contains_key(provider_key_name(provider))
            || !effective.contains_key("DECISION_BROWSER_API_KEY")
                && effective.contains_key(provider_key_name(provider)));
    if named_key
        && reqwest::Url::parse(endpoint)
            .map_err(|_| failure("invalid_provider_config"))?
            .origin()
            != preset_origin
    {
        return Err(failure("credential_route_mismatch"));
    }
    let key = if env.contains_key("DECISION_BROWSER_API_KEY") {
        direct_key(provider, env)?
    } else if env.contains_key(provider_key_name(provider)) {
        validate_key(env.get(provider_key_name(provider)).map(String::as_str))?
    } else {
        direct_key(provider, &effective)?
    };
    let text_names = [
        "DECISION_BROWSER_TEXT_ENDPOINT",
        "DECISION_BROWSER_TEXT_MODEL",
        "DECISION_BROWSER_TEXT_API_KEY",
    ];
    let text = if text_names.iter().any(|name| effective.contains_key(*name)) {
        if matches!(provider, Provider::Luna) {
            return Err(failure("invalid_provider_config"));
        }
        let endpoint = effective
            .get(text_names[0])
            .ok_or_else(|| failure("invalid_provider_config"))?;
        let model = effective
            .get(text_names[1])
            .ok_or_else(|| failure("invalid_provider_config"))?;
        validate_endpoint(endpoint, false)?;
        validate_model(model)?;
        let key = validate_key(effective.get(text_names[2]).map(String::as_str))?;
        if key.is_none()
            && !loopback(
                &reqwest::Url::parse(endpoint).map_err(|_| failure("invalid_provider_config"))?,
            )
        {
            return Err(failure("invalid_provider_config"));
        }
        Some(TextSettings {
            endpoint: endpoint.clone(),
            model: model.clone(),
            key,
        })
    } else {
        None
    };
    Ok(Settings {
        provider,
        key,
        endpoint: endpoint.to_owned(),
        model: model.to_owned(),
        max_choices,
        text,
    })
}

fn loopback(url: &reqwest::Url) -> bool {
    url.host_str().is_some_and(|host| {
        host.eq_ignore_ascii_case("localhost")
            || host
                .trim_matches(['[', ']'])
                .parse::<std::net::IpAddr>()
                .is_ok_and(|ip| ip.is_loopback())
    })
}

fn validate_endpoint(value: &str, local_only: bool) -> ProviderResult<()> {
    let url = reqwest::Url::parse(value).map_err(|_| failure("invalid_provider_config"))?;
    let authority = value
        .split_once("://")
        .map(|(_, tail)| tail.split('/').next().unwrap_or(""))
        .unwrap_or("");
    if value.len() > 2048
        || value.contains(['?', '#', '\\'])
        || value
            .chars()
            .any(|ch| ch.is_whitespace() || ch.is_control())
        || authority.is_empty()
        || authority.contains('@')
        || authority.ends_with(':')
        || !matches!(url.scheme(), "http" | "https")
        || url.host().is_none()
        || url.path() == "/"
        || url.port() == Some(0)
        || !url.username().is_empty()
        || url.password().is_some()
        || ((local_only || url.scheme() == "http") && !loopback(&url))
    {
        return Err(failure("invalid_provider_config"));
    }
    Ok(())
}

fn validate_model(value: &str) -> ProviderResult<()> {
    if value.is_empty()
        || value.len() > 256
        || value
            .chars()
            .any(|ch| ch.is_whitespace() || ch.is_control())
    {
        return Err(failure("invalid_provider_config"));
    }
    Ok(())
}

fn provider_key_name(provider: Provider) -> &'static str {
    match provider {
        Provider::Jev => "TYPESAFE_API_KEY",
        Provider::Luna => "OPENAI_API_KEY",
        Provider::Gemini | Provider::OpenCode => "OPENCODE_API_KEY",
        Provider::OpenRouter => "OPENROUTER_API_KEY",
        Provider::Local => "DECISION_BROWSER_API_KEY",
        Provider::Fixture => "DECISION_BROWSER_API_KEY",
    }
}

fn direct_key(provider: Provider, env: &Environment) -> ProviderResult<Option<String>> {
    validate_key(
        env.get("DECISION_BROWSER_API_KEY")
            .or_else(|| env.get(provider_key_name(provider)))
            .map(String::as_str),
    )
}

fn validate_key(key: Option<&str>) -> ProviderResult<Option<String>> {
    let Some(key) = key.filter(|key| !key.is_empty()) else {
        return Ok(None);
    };
    if key.len() > 8192 || !key.bytes().all(|byte| byte.is_ascii_graphic()) {
        return Err(failure("invalid_provider_config"));
    }
    Ok(Some(key.to_owned()))
}

pub(super) fn status(
    result: ProviderResult<Settings>,
    keychain_present: impl FnOnce(Provider) -> bool,
) -> Value {
    match result {
        Ok(settings) => {
            let present = settings.key.is_some()
                || (!matches!(settings.provider, Provider::Local)
                    && preset_endpoint(settings.provider, &settings.endpoint)
                    && keychain_present(settings.provider));
            let ready = present || matches!(settings.provider, Provider::Local);
            json!({
                "provider": match settings.provider { Provider::Jev => "typesafe", Provider::Luna => "openai", Provider::Gemini => "gemini", Provider::OpenRouter => "openrouter", Provider::OpenCode => "opencode", Provider::Local => "local", Provider::Fixture => "fixture" },
                "api_key_present": present, "provider_ready": ready,
                "provider_error": if ready { Value::Null } else { json!("missing_api_key") },
                "text_generation": if matches!(settings.provider, Provider::Luna) { "native" } else if settings.text.is_some() { "configured" } else { "unconfigured" }, "visual": "unsupported"
            })
        }
        Err(error) => {
            json!({"provider":"invalid", "api_key_present":false, "provider_ready":false, "provider_error":error.to_string(), "text_generation":"unconfigured", "visual":"unsupported"})
        }
    }
}

fn profile_path(env: &Environment) -> ProviderResult<PathBuf> {
    let path = match env.get("DECISION_BROWSER_CONFIG_FILE") {
        Some(path) => PathBuf::from(path),
        None => PathBuf::from(
            env.get("HOME")
                .ok_or_else(|| failure("invalid_profile_path"))?,
        )
        .join(".config/decision-browser/provider.json"),
    };
    if !path.is_absolute()
        || path
            .components()
            .any(|part| matches!(part, Component::ParentDir))
        || path.as_os_str().is_empty()
    {
        return Err(failure("invalid_profile_path"));
    }
    Ok(path)
}

fn check_parents(path: &Path) -> ProviderResult<()> {
    for parent in path.ancestors().skip(1) {
        if fs::symlink_metadata(parent)
            .map_err(|_| failure("profile_unreadable"))?
            .file_type()
            .is_symlink()
        {
            return Err(failure("unsafe_profile_path"));
        }
    }
    Ok(())
}

fn private(metadata: &fs::Metadata, directory: bool) -> ProviderResult<()> {
    if (directory && !metadata.is_dir()) || (!directory && !metadata.is_file()) {
        return Err(failure("unsafe_profile_path"));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        unsafe extern "C" {
            fn getuid() -> u32;
        }
        if metadata.uid() != unsafe { getuid() } || metadata.mode() & 0o077 != 0 {
            return Err(failure("unsafe_profile_permissions"));
        }
        if !directory && metadata.nlink() != 1 {
            return Err(failure("unsafe_profile_path"));
        }
    }
    Ok(())
}

fn load_profile(env: &Environment) -> ProviderResult<Option<Environment>> {
    let path = profile_path(env)?;
    let before = match fs::symlink_metadata(&path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(failure("profile_unreadable")),
    };
    check_parents(&path)?;
    private(&before, false)?;
    private(
        &fs::metadata(
            path.parent()
                .ok_or_else(|| failure("invalid_profile_path"))?,
        )
        .map_err(|_| failure("profile_unreadable"))?,
        true,
    )?;
    let file = File::open(&path).map_err(|_| failure("profile_unreadable"))?;
    let opened = file.metadata().map_err(|_| failure("profile_unreadable"))?;
    private(&opened, false)?;
    let after = fs::symlink_metadata(&path).map_err(|_| failure("profile_unreadable"))?;
    private(&after, false)?;
    check_parents(&path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if (before.dev(), before.ino()) != (opened.dev(), opened.ino())
            || (after.dev(), after.ino()) != (opened.dev(), opened.ino())
        {
            return Err(failure("unsafe_profile_path"));
        }
    }
    let mut bytes = Vec::new();
    file.take(16385)
        .read_to_end(&mut bytes)
        .map_err(|_| failure("profile_unreadable"))?;
    if bytes.len() > 16384 {
        return Err(failure("invalid_profile"));
    }
    let object: UniqueObject =
        serde_json::from_slice(&bytes).map_err(|_| failure("invalid_profile"))?;
    profile_environment(object.0).map(Some)
}

struct UniqueObject(BTreeMap<String, Value>);
impl<'de> Deserialize<'de> for UniqueObject {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct ObjectVisitor;
        impl<'de> Visitor<'de> for ObjectVisitor {
            type Value = UniqueObject;
            fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
                formatter.write_str("a provider profile object")
            }
            fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<Self::Value, M::Error> {
                let mut values = BTreeMap::new();
                while let Some((key, value)) = map.next_entry::<String, Value>()? {
                    if values.insert(key, value).is_some() {
                        return Err(de::Error::custom("duplicate profile field"));
                    }
                }
                Ok(UniqueObject(values))
            }
        }
        deserializer.deserialize_map(ObjectVisitor)
    }
}

fn profile_environment(object: BTreeMap<String, Value>) -> ProviderResult<Environment> {
    if object.get("version").and_then(Value::as_u64) != Some(1) || !object.contains_key("provider")
    {
        return Err(failure("invalid_profile"));
    }
    let mut env = Environment::new();
    for (key, value) in object {
        let name = match key.as_str() {
            "version" => continue,
            "provider" => "DECISION_BROWSER_PROVIDER",
            "endpoint" => "DECISION_BROWSER_ENDPOINT",
            "model" => "DECISION_BROWSER_MODEL",
            "max_choices" => {
                let number = value.as_u64().ok_or_else(|| failure("invalid_profile"))?;
                env.insert(
                    "DECISION_BROWSER_MAX_CHOICES".to_owned(),
                    number.to_string(),
                );
                continue;
            }
            "api_key" if value.is_null() => continue,
            "api_key" => "DECISION_BROWSER_API_KEY",
            _ => return Err(failure("invalid_profile")),
        };
        let text = value.as_str().ok_or_else(|| failure("invalid_profile"))?;
        if cfg!(windows) && name == "DECISION_BROWSER_API_KEY" && !text.is_empty() {
            return Err(failure("profile_credentials_unsupported"));
        }
        env.insert(name.to_owned(), text.to_owned());
    }
    settings(&env, None)?;
    Ok(env)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn env(pairs: &[(&str, &str)]) -> Environment {
        pairs
            .iter()
            .map(|(key, value)| ((*key).to_owned(), (*value).to_owned()))
            .collect()
    }
    #[test]
    fn default_and_aliases_resolve_without_provider_calls() {
        for (name, expected) in [
            ("openai", "luna"),
            ("typesafe", "jev"),
            ("jev", "jev"),
            ("gemini", "gemini"),
        ] {
            assert_eq!(
                settings(&env(&[("DECISION_BROWSER_PROVIDER", name)]), None)
                    .unwrap()
                    .provider
                    .name(),
                expected
            );
        }
        assert_eq!(settings(&env(&[]), None).unwrap().provider.name(), "luna");
    }
    #[test]
    fn explicit_key_and_named_environment_override_profile_key() {
        let profile = env(&[
            ("DECISION_BROWSER_PROVIDER", "typesafe"),
            ("DECISION_BROWSER_API_KEY", "profile-secret"),
        ]);
        assert_eq!(
            settings(
                &env(&[("TYPESAFE_API_KEY", "named-secret")]),
                Some(&profile)
            )
            .unwrap()
            .key
            .as_deref(),
            Some("named-secret")
        );
        assert_eq!(
            settings(
                &env(&[
                    ("TYPESAFE_API_KEY", "named-secret"),
                    ("DECISION_BROWSER_API_KEY", "explicit-secret")
                ]),
                Some(&profile)
            )
            .unwrap()
            .key
            .as_deref(),
            Some("explicit-secret")
        );
    }
    #[test]
    fn custom_routes_and_invalid_credentials_fail_closed() {
        for (key, value) in [
            (
                "DECISION_BROWSER_ENDPOINT",
                "https://elsewhere.example/v1/systemone",
            ),
            ("DECISION_BROWSER_MODEL", "other"),
            ("DECISION_BROWSER_API_KEY", "bad\nkey"),
            ("DECISION_BROWSER_MAX_CHOICES", "2"),
        ] {
            assert!(settings(&env(&[(key, value)]), None).is_err());
        }
    }
    #[test]
    fn custom_system_one_route_needs_explicit_credential_binding() {
        let selected = env(&[
            ("DECISION_BROWSER_PROVIDER", "typesafe"),
            (
                "DECISION_BROWSER_ENDPOINT",
                "https://selected.example/v1/systemone",
            ),
            ("DECISION_BROWSER_MODEL", "selected-model-v2"),
            ("DECISION_BROWSER_MAX_CHOICES", "42"),
            ("DECISION_BROWSER_API_KEY", "selected-test-key"),
        ]);
        let resolved = settings(&selected, None).unwrap();
        assert_eq!(resolved.endpoint, "https://selected.example/v1/systemone");
        assert_eq!(resolved.model, "selected-model-v2");
        assert_eq!(resolved.max_choices, 42);
        let mut wrong_key = selected.clone();
        wrong_key.remove("DECISION_BROWSER_API_KEY");
        wrong_key.insert("TYPESAFE_API_KEY".to_owned(), "preset-test-key".to_owned());
        assert_eq!(
            settings(&wrong_key, None).err().unwrap().to_string(),
            "credential_route_mismatch"
        );
        let profile = selected.clone();
        assert_eq!(
            settings(
                &env(&[("TYPESAFE_API_KEY", "preset-test-key")]),
                Some(&profile)
            )
            .err()
            .unwrap()
            .to_string(),
            "credential_route_mismatch"
        );
        let mut no_key = selected;
        no_key.remove("DECISION_BROWSER_API_KEY");
        let report = status(settings(&no_key, None), |_| {
            panic!("custom endpoint must never receive Keychain credentials")
        });
        assert_eq!(report["provider_ready"], false);
    }
    #[test]
    fn endpoint_validation_accepts_only_secure_or_literal_loopback_routes() {
        for value in [
            "http://public.example/chat",
            "https://@example.org/chat",
            "https://example.org/chat?secret=x",
            "https://example.org/chat#x",
            "https://example.org:/chat",
            "https://example.org/",
            "https://example.org/chat\\other",
            "http://127.0.0.1:0/chat",
        ] {
            assert!(validate_endpoint(value, false).is_err(), "{value}");
        }
        for value in [
            "http://127.0.0.2:9000/chat",
            "http://[::1]:9000/chat",
            "http://localhost:9000/chat",
            "https://selected.example/chat",
        ] {
            assert!(validate_endpoint(value, false).is_ok(), "{value}");
        }
        assert!(validate_endpoint("https://selected.example/chat", true).is_err());
    }
    #[test]
    fn independent_text_helper_reports_only_configuration() {
        let selected = env(&[
            ("DECISION_BROWSER_PROVIDER", "typesafe"),
            (
                "DECISION_BROWSER_TEXT_ENDPOINT",
                "http://127.0.0.1:9001/v1/chat/completions",
            ),
            ("DECISION_BROWSER_TEXT_MODEL", "helper-model"),
        ]);
        let report = status(settings(&selected, None), |_| false);
        assert_eq!(report["text_generation"], "configured");
        assert!(!report.to_string().contains("helper-model"));
        let mut remote = selected;
        remote.insert(
            "DECISION_BROWSER_TEXT_ENDPOINT".to_owned(),
            "https://helper.example/v1/chat/completions".to_owned(),
        );
        assert!(settings(&remote, None).is_err());
        remote.insert(
            "DECISION_BROWSER_TEXT_API_KEY".to_owned(),
            "independent-helper-key".to_owned(),
        );
        assert!(settings(&remote, None).is_ok());
        remote.insert("DECISION_BROWSER_PROVIDER".to_owned(), "openai".to_owned());
        assert!(settings(&remote, None).is_err());
    }
    #[test]
    fn status_contains_no_key_and_claims_only_supported_features() {
        let report = status(
            settings(&env(&[("OPENAI_API_KEY", "private-test-secret")]), None),
            |_| false,
        );
        assert_eq!(report["provider_ready"], true);
        assert!(!report.to_string().contains("private-test-secret"));
        assert_eq!(report["visual"], "unsupported");
        assert_eq!(
            status(settings(&env(&[]), None), |_| false)["provider_error"],
            "missing_api_key"
        );
    }
    #[test]
    fn duplicate_unknown_and_wrong_type_profile_fields_are_rejected() {
        assert!(
            serde_json::from_str::<UniqueObject>(
                r#"{"version":1,"provider":"openai","provider":"typesafe"}"#
            )
            .is_err()
        );
        for raw in [
            r#"{"version":true,"provider":"openai"}"#,
            r#"{"version":1,"provider":"openai","approval":true}"#,
            r#"{"version":1,"provider":"openai","max_choices":"255"}"#,
        ] {
            let parsed: UniqueObject = serde_json::from_str(raw).unwrap();
            assert!(profile_environment(parsed.0).is_err());
        }
    }
    #[cfg(unix)]
    #[test]
    fn private_saved_profile_loads_and_explicit_route_bypasses_it() {
        use std::os::unix::fs::PermissionsExt;
        use std::time::{SystemTime, UNIX_EPOCH};
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir()
            .canonicalize()
            .unwrap()
            .join(format!("native-profile-{}-{nonce}", std::process::id()));
        fs::create_dir(&root).unwrap();
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
        let path = root.join("provider.json");
        fs::write(
            &path,
            r#"{"version":1,"provider":"typesafe","api_key":"saved-test-key"}"#,
        )
        .unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        let selected = env(&[("DECISION_BROWSER_CONFIG_FILE", path.to_str().unwrap())]);
        let resolved = resolve(&selected).unwrap();
        assert_eq!(resolved.provider.name(), "jev");
        assert_eq!(resolved.key.as_deref(), Some("saved-test-key"));
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        assert_eq!(
            resolve(&selected).err().unwrap().to_string(),
            "unsafe_profile_permissions"
        );
        let explicit = env(&[
            ("DECISION_BROWSER_CONFIG_FILE", path.to_str().unwrap()),
            ("DECISION_BROWSER_PROVIDER", "openai"),
            ("OPENAI_API_KEY", "new-test-key"),
        ]);
        let resolved = resolve(&explicit).unwrap();
        assert_eq!(resolved.provider.name(), "luna");
        assert_eq!(resolved.key.as_deref(), Some("new-test-key"));
        fs::remove_file(&path).unwrap();
        std::os::unix::fs::symlink(root.join("missing"), &path).unwrap();
        assert!(resolve(&selected).is_err());
        fs::remove_file(path).unwrap();
        fs::remove_dir(root).unwrap();
    }
    #[test]
    fn keychain_presence_only_supplements_valid_matching_settings() {
        let report = status(
            settings(&env(&[("DECISION_BROWSER_PROVIDER", "typesafe")]), None),
            |provider| {
                assert_eq!(provider.name(), "jev");
                true
            },
        );
        assert_eq!(report["provider_ready"], true);
        assert_eq!(report["api_key_present"], true);
        assert!(report["provider_error"].is_null());
        let absent = status(
            settings(&env(&[("DECISION_BROWSER_PROVIDER", "gemini")]), None),
            |_| false,
        );
        assert_eq!(absent["provider_ready"], false);
        let invalid = status(
            settings(
                &env(&[(
                    "DECISION_BROWSER_ENDPOINT",
                    "https://elsewhere.example/v1/decisions",
                )]),
                None,
            ),
            |_| panic!("invalid routes must not query Keychain"),
        );
        assert_eq!(invalid["provider_ready"], false);
        let configured = status(
            settings(&env(&[("OPENAI_API_KEY", "private-test-secret")]), None),
            |_| panic!("an environment key needs no Keychain query"),
        );
        assert_eq!(configured["provider_ready"], true);
    }
    #[test]
    fn remaining_preset_readiness_and_keyless_local_match_fixed_routes() {
        for (provider, key_name) in [
            ("openrouter", "OPENROUTER_API_KEY"),
            ("opencode", "OPENCODE_API_KEY"),
        ] {
            let selected = env(&[
                ("DECISION_BROWSER_PROVIDER", provider),
                (key_name, "synthetic-test-key"),
            ]);
            let report = status(settings(&selected, None), |_| {
                panic!("explicit key must skip Keychain")
            });
            assert_eq!(report["provider"], provider);
            assert_eq!(report["api_key_present"], true);
            assert_eq!(report["provider_ready"], true);
            assert!(!report.to_string().contains("synthetic-test-key"));
        }
        for alias in ["local", "nimble"] {
            let selected = env(&[("DECISION_BROWSER_PROVIDER", alias)]);
            let report = status(settings(&selected, None), |_| {
                panic!("local never queries Keychain")
            });
            assert_eq!(report["provider"], "local");
            assert_eq!(report["api_key_present"], false);
            assert_eq!(report["provider_ready"], true);
            assert!(report["provider_error"].is_null());
            assert!(
                settings(
                    &env(&[
                        ("DECISION_BROWSER_PROVIDER", alias),
                        (
                            "DECISION_BROWSER_ENDPOINT",
                            "http://127.0.0.1:9000/v1/systemone"
                        )
                    ]),
                    None
                )
                .is_ok()
            );
        }
        assert!(
            settings(
                &env(&[
                    ("DECISION_BROWSER_PROVIDER", "openrouter"),
                    (
                        "DECISION_BROWSER_ENDPOINT",
                        "https://opencode.ai/inference/systemone/v1/systemone"
                    ),
                    ("OPENROUTER_API_KEY", "synthetic-test-key")
                ]),
                None
            )
            .is_err()
        );
    }
}
